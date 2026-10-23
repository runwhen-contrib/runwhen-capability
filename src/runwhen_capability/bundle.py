"""run_bundle_request -- bundle mode: a request carries a custom capability's
code inline (`BundleRequestEnvelope.bundle`) instead of the host running code
already baked into the image. Shared by `rwtask serve` (serve.py, when a
polled request's `request` is a BundleRequestEnvelope) and `rwtask run
--local` (run_local.py) -- the same code path, exactly like host.py's
run_request() is for packaged capabilities.

For each request:

1. materialise `bundle.files` under `<scope>/bundle/` and verify
   `bundle.hash` against custom.hashing.content_hash -- a corrupted-in-
   transit or tampered bundle fails closed before anything runs;
2. compile_manifest() the bundle (custom.compiler) -- an invalid bundle
   (validate() found errors) also fails closed, with no execution;
3. run the declared setup (if any), then each requested task, in order.

Unlike a packaged capability's setup/task (an in-process function call,
host.py), each one here runs as its OWN CHILD PROCESS, in its own process
group, with its own deadline -- that is what makes killing the whole process
group on a deadline possible without taking `rwtask serve`'s long-poll loop
down with it. Python and bash are both run this way, for the
same reason: _bundle_entrypoint.py is the small, uniform wrapper that gives a
Python `main(ctx, **inputs)` file the same subprocess boundary a bash file
already needs.

Inputs/outputs cross that boundary two ways:

- Bash: inputs arrive as upper-cased env vars (manifest.input_env_name);
  outputs are written to a private pipe (`$RW_OUTPUT_FD`) via `rw_append`/
  `rw_set`/`rw_skip`/`rw_artifact`, sourced from `$RW_SDK/rw.sh` (_rw_sh.py).
- Python: `_bundle_entrypoint.py` calls `main(ctx, **inputs)` with inputs as
  keyword arguments (snake_case, matching every other Python task in this
  SDK) and writes the returned dict onto the SAME private pipe, one `set`
  event per key -- the two languages share one wire format and one collector
  here.

Artifacts. A task declares a file output as `outputs.<name>.artifact`. Its
bash code writes the file to `$RW_ARTIFACTS_DIR/<name>` (or `rw_artifact <name>
<path|->`, which also registers a file elsewhere in the scope) and a Python
task uses `ctx.artifacts`, which in the child writes into that same directory:
the child never has the relay. Once the child has exited -- and only if the
task otherwise succeeded -- THIS process uploads each file to the relay, and
starts none after the request's deadline. Every file must be a regular file
inside the scope, reached without crossing a symlink (`E_ARTIFACT_PATH`
otherwise); it is opened once, safely, and uploaded from that handle. Files are
uploaded as written, not
redacted: that is the point of a dump. A schema output the platform has said
may spill, and that outgrows MAX_OUTPUT_BYTES, goes the same way as an
`application/json` artifact -- redacted first, since inline outputs are. Where
artifacts are switched off or unsupported, a spill falls back to the caps it
would have had without one; a declared artifact output fails the task.

A `resource`-typed input's value is the run's target resource (dict, JSON
over the wire); a `secret`/`credential`-typed input's value is always a file
path (the raw value is written to a new 0600 file in a fresh, randomly
named 0700 directory that is removed when the invocation ends) -- never
inlined, in either language. A `credential` of kind `k8s.kubeconfig` also
sets `KUBECONFIG` to that same path.

Each child also gets its own HOME and TMPDIR inside the scope, stdin from
/dev/null, and the host's environment minus the variables that locate the
relay and its token (see _inherited_env).
"""

from __future__ import annotations

import errno
import io
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

import jsonschema
import referencing
import requests

from ._redaction import Redactor
from ._rw_sh import BASH_ENV_SH, RW_SH
from .artifacts import SPILL_THRESHOLD_BYTES, spill_json
from .context import Context
from .custom.compiler import CompileError, compile_manifest
from .custom.diagnostics import (
    E_ARTIFACT_PATH,
    E_ARTIFACT_UPLOAD,
    E_COMMAND_NOT_FOUND,
    E_EFFECTS_REQUIRED,
    E_INPUT_TYPE,
    E_OUTPUT_MALFORMED,
    E_OUTPUT_SCHEMA,
    E_OUTPUT_TOO_LARGE,
    E_SCHEMA_FEATURE,
    E_TIMEOUT,
    E_UNKNOWN_SDK_HELPER,
)
from .custom.hashing import content_hash
from .custom.manifest import (
    Manifest,
    input_env_name,
    is_allowed_path,
    is_reserved_env_name,
    legacy_input_env_name,
    python_kwarg_name,
    task_file_language,
)
from .custom.schema_features import regex_keyword_paths
from .custom.yaml_lines import safe_load
from .errors import ArtifactError, ArtifactsUnavailableError
from .models import (
    BundleRequestEnvelope,
    InputArtifactDecl,
    OutputDecl,
    ResultEnvelope,
    SetupResult,
    TaskResult,
)
from .pathsafe import confined_to
from .relay import RelayClient

LOG_TAIL_BYTES = 64 * 1024
# A schema output over this is cut to fit (a list), fails the task, or -- where the
# platform allows it -- spills into an artifact.
MAX_OUTPUT_BYTES = SPILL_THRESHOLD_BYTES
MAX_RESULT_BYTES = 1024 * 1024
# Output events a task may write before the rest are discarded -- well above
# MAX_RESULT_BYTES so an oversized list can still be truncated to fit, but
# bounded, since the host holds every event in memory until the task ends.
MAX_EVENT_STREAM_BYTES = 8 * MAX_RESULT_BYTES
MAX_VALUE_DEPTH = 64
MAX_SCHEMA_ERRORS = 20
MAX_SCHEMA_ERROR_CHARS = 500
# seconds for the whole request (setup and every task together); used when
# the caller gives no deadline
DEFAULT_REQUEST_DEADLINE = 300
_DRAIN_JOIN_TIMEOUT = 5
_READ_CHUNK = 65536

_DURATION_RE = re.compile(r"^\d+(\.\d+)?(ms|s|m|h|d)$")

# Every artifact failure -- the relay's E_ARTIFACT_* codes, E_ARTIFACTS_DISABLED,
# E_ARTIFACT_PATH, E_ARTIFACT_UPLOAD -- starts with this, and fails the task.
_ARTIFACT_ERROR_PREFIX = "E_ARTIFACT"
# Written into every artifacts directory this host creates. Like bundle/ and .rw-sdk/, a
# non-empty `artifacts/` that lacks it (a user's own, in a reused `rwtask run --workdir`)
# is never replaced.
_ARTIFACTS_MARKER = ".rw-artifacts-dir"


@dataclass
class _RunOutcome:
    status: str  # "ok" | "failed" | "skipped" | "timeout"
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    reason: str | None = None
    errors: list[str] = field(default_factory=list)
    log_tail: str | None = None


