"""OpenAI Responses API adapter.

Written against the openai Python package guide fetched via chub
(``openai/package`` at v2.26.0, model guidance current as of April 2026).

Key choices:

- Uses :meth:`OpenAI().responses.create` per the SDK's "prefer Responses API
  for new code" guidance.
- Every available tool (including ``submit_assessment``) is declared as a
  Responses-API function tool. Under ``force_final_answer=True``, the
  adapter sets ``tool_choice`` to force the ``submit_assessment`` tool, so
  the model commits within one more turn (force-final behaviour).
- Errors are mapped to the harness's ``RetryableAdapterError`` /
  ``FatalAdapterError`` so the agent loop can decide whether to back off
  and retry vs. fail the rendering.

The SDK is imported lazily inside ``__init__``; constructing the adapter
without ``openai`` installed raises a clear :class:`FatalAdapterError`
rather than a stack trace at import time.
"""
from __future__ import annotations

import os
import time
from typing import Any

from socbench.providers.base import (
    Adapter,
    AdapterRequest,
    AdapterResponse,
    AdapterToolCall,
    FatalAdapterError,
    RetryableAdapterError,
    TokenUsage,
    parse_retry_after,
)


class OpenAIAdapter(Adapter):
    """OpenAI Responses-API adapter.

    Requires ``OPENAI_API_KEY`` in the environment. The model identifier is
    pinned at construction time (driven by ``benchmark_config.yaml``).
    """

    provider_name = "openai"

    def __init__(self, model: str) -> None:
        self.model = model
        try:
            from openai import AsyncOpenAI  # noqa: PLC0415
        except ImportError as exc:
            raise FatalAdapterError(
                "openai SDK not installed. Install with `pip install \"socbench[providers]\"` "
                "or `pip install openai`."
            ) from exc
        if not os.environ.get("OPENAI_API_KEY"):
            raise FatalAdapterError(
                "OPENAI_API_KEY is not set. Export it before running, e.g. "
                "`export OPENAI_API_KEY=sk-...`."
            )
        # The SDK reads OPENAI_API_KEY from env automatically; we just construct.
        # The async client binds its httpx.AsyncClient lazily on first request,
        # so constructing it outside an event loop here is fine.
        self._client = AsyncOpenAI()

    # -- Adapter contract ----------------------------------------------------

    async def invoke(self, request: AdapterRequest) -> AdapterResponse:
        # Lazy import to avoid attribute-error if user lacks `openai` at module load.
        import openai  # noqa: PLC0415

        api_input = _messages_to_responses_input(request.messages)
        tools = [_tool_schema_to_openai(t) for t in request.tool_schemas]

        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": request.system_prompt,
            "input": api_input,
            "max_output_tokens": request.max_output_tokens,
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if tools:
            kwargs["tools"] = tools
            # When the budget is exhausted, force the model to call
            # submit_assessment so the rendering terminates this turn.
            if request.force_final_answer and any(
                t["name"] == request.output_contract_tool_name for t in tools
            ):
                kwargs["tool_choice"] = {
                    "type": "function",
                    "name": request.output_contract_tool_name,
                }
            elif request.require_tool_call:
                # Investigation gate: submit is withheld from ``tools`` upstream,
                # so requiring a tool call forces an investigative call this turn.
                kwargs["tool_choice"] = "required"

        start = time.monotonic()
        try:
            raw = await self._client.responses.create(**kwargs)
        except openai.RateLimitError as exc:
            raise RetryableAdapterError(
                f"openai rate limit: {exc}",
                retry_after_seconds=parse_retry_after(exc),
            ) from exc
        except (openai.APITimeoutError, openai.APIConnectionError) as exc:
            raise RetryableAdapterError(f"openai transient error: {exc}") from exc
        except openai.APIStatusError as exc:
            # 5xx is transient; everything else is permanent for this rendering.
            if exc.status_code is not None and exc.status_code >= 500:
                raise RetryableAdapterError(
                    f"openai {exc.status_code} server error: {exc}"
                ) from exc
            raise FatalAdapterError(
                f"openai {exc.status_code} error: {exc} (request_id={exc.request_id})"
            ) from exc
        elapsed_ms = int((time.monotonic() - start) * 1000)

        return _parse_openai_response(
            raw,
            request.output_contract_tool_name,
            elapsed_ms,
        )


# ---------------------------------------------------------------------------
# Helpers — request shaping
# ---------------------------------------------------------------------------


