"""Pydantic models for `capability.yaml`, `apiVersion: runwhen.com/custom-capability/v1`
-- the manifest format a custom capability bundle ships.

These models exist for structural validation (a mis-shaped manifest becomes
E_MANIFEST_SCHEMA in validate.py) and for typed access in compiler.py. They
are permissive about *unknown* keys (pydantic's default `extra="ignore"`) so
a newer manifest field a validator predates does not hard-fail an otherwise
valid bundle -- and so a `_LineDict`'s bookkeeping never has to be stripped
before `model_validate`.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

API_VERSION = "runwhen.com/custom-capability/v1"

# Every path a bundle file may sit at: these four directories (recursively)
# plus the two named files at the bundle root. Enforced by validate.py
# (E_PATH_NOT_ALLOWED), independently of what the manifest itself references.
ALLOWED_DIR_PREFIXES = ("tasks/", "lib/", "schemas/", "tests/")
ALLOWED_ROOT_FILES = ("capability.yaml", "README.md")

# validate.py's E_LIMIT.
MAX_FILE_BYTES = 64 * 1024
MAX_FILES = 40
MAX_BUNDLE_BYTES = 256 * 1024

_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def input_env_name(name: str) -> str:
    """The bash env var a declared input's value arrives on:
    camelCase -> SCREAMING_SNAKE_CASE (e.g. "maxWait" -> "MAX_WAIT",
    "since" -> "SINCE"). Shared between validate.py (E_UNDECLARED_INPUT) and
    bundle.py (building the child process's environment), so the two never
    drift apart."""
    return _CAMEL_RE.sub("_", name).upper()


class InputSpec(BaseModel):
    """One `inputs.<name>` entry, at capability or task level."""

    type: str
    kind: str | None = None  # credential kind, e.g. "k8s.kubeconfig"
    optional: bool = False
    default: Any = None
    runtime: bool = False


class OutputSpec(BaseModel):
    """One task's `outputs.<name>` entry. `schema` is either compact notation
    or, when it starts with "./", a path to a JSON Schema file in the bundle
    -- compiler.py resolves which."""

    schema_: str = Field(alias="schema")

    model_config = {"populate_by_name": True}


class SetupSpec(BaseModel):
    file: str


class TaskSpec(BaseModel):
    name: str
    file: str
    description: str = ""
    readOnly: bool = False
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)


class AppliesToEntry(BaseModel):
    """`{platform, type, where}` -- `where` stays a plain dict (its keys,
    like "labels.app", are literal dotted strings, not nested paths) so it
    round-trips into compile_manifest()'s output byte-for-byte."""

    platform: str
    type: str
    where: dict[str, Any] = Field(default_factory=dict)


class Manifest(BaseModel):
    apiVersion: str
    name: str
    description: str = ""
    appliesTo: list[AppliesToEntry] = Field(default_factory=list)
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    setup: SetupSpec | None = None
    tasks: list[TaskSpec] = Field(default_factory=list)


def is_allowed_path(path: str) -> bool:
    """validate.py's E_PATH_NOT_ALLOWED: `path` must be relative, contain no
    ".." segment, and sit under one of ALLOWED_DIR_PREFIXES or be one of
    ALLOWED_ROOT_FILES."""
    if path.startswith("/") or path.startswith("../") or "/../" in path or path == "..":
        return False
    if path in ALLOWED_ROOT_FILES:
        return True
    return any(path.startswith(prefix) for prefix in ALLOWED_DIR_PREFIXES)


TaskFileLanguage = Literal["python", "bash"]


def task_file_language(path: str) -> TaskFileLanguage | None:
    """None for a file extension that isn't one of the two the host can run."""
    if path.endswith(".py"):
        return "python"
    if path.endswith(".sh"):
        return "bash"
    return None
