"""Run one compiled V13 task as a child process and derive its result envelope.

    tree = load_dir(path)
    task = next(t for t in compile_tree(tree)["tasks"] if t["ref"].endswith(":health/probe"))
    target = resolve_target("kubernetes/clusters/kind/namespaces/web/deployments/api", tree,
                            identity_overrides={})
    outcome = execute(
        RunSpec(task=task, files=tree.files, executable=tree.executable, target=target,
                inputs={}, settings={}),
        scope_dir=Path("/tmp/scope"),
    )
    outcome.envelope        # validates against `result-envelope` v1
    outcome.artifacts       # {output name: local file}

The child process is bundle.py's: the same process group, deadline, environment
stripping (`_inherited_env`: no host-only or RW_* variable, no XDG_*), private
HOME/TMPDIR, `rw.sh` for bash and `_bundle_entrypoint` for Python. What differs is
the contract the script sees (CONTRACTS.md "What a script sees", `RW_TASK_CONTRACT=v13`)
and the result: not a classic TaskResult but the D64 envelope, from `derive_envelope`.

What the script is given, beyond the host's own environment:

- `RW_RESOURCE_JSON` (identity fields, `path`, `type`), bash `RW_RESOURCE_<FIELD>` for each
  identity field and `RW_RESOURCE_PATH` / `RW_RESOURCE_TYPE`;
- `RW_SETTINGS_JSON` and bash `RW_SETTING_<NAME>` (compiled defaults overlaid by the spec);
- inputs: keyword arguments (Python, snake_case) or `RW_INPUT_<NAME>` (bash);
- `RW_DRY_RUN=1` under dry run, `RW_LIB_DIR` (the skill's lib/), `RW_OUT`, `RW_ERRORS_FILE`;
- whatever the caller passes as `extra_env` (materialized credentials), which may not
  name a reserved variable (RW_*, HOME, PATH, ...) or a host-only one.

Bash tasks run under `set -euo pipefail` with `rw.sh` already loaded (wiki 42b: "run with
set -euo pipefail"; the scripts call `rw_output_append` without sourcing anything).

Output handling. A data output is checked against its compiled JSON Schema (draft
2020-12); one that fails, an undeclared one, and a value written for an artifact output
are dropped and an `internal` error names them. An artifact output is the file the task
wrote at `$RW_OUT/<name>` (or `<name>.<ext>`), registered with `rw_artifact`, or written
to `$RW_ARTIFACTS_DIR/<name>`; the envelope carries `{"artifact": "art_local_<name>"}` and
the file is returned in `RunOutcome.artifacts`. Artifacts are collected only from a task
that exited 0. Outputs are optional: a task that writes none is `ok` with `outputs: {}`.

`resolve_target` turns a resource path into the lease's `target` (path, qualified type,
identity), from the plugin's own integration types where the platform is its own.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import tempfile
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import jsonschema
import referencing

from .. import bundle
from .._redaction import Redactor
from .._rw_sh import BASH_ENV_SH, RW_SH
from ..contracts import validate as validate_contract
from ..custom.manifest import (
    input_env_name,
    is_reserved_env_name,
    task_file_language,
)
from ..envelope import derive_envelope, errors_from_jsonl
from .tree import PluginTree

_log = logging.getLogger("runwhen_capability.plugin.run")

_PLUGIN_DIR = "plugin"
_PLUGIN_MARKER = ".rw-plugin-dir"
_ARTIFACT_OUT_DIR = "artifacts-out"
_MESSAGE_MAX = 4096
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

#: The bash prelude of a V13 task: the classic command-not-found handler, then rw.sh,
#: then strict mode. BASH_ENV is unset again so a shell the script starts is not
#: forced into -e/-u and does not re-read it.
_V13_BASH_ENV_SH = (
    BASH_ENV_SH
    + r"""source "$RW_SDK/rw.sh"
