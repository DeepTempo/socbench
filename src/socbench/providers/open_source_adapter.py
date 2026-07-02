"""Open-source / local inference adapter (OpenAI-compatible Chat Completions over HTTP).

Targets any server that speaks the OpenAI Chat Completions wire format:
Vertex AI endpoints (vLLM/TGI containers), Ollama, llama.cpp server, etc.

Key choices (kept deliberately parallel to the openai/anthropic/gemini adapters):

- Uses ``httpx`` directly instead of the ``openai`` SDK — lighter weight for
  containerised / batch jobs and removes a heavy dependency when the endpoint
  is self-hosted.
- POSTs to ``{base_url}/chat/completions`` with standard function-tool
  declarations; the Chat Completions API is universally supported by open-source
  inference stacks, unlike the newer Responses API.
- ``submit_assessment`` is declared alongside the regular tools; under
  ``force_final_answer=True`` the adapter sets
  ``tool_choice={"type": "function", "function": {"name": "submit_assessment"}}``
  so the model commits this turn.
- Reasoning budget: when ``request.reasoning_budget_tokens`` is set, the adapter
  sends ``max_tokens = max_output_tokens + reasoning_budget_tokens`` plus the vLLM
  ``thinking_token_budget`` sampling param. Paired with the server's
  ``--reasoning-parser`` (which routes the chain of thought into
  ``reasoning_content``), this gives self-hosted reasoning models the SAME budget
  semantics as the hosted APIs — ``max_output_tokens`` bounds the visible output,
  reasoning gets its own budget — instead of the CoT eating the output budget and
  truncating the tool call (``finish_reason=length`` → all-zero scores).
- Token usage is read from ``usage`` with reasoning/cache subsets handled the
  same way as the openai adapter: ``reasoning_tokens`` is split out of
  ``completion_tokens`` and ``cached_tokens`` out of ``prompt_tokens`` so the
  provider-agnostic cost formula bills each class exactly once. Local servers
  often omit the block, in which case we return zeroed counts.
- HTTP errors are mapped to the harness's ``RetryableAdapterError`` /
  ``FatalAdapterError`` so the agent loop can decide whether to back off
  and retry vs. fail the rendering. Read/connect timeouts are RETRYABLE — a
  slow open-source reasoning model that exceeds the per-request deadline must
  not be treated as a permanent failure.

Why the timeout is generous and configurable: self-hosted reasoning models
(QwQ-32B, Foundation-Sec-Reasoning, etc.) emit long chains of thought before
the tool call, and under high request concurrency each generation decodes
slowly. A short fixed deadline silently turns every rendering into a timeout →
``adapter_fatal`` → all-zero scores. The deadline therefore defaults high and
is overridable per deployment via ``OPEN_SOURCE_TIMEOUT_SECONDS``.

Environment variables:

- ``OPEN_SOURCE_BASE_URL`` — inference server base URL. Defaults to
  ``http://localhost:11434/v1`` (Ollama's OpenAI-compatible endpoint).
- ``OPEN_SOURCE_API_KEY`` — optional Bearer token sent as
  ``Authorization: Bearer <key>``. For Vertex AI endpoints this can be a
  short-lived GCP access token (``gcloud auth print-access-token``).
- ``OPEN_SOURCE_TIMEOUT_SECONDS`` — per-request read/write deadline in seconds.
  Defaults to ``600``. Set this to comfortably exceed the slowest expected
  single-turn generation at your serving concurrency.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import Any

from socbench.providers.base import (
    Adapter,
    AdapterRequest,
    AdapterResponse,
    AdapterToolCall,
    FatalAdapterError,
    RetryableAdapterError,
    TokenUsage,
)

log = logging.getLogger("socbench.providers.open_source")

# Per-request deadline default. Open-source reasoning models under concurrent
# serving routinely take minutes for a single turn; 600s leaves headroom while
# still bounding a genuinely wedged request. Override via env per deployment.
_DEFAULT_TIMEOUT_SECONDS = 600.0
# Connecting to a local/Vertex endpoint should be near-instant; cap the connect
# phase tightly so a dead endpoint fails fast instead of burning the full read
# budget. The long deadline applies to read/write (waiting on generation).
_CONNECT_TIMEOUT_SECONDS = 15.0
# Startup health-check (preflight) deadline. Short and fixed: a reachable
# endpoint answers /models near-instantly, and an unreachable one should fail
# the run fast rather than hang the operator at startup.
_PREFLIGHT_TIMEOUT_SECONDS = 10.0


def _http_module() -> Any:
    """Lazy import of httpx so the file is import-safe without it installed."""
    try:
        import httpx  # noqa: PLC0415
    except ImportError as exc:
        raise FatalAdapterError(
            "httpx is not installed. Install with `pip install httpx` or "
            '`pip install "socbench[providers]"`.'
        ) from exc
    return httpx


def _resolve_timeout() -> float:
    """Read the per-request deadline from the environment, with a safe default."""
    raw = os.environ.get("OPEN_SOURCE_TIMEOUT_SECONDS")
    if raw is None or raw == "":
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_SECONDS
    return val if val > 0 else _DEFAULT_TIMEOUT_SECONDS


def _thinking_budget_disabled() -> bool:
    """Kill-switch for the ``thinking_token_budget`` request param.

    Set ``OPEN_SOURCE_DISABLE_THINKING_BUDGET=1`` to stop sending the param while
    still widening ``max_tokens`` for reasoning. vLLM only enforces a thinking
    budget for some reasoner families (Qwen3 / DeepSeek / Nemotron3); a parser
    that doesn't support it may reject the param with a 400 and all-zero the run.
    This flag lets an operator fall back to a max_tokens-only budget (reasoning
    still separated by --reasoning-parser, just not hard-capped) with a single env
    flip — no image rebuild — if a given model/parser turns out not to support it.
    """
    raw = os.environ.get("OPEN_SOURCE_DISABLE_THINKING_BUDGET", "")
    return raw.strip().lower() in {"1", "true", "yes"}


def _flatten_transcript_enabled() -> bool:
    """Whether to collapse the multi-turn conversation into one user message.

    Set ``OPEN_SOURCE_FLATTEN_TRANSCRIPT=1`` for models whose chat template does
    NOT properly render an agentic transcript — i.e. it lacks a ``tool``-role
    branch (tool results silently dropped) and/or mangles prior assistant turns.
    Foundation-Sec-8B-Reasoning is exactly this case: its released template only
    renders system/user/assistant, has no tool-role branch, and compresses every
    non-last assistant turn to its last whitespace-delimited token — so the model
    never sees its own prior tool calls or any tool results, loops emitting prose,
    hits the turn cap, gets force-finalised, and scores all-zero.

    When enabled, the adapter delivers the entire investigation state as a single
    role-labelled ``user`` message after the system prompt, so the running
    transcript survives ANY template (it never relies on tool-role or structured
    ``tool_calls`` rendering). Leave it OFF for models with a proper tool-calling
    template (Qwen/QwQ → Seneca), which represent the multi-turn dialogue natively.
    """
    raw = os.environ.get("OPEN_SOURCE_FLATTEN_TRANSCRIPT", "")
    return raw.strip().lower() in {"1", "true", "yes"}


class OpenSourceAdapter(Adapter):
    """OpenAI-compatible Chat-Completions adapter for local / open-source inference.

    Requires ``httpx``. Connection details are pulled from the environment:

    - ``OPEN_SOURCE_BASE_URL`` — defaults to ``http://localhost:11434/v1``.
    - ``OPEN_SOURCE_API_KEY`` — optional Bearer token.
    - ``OPEN_SOURCE_TIMEOUT_SECONDS`` — per-request deadline (default 600s).
    - ``OPEN_SOURCE_DISABLE_THINKING_BUDGET`` — set to ``1`` to stop sending the
      ``thinking_token_budget`` param (fallback for parsers that reject it).

    The model identifier is pinned at construction time (driven by
    ``benchmark_config.yaml`` or the ``OPEN_SOURCE_MODEL`` env var the deploy
    entrypoint sets to vLLM's served-model name).
    """

    provider_name = "open_source"

    def __init__(self, model: str) -> None:
        self.model = model
        # Stash the module so invoke() can reference its exception classes
        # without repeating the lazy-import dance (mirrors GeminiAdapter._types).
        self._httpx = _http_module()

        base_url = os.environ.get("OPEN_SOURCE_BASE_URL", "http://localhost:11434/v1").rstrip("/")
        api_key = os.environ.get("OPEN_SOURCE_API_KEY")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # Stashed for preflight(), which probes the endpoint with a one-off
        # synchronous request rather than the async _client used by invoke().
        self._base_url = base_url
        self._headers = headers

        self._timeout_seconds = _resolve_timeout()
        # Whether to send the thinking_token_budget request param (default yes).
        # Operator kill-switch for parsers that reject it; see helper docstring.
        self._send_thinking_budget = not _thinking_budget_disabled()
        # Collapse the conversation into one user message for templates that
        # can't render an agentic transcript (e.g. Foundation-Sec-8B). Off by
        # default so models with a proper tool template (Qwen/QwQ) stay native.
        self._flatten_transcript = _flatten_transcript_enabled()
        timeout = self._httpx.Timeout(
            self._timeout_seconds,
            connect=min(_CONNECT_TIMEOUT_SECONDS, self._timeout_seconds),
        )
        self._client = self._httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=timeout
        )

    # -- Adapter contract ----------------------------------------------------

    async def invoke(self, request: AdapterRequest) -> AdapterResponse:
        httpx = self._httpx

        if self._flatten_transcript:
            api_messages = _messages_to_flat_format(
                request.messages, request.system_prompt
            )
        else:
            api_messages = _messages_to_chat_format(
                request.messages, request.system_prompt
            )
        tools = [_tool_schema_to_openai(t) for t in request.tool_schemas]
        # Names the content-fallback parser is allowed to surface as tool calls
        # (plus the submit tool). A bare JSON blob in `content` is only treated
        # as a call when its `name` is one the persona actually has — otherwise
        # prose that happens to contain `{...}` would spuriously dispatch.
        valid_tool_names = {
            t["name"] for t in request.tool_schemas if isinstance(t.get("name"), str)
        }

        # Reasoning-budget split: when a thinking budget is set, vLLM (run with
        # --reasoning-parser) emits the chain of thought into a separate
        # reasoning_content field. ``max_tokens`` still bounds the TOTAL
        # generation, so widen it to cover reasoning + visible output, and pass
        # ``thinking_token_budget`` to hard-cap the reasoning portion. The net
        # effect mirrors Gemini/Anthropic: ``max_output_tokens`` is the visible
        # output budget and reasoning gets its own, separate budget instead of
        # cannibalising it (the all-zero failure mode).
        reasoning_budget = request.reasoning_budget_tokens
        if reasoning_budget is not None and reasoning_budget > 0:
            max_tokens = request.max_output_tokens + reasoning_budget
        else:
            max_tokens = request.max_output_tokens

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": max_tokens,
        }
        if reasoning_budget is not None and reasoning_budget > 0 and self._send_thinking_budget:
            # vLLM sampling param: hard limit on reasoning tokens, enforced via a
            # logits processor that injects the think-end token at the cap.
            # Server-dependent: Ollama/llama.cpp ignore the unknown field, but
            # vLLM's V2 model runner REJECTS it with a 400 — the deploy entrypoint
            # pins reasoning models to the V1 runner (VLLM_USE_V2_MODEL_RUNNER=0)
            # so it's honoured. If a specific reasoner parser still rejects it,
            # OPEN_SOURCE_DISABLE_THINKING_BUDGET=1 drops the param (max_tokens
            # alone then bounds reasoning + output).
            payload["thinking_token_budget"] = reasoning_budget
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if tools:
            payload["tools"] = tools
            if request.force_final_answer and any(
                t["function"]["name"] == request.output_contract_tool_name for t in tools
            ):
                payload["tool_choice"] = {
                    "type": "function",
                    "function": {"name": request.output_contract_tool_name},
                }
            elif request.require_tool_call:
                # Gate: submit is withheld from ``tools`` upstream, so guided
                # decoding forces an investigative call instead of prose.
                payload["tool_choice"] = "required"
            else:
                payload["tool_choice"] = "auto"

        raw, elapsed_ms = await _post_chat_completion(self._client, httpx, payload)

        parsed = _parse_chat_response(
            raw,
            request.output_contract_tool_name,
            elapsed_ms,
            valid_tool_names=valid_tool_names,
        )
        _debug_dump(request, raw, parsed)
        return parsed

    # -- Startup health check ------------------------------------------------

    def preflight(self) -> list[str]:
        """Fail fast on a misconfigured endpoint before the run starts.

        Pings ``{base_url}/models`` once. A connection-level failure (dead
        endpoint, wrong host/port) or an auth rejection is FATAL — every
        chat/completions call would fail the same way and the whole run would
        come back all-zero, which is exactly the failure mode this guards
        against. A reachable endpoint that simply doesn't implement /models, or
        returns a transient 5xx, yields a soft warning instead. When /models
        does list the served models, a configured ``model`` that is absent from
        the list is surfaced as a warning (it may be a legitimate alias, but is
        far more often the wrong served-model id that 404s every call — the bug
        that produced the original all-zero runs).
        """
        httpx = self._httpx
        url = f"{self._base_url}/models"
        try:
            resp = httpx.get(
                url, headers=self._headers, timeout=_PREFLIGHT_TIMEOUT_SECONDS
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise FatalAdapterError(
                f"open_source endpoint unreachable at {self._base_url} ({exc}). "
                "Start the inference server or point OPEN_SOURCE_BASE_URL at a "
                "reachable endpoint. Re-run with --skip-preflight to bypass."
            ) from exc
        except httpx.HTTPError as exc:
            # Reached the network but the probe itself errored oddly; don't doom
            # the run on a probe quirk — the per-request path has its own retries.
            return [f"open_source preflight could not probe {url} ({exc}); skipped model check"]

        status = resp.status_code
        if status in (401, 403):
            raise FatalAdapterError(
                f"open_source endpoint rejected authentication at {self._base_url} "
                f"(HTTP {status}). Check OPEN_SOURCE_API_KEY — every chat/completions "
                "call would fail the same way, producing an all-zero run."
            )
        if status >= 500:
            return [
                f"open_source preflight: {url} returned HTTP {status}; the server "
                "may be unhealthy (continuing — per-request retries still apply)."
            ]
        if status == 200:
            served = _served_model_ids(resp)
            if served and self.model not in served:
                return [
                    f"open_source: configured model {self.model!r} is not among the "
                    f"served models {sorted(served)}. Unless it is an alias, every "
                    "chat/completions call will 404 and the run will be all-zero — "
                    "set OPEN_SOURCE_MODEL to a served id."
                ]
        # Any other status (e.g. 404/405 on /models) proves reachability but
        # gives us nothing to check — treat as a clean pass.
        return []


# ---------------------------------------------------------------------------
# Helpers — HTTP transport
# ---------------------------------------------------------------------------


async def _post_chat_completion(
    client: Any, httpx: Any, payload: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """POST one ``/chat/completions`` request, mapping the outcome to the
    harness's Retryable/Fatal errors and returning ``(raw_json, elapsed_ms)``.

    One self-healing retry: if the server 400s WHILE we sent
    ``thinking_token_budget`` (some runners/reasoner families — e.g. QwQ on
    the V2 runner — reject the param), strip it and retry ONCE so the
    rendering succeeds on a max_tokens-only budget instead of failing the
    whole run. Any other 400 is a genuine fatal. This removes the need to
    guess per-model whether the param is supported. 429 and 5xx are
    retryable; read/connect timeouts and other transport hiccups are
    retryable too — a slow reasoning model that blows the read deadline
    should back off and retry, not fail the rendering outright.
    """
    start = time.monotonic()
    while True:
        try:
            response = await client.post("/chat/completions", json=payload)
            response.raise_for_status()
            raw = response.json()
            break
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            body = exc.response.text
            if status == 429:
                # Try to extract a Retry-After header; fall back to None.
                retry_after = exc.response.headers.get("retry-after")
                try:
                    retry_seconds = float(retry_after) if retry_after is not None else None
                except (TypeError, ValueError):
                    retry_seconds = None
                raise RetryableAdapterError(
                    f"open_source {status} rate limit: {body}",
                    retry_after_seconds=retry_seconds,
                ) from exc
            if status >= 500:
                raise RetryableAdapterError(
                    f"open_source {status} server error: {body}"
                ) from exc
            if status == 400 and "thinking_token_budget" in payload:
                log.warning(
                    "open_source_thinking_budget_unsupported",
                    extra={"body": body[:300]},
                )
                del payload["thinking_token_budget"]
                continue
            raise FatalAdapterError(f"open_source {status} error: {body}") from exc
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RetryableAdapterError(f"open_source transport error: {exc}") from exc
        except httpx.HTTPError as exc:
            raise FatalAdapterError(f"open_source HTTP error: {exc}") from exc
    elapsed_ms = int((time.monotonic() - start) * 1000)
    return raw, elapsed_ms


# ---------------------------------------------------------------------------
# Helpers — request shaping
# ---------------------------------------------------------------------------


def _messages_to_chat_format(messages: list[Any], system_prompt: str) -> list[dict[str, Any]]:
    """Translate the harness conversation into OpenAI Chat Completions messages.

    The system prompt is prepended as a ``system`` message. Assistant messages
    carrying a ``tool_call`` become assistant messages with ``tool_calls``;
    ``role="tool"`` messages become ``role="tool"`` items referencing the
    preceding call by ``tool_call_id``.
    """
    out: list[dict[str, Any]] = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})

    for msg in messages:
        if msg.role == "user":
            out.append({"role": "user", "content": msg.content})
        elif msg.role == "assistant":
            chat_msg: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_call is not None:
                chat_msg["tool_calls"] = [
                    {
                        "id": msg.tool_call.id,
                        "type": "function",
                        "function": {
                            "name": msg.tool_call.name,
                            "arguments": json.dumps(msg.tool_call.args, sort_keys=True),
                        },
                    }
                ]
                # Some local servers complain when content is present alongside
                # tool_calls; omit empty-string content to stay safe.
                if not msg.content:
                    del chat_msg["content"]
            out.append(chat_msg)
        elif msg.role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.tool_use_id or "",
                    "content": msg.content,
                }
            )
    return out


def _messages_to_flat_format(
    messages: list[Any], system_prompt: str
) -> list[dict[str, Any]]:
    """Render the whole conversation as ``system`` + a single ``user`` message.

    Template-agnostic transport for models whose chat template can't represent an
    agentic transcript (no tool-role branch, mangled assistant history). Every
    turn is folded, role-labelled, into one user block so the model sees the full
    investigation state — its own prior tool calls and every tool result —
    regardless of what the served template does with ``tool``/``tool_calls``.

    The model's NEXT action is still parsed from its fresh generation by
    :func:`_extract_tool_call_from_content`; flattening only shapes the INPUT, so
    it never interferes with how the live tool call is read back.
    """
    out: list[dict[str, Any]] = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})

    lines: list[str] = []
    for msg in messages:
        if msg.role == "user":
            if msg.content:
                lines.append(msg.content)
        elif msg.role == "assistant":
            if msg.content:
                lines.append(msg.content)
            if msg.tool_call is not None:
                args = json.dumps(msg.tool_call.args, sort_keys=True)
                lines.append(
                    f"[assistant called tool `{msg.tool_call.name}` "
                    f"with arguments {args}]"
                )
        elif msg.role == "tool":
            lines.append(f"[tool result]\n{msg.content}")

    out.append({"role": "user", "content": "\n\n".join(lines)})
    return out


def _tool_schema_to_openai(item: dict[str, Any]) -> dict[str, Any]:
    """Convert one harness tool entry to OpenAI Chat Completions function-tool shape."""
    return {
        "type": "function",
        "function": {
            "name": item["name"],
            "description": item.get("description", ""),
            "parameters": item["schema"],
        },
    }


# ---------------------------------------------------------------------------
# Helpers — response parsing
# ---------------------------------------------------------------------------


def _native_tool_call_from_message(
    message: dict[str, Any], submit_tool_name: str
) -> tuple[AdapterToolCall | None, dict[str, Any] | None]:
    """Surface the first native ``tool_calls`` entry, if any.

    Returns ``(tool_call, submit_args)`` — at most one non-``None`` — mirroring
    :func:`_extract_tool_call_from_content`'s return shape so both the native
    and content-fallback paths in :func:`_parse_chat_response` compose the
    same way.
    """
    for tc in message.get("tool_calls", []) or []:
        if tc.get("type") != "function":
            continue
        func = tc.get("function", {})
        name = func.get("name", "")
        args = _json_loads(func.get("arguments", "{}"))
        if name == submit_tool_name:
            return None, args
        return AdapterToolCall(id=tc.get("id", ""), name=name, args=args), None
    return None, None


def _parse_chat_response(
    raw: dict[str, Any],
    submit_tool_name: str,
    elapsed_ms: int,
    valid_tool_names: set[str] | None = None,
) -> AdapterResponse:
    """Translate a Chat Completions JSON response into :class:`AdapterResponse`.

    Surfaces the first ``tool_calls`` entry (the agent loop dispatches one
    tool per turn) and accumulates ``content`` text. If the surfaced tool is
    ``submit_assessment``, populate ``submit_assessment_args`` instead of
    ``tool_call`` so the agent loop's final-answer path runs.

    Reasoning models served with vLLM's ``--reasoning-parser`` return the chain
    of thought in a separate ``message.reasoning_content`` field, leaving
    ``content`` as the clean post-reasoning answer and the tool call in its
    normal ``tool_calls`` field — which is exactly what makes tool calls parse
    for these models. ``reasoning_content`` is NOT folded into ``text`` (it is
    not the model's answer); it is stashed in ``provider_raw`` for diagnostics
    only. Its tokens are already accounted as ``reasoning_tokens`` in usage.

    When the model produced neither a tool call nor a submission, the API's own
    ``finish_reason`` is preserved as ``"length"`` (budget exhausted mid-output
    — common for reasoning models given too small a ``max_tokens``) vs ``"stop"``
    so the empty-turn case is diagnosable rather than silently uniform.
    """
    text_parts: list[str] = []
    tool_call: AdapterToolCall | None = None
    submit_args: dict[str, Any] | None = None

    choices = raw.get("choices", []) or []
    choice = choices[0] if choices else {}
    message = choice.get("message", {}) if isinstance(choice, dict) else {}

    content = message.get("content")
    if isinstance(content, str) and content:
        text_parts.append(content)

    # vLLM reasoning separation: surfaced for diagnostics, never treated as the
    # answer text. Absent on servers without a reasoning parser (e.g. Ollama).
    reasoning_content = message.get("reasoning_content")
    provider_raw = (
        {"reasoning_content": reasoning_content}
        if isinstance(reasoning_content, str) and reasoning_content
        else None
    )

    tool_call, submit_args = _native_tool_call_from_message(message, submit_tool_name)

    # Content fallback: some open-source models emit tool calls inside `content`
    # (free-form JSON, ```fenced``` JSON, <tool_call> tags, <|python_tag|>, etc.)
    # because their chat template has no native tool-calling format and no vLLM
    # tool-call parser matches what they produce — so `tool_calls` comes back
    # empty even though the model DID call a tool. Foundation-Sec-8B is the
    # canonical case (no <toolcall> token exists; the team ships no tool parser).
    # Only fire when native parsing found nothing, and only surface a known tool
    # name, so this is a strict safety net that can't perturb models whose native
    # parsing already works (Qwen/QwQ via hermes).
    fallback_used = False
    if tool_call is None and submit_args is None and isinstance(content, str) and content:
        fb_tool, fb_submit = _extract_tool_call_from_content(
            content, valid_tool_names or set(), submit_tool_name
        )
        if fb_submit is not None:
            submit_args = fb_submit
            fallback_used = True
        elif fb_tool is not None:
            tool_call = fb_tool
            fallback_used = True

    if fallback_used:
        # Tripwire: record exactly when/what the content-fallback recovered so an
        # operator can quantify how much of a model's signal bypassed native
        # parsing (a methodological caveat for the run write-up), and confirm the
        # extractor isn't misfiring on prose.
        log.warning(
            "open_source_content_fallback",
            extra={"recovered": "submit" if submit_args is not None else "tool_call"},
        )
        provider_raw = {**(provider_raw or {}), "content_fallback": True}

    if submit_args is not None:
        finish = "submit_assessment"
    elif tool_call is not None:
        finish = "tool_call"
    elif choice.get("finish_reason") == "length":
        # Output budget exhausted before any tool call surfaced.
        finish = "length"
    else:
        finish = "stop"

    return AdapterResponse(
        finish_reason=finish,  # type: ignore[arg-type]
        text="\n".join(text_parts),
        tool_call=tool_call,
        submit_assessment_args=submit_args,
        usage=_extract_chat_usage(raw),
        wall_time_ms=elapsed_ms,
        provider_raw=provider_raw,
    )


def _extract_chat_usage(raw: dict[str, Any]) -> TokenUsage:
    """Read token usage from a Chat Completions JSON response.

    Open-source servers often omit ``usage`` entirely; return zeroes in that
    case. When present, reasoning/cache subsets are split out the same way the
    openai adapter does so the cost formula bills each class exactly once:

    - ``prompt_tokens`` reported by the server is the TOTAL input; the
      ``prompt_tokens_details.cached_tokens`` subset (vLLM prefix cache) is
      moved into ``cached_tokens`` and removed from ``prompt_tokens``.
    - ``completion_tokens`` is the TOTAL output; the
      ``completion_tokens_details.reasoning_tokens`` subset is moved into
      ``reasoning_tokens`` and removed from ``output_tokens``.
    """
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return TokenUsage()

    prompt = int(usage.get("prompt_tokens", 0) or 0)
    output = int(usage.get("completion_tokens", 0) or 0)

    cached = 0
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        cached = int(prompt_details.get("cached_tokens", 0) or 0)

    reasoning = 0
    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, dict):
        reasoning = int(completion_details.get("reasoning_tokens", 0) or 0)

    return TokenUsage(
        prompt_tokens=max(0, prompt - cached),
        cached_tokens=cached,
        output_tokens=max(0, output - reasoning),
        reasoning_tokens=reasoning,
    )


def _served_model_ids(resp: Any) -> set[str]:
    """Extract served model ids from an OpenAI ``/models`` list response.

    Returns an empty set when the body is missing or malformed so the caller
    simply skips the served-model check rather than failing on a probe quirk.
    """
    try:
        data = resp.json()
    except Exception:  # any decode/shape error → skip the check
        return set()
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return set()
    out: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            mid = item.get("id")
            if isinstance(mid, str) and mid:
                out.add(mid)
    return out


def _debug_dump(request: Any, raw: dict[str, Any], parsed: Any) -> None:
    """Append a per-turn diagnostic record when OPEN_SOURCE_DEBUG_DUMP is set.

    No-op unless the env var names a writable JSONL path. Captures the raw
    model content + finish_reason + whether a tool_call / submit surfaced, so
    an all-zero run can be diagnosed (truncated reasoning vs. un-parseable
    final answer vs. server-side context truncation). Pure diagnostic; never
    affects the run when the env var is unset.
    """
    path = os.environ.get("OPEN_SOURCE_DEBUG_DUMP")
    if not path:
        return
    choices = raw.get("choices", []) or []
    choice = choices[0] if choices else {}
    message = choice.get("message", {}) if isinstance(choice, dict) else {}
    record = {
        "n_messages_in": len(request.messages),
        "force_final": request.force_final_answer,
        "max_output_tokens": request.max_output_tokens,
        "reasoning_budget_tokens": request.reasoning_budget_tokens,
        "api_finish_reason": choice.get("finish_reason"),
        "parsed_finish_reason": parsed.finish_reason,
        "raw_tool_calls": message.get("tool_calls"),
        "has_tool_call": parsed.tool_call is not None,
        "tool_call_name": parsed.tool_call.name if parsed.tool_call else None,
        "has_submit": parsed.submit_assessment_args is not None,
        "submit_args": parsed.submit_assessment_args,
        "content_fallback": bool((parsed.provider_raw or {}).get("content_fallback")),
        "usage": raw.get("usage"),
        "reasoning_content": message.get("reasoning_content"),
        "content": parsed.text,
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str))
            f.write("\n")
    except OSError:
        pass


def _json_loads(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


# ---------------------------------------------------------------------------
# Helpers — content-fallback tool-call extraction
# ---------------------------------------------------------------------------

# Literal (non-special-token) reasoning wrappers some models print inline. We
# strip a leading/closing think block before scanning so JSON the model merely
# *drafted* while reasoning isn't mistaken for its committed call.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_CLOSE_RE = re.compile(r"^.*?</think>", re.IGNORECASE | re.DOTALL)
# Tagged tool-call wrappers, both spellings (hermes <tool_call>, sec-style
# <toolcall>) plus the <function=NAME>…</function> form. Capture the body.
_TAGGED_CALL_RES = (
    re.compile(r"<tool_?call>\s*(\{.*?\})\s*</tool_?call>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<function=[^>]*>\s*(\{.*?\})\s*</function>", re.IGNORECASE | re.DOTALL),
)
# Fenced code blocks (```json … ``` or bare ``` … ```), body captured.
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.IGNORECASE | re.DOTALL)
# Llama tool sentinel; the call JSON follows it.
_PYTHON_TAG_RE = re.compile(r"<\|python_tag\|>\s*(\{.*\})", re.DOTALL)


def _strip_reasoning(content: str) -> str:
    """Drop inline ``<think>`` reasoning so we scan only the committed answer."""
    stripped = _THINK_BLOCK_RE.sub("", content)
    if _THINK_CLOSE_RE.search(stripped):
        # Unbalanced close (vLLM consumed the open tag): keep text after the close.
        stripped = _THINK_CLOSE_RE.sub("", stripped)
    return stripped.strip()


def _iter_json_objects(text: str) -> list[dict[str, Any]]:
    """Yield every balanced top-level ``{...}`` object decodable as a JSON dict.

    A brace scanner that respects string quoting/escapes — robust to objects
    embedded in prose and to multiple objects in one blob (e.g. a reasoning
    sketch followed by the real call).
    """
    objs: list[dict[str, Any]] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    quote = ""
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_str = False
            continue
        if ch in ('"', "'"):
            in_str = True
            quote = ch
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = _json_loads(text[start : i + 1])
                    if candidate:
                        objs.append(candidate)
                    start = -1
    return objs


_CALL_CONTROL_KEYS = {
    "name", "tool", "function", "type", "arguments", "parameters", "args", "input"
}

# Fingerprint for a name-less submit_assessment payload. Both required, so the
# detector stays off planning sketches; only submit carries a ``verdict``.
_SUBMIT_FINGERPRINT_KEYS = ("verdict", "rationale")
# Project a recovered payload onto the contract: SubmitAssessment is extra="forbid",
# so a stray key would otherwise fail validation and discard a good verdict.
_SUBMIT_SCHEMA_KEYS = frozenset(
    {"verdict", "confidence", "malicious_flow_indices", "malicious_destinations", "rationale"}
)


def _call_from_obj(
    obj: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    """Coerce a parsed object into a ``(name, args)`` tool call if it looks like one.

    Tolerant of the shapes free-form models emit: name under ``name``/``tool``/
    ``function``; args under ``arguments``/``parameters``/``args``/``input`` (the
    ``args`` alias avoids double-nesting ``{"name":...,"args":{...}}``); or args flat
    alongside ``name`` (the non-control keys), so a flat submission still validates.
    """
    name = obj.get("name")
    if not isinstance(name, str) or not name:
        for alt in ("tool", "function"):
            cand = obj.get(alt)
            if isinstance(cand, str) and cand:
                name = cand
                break
    if not isinstance(name, str) or not name:
        return None

    args: Any = None
    for alt in ("arguments", "parameters", "args", "input"):
        cand = obj.get(alt)
        if cand is not None:
            args = cand
            break
    if isinstance(args, str):
        args = _json_loads(args)
    if not isinstance(args, dict):
        # Flat form: everything that isn't a control key is the args payload.
        args = {k: v for k, v in obj.items() if k not in _CALL_CONTROL_KEYS}
    return name, args


def _submit_payload_from_obj(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Recognise a name-less ``submit_assessment`` payload — a bare
    ``{"verdict":..., "rationale":...}`` final answer with no name wrapper, which
    :func:`_call_from_obj` rejects. Matched by fingerprint, never when it's a named
    call, so it can't shadow a model that wraps its call properly.
    """
    if set(obj.keys()) & {"name", "tool", "function"}:
        return None
    if not all(
        isinstance(obj.get(k), str) and obj.get(k) for k in _SUBMIT_FINGERPRINT_KEYS
    ):
        return None
    return {k: v for k, v in obj.items() if k in _SUBMIT_SCHEMA_KEYS}


def _extract_tool_call_from_content(
    content: str, valid_names: set[str], submit_name: str
) -> tuple[AdapterToolCall | None, dict[str, Any] | None]:
    """Recover a tool call a model emitted as text instead of native ``tool_calls``.

    Returns ``(tool_call, submit_args)`` — at most one non-None. A candidate is
    only accepted when its ``name`` is a tool the persona actually has (or the
    submit tool), so prose containing stray JSON can't trigger a dispatch.

    Search order favours the most explicit signal first (tagged wrappers, then
    fenced blocks, then the Llama python_tag, then any bare JSON object), which
    keeps a model's deliberately-formatted call ahead of an offhand JSON snippet
    in its reasoning.
    """
    text = _strip_reasoning(content)
    allowed = valid_names | {submit_name}

    ordered_bodies: list[str] = []
    for rx in _TAGGED_CALL_RES:
        ordered_bodies.extend(rx.findall(text))
    ordered_bodies.extend(_FENCE_RE.findall(text))
    ordered_bodies.extend(_PYTHON_TAG_RE.findall(text))

    candidates: list[dict[str, Any]] = []
    for body in ordered_bodies:
        obj = _json_loads(body)
        if obj:
            candidates.append(obj)
    # Bare objects last (lowest priority); also covers the case where the whole
    # content IS the JSON call with no wrapper at all.
    candidates.extend(_iter_json_objects(text))

    for obj in candidates:
        call = _call_from_obj(obj)
        if call is not None:
            name, args = call
            if name in allowed:
                if name == submit_name:
                    return None, args
                return (
                    AdapterToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=name, args=args),
                    None,
                )
        # No named call — fall back to a name-less submit payload.
        submit = _submit_payload_from_obj(obj)
        if submit is not None:
            return None, submit
    return None, None


__all__ = ["OpenSourceAdapter"]
