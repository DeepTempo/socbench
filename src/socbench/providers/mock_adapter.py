"""Deterministic offline adapter for tests, CI, and CLI smoke runs.

The mock adapter consumes a *script*: an ordered list of canned responses,
one per call. The default script is the minimal "look around, then submit"
flow that every persona can execute against any built index:

    turn 1:  list_pairs(limit=10)
    turn 2:  submit_assessment(verdict=benign, …)

For tests that need richer behaviour (cap hits, schema violations, multi-
turn tool sequences), construct the adapter with a custom script via
:func:`MockAdapter.with_script`.

The mock never imports any third-party SDK. It is always available, and
``socbench run --providers mock`` works without any API keys.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from socbench.providers.base import (
    Adapter,
    AdapterRequest,
    AdapterResponse,
    AdapterToolCall,
    FatalAdapterError,
    TokenUsage,
)


class _CannedResponse:
    """One scripted adapter response.

    Use the convenience constructors :meth:`tool_call` and :meth:`submit`
    instead of instantiating directly.
    """

    def __init__(
        self,
        *,
        finish_reason: str,
        text: str = "",
        tool_call: AdapterToolCall | None = None,
        submit_args: dict[str, Any] | None = None,
        prompt_tokens: int = 200,
        cached_tokens: int = 0,
        output_tokens: int = 40,
        reasoning_tokens: int = 0,
        simulated_wall_time_ms: int = 5,
    ) -> None:
        self.finish_reason = finish_reason
        self.text = text
        self.tool_call = tool_call
        self.submit_args = submit_args
        self.prompt_tokens = prompt_tokens
        self.cached_tokens = cached_tokens
        self.output_tokens = output_tokens
        self.reasoning_tokens = reasoning_tokens
        self.simulated_wall_time_ms = simulated_wall_time_ms

    @classmethod
    def tool_call(
        cls, name: str, args: dict[str, Any], *, call_id: str | None = None
    ) -> _CannedResponse:
        return cls(
            finish_reason="tool_call",
            tool_call=AdapterToolCall(
                id=call_id or f"mock_tool_call_{name}",
                name=name,
                args=args,
            ),
        )

    @classmethod
    def submit(
        cls,
        *,
        verdict: str = "benign",
        confidence: float = 0.5,
        malicious_flow_indices: list[int] | None = None,
        rationale: str = "Mock adapter default submission.",
    ) -> _CannedResponse:
        return cls(
            finish_reason="submit_assessment",
            submit_args={
                "verdict": verdict,
                "confidence": confidence,
                "malicious_flow_indices": list(malicious_flow_indices or []),
                "rationale": rationale,
            },
        )


def _default_script() -> list[_CannedResponse]:
    """A minimal script every persona can execute against any index.

    Issues one cheap tool call (``list_pairs``) and then submits a low-
    confidence benign verdict. This is enough for the agent loop, artifact
    writers, and CLI to exercise their full code paths end-to-end without
    any API keys.
    """
    return [
        _CannedResponse.tool_call("list_pairs", {"limit": 5}),
        _CannedResponse.submit(
            verdict="benign",
            confidence=0.3,
            malicious_flow_indices=[],
            rationale=(
                "Mock adapter: scanned a small slice of pairs and saw no "
                "clear malicious indicators within the budget. Submitting "
                "low-confidence benign for end-to-end harness exercise."
            ),
        ),
    ]


class MockAdapter(Adapter):
    """Deterministic, offline adapter.

    Two construction modes:
      - ``MockAdapter("mock-default")``: uses the canned default script
        described in :func:`_default_script`.
      - ``MockAdapter.with_script(model, script)``: uses a caller-provided
        list of :class:`_CannedResponse` items. Tests use this to simulate
        cap hits, schema violations, or multi-step tool sequences.

    On ``force_final_answer=True`` the adapter ignores the next scripted
    item and returns a hard-coded forced submission instead. This mirrors
    real adapters' behaviour when the agent loop injects the "budget
    reached" message: the model is expected to commit.
    """

    provider_name = "mock"

    def __init__(self, model: str = "mock-default") -> None:
        self.model = model
        self._script: list[_CannedResponse] = _default_script()
        self._cursor = 0

    @classmethod
    def with_script(cls, model: str, script: list[_CannedResponse]) -> MockAdapter:
        adapter = cls(model)
        adapter._script = list(script)
        adapter._cursor = 0
        return adapter

    def reset(self) -> None:
        """Restart the script cursor for the next rendering."""
        self._cursor = 0

    async def invoke(self, request: AdapterRequest) -> AdapterResponse:
        start = time.monotonic()

        if request.force_final_answer:
            canned = _CannedResponse.submit(
                verdict="benign",
                confidence=0.1,
                malicious_flow_indices=[],
                rationale="Mock adapter: forced final answer (budget exhausted).",
            )
        else:
            if self._cursor >= len(self._script):
                raise FatalAdapterError(
                    f"mock script exhausted after {self._cursor} call(s); "
                    "lengthen the script or expect force_final_answer earlier"
                )
            canned = self._script[self._cursor]
            self._cursor += 1

        # Simulate minimal wall time so latency rollups see a non-zero value.
        if canned.simulated_wall_time_ms:
            await asyncio.sleep(canned.simulated_wall_time_ms / 1000.0)

        elapsed_ms = int((time.monotonic() - start) * 1000)
        return AdapterResponse(
            finish_reason=canned.finish_reason,  # type: ignore[arg-type]
            text=canned.text,
            tool_call=canned.tool_call,
            submit_assessment_args=canned.submit_args,
            usage=TokenUsage(
                prompt_tokens=canned.prompt_tokens,
                cached_tokens=canned.cached_tokens,
                output_tokens=canned.output_tokens,
                reasoning_tokens=canned.reasoning_tokens,
            ),
            wall_time_ms=elapsed_ms,
            provider_raw=None,
        )


__all__ = ["MockAdapter", "_CannedResponse"]
