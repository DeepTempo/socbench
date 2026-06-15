"""Google Gemini GenAI SDK adapter.

Written against the gemini Python package guide fetched via chub
(``gemini/genai`` at v1.56.0, model guidance current as of March 2026).

Key choices:

- Uses the modern ``google-genai`` package (NOT the legacy
  ``google-generativeai``), accessed via :meth:`genai.Client`.
- Calls :meth:`client.models.generate_content` with a
  :class:`types.GenerateContentConfig` that carries
  ``system_instruction``, ``tools``, and ``tool_config`` together.
- ``submit_assessment`` is declared alongside the regular tools. Under
  ``force_final_answer=True`` the adapter sets
  ``function_calling_config.mode='ANY'`` with
  ``allowed_function_names=['submit_assessment']`` so the model commits
  this turn (force-final behaviour).
- Errors surface as :class:`google.genai.errors.APIError`; ``500+`` and
  retryable connection errors map to ``RetryableAdapterError``,
  everything else to ``FatalAdapterError``.

The SDK is imported lazily inside ``__init__``; constructing without
``google-genai`` installed raises a clear :class:`FatalAdapterError`.
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
)


class GeminiAdapter(Adapter):
    """Google Gemini adapter using the ``google-genai`` SDK.

    Requires ``GEMINI_API_KEY`` in the environment. The model identifier is
    pinned at construction time (driven by ``benchmark_config.yaml``).
    """

    provider_name = "gemini"

    def __init__(self, model: str) -> None:
        self.model = model
        try:
            from google import genai  # noqa: PLC0415
            from google.genai import types  # noqa: PLC0415
        except ImportError as exc:
            raise FatalAdapterError(
                "google-genai SDK not installed. Install with "
                "`pip install \"socbench[providers]\"` or "
                "`pip install google-genai`."
            ) from exc
        if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            raise FatalAdapterError(
                "GEMINI_API_KEY (or GOOGLE_API_KEY) is not set. Export it before "
                "running, e.g. `export GEMINI_API_KEY=...`."
            )
        self._client = genai.Client()
        # Stash the types module for use in invoke(); avoids repeating the
        # lazy-import dance in each method.
        self._types = types

    # -- Adapter contract ----------------------------------------------------

    async def invoke(self, request: AdapterRequest) -> AdapterResponse:
        from google.genai import errors as genai_errors  # noqa: PLC0415
        from google.genai import types  # noqa: PLC0415

        contents = _messages_to_gemini(request.messages, types)

        tool_decls = [_tool_schema_to_gemini_decl(t, types) for t in request.tool_schemas]
        gemini_tools = [types.Tool(function_declarations=tool_decls)] if tool_decls else None

        tool_config = None
        if request.force_final_answer and any(
            d.name == request.output_contract_tool_name for d in tool_decls
        ):
            tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="ANY",
                    allowed_function_names=[request.output_contract_tool_name],
                )
            )

        config_kwargs: dict[str, Any] = {
            "system_instruction": request.system_prompt,
            "max_output_tokens": request.max_output_tokens,
            "tools": gemini_tools,
            "tool_config": tool_config,
        }
        if request.temperature is not None:
            config_kwargs["temperature"] = request.temperature
        config = types.GenerateContentConfig(**config_kwargs)

        start = time.monotonic()
        try:
            # ``client.aio`` mirrors the sync API with awaitable methods.
            raw = await self._client.aio.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )
        except genai_errors.APIError as exc:
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            if code is not None and 500 <= int(code) < 600:
                raise RetryableAdapterError(f"gemini {code} server error: {exc}") from exc
            # 429 → retryable; everything else → fatal
            if code == 429:
                raise RetryableAdapterError(f"gemini 429 rate limited: {exc}") from exc
            raise FatalAdapterError(f"gemini error: {exc}") from exc
        except (ConnectionError, TimeoutError) as exc:
            raise RetryableAdapterError(f"gemini transport error: {exc}") from exc
        elapsed_ms = int((time.monotonic() - start) * 1000)

        return _parse_gemini_response(
            raw,
            request.output_contract_tool_name,
            elapsed_ms,
        )


# ---------------------------------------------------------------------------
# Helpers: request shaping
# ---------------------------------------------------------------------------


def _messages_to_gemini(messages: list[Any], types: Any) -> list[Any]:
    """Translate the harness conversation into Gemini ``contents``.

    Gemini uses ``role`` of ``"user"`` or ``"model"`` (no separate ``tool``
    role); tool RESULTS go back as user-role content carrying a
    ``Part.from_function_response`` part. Tool CALLS go in model-role
    content carrying a ``Part.from_function_call`` part.
    """
    out: list[Any] = []
    for msg in messages:
        if msg.role == "user":
            out.append(
                types.Content(role="user", parts=[types.Part(text=msg.content)])
            )
        elif msg.role == "assistant":
            parts: list[Any] = []
            if msg.content:
                parts.append(types.Part(text=msg.content))
            if msg.tool_call is not None:
                parts.append(
                    types.Part.from_function_call(
                        name=msg.tool_call.name,
                        args=dict(msg.tool_call.args),
                    )
                )
            if parts:
                out.append(types.Content(role="model", parts=parts))
        elif msg.role == "tool":
            # Gemini expects function responses in user-role content blocks.
            response_payload: dict[str, Any]
            try:
                parsed = json.loads(msg.content)
                response_payload = parsed if isinstance(parsed, dict) else {"result": parsed}
            except json.JSONDecodeError:
                response_payload = {"result": msg.content}
            out.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_function_response(
                            name=_infer_tool_name_from_id(msg.tool_use_id),
                            response=response_payload,
                        )
                    ],
                )
            )
    return out


def _infer_tool_name_from_id(tool_use_id: str | None) -> str:
    """Gemini's function_response requires a name; the harness threads its
    own tool_use_id which doesn't carry the name. We embed the name in the
    id when constructing the AdapterToolCall, so peel it back here.
    """
    if not tool_use_id:
        return "unknown_tool"
    # If the id looks like "<name>::<rand>", split; otherwise treat the
    # whole string as the name. The Gemini parsing path below populates
    # AdapterToolCall.id with "<name>::<index>" to make this round-trip.
    if "::" in tool_use_id:
        return tool_use_id.split("::", 1)[0]
    return tool_use_id


# JSON-Schema keys that the OpenAI/Anthropic tool contracts rely on but that
# Gemini's OpenAPI-subset schema dialect rejects with a 400 INVALID_ARGUMENT
# (e.g. ``additionalProperties: false`` is mandatory for OpenAI strict mode but
# unknown to Gemini). Stripped recursively before building the declaration.
_GEMINI_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {"additionalProperties", "additional_properties", "$schema"}
)


def _sanitize_gemini_schema(node: Any) -> Any:
    """Deep-copy a JSON Schema, dropping keys Gemini does not understand."""
    if isinstance(node, dict):
        return {
            k: _sanitize_gemini_schema(v)
            for k, v in node.items()
            if k not in _GEMINI_UNSUPPORTED_SCHEMA_KEYS
        }
    if isinstance(node, list):
        return [_sanitize_gemini_schema(v) for v in node]
    return node


def _tool_schema_to_gemini_decl(item: dict[str, Any], types: Any) -> Any:
    """Convert one harness tool entry to a Gemini ``FunctionDeclaration``."""
    return types.FunctionDeclaration(
        name=item["name"],
        description=item.get("description", ""),
        parameters=_sanitize_gemini_schema(item["schema"]),
    )


# ---------------------------------------------------------------------------
# Helpers: response parsing
# ---------------------------------------------------------------------------


def _parse_gemini_response(
    raw: Any, submit_tool_name: str, elapsed_ms: int
) -> AdapterResponse:
    """Translate a Gemini SDK response into :class:`AdapterResponse`."""
    text_parts: list[str] = []
    tool_call: AdapterToolCall | None = None
    submit_args: dict[str, Any] | None = None

    # response.function_calls is a convenience accessor that flattens any
    # tool-call parts from the candidate(s).
    fcs = getattr(raw, "function_calls", None) or []
    for idx, fc in enumerate(fcs):
        name = getattr(fc, "name", "")
        args_attr = getattr(fc, "args", {}) or {}
        args = dict(args_attr) if not isinstance(args_attr, dict) else args_attr
        if name == submit_tool_name and submit_args is None:
            submit_args = dict(args)
        elif tool_call is None and submit_args is None:
            tool_call = AdapterToolCall(
                # Synthesize a stable id so the harness can match the result
                # back when constructing the next turn's contents.
                id=f"{name}::{idx}",
                name=name,
                args=dict(args),
            )

    text_attr = getattr(raw, "text", None)
    if isinstance(text_attr, str) and text_attr:
        text_parts.append(text_attr)

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
        usage=_extract_gemini_usage(raw),
        wall_time_ms=elapsed_ms,
    )


def _extract_gemini_usage(raw: Any) -> TokenUsage:
    usage = getattr(raw, "usage_metadata", None)
    if usage is None:
        return TokenUsage()
    prompt = int(getattr(usage, "prompt_token_count", 0) or 0)
    output = int(getattr(usage, "candidates_token_count", 0) or 0)
    cached = int(getattr(usage, "cached_content_token_count", 0) or 0)
    # Gemini 2.5 series exposes "thoughts" tokens for the thinking budget.
    reasoning = int(getattr(usage, "thoughts_token_count", 0) or 0)
    return TokenUsage(
        prompt_tokens=max(0, prompt - cached),
        cached_tokens=cached,
        output_tokens=output,
        reasoning_tokens=reasoning,
    )


__all__ = ["GeminiAdapter"]
