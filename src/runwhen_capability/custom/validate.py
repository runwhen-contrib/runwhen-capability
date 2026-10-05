"""validate(files) -- every static check a custom capability bundle must pass,
with no execution: manifest shape, path/size limits, task file existence,
output schema notation, undeclared inputs/outputs, and readOnly writes.

Always returns a list of Diagnostic; never raises on malformed input -- a
bundle an agent is mid-way through authoring is exactly the input this
function exists to look at, so a YAML syntax error or a missing file is a
Diagnostic, not an exception.
"""

from __future__ import annotations

import json
import unicodedata

import jsonschema
import yaml
from pydantic import ValidationError

from . import static_checks
from .diagnostics import (
    E_DUPLICATE_NAME,
    E_EFFECTS_REQUIRED,
    E_LIMIT,
    E_MANIFEST_SCHEMA,
    E_OUTPUT_UNDECLARED,
    E_PATH_NOT_ALLOWED,
    E_READONLY_WRITE,
    E_SCHEMA_FEATURE,
    E_SCHEMA_NOTATION,
    E_TASK_FILE_MISSING,
    E_UNDECLARED_INPUT,
    E_UNKNOWN_SDK_HELPER,
    W_DATA_AS_CODE,
    W_UNKNOWN_COMMAND,
    W_UNUSED_INPUT,
    Diagnostic,
)
from .manifest import (
    API_VERSION,
    INPUT_NAME_RE,
    MAX_BUNDLE_BYTES,
    MAX_FILE_BYTES,
    MAX_FILES,
    SLUG_RE,
    InputSpec,
    Manifest,
    OutputSpec,
    input_env_name,
    is_allowed_path,
    is_reserved_env_name,
    legacy_input_env_name,
    python_kwarg_name,
    task_file_language,
)
from .runtime_commands import SUBSTITUTES
from .schema_features import REGEX_HINT, REGEX_KEYWORDS, regex_keyword_paths
from .schema_notation import SchemaNotationError, compile_schema_notation
from .yaml_lines import line_of, load_with_lines

# Env vars that are never "an undeclared input" in a bash task -- ordinary
# shell/environment plumbing, or set by the bundle host itself (bundle.py)
# rather than derived from a declared input.
_STANDARD_BASH_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "PWD",
        "OLDPWD",
        "IFS",
        "SHELL",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "TERM",
        "USER",
        "HOSTNAME",
        "RW_SDK",
        "RW_OUTPUT_FD",
        "RW_WORKDIR",
        "RW_CAPABILITY",
        "RW_OPERATION",
    }
)


# An escape-hatch JSON Schema file nested deeper than this is refused -- the
# same bound the compact notation has (schema_notation.MAX_DEPTH), with room
# for JSON Schema's own wrapper objects (properties, items, ...).
MAX_SCHEMA_DEPTH = 64

# Keywords whose value is a URI reference the validator would resolve.
_REF_KEYWORDS = frozenset({"$ref", "$dynamicRef", "$recursiveRef"})


def validate(files: dict[str, str]) -> list[Diagnostic]:
    try:
        return _validate(files)
    except RecursionError:
        # Every known deep-nesting path is bounded above; this is the net
        # under them, so validate() still returns rather than raises.
        return [
            Diagnostic(
                code=E_MANIFEST_SCHEMA,
                message="the bundle is nested too deeply to analyse",
            )
        ]