def run_bundle_request(
    request: BundleRequestEnvelope,
    credentials: dict[str, str],
    scope_dir: Path,
    log: logging.Logger | None = None,
    deadline_seconds: float = DEFAULT_REQUEST_DEADLINE,
    allow_anonymous_credentials: bool = False,
    *,
    relay: RelayClient | None = None,
    outputs: Sequence[OutputDecl] = (),
    input_artifacts: Sequence[InputArtifactDecl] = (),
) -> ResultEnvelope:
    """Runs `request`'s bundle: setup, then each requested task.

    `relay`, `outputs` and `input_artifacts` are the request's artifact
    wiring (see serve.py). They stay in this host process: the bundle's
    child process is never given the relay -- no environment variable, file
    descriptor or argument carries it, and `_inherited_env` still strips the
    host's own variables -- so the host alone talks to the runner, uploading
    artifacts on the task's behalf once the child has exited. `outputs` is
    what tells the host a schema output may spill (OutputDecl.spill); without
    a relay (`rwtask run`) artifacts land in the local sink instead.

    `deadline_seconds` is ONE budget for the whole request, not a fresh
    allowance per task: setup and the tasks share it, each running with
    whatever is left when it starts, and a task that would start after it
    has run out is reported as a timeout without running. The runner gives
    the whole request that long (its deadlineMs); a per-task allowance let
    setup plus N tasks run (N + 1) times over it, long after the runner had
    given up on the result."""
    log = log or logging.getLogger("runwhen_capability.bundle")
    scope_dir = Path(scope_dir)
    result = ResultEnvelope()
    deadline_at = time.monotonic() + deadline_seconds

    files: dict[str, str] = {}
    for bundle_file in request.bundle.files:
        if bundle_file.path in files:
            # Two entries for one path: which one "is" the bundle depends on
            # list order, so the hash no longer pins what runs. Fail closed.
            result.setup = SetupResult(
                status="failed", error=f"bundle lists {bundle_file.path!r} more than once"
            )
            return result
        files[bundle_file.path] = bundle_file.content
    computed = content_hash(files)
    if computed != request.bundle.hash:
        result.setup = SetupResult(
            status="failed",
            error=(
                f"bundle hash mismatch: request says {request.bundle.hash!r}, computed {computed!r}"
            ),
        )
        return result

    try:
        # E_EFFECTS_REQUIRED and E_UNKNOWN_SDK_HELPER are authoring/publish
        # gates; an already-published (immutable) bundle that predates them must
        # still run.
        compiled = compile_manifest(
            files, ignore=frozenset({E_EFFECTS_REQUIRED, E_UNKNOWN_SDK_HELPER})
        )
    except CompileError as exc:
        result.setup = SetupResult(status="failed", error=f"invalid bundle: {exc}")
        return result

    manifest = Manifest.model_validate(safe_load(files["capability.yaml"]))
    compiled_tasks = {t["name"]: t for t in compiled["tasks"]}

    files_dir = scope_dir / "bundle"
    rw_sdk_dir = scope_dir / ".rw-sdk"
    try:
        _prepare_scope(scope_dir)
        # What an artifact's path is walked from must still be THIS directory when the
        # task has finished (see _open_artifact).
        scope_id = _dir_identity(scope_dir)
        _materialize(files_dir, files)
        _fresh_dir(rw_sdk_dir, marker="rw.sh")
        _write_private_file(rw_sdk_dir, "rw.sh", RW_SH)
        _write_private_file(rw_sdk_dir, "bash_env.sh", BASH_ENV_SH)
    except (OSError, ValueError) as exc:
        result.setup = SetupResult(status="failed", error=f"could not write the bundle: {exc}")
        return result

    target = request.target.model_dump(mode="json") if request.target else None
    # Every secret/credential value the request carries -- not only the ones
    # a given task declares -- is redacted from everything any task returns.
    redactor = Redactor([value for value in credentials.values() if value])

    if manifest.setup is not None:
        outcome = _run_setup_or_task(
            files_dir=files_dir,
            file_path=manifest.setup.file,
            input_specs={
                name: spec.model_dump(mode="json") for name, spec in manifest.inputs.items()
            },
            declared_outputs=None,
            is_setup=True,
            provided_inputs=request.inputs,
            target=target,
            credentials=credentials,
            capability=compiled["capability"],
            operation="setup",
            scope_dir=scope_dir,
            rw_sdk_dir=rw_sdk_dir,
            deadline_seconds=deadline_at - time.monotonic(),
            deadline_at=deadline_at,
            scope_id=scope_id,
            allow_anonymous=allow_anonymous_credentials,
            redactor=redactor,
            relay=relay,
            output_decls=outputs,
            log=log.getChild("setup"),
        )
        outcome = _redacted(outcome, redactor)
        result.setup = SetupResult(
            # ok/failed/timeout -- never "skipped" (is_setup=True maps a skip to failed)
            status=outcome.status,
            outputs=outcome.outputs,
            error=outcome.error,
            logTail=outcome.log_tail,
        )

    # A failed/timed-out setup does not stop the requested tasks from being
    # attempted -- host.run_request() does the same for packaged
    # capabilities (see its module docstring): each task's own outcome is
    # still reported on its own terms.
    for task_name in request.tasks:
        task_compiled = compiled_tasks.get(task_name)
        if task_compiled is None:
            result.tasks.append(
                TaskResult(task=task_name, status="failed", error=f"unknown task {task_name!r}")
            )
            continue

        outcome = _run_setup_or_task(
            files_dir=files_dir,
            file_path=task_compiled["file"],
            input_specs=task_compiled["inputs"],
            declared_outputs=task_compiled["outputs"],
            is_setup=False,
            provided_inputs=request.inputs,
            target=target,
            credentials=credentials,
            capability=compiled["capability"],
            operation=task_name,
            scope_dir=scope_dir,
            rw_sdk_dir=rw_sdk_dir,
            deadline_seconds=deadline_at - time.monotonic(),
            deadline_at=deadline_at,
            scope_id=scope_id,
            allow_anonymous=allow_anonymous_credentials,
            redactor=redactor,
            relay=relay,
            output_decls=outputs,
            log=log.getChild(task_name),
        )
        outcome = _redacted(outcome, redactor)
        result.tasks.append(
            TaskResult(
                task=task_name,
                status=outcome.status,
                outputs=outcome.outputs,
                error=outcome.error,
                reason=outcome.reason,
                errors=outcome.errors,
                logTail=outcome.log_tail,
            )
        )
    return result


# -- one setup/task invocation -------------------------------------------------


def _run_setup_or_task(**invocation: Any) -> _RunOutcome:
    """_run_invocation(), then makes sure the artifacts directory carries its marker.
    A task may have removed `$RW_ARTIFACTS_DIR` and `rw_artifact` (or the task)
    recreated it, unmarked; left like that, the next `rwtask run --workdir` on this
    scope would take it for a directory of the user's and refuse to start."""
    try:
        return _run_invocation(**invocation)
    finally:
        _stamp_artifacts_dir(invocation["scope_dir"])


