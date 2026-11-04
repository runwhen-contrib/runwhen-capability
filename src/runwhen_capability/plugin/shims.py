"""PATH shims that record and replay the CLI calls a task makes (`rwtask test`, D43).

    with ShimSession.record(DEFAULT_SHIMS) as session:       # once, against the real tools
        execute(spec, ..., path_prepend=session.path_prepend, shim_env=session.env)
        calls = session.recorded()          # [{argv, rc, stdout, stderr, stdin?}, ...]

    with ShimSession.replay(calls, DEFAULT_SHIMS) as session:    # in CI, no tools needed
        execute(spec, ..., path_prepend=session.path_prepend, shim_env=session.env)
        session.mismatches(); session.unconsumed()

A session is a private temporary directory of tiny executables, one per tool, that goes
FIRST on the task's PATH (execute's `path_prepend`). Each is `#!/bin/sh` and
`exec`s `sys.executable -I -S` on one stdlib-only Python script (`.rw-shim.py`) beside
it, so no shim depends on what the host's PATH holds.

Record: the shim finds the real tool on the remaining PATH (never itself), runs it with
the same argv and stdin, appends `{argv, stdin?, rc, stdout, stderr}` (argv[0] is the
tool's basename; `stdin` only when something was piped in) as one JSON line to
`$RW_SHIM_LOG`, and passes stdout, stderr and the exit status through. Output is captured
whole, then written: a tool that streams or needs a terminal is not what these shims are
for. A tool that is not installed is not shimmed in record mode (the task sees what it
would see anyway: `command not found`); `session.missing` lists them.

Replay: the shim reads the expected calls (`$RW_SHIM_CALLS`, JSON), takes the next index
from the counter file `$RW_SHIM_STATE`, and compares argv exactly (and stdin, when the
recorded call has one). On a match it writes the recorded stdout and stderr and exits
with the recorded rc. On a mismatch, or when the task makes more calls than were
recorded, it writes `E_FIXTURE_MISMATCH: expected <argv> got <argv>` to stderr, appends
the mismatch to `$RW_SHIM_MISMATCH` for the host, consumes nothing and exits 97. After
the run the host reads `mismatches()` and `unconsumed()` (calls recorded but never made).

A scenario's calls are not only CLI calls: `ctx.k8s_proxy_get` / `rw_k8s_proxy_get`
(k8s_proxy.py) are calls of their own, `{type: k8s-proxy-get, request, response | error}`.
They share this session's files: under `RW_SHIM_MODE=record` the helper appends its line to
`$RW_SHIM_LOG` after the real call (so the log keeps the order of mixed calls); under
`replay` it takes the next index from `$RW_SHIM_STATE` the same way a shim does, requires a
`k8s-proxy-get` with an equal request (else `E_FIXTURE_MISMATCH`, exit 97, an entry in
`$RW_SHIM_MISMATCH` whose `expected` is a description instead of an argv), and starts no
proxy. A shim that finds a proxy call where it expected its own is the same mismatch.

Calls are sequential in v1: the counter is taken under a file lock, but which of two
concurrent calls gets which recorded answer is not defined. A tool invoked by absolute
path, or by a task that replaces PATH or runs with `inherit_env=False`, is not shimmed;
the shims are a test aid, not a sandbox. The shim directory and its log, counter and
mismatch files are outside the task's scope directory, but owned by the same user as the
task, so a hostile task could rewrite them; the host trusts them only as far as it trusts
the task it is testing.

`RW_SHIM_*` names are reserved (RW_*) and reach the child only through `execute`'s
`shim_env`, never through `extra_env`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path

#: What `rwtask test` shims by default. Never jq, yq, python, coreutils or bash: they are
#: deterministic parts of the task's own logic, not the outside world.
DEFAULT_SHIMS: tuple[str, ...] = (
    "kubectl",
    "helm",
    "flux",
    "argocd",
    "git",
    "psql",
    "redis-cli",
    "curl",
    "az",
    "gcloud",
    "aws",
)

#: The exit status of a shim that found a mismatch.
MISMATCH_EXIT = 97

SHIM_ENV_NAMES = (
    "RW_SHIM_MODE",
    "RW_SHIM_LOG",
    "RW_SHIM_CALLS",
    "RW_SHIM_STATE",
    "RW_SHIM_MISMATCH",
)

_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

_SHIM_PY = r"""
import fcntl
import json
import os
import shlex
import subprocess
import sys