def _validate(files: dict[str, str]) -> list[Diagnostic]:
    diagnostics = _check_paths_and_limits(files)

    if "capability.yaml" not in files:
        diagnostics.append(
            Diagnostic(
                code=E_MANIFEST_SCHEMA,
                file="capability.yaml",
                message="capability.yaml is missing",
            )
        )
        return diagnostics

    try:
        raw = load_with_lines(files["capability.yaml"])
    except RecursionError:
        diagnostics.append(
            Diagnostic(
                code=E_MANIFEST_SCHEMA,
                file="capability.yaml",
                message="capability.yaml is nested too deeply to load",
            )
        )
        return diagnostics
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        diagnostics.append(
            Diagnostic(
                code=E_MANIFEST_SCHEMA,
                file="capability.yaml",
                line=(mark.line + 1) if mark is not None else None,
                message=f"capability.yaml is not valid YAML: {exc}",
            )
        )
        return diagnostics

    if not isinstance(raw, dict):
        diagnostics.append(
            Diagnostic(
                code=E_MANIFEST_SCHEMA,
                file="capability.yaml",
                message="capability.yaml must be a mapping",
            )
        )
        return diagnostics

    manifest, shape_diagnostics = _validate_manifest_shape(raw)
    diagnostics += shape_diagnostics
    if manifest is None:
        return diagnostics

    diagnostics += _check_names(raw, manifest)
    diagnostics += _check_input_schema_features(raw)
    diagnostics += _check_duplicate_task_names(raw, manifest)
    diagnostics += _check_effects(raw, manifest)
    diagnostics += _check_setup_file(files, raw, manifest)
    diagnostics += _check_tasks(files, raw, manifest)
    diagnostics += _check_read_only_shared_code(files, manifest)
    diagnostics += _check_unknown_commands(files, manifest)
    return diagnostics


# -- manifest shape -----------------------------------------------------------


def _format_loc(loc: tuple) -> str:
    parts: list[str] = []
    for key in loc:
        if isinstance(key, int) and parts:
            parts[-1] = f"{parts[-1]}[{key}]"
        else:
            parts.append(str(key))
    return ".".join(parts)


def _line_for_loc(raw, loc: tuple) -> int | None:
    node = raw
    best = line_of(raw)
    for key in loc:
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError):
            break
        line = line_of(node)
        if line is not None:
            best = line
    return best


def _validate_manifest_shape(raw) -> tuple[Manifest | None, list[Diagnostic]]:
    try:
        manifest = Manifest.model_validate(raw)
    except ValidationError as exc:
        diagnostics = [
            Diagnostic(
                code=E_MANIFEST_SCHEMA,
                file="capability.yaml",
                line=_line_for_loc(raw, error["loc"]),
                path=_format_loc(error["loc"]) or None,
                message=error["msg"],
            )
            for error in exc.errors()
        ]
        return None, diagnostics

    diagnostics = []
    if manifest.apiVersion != API_VERSION:
        diagnostics.append(
            Diagnostic(
                code=E_MANIFEST_SCHEMA,
                file="capability.yaml",
                line=line_of(raw),
                path="apiVersion",
                message=f"apiVersion must be {API_VERSION!r}, got {manifest.apiVersion!r}",
            )
        )
    return manifest, diagnostics


# -- paths and limits ----------------------------------------------------------


def _check_paths_and_limits(files: dict[str, str]) -> list[Diagnostic]:
    diagnostics = []
    if len(files) > MAX_FILES:
        diagnostics.append(
            Diagnostic(
                code=E_LIMIT,
                message=f"bundle has {len(files)} files, over the {MAX_FILES}-file limit",
            )
        )

    total_bytes = 0
    for path, content in files.items():
        size = len(content.encode("utf-8"))
        total_bytes += size
        if not is_allowed_path(path):
            diagnostics.append(
                Diagnostic(
                    code=E_PATH_NOT_ALLOWED,
                    file=path,
                    message=(
                        f"{path!r} is outside the allowed paths (capability.yaml, tasks/**, "
                        "lib/**, schemas/**, tests/**, README.md)"
                    ),
                )
            )
            continue
        if size > MAX_FILE_BYTES:
            diagnostics.append(
                Diagnostic(
                    code=E_LIMIT,
                    file=path,
                    message=(
                        f"{path!r} is {size} bytes, over the {MAX_FILE_BYTES}-byte per-file limit"
                    ),
                )
            )

    if total_bytes > MAX_BUNDLE_BYTES:
        diagnostics.append(
            Diagnostic(
                code=E_LIMIT,
                message=(
                    f"bundle is {total_bytes} bytes, over the {MAX_BUNDLE_BYTES}-byte bundle limit"
                ),
            )
        )
    diagnostics += _check_path_collisions(files)
    return diagnostics


