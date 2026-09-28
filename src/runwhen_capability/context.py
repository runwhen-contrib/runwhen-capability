"""Context -- the SDK's runtime boundary. A task has no config: the task host
builds a Context from the request envelope, the resolved credentials, and the
request scope path, then calls the setup/task function. There is no config
file a task reads, no environment it inspects, no path it constructs.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from pathlib import Path

from .errors import CredentialNotFoundError, OutputTooLargeError, SkipTask
from .findings import FindingsClient
from .git import GitClient
from .repo_fs import RepoFsClient
from .sarif import SARIF_BYTE_BUDGET, SarifClient

DEFAULT_RUN_TIMEOUT = 600  # seconds

# A subprocess's stdout shares SARIF_BYTE_BUDGET (sarif.py) rather than
# defining its own: it is the same risk (an oversized blob held in memory
# before anything downstream can react), on the same size scale (SARIF text
# IS a tool's captured stdout, for every SARIF-emitting tool) -- see
# OutputTooLargeError's docstring (errors.py) for why one budget covers both.
#
# stderr gets its own, much smaller budget: it is diagnostic text (a
# capability typically folds it into one failure message), never the payload
# a task's findings come from, so there is no reason to let it grow anywhere
# near stdout's size before capping it.
# 1 MiB comfortably fits even a large stack trace or a verbose crash dump
# without asking a legitimate failure message to compete with the same
# OOM risk stdout has.
_STDERR_BYTE_BUDGET = 1024 * 1024  # 1 MiB

# Read size for both the stdout and stderr drain threads below. Arbitrary
# but conventional -- matches the common OS pipe buffer size, so a read
# rarely blocks waiting for more than one chunk's worth of data.
_RUN_READ_CHUNK = 65536

# How long a drain thread gets to notice a kill and finish reading whatever
# was already buffered, once the process itself is confirmed dead (this
# runs AFTER proc.wait() has already returned) -- not a bound on the tool's
# own runtime, which `timeout`/DEFAULT_RUN_TIMEOUT already governs. A few
# seconds is generous for draining bytes a dead process can no longer add
# to; see `run()`'s docstring for why this is only a belt-and-braces bound,
# not the primary fix for a grandchild holding a pipe open.
_DRAIN_JOIN_TIMEOUT = 5


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the whole process group `proc` leads (see `start_new_session=
    True` in run(), which makes `proc.pid` a process group id too), not
    just the direct child `proc.kill()` would reach. A tool that shells out
    to its own scanner engine (semgrep, checkov, trivy's embedded scanners)
    can leave a grandchild running after the direct child is killed -- and
    that grandchild inherits the same stdout/stderr pipe fds, so it alone
    is enough to keep a drain thread's `.read()` from ever seeing EOF, even
    once `proc.wait()` has confirmed the direct child is gone.
    `ProcessLookupError` means the whole group already exited on its own
    (an ordinary race, not a bug) -- nothing left to kill."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _drain(stream, budget: int, chunks: list[str], state: dict, kill) -> None:
    """Read `stream` (a text-mode pipe) to EOF in bounded chunks, appending
    each to `chunks` until the running character total exceeds `budget` --
    at which point `kill()` is called (once) and this keeps reading WITHOUT
    appending, so the child can still exit (its pipe keeps draining) instead
    of blocking forever on a full pipe buffer. `state["chars"]`/`state["over"]`
    are only written once, right when the excess is first detected, and are
    the caller's signal -- read only AFTER joining this thread, so there is
    no race between this thread setting them and the caller checking them.

    Character count, not `len(bytes)`, is the budget unit here -- the same
    trade SarifClient.parse() makes (see its docstring): bytes >= chars
    always, so a character total already over `budget` proves the byte
    total is over it too, without ever encoding a chunk just to count it.
    For nearly-all-ASCII tool output (SARIF/JSON, which is what every one
    of these tools emits) this is effectively exact; worst case it lets a
    heavily multi-byte stream run up to ~4x `budget` bytes before tripping,
    which is still far short of the pod's own memory limit."""
    total = 0
    over = False
    for piece in iter(lambda: stream.read(_RUN_READ_CHUNK), ""):
        total += len(piece)
        if not over and total > budget:
            over = True
            state["chars"] = total
            state["over"] = True
            kill()
        if not over:
            chunks.append(piece)
    state.setdefault("chars", total)
    state.setdefault("over", False)


class StorageClient:
    """`ctx.storage` -- object storage. Reserved: results travel inline in
    the result envelope, and no storage backend exists yet."""

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx

    def put(self, name: str, data: bytes):
        raise NotImplementedError(
            "ctx.storage.put() is not implemented: results travel inline in the "
            "result envelope; no object storage backend exists yet"
        )


