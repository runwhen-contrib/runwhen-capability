"""runwhen_capability.custom -- parses, validates and compiles a custom
capability bundle (`capability.yaml` plus its `tasks/`, `lib/`, `schemas/`,
`tests/` files).

Public API:

    validate(files: dict[str, str]) -> list[Diagnostic]
    compile_manifest(files: dict[str, str]) -> dict
    content_hash(files: dict[str, str]) -> str
    task_hash(files: dict[str, str], task: str) -> str

`files` is always a bundle-relative-path -> UTF-8 text mapping. No filesystem
access, no execution -- everything here is static.
"""

from __future__ import annotations

from .compiler import CompileError, compile_manifest
from .diagnostics import (
    E_DUPLICATE_NAME,
    E_INPUT_TYPE,
    E_LIMIT,
    E_MANIFEST_SCHEMA,
    E_OUTPUT_SCHEMA,
    E_OUTPUT_TOO_LARGE,
    E_OUTPUT_UNDECLARED,
    E_PATH_NOT_ALLOWED,
    E_READONLY_WRITE,
    E_SCHEMA_NOTATION,
    E_TASK_FILE_MISSING,
    E_TIMEOUT,
    E_UNDECLARED_INPUT,
    Diagnostic,
)
from .hashing import content_hash, task_hash
from .manifest import Manifest
from .schema_notation import SchemaNotationError, compile_schema_notation
from .validate import validate

__all__ = [
    "Diagnostic",
    "CompileError",
    "SchemaNotationError",
    "Manifest",
    "validate",
    "compile_manifest",
    "compile_schema_notation",
    "content_hash",
    "task_hash",
    "E_MANIFEST_SCHEMA",
    "E_SCHEMA_NOTATION",
    "E_PATH_NOT_ALLOWED",
    "E_LIMIT",
    "E_TASK_FILE_MISSING",
    "E_UNDECLARED_INPUT",
    "E_OUTPUT_UNDECLARED",
    "E_READONLY_WRITE",
    "E_DUPLICATE_NAME",
    "E_INPUT_TYPE",
    "E_OUTPUT_SCHEMA",
    "E_OUTPUT_TOO_LARGE",
    "E_TIMEOUT",
]
