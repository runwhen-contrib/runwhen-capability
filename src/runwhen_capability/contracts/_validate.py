"""Offline validation of V13 documents against the contracts' JSON Schemas.

The schemas live in the repo-level contracts/ directory and are mirrored into
the package as `_data/` by scripts/sync_contracts.py, so an installed wheel
carries exactly the schemas it validates against. All of them are loaded into
one `referencing.Registry` keyed on `$id`: a `$ref` from one schema to another
resolves in memory. A `$ref` to an `$id` that is not in the registry raises --
the registry has no `retrieve=` callback, so nothing here ever fetches.
"""

from __future__ import annotations

import copy
import importlib.resources
import json
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

_SUFFIX = ".schema.json"


@dataclass(frozen=True)
class ContractViolation:
    """One way a document breaks a schema."""

    path: str  # JSON pointer to the offending value; "" is the document root
    message: str
    schema_path: str  # JSON pointer to the failing keyword inside the schema


class ContractError(ValueError):
    """Raised by `check()` when a document does not satisfy its schema."""

    def __init__(self, violations: list[ContractViolation]) -> None:
        self.violations = violations
        lines = [f"{v.path or '/'}: {v.message}" for v in violations]
        super().__init__(f"{len(violations)} contract violation(s):\n" + "\n".join(lines))


class _Loaded:
    """The schemas found under one data dir, and the registry built from them."""

    def __init__(self, root: Traversable) -> None:
        self.schemas: dict[str, dict[str, Any]] = {}
        schemas_dir = root / "schemas"
        if schemas_dir.is_dir():
            for entry in schemas_dir.iterdir():
                if entry.name.endswith(_SUFFIX):
                    name = entry.name[: -len(_SUFFIX)]
                    self.schemas[name] = json.loads(entry.read_text(encoding="utf-8"))
        self.names = tuple(sorted(self.schemas))
        resources = [
            (doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012))
            for doc in self.schemas.values()
            if "$id" in doc
        ]
        self.registry: Registry = Registry().with_resources(resources)
        self.validators: dict[str, Draft202012Validator] = {}

    def validator(self, name: str) -> Draft202012Validator:
        if name not in self.schemas:
            raise KeyError(name)
        if name not in self.validators:
            self.validators[name] = Draft202012Validator(
                self.schemas[name],
                registry=self.registry,
                format_checker=Draft202012Validator.FORMAT_CHECKER,
            )
        return self.validators[name]


# Keyed on the data dir, so repointing `data_dir` (tests do) loads afresh.
_cache: dict[str, _Loaded] = {}


def _reset_cache() -> None:
    _cache.clear()


def data_dir() -> Traversable:
    """The packaged copy of contracts/."""
    return importlib.resources.files("runwhen_capability.contracts") / "_data"


def _loaded() -> _Loaded:
    root = data_dir()
    key = str(root)
    if key not in _cache:
        _cache[key] = _Loaded(root)
    return _cache[key]


def __getattr__(name: str) -> Any:
    # SCHEMA_NAMES is computed on first use, not at import, so importing the
    # package never reads files and `data_dir` can be repointed.
    if name == "SCHEMA_NAMES":
        return _loaded().names
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def schema(name: str) -> dict[str, Any]:
    """The JSON Schema called `name` (a copy). KeyError if there is none."""
    loaded = _loaded()
    if name not in loaded.schemas:
        raise KeyError(name)
    return copy.deepcopy(loaded.schemas[name])


def _pointer(parts: Any) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def validate(name: str, doc: Any) -> list[ContractViolation]:
    """Every way `doc` breaks the schema `name`, sorted by path; [] if valid.

    KeyError for an unknown schema name. A `$ref` that points outside the
    packaged schemas raises (the underlying referencing error) rather than
    being fetched.
    """
    validator = _loaded().validator(name)
    violations = [
        ContractViolation(
            path=_pointer(error.absolute_path),
            message=error.message,
            schema_path=_pointer(error.absolute_schema_path),
        )
        for error in validator.iter_errors(doc)
    ]
    return sorted(violations, key=lambda v: (v.path, v.message))


def check(name: str, doc: Any) -> None:
    """Raise ContractError if `doc` does not satisfy the schema `name`."""
    violations = validate(name, doc)
    if violations:
        raise ContractError(violations)
