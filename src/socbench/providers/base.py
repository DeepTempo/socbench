"""Provider-neutral types + adapter contract + factory.

Every provider adapter normalises its SDK-specific call into one of two
shapes, defined here:

- :class:`AdapterRequest`:  what the agent loop hands to the provider
  (system scaffold, conversation, tool schemas, output contract, sampling
  knobs). The system + tool_schemas + output_contract block is treated as
  the stable cacheable prefix.
- :class:`AdapterResponse`: what the provider returns to the agent loop
  (text and/or a tool_use, finish reason, and a complete token-usage block
  including `cached_tokens` so the cost model can attribute cache savings).

The agent loop never imports `openai` / `anthropic` / `google-genai`. Real
adapters do, lazily, behind :func:`build_adapter`.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AdapterError(RuntimeError):
    """Base class for adapter-side failures."""


class RetryableAdapterError(AdapterError):
    """Transient failure: agent loop SHOULD retry with backoff.

    ``retry_after_seconds`` carries the provider's ``Retry-After`` hint when
    present (e.g. on HTTP 429), so the agent loop can wait the advised time
    instead of a blind exponential backoff.
    """

    def __init__(self, *args: Any, retry_after_seconds: float | None = None) -> None:
        super().__init__(*args)
        self.retry_after_seconds = retry_after_seconds


class FatalAdapterError(AdapterError):
    """Permanent failure: agent loop MUST mark the rendering invalid."""


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------

MessageRole = Literal["user", "assistant", "tool"]
FinishReason = Literal["tool_call", "submit_assessment", "stop", "length", "error"]


class AdapterToolCall(BaseModel):
    """A model-issued tool call surfaced by the adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """One message in the rolling conversation handed to the adapter.

    ``role="tool"`` messages carry the JSON-serialised result of a previously
    requested tool call; ``tool_use_id`` ties the result back to the call.
    ``role="assistant"`` messages with a ``tool_call`` field represent a
    model-issued tool invocation from a prior turn (echoed back to the
    provider so the next turn is well-formed).
    """

    model_config = ConfigDict(extra="forbid")

    role: MessageRole
    content: str = ""
    tool_call: AdapterToolCall | None = None
    tool_use_id: str | None = None


class AdapterRequest(BaseModel):
    """Inputs to one adapter call (one turn in the agent loop).

    The ``system_prompt + tool_schemas + output_contract_schema`` block is
    the cacheable stable prefix; adapters that support explicit
    caching (Anthropic ``cache_control``, Gemini cached content) attach the
    cache hint to this prefix.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    system_prompt: str
    messages: list[Message]
    tool_schemas: list[dict[str, Any]]
    output_contract_schema: dict[str, Any]
    output_contract_tool_name: str = "submit_assessment"
    max_output_tokens: Annotated[int, Field(ge=1)] = 2048
    # None → omit the sampling param entirely. Newer reasoning models
    # (e.g. Claude Opus 4.x) reject ``temperature``; only send it when a
    # provider is explicitly configured with one.
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] | None = None
    force_final_answer: bool = False  # set when budget is exhausted


class TokenUsage(BaseModel):
    """Token counts returned by the provider for one call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_tokens: Annotated[int, Field(ge=0)] = 0
    cached_tokens: Annotated[int, Field(ge=0)] = 0  # subset of prompt_tokens served from cache
    output_tokens: Annotated[int, Field(ge=0)] = 0
    reasoning_tokens: Annotated[int, Field(ge=0)] = 0