def _messages_to_responses_input(messages: list[Any]) -> list[dict[str, Any]]:
    """Translate the harness conversation into Responses API ``input`` items.

    Responses uses a flat list of role/content items, with tool calls and
    tool results expressed as ``"type": "function_call"`` and
    ``"type": "function_call_output"`` items respectively.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "user":
            out.append({"role": "user", "content": msg.content})
        elif msg.role == "assistant":
            if msg.tool_call is not None:
                out.append(
                    {
                        "type": "function_call",
                        "call_id": msg.tool_call.id,
                        "name": msg.tool_call.name,
                        "arguments": _json_dumps(msg.tool_call.args),
                    }
                )
            if msg.content:
                out.append({"role": "assistant", "content": msg.content})
        elif msg.role == "tool":
            out.append(
                {
                    "type": "function_call_output",
                    "call_id": msg.tool_use_id or "",
                    "output": msg.content,
                }
            )
    return out


def _tool_schema_to_openai(item: dict[str, Any]) -> dict[str, Any]:
    """Convert one harness tool entry to OpenAI Responses function-tool shape."""
    return {
        "type": "function",
        "name": item["name"],
        "description": item.get("description", ""),
        "parameters": item["schema"],
        "strict": False,  # tools accept partial args; strict mode rejects oneOf/anyOf
    }


# ---------------------------------------------------------------------------
# Helpers — response parsing
# ---------------------------------------------------------------------------


def _parse_openai_response(
    raw: Any, submit_tool_name: str, elapsed_ms: int
) -> AdapterResponse:
    """Translate a Responses-API SDK object into :class:`AdapterResponse`.

    The Responses API can return a mix of items in ``output``: text blocks,
    function calls, and reasoning summaries. We surface the FIRST function
    call (since the agent loop dispatches one tool per turn) and accumulate
    text content into ``response.text``.
    """
    text_parts: list[str] = []
    tool_call: AdapterToolCall | None = None
    submit_args: dict[str, Any] | None = None

    for item in getattr(raw, "output", []) or []:
        item_type = getattr(item, "type", None)
        if item_type == "message":
            for block in getattr(item, "content", []) or []:
                if getattr(block, "type", None) == "output_text":
                    text_parts.append(getattr(block, "text", ""))
        elif item_type == "function_call" and tool_call is None and submit_args is None:
            name = getattr(item, "name", "")
            args_raw = getattr(item, "arguments", "{}")
            args = _json_loads(args_raw) or {}
            if name == submit_tool_name:
                submit_args = args
            else:
                tool_call = AdapterToolCall(
                    id=getattr(item, "call_id", "") or getattr(item, "id", ""),
                    name=name,
                    args=args,
                )

    finish = (
        "submit_assessment" if submit_args is not None
        else "tool_call" if tool_call is not None
        else "stop"
    )

    usage = _extract_openai_usage(raw)
    return AdapterResponse(
        finish_reason=finish,  # type: ignore[arg-type]
        text="\n".join(text_parts),
        tool_call=tool_call,
        submit_assessment_args=submit_args,
        usage=usage,
        wall_time_ms=elapsed_ms,
    )


def _extract_openai_usage(raw: Any) -> TokenUsage:
    usage = getattr(raw, "usage", None)
    if usage is None:
        return TokenUsage()
    prompt = int(getattr(usage, "input_tokens", 0) or 0)
    output = int(getattr(usage, "output_tokens", 0) or 0)
    cached = 0
    reasoning = 0
    details = getattr(usage, "input_tokens_details", None)
    if details is not None:
        cached = int(getattr(details, "cached_tokens", 0) or 0)
    out_details = getattr(usage, "output_tokens_details", None)
    if out_details is not None:
        reasoning = int(getattr(out_details, "reasoning_tokens", 0) or 0)
    # The Responses API reports `output_tokens` as the FULL output count, with
    # reasoning tokens included as a subset. We store `output_tokens` as the
    # visible (non-reasoning) portion so the provider-agnostic cost formula —
    # output * output_rate + reasoning * reasoning_rate — bills reasoning exactly
    # once. This matches Gemini, where candidates/thoughts are already disjoint.
    return TokenUsage(
        prompt_tokens=max(0, prompt - cached),
        cached_tokens=cached,
        output_tokens=max(0, output - reasoning),
        reasoning_tokens=reasoning,
    )


# ---------------------------------------------------------------------------
# Tiny JSON helpers — local so this file has no extra imports
# ---------------------------------------------------------------------------


def _json_dumps(obj: Any) -> str:
    import json  # noqa: PLC0415

    return json.dumps(obj, sort_keys=True)


def _json_loads(text: str) -> Any:
    import json  # noqa: PLC0415

    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}


__all__ = ["OpenAIAdapter"]
