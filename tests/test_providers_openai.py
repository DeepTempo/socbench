"""Unit tests for the OpenAI adapter's token-usage accounting.

The Responses API reports ``output_tokens`` as the FULL output count with
reasoning tokens included as a subset. The adapter must store ``output_tokens``
as the visible (non-reasoning) portion so the provider-agnostic cost formula
(``output * output_rate + reasoning * reasoning_rate``) bills reasoning exactly
once rather than double-charging it.
"""

from __future__ import annotations

from types import SimpleNamespace

from socbench.providers.openai_adapter import _extract_openai_usage


def _usage(*, input_tokens: int, cached: int, output_tokens: int, reasoning: int):
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_tokens_details=SimpleNamespace(cached_tokens=cached),
            output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
        )
    )


def test_reasoning_is_subtracted_from_output() -> None:
    # output_tokens (1207) already includes the 963 reasoning tokens.
    u = _extract_openai_usage(
        _usage(input_tokens=5_000, cached=1_000, output_tokens=1_207, reasoning=963)
    )
    assert u.reasoning_tokens == 963
    assert u.output_tokens == 1_207 - 963  # visible-only output
    assert u.prompt_tokens == 5_000 - 1_000
    assert u.cached_tokens == 1_000


def test_no_reasoning_leaves_output_unchanged() -> None:
    u = _extract_openai_usage(
        _usage(input_tokens=200, cached=0, output_tokens=120, reasoning=0)
    )
    assert u.output_tokens == 120
    assert u.reasoning_tokens == 0


def test_reasoning_exceeding_output_clamps_to_zero() -> None:
    # Defensive: never report negative visible output even on odd provider data.
    u = _extract_openai_usage(
        _usage(input_tokens=10, cached=0, output_tokens=50, reasoning=80)
    )
    assert u.output_tokens == 0
    assert u.reasoning_tokens == 80
