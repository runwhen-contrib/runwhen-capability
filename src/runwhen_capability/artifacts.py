"""`ctx.artifacts` -- writing artifacts: large or binary task output that
travels to the runner's relay as its own upload instead of inside the result.

Three ways to write one, all streamed (memory stays bounded however big the
artifact is):

    with ctx.artifacts.open("report.txt", "text/plain") as out:
        out.write(b"...")                 # any number of writes
    ref = out.ref                         # an ArtifactRef; return it from the task

    ref = ctx.artifacts.add_file(path, "dump.bin")
    run = ctx.artifacts.from_process(["pg_dump", ...], "dump.sql")   # run.ref, .returncode, .stderr

A task returns the ref as (part of) an output; `model_dump(mode="json")` of
it is `{"$artifact": ref}`, which is what the platform substitutes the stored
artifact for. That marker is all a result ever carries of the bytes.

How a streamed write works (`ArtifactWriter`). `write()` cuts its input into
chunks of at most 1 MiB and puts them on a queue of four; one background
thread runs `RelayClient.put_artifact` over a generator that drains that
queue. The producer is therefore held back by the upload (a full queue blocks
`write`), and memory is about six chunks (four queued, one being sent, one
being filled) plus whatever the HTTP layer holds, whatever the artifact's size.

Two rules keep that from going wrong:

- A writer must never hang. The runner answers an upload early when it can
  (413 after `maxBytes + 1` bytes, 503 before any), and then the upload
  thread is gone while the queue is full and nothing will ever drain it. So
  `write` puts with a timeout in a loop and, each time round, checks that the
  thread is still running; if it is not, `write` raises that thread's error,
  and so does every `write` after it.
- An abort must look like one. Leaving a `with` block on an exception, or
  `abort()`, does not end the body: it makes the body generator RAISE. A
  generator that simply returned would make urllib3 send the chunked
  terminator, the runner would see a complete body and finalize a
  half-written artifact. Raising drops the connection without the
  terminator, which the runner reads as an abandoned upload. The writer
  swallows its own abort; the caller's original exception is what propagates.

`add_file` re-reads its file, so a busy runner (503) is retried; `open` and
`from_process` consume a stream that cannot be replayed, so they raise
`ArtifactBusyError` instead.

Reading goes the other way, from the artifacts the platform bound to the
task's inputs (`ctx.input_artifacts`, from the lease):

    with ctx.artifacts.input("prior-report") as fh:    # a stream; the caller closes it
        for line in fh: ...
    path = ctx.artifacts.input_path("prior-report")    # a local file, for tools that want one

`input` is the relay's response body, undecoded, never buffered. `input_path`
spools the artifact to a file in a sha256-keyed cache that outlives the
request, so the same artifact is downloaded once however many requests use it,
and verifies the digest while it streams (`E_ARTIFACT_CORRUPT` on a mismatch).
The cache's design -- keys, locking, eviction -- is in `_artifact_cache.py`.
A name the request did not bind is `ArtifactUndeclaredError`, raised before
any HTTP.

With no relay (`rwtask run`, `ctx.relay is None`) the same API writes files
under `<workdir>/.rw-artifacts/<name>` through `LocalArtifactSink`, so a task
runs the same way on a laptop, and reads inputs from
`<workdir>/.rw-artifacts/inputs/<name>` (`input_path` returns that path itself:
there is nothing to cache). This module does not import `requests` (the
relay client is only duck-typed here): a bundle's child process imports it
through `context`, and must not pay for that.
"""

from __future__ import annotations

import hashlib
import os
import queue
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, NamedTuple

from pydantic import BaseModel, model_serializer

from ._artifact_cache import (
    ARTIFACT_CACHE_DIR,
    ArtifactCache,
    max_bytes_from_env,
    normalize_sha256,
)
from .errors import ArtifactBusyError, ArtifactError, ArtifactUndeclaredError

if TYPE_CHECKING:
    from .context import Context
    from .models import InputArtifactDecl
    from .relay import RelayClient

# The largest piece `write` hands to the upload, and the size `from_process`
# and `add_file` read in.
CHUNK_BYTES = 1 << 20
# Chunks the queue holds: with the one being sent and the one being filled,
# a writer holds about six chunks.
QUEUE_DEPTH = 4

