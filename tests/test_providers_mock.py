"""MockAdapter behaviour: default script, custom script, reset, force-final.

The mock is always available (no SDK deps) and underpins every other test
that exercises the agent loop; its behaviour is the contract the loop
relies on.
"""
from __future__ import annotations

import pytest

from socbench.providers import build_adapter, list_known_providers
from socbench.providers.base import (
    AdapterRequest,
    FatalAdapterError,
    Message,
)
from socbench.providers.mock_adapter import MockAdapter, _CannedResponse


def _basic_request(force_final: bool = False) -> AdapterRequest:
    return AdapterRequest(
        system_prompt="test",
        messages=[Message(role="user", content="kick")],
        tool_schemas=[],
        output_contract_schema={"type": "object"},
        force_final_answer=force_final,
    )


def test_default_script_returns_tool_call_then_submission() -> None:
    adapter = MockAdapter()
    r1 = adapter.invoke_sync(_basic_request())
    assert r1.finish_reason == "tool_call"
    assert r1.tool_call is not None
    assert r1.tool_call.name == "list_pairs"
    assert r1.submit_assessment_args is None

    r2 = adapter.invoke_sync(_basic_request())
    assert r2.finish_reason == "submit_assessment"
    assert r2.submit_assessment_args is not None
    assert r2.submit_assessment_args["verdict"] in {"benign", "malicious"}


def test_default_script_exhaustion_raises_fatal() -> None:
    adapter = MockAdapter()
    adapter.invoke_sync(_basic_request())
    adapter.invoke_sync(_basic_request())
    with pytest.raises(FatalAdapterError, match="script exhausted"):
        adapter.invoke_sync(_basic_request())


def test_with_script_runs_custom_sequence() -> None:
    script = [
        _CannedResponse.tool_call("list_pairs", {"limit": 3}),
        _CannedResponse.tool_call(
            "get_pair_timeline", {"src_ip": "10.0.0.1", "dst_ip": "10.0.0.2"}
        ),
        _CannedResponse.submit(verdict="malicious", confidence=0.9, rationale="custom"),
    ]
    adapter = MockAdapter.with_script("mock-test", script)
    assert adapter.invoke_sync(_basic_request()).tool_call.name == "list_pairs"
    assert adapter.invoke_sync(_basic_request()).tool_call.name == "get_pair_timeline"
    final = adapter.invoke_sync(_basic_request())
    assert final.finish_reason == "submit_assessment"
    assert final.submit_assessment_args["verdict"] == "malicious"


def test_reset_restarts_script_cursor() -> None:
    adapter = MockAdapter()
    adapter.invoke_sync(_basic_request())
    adapter.invoke_sync(_basic_request())
    adapter.reset()
    # Should be back at the start of the default script.
    r = adapter.invoke_sync(_basic_request())
    assert r.tool_call is not None and r.tool_call.name == "list_pairs"


def test_force_final_overrides_script() -> None:
    """force_final_answer must yield a submission even mid-script."""
    script = [_CannedResponse.tool_call("list_pairs", {"limit": 1})] * 5
    adapter = MockAdapter.with_script("mock-test", script)
    resp = adapter.invoke_sync(_basic_request(force_final=True))
    assert resp.finish_reason == "submit_assessment"
    assert resp.submit_assessment_args is not None
    # Cursor should not advance when forced; subsequent normal call returns
    # script[0].
    assert adapter._cursor == 0
    next_resp = adapter.invoke_sync(_basic_request())
    assert next_resp.tool_call is not None
    assert next_resp.tool_call.name == "list_pairs"


def test_invoke_populates_wall_time_and_usage() -> None:
    adapter = MockAdapter()
    resp = adapter.invoke_sync(_basic_request())
    assert resp.wall_time_ms >= 0
    # Default canned response declares non-zero prompt/output tokens.
    assert resp.usage.prompt_tokens > 0
    assert resp.usage.output_tokens > 0


def test_build_adapter_mock_works_without_keys() -> None:
    adapter = build_adapter("mock", "mock-default")
    assert adapter.provider_name == "mock"
    assert adapter.model == "mock-default"


def test_list_known_providers_includes_all_four() -> None:
    known = set(list_known_providers())
    assert {"mock", "openai", "anthropic", "gemini"} <= known


def test_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        build_adapter("nope", "any-model")