def _run_invocation(
    *,
    files_dir: Path,
    file_path: str,
    input_specs: dict[str, dict],
    declared_outputs: dict[str, dict] | None,
    is_setup: bool,
    provided_inputs: dict[str, Any],
    target: dict | None,
    credentials: dict[str, str],
    capability: str,
    operation: str,
    scope_dir: Path,
    rw_sdk_dir: Path,
    deadline_seconds: float,
    deadline_at: float,
    scope_id: tuple[int, int],
    allow_anonymous: bool,
    redactor: Redactor,
    relay: RelayClient | None,
    output_decls: Sequence[OutputDecl],
    log: logging.Logger,
) -> _RunOutcome:
    """Runs one setup/task file. Outputs, errors and the skip reason come
    back unredacted -- run_bundle_request() redacts the whole outcome --
    except the log tail, which has to be redacted before it is cut to size
    (see _execute), and a spilled output, which is redacted before it is
    uploaded (see _apply_size_caps). Artifact files are uploaded here, after
    the child has exited and only when everything else about the task
    succeeded (see _upload_artifacts); `deadline_at` (a time.monotonic()
    value) is the request's deadline, after which no upload is started."""
    language = task_file_language(file_path)
    if language is None:
        return _RunOutcome(status="failed", error=f"{file_path}: unsupported file type")
    if deadline_seconds <= 0:
        message = f"{E_TIMEOUT}: the request's deadline passed before this started"
        return _RunOutcome(status="timeout", error=message, errors=[message])

    # Each invocation's secret files live in their own fresh, randomly named
    # 0700 directory, removed as soon as the invocation ends -- a later task
    # (or anything an earlier one left behind) never finds them at a known
    # path, and they never outlive the process that needed them.
    with tempfile.TemporaryDirectory(
        prefix=".secrets-", dir=scope_dir, ignore_cleanup_errors=True
    ) as secrets_tmp:
        secrets_dir = Path(secrets_tmp)
        resolved, type_errors = _resolve_inputs(
            input_specs, provided_inputs, target, credentials, secrets_dir, allow_anonymous
        )
        if type_errors:
            return _RunOutcome(status="failed", error="; ".join(type_errors), errors=type_errors)

        # A Python task's ctx.credential() sees only the secrets/credentials its
        # own declared inputs name -- exactly what a bash task gets as files --
        # never every credential the request happens to carry.
        task_credentials = {
            name: credentials[name]
            for name, spec in input_specs.items()
            if spec.get("type") in ("secret", "credential") and name in credentials
        }

        kubeconfig_path = next(
            (
                resolved[name]
                for name, spec in input_specs.items()
                if spec.get("type") == "credential"
                and spec.get("kind") == "k8s.kubeconfig"
                and name in resolved
            ),
            None,
        )

        try:
            # Fresh for every invocation: what an earlier task left in it is never
            # this task's artifact. (The request's first check, which refuses a
            # directory that is not ours, is in _prepare_scope.)
            _reset_artifacts_dir(scope_dir)
            exit_code, stream, log_tail, timed_out = _execute(
                files_dir=files_dir,
                file_path=file_path,
                language=language,
                resolved_inputs=resolved,
                capability=capability,
                operation=operation,
                scope_dir=scope_dir,
                rw_sdk_dir=rw_sdk_dir,
                secrets_dir=secrets_dir,
                credentials=task_credentials,
                deadline_seconds=deadline_seconds,
                allow_anonymous=allow_anonymous,
                kubeconfig_path=kubeconfig_path,
                tail_guard=redactor.longest,
                log=log,
            )
        except (_LaunchError, OSError) as exc:
            return _RunOutcome(status="failed", error=str(exc), errors=[str(exc)])
        log_tail = redactor.text(log_tail)[-LOG_TAIL_BYTES:] if log_tail else log_tail

        if timed_out:
            message = (
                f"{E_TIMEOUT}: killed after {deadline_seconds:.1f}s, at the request's deadline"
            )
            return _RunOutcome(status="timeout", error=message, errors=[message], log_tail=log_tail)

        events = stream.events
        # Checked before skip: a missing tool is a bug in the task, never
        # "nothing to check", so a later rw_skip must not turn it into a pass.
        missing = sorted({e["name"] for e in events if e["op"] == "missing_command"})
        missing_error = None
        if missing:
            names = ", ".join(repr(name) for name in missing)
            missing_error = (
                f"{E_COMMAND_NOT_FOUND}: {names} is not on this image; bash carried on "
                "without it (exit 127). Use a tool the image has"
            )
        skip_event = None if missing else next((e for e in events if e["op"] == "skip"), None)
        if skip_event is not None:
            reason = skip_event.get("reason") or ""
            if is_setup:
                # "Only tasks can skip" -- a setup that skips has not
                # materialised anything, exactly like a packaged capability's
                # setup raising SkipTask (host.py, SkipTask's own docstring).
                return _RunOutcome(
                    status="failed", error=reason or "setup skipped", log_tail=log_tail
                )
            return _RunOutcome(status="skipped", reason=reason, log_tail=log_tail)

        # _parse_event() already guaranteed every event's shape: op is
        # set/append/skip/artifact, name a string, reason a string, path a string.
        outputs: dict[str, Any] = {}
        for event in events:
            if event["op"] == "set":
                outputs[event["name"]] = event["value"]
            elif event["op"] == "append":
                bucket = outputs.setdefault(event["name"], [])
                if isinstance(bucket, list):
                    bucket.append(event["value"])

        artifact_specs = {
            name: decl["artifact"]
            for name, decl in (declared_outputs or {}).items()
            if "artifact" in decl
        }
        if declared_outputs is not None:
            # A name the source produced but never declared already failed
            # validate() at authoring time (E_OUTPUT_UNDECLARED) -- at runtime
            # it is simply dropped rather than failing an otherwise-good result.
            # An artifact output is only ever the ref of a file this host
            # uploaded (below): a value the task set for one -- a ref it made
            # up, a string -- is dropped the same way.
            outputs = {
                name: value
                for name, value in outputs.items()
                if name in declared_outputs and name not in artifact_specs
            }

        errors: list[str] = [missing_error] if missing_error else []
        if exit_code != 0:
            errors.append(f"process exited {exit_code}")
        if stream.overflowed:
            errors.append(
                f"{E_OUTPUT_TOO_LARGE}: the task wrote more than {MAX_EVENT_STREAM_BYTES} bytes "
                "of output events; everything after that was discarded"
            )
        if stream.malformed:
            # Fails the run, like a schema violation: a dropped line is an
            # output the task meant to produce and didn't, so the result must
            # not pass as evidence. Every well-formed line still counts.
            errors.append(
                f"{E_OUTPUT_MALFORMED}: {stream.malformed} output line(s) were not a "
                "well-formed rw_set/rw_append/rw_skip/rw_artifact event and were dropped; "
                "an output was lost"
            )

        if declared_outputs is not None:
            for name, value in outputs.items():
                if "schema" in declared_outputs[name]:  # an artifact output has none
                    errors.extend(
                        _validate_output_schema(name, value, declared_outputs[name]["schema"])
                    )

        # A list output cut to fit its size cap is noted in `errors` but does
        # NOT fail the task -- only a non-list output/whole result still over
        # the cap (E_OUTPUT_TOO_LARGE) does, same as an exit code, a schema
        # violation, a refused schema, a dropped output line
        # (E_OUTPUT_MALFORMED) or an artifact that could not be uploaded
        # (E_ARTIFACT_*). Notes (truncation) carry no E_ prefix, so this
        # is exactly "every error EXCEPT a plain note".
        failing = (
            E_OUTPUT_SCHEMA,
            E_OUTPUT_TOO_LARGE,
            E_OUTPUT_MALFORMED,
            E_COMMAND_NOT_FOUND,
            E_SCHEMA_FEATURE,
            E_TIMEOUT,  # only the deadline passing before an upload started gets here
            _ARTIFACT_ERROR_PREFIX,
        )

        def failed() -> bool:
            return exit_code != 0 or any(msg.startswith(failing) for msg in errors)

        # An output the platform lets spill goes to an artifact instead of being
        # truncated or failing the task -- but a task that has already failed
        # uploads nothing, so it is capped exactly as before.
        spillable = frozenset(
            decl.name for decl in output_decls if decl.spill and decl.task == operation
        )
        spill = frozenset() if failed() or is_setup else spillable
        uncapped = outputs
        outputs, size_errors, spilled = _apply_size_caps(uncapped, spill, redactor)
        caps_at = len(errors)
        errors.extend(size_errors)

        # Nothing is uploaded unless the task succeeded in every other respect: a
        # half-written dump from a task that then failed must not be finalised as
        # a complete artifact.
        upload_timed_out = False
        if not failed() and (artifact_specs or spilled):
            registered = {e["name"]: e["path"] for e in events if e["op"] == "artifact"}
            files, path_errors = _collect_artifacts(
                scope_dir, secrets_dir, artifact_specs, registered, scope_id
            )
            try:
                errors.extend(path_errors)
                if not path_errors and (files or spilled):
                    refs, upload_errors, unavailable = _upload_artifacts(
                        capability=capability,
                        operation=operation,
                        scope_dir=scope_dir,
                        relay=relay,
                        output_decls=output_decls,
                        artifact_specs=artifact_specs,
                        files=files,
                        spilled=spilled,
                        deadline_at=deadline_at,
                    )
                    if unavailable is not None:
                        # Artifacts are off, or this runner has none: what could not
                        # spill is capped as it was before spill existed.
                        unspilled = frozenset(spilled) - refs.keys()
                        log.warning(
                            "spill unavailable (%s): output(s) %s capped inline instead",
                            unavailable,
                            ", ".join(repr(name) for name in sorted(unspilled)),
                        )
                        outputs, recapped, _ = _apply_size_caps(
                            uncapped, spill - unspilled, redactor
                        )
                        errors[caps_at : caps_at + len(size_errors)] = recapped
                    errors.extend(upload_errors)
                    outputs.update(refs)
                    upload_timed_out = any(e.startswith(E_TIMEOUT) for e in upload_errors)
            finally:
                for handle in files.values():
                    handle.close()

        if failed():
            return _RunOutcome(
                status="timeout" if upload_timed_out else "failed",
                outputs=outputs,
                error="; ".join(errors),
                errors=errors,
                log_tail=log_tail,
            )
        return _RunOutcome(status="ok", outputs=outputs, errors=errors, log_tail=log_tail)