MISMATCH_EXIT = 97


def append_line(var, obj):
    path = os.environ.get(var)
    if not path:
        return
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(obj) + "\n").encode())
    finally:
        os.close(fd)


def real_tool(tool):
    own = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory or os.path.realpath(directory) == own:
            continue
        candidate = os.path.join(directory, tool)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def read_stdin():
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return b""
        return sys.stdin.buffer.read()
    except (OSError, ValueError):
        return b""


def finish(rc, out, err):
    for stream, data in ((sys.stdout, out), (sys.stderr, err)):
        if data:
            stream.buffer.write(data)
        stream.buffer.flush()
    sys.exit(rc & 0xFF)


def record(argv, stdin):
    tool, args = argv[0], argv[1:]
    real = real_tool(tool)
    if real is None:
        rc, out, err = 127, b"", ("%s: command not found\n" % tool).encode()
    else:
        done = subprocess.run(
            [real] + args,
            input=stdin if stdin else None,
            stdin=None if stdin else subprocess.DEVNULL,
            capture_output=True,
        )
        rc = done.returncode if done.returncode >= 0 else 128 - done.returncode
        out, err = done.stdout, done.stderr
    call = {
        "argv": argv,
        "rc": rc,
        "stdout": out.decode("utf-8", "replace"),
        "stderr": err.decode("utf-8", "replace"),
    }
    if stdin:
        call["stdin"] = stdin.decode("utf-8", "replace")
    append_line("RW_SHIM_LOG", call)
    finish(rc, out, err)


def describe(call):
    # a recorded call that is not a CLI call: k8s-proxy-get (runwhen_capability.k8s_proxy)
    request = call.get("request") or {}
    return "%s %s %s/%s/%s:%s %s" % (
        call.get("type"),
        request.get("scheme") or "http",
        request.get("namespace"),
        request.get("kind"),
        request.get("name"),
        request.get("port"),
        request.get("path"),
    )


def replay(argv, stdin):
    with open(os.environ["RW_SHIM_CALLS"], encoding="utf-8") as handle:
        calls = json.load(handle)
    with open(os.environ["RW_SHIM_STATE"], "a+", encoding="utf-8") as state:
        fcntl.flock(state, fcntl.LOCK_EX)
        state.seek(0)
        index = int(state.read() or 0)
        expected = calls[index] if index < len(calls) else None
        stdin_text = stdin.decode("utf-8", "replace")
        problem = None
        if expected is None:
            problem = "expected no further call got " + shlex.join(argv)
        elif expected.get("type") is not None:
            problem = "expected %s got %s" % (describe(expected), shlex.join(argv))
        elif expected["argv"] != argv:
            problem = "expected %s got %s" % (shlex.join(expected["argv"]), shlex.join(argv))
        elif expected.get("stdin") is not None and expected["stdin"] != stdin_text:
            problem = "expected stdin %r got %r for %s" % (
                expected["stdin"],
                stdin_text,
                shlex.join(argv),
            )
        if problem is None:
            state.seek(0)
            state.truncate()
            state.write(str(index + 1))
    if problem is not None:
        append_line(
            "RW_SHIM_MISMATCH",
            {
                "index": index,
                "expected": (expected["argv"] if "argv" in expected else describe(expected))
                if expected
                else None,
                "got": argv,
                "message": problem,
            },
        )
        finish(MISMATCH_EXIT, b"", ("E_FIXTURE_MISMATCH: " + problem + "\n").encode())
    finish(
        int(expected.get("rc", 0)),
        expected.get("stdout", "").encode("utf-8"),
        expected.get("stderr", "").encode("utf-8"),
    )


