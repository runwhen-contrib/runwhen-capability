"""compile_manifest(files) -- the packaged-capability manifest shape papi's
CapabilityManifest parses, compiled from a custom capability bundle's
capability.yaml: `capability`, `appliesTo`, `needs.credentials`, and
`tasks[{name, description, readOnly, file, inputs, outputs}]` with every
output's compact schema (or escape-hatch file) resolved to JSON Schema.
`execution` is omitted -- papi fills the image (the rw-task runtime).
"""

from __future__ import annotations

import json

import yaml

from .diagnostics import Diagnostic
from .manifest import Manifest, TaskSpec
from .schema_notation import compile_schema_notation
from .validate import validate


class CompileError(RuntimeError):
    """compile_manifest() found at least one error-severity Diagnostic and
    refused to compile. `diagnostics` carries the full list (errors and any
    warnings) validate() produced, for a caller that wants more than the
    summary message."""

    def __init__(self, diagnostics: list[Diagnostic]) -> None:
        self.diagnostics = diagnostics
        errors = [d for d in diagnostics if d.severity == "error"]
        summary = "; ".join(f"{d.code}: {d.message}" for d in errors) or "invalid manifest"
        super().__init__(summary)


def compile_manifest(files: dict[str, str]) -> dict:
    diagnostics = validate(files)
    if any(d.severity == "error" for d in diagnostics):
        raise CompileError(diagnostics)

    # validate() already proved capability.yaml parses and matches Manifest,
    # every task file/schema resolves, and every input is well-formed --
    # re-parsing here is cheap and keeps this function self-contained rather
    # than threading the already-validated model back out of validate().
    manifest = Manifest.model_validate(yaml.safe_load(files["capability.yaml"]))

    return {
        "capability": manifest.name,
        "appliesTo": [entry.model_dump(mode="json") for entry in manifest.appliesTo],
        "needs": {"credentials": _needs_credentials(manifest)},
        "tasks": [_compile_task(files, manifest, task) for task in manifest.tasks],
    }


def _needs_credentials(manifest: Manifest) -> list[dict]:
    """Every `type: credential` input, capability-level and task-level alike,
    deduped by name (first declaration wins -- validate() does not currently
    flag a name redeclared with a different kind/optional, so this is a
    plain first-wins merge, matching how merged_inputs already lets a
    task-level entry shadow a capability-level one of the same name)."""
    seen: dict[str, dict] = {}
    sources = [manifest.inputs] + [task.inputs for task in manifest.tasks]
    for inputs in sources:
        for name, spec in inputs.items():
            if spec.type == "credential" and name not in seen:
                seen[name] = {"name": name, "kind": spec.kind, "optional": spec.optional}
    return list(seen.values())


def _compile_task(files: dict[str, str], manifest: Manifest, task: TaskSpec) -> dict:
    # Capability-level inputs are "shared by every task" (section 1); a
    # task-level entry of the same name overrides it. The compiled shape
    # carries no separate top-level `inputs` -- each task's `inputs` here is
    # the complete, self-contained set it runs with.
    merged_inputs = {**manifest.inputs, **task.inputs}
    return {
        "name": task.name,
        "description": task.description,
        "readOnly": task.readOnly,
        "file": task.file,
        "inputs": {name: spec.model_dump(mode="json") for name, spec in merged_inputs.items()},
        "outputs": {
            name: {"schema": _compile_output_schema(files, output.schema_)}
            for name, output in task.outputs.items()
        },
    }


def _compile_output_schema(files: dict[str, str], schema_text: str) -> dict:
    if schema_text.startswith("./"):
        return json.loads(files[schema_text[2:]])
    return compile_schema_notation(schema_text)
