"""Compile a validated plugin tree to the `compiled-plugin` v1 document
(CONTRACTS.md "Compiled plugin", archive.md section 7).

    doc = compile_tree(load_dir(path))            # or compile_entries(archive.read(tar))

Everything is resolved so papi and the registry can validate, match and preview
without unpacking the archive: defaults applied, credentials normalized,
permissions hashed, settings merged, inputs with `required` resolved, an
`inputSchema` per task, output JSON Schemas inlined, and the per-task `hash`.

The output is deterministic: skills, tasks (file order within a skill), rules and
knowledge have a fixed order, and it never depends on the order of the entries.
All paths inside the document (`script`, `files`, `schemaRef`) are relative to the
plugin root, the same paths the lease and the archive use.

A skill-only tree (a workspace skill revision: no plugin.yaml) compiles when the
caller passes `workspace_revision`: plugin `workspace` at `0.0.0-rev.<n>`, task refs
`workspace:<skill>/<task>`, no integration.

`compile_tree` raises `PluginInvalid` when `validate_tree` finds an error, and
checks its own output against the schema before returning it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import posixpath
from collections.abc import Iterable
from typing import Any

from .. import __version__
from ..contracts import TreeEntry, check, permission_hash, schema, task_hash
from ..custom.diagnostics import Diagnostic
from ..custom.schema_notation import compile_schema_notation
from . import PluginInvalid
from .tree import PluginTree, load_entries
from .validate import E_PLUGIN_MISSING, validate_tree

API_VERSION = "runwhen.com/compiled-plugin/v1"
DEFAULT_RUNTIME = "generic@1"
WORKSPACE_PLUGIN = "workspace"
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def compile_entries(
    entries: Iterable[TreeEntry],
    *,
    sdk_version: str | None = None,
    workspace_revision: int | None = None,
) -> dict:
    """`compile_tree(load_entries(entries))`; accepts `contracts.archive.read` output."""
    return compile_tree(
        load_entries(entries), sdk_version=sdk_version, workspace_revision=workspace_revision
    )


def compile_tree(
    tree: PluginTree,
    *,
    sdk_version: str | None = None,
    workspace_revision: int | None = None,
) -> dict:
    """The compiled-plugin document for `tree`. Raises PluginInvalid if validation
    finds an error (warnings do not block).

    `workspace_revision` (>= 1) compiles a skill-only tree as the workspace plugin at
    version `0.0.0-rev.<n>`; without it such a tree is E_PLUGIN_MISSING. It is a
    ValueError for a tree that has a plugin.yaml or for a revision below 1."""
    if workspace_revision is not None and (
        isinstance(workspace_revision, bool)
        or not isinstance(workspace_revision, int)
        or workspace_revision < 1
    ):
        raise ValueError(f"workspace_revision must be an integer >= 1, got {workspace_revision!r}")
    if workspace_revision is not None and tree.plugin is not None:
        raise ValueError("workspace_revision applies only to a tree without a plugin.yaml")
    errors = [d for d in validate_tree(tree) if d.severity == "error"]
    if errors:
        raise PluginInvalid(errors)
    if tree.plugin is not None:
        plugin = copy.deepcopy(tree.plugin)
    elif workspace_revision is not None and tree.skills:
        plugin = _workspace_plugin(tree, workspace_revision)
    else:
        raise PluginInvalid(
            [
                Diagnostic(
                    code=E_PLUGIN_MISSING,
                    file="plugin.yaml",
                    message="only a plugin with a plugin.yaml compiles"
                    + (
                        ""
                        if workspace_revision is None
                        else " (a workspace skill revision needs at least one skill)"
                    ),
                )
            ]
        )
    plugin_name = plugin["name"]
    tasks: list[dict] = []
    for name in sorted(tree.skills):
        tasks.extend(_compile_tasks(tree, plugin_name, name))
    doc = {
        "apiVersion": API_VERSION,
        "compiler": {"name": "runwhen-capability", "version": sdk_version or __version__},
        "plugin": plugin,
        "integration": _compile_integration(tree),
        "skills": [_compile_skill(tree, name) for name in sorted(tree.skills)],
        "tasks": tasks,
        "rules": [_compile_rule(tree, stem) for stem in sorted(tree.rules)],
        "knowledge": [_compile_knowledge(tree, stem) for stem in sorted(tree.knowledge)],
    }
    check("compiled-plugin", doc)
    return doc


def _workspace_plugin(tree: PluginTree, revision: int) -> dict:
    description = "Workspace skill " + ", ".join(sorted(tree.skills))
    return {
        "name": WORKSPACE_PLUGIN,
        "version": f"0.0.0-rev.{revision}",
        "description": description[:1024],
    }


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _json_file(tree: PluginTree, path: str) -> dict:
    return json.loads(tree.files[path].decode("utf-8"))


def _compile_integration(tree: PluginTree) -> dict | None:
    if tree.integration is None:
        return None
    out: dict[str, Any] = {"definition": copy.deepcopy(tree.integration)}
    permissions = tree.integration.get("permissions")
    if isinstance(permissions, dict) and isinstance(permissions.get("schema"), str):
        out["permissionSchema"] = _json_file(tree, _integration_path(permissions["schema"]))
    facet_schemas = {
        facet["key"]: _json_file(tree, _integration_path(facet["schema"]))
        for facet in tree.integration.get("facets") or []
    }
    if facet_schemas:
        out["facetSchemas"] = facet_schemas
    return out


def _integration_path(ref: str) -> str:
    return "integration/" + posixpath.normpath(ref)


def _compile_skill(tree: PluginTree, name: str) -> dict:
    skill = tree.skills[name]
    out: dict[str, Any] = {
        "name": name,
        "frontmatter": copy.deepcopy(skill.frontmatter),
        "skillMdSha256": _sha256(skill.files["SKILL.md"]),
        "files": sorted(f"skills/{name}/{path}" for path in skill.files),
        "taskNames": [t["name"] for t in (skill.tasks or {}).get("tasks", [])],
    }
    if skill.tasks is not None:
        out["runtime"] = skill.tasks.get("runtime", DEFAULT_RUNTIME)
    return out


def _credentials(value: Any) -> list[dict]:
    if value is None:
        return []
    items = [value] if isinstance(value, str) else value
    return [{"type": item} if isinstance(item, str) else copy.deepcopy(item) for item in items]


def _compile_tasks(tree: PluginTree, plugin_name: str, skill_name: str) -> list[dict]:
    skill = tree.skills[skill_name]
    if skill.tasks is None:
        return []
    doc = skill.tasks
    defaults = doc.get("defaults") or {}
    runwhen = skill.frontmatter.get("runwhen") or {}
    base = f"skills/{skill_name}/"
    compiled = []
    for task in doc["tasks"]:
        script = base + posixpath.normpath(task["script"])
        out: dict[str, Any] = {
            "ref": f"{plugin_name}:{skill_name}/{task['name']}",
            "skill": skill_name,
            "name": task["name"],
            "title": task["title"],
            "description": task["description"],
            "script": script,
            "runtime": doc.get("runtime", DEFAULT_RUNTIME),
        }
        for key in ("role", "appliesTo"):
            if key in runwhen:
                out[key] = copy.deepcopy(runwhen[key])
        out["readOnly"] = task["readOnly"]
        for key in ("effects", "destructive"):
            if key in task:
                out[key] = copy.deepcopy(task[key])
        out["idempotent"] = task.get("idempotent", False)
        out["dryRun"] = task.get("dryRun", False)
        out["credentials"] = _credentials(
            task["credentials"] if "credentials" in task else defaults.get("credentials")
        )
        out["permissions"] = [
            {"rule": copy.deepcopy(rule), "hash": permission_hash(rule)}
            for rule in task.get("permissions") or []
        ]
        out["settings"] = copy.deepcopy(
            {**(doc.get("settings") or {}), **(task.get("settings") or {})}
        )
        inputs = {}
        for name, spec in (task.get("inputs") or {}).items():
            spec = copy.deepcopy(spec)
            spec["required"] = spec.get("required", "default" not in spec)
            inputs[name] = spec
        out["inputs"] = inputs
        out["inputSchema"] = _input_schema(tree, base, inputs)
        out["outputs"], schema_files = _compile_outputs(tree, base, task.get("outputs") or {})
        for key in ("timeout", "resources"):
            if key in task:
                out[key] = copy.deepcopy(task[key])
            elif key in defaults:
                out[key] = copy.deepcopy(defaults[key])
        out["tags"] = copy.deepcopy(task.get("tags", runwhen.get("tags", [])))
        files = {script: tree.files[script]}
        for path in tree.files:
            if path.startswith(base + "lib/"):
                files[path] = tree.files[path]
        for path in schema_files:
            files[path] = tree.files[path]
        out["hash"] = task_hash(out, files)
        compiled.append(out)
    return compiled


# -- input and output schemas ------------------------------------------------------


def _schema_value(tree: PluginTree, base: str, value: Any) -> tuple[dict, str | None]:
    """(JSON Schema, plugin-root path of the file it came from or None) for a
    `schema:` value: an inline mapping, a `./schemas/...` file or compact notation."""
    if isinstance(value, dict):
        return copy.deepcopy(value), None
    if value.startswith("./"):
        path = base + posixpath.normpath(value)
        return _json_file(tree, path), path
    return compile_schema_notation(value), None


def _input_property(tree: PluginTree, base: str, spec: dict) -> dict:
    kind: str = spec["type"]
    is_array = kind.endswith("[]")
    name = kind[:-2] if is_array else kind
    if name in ("string", "integer", "number", "boolean"):
        item: dict[str, Any] = {"type": name}
    elif name == "duration":
        item = {"type": "string", "pattern": schema("common")["$defs"]["duration"]["pattern"]}
    elif name == "datetime":
        item = {"type": "string", "format": "date-time"}
    elif name == "enum":
        item = {"enum": copy.deepcopy(spec["enum"])}
    else:  # object
        item = _schema_value(tree, base, spec["schema"])[0]
    constraints = {"minimum": spec.get("min"), "maximum": spec.get("max")}
    for key, value in constraints.items():
        if value is not None:
            item[key] = value
    if "pattern" in spec:
        if "pattern" in item:  # a duration: both must hold
            item["allOf"] = [{"pattern": spec["pattern"]}]
        else:
            item["pattern"] = spec["pattern"]
    prop = {"type": "array", "items": item} if is_array else item
    for key in ("description", "default", "examples"):
        if key in spec:
            prop[key] = copy.deepcopy(spec[key])
    return prop


def _input_schema(tree: PluginTree, base: str, inputs: dict) -> dict:
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "type": "object",
        "additionalProperties": False,
        "properties": {name: _input_property(tree, base, spec) for name, spec in inputs.items()},
        "required": [name for name, spec in inputs.items() if spec["required"]],
    }


def _compile_outputs(tree: PluginTree, base: str, outputs: dict) -> tuple[dict, list[str]]:
    compiled: dict[str, dict] = {}
    files: list[str] = []
    for name, spec in outputs.items():
        if "schema" not in spec:
            compiled[name] = copy.deepcopy(spec)  # artifact or platform kind
            continue
        json_schema, path = _schema_value(tree, base, spec["schema"])
        entry: dict[str, Any] = {"jsonSchema": json_schema}
        if path is not None:
            entry["schemaRef"] = path
            files.append(path)
        if "description" in spec:
            entry["description"] = spec["description"]
        compiled[name] = entry
    return compiled, files


# -- rules and knowledge -------------------------------------------------------------


def _compile_rule(tree: PluginTree, stem: str) -> dict:
    frontmatter, body = tree.rules[stem]
    return {
        "name": frontmatter["name"],
        "description": frontmatter["description"],
        "body": body,
        "words": len(body.split()),
        "sha256": _sha256(tree.files[f"rules/{stem}.md"]),
    }


def _compile_knowledge(tree: PluginTree, stem: str) -> dict:
    frontmatter, body = tree.knowledge[stem]
    out: dict[str, Any] = {"name": frontmatter["name"], "description": frontmatter["description"]}
    for key in ("appliesTo", "resources", "tags"):
        if key in frontmatter:
            out[key] = copy.deepcopy(frontmatter[key])
    out["body"] = body
    out["sha256"] = _sha256(tree.files[f"knowledge/{stem}.md"])
    return out
