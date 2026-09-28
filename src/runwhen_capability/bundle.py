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
path (the raw value is written to `<scope>/secrets/<name>`, 0600) -- never
inlined, in either language. A `credential` of kind `k8s.kubeconfig` also
sets `KUBECONFIG` to that same path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from ._rw_sh import RW_SH
from .custom.compiler import CompileError, compile_manifest
from .custom.diagnostics import E_INPUT_TYPE, E_OUTPUT_SCHEMA, E_OUTPUT_TOO_LARGE, E_TIMEOUT
from .custom.hashing import content_hash
from .custom.manifest import Manifest, input_env_name, python_kwarg_name, task_file_language
from .models import BundleRequestEnvelope, ResultEnvelope, SetupResult, TaskResult

LOG_TAIL_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_RESULT_BYTES = 1024 * 1024
DEFAULT_TASK_DEADLINE = 300  # seconds; used when the caller gives no deadline
_DRAIN_JOIN_TIMEOUT = 5
_READ_CHUNK = 65536
_REDACTED = "***REDACTED***"

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
    deadline_seconds: float = DEFAULT_TASK_DEADLINE,
    allow_anonymous_credentials: bool = False,
) -> ResultEnvelope:
    log = log or logging.getLogger("runwhen_capability.bundle")
    scope_dir = Path(scope_dir)
    result = ResultEnvelope()

    files = {f.path: f.content for f in request.bundle.files}
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
        compiled = compile_manifest(files)
    except CompileError as exc:
        result.setup = SetupResult(status="failed", error=f"invalid bundle: {exc}")
        return result

    manifest = Manifest.model_validate(yaml.safe_load(files["capability.yaml"]))
    compiled_tasks = {t["name"]: t for t in compiled["tasks"]}

    files_dir = scope_dir / "bundle"
    _materialize(files_dir, files)
    rw_sdk_dir = scope_dir / ".rw-sdk"
    _write_rw_sdk(rw_sdk_dir)

    target = request.target.model_dump(mode="json") if request.target else None

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
            deadline_seconds=deadline_seconds,
            allow_anonymous=allow_anonymous_credentials,
            log=log.getChild("setup"),
        )
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
            deadline_seconds=deadline_seconds,
            allow_anonymous=allow_anonymous_credentials,
            log=log.getChild(task_name),
        )
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
    log: logging.Logger,
) -> _RunOutcome:
    language = task_file_language(file_path)
    if language is None:
        return _RunOutcome(status="failed", error=f"{file_path}: unsupported file type")

    resolved, type_errors = _resolve_inputs(
        input_specs, provided_inputs, target, credentials, scope_dir, allow_anonymous
    )
    secrets = [v for v in credentials.values() if v]
    if type_errors:
        return _RunOutcome(status="failed", error="; ".join(type_errors), errors=type_errors)

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

    exit_code, events, log_tail, timed_out = _execute(
        files_dir=files_dir,
        file_path=file_path,
        language=language,
        resolved_inputs=resolved,
        capability=capability,
        operation=operation,
        scope_dir=scope_dir,
        rw_sdk_dir=rw_sdk_dir,
        credentials=credentials,
        deadline_seconds=deadline_seconds,
        allow_anonymous=allow_anonymous,
        kubeconfig_path=kubeconfig_path,
        log=log,
    )
    log_tail = _redact_text(log_tail, secrets) if log_tail else log_tail

    if timed_out:
        return _RunOutcome(
            status="timeout",
            error=f"{E_TIMEOUT}: exceeded the {deadline_seconds}s deadline",
            errors=[f"{E_TIMEOUT}: exceeded the {deadline_seconds}s deadline"],
            log_tail=log_tail,
        )

    skip_event = next((e for e in events if e.get("op") == "skip"), None)
    if skip_event is not None:
        reason = skip_event.get("reason") or ""
        if is_setup:
            # "Only tasks can skip" -- a setup that skips has not
            # materialised anything, exactly like a packaged capability's
            # setup raising SkipTask (host.py, SkipTask's own docstring).
            return _RunOutcome(status="failed", error=reason or "setup skipped", log_tail=log_tail)
        return _RunOutcome(status="skipped", reason=reason, log_tail=log_tail)

    outputs: dict[str, Any] = {}
    for event in events:
        op, name = event.get("op"), event.get("name")
        if op == "set" and name is not None:
            outputs[name] = event.get("value")
        elif op == "append" and name is not None:
            bucket = outputs.setdefault(name, [])
            if isinstance(bucket, list):
                bucket.append(event.get("value"))

    if declared_outputs is not None:
        # A name the source produced but never declared already failed
        # validate() at authoring time (E_OUTPUT_UNDECLARED) -- at runtime
        # it is simply dropped rather than failing an otherwise-good result.
        outputs = {name: value for name, value in outputs.items() if name in declared_outputs}

    errors: list[str] = []
    if exit_code != 0:
        errors.append(f"process exited {exit_code}")

    if declared_outputs is not None:
        for name, value in outputs.items():
            errors.extend(_validate_output_schema(name, value, declared_outputs[name]["schema"]))

    outputs, size_errors = _apply_size_caps(outputs)
    errors.extend(size_errors)

    outputs = _redact_value(outputs, secrets)

    # A list output cut to fit its size cap is noted in `errors` but does
    # NOT fail the task -- only a non-list output/whole result still over
    # the cap (E_OUTPUT_TOO_LARGE) does, same as an exit code or a schema
    # violation. Truncation notices carry neither prefix, so this is exactly
    # "every error EXCEPT a plain truncation notice".
    failed = exit_code != 0 or any(
        msg.startswith(E_OUTPUT_SCHEMA) or msg.startswith(E_OUTPUT_TOO_LARGE) for msg in errors
    )
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
    scope_dir: Path,
    allow_anonymous: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    resolved: dict[str, Any] = {}
    errors: list[str] = []
    secrets_dir = scope_dir / "secrets"

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
            secrets_dir.mkdir(parents=True, exist_ok=True)
            path = secrets_dir / name
            path.write_text(raw, encoding="utf-8")
            os.chmod(path, 0o600)
            resolved[name] = str(path)
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
    validator_cls = jsonschema.validators.validator_for(schema)
    validator = validator_cls(schema)
    messages = []
    for error in validator.iter_errors(value):
        json_path = "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path
        )
        messages.append(f"{E_OUTPUT_SCHEMA} outputs.{name}{json_path}: {error.message}")
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
        errors.append(
            f"{E_OUTPUT_TOO_LARGE}: result is {total} bytes, over the {MAX_RESULT_BYTES}-byte "
            "per-result limit"
        )
    return capped, errors