def _check_path_collisions(files: dict[str, str]) -> list[Diagnostic]:
    """Two paths that would land on the same file, or a path that would
    have to be both a file and a directory, once written to disk. On a
    case-insensitive or normalization-insensitive filesystem `tasks/a.sh`
    and `tasks/A.sh` are one file -- whichever is written last would run in
    place of the one validate() actually checked."""
    diagnostics = []
    seen: dict[str, str] = {}
    for path in sorted(files):
        key = unicodedata.normalize("NFC", path).casefold()
        if key in seen:
            diagnostics.append(
                Diagnostic(
                    code=E_DUPLICATE_NAME,
                    file=path,
                    message=f"{path!r} and {seen[key]!r} differ only in case or normalization",
                )
            )
        else:
            seen[key] = path
    for path in files:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            prefix = "/".join(parts[:depth])
            if prefix in files:
                diagnostics.append(
                    Diagnostic(
                        code=E_PATH_NOT_ALLOWED,
                        file=path,
                        message=f"{path!r} needs {prefix!r} to be a directory, but it is a file",
                    )
                )
                break
    return diagnostics


# -- names ----------------------------------------------------------------------


def _check_names(raw, manifest: Manifest) -> list[Diagnostic]:
    """The capability name and every task name are lowercase slugs; every
    input name is an identifier that maps onto a non-reserved env var, and
    no two inputs a task sees map onto the same env var or Python keyword
    argument (one would silently overwrite the other)."""
    diagnostics = []
    if not SLUG_RE.match(manifest.name):
        diagnostics.append(
            Diagnostic(
                code=E_MANIFEST_SCHEMA,
                file="capability.yaml",
                line=line_of(raw),
                path="name",
                message=f"name {manifest.name!r} must be a lowercase slug (a-z, 0-9, '-')",
            )
        )

    raw_tasks = raw.get("tasks") or []
    diagnostics += _check_input_names(manifest.inputs, "inputs", line_of(raw.get("inputs")))
    for index, task in enumerate(manifest.tasks):
        raw_task = raw_tasks[index] if index < len(raw_tasks) else {}
        task_line = line_of(raw_task)
        if not SLUG_RE.match(task.name):
            diagnostics.append(
                Diagnostic(
                    code=E_MANIFEST_SCHEMA,
                    file="capability.yaml",
                    line=task_line,
                    path=f"tasks[{index}].name",
                    message=f"task name {task.name!r} must be a lowercase slug (a-z, 0-9, '-')",
                )
            )
        raw_inputs = raw_task.get("inputs") if isinstance(raw_task, dict) else None
        inputs_line = line_of(raw_inputs) or task_line
        diagnostics += _check_input_names(task.inputs, f"tasks[{index}].inputs", inputs_line)
        diagnostics += _check_input_collisions(
            {**manifest.inputs, **task.inputs}, f"tasks[{index}].inputs", inputs_line
        )
    return diagnostics


def _check_input_names(
    inputs: dict[str, InputSpec], loc: str, line: int | None
) -> list[Diagnostic]:
    diagnostics = []
    for name in inputs:
        if not INPUT_NAME_RE.match(name):
            message = (
                f"input name {name!r} must be an identifier (a letter, then letters, digits, _)"
            )
        elif is_reserved_env_name(input_env_name(name)):
            message = f"input name {name!r} would set the reserved env var {input_env_name(name)!r}"
            if not is_reserved_env_name(legacy_input_env_name(name)):
                # An upper-case name published before H48 mapped to a harmless
                # per-letter name, so refusing it now would break a bundle that ran
                # fine. Warn; the host delivers it under the legacy name only.
                diagnostics.append(
                    Diagnostic(
                        code=E_MANIFEST_SCHEMA,
                        severity="warning",
                        file="capability.yaml",
                        line=line,
                        path=f"{loc}.{name}",
                        message=f"{message}, so it arrives as {legacy_input_env_name(name)!r} only",
                        hint="rename the input",
                    )
                )
                continue
        else:
            continue
        diagnostics.append(
            Diagnostic(
                code=E_MANIFEST_SCHEMA,
                file="capability.yaml",
                line=line,
                path=f"{loc}.{name}",
                message=message,
            )
        )
    return diagnostics