set -euo pipefail
unset BASH_ENV
"""
)


@dataclass
class RunSpec:
    """Everything one run needs. `task` is one entry of `compile_tree(...)["tasks"]`;
    `files` and `executable` are plugin-root paths (`PluginTree.files`/`.executable`);
    `target` is `{path, type, identity, labels?}` (None for a workspace-wide skill);
    `inputs` and `settings` overlay the task's compiled defaults."""

    task: dict
    files: dict[str, bytes]
    executable: frozenset[str] = frozenset()
    target: dict | None = None
    inputs: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    dry_run: bool = False
    timeout_s: float | None = None


@dataclass
class RunOutcome:
    """`envelope` validates against `result-envelope` v1; `artifacts` maps an artifact
    output's name to the local file holding it (under the scope directory). `log_tail`
    is the redacted combined stdout+stderr tail, which the envelope does not carry."""

    envelope: dict
    artifacts: dict[str, Path] = field(default_factory=dict)
    log_tail: str = ""


# -- resolve_target ---------------------------------------------------------------


def _guess_type(plural: str) -> str:
    if plural.endswith(("sses", "ches", "shes", "xes")):
        return plural[:-2]
    if plural.endswith("s"):
        return plural[:-1]
    return plural


def resolve_target(
    resource_path: str, tree: PluginTree, *, identity_overrides: Mapping[str, str]
) -> dict:
    """The lease `target` for `platform/<plural>/<name>/<plural>/<name>...`.

    Where the platform is this plugin's own integration, each plural is looked up in its
    `types` and the parent chain is checked (root first, each type the child of the one
    before). For any other platform the types are guessed from the plurals and a warning
    says so. `identity` is `{<ancestor type>: <name>}` for every ancestor plus
    `{"name": <the target's name>}`; `identity_overrides` replace or add fields. Names
    are percent-decoded (a `/` in a name travels as `%2F`). Raises ValueError for a path
    that cannot be one. The result's optional `warnings` list is for the caller to show;
    it is not part of the lease."""
    path = resource_path.strip()
    parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/")]
    if len(parts) < 3 or len(parts[1:]) % 2 or any(not p for p in parts):
        raise ValueError(
            f"{resource_path!r} is not a resource path: expected "
            "<platform>/<plural>/<name>[/<plural>/<name>...]"
        )
    platform, rest = parts[0], parts[1:]
    pairs = [(rest[i], rest[i + 1]) for i in range(0, len(rest), 2)]

    integration = tree.integration or {}
    types: dict[str, dict] = integration.get("types") or {}
    own = integration.get("platform") == platform and bool(types)
    by_plural = {
        spec["plural"]: name
        for name, spec in types.items()
        if isinstance(spec, dict) and "plural" in spec
    }
    warnings: list[str] = []
    if not own:
        warnings.append(
            f"platform {platform!r} is not this plugin's: types are guessed from the path"
        )

    chain: list[tuple[str, str]] = []  # (type, name)
    previous: str | None = None
    for plural, name in pairs:
        known = by_plural.get(plural) if own else None
        if known is None:
            kind = _guess_type(plural)
            if own:
                warnings.append(
                    f"{plural!r} is not a plural of any type of {platform!r}: guessed {kind!r}"
                )
        else:
            kind = known
            spec = types[kind]
            if previous is None:
                if not spec.get("root"):
                    raise ValueError(
                        f"{resource_path!r}: {kind!r} is not a root type of {platform!r}"
                    )
            else:
                parent = str(spec.get("parent", "")).removeprefix(f"{platform}/")
                if parent != previous:
                    raise ValueError(
                        f"{resource_path!r}: {kind!r} is not a child of {previous!r} "
                        f"(its parent is {parent or 'none'!r})"
                    )
        chain.append((kind, name))
        previous = kind

    identity: dict[str, str] = {kind: name for kind, name in chain[:-1]}
    identity["name"] = chain[-1][1]
    identity.update(identity_overrides)
    target: dict[str, Any] = {
        "path": path.strip("/"),
        "type": f"{platform}/{chain[-1][0]}",
        "identity": identity,
    }
    if warnings:
        target["warnings"] = warnings
    return target


# -- value coercion (the CLI's --input/--setting) ----------------------------------


