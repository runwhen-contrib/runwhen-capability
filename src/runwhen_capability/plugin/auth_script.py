"""Run one script auth plan (CONTRACTS.md "Auth plan", D67, call F7) in the executor, before
the task script, and hand back what it produced.

    result = run_script_plan(
        plan,                          # lease credentials[].authPlan whose method is a `script`
        files=files, executable=executable,   # the integration plugin's archive, read
        reference=[("key.json", b"...")],     # materialized[].reference, decoded
        work_dir=task_dir / "auth-0",  # a fresh path under the task's 0700 secrets directory
        timeout_s=60.0,
        redactor=Redactor([...]),
    )
    result.env      # variables to export to the task: the file type's path, or the script's pairs
    result.secrets  # every produced secret value, for the envelope's redaction (not the
                    # env values of the names in the plan's materialize.nonSecret)

What the script sees is the frozen contract: `RW_AUTH_REF_DIR/<key>` (one file per reference
key, 0600 in a 0700 directory; the variable is set, to an empty directory, when there is no
reference material), `RW_AUTH_INPUT_<NAME>` for each plan input (camelCase to
SCREAMING_SNAKE; a value that is not a string as JSON), and where it writes: `RW_AUTH_OUT_FILE`
when the credential type materializes as a file (the plan's `materialize.file`), else
`RW_AUTH_OUT_ENV` (`NAME=value` lines; a value holds no newline), and `RW_AUTH_OUT_ERROR`
(one `{code, message}` JSON line, the code from the closed D64 set). The script runs with the
working directory of the integration plugin's root (the archive's root, where `integration/`
is), `.py` under `python3` and `.sh` under `bash -euo pipefail`, in bundle.py's child runner
discipline: the host's environment minus the host-only variables, every RW_* and XDG_*
variable (so the executor token is never visible to it), a private HOME and TMPDIR under
`work_dir`, its own process group killed on every exit, a deadline, stdin closed. Its stdout
and stderr are never task output: a short, redacted tail is kept for an error message only.

Everything under `work_dir` stays until the caller deletes it when the task ends (the
reference files and the produced file are read by the task, by design), so the caller owns
cleanup, success or failure.

A failure raises AuthScriptError: `invalid_input` (the plan or the reference breaks its
contract), `timeout` (the script ran past its deadline), the script's own code when it wrote
a valid `{code, message}`, else `internal` -- always with scope `auth`. The task script never
runs after one.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess
import threading
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .. import bundle
from .._redaction import Redactor
from ..contracts import schema
from ..custom.manifest import task_file_language
from .run import _env_field, _env_text, _materialize, check_extra_env

_log = logging.getLogger("runwhen_capability.plugin.auth_script")

#: The longest an auth script may run, whatever the request's deadline allows.
MAX_SECONDS = 60.0
#: Where a script's files are in the integration's archive.
INTEGRATION_DIR = "integration"

#: What a script may leave: a kubeconfig or key file is KBs; this is a sanity bound.
_MAX_OUTPUT_BYTES = 1024 * 1024
_ERROR_TAIL_CHARS = 1000
_TAIL_KEEP_CHARS = 8 * 1024
_SCOPE = "auth"

_ERROR_CODES = frozenset(schema("error")["properties"]["code"]["enum"])
_ENV_NAME = re.compile(schema("common")["$defs"]["envVarName"]["pattern"])
REFERENCE_KEY = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
_MODE = re.compile(r"^0[0-7]{3}$")


class AuthScriptError(Exception):
    """An auth plan that did not produce its credential; `code` is the envelope error code."""

    def __init__(self, code: str, message: str, scope: str = _SCOPE) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.scope = scope


@dataclass(frozen=True)
class AuthResult:
    #: variable -> value to export to the task (the file's path, or the script's pairs)
    env: dict[str, str]
    #: every secret value the script produced (file content, env values of the names the plan
    #: does not list in `materialize.nonSecret`) for the redaction list
    secrets: list[str] = field(default_factory=list)


def script_archive_path(rel: str) -> str:
    """The path in the integration's archive of a plan's `method.script.path`, which is
    relative to `integration/` (CONTRACTS.md "Integration"). AuthScriptError(invalid_input)
    for an absolute path, `..`, an empty part, a backslash or a NUL: nothing outside
    `integration/` is ever run."""
    posix = PurePosixPath(rel)
    if (
        not isinstance(rel, str)
        or not rel
        or posix.is_absolute()
        or "\\" in rel
        or "\x00" in rel
        or any(part in ("", ".", "..") for part in rel.split("/"))
    ):
        raise AuthScriptError(
            "invalid_input", f"auth script path {rel!r} is not a path inside integration/"
        )
    return f"{INTEGRATION_DIR}/{rel}"


def input_env_var(name: str) -> str:
    """`RW_AUTH_INPUT_<NAME>` for a plan input called `name`."""
    return f"RW_AUTH_INPUT_{_env_field(name)}"


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=False)
    return path


def _write_private(path: Path, data: bytes, mode: int) -> None:
    """A new file at `path` with exactly `mode`, never through a symlink or over a file."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)  # the umask must not decide who can read it
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _write_reference(ref_dir: Path, reference: Sequence[tuple[str, bytes]]) -> None:
    seen: set[str] = set()
    for key, data in reference:
        if not REFERENCE_KEY.match(key) or key in (".", ".."):
            raise AuthScriptError("invalid_input", f"reference key {key!r} is not a file name")
        if key in seen:
            raise AuthScriptError("invalid_input", f"reference key {key!r} is given twice")
        seen.add(key)
        _write_private(ref_dir / key, data, 0o600)


