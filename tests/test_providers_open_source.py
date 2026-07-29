"""Content-fallback parser tests: recovering tool calls / submissions that
non-tool-trained reasoners emit as content text (bare ``{"verdict":...}`` payloads,
``args``-nested calls) rather than native ``tool_calls``.
"""

from __future__ import annotations

from typing import Any

from socbench.models import SubmitAssessment
from socbench.providers.open_source_adapter import _parse_chat_response

SUBMIT = "submit_assessment"
TOOLS = {"list_pairs", "host_rollup", "rarity_stats", "get_flows"}


def _raw(content: str, *, finish: str = "stop") -> dict[str, Any]:
    """A vLLM Chat Completions response with no native tool_calls (sec8 shape)."""
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {},
    }


def _parse(content: str):
    return _parse_chat_response(_raw(content), SUBMIT, 0, valid_tool_names=TOOLS)


# --- name-less submit recovery -------------------------------------------------


def test_bare_submit_payload_in_fence_is_recovered() -> None:
    content = (
        "<think>fan-out looks like beaconing</think>\n"
        "Final Assessment:\n"
        "```json\n"
        '{"verdict": "malicious", "confidence": 0.65, '
        '"malicious_destinations": ["192.168.101.42"], '
        '"malicious_flow_indices": [], '
        '"rationale": "diverse fan-out consistent with C2 beaconing"}\n'
        "```"
    )
    parsed = _parse(content)
    assert parsed.finish_reason == "submit_assessment"
    assert parsed.tool_call is None
    args = parsed.submit_assessment_args
    assert args is not None
    assert args["verdict"] == "malicious"
    # And it must satisfy the strict (extra="forbid") contract.
    SubmitAssessment(**args)


def test_bare_submit_strips_stray_keys_for_forbid_contract() -> None:
    # A stray key would otherwise trip ConfigDict(extra="forbid") and discard
    # an otherwise-valid verdict; the recovered payload is projected to schema.
    content = (
        '{"verdict": "benign", "confidence": 0.9, '
        '"rationale": "ordinary web traffic", '
        '"threat_type": "none", "summary": "nothing to see"}'
    )
    parsed = _parse(content)
    args = parsed.submit_assessment_args
    assert args is not None
    assert "threat_type" not in args and "summary" not in args
    SubmitAssessment(**args)  # must not raise


def test_unknown_verdict_recovered_but_not_coerced() -> None:
    # The model hedging with verdict="unknown" is a real model behaviour, not a
    # parse failure: we recover it faithfully and let the contract reject it,
    # rather than fabricating a benign/malicious call.
    content = '{"verdict": "unknown", "confidence": 0.5, "rationale": "inconclusive"}'
    parsed = _parse(content)
    assert parsed.submit_assessment_args is not None
    assert parsed.submit_assessment_args["verdict"] == "unknown"


def test_prose_without_verdict_block_stays_text() -> None:
    # No committed JSON answer → must remain a plain text turn, not a misfire.
    content = "I would start by running rarity_stats on the source host {scope}."
    parsed = _parse(content)
    assert parsed.finish_reason == "stop"
    assert parsed.tool_call is None
    assert parsed.submit_assessment_args is None


# --- args/input alias (double-nesting bug) ------------------------------------


def test_named_tool_call_with_args_key_is_not_double_nested() -> None:
    # Untrained models reach for "args"; without the alias this fell to flat-form
    # and wrapped the payload as {"args": {...}}, so nothing reached the tool.
    content = '{"name": "rarity_stats", "args": {"limit": 10, "scope": {"src_ip": "10.0.0.1"}}}'
    parsed = _parse(content)
    assert parsed.tool_call is not None
    assert parsed.tool_call.name == "rarity_stats"
    assert parsed.tool_call.args == {"limit": 10, "scope": {"src_ip": "10.0.0.1"}}


def test_named_tool_call_with_arguments_key_still_works() -> None:
    content = '{"name": "host_rollup", "arguments": {"host": "10.0.0.1"}}'
    parsed = _parse(content)
    assert parsed.tool_call is not None
    assert parsed.tool_call.name == "host_rollup"
    assert parsed.tool_call.args == {"host": "10.0.0.1"}


def test_named_submit_call_still_takes_precedence() -> None:
    # A model that DOES wrap its call properly is unaffected by the bare-payload
    # path (Qwen/QwQ regression guard).
    content = (
        '{"name": "submit_assessment", "arguments": '
        '{"verdict": "malicious", "confidence": 0.8, "rationale": "clear C2"}}'
    )
    parsed = _parse(content)
    assert parsed.submit_assessment_args is not None
    assert parsed.submit_assessment_args["verdict"] == "malicious"


def test_native_tool_calls_unaffected_by_fallback() -> None:
    # When the server returns native tool_calls, the fallback must never run.
    raw = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "call_1",
                            "function": {
                                "name": "list_pairs",
                                "arguments": '{"sort": "flow_count"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {},
    }
    parsed = _parse_chat_response(raw, SUBMIT, 0, valid_tool_names=TOOLS)
    assert parsed.tool_call is not None
    assert parsed.tool_call.name == "list_pairs"
    assert parsed.tool_call.args == {"sort": "flow_count"}


def test_native_tool_calls_already_decoded_dict_works() -> None:
    # Some endpoints return arguments as a decoded dict instead of a JSON string.
    raw = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "call_2",
                            "function": {
                                "name": "list_pairs",
                                "arguments": {"sort": "flow_count"},
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {},
    }
    parsed = _parse_chat_response(raw, SUBMIT, 0, valid_tool_names=TOOLS)
    assert parsed.tool_call is not None
    assert parsed.tool_call.name == "list_pairs"
    assert parsed.tool_call.args == {"sort": "flow_count"}