def _check_input_collisions(
    inputs: dict[str, InputSpec], loc: str, line: int | None
) -> list[Diagnostic]:
    diagnostics = []
    for mapping in (input_env_name, python_kwarg_name):
        seen: dict[str, str] = {}
        for name in inputs:
            mapped = mapping(name)
            if mapped in seen:
                diagnostics.append(
                    Diagnostic(
                        code=E_DUPLICATE_NAME,
                        file="capability.yaml",
                        line=line,
                        path=f"{loc}.{name}",
                        message=f"inputs {seen[mapped]!r} and {name!r} both arrive as {mapped!r}",
                    )
                )
            else:
                seen[mapped] = name
    return diagnostics


# -- duplicate task names -----------------------------------------------------


def _check_duplicate_task_names(raw, manifest: Manifest) -> list[Diagnostic]:
    diagnostics = []
    raw_tasks = raw.get("tasks") or []
    seen: dict[str, int] = {}
    for index, task in enumerate(manifest.tasks):
        if task.name in seen:
            raw_task = raw_tasks[index] if index < len(raw_tasks) else None
            diagnostics.append(
                Diagnostic(
                    code=E_DUPLICATE_NAME,
                    file="capability.yaml",
                    line=line_of(raw_task),
                    path=f"tasks[{index}].name",
                    message=f"task name {task.name!r} is already used by tasks[{seen[task.name]}]",
                )
            )
        else:
            seen[task.name] = index
    return diagnostics


def _check_effects(raw, manifest: Manifest) -> list[Diagnostic]:
    """E_EFFECTS_REQUIRED for a task that changes things but doesn't say what."""
    diagnostics = []
    raw_tasks = raw.get("tasks") or []
    for index, task in enumerate(manifest.tasks):
        if task.readOnly or any(effect.strip() for effect in task.effects):
            continue
        raw_task = raw_tasks[index] if index < len(raw_tasks) else None
        diagnostics.append(
            Diagnostic(
                code=E_EFFECTS_REQUIRED,
                file="capability.yaml",
                line=line_of(raw_task),
                path=f"tasks[{index}].effects",
                message=f"task {task.name!r} is not readOnly, so it must declare its effects",
                hint="if the task changes nothing, set readOnly: true; otherwise add "
                "effects: one plain sentence per change, e.g. "
                "'Compacts volumes whose garbage ratio exceeds the threshold'",
            )
        )
    return diagnostics


# -- task/setup files: existence, language, static reads/writes --------------


def _check_setup_file(files: dict[str, str], raw, manifest: Manifest) -> list[Diagnostic]:
    if manifest.setup is None:
        return []
    path = manifest.setup.file
    line = line_of(raw.get("setup"))

    if path not in files:
        return [
            Diagnostic(
                code=E_TASK_FILE_MISSING,
                file="capability.yaml",
                line=line,
                path="setup.file",
                message=f"setup file {path!r} is not in the bundle",
            )
        ]
    language = task_file_language(path)
    if language is None:
        return [
            Diagnostic(
                code=E_PATH_NOT_ALLOWED,
                file="capability.yaml",
                line=line,
                path="setup.file",
                message=f"setup file {path!r} must end in .py or .sh",
            )
        ]
    # Setup declares no outputs of its own in this manifest version, so
    # output-undeclared checking does not apply (outputs=None skips it).
    return _check_source(
        files[path],
        path,
        language,
        manifest.inputs,
        outputs=None,
        read_only=False,
        shared_functions=_shared_rw_functions(files, manifest),
    )


def _shared_rw_functions(files: dict[str, str], manifest: Manifest) -> frozenset[str]:
    """rw_* functions defined by the bash code every task may source: lib/*.sh
    and a bash setup file."""
    paths = {p for p in files if p.startswith("lib/") and p.endswith(".sh")}
    if manifest.setup is not None and manifest.setup.file.endswith(".sh"):
        paths.add(manifest.setup.file)
    names: set[str] = set()
    for path in paths:
        if path in files:
            names |= static_checks.bash_defined_rw_functions(files[path])
    return frozenset(names)