def coerce_value(type_name: str | None, raw: str) -> Any:
    """`raw` as the declared type: JSON for integer, number, boolean, arrays (`T[]`) and
    object; everything else (string, duration, datetime, enum, an unknown type) stays
    text. Raises ValueError, with the reason, for a value that is not JSON of that kind."""
    kind = type_name or "string"
    if kind.endswith("[]") or kind in ("integer", "number", "boolean", "object"):
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"expected JSON for type {kind}, got {raw!r} ({exc})") from None
    return raw


# -- execute ----------------------------------------------------------------------------


def _clip(message: str) -> str:
    return message[:_MESSAGE_MAX]


def _error(code: str, message: str, scope: str | None = None) -> dict:
    out = {"code": code, "message": _clip(message)}
    if scope:
        out["scope"] = scope[:1024]
    return out


def _duration_seconds(text: str) -> float | None:
    parts = _DURATION_PART.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        return None
    return sum(float(n) * _DURATION_UNITS[u] for n, u in parts)


def _env_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value)


def _env_field(name: str) -> str:
    return input_env_name(re.sub(r"[^A-Za-z0-9_]", "_", name))


def _check_extra_env(extra_env: Mapping[str, str]) -> dict[str, str]:
    for key in extra_env:
        if is_reserved_env_name(key) or key in bundle._HOST_ONLY_ENV or "=" in key or not key:
            raise ValueError(f"extra_env may not set {key!r}")
    return dict(extra_env)


def _materialize(files_dir: Path, files: Mapping[str, bytes], executable: frozenset[str]) -> None:
    """The plugin tree under a fresh `files_dir`. Paths are checked again here, the one
    place a bad path would turn into a write outside the scope."""
    bundle._fresh_dir(files_dir, marker=_PLUGIN_MARKER, what="a plugin directory of ours")
    bundle._write_private_file(files_dir, _PLUGIN_MARKER, "")
    for path, data in files.items():
        posix = PurePosixPath(path)
        if (
            not path
            or posix.is_absolute()
            or "\\" in path
            or "\x00" in path
            or any(part in ("", ".", "..") for part in path.split("/"))
        ):
            raise ValueError(f"refusing to write {path!r}")
        target = files_dir.joinpath(*posix.parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = 0o700 if path in executable else 0o600
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)


def _validator(schema: dict) -> jsonschema.Validator:
    cls = jsonschema.Draft202012Validator
    return cls(schema, registry=referencing.Registry(), format_checker=cls.FORMAT_CHECKER)


def _json_path(parts) -> str:
    return "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in parts)


def _problems(validator: jsonschema.Validator, value: Any, limit: int = 5) -> list[str]:
    found = []
    try:
        for error in validator.iter_errors(value):
            if len(found) == limit:
                found.append("more errors not shown")
                break
            found.append(f"{_json_path(error.absolute_path) or '(value)'}: {error.message}"[:500])
    except Exception as exc:  # noqa: BLE001 -- a plugin-authored schema; contain it
        found.append(f"the schema could not be evaluated ({type(exc).__name__})")
    return found


def _resolve_inputs(task: dict, provided: Mapping[str, Any]) -> tuple[dict, list[dict]]:
    """(inputs, invalid_input errors): defaults overlaid by `provided`, checked against
    the task's compiled `inputSchema`."""
    inputs = {
        name: copy.deepcopy(spec["default"])
        for name, spec in (task.get("inputs") or {}).items()
        if "default" in spec
    }
    inputs.update(provided)
    errors = []
    for error in sorted(
        _validator(task["inputSchema"]).iter_errors(inputs), key=lambda e: list(e.absolute_path)
    ):
        if error.validator == "required":
            names = [n for n in task["inputSchema"].get("required", []) if n not in inputs]
            for name in names or [error.message]:
                errors.append(_error("invalid_input", f"missing required input {name!r}", name))
            continue
        path = _json_path(error.absolute_path)
        scope = str(error.absolute_path[0]) if error.absolute_path else None
        errors.append(_error("invalid_input", f"input{path or ''}: {error.message}", scope))
    seen: set[str] = set()
    unique = [e for e in errors if not (e["message"] in seen or seen.add(e["message"]))]
    return inputs, unique[:20]


