"""The V13 plugin contracts: JSON Schemas for every file and wire format at a
seam, plus an offline validator for them.

    from runwhen_capability.contracts import check, validate
    check("tasks", doc)                 # raises ContractError
    for v in validate("tasks", doc):    # or inspect every violation
        print(v.path, v.message)

The plugin archive (deterministic ustar tar, `contracts.archive.build/read/verify`)
and the canonical-JSON hashes live alongside. `load_yaml` / `split_frontmatter` parse
plugin files as YAML 1.2 (`no` is a string, timestamps stay strings).

The schemas are authored in the repo-level contracts/ directory and mirrored
into `_data/` by scripts/sync_contracts.py (`make contracts-sync`).
"""

from __future__ import annotations

from typing import Any

from . import _validate
from ._validate import ContractError, ContractViolation, check, data_dir, schema, validate
from ._yaml12 import load_yaml, split_frontmatter
from .archive import ArchiveError, TreeEntry, archive_sha256
from .hashing import (
    canonical_json,
    normalize_permission,
    permission_hash,
    task_hash,
)

__all__ = [
    "SCHEMA_NAMES",
    "ArchiveError",
    "ContractError",
    "ContractViolation",
    "TreeEntry",
    "archive_sha256",
    "canonical_json",
    "check",
    "data_dir",
    "load_yaml",
    "normalize_permission",
    "permission_hash",
    "schema",
    "split_frontmatter",
    "task_hash",
    "validate",
]


def __getattr__(name: str) -> Any:
    if name == "SCHEMA_NAMES":
        return _validate.SCHEMA_NAMES
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