# -- input resolution -----------------------------------------------------------


def _resolve_inputs(
    input_specs: dict[str, dict],
    provided: dict[str, Any],
    target: dict | None,
    credentials: dict[str, str],
    secrets_dir: Path,
    allow_anonymous: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    resolved: dict[str, Any] = {}
    errors: list[str] = []

    for name, spec in input_specs.items():
        spec_type = spec.get("type")
        optional = bool(spec.get("optional"))

        if spec_type == "resource":
            if target is not None:
                resolved[name] = target
            elif not optional:
                errors.append(f"{E_INPUT_TYPE} inputs.{name}: no target resource for this run")
            continue

        if spec_type in ("secret", "credential"):
            raw = credentials.get(name)
            if raw is None:
                # `--allow-anonymous` (rwtask run --local only, never
                # `rwtask serve`) degrades a required-but-unresolved
                # secret/credential the same way Context.credential()
                # already does for a packaged capability -- see its
                # docstring. The input is simply absent downstream (no env
                # var, no file), not an empty string standing in for it.
                if not optional and not allow_anonymous:
                    errors.append(f"{E_INPUT_TYPE} inputs.{name}: no {spec_type} resolved")
                continue
            resolved[name] = str(_write_private_file(secrets_dir, name, raw))
            continue

        value = provided.get(name, spec.get("default"))
        if value is None:
            if not optional:
                errors.append(f"{E_INPUT_TYPE} inputs.{name}: no value provided and no default")
            continue
        if not _matches_input_type(spec_type, value):
            errors.append(f"{E_INPUT_TYPE} inputs.{name}: expected {spec_type}, got {value!r}")
            continue
        resolved[name] = value

    return resolved, errors


def _matches_input_type(spec_type: str, value: Any) -> bool:
    if spec_type.endswith("[]"):
        return isinstance(value, list) and all(
            _matches_input_type(spec_type[:-2], item) for item in value
        )
    if spec_type.startswith("enum(") and spec_type.endswith(")"):
        allowed = spec_type[len("enum(") : -1].split("|")
        return isinstance(value, str) and value in allowed
    if spec_type == "string":
        return isinstance(value, str)
    if spec_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if spec_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if spec_type == "boolean":
        return isinstance(value, bool)
    if spec_type == "duration":
        return isinstance(value, str) and bool(_DURATION_RE.match(value))
    if spec_type == "datetime":
        if not isinstance(value, str):
            return False
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True
    return True  # an unrecognised type string already failed validate(); don't also block execution


# -- output schema and size checks --------------------------------------------


def _validate_output_schema(name: str, value: Any, schema: dict) -> list[str]:
    """E_OUTPUT_SCHEMA messages for `value`, at most MAX_SCHEMA_ERRORS of
    them, each at most MAX_SCHEMA_ERROR_CHARS long (a message quotes the
    offending value, and a list can fail once per item).

    References resolve only within `schema` itself: an empty Registry
    means jsonschema never fetches a URL or reads a file to follow a
    `$ref` -- by default it would. A schema that cannot be evaluated at all
    fails this one output, never the whole request.

    A schema with a regex keyword is never evaluated at all: validate()
    already refuses one (E_SCHEMA_FEATURE), and this is the guard for a
    compiled schema that reached the host some other way -- a regex that
    backtracks catastrophically would run here, in the host process,
    outside any deadline."""
    regex_paths = regex_keyword_paths(schema)
    if regex_paths:
        return [
            f"{E_SCHEMA_FEATURE} outputs.{name}: the schema uses regex keywords "
            f"({', '.join(regex_paths[:5])}), which the host does not evaluate"
        ]
    messages = []
    try:
        validator_cls = jsonschema.validators.validator_for(schema)
        validator = validator_cls(schema, registry=referencing.Registry())
        for error in validator.iter_errors(value):
            if len(messages) == MAX_SCHEMA_ERRORS:
                messages.append(f"{E_OUTPUT_SCHEMA} outputs.{name}: more errors not shown")
                break
            json_path = "".join(
                f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path
            )
            message = f"{E_OUTPUT_SCHEMA} outputs.{name}{json_path}: {error.message}"
            messages.append(message[:MAX_SCHEMA_ERROR_CHARS])
    except Exception as exc:  # noqa: BLE001 -- the schema is bundle-authored; contain it
        messages.append(
            f"{E_OUTPUT_SCHEMA} outputs.{name}: the schema could not be evaluated "
            f"({type(exc).__name__})"
        )
    return messages


def _json_size(value: Any) -> int:
    """The size the caps measure: `spill_json`'s, compact JSON -- what the
    result carries, and how the packaged host measures a spill (host.py)."""
    return len(spill_json(value))


def _truncate_list_to_budget(value: list, budget: int) -> list:
    """The largest prefix of `value` whose JSON encoding (`_json_size`) fits
    `budget` bytes, computed in one O(n) pass -- each element is encoded
    exactly once, not the whole (shrinking) list once per dropped element.
    Exact, not approximate: compact JSON renders a list as
    "[" + ",".join(dumps(item) for item in list) + "]", so summing each
    item's own encoded length plus its "," separator reproduces the real
    total byte-for-byte."""
    kept: list = []
    total = 2  # the array's own "[" + "]"
    for item in value:
        extra = _json_size(item) + (1 if kept else 0)  # "," between items
        if total + extra > budget:
            break
        total += extra
        kept.append(item)
    return kept


def _apply_size_caps(
    outputs: dict[str, Any],
    spill: frozenset[str] = frozenset(),
    redactor: Redactor | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, bytes]]:
    """Applies the per-output and per-result caps, measured as compact JSON
    (`_json_size`). Returns the outputs that stay inline, the error messages,
    and -- for each output named in `spill` that outgrew MAX_OUTPUT_BYTES --
    the bytes to upload for it: its value REDACTED (the inline ones are
    redacted by the caller, after this) and serialised compactly as JSON. A
    spilled output is neither truncated nor counted against the per-result
    cap, which applies to what stays inline; nothing is uploaded here, and if
    the result is dropped for being over the cap so are the spilled bytes."""
    errors: list[str] = []
    capped: dict[str, Any] = {}
    spilled: dict[str, bytes] = {}
    for name, value in outputs.items():
        size = _json_size(value)
        if size <= MAX_OUTPUT_BYTES:
            capped[name] = value
            continue
        if name in spill:
            if redactor:
                value = redactor.value(value)
            spilled[name] = spill_json(value)
            continue
        if isinstance(value, list):
            truncated = _truncate_list_to_budget(value, MAX_OUTPUT_BYTES)
            capped[name] = truncated
            errors.append(
                f"outputs.{name} exceeded {MAX_OUTPUT_BYTES} bytes and was truncated to "
                f"{len(truncated)} item(s)"
            )
        else:
            errors.append(
                f"{E_OUTPUT_TOO_LARGE} outputs.{name}: {size} bytes, over the "
                f"{MAX_OUTPUT_BYTES}-byte per-output limit"
            )

    total = sum(_json_size(v) for v in capped.values())
    if total > MAX_RESULT_BYTES:
        # The task fails; its outputs are dropped rather than sent anyway --
        # the limit exists to bound what goes back to the relay, and many
        # outputs just under their own cap can add up to many times it.
        errors.append(
            f"{E_OUTPUT_TOO_LARGE}: result is {total} bytes, over the {MAX_RESULT_BYTES}-byte "
            "per-result limit; its outputs were dropped"
        )
        return {}, errors, {}
    return capped, errors, spilled


