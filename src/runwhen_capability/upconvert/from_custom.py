"""`runwhen.com/custom-capability/v1` bundle -> V13 skill directory (wiki 42a, CAP-1).

    result = upconvert_custom(Path("bundles/pgbouncer-health"))
    write_result(result, Path("skills") / result.skill_name)

What maps, what is dropped and why is in the findings the result carries:

- W_UPCONVERT_LOSSY -- information V13 cannot hold (`where` keys other than labels, a
  `resource` input: the target is automatic, stray files).
- W_UPCONVERT_REVIEW -- a stand-in the author should look at (generated title, a write
  task's `destructive: false`, a renamed input or output, a script that reads an input the
  way custom tasks did).
- W_UPCONVERT_CREDENTIAL -- a `credential`/`secret` input became a `credentials` entry.
- W_UPCONVERT_SETUP_DROPPED -- the bundle's `setup` (D40: no setup phase).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..custom.manifest import (
    API_VERSION,
    InputSpec,
    Manifest,
    TaskSpec,
    input_env_name,
    legacy_input_env_name,
    python_kwarg_name,
)
from ..custom.yaml_lines import safe_load
from ._common import (
    KUBECONFIG_TYPE,
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

MANIFEST = "capability.yaml"
_DESCRIPTION_FALLBACK_EFFECT = "Changes state; the source bundle did not say how"


def upconvert_custom(src: Path, *, name: str | None = None) -> UpconvertResult:
    """Up-convert the custom bundle in directory `src`. `name` overrides the skill name
    (default: the manifest's `name`). Raises `UpconvertError` when `src` is not a readable
    custom bundle."""
    src = Path(src)
    if not (src / MANIFEST).is_file():
        raise UpconvertError(f"no {MANIFEST} in {src}")
    files, executable = read_tree(src)
    manifest = _parse_manifest(files[MANIFEST])
    diags = Diagnostics(MANIFEST)

    skill_name = name or slug(manifest.name)
    out: dict[str, bytes] = {}
    out_exec: set[str] = set()

    # -- copied trees ------------------------------------------------------------
    schemas = SchemaFiles(files, diags)
    missing: list[str] = []
    reviews: dict[str, dict[str, set[str]]] = {}
    task_docs, credential_groups = _convert_tasks(manifest, files, diags, schemas, missing, reviews)
    note_missing_descriptions(diags, missing)
    _flush_script_reviews(diags, reviews)
    for path, data in sorted(files.items()):
        if path in (MANIFEST, "README.md") or path in schemas.moved:
            continue
        head = path.split("/", 1)[0]
        if head == "tasks":
            dest = "scripts/" + path[len("tasks/") :]
        elif head in ("lib", "schemas", "tests") and "/" in path:
            dest = path
        else:
            diags.add(
                W_UPCONVERT_LOSSY,
                None,
                f"{path} is not part of a V13 skill and is not copied",
                file=path,
            )
            continue
        out[dest] = data
        if path in executable:
            out_exec.add(dest)
    for source, dest in schemas.moved.items():
        out[dest] = files[source]
        if source in executable:
            out_exec.add(dest)

    # -- setup -------------------------------------------------------------------
    if manifest.setup is not None:
        diags.add(
            W_UPCONVERT_SETUP_DROPPED,
            "setup",
            f"setup ({manifest.setup.file}) is dropped: V13 has no setup phase (D40); "
            "the file is copied under lib/ but nothing runs it",
        )

    # -- tasks.yaml --------------------------------------------------------------
    note_generated_titles(diags, len(task_docs))
    doc: dict[str, Any] = {
        "apiVersion": TASKS_API_VERSION,
        "runtime": credential_runtime(credential_groups),
    }
    shared = credential_groups[0] if credential_groups else []
    if shared and all(group == shared for group in credential_groups):
        doc["defaults"] = {"credentials": credentials_value(shared)}
    else:
        for task_doc, group in zip(task_docs, credential_groups, strict=True):
            value = credentials_value(group)
            if value is not None:
                _insert_credentials(task_doc, value)
    doc["tasks"] = task_docs

    # -- SKILL.md ----------------------------------------------------------------
    description = description_or(
        manifest.description, title_case(manifest.name), diags, "description", "the bundle"
    )
    frontmatter: dict[str, Any] = {"name": skill_name, "description": description}
    applies = applies_to_block([e.model_dump() for e in manifest.appliesTo], diags)
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
    return UpconvertResult(skill_name, result_files, frozenset(out_exec), diags.items)


def _insert_credentials(task_doc: dict, value: Any) -> None:
    """`credentials` after `destructive`/`effects`, before inputs, so the file reads in order."""
    items = list(task_doc.items())
    task_doc.clear()
    placed = False
    for key, item in items:
        if key in ("inputs", "outputs") and not placed:
            task_doc["credentials"] = value
            placed = True
        task_doc[key] = item
    if not placed:
        task_doc["credentials"] = value


def _parse_manifest(data: bytes) -> Manifest:
    try:
        raw = safe_load(data.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise UpconvertError(f"{MANIFEST} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("apiVersion") != API_VERSION:
        raise UpconvertError(f"{MANIFEST}: apiVersion must be {API_VERSION}")
    try:
        return Manifest.model_validate(raw)
    except ValidationError as exc:
        raise UpconvertError(
            f"{MANIFEST} does not match the custom-capability schema: {exc}"
        ) from exc


# -- tasks ----------------------------------------------------------------------


def _convert_tasks(
    manifest: Manifest,
    files: dict[str, bytes],
    diags: Diagnostics,
    schemas: SchemaFiles,
    missing: list[str],
    reviews: dict[str, dict[str, set[str]]],
) -> tuple[list[dict], list[list[str | dict]]]:
    task_docs: list[dict] = []
    groups: list[list[str | dict]] = []
    for index, task in enumerate(manifest.tasks):
        if task.file not in files:
            raise UpconvertError(f"task {task.name!r}: {task.file} is not in the bundle")
        if not task.file.startswith("tasks/"):
            raise UpconvertError(f"task {task.name!r}: {task.file} is not under tasks/")
        doc, group = _convert_task(manifest, task, index, files, diags, schemas, missing, reviews)
        task_docs.append(doc)
        groups.append(group)
    return task_docs, groups


def _convert_task(
    manifest: Manifest,
    task: TaskSpec,
    index: int,
    files: dict[str, bytes],
    diags: Diagnostics,
    schemas: SchemaFiles,
    missing: list[str],
    reviews: dict[str, dict[str, set[str]]],
) -> tuple[dict, list[str | dict]]:
    here = f"tasks[{index}]"
    name = slug(task.name)
    if name != task.name:
        diags.add(W_UPCONVERT_REVIEW, f"{here}.name", f"task {task.name!r} is renamed {name!r}")
    title = title_case(task.name)
    script = "scripts/" + task.file[len("tasks/") :]
    doc: dict[str, Any] = {
        "name": name,
        "title": title,
        "description": description_or(
            task.description,
            title,
            diags,
            f"{here}.description",
            name,
            missing=missing,
        ),
        "script": script,
        "readOnly": task.readOnly,
    }
    if not task.readOnly:
        effects = [e for e in task.effects if e.strip()] or [_DESCRIPTION_FALLBACK_EFFECT]
        doc["effects"] = effects
        doc["destructive"] = False
        diags.add(
            W_UPCONVERT_REVIEW,
            f"{here}.readOnly",
            f"write task {name!r} gets destructive: false; decide whether it is destructive",
        )

    inputs: dict[str, Any] = {}
    credentials: list[str | dict] = []
    dropped: dict[str, str] = {}
    kept: dict[str, str] = {}
    taken: set[str] = set()
    for in_name, spec in {**manifest.inputs, **task.inputs}.items():
        where = f"{here}.inputs.{in_name}" if in_name in task.inputs else f"inputs.{in_name}"
        if spec.type == "resource":
            dropped[in_name] = "resource"
            diags.add(
                W_UPCONVERT_LOSSY,
                where,
                f"resource input {in_name!r} is dropped: the target resource's identity is "
                "automatic in V13 (ctx.resource / RW_RESOURCE_*)",
            )
        elif spec.type in ("credential", "secret"):
            dropped[in_name] = spec.type
            credentials.append(_convert_credential(in_name, spec, where, diags))
        else:
            converted = _convert_input(in_name, spec, where, taken, diags)
            if converted is not None:
                new_name, value = converted
                inputs[new_name] = value
                kept[in_name] = new_name
    if inputs:
        doc["inputs"] = inputs

    out_taken: set[str] = set()
    outputs: dict[str, Any] = {}
    for out_name, spec in task.outputs.items():
        where = f"{here}.outputs.{out_name}"
        new = v13_name(out_name, out_taken, diags, where, "output", reserved=RESERVED_OUTPUT_NAMES)
        if spec.artifact is not None:
            artifact: dict[str, Any] = {"contentType": spec.artifact.contentType}
            if spec.artifact.maxSize is not None:
                artifact["maxSize"] = spec.artifact.maxSize
            outputs[new] = {"artifact": artifact}
        else:
            outputs[new] = {"schema": schemas.resolve(spec.schema_ or "", f"{where}.schema")}
    if outputs:
        doc["outputs"] = outputs

    _scan_script(reviews, script, files[task.file].decode("utf-8", "replace"), dropped, kept)
    return doc, merge_credentials(credentials)


def _convert_credential(
    in_name: str, spec: InputSpec, where: str, diags: Diagnostics
) -> str | dict:
    kind = spec.kind if spec.type == "credential" else None
    entry = credential_entry(kind, in_name)
    notes = []
    if spec.type == "secret" or entry != KUBECONFIG_TYPE:
        notes.append(
            "custom tasks were handed a file path; generic/env sets the variable to the value"
        )
    if spec.optional:
        notes.append("V13 has no optional credential: the tasks now need it")
    label = f"{spec.type} input {in_name!r}" + (f" (kind {spec.kind})" if spec.kind else "")
    diags.add(
        W_UPCONVERT_CREDENTIAL,
        where,
        f"{label} becomes the credential {entry if isinstance(entry, str) else 'generic/env'}"
        + (f" with vars {entry['vars']}" if isinstance(entry, dict) else "")
        + "; a connection supplies it"
        + ("; " + "; ".join(notes) if notes else ""),
    )
    return entry


def _convert_input(
    in_name: str, spec: InputSpec, where: str, taken: set[str], diags: Diagnostics
) -> tuple[str, dict] | None:
    fields, problem = input_type(spec.type)
    if fields is None:
        diags.add(W_UPCONVERT_LOSSY, where, f"input {in_name!r} is dropped: {problem}")
        return None
    value: dict[str, Any] = dict(fields)
    if spec.default is not None:
        value["default"] = spec.default
    elif spec.optional:
        value["required"] = False
    return v13_name(in_name, taken, diags, where, "input"), value


# -- scripts ----------------------------------------------------------------------


def _scan_script(
    reviews: dict[str, dict[str, set[str]]],
    script: str,
    text: str,
    dropped: dict[str, str],
    kept: dict[str, str],
) -> None:
    """Record what a script reads the way a custom task did, into `reviews[script]`: a Python
    `main` that takes a dropped input as an argument (`args`), a bash script reading a dropped
    resource input (`resource`) or any kept input as a bare `$NAME` (`bare`; V13 delivers
    `RW_INPUT_<NAME>`). Looked up by pattern; nothing is rewritten. `_flush_script_reviews`
    turns each script's record into one finding."""
    found = reviews.setdefault(script, {"args": set(), "resource": set(), "bare": set()})
    if script.endswith(".py"):
        match = re.search(r"def\s+main\s*\(([^)]*)\)", text, re.S)
        signature = match.group(1) if match else ""
        found["args"] |= {
            n for n in dropped if re.search(rf"\b{python_kwarg_name(n)}\b", signature)
        }
        return
    found["resource"] |= {
        n
        for n, kind in dropped.items()
        if kind == "resource" and _reads_env(text, n, allow_prefixed=True)
    }
    found["bare"] |= {
        input_env_name(new) for n, new in kept.items() if _reads_env(text, n, allow_prefixed=False)
    }


def _flush_script_reviews(diags: Diagnostics, reviews: dict[str, dict[str, set[str]]]) -> None:
    """One W_UPCONVERT_REVIEW per script file, listing every variable it reads."""
    for script, found in sorted(reviews.items()):
        parts = []
        if found["args"]:
            parts.append(
                f"main() takes {', '.join(sorted(found['args']))}, which V13 no longer passes "
                "(the target is ctx.resource; credentials come from the connection's "
                "standard environment)"
            )
        if found["resource"]:
            parts.append(
                f"reads the dropped resource input {', '.join(sorted(found['resource']))}; "
                "use RW_RESOURCE_* instead"
            )
        if found["bare"]:
            names = sorted(found["bare"])
            parts.append(
                f"reads {', '.join('$' + n for n in names)} as bare variables; "
                f"use {', '.join('RW_INPUT_' + n for n in names)}"
            )
        if parts:
            diags.add(W_UPCONVERT_REVIEW, None, "; ".join(parts), file=script)


def _reads_env(text: str, input_name: str, *, allow_prefixed: bool) -> bool:
    for env in {input_env_name(input_name), legacy_input_env_name(input_name)}:
        pattern = rf"\$\{{?{env}\b"
        if allow_prefixed:
            pattern += rf"|RW_INPUT_{env}\b"
        if re.search(pattern, text):
            return True
    return False