def _check_tasks(files: dict[str, str], raw, manifest: Manifest) -> list[Diagnostic]:
    diagnostics = []
    raw_tasks = raw.get("tasks") or []
    # Code every task may run besides its own file: the setup file and lib/.
    shared_paths = {path for path in files if path.startswith("lib/")}
    if manifest.setup is not None:
        shared_paths.add(manifest.setup.file)
    shared_text = "\n".join(files[path] for path in sorted(shared_paths) if path in files)
    shared_functions = _shared_rw_functions(files, manifest)
    # Capability-level secrets/credentials no task reads; a task that cannot be
    # analysed empties it (unknown counts as used).
    cap_unused_by_all = {
        name for name, spec in manifest.inputs.items() if spec.type in ("secret", "credential")
    }
    for index, task in enumerate(manifest.tasks):
        raw_task = raw_tasks[index] if index < len(raw_tasks) else {}
        task_line = line_of(raw_task)
        path = task.file

        if path not in files:
            cap_unused_by_all.clear()
            diagnostics.append(
                Diagnostic(
                    code=E_TASK_FILE_MISSING,
                    file="capability.yaml",
                    line=task_line,
                    path=f"tasks[{index}].file",
                    message=f"task {task.name!r}'s file {path!r} is not in the bundle",
                )
            )
            continue

        language = task_file_language(path)
        if language is None:
            cap_unused_by_all.clear()
            diagnostics.append(
                Diagnostic(
                    code=E_PATH_NOT_ALLOWED,
                    file="capability.yaml",
                    line=task_line,
                    path=f"tasks[{index}].file",
                    message=f"task {task.name!r}'s file {path!r} must end in .py or .sh",
                )
            )
            continue

        merged_inputs = {**manifest.inputs, **task.inputs}
        diagnostics += _check_source(
            files[path],
            path,
            language,
            merged_inputs,
            outputs=task.outputs,
            read_only=task.readOnly,
            shared_functions=shared_functions,
        )

        task_unused, cap_unused = _check_unused_inputs(
            files, raw_task, manifest, task, index, language, shared_text
        )
        diagnostics += task_unused
        cap_unused_by_all &= cap_unused

        raw_outputs = (raw_task or {}).get("outputs") or {}
        for name, output in task.outputs.items():
            diagnostics += _check_output_schema(
                files,
                name,
                output.schema_,
                loc_path=f"tasks[{index}].outputs.{name}.schema",
                line=line_of(raw_outputs.get(name)),
            )
    raw_cap_inputs = (raw or {}).get("inputs") or {}
    for name in manifest.inputs:
        if name in cap_unused_by_all and manifest.tasks:
            diagnostics.append(
                _unused_input_diagnostic(name, f"inputs.{name}", line_of(raw_cap_inputs.get(name)))
            )
    return diagnostics


def _check_unused_inputs(
    files: dict[str, str],
    raw_task,
    manifest: Manifest,
    task,
    index: int,
    language: str,
    shared_text: str,
) -> tuple[list[Diagnostic], set[str]]:
    """W_UNUSED_INPUT for the task's own secret/credential inputs, plus the
    names of capability-level ones this task never reads (the caller warns
    once, when no task reads them). Skipped, never guessed, when
    the task's Python has no parseable `main`. An input the setup file or
    lib/ mentions is treated as used -- they run on the task's behalf.
    A `k8s.kubeconfig` credential counts as used when the task calls
    kubectl or names KUBECONFIG, since KUBECONFIG is set automatically."""
    source = files[task.file]
    calls_kubectl = static_checks.runs_kubectl(source)
    raw_task_inputs = (raw_task or {}).get("inputs") or {}
    diagnostics = []
    cap_unused: set[str] = set()
    for name, spec in {**manifest.inputs, **task.inputs}.items():
        if spec.type not in ("secret", "credential"):
            continue
        if spec.type == "credential" and spec.kind == "k8s.kubeconfig" and calls_kubectl:
            continue
        if language == "python":
            used = static_checks.python_input_used(source, name)
            if used is None:
                continue
        else:
            used = static_checks.bash_input_used(source, name)
        if spec.type == "credential" and spec.kind == "k8s.kubeconfig":
            used = used or static_checks.bash_input_used(source, "kubeconfig")
        if used or (
            shared_text
            and (
                static_checks.bash_input_used(shared_text, name)
                or f'"{name}"' in shared_text
                or f"'{name}'" in shared_text
            )
        ):
            continue
        if name not in task.inputs:
            cap_unused.add(name)
            continue
        diagnostics.append(
            _unused_input_diagnostic(
                name, f"tasks[{index}].inputs.{name}", line_of(raw_task_inputs.get(name))
            )
        )
    return diagnostics, cap_unused


