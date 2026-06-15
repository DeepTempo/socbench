"""Anthropic Messages API adapter.

Written against the anthropic Python package guide fetched via chub
(``anthropic/package`` at v0.84.0, docs current as of March 2026).

Key choices:

- Uses :meth:`Anthropic().messages.create` per the SDK's modern-API guidance.
- ``submit_assessment`` is declared alongside the regular tools; under
  ``force_final_answer=True`` the adapter sets
  ``tool_choice={"type": "tool", "name": "submit_assessment"}`` so the
  model commits this turn (force-final behaviour via a single forced tool).
- The system prompt is sent as a block list with ``cache_control``
  attached so the stable prefix qualifies for Anthropic's prompt cache.
  Cache token counts surface as ``cached_tokens`` in the response.
- Errors are mapped to the harness's ``RetryableAdapterError`` /
  ``FatalAdapterError`` so the agent loop can decide whether to back off
  and retry vs. fail the rendering.

The SDK is imported lazily inside ``__init__``; constructing the adapter
without ``anthropic`` installed raises a clear :class:`FatalAdapterError`.
"""
from __future__ import annotations

import json
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


class AnthropicAdapter(Adapter):
    """Anthropic Messages-API adapter.

    Requires ``ANTHROPIC_API_KEY`` in the environment. The model identifier
    is pinned at construction time (driven by ``benchmark_config.yaml``).
    """

    provider_name = "anthropic"

    def __init__(self, model: str) -> None:
        self.model = model
        try:
            from anthropic import AsyncAnthropic  # noqa: PLC0415
        except ImportError as exc:
            raise FatalAdapterError(
                "anthropic SDK not installed. Install with `pip install \"socbench[providers]\"` "
                "or `pip install anthropic`."
            ) from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise FatalAdapterError(
                "ANTHROPIC_API_KEY is not set. Export it before running, e.g. "
                "`export ANTHROPIC_API_KEY=sk-ant-...`."
            )
        self._client = AsyncAnthropic()

    # -- Adapter contract ----------------------------------------------------

    async def invoke(self, request: AdapterRequest) -> AdapterResponse:
        import anthropic  # noqa: PLC0415

        api_messages = _messages_to_anthropic(request.messages)
        tools = [_tool_schema_to_anthropic(t) for t in request.tool_schemas]

        # System prompt + tool schemas form the cacheable stable prefix.
        # cache_control on the system block enables Anthropic's prompt cache;
        # the response surfaces cache_read_input_tokens.
        system_blocks = [
            {
                "type": "text",
                "text": request.system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            "system": system_blocks,
            "messages": api_messages,
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if tools:
            kwargs["tools"] = tools
            if request.force_final_answer and any(
                t["name"] == request.output_contract_tool_name for t in tools
            ):
                kwargs["tool_choice"] = {
                    "type": "tool",
                    "name": request.output_contract_tool_name,
                }
            else:
                kwargs["tool_choice"] = {"type": "auto"}

        start = time.monotonic()
        try:
            raw = await self._client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise RetryableAdapterError(
                f"anthropic rate limit: {exc}",
                retry_after_seconds=parse_retry_after(exc),
            ) from exc
        except (
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
        ) as exc:
            raise RetryableAdapterError(f"anthropic transient error: {exc}") from exc
        except anthropic.APIStatusError as exc:
            status = getattr(exc, "status_code", None)
            if status is not None and status >= 500:
                raise RetryableAdapterError(
                    f"anthropic {status} server error: {exc}"
                ) from exc
            raise FatalAdapterError(
                f"anthropic {status} error: {exc}"
            ) from exc
        elapsed_ms = int((time.monotonic() - start) * 1000)

        return _parse_anthropic_response(
            raw,
            request.output_contract_tool_name,
            elapsed_ms,
        )


# ---------------------------------------------------------------------------
# Helpers — request shaping
# ---------------------------------------------------------------------------


def _messages_to_anthropic(messages: list[Any]) -> list[dict[str, Any]]:
    """Translate the harness conversation into Anthropic message blocks.

    Anthropic uses ``tool_use`` assistant blocks and ``tool_result`` user
    blocks; the agent loop's ``Message.role == "tool"`` becomes a user
    message carrying a single tool_result block, following the SDK pattern.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "user":
            out.append({"role": "user", "content": msg.content})
        elif msg.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if msg.content:
                blocks.append({"type": "text", "text": msg.content})
            if msg.tool_call is not None:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": msg.tool_call.id,
                        "name": msg.tool_call.name,
                        "input": dict(msg.tool_call.args),
                    }
                )
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        elif msg.role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.tool_use_id or "",
                            "content": msg.content,
                        }
                    ],
                }
            )
    return out


def _tool_schema_to_anthropic(item: dict[str, Any]) -> dict[str, Any]:
    """Convert one harness tool entry to Anthropic tool shape."""
    return {
        "name": item["name"],
        "description": item.get("description", ""),
        "input_schema": item["schema"],
    }


# ---------------------------------------------------------------------------
# Helpers — response parsing
# ---------------------------------------------------------------------------


def _parse_anthropic_response(
    raw: Any, submit_tool_name: str, elapsed_ms: int
) -> AdapterResponse:
    """Translate a Messages-API SDK object into :class:`AdapterResponse`.

    Surfaces the first ``tool_use`` block (the agent loop dispatches one
    tool per turn) and accumulates ``text`` blocks. If the surfaced tool is
    ``submit_assessment``, populate ``submit_assessment_args`` instead of
    ``tool_call`` so the agent loop's final-answer path runs.
    """
    text_parts: list[str] = []
    tool_call: AdapterToolCall | None = None
    submit_args: dict[str, Any] | None = None

    for block in getattr(raw, "content", []) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", ""))
        elif btype == "tool_use" and tool_call is None and submit_args is None:
            name = getattr(block, "name", "")
            args = getattr(block, "input", {}) or {}
            if not isinstance(args, dict):
                args = _safe_json_dict(args)
            if name == submit_tool_name:
                submit_args = dict(args)
            else:
                tool_call = AdapterToolCall(
                    id=getattr(block, "id", ""), name=name, args=dict(args)
                )

    finish = (
        "submit_assessment" if submit_args is not None
        else "tool_call" if tool_call is not None
        else "stop"
    )
    return AdapterResponse(
        finish_reason=finish,  # type: ignore[arg-type]
        text="\n".join(text_parts),
        tool_call=tool_call,
        submit_assessment_args=submit_args,
        usage=_extract_anthropic_usage(raw),
        wall_time_ms=elapsed_ms,
    )


def _extract_anthropic_usage(raw: Any) -> TokenUsage:
    """Read token usage from a Messages-API response.

    Anthropic reports ``cache_read_input_tokens`` separately from
    ``input_tokens``. The harness's ``cached_tokens`` field is the
    cache-hit subset; ``prompt_tokens`` is the uncached remainder.
    """
    usage = getattr(raw, "usage", None)
    if usage is None:
        return TokenUsage()
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    # Cache creation tokens are counted under input_tokens by Anthropic.
    return TokenUsage(
        prompt_tokens=input_tokens,
        cached_tokens=cache_read,
        output_tokens=output_tokens,
        reasoning_tokens=0,
    )


def _safe_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        
    return {}


__all__ = ["AnthropicAdapter"]
