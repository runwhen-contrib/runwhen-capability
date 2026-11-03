"""The V13 plugin contracts: JSON Schemas for every file and wire format at a
seam, plus an offline validator for them.

    from runwhen_capability.contracts import check, validate
    check("tasks", doc)                 # raises ContractError
    for v in validate("tasks", doc):    # or inspect every violation
        print(v.path, v.message)

The schemas are authored in the repo-level contracts/ directory and mirrored
into `_data/` by scripts/sync_contracts.py (`make contracts-sync`).
"""

from __future__ import annotations

from typing import Any

from . import _validate
from ._validate import ContractError, ContractViolation, check, data_dir, schema, validate
from .hashing import (
    TreeEntry,
    canonical_json,
    normalize_permission,
    permission_hash,
    task_hash,
    tree_hash,
)

__all__ = [
    "SCHEMA_NAMES",
    "ContractError",
    "ContractViolation",
    "TreeEntry",
    "canonical_json",
    "check",
    "data_dir",
    "normalize_permission",
    "permission_hash",
    "schema",
    "task_hash",
    "tree_hash",
    "validate",
]


def __getattr__(name: str) -> Any:
    if name == "SCHEMA_NAMES":
        return _validate.SCHEMA_NAMES
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