# -- redaction ------------------------------------------------------------------


def _redact_text(text: str, secrets: list[str]) -> str:
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, _REDACTED)
    return text


def _redact_value(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, dict):
        return {k: _redact_value(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v, secrets) for v in value]
    return value


# -- the subprocess boundary ---------------------------------------------------


class _TailBuffer:
    """Keeps only the last `max_chars` characters written to it, without
    holding the full (potentially unbounded) stream in memory first."""

    def __init__(self, max_chars: int) -> None:
        self._max = max_chars
        self._chunks: list[str] = []
        self._total = 0

    def write(self, text: str) -> None:
        if not text:
            return
        self._chunks.append(text)
        self._total += len(text)
        while self._total > self._max and len(self._chunks) > 1:
            self._total -= len(self._chunks.pop(0))

    def get(self) -> str:
        return "".join(self._chunks)[-self._max :]


def _drain_text(stream, tail: _TailBuffer) -> None:
    for chunk in iter(lambda: stream.read(_READ_CHUNK), ""):
        tail.write(chunk)


def _drain_events(fd: int, events: list[dict]) -> None:
    """Reads the private output-fd to EOF, parsing one JSON object per line.
    A malformed line (a task bug: `rw_append`/`rw_set` called with text that
    is not valid JSON) is dropped rather than raised -- one bad line must not
    lose every output line that read cleanly."""
    buf = b""
    while True:
        try:
            chunk = os.read(fd, _READ_CHUNK)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                events.append(json.loads(line.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue


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
    credentials: dict[str, str],
    deadline_seconds: float,
    allow_anonymous: bool,
    kubeconfig_path: str | None,
    log: logging.Logger,
) -> tuple[int, list[dict], str, bool]:
    """Runs one setup/task file as a child process in its own process group,
    with `deadline_seconds` enforced by SIGKILL-ing the whole group. Returns
    (exit code, parsed output-fd events, combined stdout+stderr tail, whether
    the deadline was hit)."""
    read_fd, write_fd = os.pipe()

    base_env = {
        **os.environ,
        "RW_WORKDIR": str(scope_dir),
        "RW_CAPABILITY": capability,
        "RW_OPERATION": operation,
        "RW_OUTPUT_FD": str(write_fd),
    }
    if kubeconfig_path:
        base_env["KUBECONFIG"] = kubeconfig_path

    if language == "bash":
        argv = ["bash", str(files_dir / file_path)]
        env = {**base_env, "RW_SDK": str(rw_sdk_dir)}
        for name, value in resolved_inputs.items():
            env[input_env_name(name)] = value if isinstance(value, str) else json.dumps(value)
    else:
        python_kwargs = {python_kwarg_name(name): value for name, value in resolved_inputs.items()}
        argv = [sys.executable, "-m", "runwhen_capability._bundle_entrypoint"]
        env = {
            **base_env,
            "RW_TASK_FILE": str(files_dir / file_path),
            "RW_INPUTS_JSON": json.dumps(python_kwargs),
            "RW_CREDENTIALS_JSON": json.dumps(credentials),
            "RW_ALLOW_ANONYMOUS": "1" if allow_anonymous else "0",
        }

    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is this module's own construction
            argv,
            cwd=str(scope_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            pass_fds=(write_fd,),
            # proc.pid is also the process group id -- see _kill_process_group.
            start_new_session=True,
        )
    finally:
        os.close(write_fd)  # the parent's copy; the child keeps its own via pass_fds

    events: list[dict] = []
    stdout_tail = _TailBuffer(LOG_TAIL_BYTES)
    stderr_tail = _TailBuffer(LOG_TAIL_BYTES)
    events_thread = threading.Thread(target=_drain_events, args=(read_fd, events), daemon=True)
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
            "%s: exceeded the %ss deadline; killing its process group", file_path, deadline_seconds
        )
        _kill_process_group(proc)
        proc.wait()

    events_thread.join(timeout=_DRAIN_JOIN_TIMEOUT)
    stdout_thread.join(timeout=_DRAIN_JOIN_TIMEOUT)
    stderr_thread.join(timeout=_DRAIN_JOIN_TIMEOUT)
    try:
        os.close(read_fd)
    except OSError:
        pass
    if not stdout_thread.is_alive():
        proc.stdout.close()
    if not stderr_thread.is_alive():
        proc.stderr.close()

    log_tail = (stdout_tail.get() + stderr_tail.get())[-LOG_TAIL_BYTES:]
    return proc.returncode, events, log_tail, timed_out


# -- bundle materialisation -----------------------------------------------------


def _materialize(files_dir: Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        dest = files_dir / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def _write_rw_sdk(rw_sdk_dir: Path) -> None:
    rw_sdk_dir.mkdir(parents=True, exist_ok=True)
    (rw_sdk_dir / "rw.sh").write_text(RW_SH, encoding="utf-8")