# A task output declared `spill: auto` whose JSON is larger than this goes to
# an artifact instead of the result (host.py).
SPILL_THRESHOLD_BYTES = 256 * 1024

# `add_file` retries a busy runner (503) this many times, waiting this many
# seconds before each retry. Module constants so a test can set the delay to 0.
BUSY_RETRIES = 3
BUSY_RETRY_DELAY = 5

DEFAULT_CONTENT_TYPE = "application/octet-stream"

# `LocalArtifactSink`'s directory, under the Context's workdir.
LOCAL_ARTIFACT_DIR = ".rw-artifacts"
# Where, under that directory, a local run (no relay) keeps its input artifacts.
LOCAL_INPUT_DIR = "inputs"

# What `from_process` keeps of the child's stderr, same budget as `ctx.run`'s.
STDERR_BYTE_BUDGET = 1024 * 1024

# How long `write` waits on a full queue before it re-checks that the upload
# is still running; also the pace at which `from_process` checks on its two
# halves (the child and the upload).
_POLL_SECONDS = 0.1
# How long `from_process` gives a reader thread to notice a dead child and
# finish, once the child is reaped: a bound, not the child's runtime. A
# grandchild that kept a pipe open past the group kill must not hold the
# task up.
_DRAIN_JOIN_TIMEOUT = 5

_sleep = time.sleep  # indirection so a test can observe the busy backoff

_END = object()  # queue sentinel: the body is complete
_ABORT = object()  # queue sentinel: abandon the upload


class _UploadAborted(Exception):
    """Raised inside the body generator when the writer was aborted. Internal:
    the upload thread swallows it."""


class ArtifactRef(BaseModel):
    """A stored artifact, as the relay reported it. In a task's output it
    serializes to `{"$artifact": ref}` -- the marker the platform resolves --
    so returning one needs no conversion."""

    ref: str
    size_bytes: int
    sha256: str

    @model_serializer
    def _serialize(self) -> dict[str, str]:
        return {"$artifact": self.ref}


class ProcessArtifact(NamedTuple):
    """What `from_process` returns: the artifact made of the child's stdout,
    its exit code, and its stderr (decoded with replacement, at most 1 MiB)."""

    ref: ArtifactRef
    returncode: int
    stderr: str