def _resolve_settings(task: dict, provided: Mapping[str, Any]) -> tuple[dict, list[dict]]:
    declared: dict[str, dict] = task.get("settings") or {}
    settings = {name: copy.deepcopy(spec.get("default")) for name, spec in declared.items()}
    errors = []
    for name, value in provided.items():
        if name not in declared:
            errors.append(_error("invalid_input", f"unknown setting {name!r}", name))
            continue
        if not bundle._matches_input_type(str(declared[name].get("type", "string")), value):
            errors.append(
                _error(
                    "invalid_input",
                    f"setting {name!r}: expected {declared[name].get('type')}, got {value!r}",
                    name,
                )
            )
            continue
        settings[name] = value
    return settings, errors


def _v13_env(
    spec: RunSpec,
    inputs: dict,
    settings: dict,
    language: str,
) -> dict[str, str]:
    env = {"RW_TASK_CONTRACT": "v13", "RW_SETTINGS_JSON": json.dumps(settings)}
    for name, value in settings.items():
        env[f"RW_SETTING_{_env_field(name)}"] = _env_text(value)
    if spec.target is not None:
        resource = {
            **spec.target.get("identity", {}),
            "path": spec.target["path"],
            "type": spec.target["type"],
        }
        env["RW_RESOURCE_JSON"] = json.dumps(resource)
        for name, value in resource.items():
            env[f"RW_RESOURCE_{_env_field(name)}"] = _env_text(value)
    if language == "bash":
        for name, value in inputs.items():
            env[f"RW_INPUT_{input_env_name(name)}"] = _env_text(value)
    if spec.dry_run:
        env["RW_DRY_RUN"] = "1"
    return env


