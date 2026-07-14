"""Open-source / local inference adapter: OpenAI-compatible Chat Completions over HTTP.

Targets vLLM/TGI (incl. Vertex AI), Ollama, llama.cpp; config via OPEN_SOURCE_* env vars.
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

# Per-request read/write deadline; override via OPEN_SOURCE_TIMEOUT_SECONDS.
_DEFAULT_TIMEOUT_SECONDS = 600.0
# Connect-phase deadline: fail fast on a dead endpoint, unlike the read side.
_CONNECT_TIMEOUT_SECONDS = 15.0


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


def _resolve_reasoning_budget() -> int | None:
    """Separate thinking-token budget for reasoning models, from
    ``OPEN_SOURCE_REASONING_BUDGET_TOKENS``. None → don't send one."""
    raw = os.environ.get("OPEN_SOURCE_REASONING_BUDGET_TOKENS")
    if raw is None or raw == "":
        return None
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def _thinking_budget_disabled() -> bool:
    """Kill-switch (``OPEN_SOURCE_DISABLE_THINKING_BUDGET=1``): stop sending the
    ``thinking_token_budget`` param. vLLM only enforces it for some reasoner
    families; an unsupporting parser may 400 and all-zero the run."""
    raw = os.environ.get("OPEN_SOURCE_DISABLE_THINKING_BUDGET", "")
    return raw.strip().lower() in {"1", "true", "yes"}


def _flatten_transcript_enabled() -> bool:
    """``OPEN_SOURCE_FLATTEN_TRANSCRIPT=1``: fold the transcript into one user
    message, for templates with no tool-role branch. Foundation-Sec-8B's template
    drops tool turns and mangles prior assistant turns, causing all-zero scores."""
    raw = os.environ.get("OPEN_SOURCE_FLATTEN_TRANSCRIPT", "")
    return raw.strip().lower() in {"1", "true", "yes"}


