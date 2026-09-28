"""`rwtask schemas` -- export each task output's JSON Schema from the pydantic
model that produces it, so a schema is generated, never hand-written, and
cannot drift from the code.

A capability repo lists what to export in its pyproject.toml, one table per
capability directory, mapping a file under that directory's schemas/ to a
`module:attribute` reference:

    [tool.rwtask.schemas."capabilities/rw-checks"]
    "findings.v1.json" = "runwhen_capability.models:FindingsResult"

`rwtask schemas` writes every listed file; `rwtask schemas --check` writes
nothing and fails if any listed file is missing or differs from what the
export would write, byte for byte. Files under schemas/ that are not listed
(an older published version, say) are left alone.

The project directory (the one holding pyproject.toml) is put on sys.path
before models are imported, so a repo's own modules resolve without being
installed.
"""

from __future__ import annotations

import importlib
import json
import posixpath
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

PYPROJECT = "pyproject.toml"


class SchemaConfigError(RuntimeError):
    """The [tool.rwtask.schemas] configuration is missing or malformed, or
    names a model that cannot be imported."""


@dataclass(frozen=True)
class SchemaTarget:
    capability_dir: str  # relative to the project directory, as configured
    filename: str  # a plain file name under <capability_dir>/schemas/
    model: str  # "module:attribute"

    @property
    def relpath(self) -> str:
        return posixpath.join(self.capability_dir, "schemas", self.filename)


def load_targets(project_dir: Path) -> list[SchemaTarget]:
    """Every configured schema file, in configuration order. Raises
    SchemaConfigError on a missing pyproject.toml, a missing table, or a
    malformed entry."""
    pyproject = Path(project_dir) / PYPROJECT
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SchemaConfigError(f"{pyproject}: no such file") from None
    except tomllib.TOMLDecodeError as exc:
        raise SchemaConfigError(f"{pyproject}: not valid TOML: {exc}") from exc

    table = data.get("tool", {}).get("rwtask", {}).get("schemas")
    if not table:
        raise SchemaConfigError(f"{pyproject}: has no [tool.rwtask.schemas] table")

    targets = []
    for capability_dir, files in table.items():
        normalized = posixpath.normpath(capability_dir)
        if posixpath.isabs(normalized) or normalized == ".." or normalized.startswith("../"):
            raise SchemaConfigError(
                f"{pyproject}: capability directory {capability_dir!r} must be relative and "
                "stay under the project directory"
            )
        if not isinstance(files, dict):
            raise SchemaConfigError(
                f"{pyproject}: [tool.rwtask.schemas.{capability_dir!r}] must be a table of "
                '"<file>.json" = "module:attribute" entries'
            )
        for filename, model in files.items():
            if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
                raise SchemaConfigError(
                    f"{pyproject}: {filename!r} under {capability_dir!r} must be a plain file "
                    "name (it is written to that directory's schemas/)"
                )
            if not filename.endswith(".json"):
                raise SchemaConfigError(f"{pyproject}: {filename!r} must end in .json")
            if not isinstance(model, str) or ":" not in model:
                raise SchemaConfigError(
                    f"{pyproject}: {filename!r} under {capability_dir!r} must name its model as "
                    f"'module:attribute', got {model!r}"
                )
            targets.append(SchemaTarget(capability_dir, filename, model))
    return targets


def resolve_model(model: str, project_dir: Path) -> Any:
    """Imports `module:attribute` (the attribute may be dotted)."""
    project = str(Path(project_dir).resolve())
    if project not in sys.path:
        sys.path.insert(0, project)
    module_name, _, attribute = model.partition(":")
    try:
        value: Any = importlib.import_module(module_name)
        for part in attribute.split("."):
            value = getattr(value, part)
    except (ImportError, AttributeError) as exc:
        raise SchemaConfigError(f"cannot import {model!r}: {exc}") from exc
    return value


def render(target: SchemaTarget, project_dir: Path) -> str:
    """The exact text written for `target`: the model's JSON Schema, indented
    two spaces, with a trailing newline."""
    schema = TypeAdapter(resolve_model(target.model, project_dir)).json_schema()
    return json.dumps(schema, indent=2) + "\n"


def write_schemas(project_dir: Path) -> list[str]:
    """Writes every configured schema file; returns their project-relative
    paths, in configuration order."""
    project_dir = Path(project_dir)
    written = []
    for target in load_targets(project_dir):
        text = render(target, project_dir)
        path = project_dir / target.relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(target.relpath)
    return written


def check_schemas(project_dir: Path) -> tuple[int, list[str]]:
    """(how many files were checked, one problem line per missing or
    out-of-date file). Writes nothing."""
    project_dir = Path(project_dir)
    targets = load_targets(project_dir)
    problems = []
    for target in targets:
        text = render(target, project_dir)
        path = project_dir / target.relpath
        try:
            on_disk = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            problems.append(f"{target.relpath}: missing")
            continue
        if on_disk != text:
            problems.append(f"{target.relpath}: out of date")
    return len(targets), problems