def _unused_input_diagnostic(name: str, loc_path: str, line: int | None) -> Diagnostic:
    return Diagnostic(
        code=W_UNUSED_INPUT,
        severity="warning",
        file="capability.yaml",
        line=line,
        path=loc_path,
        message=(
            f"{name!r} is declared but never read; remove it. An unread secret or "
            "credential is still shown to the approving admin and can stop the "
            "capability running where it isn't available"
        ),
    )


def _check_read_only_shared_code(files: dict[str, str], manifest: Manifest) -> list[Diagnostic]:
    """E_READONLY_WRITE for code a readOnly task runs besides its own file:
    the setup file, which runs before every task in a request, and every
    file under lib/, which any task may source or import -- whatever its
    language. A mutating kubectl call there runs on a readOnly task's
    behalf just as surely as one in the task's own file (checked in
    _check_source). Each offending line is reported once, at its own file
    and line, naming the readOnly tasks it affects."""
    read_only = [task for task in manifest.tasks if task.readOnly]
    if not read_only:
        return []
    shared = {path for path in files if path.startswith("lib/")}
    if manifest.setup is not None and manifest.setup.file in files:
        shared.add(manifest.setup.file)
    # A readOnly task's own file is already scanned, and reported, as such.
    shared -= {task.file for task in read_only}
    names = ", ".join(repr(task.name) for task in read_only)
    diagnostics = []
    for path in sorted(shared):
        for line, call in static_checks.mutating_kubectl_calls(files[path]):
            diagnostics.append(
                Diagnostic(
                    code=E_READONLY_WRITE,
                    file=path,
                    line=line,
                    message=(
                        f"{call!r} mutates cluster state, but {path} runs as part of "
                        f"readOnly task(s) {names}"
                    ),
                )
            )
    return diagnostics


def _check_unknown_commands(files: dict[str, str], manifest: Manifest) -> list[Diagnostic]:
    """W_UNKNOWN_COMMAND for every bash file a task runs -- each task's own
    file, a bash setup file and lib/*.sh -- once per file however many tasks
    share it. A function any .sh file in the bundle defines counts as known
    everywhere: lib/ and setup define them for tasks, and which file sources
    which is not tracked."""
    paths = {task.file for task in manifest.tasks if task.file.endswith(".sh")}
    paths |= {p for p in files if p.startswith("lib/") and p.endswith(".sh")}
    if manifest.setup is not None and manifest.setup.file.endswith(".sh"):
        paths.add(manifest.setup.file)
    defined: set[str] = set()
    for path in files:
        if path.endswith(".sh"):
            defined |= static_checks.bash_defined_functions(files[path])
    diagnostics = []
    for path in sorted(p for p in paths if p in files):
        for line, name in static_checks.bash_unknown_commands(files[path], frozenset(defined)):
            substitute = SUBSTITUTES.get(name)
            advice = (
                f"use {substitute} or a tool listed in the authoring README"
                if substitute
                else "use a tool that ships there (see the authoring README)"
            )
            diagnostics.append(
                Diagnostic(
                    code=W_UNKNOWN_COMMAND,
                    severity="warning",
                    file=path,
                    line=line,
                    message=f"{name!r} is not on the rw-task image; {advice}",
                    hint=(
                        "the image has bash, coreutils, grep, sed, awk, find, xargs, tar, gzip, "
                        "curl, jq, yq, kubectl, psql, redis-cli, openssl and python3; "
                        "a command it lacks fails with exit 127 and the script carries on"
                    ),
                )
            )
    return diagnostics