def execute(
    spec: RunSpec,
    *,
    scope_dir: Path,
    extra_env: Mapping[str, str] | None = None,
    inherit_home: bool = False,
    log: logging.Logger | None = None,
) -> RunOutcome:
    """Runs `spec.task` and returns its envelope. Never raises for a problem with the run
    itself: a bad input, a script that crashes, a timeout and an unreadable output are
    all a `failed` (or `partial`) envelope. `extra_env` is added to the child's
    environment (it may not name a reserved or host-only variable: ValueError), and its
    values are redacted from what comes back. `inherit_home` runs the child with the
    caller's own HOME instead of a private one, for `rwtask run` on a developer's
    machine, where `~/.kube` and `~/.aws` are the credential; never set it for a runner."""
    log = log or _log
    scope_dir = Path(scope_dir)
    task = spec.task
    caller_env = _check_extra_env(extra_env or {})
    started_at = datetime.now(UTC)

    def finish(
        envelope: dict, artifacts: dict[str, Path] | None = None, log_tail: str = ""
    ) -> RunOutcome:
        if spec.dry_run:
            envelope["dryRun"] = True
        violations = validate_contract("result-envelope", envelope)
        if violations:
            first = violations[0]
            envelope = derive_envelope(
                exit_code=1,
                outputs={},
                errors=[
                    _error(
                        "internal",
                        "the result did not satisfy the result envelope contract: "
                        f"{first.path or '/'}: {first.message}",
                    )
                ],
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )
            artifacts = {}
        return RunOutcome(envelope=envelope, artifacts=artifacts or {}, log_tail=log_tail)

    def failed(errors: list[dict]) -> RunOutcome:
        return finish(
            derive_envelope(
                exit_code=1,
                outputs={},
                errors=errors,
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )
        )

    language = task_file_language(task["script"])
    if language is None:
        return failed([_error("internal", f"{task['script']}: unsupported script type")])
    if spec.dry_run and not task.get("dryRun"):
        return failed([_error("invalid_input", "this task does not support dry run")])
    inputs, input_errors = _resolve_inputs(task, spec.inputs)
    settings, setting_errors = _resolve_settings(task, spec.settings)
    if input_errors or setting_errors:
        return failed(input_errors + setting_errors)

    timeout_s = spec.timeout_s
    if timeout_s is None and isinstance(task.get("timeout"), str):
        timeout_s = _duration_seconds(task["timeout"])
    if timeout_s is None:
        timeout_s = float(bundle.DEFAULT_REQUEST_DEADLINE)

    redactor = Redactor([v for v in caller_env.values() if v])
    files_dir = scope_dir / _PLUGIN_DIR
    rw_sdk_dir = scope_dir / ".rw-sdk"
    try:
        bundle._prepare_scope(scope_dir)
        scope_id = bundle._dir_identity(scope_dir)
        _materialize(files_dir, spec.files, spec.executable)
        bundle._fresh_dir(rw_sdk_dir, marker="rw.sh")
        bundle._write_private_file(rw_sdk_dir, "rw.sh", RW_SH)
        bundle._write_private_file(rw_sdk_dir, "bash_env.sh", _V13_BASH_ENV_SH)
        bundle._reset_artifacts_dir(scope_dir)
        bundle._reset_task_channels(scope_dir)
    except (OSError, ValueError) as exc:
        return failed([_error("internal", f"could not prepare the task's scope: {exc}")])

    env = {**caller_env, **_v13_env(spec, inputs, settings, language)}
    skill = task["skill"]
    plugin_name = task["ref"].split(":", 1)[0]
    started_at = datetime.now(UTC)
    with tempfile.TemporaryDirectory(
        prefix=".secrets-", dir=scope_dir, ignore_cleanup_errors=True
    ) as secrets_tmp:
        secrets_dir = Path(secrets_tmp)
        try:
            exit_code, stream, log_tail, timed_out = bundle._execute(
                files_dir=files_dir,
                file_path=task["script"],
                language=language,
                resolved_inputs=inputs if language == "python" else {},
                capability=plugin_name,
                operation=f"{skill}/{task['name']}",
                scope_dir=scope_dir,
                rw_sdk_dir=rw_sdk_dir,
                secrets_dir=secrets_dir,
                credentials={},
                deadline_seconds=timeout_s,
                allow_anonymous=False,
                kubeconfig_path=None,
                tail_guard=redactor.longest,
                log=log,
                extra_env=env,
                lib_dir=files_dir / "skills" / skill / "lib",
                home=Path(os.environ["HOME"]) if inherit_home and "HOME" in os.environ else None,
            )
        except (bundle._LaunchError, OSError) as exc:
            return failed([_error("internal", str(exc))])
        finished_at = datetime.now(UTC)
        log_tail = (redactor.text(log_tail) if log_tail else log_tail)[-bundle.LOG_TAIL_BYTES :]

        events = stream.events
        errors = errors_from_jsonl(scope_dir / bundle._ERRORS_FILE)
        missing = sorted({e["name"] for e in events if e["op"] == "missing_command"})
        for name in missing:
            errors.append(_error("dependency", f"{name!r} is not installed (command not found)"))
        skip = None if missing else next((e for e in events if e["op"] == "skip"), None)

        raw_outputs: dict[str, Any] = {}
        for event in events:
            if event["op"] == "set":
                raw_outputs[event["name"]] = event["value"]
            elif event["op"] == "append":
                bucket = raw_outputs.setdefault(event["name"], [])
                if isinstance(bucket, list):
                    bucket.append(event["value"])
        outputs, output_errors = _check_outputs(task, raw_outputs)
        errors.extend(output_errors)
        if stream.overflowed:
            errors.append(
                _error(
                    "internal",
                    f"the task wrote more than {bundle.MAX_EVENT_STREAM_BYTES} bytes of "
                    "output events; everything after that was discarded",
                )
            )
        if stream.malformed:
            errors.append(
                _error(
                    "internal",
                    f"{stream.malformed} output line(s) were not a well-formed event and were "
                    "dropped; an output was lost",
                )
            )

        artifacts: dict[str, Path] = {}
        if exit_code == 0 and not timed_out and skip is None:
            artifacts, artifact_errors = _collect_artifacts(
                task, scope_dir, secrets_dir, scope_id, events
            )
            errors.extend(artifact_errors)
            for name in artifacts:
                outputs[name] = {"artifact": f"art_local_{name}"}

    if redactor:
        outputs = redactor.value(outputs)
        errors = [
            {k: (v if k == "code" else redactor.value(v)) for k, v in e.items()} for e in errors
        ]
    envelope = derive_envelope(
        exit_code=exit_code,
        outputs=outputs,
        errors=errors,
        started_at=started_at,
        finished_at=finished_at,
        skipped_reason=(skip.get("reason") or "") if skip is not None else None,
        timed_out=timed_out,
        stderr_tail=log_tail,
    )
    if envelope["status"] == "skipped":
        artifacts = {}
        if redactor and "reason" in envelope:
            envelope["reason"] = redactor.text(envelope["reason"])
    return finish(envelope, artifacts, log_tail)