def _ref_from(created: dict) -> ArtifactRef:
    """The relay's 201 JSON as an ArtifactRef."""
    try:
        return ArtifactRef(
            ref=created["artifactRef"], size_bytes=created["sizeBytes"], sha256=created["sha256"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactError("E_ARTIFACT_RESPONSE", f"unexpected relay answer: {created!r}") from exc


class ArtifactWriter:
    """An artifact being uploaded. A context manager: leaving the block
    normally closes it (`.ref` is then the ArtifactRef; `close()` returns it
    too); leaving it on an exception aborts the upload and stores nothing.
    For one producer thread. See the module docstring for the mechanics."""

    @staticmethod
    def _new_queue(maxsize: int) -> queue.Queue:
        return queue.Queue(maxsize)

    def __init__(self, upload: Callable[[Iterator[bytes]], ArtifactRef]) -> None:
        """`upload` consumes the body iterator and returns the ArtifactRef;
        it runs on the writer's own thread, which starts here."""
        self._upload = upload
        self._queue = self._new_queue(QUEUE_DEPTH)
        self._pending = bytearray()  # small writes, coalesced up to one chunk
        self._ref: ArtifactRef | None = None
        self._error: BaseException | None = None
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="rw-artifact-upload", daemon=True)
        self._thread.start()

    @property
    def ref(self) -> ArtifactRef | None:
        """The stored artifact, once the writer closed successfully; None
        before that, after an error, and after an abort."""
        return self._ref

    @property
    def closed(self) -> bool:
        """True once `close()` or `abort()` was called (no more writes)."""
        return self._closed

    @property
    def failure(self) -> BaseException | None:
        """The error that ended the upload, if it has failed."""
        return self._error

    def write(self, b: bytes) -> None:
        """Queues `b` for upload, in chunks of at most 1 MiB; blocks while
        the queue is full. Raises the upload's error as soon as it has
        failed (the relay refused the artifact, the connection broke), and
        keeps raising it. Writing nothing does nothing."""
        if self._error is not None:
            raise self._error
        if self._closed:
            raise ValueError("write to a closed artifact writer")
        view = memoryview(b).cast("B")
        size, pos = len(view), 0
        while pos < size:
            if not self._pending and size - pos >= CHUNK_BYTES:
                self._put(bytes(view[pos : pos + CHUNK_BYTES]))
                pos += CHUNK_BYTES
                continue
            take = min(CHUNK_BYTES - len(self._pending), size - pos)
            self._pending += view[pos : pos + take]
            pos += take
            if len(self._pending) >= CHUNK_BYTES:
                self._flush()

    def close(self) -> ArtifactRef:
        """Ends the body, waits for the relay's answer and returns the
        ArtifactRef -- or raises the error the upload hit. Closing again
        returns the same ref (or raises the same error)."""
        if not self._closed:
            self._closed = True
            try:
                self._flush()
                self._put(_END)
            except BaseException:
                self.abort()
                raise
        self._thread.join()
        if self._error is not None:
            raise self._error
        if self._ref is None:
            raise ArtifactError("E_ARTIFACT_ABORTED", "the upload was aborted; nothing was stored")
        return self._ref

    def abort(self) -> None:
        """Abandons the upload: the connection is dropped mid-body, so the
        runner stores nothing. Queued data is discarded. Idempotent, and a
        no-op once the artifact is stored."""
        self._closed = True
        self._pending.clear()
        while self._thread.is_alive():
            self._discard_queued()
            try:
                self._queue.put(_ABORT, timeout=_POLL_SECONDS)
                break
            except queue.Full:
                continue  # the producer raced a chunk in after the discard; clear it again
        self._thread.join()

    def __enter__(self) -> ArtifactWriter:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()  # the with-block's own exception is what propagates

    # -- internals -----------------------------------------------------------

    def _run(self) -> None:
        try:
            self._ref = self._upload(self._drain())
        except _UploadAborted:
            pass
        except BaseException as exc:  # noqa: BLE001 -- handed to the producer, see _check_upload
            self._error = exc

    def _drain(self) -> Iterator[bytes]:
        while True:
            item = self._queue.get()
            if item is _END:
                return
            if item is _ABORT:
                raise _UploadAborted  # never `return`: that would end the body cleanly
            yield item

    def _flush(self) -> None:
        if self._pending:
            data = bytes(self._pending)
            self._pending.clear()
            self._put(data)

    def _put(self, item) -> None:
        while True:
            self._check_upload()
            try:
                self._queue.put(item, timeout=_POLL_SECONDS)
                return
            except queue.Full:
                continue  # the loop's check is what keeps a dead upload from blocking us forever

    def _check_upload(self) -> None:
        alive = self._thread.is_alive()  # read first: the error is set before the thread exits
        if self._error is not None:
            raise self._error
        if not alive:
            raise ArtifactError("E_ARTIFACT_ENDED", "the upload ended before the writer was closed")

    def _discard_queued(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return


def _local_path(root: Path, name: str) -> Path:
    """`root/name`, for a name that stays inside `root` (`E_ARTIFACT_PATH`
    otherwise): relative, with no empty, '.' or '..' parts."""
    parts = name.split("/")
    if not name or name.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise ArtifactError(
            "E_ARTIFACT_PATH",
            f"artifact name {name!r} must be a relative path with no empty, '.' or '..' parts",
        )
    return root.joinpath(*parts)


class LocalArtifactSink:
    """Stands in for the relay when there is none: stores each artifact as the
    file `<root>/<name>` and answers like the relay's 201. The ref is
    `local_<first 32 hex of the sha256>`. Only a name that stays inside
    `root` is accepted (`E_ARTIFACT_PATH`). A file appears only once it is
    complete: an aborted or failed write leaves nothing. Writing a name again
    replaces it."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def path_for(self, name: str) -> Path:
        return _local_path(self.root, name)

    def put_artifact(
        self, *, task: str, name: str, content_type: str, body: Iterable[bytes]
    ) -> dict:
        """Same shape as `RelayClient.put_artifact` (`task` and `content_type`
        are accepted for that and not recorded)."""
        dest = self.path_for(name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".rw-part-")
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as fh:
                for piece in body:
                    fh.write(piece)
                    digest.update(piece)
                    size += len(piece)
            os.replace(tmp_name, dest)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        sha256 = digest.hexdigest()
        return {"artifactRef": "local_" + sha256[:32], "sizeBytes": size, "sha256": sha256}


def _read_chunks(fh) -> Iterator[bytes]:
    while True:
        data = fh.read(CHUNK_BYTES)
        if not data:
            return
        yield data


def _kill_if_unreaped(proc: subprocess.Popen) -> None:
    """Kills `proc`'s process group unless `proc` was already waited on.
    Not `poll()`: that would reap a dead leader, and a group whose leader is
    only a zombie can still hold a grandchild that has the pipes open. A
    reaped leader's pid may belong to someone else by now, so then no kill."""
    from .context import _kill_process_group  # context imports this module

    if proc.returncode is None:
        try:
            _kill_process_group(proc)
        except PermissionError:
            pass  # macOS: a group that is only a zombie leader refuses the signal; nothing to kill


def _pump_stdout(proc: subprocess.Popen, writer: ArtifactWriter, failure: list) -> None:
    """Copies the child's stdout into `writer` in 1 MiB reads. If anything
    fails -- chiefly the upload -- records it and kills the child, which would
    otherwise sit blocked on a pipe nobody reads."""
    try:
        while True:
            data = proc.stdout.read(CHUNK_BYTES)
            if not data:
                return
            writer.write(data)
    except BaseException as exc:  # noqa: BLE001 -- reported by from_process
        failure.append(exc)
        _kill_if_unreaped(proc)


def _drain_stderr(stream, kept: bytearray) -> None:
    """Reads `stream` to EOF, keeping the first STDERR_BYTE_BUDGET bytes and
    discarding the rest (the child must never block on a full pipe)."""
    while True:
        data = stream.read(65536)
        if not data:
            return
        room = STDERR_BYTE_BUDGET - len(kept)
        if room > 0:
            kept += data[:room]


class ArtifactsClient:
    """`ctx.artifacts` -- writing artifacts through the runner's relay, or into
    `LocalArtifactSink` when the Context has no relay, and reading the
    request's input artifacts. It holds the Context it reads the relay, the
    task name, the declared outputs and the input artifacts from.
    `local_sink` replaces the default sink (`<workdir>/.rw-artifacts`)."""

    def __init__(self, ctx: Context, local_sink: LocalArtifactSink | None = None) -> None:
        self._ctx = ctx
        self._local = local_sink
        self._writers: list[tuple[str, ArtifactWriter]] = []  # opened here, see abort_unclosed
        self._cache: ArtifactCache | None = None  # made on the first input_path

    def open(self, name: str, content_type: str | None = None) -> ArtifactWriter:
        """Starts the upload of artifact `name` and returns its writer (use
        it as a context manager). `content_type` defaults to the one the
        output declares, else `application/octet-stream`. A busy runner (503)
        raises `ArtifactBusyError` from the write or close that sees it: a
        stream cannot be replayed, so there is no retry."""
        store = self._store()
        if isinstance(store, LocalArtifactSink):
            store.path_for(name)  # a bad name fails here, not in the background
        writer = ArtifactWriter(lambda body: self._put(name, content_type, body))
        self._writers = [(n, w) for n, w in self._writers if not w.closed]
        self._writers.append((name, writer))
        return writer

    def input(self, name: str) -> BinaryIO:
        """The bytes of input artifact `name`, as a stream the relay is still
        sending: read it in pieces and close it (it is a context manager). The
        body is exactly the stored bytes -- any `Content-Encoding` is left
        alone. `name` must be one of the request's input artifacts, else
        `ArtifactUndeclaredError` (before any HTTP). With no relay it is the
        file `<workdir>/.rw-artifacts/inputs/<name>`, opened for reading."""
        if self._ctx.relay is None:
            return open(self._local_input(name), "rb")
        self._declared_input(name)
        raw = self._ctx.relay.open_input(name).raw
        raw.decode_content = False
        return raw

    def input_path(self, name: str) -> Path:
        """Input artifact `name` as a local file, for code that wants a path.
        The first use downloads it into a cache that is shared with every
        other request this executor serves and bounded by
        `RW_ARTIFACT_CACHE_MAX_BYTES` (default 1 GiB, least recently used
        entries go first); a later use finds it there with no request at all.
        The file is the lease's sha256-verified bytes (`E_ARTIFACT_CORRUPT` if
        they do not hash to it), and read-only (mode 0444): copy it to edit
        it, the original is shared with every later request. It is yours for the
        rest of the task; do not hold on to the path past it. With no relay
        it is `<workdir>/.rw-artifacts/inputs/<name>` itself."""
        if self._ctx.relay is None:
            return self._local_input(name)
        decl = self._declared_input(name)
        expected = (
            normalize_sha256(decl.sha256, f"the declared sha256 of input artifact {name!r}")
            if decl.sha256
            else None
        )
        relay = self._ctx.relay
        return self._input_cache().fetch(name, expected, lambda: relay.open_input(name))

    def abort_unclosed(self) -> None:
        """Aborts every writer opened through this client that was neither
        closed nor aborted, logging a warning for each. The task host calls it
        when a setup/task returns or raises: a writer the task forgot would
        otherwise keep its upload thread, its socket and a few MiB of queue
        alive for as long as the process lives, and the runner would hold the
        artifact open. Nothing is stored for an aborted writer."""
        writers, self._writers = self._writers, []
        for name, writer in writers:
            if not writer.closed:
                self._ctx.log.warning(
                    "artifact %r was left open by the task; its upload is aborted, nothing stored",
                    name,
                )
                writer.abort()

    def add_file(self, path: str | Path, name: str, content_type: str | None = None) -> ArtifactRef:
        """Uploads the file at `path` as artifact `name`. A busy runner (503)
        is retried: three more attempts, BUSY_RETRY_DELAY seconds apart, the
        file read again each time. Any other error is final."""
        attempt = 0
        while True:
            try:
                with open(path, "rb") as fh:
                    return self._put(name, content_type, _read_chunks(fh))
            except ArtifactBusyError:
                if attempt >= BUSY_RETRIES:
                    raise
            attempt += 1
            _sleep(BUSY_RETRY_DELAY)

    def from_process(
        self,
        argv: list[str],
        name: str,
        *,
        content_type: str | None = "text/plain",
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ProcessArtifact:
        """Runs `argv` and uploads its stdout as artifact `name`, as it is
        produced. Runs in the Context's workdir; `env` is merged over the
        parent's environment, as in `ctx.run`. A non-zero exit still completes
        the artifact: check `.returncode`. stderr is returned (the first 1 MiB,
        decoded with replacement).

        `timeout` (seconds): when it passes, the child's whole process group
        is killed, the upload is abandoned (no artifact) and
        `subprocess.TimeoutExpired` is raised. Unlike `ctx.run`, unset (None)
        means no limit here, not `DEFAULT_RUN_TIMEOUT`: this is for long dumps,
        and the request's own deadline already bounds the run, so a silent cut
        would only truncate a legitimate artifact.
        If the upload fails first -- the relay refused it, the connection
        broke -- the group is killed and that error raised. The child is
        always reaped. A busy runner is an `ArtifactBusyError`, not retried.

        The runner's refusal can only be seen once a chunk is sent, so for a
        quiet child it surfaces when the child has written 1 MiB or exited."""
        run_env = {**os.environ, **env} if env else None
        proc = subprocess.Popen(  # noqa: S603 -- argv is capability-controlled, by design
            argv,
            cwd=str(self._ctx.workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=run_env,
            start_new_session=True,  # so `proc.pid` is also the process group id
        )
        stderr_kept = bytearray()
        stderr_thread = threading.Thread(
            target=_drain_stderr, args=(proc.stderr, stderr_kept), daemon=True
        )
        stderr_thread.start()
        writer: ArtifactWriter | None = None
        pump: threading.Thread | None = None
        failure: list[BaseException] = []
        try:
            writer = self.open(name, content_type)
            pump = threading.Thread(target=_pump_stdout, args=(proc, writer, failure), daemon=True)
            pump.start()
            self._await_exit(proc, pump, writer, failure, argv, timeout)
            ref = writer.close()
        except BaseException:
            _kill_if_unreaped(proc)
            if writer is not None:
                writer.abort()
            raise
        finally:
            proc.wait()
            for thread in (pump, stderr_thread):
                if thread is not None:
                    thread.join(timeout=_DRAIN_JOIN_TIMEOUT)
            # Only close a pipe whose reader finished: closing it under a
            # blocked read (an abandoned thread) is worse than leaving it.
            if pump is None or not pump.is_alive():
                proc.stdout.close()
            if not stderr_thread.is_alive():
                proc.stderr.close()
        stderr = bytes(stderr_kept).decode("utf-8", errors="replace")
        return ProcessArtifact(ref, proc.returncode, stderr)

    @staticmethod
    def _await_exit(
        proc: subprocess.Popen,
        pump: threading.Thread,
        writer: ArtifactWriter,
        failure: list[BaseException],
        argv: list[str],
        limit: float | None,
    ) -> None:
        """Returns once the child has exited and its stdout is fully copied
        into the writer; raises if the upload failed first or `limit` (None:
        no limit) passed. The caller kills the child on a raise."""
        deadline = None if limit is None else time.monotonic() + limit
        # The upload's failure is checked on every turn: a relay that said
        # "busy" or "too large" at once must not wait for the child's first
        # byte of output (which a quiet child may never produce in time).
        while pump.is_alive() and not failure and writer.failure is None:
            wait = _POLL_SECONDS
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, limit)
                wait = min(remaining, wait)
            pump.join(timeout=wait)
        error = failure[0] if failure else writer.failure
        if error is not None:
            raise error
        try:
            proc.wait(timeout=None if deadline is None else max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired(argv, limit) from None

    def _put(self, name: str, content_type: str | None, body: Iterable[bytes]) -> ArtifactRef:
        ctx = self._ctx
        task = ctx.task if ctx.task is not None else ctx.operation
        created = self._store().put_artifact(
            task=task,
            name=name,
            content_type=content_type or self._declared_content_type(task, name),
            body=body,
        )
        return _ref_from(created)

    def _declared_input(self, name: str) -> InputArtifactDecl:
        for decl in self._ctx.input_artifacts:
            if decl.input == name:
                return decl
        declared = ", ".join(repr(d.input) for d in self._ctx.input_artifacts) or "none"
        raise ArtifactUndeclaredError(
            "E_ARTIFACT_UNDECLARED",
            f"{name!r} is not one of this request's input artifacts (declared: {declared})",
        )

    def _local_input(self, name: str) -> Path:
        """The file a local run (no relay) reads input `name` from."""
        path = _local_path(self._ctx.workdir / LOCAL_ARTIFACT_DIR / LOCAL_INPUT_DIR, name)
        if not path.is_file():
            raise ArtifactUndeclaredError(
                "E_ARTIFACT_UNDECLARED",
                f"no input artifact {name!r}: a local run reads it from {path}, not a file",
            )
        return path

    def _input_cache(self) -> ArtifactCache:
        if self._cache is None:
            root = self._ctx.artifact_cache_dir or self._ctx.workdir / ARTIFACT_CACHE_DIR
            self._cache = ArtifactCache(root, max_bytes_from_env(), self._ctx.log)
        return self._cache

    def _store(self) -> RelayClient | LocalArtifactSink:
        if self._ctx.relay is not None:
            return self._ctx.relay
        if self._local is None:
            self._local = LocalArtifactSink(self._ctx.workdir / LOCAL_ARTIFACT_DIR)
        return self._local

    def _declared_content_type(self, task: str, name: str) -> str:
        """The content type the output declares for `name` (an exact name
        first, then a wildcard output whose prefix `name` is under), else the
        generic binary type."""
        wildcard = None
        for decl in self._ctx.outputs:
            if decl.task != task or not decl.contentType:
                continue
            if decl.name == name:
                return decl.contentType
            if decl.wildcard and name.startswith(decl.name.rstrip("/") + "/"):
                wildcard = decl.contentType
        return wildcard or DEFAULT_CONTENT_TYPE