# -- artifacts: finding the files a task wrote, and uploading them ----------------


class _ArtifactPathError(ValueError):
    """A file a task asked to upload is not one the host will read."""


def _clipped(message: str) -> str:
    """`message` cut to MAX_SCHEMA_ERROR_CHARS, like a schema error: it quotes a
    path or a library's text, which the task (or whatever it left on disk) chose."""
    return message[:MAX_SCHEMA_ERROR_CHARS]


def _dir_identity(path: Path | int) -> tuple[int, int]:
    """(device, inode) of a directory, by path (following links) or open descriptor."""
    info = os.fstat(path) if isinstance(path, int) else os.stat(path)
    return info.st_dev, info.st_ino


def _open_artifact(
    scope_dir: Path,
    secrets_dir: Path,
    raw: str,
    scope_id: tuple[int, int] | None = None,
) -> BinaryIO:
    """Opens `raw` -- an artifact path from the child, absolute (rw_artifact) or
    relative to the scope -- for reading, and returns the open file. It is a
    regular file inside `scope_dir` reached without crossing a symlink, or
    this raises _ArtifactPathError.

    The file is opened exactly once, and that handle is what gets uploaded: a
    separate check followed by a second open by name could be raced by a
    process that outlived the task (one that left its process group). So the
    path is normalised, and then walked from the scope with O_NOFOLLOW at every
    step -- each directory opened relative to the one before it, so a swap
    higher up cannot redirect the walk -- and the leaf is opened non-blocking
    (a FIFO must not wedge `rwtask serve`) and then required to be a regular
    file by fstat on the descriptor itself (not /dev/zero, a socket, ...).

    Nothing above the scope matters, which is why the scope is also accepted by
    its resolved name (the child's `$PWD` is the kernel's idea of its working
    directory) and the walk starts from that resolved name. But the walk's
    starting point is itself found by name, so it is opened with O_NOFOLLOW and
    checked against `scope_id`, the (device, inode) recorded when the scope was
    prepared: a scope renamed away and replaced (by a symlink, or another
    directory) by a process that outlived the task is `E_ARTIFACT_PATH`, not
    a walk through someone else's tree. The invocation's secrets directory is
    off limits: a credential file is never an artifact."""
    if not raw or "\x00" in raw:
        raise _ArtifactPathError("the path is empty or has a NUL byte")
    candidate = os.path.normpath(os.path.join(scope_dir, raw))
    for anchor in dict.fromkeys((os.path.abspath(scope_dir), os.path.realpath(scope_dir))):
        if confined_to(Path(anchor), Path(candidate)):
            break
    else:
        raise _ArtifactPathError(f"{raw} is outside the task's scope")
    parts = os.path.relpath(candidate, anchor).split(os.sep)
    if parts[0] == secrets_dir.name:
        raise _ArtifactPathError(f"{raw} is inside the credentials directory")
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0)
    try:
        dir_fd = os.open(os.path.realpath(scope_dir), dir_flags)
    except OSError as exc:
        raise _ArtifactPathError(
            f"the task's scope directory cannot be opened ({exc.strerror or exc})"
        ) from None
    try:
        if scope_id is not None and _dir_identity(dir_fd) != scope_id:
            raise _ArtifactPathError(
                "the task's scope directory is not the one that was prepared for it"
            )
        for part in parts[:-1]:
            next_fd = os.open(part, dir_flags, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = next_fd
        file_fd = os.open(parts[-1], file_flags, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _ArtifactPathError(
                f"{raw} goes through a symbolic link (or a file that is not a directory)"
            ) from None
        raise _ArtifactPathError(f"{raw} cannot be opened ({exc.strerror or exc})") from None
    finally:
        os.close(dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise _ArtifactPathError(f"{raw} is not a regular file")
        return os.fdopen(file_fd, "rb")
    except BaseException:
        os.close(file_fd)
        raise


def _collect_artifacts(
    scope_dir: Path,
    secrets_dir: Path,
    artifact_specs: dict[str, dict],
    registered: dict[str, str],
    scope_id: tuple[int, int] | None = None,
) -> tuple[dict[str, BinaryIO], list[str]]:
    """The open file to upload for each declared artifact output, by name: the
    one the task registered (rw_artifact), else a regular file sitting at
    `$RW_ARTIFACTS_DIR/<name>`; none for an output the task never wrote.
    A registration for a name that is not a declared artifact output is
    ignored, like any undeclared output. Any file that fails _open_artifact()
    is an E_ARTIFACT_PATH error, and so no upload happens: every file already
    opened is closed again. The caller closes the files it is given."""
    files: dict[str, BinaryIO] = {}
    errors: list[str] = []
    for name in sorted(artifact_specs):
        raw = registered.get(name)
        if raw is None:
            default = os.path.join(scope_dir, _ARTIFACTS_DIR, name)
            try:
                mode = os.lstat(default).st_mode
            except OSError:
                continue  # never written (or a parent of it is a file)
            if stat.S_ISDIR(mode):
                continue
            raw = default
        try:
            files[name] = _open_artifact(scope_dir, secrets_dir, raw, scope_id)
        except _ArtifactPathError as exc:
            errors.append(_clipped(f"{E_ARTIFACT_PATH}: artifact {name!r}: {exc}"))
    if errors:
        for handle in files.values():
            handle.close()
        files = {}
    return files, errors


def _upload_artifacts(
    *,
    capability: str,
    operation: str,
    scope_dir: Path,
    relay: RelayClient | None,
    output_decls: Sequence[OutputDecl],
    artifact_specs: dict[str, dict],
    files: dict[str, BinaryIO],
    spilled: dict[str, bytes],
    deadline_at: float,
) -> tuple[dict[str, Any], list[str], ArtifactsUnavailableError | None]:
    """Uploads the open files and the spilled outputs through a Context of the
    host's own -- the one that has the relay -- and returns `{name: {"$artifact":
    ref}}` for them, the errors, and the ArtifactsUnavailableError a spill met,
    if one did. The first failure ends it: it returns no refs, and one error
    that starts with the error's code (E_ARTIFACT_TOO_LARGE, ...). With no relay
    (`rwtask run`) the same calls land in the local sink and yield `local_...`
    refs.

    A spill the relay cannot take because artifacts are off or unsupported is
    not a failure: the spills from that one on are not uploaded, no error is
    returned, and the caller caps those outputs inline instead (the refs of what
    was uploaded are kept). A file -- an artifact output the task declared --
    is never treated that way: it fails like any other upload error.

    The files are uploaded from the handles _open_artifact() returned, and the
    spilled bytes from memory (they are bounded by MAX_EVENT_STREAM_BYTES): the
    upload never opens anything by name. Both go through `add_file`, so a busy
    runner (503) is retried. No upload is started once `deadline_at` (a
    time.monotonic() value, the request's deadline) has passed: that is
    E_TIMEOUT naming the artifact. One already running is left to the relay
    client's own timeouts, or to the runner cancelling the request."""
    ctx = Context(
        capability=capability,
        operation=operation,
        workdir=scope_dir,
        relay=relay,
        task=operation,
        outputs=output_decls,
    )
    refs: dict[str, Any] = {}
    current = ""
    try:
        for current, handle in files.items():
            if time.monotonic() >= deadline_at:
                return {}, [_deadline_error(current)], None
            ref = ctx.artifacts.add_file(
                handle, current, content_type=artifact_specs[current]["contentType"]
            )
            refs[current] = ref.model_dump(mode="json")
        for current, data in spilled.items():
            if time.monotonic() >= deadline_at:
                return {}, [_deadline_error(current)], None
            try:
                ref = ctx.artifacts.add_file(io.BytesIO(data), current, "application/json")
            except ArtifactsUnavailableError as exc:
                return refs, [], exc
            refs[current] = ref.model_dump(mode="json")
    except ArtifactError as exc:
        code = exc.code if exc.code.startswith(_ARTIFACT_ERROR_PREFIX) else E_ARTIFACT_UPLOAD
        return {}, [_clipped(f"{code}: artifact {current!r}: {exc}")], None
    except (OSError, requests.RequestException) as exc:
        return {}, [_clipped(f"{E_ARTIFACT_UPLOAD}: artifact {current!r}: {exc}")], None
    return refs, [], None


def _deadline_error(name: str) -> str:
    return f"{E_TIMEOUT}: the request's deadline passed before artifact {name!r} was uploaded"


# -- redaction ------------------------------------------------------------------


def _redacted(outcome: _RunOutcome, redactor: Redactor) -> _RunOutcome:
    """`outcome` with every secret removed from what a task controls or
    can influence: outputs (keys included), the error summary, each
    runtime error -- a schema error quotes the offending value -- and the
    skip reason. The log tail is already redacted (_run_setup_or_task)."""
    if not redactor:
        return outcome
    return _RunOutcome(
        status=outcome.status,
        outputs=redactor.value(outcome.outputs),
        error=redactor.text(outcome.error) if outcome.error else outcome.error,
        reason=redactor.text(outcome.reason) if outcome.reason else outcome.reason,
        errors=[redactor.text(message) for message in outcome.errors],
        log_tail=outcome.log_tail,
    )


# -- the subprocess boundary ---------------------------------------------------


class _TailBuffer:
    """Keeps only the last `max_chars` characters written to it, without
    holding the full (potentially unbounded) stream in memory first.

    `guard` extra characters are kept in front of those, and dropped again
    by get() whenever the stream was longer than the buffer: the cut can
    land in the middle of a secret, and a secret's tail on its own no
    longer matches the redactor. As long as `guard` is at least the longest
    value being redacted, whatever partial secret the cut leaves at the
    front falls inside the guard and is discarded."""

    def __init__(self, max_chars: int, guard: int = 0) -> None:
        self._max = max_chars
        self._guard = guard
        self._capacity = max_chars + guard
        self._chunks: list[str] = []
        self._total = 0
        self._cut = False

    def write(self, text: str) -> None:
        if not text:
            return
        self._chunks.append(text)
        self._total += len(text)
        while self._total > self._capacity and len(self._chunks) > 1:
            self._total -= len(self._chunks.pop(0))
            self._cut = True

    def get(self) -> str:
        text = "".join(self._chunks)
        if self._cut or len(text) > self._capacity:
            return text[-self._capacity :][self._guard :]
        return text


def _drain_text(stream, tail: _TailBuffer) -> None:
    for chunk in iter(lambda: stream.read(_READ_CHUNK), ""):
        tail.write(chunk)


class _LaunchError(RuntimeError):
    """The child process for a setup/task could not be started."""


@dataclass
class _EventStream:
    """What _drain_events() read off a task's private output fd."""

    events: list[dict] = field(default_factory=list)
    malformed: int = 0
    overflowed: bool = False


def _drain_events(fd: int, stream: _EventStream) -> None:
    """Reads the private output fd to EOF, one JSON event per line.

    The fd is written by task code, so nothing on it is trusted: a line
    that is not exactly one well-formed event (see _parse_event) is counted
    and dropped rather than raised -- one bad line must not lose every
    line that read cleanly, nor crash the host. At most
    MAX_EVENT_STREAM_BYTES are kept; past that the stream is still drained
    (so the writer never blocks) but discarded. Linear in the bytes read,
    however they are split into lines."""
    buf = bytearray()
    total = 0
    while True:
        try:
            chunk = os.read(fd, _READ_CHUNK)
        except OSError:
            break
        if not chunk:
            break
        if stream.overflowed:
            continue
        total += len(chunk)
        if total > MAX_EVENT_STREAM_BYTES:
            stream.overflowed = True
            buf.clear()
            continue
        scan_from = len(buf)
        buf += chunk
        start = 0
        newline = buf.find(b"\n", scan_from)
        while newline >= 0:
            _parse_event(bytes(buf[start:newline]), stream)
            start = newline + 1
            newline = buf.find(b"\n", start)
        del buf[:start]
    if buf.strip() and not stream.overflowed:
        stream.malformed += 1  # a last line with no newline: a write cut short


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not valid JSON")


def _parse_event(line: bytes, stream: _EventStream) -> None:
    """Appends `line` to stream.events if it is exactly one event of the
    wire format (_bundle_entrypoint.py's docstring): an object whose `op`
    is set/append (keys exactly op, name, value; name a string), skip
    (keys op and optionally reason, a string) or artifact (keys exactly op,
    name, path; both strings). Duplicate keys are refused
    -- rw_set/rw_append splice their value argument into the line
    unescaped, so a value like `1,"op":"skip"` would otherwise rewrite the
    event it sits in -- as are NaN/Infinity (not JSON; the result could not
    be posted) and values nested deeper than MAX_VALUE_DEPTH."""
    if not line.strip():
        return
    try:
        event = json.loads(
            line.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        stream.malformed += 1
        return
    if not _is_event(event) or _nested_deeper_than(event.get("value"), MAX_VALUE_DEPTH):
        stream.malformed += 1
        return
    stream.events.append(event)


def _is_event(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    op = event.get("op")
    if op in ("set", "append"):
        return event.keys() == {"op", "name", "value"} and isinstance(event["name"], str)
    if op == "skip":
        return event.keys() <= {"op", "reason"} and isinstance(event.get("reason", ""), str)
    if op == "missing_command":
        return event.keys() == {"op", "name"} and isinstance(event["name"], str)
    if op == "artifact":
        return event.keys() == {"op", "name", "path"} and all(
            isinstance(event[key], str) for key in ("name", "path")
        )
    return False


def _nested_deeper_than(value: Any, limit: int) -> bool:
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict | list):
            if depth > limit:
                return True
            children = node.values() if isinstance(node, dict) else node
            stack.extend((child, depth + 1) for child in children)
    return False


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _execute(
    *,
    files_dir: Path,
    file_path: str,
    language: str,
    resolved_inputs: dict[str, Any],
    capability: str,
    operation: str,
    scope_dir: Path,
    rw_sdk_dir: Path,
    secrets_dir: Path,
    credentials: dict[str, str],
    deadline_seconds: float,
    allow_anonymous: bool,
    kubeconfig_path: str | None,
    tail_guard: int,
    log: logging.Logger,
) -> tuple[int, _EventStream, str, bool]:
    """Runs one setup/task file as a child process in its own process group,
    with `deadline_seconds` enforced by SIGKILL-ing the whole group. Returns
    (exit code, what was read off the output fd, combined stdout+stderr
    tail -- up to 2 * LOG_TAIL_BYTES, unredacted -- whether the deadline was
    hit). Raises _LaunchError if the child could not be started at all."""
    base_env = {
        **_inherited_env(),
        "HOME": str(scope_dir / _HOME_DIR),
        "TMPDIR": str(scope_dir / _TMP_DIR),
        "RW_WORKDIR": str(scope_dir),
        # Not inherited (every RW_* is stripped): set here, for bash and Python alike.
        "RW_ARTIFACTS_DIR": str(scope_dir / _ARTIFACTS_DIR),
        "RW_CAPABILITY": capability,
        "RW_OPERATION": operation,
    }
    if kubeconfig_path:
        base_env["KUBECONFIG"] = kubeconfig_path

    if language == "bash":
        argv = ["bash", str(files_dir / file_path)]
        env = {
            **base_env,
            "RW_SDK": str(rw_sdk_dir),
            # command_not_found_handle (E_COMMAND_NOT_FOUND), read before the task runs.
            "BASH_ENV": str(rw_sdk_dir / "bash_env.sh"),
        }
        for name, value in resolved_inputs.items():
            text = value if isinstance(value, str) else json.dumps(value)
            # The legacy (pre-0.3.0) spelling too, for bundles published against it;
            # never a reserved variable, and the current name wins any clash.
            legacy = legacy_input_env_name(name)
            if not is_reserved_env_name(legacy):
                env.setdefault(legacy, text)
            if not is_reserved_env_name(input_env_name(name)):
                env[input_env_name(name)] = text
    else:
        python_kwargs = {python_kwarg_name(name): value for name, value in resolved_inputs.items()}
        # -P: the working directory (the writable scope) is not put on
        # sys.path, so nothing a task writes there can shadow this entry
        # point's own imports. -s: no per-user site-packages.
        argv = [sys.executable, "-P", "-s", "-m", "runwhen_capability._bundle_entrypoint"]
        # Credentials travel as a 0600 file the entry point reads and
        # deletes -- never in the environment, where every process the task
        # starts would inherit them and /proc/<pid>/environ would show them.
        credentials_file = _write_private_file(
            secrets_dir, ".credentials.json", json.dumps(credentials)
        )
        env = {
            **base_env,
            "RW_TASK_FILE": str(files_dir / file_path),
            "RW_INPUTS_JSON": json.dumps(python_kwargs),
            "RW_CREDENTIALS_FILE": str(credentials_file),
            "RW_ALLOW_ANONYMOUS": "1" if allow_anonymous else "0",
        }

    read_fd, write_fd = os.pipe()
    env["RW_OUTPUT_FD"] = str(write_fd)
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is this module's own construction
            argv,
            cwd=str(scope_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            pass_fds=(write_fd,),
            # proc.pid is also the process group id -- see _kill_process_group.
            start_new_session=True,
        )
    except OSError as exc:
        # E.g. E2BIG: an input value too large for the environment. This
        # task fails; the rest of the request still runs.
        os.close(read_fd)
        raise _LaunchError(f"could not start {file_path}: {exc}") from exc
    finally:
        os.close(write_fd)  # the parent's copy; the child keeps its own via pass_fds

    stream = _EventStream()
    stdout_tail = _TailBuffer(LOG_TAIL_BYTES, guard=tail_guard)
    stderr_tail = _TailBuffer(LOG_TAIL_BYTES, guard=tail_guard)
    events_thread = threading.Thread(target=_drain_events, args=(read_fd, stream), daemon=True)
    stdout_thread = threading.Thread(
        target=_drain_text, args=(proc.stdout, stdout_tail), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_drain_text, args=(proc.stderr, stderr_tail), daemon=True
    )
    events_thread.start()
    stdout_thread.start()
    stderr_thread.start()

    timed_out = False
    try:
        proc.wait(timeout=deadline_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        log.warning(
            "%s: reached the request's deadline after %.1fs; killing its process group",
            file_path,
            deadline_seconds,
        )
    # Kill the group on EVERY exit, not only on a timeout: a task's
    # background children (`cmd &`, a daemon it started) stay in its
    # process group after the main process returns, and would otherwise
    # outlive the task on this pod -- still holding its pipes, and able to
    # read whatever the next request writes under the same user.
    _kill_process_group(proc)
    proc.wait()

    join_deadline = time.monotonic() + _DRAIN_JOIN_TIMEOUT
    for thread in (events_thread, stdout_thread, stderr_thread):
        thread.join(timeout=max(0.0, join_deadline - time.monotonic()))
    if events_thread.is_alive():
        # Something outside the process group (it called setsid) still holds
        # the output channel open. Leave read_fd open: the drain thread is
        # still reading it, and closing it would let the next os.pipe() reuse
        # the same fd number under that thread -- which would then consume
        # the NEXT task's output events.
        log.warning(
            "%s: a process that left its process group still holds the output channel", file_path
        )
    else:
        os.close(read_fd)
    if not stdout_thread.is_alive():
        proc.stdout.close()
    if not stderr_thread.is_alive():
        proc.stderr.close()

    # Not cut to LOG_TAIL_BYTES here: the caller redacts it first, then cuts.
    log_tail = stdout_tail.get() + stderr_tail.get()
    snapshot = _EventStream(list(stream.events), stream.malformed, stream.overflowed)
    return proc.returncode, snapshot, log_tail, timed_out


# -- bundle materialisation -----------------------------------------------------


# Host-side variables a task has no business seeing: where the relay is, which
# pool this executor serves, and where its bearer token is mounted.
_HOST_ONLY_ENV = frozenset({"EXECUTOR_TOKEN_FILE", "RELAY_URL", "POOL_ID"})


def _inherited_env() -> dict[str, str]:
    """The host's environment, minus what a task must not inherit: the
    host-only variables above, any RW_* variable (the host sets its own for
    each child), and XDG_* base directories (so they fall back to the
    per-request HOME instead of a directory every run shares)."""
    return {
        key: value
        for key, value in os.environ.items()
        if key not in _HOST_ONLY_ENV and not key.startswith(("RW_", "XDG_"))
    }


_HOME_DIR = ".rw-home"
_TMP_DIR = ".rw-tmp"
_ARTIFACTS_DIR = "artifacts"


def _prepare_scope(scope_dir: Path) -> None:
    """Gives the request a fresh, private HOME and TMPDIR inside its scope,
    so anything a task writes there -- dotfiles, tool config, caches, temp
    files -- is wiped with the scope rather than left for the next request
    on this pod to pick up; and an empty artifacts directory (see below)."""
    scope_dir.mkdir(parents=True, exist_ok=True)
    for name in (_HOME_DIR, _TMP_DIR):
        _fresh_dir(scope_dir / name)
    # The one place a directory that is not ours can be in the way: `rwtask run
    # --workdir` uses the user's own directory as the scope, and `artifacts/` is a
    # likely name in it. Refused, with nothing deleted, unless it is empty or carries
    # our marker (an earlier run of this host).
    _fresh_dir(
        scope_dir / _ARTIFACTS_DIR, marker=_ARTIFACTS_MARKER, what="an artifacts directory of ours"
    )
    _write_private_file(scope_dir / _ARTIFACTS_DIR, _ARTIFACTS_MARKER, "")


def _stamp_artifacts_dir(scope_dir: Path) -> None:
    """Writes the marker into `<scope>/artifacts` if it is a directory without it.
    Never through a symlink (a task can make `artifacts` one) and never an error:
    a missing directory has nothing to protect."""
    try:
        dir_fd = os.open(scope_dir / _ARTIFACTS_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        os.close(os.open(_ARTIFACTS_MARKER, flags, 0o600, dir_fd=dir_fd))
    except OSError:
        pass  # already there
    finally:
        os.close(dir_fd)


def _reset_artifacts_dir(scope_dir: Path) -> None:
    """A fresh, empty `$RW_ARTIFACTS_DIR`, with the marker, for the next task:
    whatever is there now is this request's own (a task may have replaced it)."""
    _fresh_dir(scope_dir / _ARTIFACTS_DIR)
    _write_private_file(scope_dir / _ARTIFACTS_DIR, _ARTIFACTS_MARKER, "")


def _fresh_dir(path: Path, marker: str | None = None, what: str = "a bundle directory") -> None:
    """An empty 0700 directory at `path`, replacing whatever was there -- a
    reused `rwtask run --workdir` may hold an earlier run's tree, symlinks
    included (a symlink is removed, never followed). With `marker`, an
    existing non-empty directory is only replaced if it contains that file,
    i.e. it is one this module wrote; anything else is refused rather than
    deleted."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        if marker is not None and any(path.iterdir()) and not (path / marker).is_file():
            raise ValueError(f"refusing to replace {path}: it is not {what}")
        shutil.rmtree(path)
    path.mkdir(mode=0o700)


def _write_private_file(directory: Path, name: str, content: str) -> Path:
    """Creates `directory/name` as a new 0600 file -- never following a
    symlink or reusing an existing file, and never briefly readable by
    anyone else, as a write-then-chmod would be."""
    if name in ("", ".", "..") or "/" in name or "\x00" in name:
        raise ValueError(f"unsafe file name {name!r}")
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def _materialize(files_dir: Path, files: dict[str, str]) -> None:
    """Writes `files` under a fresh `files_dir`. Every path already passed
    validate()'s is_allowed_path (compile_manifest() runs first); it is
    checked again here because this is the one place a bad path would
    turn into a write outside the scope."""
    _fresh_dir(files_dir, marker="capability.yaml")
    for path, content in files.items():
        if not is_allowed_path(path):
            raise ValueError(f"refusing to write {path!r}")
        *parents, name = path.split("/")
        directory = files_dir.joinpath(*parents)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_private_file(directory, name, content)
