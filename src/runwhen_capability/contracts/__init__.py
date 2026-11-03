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

__all__ = [
    "SCHEMA_NAMES",
    "ContractError",
    "ContractViolation",
    "check",
    "data_dir",
    "schema",
    "validate",
]


def __getattr__(name: str) -> Any:
    if name == "SCHEMA_NAMES":
        return _validate.SCHEMA_NAMES
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