def _check_output_schema(
    files: dict[str, str], name: str, schema_text: str, loc_path: str, line: int | None
) -> list[Diagnostic]:
    if schema_text.startswith("./"):
        ref = schema_text[2:]
        if ref not in files:
            return [
                Diagnostic(
                    code=E_SCHEMA_NOTATION,
                    file="capability.yaml",
                    line=line,
                    path=loc_path,
                    message=(
                        f"output {name!r} references schema file {schema_text!r}, not in the bundle"
                    ),
                )
            ]
        problem = _json_schema_file_problem(files[ref])
        if problem is not None:
            return [
                Diagnostic(
                    code=E_SCHEMA_NOTATION,
                    file=ref,
                    path=loc_path,
                    message=f"{ref}: {problem}",
                )
            ]
        return _regex_keyword_diagnostics(json.loads(files[ref]), ref, loc_path, None)

    try:
        compiled = compile_schema_notation(schema_text)
    except SchemaNotationError as exc:
        return [
            Diagnostic(
                code=E_SCHEMA_NOTATION,
                file="capability.yaml",
                line=line,
                path=loc_path,
                message=str(exc),
            )
        ]
    # The compact notation has no way to write a regex today; checked anyway
    # so the rule holds for every compiled schema, whatever the notation grows.
    return _regex_keyword_diagnostics(compiled, "capability.yaml", loc_path, line)


def _regex_keyword_diagnostics(
    schema: dict, file: str, loc_path: str, line: int | None
) -> list[Diagnostic]:
    return [
        Diagnostic(
            code=E_SCHEMA_FEATURE,
            file=file,
            line=line,
            path=loc_path,
            message=f"{file}: {pointer.rsplit('/', 1)[-1]!r} at {pointer} is not supported",
            hint=REGEX_HINT,
        )
        for pointer in regex_keyword_paths(schema)
    ]


def _check_input_schema_features(raw) -> list[Diagnostic]:
    """An input is declared by `type` alone -- there is no input JSON Schema
    -- so a `pattern`/`patternProperties` key on an input would be silently
    ignored. Reported instead, so an author never believes a format is
    being enforced when it is not."""
    raw_tasks = raw.get("tasks") if isinstance(raw.get("tasks"), list) else []
    scopes = [("inputs", raw.get("inputs"))] + [
        (f"tasks[{index}].inputs", task.get("inputs") if isinstance(task, dict) else None)
        for index, task in enumerate(raw_tasks)
    ]
    diagnostics = []
    for loc, inputs in scopes:
        if not isinstance(inputs, dict):
            continue
        for name, spec in inputs.items():
            if not isinstance(spec, dict):
                continue
            for keyword in REGEX_KEYWORDS:
                if keyword in spec:
                    diagnostics.append(
                        Diagnostic(
                            code=E_SCHEMA_FEATURE,
                            file="capability.yaml",
                            line=line_of(spec),
                            path=f"{loc}.{name}.{keyword}",
                            message=f"input {name!r} uses {keyword!r}, which is not supported",
                            hint=REGEX_HINT,
                        )
                    )
    return diagnostics