class Context:
    """Built once per setup/task invocation by the task host. `capability`
    and `operation` are the task host's own knowledge (from the manifest and
    the request), never read by the task function itself -- they exist on
    Context so ctx.sarif and ctx.findings can tag findings with them without
    the task passing them explicitly."""

    def __init__(
        self,
        capability: str,
        operation: str,
        workdir: Path,
        credentials: dict[str, str] | None = None,
        log: logging.Logger | None = None,
        allow_anonymous_credentials: bool = False,
    ) -> None:
        self.capability = capability
        self.operation = operation
        self.workdir = Path(workdir)
        self.log = log or logging.getLogger(f"runwhen_capability.{capability}.{operation}")
        self._credentials = dict(credentials or {})
        # `rwtask run --allow-anonymous` only -- see cli.py/run_local.py.
        # Never set by `rwtask serve`: an operator degrading a REQUIRED
        # credential to anonymous must be a deliberate local-dev act, not
        # something reachable from a running pod.
        self._allow_anonymous_credentials = allow_anonymous_credentials

        self.git = GitClient(self)
        self.sarif = SarifClient(self)
        self.findings = FindingsClient(self)
        self.storage = StorageClient(self)
        self.repo_fs = RepoFsClient(self)

    def credential(self, name: str, optional: bool = False) -> str | None:
        """The resolved value for a declared credential -- in memory, never
        in os.environ. When `name` was not resolved for this request:
        `optional=True` (the caller's own deliberate opt-in, mirroring the
        manifest's `needs.credentials[].optional: true`) returns None;
        otherwise raises CredentialNotFoundError naming `name` -- a
        declared credential that cannot be resolved is a hard failure, not
        a silent degrade. `--allow-anonymous` (`rwtask run` only) also
        returns None instead of raising, as a blanket local-dev override --
        see `_allow_anonymous_credentials` above."""
        try:
            return self._credentials[name]
        except KeyError:
            if optional:
                return None
            if self._allow_anonymous_credentials:
                self.log.warning(
                    "credential %r not resolved; --allow-anonymous is degrading this "
                    "request to anonymous -- a deliberate local-dev override, never "
                    "the platform default",
                    name,
                )
                return None
            raise CredentialNotFoundError(f"no credential resolved for {name!r}") from None

    def skip(self, reason: str = "") -> None:
        """Raises SkipTask(reason) -- the same mechanism a packaged
        capability's task already has via `raise SkipTask(...)`, spelled as
        `ctx.skip(reason)` for a custom bundle task's `main(ctx, **inputs)`
        (see bundle.py). Only a task may skip: called from setup, this is an
        ordinary setup failure, exactly like `raise SkipTask(...)` from
        setup already is -- see SkipTask's docstring."""
        raise SkipTask(reason)

    def run(
        self,
        argv: list[str],
        cwd: Path | str | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
        *,
        inherit_env: bool = True,
    ) -> subprocess.CompletedProcess:
        """Runs argv as a subprocess. stdout is captured (returned on
        `.stdout`); stderr is captured and forwarded line-by-line to
        ctx.log; the timeout (default DEFAULT_RUN_TIMEOUT) is enforced --
        a timeout raises subprocess.TimeoutExpired, which the task host
        treats like any other task exception.

        `env` is MERGED OVER the parent environment, never a replacement:
        a tool needs PATH and the rest of its runtime, and a caller that
        only wants to add one variable must not have to reconstruct
        everything else. Merged into a fresh dict per call rather than
        mutating `os.environ`, because tools run concurrently in one
        process -- a global mutation would leak into whatever else is
        running alongside.

        `inherit_env=False` replaces that merge with EXACTLY `env` -- no
        `os.environ` in the child at all. This is for a capability that
        runs tools whose OWN config, supplied by an untrusted repo, decides
        what that tool reads or reaches: such a caller builds its own
        allow-list rather than handing the tool every variable the
        executor pod happens to hold, including anything secret. The
        default stays `True` -- most callers rely on inheriting the parent
        environment, so opting OUT of that is a deliberate choice a caller
        makes, never the default.

        The case the MERGE exists for: the capability's root filesystem is
        READ-ONLY, so a tool that writes to the default `/tmp` fails
        outright. Only `workdir` (`/work`) is writable, so such a tool
        must be pointed at it (trivy's vulnerability DB download, for one,
        dies on `mkdir /tmp/trivy-...: read-only file system`).

        Bounded at SARIF_BYTE_BUDGET: a tool's stdout is captured
        INCREMENTALLY (Popen + a dedicated drain thread per stream, not
        subprocess.run's capture_output, which buffers everything before
        the caller sees any of it) so a pathological amount of output
        raises OutputTooLargeError -- the process is killed -- instead of
        being held in memory in full. This is the same failure this
        module's docstring already documents for SarifClient.parse(): the
        oversized-payload guard has to run BEFORE the bytes are fully
        materialised, not after, or the guard cannot prevent the OOM it
        exists to stop. Unlike parse() (which already has the whole SARIF
        text in hand and can check its length directly), ctx.run has to
        stop READING partway through, so it can only report a lower bound
        on the true size -- hence ">N bytes" rather than "N bytes" in the
        message. stderr gets the same treatment against a much smaller
        budget (_STDERR_BYTE_BUDGET) -- see that constant's comment.

        Both streams are drained by their own thread regardless of what the
        other is doing, specifically so the child can never block writing
        to a full pipe while this method is only reading the other one --
        the classic two-pipe deadlock. On a timeout OR an oversized-stdout
        kill, the process is killed and then waited on (never left a
        zombie) before either raising TimeoutExpired or returning control
        to the oversized-output check below.

        The kill reaches the whole process GROUP (`start_new_session=True`
        below, killed via _kill_process_group's `os.killpg`), not just the
        direct child `proc.kill()` would reach: a tool that shells out to
        its own scanner engine (semgrep, checkov, ...) can leave a
        grandchild running that inherits the same stdout/stderr pipes, and
        that alone is enough to keep a drain thread's `.read()` from ever
        seeing EOF -- turning a refusal that is supposed to be fast and
        1Gi-safe into a hung task instead (the lease then expires and the
        real cause is lost). The thread joins below are ALSO bounded
        (_DRAIN_JOIN_TIMEOUT), as a second, independent backstop: a
        grandchild that further detached into its own session escapes even
        the process-group kill, and this method still has to return control
        -- with the right exception -- rather than block on a thread that
        may now never finish.

        `errors="replace"` on the Popen: a linter can write a byte its own
        stdout encoding does not consider valid (a mangled multibyte
        sequence in a repo file it is reporting on, for instance). Without
        this, `stream.read()` inside a drain thread's own loop raises
        UnicodeDecodeError -- uncaught there, that kills the thread mid-read
        with `chunks` and `state` however far they had got, which this
        method never learns about: `stdout_state.get("over")` below then
        reads as falsy (never set, not merely False), so a tool that
        actually emitted invalid UTF-8 comes back as returncode=<real
        code>, stdout='' -- a silent empty result, exactly what errors.py's
        own docstring says must never happen. Replacing the bad byte with
        U+FFFD instead loses only that one mangled unit, not everything
        the drain thread had already read or would have read after it --
        a linter's finding should not cost the whole result over one byte
        it got wrong.
        """
        run_cwd = Path(cwd) if cwd is not None else self.workdir
        if inherit_env:
            run_env = {**os.environ, **env} if env else None
        else:
            run_env = dict(env or {})
        proc = subprocess.Popen(  # noqa: S603 -- argv is capability-controlled, by design
            argv,
            cwd=str(run_cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",  # see the docstring above: a mangled byte must not go silent
            env=run_env,
            start_new_session=True,  # so `proc.pid` is also the process group id -- see above
        )
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        stdout_state: dict = {}
        stderr_state: dict = {}
        stdout_thread = threading.Thread(
            target=_drain,
            args=(
                proc.stdout,
                SARIF_BYTE_BUDGET,
                stdout_chunks,
                stdout_state,
                lambda: _kill_process_group(proc),
            ),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain,
            args=(proc.stderr, _STDERR_BYTE_BUDGET, stderr_chunks, stderr_state, lambda: None),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            proc.wait(timeout=timeout or DEFAULT_RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            proc.wait()
            raise
        finally:
            # Bounded, not an unbounded join() -- see the docstring above.
            # `daemon=True` on both threads means one abandoned past this
            # bound does not stop the process (or this method) from moving
            # on; it is simply left running, reading a pipe nothing else
            # will ever act on, until it (eventually) sees EOF or this
            # process exits.
            stdout_thread.join(timeout=_DRAIN_JOIN_TIMEOUT)
            stderr_thread.join(timeout=_DRAIN_JOIN_TIMEOUT)
            # Only close a stream whose reader actually finished: closing
            # the fd out from under a thread still blocked in `.read()` on
            # it (the abandoned-thread case) risks that read raising
            # mid-flight or the fd number being reused by something else in
            # this process before the abandoned read ever returns.
            if not stdout_thread.is_alive():
                proc.stdout.close()
            if not stderr_thread.is_alive():
                proc.stderr.close()

        if stdout_state.get("over"):
            raise OutputTooLargeError(
                f"check output too large to process: >{stdout_state['chars']} bytes"
            )

        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
        if stderr_state.get("over"):
            stderr += f"\n... stderr truncated at {_STDERR_BYTE_BUDGET} bytes"

        if stderr:
            prog = argv[0] if argv else "?"
            for line in stderr.splitlines():
                self.log.info("%s: %s", prog, line)
        return subprocess.CompletedProcess(argv, proc.returncode, stdout=stdout, stderr=stderr)
