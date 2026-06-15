"""Open-source / local inference adapter (OpenAI-compatible Chat Completions over HTTP).

Targets any server that speaks the OpenAI Chat Completions wire format:
Vertex AI endpoints (vLLM/TGI containers), Ollama, llama.cpp server, etc.

Key choices:

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
- Token usage is read from ``usage.{prompt,completion}_tokens``; local servers
  often omit the block, in which case we return zeroed counts.
- HTTP errors are mapped to the harness's ``RetryableAdapterError`` /
  ``FatalAdapterError`` so the agent loop can decide whether to back off
  and retry vs. fail the rendering.

Environment variables:

- ``OPEN_SOURCE_BASE_URL`` — inference server base URL. Defaults to
  ``http://localhost:11434/v1`` (Ollama's OpenAI-compatible endpoint).
- ``OPEN_SOURCE_API_KEY`` — optional Bearer token sent as
  ``Authorization: Bearer <key>``. For Vertex AI endpoints this can be a
  short-lived GCP access token (``gcloud auth print-access-token``).
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


def _http_client() -> Any:
    """Lazy import of httpx so the file is import-safe without it installed."""
    try:
        import httpx  # noqa: PLC0415
    except ImportError as exc:
        raise FatalAdapterError(
            "httpx is not installed. Install with `pip install httpx` or "
            '`pip install "socbench[providers]"`.'
        ) from exc
    return httpx


class OpenSourceAdapter(Adapter):
    """OpenAI-compatible Chat-Completions adapter for local / open-source inference.

    Requires ``httpx``. Connection details are pulled from the environment:

    - ``OPEN_SOURCE_BASE_URL`` — defaults to ``http://localhost:11434/v1``.
    - ``OPEN_SOURCE_API_KEY`` — optional Bearer token.

    The model identifier is pinned at construction time (driven by
    ``benchmark_config.yaml``).
    """

    provider_name = "open_source"

    def __init__(self, model: str) -> None:
        self.model = model
        httpx = _http_client()

        base_url = os.environ.get("OPEN_SOURCE_BASE_URL", "http://localhost:11434/v1").rstrip("/")
        api_key = os.environ.get("OPEN_SOURCE_API_KEY")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=120.0)

    # -- Adapter contract ----------------------------------------------------

    async def invoke(self, request: AdapterRequest) -> AdapterResponse:
        httpx = _http_client()

        api_messages = _messages_to_chat_format(request.messages, request.system_prompt)
        tools = [_tool_schema_to_openai(t) for t in request.tool_schemas]

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": request.max_output_tokens,
        }
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
            else:
                payload["tool_choice"] = "auto"

        start = time.monotonic()
        try:
            response = await self._client.post("/chat/completions", json=payload)
            response.raise_for_status()
            raw = response.json()
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
            raise FatalAdapterError(
                f"open_source {status} error: {body}"
            ) from exc
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RetryableAdapterError(f"open_source transport error: {exc}") from exc
        except httpx.HTTPError as exc:
            raise FatalAdapterError(f"open_source HTTP error: {exc}") from exc
        elapsed_ms = int((time.monotonic() - start) * 1000)

        return _parse_chat_response(
            raw,
            request.output_contract_tool_name,
            elapsed_ms,
        )


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


def _parse_chat_response(
    raw: dict[str, Any], submit_tool_name: str, elapsed_ms: int
) -> AdapterResponse:
    """Translate a Chat Completions JSON response into :class:`AdapterResponse`.

    Surfaces the first ``tool_calls`` entry (the agent loop dispatches one
    tool per turn) and accumulates ``content`` text. If the surfaced tool is
    ``submit_assessment``, populate ``submit_assessment_args`` instead of
    ``tool_call`` so the agent loop's final-answer path runs.
    """
    text_parts: list[str] = []
    tool_call: AdapterToolCall | None = None
    submit_args: dict[str, Any] | None = None

    choices = raw.get("choices", [])
    message = choices[0].get("message", {}) if choices else {}

    content = message.get("content")
    if isinstance(content, str) and content:
        text_parts.append(content)

    for tc in message.get("tool_calls", []) or []:
        if tool_call is not None or submit_args is not None:
            break
        if tc.get("type") != "function":
            continue
        func = tc.get("function", {})
        name = func.get("name", "")
        args = _json_loads(func.get("arguments", "{}"))
        if name == submit_tool_name:
            submit_args = args
        else:
            tool_call = AdapterToolCall(
                id=tc.get("id", ""),
                name=name,
                args=args,
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
        usage=_extract_chat_usage(raw),
        wall_time_ms=elapsed_ms,
    )


def _extract_chat_usage(raw: dict[str, Any]) -> TokenUsage:
    """Read token usage from a Chat Completions JSON response.

    Open-source servers often omit ``usage`` entirely; return zeroes in that
    case.  ``prompt_tokens`` is treated as the uncached input; local servers
    rarely report cache hits separately.
    """
    usage = raw.get("usage")
    if usage is None:
        return TokenUsage()
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    output = int(usage.get("completion_tokens", 0) or 0)
    return TokenUsage(
        prompt_tokens=prompt,
        cached_tokens=0,
        output_tokens=output,
        reasoning_tokens=0,
    )


def _json_loads(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


__all__ = ["OpenSourceAdapter"]