def _check_outputs(task: dict, raw: dict[str, Any]) -> tuple[dict[str, Any], list[dict]]:
    """(the declared data outputs that are valid, an `internal` error per dropped one)."""
    declared: dict[str, dict] = task.get("outputs") or {}
    outputs: dict[str, Any] = {}
    errors: list[dict] = []
    for name, value in raw.items():
        spec = declared.get(name)
        if spec is None:
            errors.append(
                _error("internal", f"output {name!r} is not declared by this task; dropped", name)
            )
        elif "artifact" in spec:
            errors.append(
                _error(
                    "internal",
                    f"output {name!r} is an artifact: write the file to $RW_OUT/{name}, "
                    "not a value; dropped",
                    name,
                )
            )
        elif "jsonSchema" in spec:
            problems = _problems(_validator(spec["jsonSchema"]), value)
            if problems:
                errors.append(
                    _error(
                        "internal",
                        f"output {name!r} does not match its schema and was dropped: "
                        + "; ".join(problems),
                        name,
                    )
                )
            else:
                outputs[name] = value
        elif "kind" in spec:
            try:
                violations = validate_contract(spec["kind"], value)
            except KeyError:
                violations = []
            if violations:
                errors.append(
                    _error(
                        "internal",
                        f"output {name!r} is not a valid {spec['kind']} and was dropped: "
                        f"{violations[0].path or '(value)'}: {violations[0].message}",
                        name,
                    )
                )
            else:
                outputs[name] = value
        else:
            outputs[name] = value
    return outputs, errors


def _out_dir_candidate(scope_dir: Path, name: str) -> str | None:
    """`.rw-out/<name>`, or the only `.rw-out/<name>.<ext>`: scratch files in $RW_OUT
    stay scratch, but the one named for a declared artifact output is that output."""
    out = scope_dir / bundle._OUT_DIR
    exact = out / name
    try:
        if exact.is_file() or exact.is_symlink():
            return f"{bundle._OUT_DIR}/{name}"
        stems = sorted(
            p.name
            for p in out.iterdir()
            if p.name.split(".", 1)[0] == name and (p.is_file() or p.is_symlink())
        )
    except OSError:
        return None
    return f"{bundle._OUT_DIR}/{stems[0]}" if len(stems) == 1 else None


def _collect_artifacts(
    task: dict,
    scope_dir: Path,
    secrets_dir: Path,
    scope_id: tuple[int, int],
    events: list[dict],
) -> tuple[dict[str, Path], list[dict]]:
    specs = {
        name: spec["artifact"]
        for name, spec in (task.get("outputs") or {}).items()
        if "artifact" in spec
    }
    if not specs:
        return {}, []
    registered = {e["name"]: e["path"] for e in events if e["op"] == "artifact"}
    for name in specs:
        if name not in registered and (found := _out_dir_candidate(scope_dir, name)):
            registered[name] = found
    handles, problems = bundle._collect_artifacts(
        scope_dir, secrets_dir, specs, registered, scope_id
    )
    errors = [_error("storage", message.split(": ", 1)[-1]) for message in problems]
    artifacts: dict[str, Path] = {}
    try:
        if handles:
            out_dir = scope_dir / _ARTIFACT_OUT_DIR
            shutil.rmtree(out_dir, ignore_errors=True)
            out_dir.mkdir(mode=0o700)
        for name, handle in handles.items():
            destination = out_dir / name
            with open(destination, "wb") as sink:
                shutil.copyfileobj(handle, sink)
            artifacts[name] = destination
    except OSError as exc:
        errors.append(_error("storage", f"could not keep an artifact: {exc}"))
        artifacts = {}
    finally:
        for handle in handles.values():
            handle.close()
    return artifacts, errors


__all__ = ["RunOutcome", "RunSpec", "coerce_value", "execute", "resolve_target"]