def _read_regular(path: Path, what: str) -> bytes | None:
    """The bytes of `path` if it is a regular file (never a symlink) of a sane size; None if
    it does not exist. AuthScriptError(internal) for anything else."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AuthScriptError(
            "internal", f"the auth script's {what} cannot be read: {exc}"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise AuthScriptError("internal", f"the auth script's {what} is not a regular file")
        if info.st_size > _MAX_OUTPUT_BYTES:
            raise AuthScriptError(
                "internal", f"the auth script's {what} is over {_MAX_OUTPUT_BYTES} bytes"
            )
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(_MAX_OUTPUT_BYTES + 1)
    finally:
        os.close(fd)


def _script_error(
    rc: int, error_file: Path, script: str, tail: str, redactor: Redactor
) -> AuthScriptError:
    """The AuthScriptError for a script that exited `rc` != 0: its own `{code, message}`
    when it wrote a valid one, else `internal` with what it printed last."""

    def clean(text: str) -> str:
        return redactor.text(text) if redactor else text

    problem = ""
    try:
        raw = _read_regular(error_file, "error file")
    except AuthScriptError as exc:
        raw, problem = None, exc.message
    if raw is not None:
        line = next((x for x in raw.decode("utf-8", "replace").splitlines() if x.strip()), "")
        try:
            doc = json.loads(line)
        except ValueError:
            doc = None
        message = doc.get("message") if isinstance(doc, dict) else None
        if isinstance(message, str) and 0 < len(message) <= 4096:
            code = doc.get("code")
            if isinstance(code, str) and code in _ERROR_CODES:
                return AuthScriptError(code, clean(message))
            problem = f"it reported the error code {str(doc.get('code'))[:40]!r}, not a known one"
            tail = message
        else:
            problem = "its error file is not one {code, message} JSON line"
    text = f"auth script {script} exited {rc}"
    if problem:
        text += f" ({problem})"
    if tail.strip():
        text += f": {clean(tail.strip()[-_ERROR_TAIL_CHARS:])}"
    return AuthScriptError("internal", text)


def _parse_env_lines(
    raw: bytes, script: str, allowed: Collection[str], taken: Collection[str]
) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise AuthScriptError(
            "internal", f"auth script {script}: RW_AUTH_OUT_ENV is not UTF-8"
        ) from None
    out: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        name, sep, value = line.partition("=")
        if not sep or not _ENV_NAME.match(name):
            raise AuthScriptError(
                "internal",
                f"auth script {script}: RW_AUTH_OUT_ENV line {number} is not NAME=value "
                "(NAME is A-Z, 0-9 and _)",
            )
        try:
            check_extra_env({name: value})
        except ValueError:
            raise AuthScriptError(
                "internal",
                f"auth script {script} may not export {name!r}: it is a reserved variable",
            ) from None
        if name not in allowed:
            raise AuthScriptError(
                "internal",
                f"auth script {script} exported {name!r}, which the plan's materialize.env "
                "does not list",
            )
        if name in out or name in taken:
            raise AuthScriptError("internal", f"auth script {script} sets {name!r} twice")
        out[name] = value
    if not out:
        raise AuthScriptError("internal", f"auth script {script} exported no variable")
    return out


def _run_child(
    argv: list[str], *, env: dict[str, str], cwd: Path, timeout_s: float, tail_guard: int
) -> tuple[int, str, bool]:
    """(exit code, combined stdout+stderr tail unredacted, timed out): bundle._execute's
    discipline for a child that has no event channel -- its own process group, killed on
    every exit (a daemon it started never outlives the script), the output drained by
    threads into bounded tails, and a deadline."""
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is this module's own construction
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        raise AuthScriptError("internal", f"could not start {argv[-1]}: {exc}") from exc
    tails = [bundle._TailBuffer(_TAIL_KEEP_CHARS, guard=tail_guard) for _ in range(2)]
    threads = [
        threading.Thread(target=bundle._drain_text, args=(stream, tail), daemon=True)
        for stream, tail in zip((proc.stdout, proc.stderr), tails, strict=True)
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
    bundle._kill_process_group(proc)
    proc.wait()
    join_deadline = time.monotonic() + bundle._DRAIN_JOIN_TIMEOUT
    for thread in threads:
        thread.join(timeout=max(0.0, join_deadline - time.monotonic()))
    for thread, stream in zip(threads, (proc.stdout, proc.stderr), strict=True):
        if not thread.is_alive():
            stream.close()
    return proc.returncode, "".join(tail.get() for tail in tails), timed_out


def run_script_plan(
    plan: Mapping[str, Any],
    *,
    files: Mapping[str, bytes],
    executable: frozenset[str],
    reference: Sequence[tuple[str, bytes]],
    work_dir: Path,
    timeout_s: float,
    redactor: Redactor,
    taken: Collection[str] = (),
    scratch_dir: Path | None = None,
    log: logging.Logger | None = None,
) -> AuthResult:
    """Runs the script of `plan` (an auth plan whose method is a `script`; `files` and
    `executable` are its archive, read) under `work_dir`, which must not exist yet. `taken`
    is the variables the task already has: producing one of them is an error. Raises
    AuthScriptError.

    `work_dir` holds this plan's SECRET material -- the reference files, the credential the
    script produces, its private HOME and TMPDIR -- and callers put it under
    `$RW_CREDENTIALS_DIR`, a small tmpfs: a kubeconfig is KBs, and it never touches disk.
    The plugin archive is not secret and is megabytes (the kubernetes plugin is ~570 files),
    so it must NOT be unpacked there -- that is an ENOSPC on the credentials tmpfs. Pass
    `scratch_dir`, on the task's writable workdir, and only the archive goes there."""
    log = log or _log
    rel = plan["method"]["script"]["path"]
    archive_path = script_archive_path(rel)
    if archive_path not in files:
        raise AuthScriptError("internal", f"the auth script archive has no {archive_path}")
    language = task_file_language(archive_path)
    if language is None:
        raise AuthScriptError("internal", f"{archive_path}: unsupported auth script type")
    materialize = plan["materialize"]
    file_target = materialize.get("file")
    allowed_env = tuple(materialize.get("env") or ())
    mode = 0o600
    if file_target is not None:
        spelled = file_target.get("mode", "0600")
        if not isinstance(spelled, str) or not _MODE.match(spelled):
            raise AuthScriptError("invalid_input", "materialize.file.mode must look like 0600")
        mode = int(spelled, 8)
        try:
            check_extra_env({file_target["env"]: ""})
        except ValueError:
            raise AuthScriptError(
                "internal", f"the auth plan may not set {file_target['env']!r}: it is reserved"
            ) from None
        if file_target["env"] in taken:
            raise AuthScriptError(
                "internal", f"the auth plan's {file_target['env']!r} is also set another way"
            )

    try:
        _private_dir(work_dir)
        # The archive is code, not secret: unpack it on the scratch root when one is
        # given, so a megabyte-scale plugin does not have to fit the credentials tmpfs.
        if scratch_dir is None:
            plugin_dir = work_dir / "plugin"
        else:
            scratch_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            plugin_dir = scratch_dir / "plugin"
        _materialize(plugin_dir, dict(files), executable)
        ref_dir = _private_dir(work_dir / "ref")
        out_dir = _private_dir(work_dir / "out")
        home_dir = _private_dir(work_dir / "home")
        tmp_dir = _private_dir(work_dir / "tmp")
        _write_reference(ref_dir, reference)
    except (OSError, ValueError) as exc:
        raise AuthScriptError("internal", f"could not set up the auth script: {exc}") from exc

    out_file, out_env, out_error = out_dir / "file", out_dir / "env", out_dir / "error"
    env = {
        key: value
        for key, value in bundle._inherited_env().items()
        # a shell or interpreter start-up hook of the host must not run in the script
        if key not in ("BASH_ENV", "ENV")
    }
    env.update(
        {
            "HOME": str(home_dir),
            "TMPDIR": str(tmp_dir),
            "RW_AUTH_REF_DIR": str(ref_dir),
            "RW_AUTH_OUT_ERROR": str(out_error),
        }
    )
    env["RW_AUTH_OUT_FILE" if file_target is not None else "RW_AUTH_OUT_ENV"] = str(
        out_file if file_target is not None else out_env
    )
    for name, value in (plan.get("inputs") or {}).items():
        env[input_env_var(name)] = _env_text(value)

    script_path = plugin_dir / archive_path
    argv = (
        ["bash", "-euo", "pipefail", str(script_path)]
        if language == "bash"
        else ["python3", str(script_path)]
    )
    rc, tail, timed_out = _run_child(
        argv, env=env, cwd=plugin_dir, timeout_s=timeout_s, tail_guard=redactor.longest
    )
    tail = redactor.text(tail) if redactor else tail
    log.debug("auth script %s exited %s%s", rel, rc, " (timed out)" if timed_out else "")
    if timed_out:
        raise AuthScriptError(
            "timeout", f"auth script {rel} did not finish in {timeout_s:.0f}s and was stopped"
        )
    if rc != 0:
        raise _script_error(rc, out_error, rel, tail, redactor)

    if file_target is not None:
        data = _read_regular(out_file, "output file")
        if not data:
            raise AuthScriptError(
                "internal",
                f"auth script {rel} exited 0 but wrote no credential to RW_AUTH_OUT_FILE",
            )
        try:
            os.chmod(out_file, mode)
        except OSError as exc:
            raise AuthScriptError(
                "internal", f"could not set the credential file's mode: {exc}"
            ) from exc
        try:
            secrets = [data.decode("utf-8")]
        except UnicodeDecodeError:
            secrets = []
        return AuthResult({file_target["env"]: str(out_file)}, [x for x in secrets if x])

    raw = _read_regular(out_env, "RW_AUTH_OUT_ENV file")
    if not raw:
        raise AuthScriptError(
            "internal", f"auth script {rel} exited 0 but wrote nothing to RW_AUTH_OUT_ENV"
        )
    pairs = _parse_env_lines(raw, rel, allowed_env, taken)
    non_secret = set(materialize.get("nonSecret") or ())
    return AuthResult(
        pairs, [value for name, value in pairs.items() if value and name not in non_secret]
    )