class OpenSourceAdapter(Adapter):
    """OpenAI-compatible Chat-Completions adapter for local / open-source inference.

    Requires ``httpx``; configured via ``OPEN_SOURCE_BASE_URL/API_KEY/TIMEOUT_SECONDS``.
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

        self._timeout_seconds = _resolve_timeout()
        # Thinking-token budget for reasoning models (env-configured). See kill-switch.
        self._reasoning_budget = _resolve_reasoning_budget()
        self._send_thinking_budget = not _thinking_budget_disabled()
        # Flatten transcript for templates that can't render tool turns (off by default).
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
        # Names the content-fallback parser may surface as tool calls (plus submit);
        # prevents stray `{...}` prose from spuriously dispatching.
        valid_tool_names = {
            t["name"] for t in request.tool_schemas if isinstance(t.get("name"), str)
        }

        # Widen max_tokens by the reasoning budget so CoT doesn't eat the output budget.
        reasoning_budget = self._reasoning_budget
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
            # vLLM's V2 model runner REJECTS this param with a 400; the deploy
            # entrypoint pins reasoning models to V1 (VLLM_USE_V2_MODEL_RUNNER=0)
            # so it's honoured. OPEN_SOURCE_DISABLE_THINKING_BUDGET=1 is the fallback.
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


# ---------------------------------------------------------------------------
# Helpers — HTTP transport
# ---------------------------------------------------------------------------


async def _post_chat_completion(
    client: Any, httpx: Any, payload: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """POST one ``/chat/completions`` request; maps outcomes to Retryable/Fatal
    errors, returns ``(raw_json, elapsed_ms)``. Self-heals a ``thinking_token_budget``
    400 (rejected by some runners, e.g. QwQ/V2) by stripping it and retrying once."""
    start = time.monotonic()
    while True:
        try:
            response = await client.post("/chat/completions", json=payload)
            response.raise_for_status()
            try:
                raw = response.json()
            except ValueError as exc:
                # HTTP 200 with a non-JSON body: OpenRouter can emit SSE keep-alive
                # comments (": OPENROUTER PROCESSING") or a truncated body under burst
                # load. Almost always transient — retry rather than crash the run.
                raise RetryableAdapterError(
                    f"open_source non-JSON 200 body: {response.text[:300]}"
                ) from exc
            break
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            body = exc.response.text
            if status == 429:
                # Retry-After header, if present.
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
        except (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.NetworkError,
            # A mid-stream disconnect ("peer closed connection without sending a
            # complete message body / incomplete chunked read") is httpx.RemoteProtocolError
            # — a ProtocolError, NOT a NetworkError, so it must be listed explicitly.
            # It's the single most common transient failure when streaming from a
            # busy provider (OpenRouter); retry rather than lose the rendering.
            httpx.ProtocolError,
        ) as exc:
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

    Assistant ``tool_call``s become ``tool_calls``; ``tool`` messages reference
    the preceding call by ``tool_call_id``."""
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
                # Some servers complain if content is present alongside tool_calls.
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
    """Render the whole conversation as ``system`` + one role-labelled ``user``
    message, for chat templates that can't represent a multi-turn tool transcript."""
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

    Returns ``(tool_call, submit_args)`` — at most one non-``None``."""
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

    Surfaces the first tool call; if it's ``submit_assessment``, populates
    ``submit_assessment_args`` instead so the agent loop's final-answer path runs."""
    text_parts: list[str] = []
    tool_call: AdapterToolCall | None = None
    submit_args: dict[str, Any] | None = None

    choices = raw.get("choices", []) or []
    choice = choices[0] if choices else {}
    message = choice.get("message", {}) if isinstance(choice, dict) else {}

    content = message.get("content")
    if isinstance(content, str) and content:
        text_parts.append(content)

    # vLLM reasoning separation: diagnostics only, never the answer text.
    reasoning_content = message.get("reasoning_content")
    provider_raw = (
        {"reasoning_content": reasoning_content}
        if isinstance(reasoning_content, str) and reasoning_content
        else None
    )

    tool_call, submit_args = _native_tool_call_from_message(message, submit_tool_name)

    # Content fallback: recovers tool calls models emit inside `content` (JSON,
    # fenced blocks, tags) when native `tool_calls` parsing finds nothing.
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
        # Tripwire: track how often the content-fallback recovers a call.
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
    """Read token usage from a Chat Completions JSON response (zeroes if absent).

    Splits reasoning/cache subsets out of completion/prompt tokens, same as the openai adapter."""
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


def _debug_dump(request: Any, raw: dict[str, Any], parsed: Any) -> None:
    """Append a per-turn diagnostic record to OPEN_SOURCE_DEBUG_DUMP (JSONL), if set.

    No-op otherwise; never affects the run."""
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


def _json_loads(text: Any) -> dict[str, Any]:
    if isinstance(text, dict):
        return text
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

# Strip inline <think> blocks first so drafted JSON isn't mistaken for the committed call.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_CLOSE_RE = re.compile(r"^.*?</think>", re.IGNORECASE | re.DOTALL)
# Tagged tool-call wrappers (hermes/sec-style <tool_call>, <function=NAME>).
_TAGGED_CALL_RES = (
    re.compile(r"<tool_?call>\s*(\{.*?\})\s*</tool_?call>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<function=[^>]*>\s*(\{.*?\})\s*</function>", re.IGNORECASE | re.DOTALL),
)
# Fenced code blocks (```json … ``` or bare ``` … ```).
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.IGNORECASE | re.DOTALL)
# Llama tool sentinel; call JSON follows it.
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

    A quote/escape-aware brace scanner, robust to objects embedded in prose."""
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

    Tolerant of the shapes free-form models emit for name/args keys, or flat args."""
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
    """Recognise a name-less ``{"verdict":..., "rationale":...}`` submit payload
    by fingerprint; never fires for a named call."""
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

    Search order is tagged wrappers > fenced blocks > python_tag > bare JSON,
    most-explicit-first, so a deliberate call outranks an offhand JSON snippet
    the model left in its reasoning; only known tool names are accepted."""
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
    # Bare objects last (lowest priority); also covers unwrapped JSON content.
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
