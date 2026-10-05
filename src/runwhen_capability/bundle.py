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
  `rw_set`/`rw_skip`, sourced from `$RW_SDK/rw.sh` (_rw_sh.py).
- Python: `_bundle_entrypoint.py` calls `main(ctx, **inputs)` with inputs as
  keyword arguments (snake_case, matching every other Python task in this
  SDK) and writes the returned dict onto the SAME private pipe, one `set`
  event per key -- the two languages share one wire format and one collector
  here.

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

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema
import referencing

from ._redaction import Redactor
from ._rw_sh import BASH_ENV_SH, RW_SH
from .custom.compiler import CompileError, compile_manifest
from .custom.diagnostics import (
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
from .models import BundleRequestEnvelope, ResultEnvelope, SetupResult, TaskResult

LOG_TAIL_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
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
) -> ResultEnvelope:
    """Runs `request`'s bundle: setup, then each requested task.

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
            allow_anonymous=allow_anonymous_credentials,
            redactor=redactor,
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
            allow_anonymous=allow_anonymous_credentials,
            redactor=redactor,
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


def _run_setup_or_task(
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
    allow_anonymous: bool,
    redactor: Redactor,
    log: logging.Logger,
) -> _RunOutcome:
    """Runs one setup/task file. Outputs, errors and the skip reason come
    back unredacted -- run_bundle_request() redacts the whole outcome --
    except the log tail, which has to be redacted before it is cut to size
    (see _execute)."""
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
        # set/append/skip, name a string, reason a string.
        outputs: dict[str, Any] = {}
        for event in events:
            if event["op"] == "set":
                outputs[event["name"]] = event["value"]
            elif event["op"] == "append":
                bucket = outputs.setdefault(event["name"], [])
                if isinstance(bucket, list):
                    bucket.append(event["value"])

        if declared_outputs is not None:
            # A name the source produced but never declared already failed
            # validate() at authoring time (E_OUTPUT_UNDECLARED) -- at runtime
            # it is simply dropped rather than failing an otherwise-good result.
            outputs = {name: value for name, value in outputs.items() if name in declared_outputs}

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
                "well-formed rw_set/rw_append/rw_skip event and were dropped; "
                "an output was lost"
            )

        if declared_outputs is not None:
            for name, value in outputs.items():
                errors.extend(
                    _validate_output_schema(name, value, declared_outputs[name]["schema"])
                )

        outputs, size_errors = _apply_size_caps(outputs)
        errors.extend(size_errors)

        # A list output cut to fit its size cap is noted in `errors` but does
        # NOT fail the task -- only a non-list output/whole result still over
        # the cap (E_OUTPUT_TOO_LARGE) does, same as an exit code, a schema
        # violation, a refused schema or a dropped output line
        # (E_OUTPUT_MALFORMED). Notes (truncation) carry no E_ prefix, so this
        # is exactly "every error EXCEPT a plain note".
        failing = (
            E_OUTPUT_SCHEMA,
            E_OUTPUT_TOO_LARGE,
            E_OUTPUT_MALFORMED,
            E_COMMAND_NOT_FOUND,
            E_SCHEMA_FEATURE,
        )
        failed = exit_code != 0 or any(msg.startswith(failing) for msg in errors)
        if failed:
            return _RunOutcome(
                status="failed",
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
    return len(json.dumps(value).encode("utf-8"))


def _truncate_list_to_budget(value: list, budget: int) -> list:
    """The largest prefix of `value` whose JSON encoding fits `budget`
    bytes, computed in one O(n) pass -- each element is encoded exactly
    once, not the whole (shrinking) list once per dropped element. Exact,
    not approximate: `json.dumps` with its default separators renders a
    list as "[" + ", ".join(dumps(item) for item in list) + "]", so summing
    each item's own encoded length plus its ", " separator reproduces the
    real total byte-for-byte."""
    kept: list = []
    total = 2  # the array's own "[" + "]"
    for item in value:
        extra = len(json.dumps(item).encode("utf-8")) + (2 if kept else 0)  # ", " between items
        if total + extra > budget:
            break
        total += extra
        kept.append(item)
    return kept


def _apply_size_caps(outputs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    capped: dict[str, Any] = {}
    for name, value in outputs.items():
        size = _json_size(value)
        if size <= MAX_OUTPUT_BYTES:
            capped[name] = value
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
        return {}, errors
    return capped, errors


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
    is set/append (keys exactly op, name, value; name a string) or skip
    (keys op and optionally reason, a string). Duplicate keys are refused
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
            # The legacy (pre-H48) spelling too, for bundles published against it;
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


def _prepare_scope(scope_dir: Path) -> None:
    """Gives the request a fresh, private HOME and TMPDIR inside its scope,
    so anything a task writes there -- dotfiles, tool config, caches, temp
    files -- is wiped with the scope rather than left for the next request
    on this pod to pick up."""
    scope_dir.mkdir(parents=True, exist_ok=True)
    for name in (_HOME_DIR, _TMP_DIR):
        _fresh_dir(scope_dir / name)


def _fresh_dir(path: Path, marker: str | None = None) -> None:
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
            raise ValueError(f"refusing to replace {path}: it is not a bundle directory")
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
