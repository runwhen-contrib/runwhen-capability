"""`capability/v1` packaged capability (manifest.yaml + tasks.py) -> V13 skill directory.

The original `tasks.py` (and every other .py module of the directory) moves under `lib/`
unchanged; each task gets a `scripts/<function>.py` shim that calls its decorated function.
Findings use the codes documented in from_custom.py; the ones specific to this format:

- `needs.credentials` kinds go through the fixed map (`k8s.kubeconfig`,
  `runwhen.resourceSync`; anything else is a `generic/env` variable named after the
  credential) -- W_UPCONVERT_CREDENTIAL.
- `setup` and `${setup.x}` inputs are dropped (D40) -- W_UPCONVERT_SETUP_DROPPED.
- `invocation`, `retention`, `needs.egress` and the pool-shaped `execution` keys are
  dropped -- W_UPCONVERT_LOSSY. Output `kind` and `spill` are dropped silently, except
  `rw.findings.v1` (D63: no findings), which stays a data output with a W_UPCONVERT_REVIEW.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from ._common import (
    RESERVED_OUTPUT_NAMES,
    TASKS_API_VERSION,
    W_UPCONVERT_CREDENTIAL,
    W_UPCONVERT_LOSSY,
    W_UPCONVERT_REVIEW,
    W_UPCONVERT_SETUP_DROPPED,
    Diagnostics,
    SchemaFiles,
    UpconvertError,
    UpconvertResult,
    applies_to_block,
    credential_entry,
    credential_runtime,
    credentials_value,
    description_or,
    dump_yaml,
    input_type,
    merge_credentials,
    note_generated_titles,
    note_missing_descriptions,
    render_skill_md,
    slug,
    title_case,
    v13_name,
)
from ._tree import read_tree

MANIFEST = "manifest.yaml"
API_VERSION = "capability/v1"
FINDINGS_KIND = "rw.findings.v1"
_EFFECTS_PLACEHOLDER = "Changes state; the packaged manifest declares no effects"
_HANDLED_EXECUTION = {"resources", "requestTimeoutSeconds"}
_CONTEXT_API = re.compile(r"ctx\.(credential|workdir|repo_fs)\b")


def upconvert_packaged(src: Path, *, name: str | None = None) -> UpconvertResult:
    """Up-convert the packaged capability in directory `src`. `name` overrides the skill
    name (default: the manifest's `capability`). Raises `UpconvertError` when `src` is not a
    readable packaged capability."""
    src = Path(src)
    if not (src / MANIFEST).is_file():
        raise UpconvertError(f"no {MANIFEST} in {src}")
    files, _executable = read_tree(src)
    manifest = _parse_manifest(files[MANIFEST])
    if "tasks.py" not in files:
        raise UpconvertError(f"no tasks.py in {src}")
    diags = Diagnostics(MANIFEST)
    capability = str(manifest["capability"])
    skill_name = name or slug(capability)

    out: dict[str, bytes] = {}
    schemas = SchemaFiles(files, diags)

    # -- credentials, execution, setup, retention ------------------------------------
    credentials = _convert_credentials(manifest, diags)
    defaults: dict[str, Any] = {}
    value = credentials_value(credentials)
    if value is not None:
        defaults["credentials"] = value
    defaults.update(_convert_execution(manifest.get("execution") or {}, diags))
    if manifest.get("setup"):
        diags.add(
            W_UPCONVERT_SETUP_DROPPED,
            "setup",
            f"setup task {(manifest['setup'] or {}).get('task')!r} is dropped: V13 has no setup "
            "phase (D40); its function stays in lib/tasks.py but nothing runs it",
        )
    if manifest.get("retention") is not None:
        diags.add(W_UPCONVERT_LOSSY, "retention", "retention is dropped: V13 has no equivalent")

    # -- tasks -------------------------------------------------------------------
    task_docs: list[dict] = []
    missing: list[str] = []
    for index, task in enumerate(manifest.get("tasks") or []):
        doc, fn = _convert_task(task, index, diags, schemas, missing)
        task_docs.append(doc)
        out[doc["script"]] = _shim(fn)
    note_generated_titles(diags, len(task_docs))
    note_missing_descriptions(diags, missing)
    doc: dict[str, Any] = {
        "apiVersion": TASKS_API_VERSION,
        "runtime": credential_runtime([credentials]),
    }
    if defaults:
        doc["defaults"] = defaults
    doc["tasks"] = task_docs

    # -- code and schema files ---------------------------------------------------------
    for path, data in sorted(files.items()):
        if path.endswith(".py") and not path.startswith(("tests/", "schemas/")):
            out["lib/" + path] = data
        elif path.startswith("schemas/") and path not in schemas.moved:
            out[path] = data
    for source, dest in schemas.moved.items():
        out[dest] = files[source]
    code = "\n".join(
        data.decode("utf-8", "replace") for path, data in files.items() if path.endswith(".py")
    )
    used = sorted({m.group(1) for m in _CONTEXT_API.finditer(code)})
    if used:
        diags.add(
            W_UPCONVERT_REVIEW,
            None,
            f"uses the packaged Context API (ctx.{', ctx.'.join(used)}); V13's ctx differs "
            "(ctx.resource, ctx.settings, credentials from the environment)",
            file="lib/tasks.py",
        )

    # -- SKILL.md ----------------------------------------------------------------
    description = description_or(
        manifest.get("description"), title_case(capability), diags, "description", "the capability"
    )
    frontmatter: dict[str, Any] = {"name": skill_name, "description": description}
    applies = applies_to_block(
        [
            {"platform": e.get("platform"), "type": e.get("type"), "where": e.get("where")}
            for e in manifest.get("appliesTo") or []
        ],
        diags,
    )
    if applies:
        frontmatter["runwhen"] = {"appliesTo": applies}
    readme = files.get("README.md")
    body = (
        readme.decode("utf-8", "replace")
        if readme is not None
        else f"## When to use\n\n{description}"
    )
    result_files = {
        "SKILL.md": render_skill_md(frontmatter, body),
        "tasks.yaml": dump_yaml(doc).encode(),
    }
    result_files.update(dict(sorted(out.items())))
    return UpconvertResult(skill_name, result_files, frozenset(), diags.items)


def _parse_manifest(data: bytes) -> dict:
    try:
        raw = yaml.safe_load(data.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise UpconvertError(f"{MANIFEST} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict) or "capability" not in raw:
        raise UpconvertError(f"{MANIFEST}: missing the required 'capability' key")
    if raw.get("apiVersion", API_VERSION) != API_VERSION:
        raise UpconvertError(f"{MANIFEST}: apiVersion must be {API_VERSION}")
    return raw


def _convert_credentials(manifest: dict, diags: Diagnostics) -> list[str | dict]:
    needs = manifest.get("needs") or {}
    entries: list[str | dict] = []
    for i, need in enumerate(needs.get("credentials") or []):
        cred_name = str(need.get("name"))
        kind = need.get("kind")
        entry = credential_entry(kind, cred_name)
        entries.append(entry)
        notes = []
        if entry == "runwhen/sync":
            notes.append(
                "runwhen/sync is platform-minted and valid only on an integration's discovery "
                "entry-point task, so validation will flag it in a lone skill"
            )
        if need.get("optional"):
            notes.append("V13 has no optional credential: the tasks now need it")
        if need.get("access"):
            notes.append(f"the access level {need['access']!r} is not carried over")
        diags.add(
            W_UPCONVERT_CREDENTIAL,
            f"needs.credentials[{i}]",
            f"credential {cred_name!r} (kind {kind}) becomes "
            + (entry if isinstance(entry, str) else f"generic/env with vars {entry['vars']}")
            + "; a connection supplies it"
            + ("; " + "; ".join(notes) if notes else ""),
        )
    if needs.get("egress"):
        diags.add(
            W_UPCONVERT_LOSSY,
            "needs.egress",
            "needs.egress is dropped: V13 has no egress declaration",
        )
    return merge_credentials(entries)


def _convert_execution(execution: dict, diags: Diagnostics) -> dict:
    defaults: dict[str, Any] = {}
    seconds = execution.get("requestTimeoutSeconds")
    if seconds is not None:
        defaults["timeout"] = f"{seconds}s"
    resources = execution.get("resources")
    if isinstance(resources, dict):
        converted: dict[str, str] = {}
        differs = False
        for target, request_key, limit_key in (
            ("cpu", "cpuRequest", "cpuLimit"),
            ("memory", "memoryRequest", "memoryLimit"),
        ):
            request, limit = resources.get(request_key), resources.get(limit_key)
            chosen = request if request is not None else limit
            chosen = chosen if chosen is not None else resources.get(target)
            if chosen is not None:
                converted[target] = str(chosen)
            if request is not None and limit is not None and str(request) != str(limit):
                differs = True
        if converted:
            defaults["resources"] = converted
        if differs:
            diags.add(
                W_UPCONVERT_LOSSY,
                "execution.resources",
                "V13 has one cpu and one memory value; the request is kept and the limit dropped",
            )
    for key in execution:
        if key not in _HANDLED_EXECUTION:
            diags.add(
                W_UPCONVERT_LOSSY,
                f"execution.{key}",
                f"execution.{key} is dropped: pool and lifecycle settings are the runtime's in V13",
            )
    return defaults


def _convert_task(
    task: dict, index: int, diags: Diagnostics, schemas: SchemaFiles, missing: list[str]
) -> tuple[dict, str]:
    here = f"tasks[{index}]"
    original = str(task.get("name"))
    fn = original.replace("-", "_")
    if not fn.isidentifier():
        raise UpconvertError(f"task {original!r} is not a Python function name")
    name = slug(original)
    if name != original:
        diags.add(
            W_UPCONVERT_REVIEW,
            f"{here}.name",
            f"task {original!r} is renamed {name!r} (V13 task names are slugs); "
            f"the function stays {fn}",
        )
    title = title_case(original)
    doc: dict[str, Any] = {
        "name": name,
        "title": title,
        "description": description_or(
            task.get("description"),
            title,
            diags,
            f"{here}.description",
            name,
            missing=missing,
        ),
        "script": f"scripts/{fn}.py",
    }
    read_only = bool(task.get("readOnly", False))
    doc["readOnly"] = read_only
    if not read_only:
        doc["effects"] = [_EFFECTS_PLACEHOLDER]
        doc["destructive"] = False
        diags.add(
            W_UPCONVERT_REVIEW,
            f"{here}.readOnly",
            f"task {name!r} is not read-only (the packaged default): effects is a placeholder "
            "and destructive: false; declare what it changes",
        )
    if task.get("invocation") is not None:
        diags.add(
            W_UPCONVERT_LOSSY,
            f"{here}.invocation",
            "invocation (queued/sync) is dropped: V13 has no equivalent",
        )

    inputs: dict[str, Any] = {}
    taken: set[str] = set()
    for in_name, spec in (task.get("inputs") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        where = f"{here}.inputs.{in_name}"
        source = str(spec.get("from", "request"))
        if source.startswith("${setup."):
            diags.add(
                W_UPCONVERT_SETUP_DROPPED,
                where,
                f"input {in_name!r} comes from {source}, and setup is dropped (D40); the task "
                "must find that value itself",
            )
            continue
        if spec.get("kind") == "artifact":
            diags.add(
                W_UPCONVERT_LOSSY, where, f"artifact input {in_name!r} is dropped: V13 has none"
            )
            continue
        inputs[v13_name(in_name, taken, diags, where, "input")] = _input_value(spec, where, diags)
    if inputs:
        doc["inputs"] = inputs

    outputs: dict[str, Any] = {}
    out_taken: set[str] = set()
    for out_name, spec in (task.get("outputs") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        where = f"{here}.outputs.{out_name}"
        converted = _convert_output(spec, where, diags, schemas)
        if converted is None:
            continue
        new = v13_name(out_name, out_taken, diags, where, "output", reserved=RESERVED_OUTPUT_NAMES)
        outputs[new] = converted
    if outputs:
        doc["outputs"] = outputs
    return doc, fn


def _input_value(spec: dict, where: str, diags: Diagnostics) -> dict:
    value: dict[str, Any] = {}
    fields, problem = input_type(str(spec.get("type", "string")))
    if fields is None:
        diags.add(
            W_UPCONVERT_REVIEW, where, f"{problem}; it becomes a string input, check the type"
        )
        fields = {"type": "string"}
    value.update(fields)
    if spec.get("description"):
        value["description"] = str(spec["description"])
    if spec.get("enum"):
        value["type"] = "enum" + ("[]" if str(value["type"]).endswith("[]") else "")
        value["enum"] = list(spec["enum"])
    if "default" in spec:
        value["default"] = spec["default"]
    elif spec.get("optional"):
        value["required"] = False
    return value


def _convert_output(
    spec: dict, where: str, diags: Diagnostics, schemas: SchemaFiles
) -> dict | None:
    kind = spec.get("kind")
    if kind == "artifact":
        artifact: dict[str, Any] = {
            "contentType": str(spec.get("contentType", "application/octet-stream"))
        }
        if spec.get("maxSize") is not None:
            artifact["maxSize"] = str(spec["maxSize"])
        return {"artifact": artifact}
    schema = spec.get("schema")
    if not isinstance(schema, str) or not schema:
        diags.add(W_UPCONVERT_LOSSY, where, "output has no schema and is dropped")
        return None
    if not schema.startswith("./") and schema.endswith(".json"):
        schema = "./" + schema
    if kind == FINDINGS_KIND:
        diags.add(
            W_UPCONVERT_REVIEW,
            where,
            "D63: no findings in V13; kept as a data output with its schema, rework it as data",
        )
    return {"schema": schemas.resolve(schema, f"{where}.schema")}


def _shim(fn: str) -> bytes:
    if fn == "main":  # `def main` below would shadow the import
        text = (
            "from tasks import main as _task\n\n\ndef main(ctx, **inputs):\n"
            "    return _task(ctx, **inputs)\n"
        )
    else:
        text = (
            f"from tasks import {fn}\n\n\ndef main(ctx, **inputs):\n"
            f"    return {fn}(ctx, **inputs)\n"
        )
    return text.encode()