def main():
    argv = sys.argv[1:]
    stdin = read_stdin()
    if os.environ.get("RW_SHIM_MODE") == "record":
        record(argv, stdin)
    elif os.environ.get("RW_SHIM_MODE") == "replay":
        replay(argv, stdin)
    finish(
        MISMATCH_EXIT, b"", b"E_FIXTURE_MISMATCH: the shim has no mode (RW_SHIM_MODE unset)\n"
    )


main()
"""


def check_tool_name(name: str) -> None:
    """ValueError unless `name` is a plain file name a shim can be called."""
    if not _TOOL_NAME.match(name):
        raise ValueError(f"not a usable tool name: {name!r}")


class ShimSession:
    """A shim directory plus the files the shims and the host share; see the module
    docstring. Use as a context manager: leaving removes the directory."""

    def __init__(
        self,
        mode: str,
        tools: Iterable[str],
        *,
        calls: Sequence[dict] | None = None,
        path: str | None = None,
    ) -> None:
        names = list(dict.fromkeys(tools))
        for name in names:
            check_tool_name(name)
        self.mode = mode
        self.missing: list[str] = []
        self._calls = [dict(c) for c in calls or []]
        search = os.environ.get("PATH", os.defpath) if path is None else path
        if mode == "record":
            present = []
            for name in names:
                if shutil.which(name, path=search):
                    present.append(name)
                else:
                    self.missing.append(name)
            names = present
        self.tools = names
        self._dir = Path(tempfile.mkdtemp(prefix="rwtask-shims-"))
        try:
            self._build(names)
        except BaseException:
            shutil.rmtree(self._dir, ignore_errors=True)
            raise

    @classmethod
    def record(cls, tools: Iterable[str], *, path: str | None = None) -> ShimSession:
        """Shims that run the real tools found on `path` (default: $PATH) and log them."""
        return cls("record", tools, path=path)

    @classmethod
    def replay(cls, calls: Sequence[dict], tools: Iterable[str]) -> ShimSession:
        """Shims that answer from `calls` (`{argv, rc?, stdout?, stderr?, stdin?}`)."""
        return cls("replay", tools, calls=calls)

    def _build(self, names: list[str]) -> None:
        shim_py = self._dir / ".rw-shim.py"
        shim_py.write_text(_SHIM_PY, encoding="utf-8")
        launcher = f"exec {shlex.quote(sys.executable)} -I -S {shlex.quote(str(shim_py))}"
        for name in names:
            script = self._dir / name
            script.write_text(f'#!/bin/sh\n{launcher} {shlex.quote(name)} "$@"\n')
            script.chmod(0o755)
        self._log = self._dir / ".log.jsonl"
        self._mismatch = self._dir / ".mismatch.jsonl"
        self._state = self._dir / ".state"
        calls_file = self._dir / ".calls.json"
        self.env: dict[str, str] = {"RW_SHIM_MODE": self.mode}
        if self.mode == "record":
            self.env["RW_SHIM_LOG"] = str(self._log)
        else:
            calls_file.write_text(json.dumps(self._calls), encoding="utf-8")
            self._state.write_text("0")
            self.env.update(
                RW_SHIM_CALLS=str(calls_file),
                RW_SHIM_STATE=str(self._state),
                RW_SHIM_MISMATCH=str(self._mismatch),
            )
        self.path_prepend: list[str] = [str(self._dir)]

    @staticmethod
    def _lines(path: Path) -> list[dict]:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def recorded(self) -> list[dict]:
        """The calls the real tools served, in the order they were logged."""
        return self._lines(self._log)

    def mismatches(self) -> list[dict]:
        """`{index, expected (argv or None), got, message}` for every refused call."""
        return self._lines(self._mismatch)

    def unconsumed(self) -> int:
        """How many replayed calls the task never made."""
        try:
            used = int(self._state.read_text() or 0)
        except (FileNotFoundError, ValueError):
            used = 0
        return max(0, len(self._calls) - used)

    def close(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)

    def __enter__(self) -> ShimSession:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


__all__ = ["DEFAULT_SHIMS", "MISMATCH_EXIT", "SHIM_ENV_NAMES", "ShimSession", "check_tool_name"]
