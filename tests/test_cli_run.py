"""End-to-end CLI tests for `socbench run` (mock provider).

Each test exercises the full CLI argv → Runner → artifacts pipeline using
Click's :class:`CliRunner`. Real-provider paths are skipped here because
they require live API keys; the provider unit tests cover SDK-shape
correctness, and the mock adapter exercises the same agent loop end-to-end.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from socbench.cli import cli
from socbench.providers import build_adapter
from socbench.providers.base import FatalAdapterError

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cli_runner():
    return CliRunner()


@pytest.fixture
def workdir(tmp_path: Path, built_index, monkeypatch):
    """Provide a tmpdir with the repo's config + indexes/<hash>/ symlinked in.

    The CLI uses cwd-relative paths for index_root and runs_root; we change
    into ``tmp_path`` so artifacts land in the test's tmpdir, and link the
    pre-built synthetic index into ``indexes/<dataset_hash>/``.
    """
    # Copy config dir (so any relative paths resolve consistently).
    cfg_dst = tmp_path / "config"
    cfg_dst.mkdir()
    for child in (REPO_ROOT / "config").iterdir():
        if child.is_dir():
            (cfg_dst / child.name).symlink_to(child.resolve())
        else:
            (cfg_dst / child.name).write_text(child.read_text(encoding="utf-8"), encoding="utf-8")

    # Symlink the built index into cwd-relative indexes/<hash>/
    indexes_dir = tmp_path / "indexes"
    indexes_dir.mkdir()
    (indexes_dir / built_index.dataset_hash).symlink_to(built_index.index_dir.resolve())

    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_cli_help_shows_run_options(cli_runner):
    result = cli_runner.invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    for opt in ("--dataset-hash", "--mode", "--ablation", "--providers",
                "--personas", "--unit-id", "--limit", "--cost-budget-usd"):
        assert opt in result.output


def test_cli_run_defaults_to_stratified_sampling(cli_runner, workdir, built_index):
    # No --unit-id / --limit → stratified sampling selects the units.
    result = cli_runner.invoke(
        cli,
        [
            "--log-level", "WARNING",
            "run",
            "--dataset-hash", built_index.dataset_hash,
            "--providers", "mock",
            "--personas", "soc_analyst",
        ],
    )
    assert result.exit_code == 0, result.output + str(result.exception)
    output = json.loads(result.output)
    # smoke default samples at least one unit per non-empty stratum (min 8).
    assert output["rendering_count"] >= 1


def test_cli_run_smoke_mock_writes_artifacts(cli_runner, workdir, built_index):
    result = cli_runner.invoke(
        cli,
        [
            "--log-level", "WARNING",
            "run",
            "--dataset-hash", built_index.dataset_hash,
            "--providers", "mock",
            "--personas", "soc_analyst",
            "--limit", "2",
        ],
    )
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert output["rendering_count"] == 2
    assert output["aborted_for_budget"] is False

    run_dir = Path(output["run_dir"])
    # Every declared run artifact must land on disk AND be non-empty. The
    # parquet mirrors are included deliberately — a prior regression silently
    # skipped predictions_raw.parquet, which this assertion now guards.
    for f in ("run_metadata.json", "predictions_raw.jsonl", "predictions_raw.parquet",
              "predictions_per_flow.parquet", "renderings.jsonl",
              "eval_units_summary.jsonl", "tool_calls.jsonl", "summary.json",
              "index_manifest_link.json"):
        p = run_dir / f
        assert p.exists(), f"missing artifact {f}"
        assert p.stat().st_size > 0, f"empty artifact {f}"
    assert (run_dir / "prompts_used" / "soc_analyst_mock.txt").exists()


def test_cli_run_ablation_rotates_playbooks_manifest(cli_runner, workdir, built_index):
    """playbooks_off must change playbooks_manifest_sha; main must NOT."""
    main_result = cli_runner.invoke(
        cli, ["--log-level", "WARNING", "run", "--dataset-hash", built_index.dataset_hash,
              "--providers", "mock", "--personas", "soc_analyst", "--limit", "1",
              "--ablation", "main"],
    )
    off_result = cli_runner.invoke(
        cli, ["--log-level", "WARNING", "run", "--dataset-hash", built_index.dataset_hash,
              "--providers", "mock", "--personas", "soc_analyst", "--limit", "1",
              "--ablation", "playbooks_off"],
    )
    assert main_result.exit_code == 0 and off_result.exit_code == 0

    main_meta = json.loads(
        (Path(json.loads(main_result.output)["run_dir"]) / "run_metadata.json").read_text()
    )
    off_meta = json.loads(
        (Path(json.loads(off_result.output)["run_dir"]) / "run_metadata.json").read_text()
    )
    assert main_meta["prompts_manifest_sha"] == off_meta["prompts_manifest_sha"]
    assert main_meta["playbooks_manifest_sha"] != off_meta["playbooks_manifest_sha"]


def test_cli_run_providers_all_with_none_enabled_errors(cli_runner, workdir, built_index):
    result = cli_runner.invoke(
        cli, ["run", "--dataset-hash", built_index.dataset_hash,
              "--providers", "all", "--limit", "1"],
    )
    assert result.exit_code != 0
    assert "all" in result.output.lower() or "enabled" in result.output.lower()


def test_cli_run_unknown_persona_errors(cli_runner, workdir, built_index):
    result = cli_runner.invoke(
        cli, ["run", "--dataset-hash", built_index.dataset_hash, "--providers", "mock",
              "--personas", "nope", "--limit", "1"],
    )
    assert result.exit_code != 0
    assert "unknown persona" in (result.output + str(result.exception)).lower()


def test_cli_run_unknown_dataset_hash_errors(cli_runner, workdir):
    result = cli_runner.invoke(
        cli, ["run", "--dataset-hash", "deadbeef" * 4, "--providers", "mock", "--limit", "1"],
    )
    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "build-index" in result.output.lower()


# ---------------------------------------------------------------------------
# Real-provider adapters: structural lazy-import tests only (no live calls).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_real_adapter_imports_lazily_and_errors_without_sdk(provider):
    """Importing the providers package must NOT pull in optional SDKs.

    build_adapter() for a real provider must either succeed (when the SDK
    is installed AND credentials are present) OR raise FatalAdapterError with
    a helpful message — never a bare ImportError or attribute error.
    """
    try:
        adapter = build_adapter(provider, "any-model-id")
    except FatalAdapterError as exc:
        # Either the SDK isn't installed, or the API key isn't set. Both are
        # expected, recoverable conditions — verify the message is actionable
        # (points at the install command or the missing env var).
        msg = str(exc).lower()
        assert "install" in msg or "set" in msg or "api_key" in msg
    else:
        # SDK IS installed; just verify identity. (We don't make a live
        # network call here; that belongs in a marked integration test.)
        assert adapter.provider_name == provider
