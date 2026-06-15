"""Deterministic hashing utilities used across socbench.

All artifact identifiers (``dataset_hash``, ``tools_manifest_sha``,
``prompts_manifest_sha``, ``playbooks_manifest_sha``, ``rendering_id``,
``eval_unit_id``) flow through helpers in this module so the algorithm is
controlled in one place. Stability across Python versions and platforms is a
hard requirement: every published number is tagged with one or more of these
hashes.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_BLAKE2B_DIGEST_BYTES = 16  # 128-bit; hex-encoded that is 32 chars.


def canonical_json(value: Any) -> bytes:
    """Serialize ``value`` deterministically.

    JSON keys are sorted; ``ensure_ascii`` is enabled so the encoding is
    byte-stable across locales; separators are tight so whitespace can never
    perturb the hash.
    """
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def _json_default(obj: Any) -> Any:
    # Keep this list short; surprise types should raise so callers notice.
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON-serializable")


def hash_bytes(data: bytes, *, digest_bytes: int = _BLAKE2B_DIGEST_BYTES) -> str:
    """Hash arbitrary bytes with BLAKE2b, hex-encoded."""
    return hashlib.blake2b(data, digest_size=digest_bytes).hexdigest()


def hash_obj(value: Any, *, digest_bytes: int = _BLAKE2B_DIGEST_BYTES) -> str:
    """Hash any JSON-serializable object deterministically."""
    return hash_bytes(canonical_json(value), digest_bytes=digest_bytes)


def hash_file(path: str | Path, *, digest_bytes: int = _BLAKE2B_DIGEST_BYTES) -> str:
    """Hash a file's contents in a streaming fashion."""
    h = hashlib.blake2b(digest_size=digest_bytes)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_hash(parts: Mapping[str, Any], *, digest_bytes: int = _BLAKE2B_DIGEST_BYTES) -> str:
    """Hash a manifest-shaped dict.

    Each value is canonicalized independently and the resulting (key, hash)
    pairs are then themselves hashed. This makes a single perturbed part
    easy to attribute when comparing two manifests.
    """
    part_hashes = {key: hash_obj(parts[key], digest_bytes=digest_bytes) for key in sorted(parts)}
    return hash_obj(part_hashes, digest_bytes=digest_bytes)


def short_hash(value: Any, *, length: int = 8) -> str:
    """Short hash for human-facing IDs (e.g. ``manifest`` portion of ``run_id``)."""
    full = hash_obj(value) if not isinstance(value, str) else hash_bytes(value.encode("utf-8"))
    return full[:length]


def hash_flow_ids(flow_ids: Iterable[int]) -> str:
    """Stable hash of a flow-id set (used in ``eval_unit_id`` derivation).

    The input order is irrelevant; we sort first.
    """
    sorted_ids = sorted(int(x) for x in flow_ids)
    return hash_obj(sorted_ids)