def _json_schema_file_problem(text: str) -> str | None:
    """Why an escape-hatch schema file is unusable, or None. It must be a
    JSON object, nested no deeper than MAX_SCHEMA_DEPTH, a valid JSON Schema
    (its own metaschema), and every `$ref` in it must be local ("#...") --
    the bundle host evaluates this schema against task output, and a
    non-local `$ref` would make it fetch a URL or read a file to do so."""
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError) as exc:
        return f"not valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return "must be a JSON Schema object"

    stack: list[tuple[object, int]] = [(parsed, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_SCHEMA_DEPTH:
            return f"nested deeper than {MAX_SCHEMA_DEPTH} levels"
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _REF_KEYWORDS and not (isinstance(value, str) and value.startswith("#")):
                    return f"{key} {value!r} is not a local reference (must start with '#')"
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            stack.extend((item, depth + 1) for item in node)

    try:
        jsonschema.validators.validator_for(parsed).check_schema(parsed)
    except jsonschema.exceptions.SchemaError as exc:
        return f"not a valid JSON Schema: {exc.message}"
    except Exception as exc:  # noqa: BLE001 -- untrusted input; report, never raise
        return f"not a valid JSON Schema: {type(exc).__name__}"
    return None


def _check_source(
    source: str,
    file_path: str,
    language: str,
    declared_inputs: dict[str, InputSpec],
    outputs: dict[str, OutputSpec] | None,
    read_only: bool,
    shared_functions: frozenset[str] = frozenset(),
) -> list[Diagnostic]:
    """`shared_functions`: rw_* functions lib/ (and setup) define, which a
    bash task may call without tripping E_UNKNOWN_SDK_HELPER."""
    diagnostics = []

    if read_only:
        for line, call in static_checks.mutating_kubectl_calls(source):
            diagnostics.append(
                Diagnostic(
                    code=E_READONLY_WRITE,
                    file=file_path,
                    line=line,
                    message=f"{call!r} mutates cluster state, but this task is readOnly",
                )
            )

    data_as_code = (
        static_checks.python_data_as_code(source)
        if language == "python"
        else static_checks.bash_data_as_code(source)
    )
    for line, construct in data_as_code:
        diagnostics.append(
            Diagnostic(
                code=W_DATA_AS_CODE,
                severity="warning",
                file=file_path,
                line=line,
                message=(
                    f"{construct} runs text as a command; if that text comes from data "
                    "(a log line, an API body), a quote breaks it and the data can run commands"
                ),
                hint="pass data as data: environment variables, stdin, jq --arg, or Python",
            )
        )

    if language == "python":
        signature = static_checks.python_main_params(source)
        if signature is not None:
            params, has_var_keyword = signature
            if not has_var_keyword:
                for param, line in params:
                    if param not in declared_inputs:
                        diagnostics.append(
                            Diagnostic(
                                code=E_UNDECLARED_INPUT,
                                file=file_path,
                                line=line,
                                message=f"main() parameter {param!r} has no declared input",
                            )
                        )
        if outputs is not None:
            returned = static_checks.python_returned_output_names(source)
            if returned is not None:
                for line, out_name in returned:
                    if out_name not in outputs:
                        diagnostics.append(
                            Diagnostic(
                                code=E_OUTPUT_UNDECLARED,
                                file=file_path,
                                line=line,
                                message=f"returned output {out_name!r} is not declared",
                            )
                        )
        return diagnostics

    # bash
    kubeconfig_auto = any(
        spec.type == "credential" and spec.kind == "k8s.kubeconfig"
        for spec in declared_inputs.values()
    )
    expected_env = (
        {input_env_name(name) for name in declared_inputs}
        # the pre-H48 spelling, which the host still sets: a published bundle may read it
        | {legacy_input_env_name(name) for name in declared_inputs}
        | _STANDARD_BASH_ENV
    )
    if kubeconfig_auto:
        expected_env.add("KUBECONFIG")

    for line, var_name in static_checks.bash_env_reads(source):
        if var_name not in expected_env:
            diagnostics.append(
                Diagnostic(
                    code=E_UNDECLARED_INPUT,
                    file=file_path,
                    line=line,
                    message=f"${{{var_name}}} has no declared input",
                )
            )
    for line, name in static_checks.bash_rw_input_reads(source):
        if name not in declared_inputs:
            diagnostics.append(
                Diagnostic(
                    code=E_UNDECLARED_INPUT,
                    file=file_path,
                    line=line,
                    message=f"rw_input {name!r} has no declared input",
                )
            )
    for line, helper in static_checks.bash_unknown_sdk_helpers(source, shared_functions):
        diagnostics.append(
            Diagnostic(
                code=E_UNKNOWN_SDK_HELPER,
                file=file_path,
                line=line,
                message=(
                    f"{helper!r} is not an SDK helper; rw.sh defines only "
                    + ", ".join(static_checks.RW_SH_HELPERS)
                ),
                hint="severity and other fields go in an output (rw_set/rw_append)",
            )
        )
    if outputs is not None:
        for line, out_name in static_checks.bash_output_writes(source):
            if out_name not in outputs:
                diagnostics.append(
                    Diagnostic(
                        code=E_OUTPUT_UNDECLARED,
                        file=file_path,
                        line=line,
                        message=f"output {out_name!r} is not declared",
                    )
                )
    return diagnostics