class AdapterResponse(BaseModel):
    """Outputs from one adapter call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    finish_reason: FinishReason
    text: str = ""
    tool_call: AdapterToolCall | None = None
    submit_assessment_args: dict[str, Any] | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    wall_time_ms: Annotated[int, Field(ge=0)] = 0
    provider_raw: dict[str, Any] | None = None  # opaque debug payload; not persisted by default


# ---------------------------------------------------------------------------
# Adapter contract
# ---------------------------------------------------------------------------


class Adapter(ABC):
    """Provider adapter contract.

    Subclasses implement ``invoke``; the agent loop is fully provider-agnostic
    above this line.
    """

    provider_name: str  # set by subclasses; matches the key in benchmark_config.providers
    model: str          # the pinned model identifier

    @abstractmethod
    async def invoke(self, request: AdapterRequest) -> AdapterResponse:
        """Issue one call to the provider and return a normalised response.

        Implementations MUST:
          - measure wall-clock time and populate ``response.wall_time_ms``
          - populate ``usage`` from whatever the provider returns (zeros if
            the SDK doesn't surface a given field)
          - raise :class:`RetryableAdapterError` on transient failures
            (HTTP 429/5xx, timeouts, parse errors that retry might fix)
          - raise :class:`FatalAdapterError` on permanent failures
            (auth, invalid request, unsupported model)

        Adapters are async so the agent loop can keep hundreds of renderings
        in flight on one event loop. The underlying SDK clients are async
        (``AsyncOpenAI``, ``AsyncAnthropic``, ``genai.Client().aio``).
        """

    def invoke_sync(self, request: AdapterRequest) -> AdapterResponse:
        """Blocking convenience wrapper around :meth:`invoke`.

        For one-off / synchronous callers (tests, simple scripts). MUST NOT be
        called from within a running event loop; the async :meth:`invoke` is
        the path the Runner uses.
        """
        return asyncio.run(self.invoke(request))

    def reset(self) -> None:
        """Clear any per-rendering adapter state.

        Real adapters are stateless API clients; the default no-op is
        correct. The mock adapter overrides to reset its script cursor so
        one instance can drive many renderings.
        """
        return None


def _now_ms() -> int:
    """Monotonic wall-time accessor; carved out so tests can monkeypatch it."""
    return int(time.monotonic() * 1000)


def parse_retry_after(exc: Any) -> float | None:
    """Best-effort extract of a ``Retry-After`` header (in seconds) from an SDK
    error. Returns ``None`` when absent or in HTTP-date form (callers then fall
    back to exponential backoff).
    """
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    try:
        val = headers.get("retry-after")
    except Exception:
        return None
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Adapter factory + registry
# ---------------------------------------------------------------------------

# Adapters register themselves lazily. The factory imports the matching
# module on first use, which is the only place we touch the optional SDKs.

_AdapterFactory = Callable[[str], Adapter]
_FACTORIES: dict[str, _AdapterFactory] = {}


def register_adapter(name: str, factory: _AdapterFactory) -> None:
    """Register a factory under ``name``. Idempotent for the same callable."""
    existing = _FACTORIES.get(name)
    if existing is not None and existing is not factory:
        raise ValueError(f"adapter {name!r} already registered to a different factory")
    _FACTORIES[name] = factory


def list_known_providers() -> list[str]:
    """All provider names recognised by :func:`build_adapter` (lazy + eager)."""
    return sorted(set(_FACTORIES) | {"openai", "anthropic", "gemini", "open_source", "mock"})


def build_adapter(provider: str, model: str) -> Adapter:
    """Return an adapter for ``provider`` pinned to ``model``.

    The first call to a real provider imports its SDK; the mock adapter
    has no SDK dependency. Unknown providers raise :class:`ValueError` with
    the list of known names.
    """
    if provider in _FACTORIES:
        return _FACTORIES[provider](model)

    # Lazy imports keep optional SDKs optional.
    if provider == "mock":
        from socbench.providers.mock_adapter import MockAdapter

        register_adapter("mock", MockAdapter)
        return MockAdapter(model)
    if provider == "openai":
        from socbench.providers.openai_adapter import OpenAIAdapter

        register_adapter("openai", OpenAIAdapter)
        return OpenAIAdapter(model)
    if provider == "anthropic":
        from socbench.providers.anthropic_adapter import AnthropicAdapter

        register_adapter("anthropic", AnthropicAdapter)
        return AnthropicAdapter(model)
    if provider == "gemini":
        from socbench.providers.gemini_adapter import GeminiAdapter

        register_adapter("gemini", GeminiAdapter)
        return GeminiAdapter(model)
    if provider == "open_source":
        from socbench.providers.open_source_adapter import OpenSourceAdapter

        register_adapter("open_source", OpenSourceAdapter)
        return OpenSourceAdapter(model)

    raise ValueError(
        f"unknown provider {provider!r}; known: {list_known_providers()}"
    )


__all__ = [
    "Adapter",
    "AdapterError",
    "AdapterRequest",
    "AdapterResponse",
    "AdapterToolCall",
    "FatalAdapterError",
    "FinishReason",
    "Message",
    "MessageRole",
    "RetryableAdapterError",
    "TokenUsage",
    "_now_ms",
    "build_adapter",
    "list_known_providers",
    "parse_retry_after",
    "register_adapter",
]
