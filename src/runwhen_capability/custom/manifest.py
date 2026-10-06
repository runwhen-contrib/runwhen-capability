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

# A word boundary is a lower-case letter or digit followed by a capital
# ("maxWait"), or the last capital of an acronym followed by a lower-case
# letter ("HTTPTimeout" -> HTTP|Timeout). A run of capitals is one word, so an
# all-caps name ("THRESHOLD", "DRY_RUN") maps to itself.
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


# The pre-0.3.0 mapping: every capital starts a new word, so an upper-case name was split
# per letter ("THRESHOLD" -> "T_H_R_E_S_H_O_L_D"). Kept only so bundles published against
# it keep running; see legacy_input_env_name.
_LEGACY_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def legacy_input_env_name(name: str) -> str:
    """The env var name input_env_name produced before 0.3.0. A published bundle is
    immutable and may read this spelling, so the host still delivers the value under
    it and validate() still accepts reading it. Equal to input_env_name for every
    camelCase / snake_case name."""
    return _LEGACY_CAMEL_RE.sub("_", name).upper()


def input_env_name(name: str) -> str:
    """The bash env var a declared input's value arrives on:
    camelCase -> SCREAMING_SNAKE_CASE (e.g. "maxWait" -> "MAX_WAIT",
    "since" -> "SINCE", "HTTPTimeout" -> "HTTP_TIMEOUT"); a name that is
    already upper case keeps its spelling ("DRY_RUN" -> "DRY_RUN"). Shared
    between validate.py (E_UNDECLARED_INPUT) and bundle.py (building the child
    process's environment), so the two never drift apart."""
    return _CAMEL_RE.sub("_", name).upper()


def python_kwarg_name(name: str) -> str:
    """The keyword argument a declared input's value arrives on in a Python
    task's `main(ctx, **inputs)`: camelCase -> snake_case ("maxWait" ->
    "max_wait"). Shared by validate.py and bundle.py for the same reason
    input_env_name() is."""
    return _CAMEL_RE.sub("_", name).lower()


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
    #: Plain-language sentences saying what the task changes. Required when readOnly is false
    #: (E_EFFECTS_REQUIRED); optional description on a read-only task.
    effects: list[str] = Field(
        default_factory=list,
        description="Plain-language sentences saying what the task changes. "
        "Required unless readOnly is true.",
    )
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


MAX_PATH_CHARS = 255

# No control characters (NUL, newline, ...) and no backslash anywhere in a
# bundle path: they are never needed, they break the unambiguous encoding
# content_hash() relies on, and a backslash is a separator on some hosts.
_BAD_PATH_CHARS_RE = re.compile(r"[\x00-\x1f\x7f\\]")


def is_allowed_path(path: str) -> bool:
    """validate.py's E_PATH_NOT_ALLOWED: `path` must be a plain relative
    file path -- non-empty "/"-separated segments, none of them "." or
    "..", no control characters or backslashes, not over MAX_PATH_CHARS --
    and sit under one of ALLOWED_DIR_PREFIXES or be one of
    ALLOWED_ROOT_FILES. The bundle host writes each file at exactly this
    path under its own directory, so anything that could resolve elsewhere
    (or to a directory) is refused here, before anything is written."""
    if not path or len(path) > MAX_PATH_CHARS or _BAD_PATH_CHARS_RE.search(path):
        return False
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return False
    if path in ALLOWED_ROOT_FILES:
        return True
    return any(path.startswith(prefix) for prefix in ALLOWED_DIR_PREFIXES)


# `name` (the capability) and every task name: a lowercase slug.
SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

# An input name: an identifier. It becomes an env var name (bash), a
# keyword argument (Python) and a file name (secret/credential inputs), so
# nothing else is safe in all three places.
INPUT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

# Env var names an input must never map onto (input_env_name): the host's
# own variables, and variables the shell, the dynamic loader or Python
# act on before a task's first line runs. KUBECONFIG is deliberately not
# here -- a `kubeconfig` credential input setting it is the documented
# behaviour.
RESERVED_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "PWD",
        "OLDPWD",
        "SHELL",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "IFS",
        "ENV",
        "CDPATH",
        "GLOBIGNORE",
        "PS4",
        "SHELLOPTS",
        "BASHOPTS",
    }
)
RESERVED_ENV_PREFIXES = ("RW_", "BASH", "LD_", "DYLD_", "PYTHON")


def is_reserved_env_name(env_name: str) -> bool:
    return env_name in RESERVED_ENV_NAMES or env_name.startswith(RESERVED_ENV_PREFIXES)


TaskFileLanguage = Literal["python", "bash"]


def task_file_language(path: str) -> TaskFileLanguage | None:
    """None for a file extension that isn't one of the two the host can run."""
    if path.endswith(".py"):
        return "python"
    if path.endswith(".sh"):
        return "bash"
    return None


#: The description the platform's /capabilities/schema.json carries, verbatim.
MANIFEST_SCHEMA_DESCRIPTION = (
    "JSON Schema for capability.yaml, apiVersion: runwhen.com/custom-capability/v1. Generated "
    "from runwhen_capability.custom.manifest.Manifest, the same Pydantic model the SDK's "
    "validate() uses for E_MANIFEST_SCHEMA -- this file is never hand-written, only "
    "regenerated from the SDK."
)


def manifest_json_schema() -> dict[str, Any]:
    """The capability.yaml JSON Schema the platform serves at /capabilities/schema.json."""
    model = Manifest.model_json_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": model.pop("title", "Manifest"),
        "description": MANIFEST_SCHEMA_DESCRIPTION,
        **model,
    }
