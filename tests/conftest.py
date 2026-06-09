"""Shared pytest fixtures: a tiny built index for tool / scoring tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from socbench.config import DatasetEntry, IndexConfig
from socbench.index import BuildResult, build_index_for_dataset
from socbench.schema import load_schema
from tests.synthetic_flows import SyntheticFlowSpec, write_synthetic_parquet

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"
SCHEMA_PATH = CONFIG_DIR / "schema.json"


@pytest.fixture(scope="session")
def schema():
    return load_schema(SCHEMA_PATH)


@pytest.fixture(scope="session")
def synthetic_parquet(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out_dir = tmp_path_factory.mktemp("synthetic_input")
    parquet_path = out_dir / "synthetic.parquet"
    write_synthetic_parquet(parquet_path, SyntheticFlowSpec())
    return parquet_path


@pytest.fixture(scope="session")
def built_index(
    synthetic_parquet: Path,
    tmp_path_factory: pytest.TempPathFactory,
    schema,  # type: ignore[no-untyped-def]
) -> BuildResult:
    index_root = tmp_path_factory.mktemp("indexes")
    dataset = DatasetEntry(paths=[synthetic_parquet], label_group="malicious")
    return build_index_for_dataset(
        dataset_name="synth",
        dataset=dataset,
        schema=schema,
        index_cfg=IndexConfig(),
        index_root=index_root,
    )
