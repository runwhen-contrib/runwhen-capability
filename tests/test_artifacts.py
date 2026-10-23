"""ctx.artifacts -- the writers (open / add_file / from_process), the local
sink used when there is no relay, and the spill of large task outputs. The
upload tests run against the real local relay (tests/relay_server.py): the
point is the bytes and the termination on the wire, which a mock cannot show.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tracemalloc
from pathlib import Path

import pytest
import relay_server as relay_module
import requests
from relay_server import PieceStream, Response

from runwhen_capability import Context, artifacts
from runwhen_capability import context as context_module
from runwhen_capability.artifacts import (
    ArtifactRef,
    ArtifactsClient,
    ArtifactWriter,
    LocalArtifactSink,
    ProcessArtifact,
)
from runwhen_capability.errors import (
    ArtifactBusyError,
    ArtifactError,
    ArtifactExistsError,
    ArtifactTooLargeError,
)
from runwhen_capability.host import _to_jsonable, run_request
from runwhen_capability.loader import load_capability
from runwhen_capability.models import OutputDecl, RequestEnvelope
from runwhen_capability.relay import RelayClient

MIB = 1 << 20
PUT_PREFIX = "/v1/tasks/req-1/artifacts/"


def _created(ref: str = "art_abc", size: int = 0, sha256: str = "0" * 64) -> Response:
    body = {"artifactRef": ref, "sizeBytes": size, "sha256": sha256, "lineCount": 1, "isText": True}
    return Response(201, json=body)


def _relay(relay_server, tmp_path: Path) -> RelayClient:
    token_file = tmp_path / "token"
    token_file.write_text("tok-1")
    return RelayClient(relay_server.url, token_file, "req-1", requests.Session())


def _ctx(relay_server, tmp_path: Path, outputs=()) -> Context:
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    return Context(
        "rw-fixture",
        "scan",
        workdir,
        relay=_relay(relay_server, tmp_path),
        task="scan",
        outputs=outputs,
    )


def _local_ctx(tmp_path: Path) -> Context:
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    return Context("rw-fixture", "scan", workdir, task="scan")


def _wait_for_requests(relay_server, count: int, timeout: float = 10.0):
    """The handler records a request only once it has finished reading the
    body, which for an aborted upload is after the client call returned."""
    deadline = time.monotonic() + timeout
    while len(relay_server.requests) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(relay_server.requests) >= count, f"server saw {len(relay_server.requests)} requests"
    return relay_server.requests


def _run_with_deadline(fn, seconds: float = 20.0):
    """Runs `fn` in a thread; fails (instead of hanging the suite) if it does
    not finish. Returns (result, exception)."""
    outcome: list = [None, None]

    def target():
        try:
            outcome[0] = fn()
        except BaseException as exc:  # noqa: BLE001 -- handed to the test to assert on
            outcome[1] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"still running after {seconds}s: it hung"
    return outcome[0], outcome[1]


# -- ArtifactRef ------------------------------------------------------------


def test_ref_serializes_to_dollar_artifact():
    ref = ArtifactRef(ref="art_1", size_bytes=5, sha256="ab" * 32)

    assert ref.model_dump(mode="json") == {"$artifact": "art_1"}
    assert ref.model_dump() == {"$artifact": "art_1"}
    # The host's _to_jsonable needs no special case for it, at any depth.
    assert _to_jsonable({"report": ref, "all": [ref]}) == {
        "report": {"$artifact": "art_1"},
        "all": [{"$artifact": "art_1"}],
    }
    assert json.loads(ref.model_dump_json()) == {"$artifact": "art_1"}
    assert (ref.ref, ref.size_bytes, ref.sha256) == ("art_1", 5, "ab" * 32)


# -- open -------------------------------------------------------------------


class _TrackingQueue(queue.Queue):
    """Counts the payload bytes sitting in the queue, and their peak."""

    def __init__(self, maxsize: int = 0) -> None:
        super().__init__(maxsize)
        self.queued = 0
        self.peak = 0

    def _put(self, item) -> None:
        super()._put(item)
        if isinstance(item, bytes):
            self.queued += len(item)
            self.peak = max(self.peak, self.queued)

    def _get(self):
        item = super()._get()
        if isinstance(item, bytes):
            self.queued -= len(item)
        return item


def test_open_streams_with_bounded_queue(relay_server, tmp_path, monkeypatch):
    """50 MiB goes through the writer in odd-sized writes while the relay
    reads slowly. The producer must be held back by the queue (4 chunks of
    1 MiB), not run ahead of it, and every byte must still arrive, in order."""
    tracking: list[_TrackingQueue] = []

    def make_queue(maxsize: int) -> _TrackingQueue:
        tracking.append(_TrackingQueue(maxsize))
        return tracking[0]

    monkeypatch.setattr(ArtifactWriter, "_new_queue", staticmethod(make_queue))
    relay_server.read_delay = 0.0005
    relay_server.script("PUT", PUT_PREFIX, _created("art_big", size=50 * MIB))
    total = 50 * MIB
    stream = PieceStream(total, piece_size=700_000)  # neither aligned to nor below the chunk size
    ctx = _ctx(relay_server, tmp_path)

    with ctx.artifacts.open("big.bin", "application/octet-stream") as writer:
        for piece in stream:
            writer.write(piece)

    (seen,) = relay_server.requests
    assert writer.ref.ref == "art_big"
    assert (seen.body_length, seen.body_sha256) == (total, stream.sha256)
    assert seen.chunked and seen.body_complete
    (tq,) = tracking
    assert tq.peak <= 4 * MIB + MIB, f"{tq.peak / MIB:.1f} MiB queued"
    assert tq.peak >= 2 * MIB, "the queue never backed up, so the bound was not exercised"


def test_open_sends_the_task_name_content_type_and_path(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_1", size=3))
    ctx = _ctx(relay_server, tmp_path)

    with ctx.artifacts.open("out dir/a b.txt", "text/plain") as writer:
        writer.write(b"abc")

    (seen,) = relay_server.requests
    assert seen.method == "PUT"
    assert seen.path == "/v1/tasks/req-1/artifacts/out%20dir/a%20b.txt"
    assert seen.header("X-RW-Task") == "scan"
    assert seen.header("Content-Type") == "text/plain"
    assert seen.body == b"abc"
    assert writer.ref == ArtifactRef(ref="art_1", size_bytes=3, sha256="0" * 64)


def test_close_returns_the_ref_and_closing_twice_is_harmless(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_1", size=2, sha256="f" * 64))
    ctx = _ctx(relay_server, tmp_path)

    writer = ctx.artifacts.open("a.txt")
    writer.write(b"hi")
    first = writer.close()
    second = writer.close()

    assert first == second == writer.ref
    assert first == ArtifactRef(ref="art_1", size_bytes=2, sha256="f" * 64)
    assert len(relay_server.requests) == 1
    with pytest.raises(ValueError):
        writer.write(b"more")


def test_open_zero_bytes_uploads_empty_artifact(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_empty", size=0))
    ctx = _ctx(relay_server, tmp_path)

    with ctx.artifacts.open("empty.txt", "text/plain") as writer:
        pass

    (seen,) = relay_server.requests
    assert seen.chunked
    assert seen.body == b""
    assert seen.body_complete  # the terminator went out: a zero-length artifact, not an abort
    assert writer.ref.ref == "art_empty"


def test_write_of_nothing_is_a_noop_not_a_terminator(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created())
    ctx = _ctx(relay_server, tmp_path)

    with ctx.artifacts.open("a.txt") as writer:
        writer.write(b"")
        writer.write(b"abc")
        writer.write(b"")
        writer.write(b"def")

    (seen,) = relay_server.requests
    assert seen.body == b"abcdef"
    assert seen.body_complete


def test_many_small_writes_arrive_whole_and_in_order(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created())
    ctx = _ctx(relay_server, tmp_path)
    lines = [f"line {i}\n".encode() for i in range(20_000)]

    with ctx.artifacts.open("lines.txt", "text/plain") as writer:
        for line in lines:
            writer.write(line)

    (seen,) = relay_server.requests
    assert seen.body == b"".join(lines)


def test_open_busy_raises_without_retry(relay_server, tmp_path, monkeypatch):
    """A stream cannot be replayed, so a busy answer is final for `open`."""
    monkeypatch.setattr(artifacts, "BUSY_RETRY_DELAY", 0)
    relay_server.script("PUT", PUT_PREFIX, Response(503, json={"code": "E_ARTIFACT_BUSY"}))
    ctx = _ctx(relay_server, tmp_path)

    with pytest.raises(ArtifactBusyError):
        with ctx.artifacts.open("a.txt") as writer:
            writer.write(b"x" * 1000)

    assert len(relay_server.requests) == 1
    assert writer.ref is None


def test_close_raises_the_error_the_relay_answered_with(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, Response(409, json={"code": "E_ARTIFACT_EXISTS"}))
    ctx = _ctx(relay_server, tmp_path)

    writer = ctx.artifacts.open("a.txt")
    writer.write(b"abc")
    with pytest.raises(ArtifactExistsError):
        writer.close()
    with pytest.raises(ArtifactExistsError):  # and again: the same error, no second attempt
        writer.close()

    assert len(relay_server.requests) == 1
    assert writer.ref is None


def test_exception_inside_with_block_aborts_upload(relay_server, tmp_path):
    """The with-block's own exception propagates, the runner sees a body that
    never reached its terminator (so it cannot finalize an artifact), and no
    ref comes back."""
    # Were the abort to end the body cleanly, this is the answer the runner
    # would give: a finalized artifact.
    relay_server.script("PUT", PUT_PREFIX, _created("art_should_not_exist"))
    ctx = _ctx(relay_server, tmp_path)

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with ctx.artifacts.open("partial.bin") as writer:
            for _ in range(6):  # more than the queue holds: the upload is mid-flight
                writer.write(b"x" * MIB)
            raise Boom("task failed half-way")

    (seen,) = _wait_for_requests(relay_server, 1)
    assert not seen.body_complete
    assert writer.ref is None
    with pytest.raises(ArtifactError):  # nothing to hand back, and not a silent success
        writer.close()


def test_abort_makes_the_body_raise_it_never_just_ends():
    """urllib3 sends the chunked terminator when the body iterator returns,
    which would finalize a half-written artifact. So an abort must surface
    inside the iterator as an exception."""
    seen: dict = {}

    def upload(body):
        try:
            for _ in body:
                pass
        except BaseException as exc:  # noqa: BLE001 -- recording what the iterator did
            seen["raised"] = type(exc)
            raise
        seen["ended"] = True
        return ArtifactRef(ref="art_x", size_bytes=0, sha256="")

    writer = ArtifactWriter(upload)
    writer.write(b"x" * (3 * MIB))
    writer.abort()

    assert "raised" in seen
    assert "ended" not in seen
    assert writer.ref is None


def test_write_after_early_413_raises_instead_of_hanging(relay_server, tmp_path, monkeypatch):
    """The runner answers 413 after `maxBytes + 1` bytes and drops the
    connection while the producer is still writing. The upload thread is then
    gone, and the producer is typically parked on a full queue that nothing
    will drain; a `write` that just blocked there would never return. The
    slow relay keeps the queue full up to the moment the connection drops."""
    monkeypatch.setattr(relay_module, "_DRAIN_TOTAL_TIMEOUT", 0.3)  # the runner closes soon
    relay_server.read_delay = 0.005
    max_bytes = 1024
    relay_server.script(
        "PUT",
        PUT_PREFIX,
        Response(
            413, json={"code": "E_ARTIFACT_TOO_LARGE", "maxBytes": max_bytes}, read_limit=max_bytes
        ),
    )
    ctx = _ctx(relay_server, tmp_path)

    def produce():
        with ctx.artifacts.open("big.bin") as writer:
            for _ in range(200):  # far more than the queue holds
                writer.write(b"x" * MIB)

    _, error = _run_with_deadline(produce)

    assert isinstance(error, ArtifactTooLargeError)
    assert error.max_bytes == max_bytes
    assert relay_server.requests[0].body_length == max_bytes
    assert not relay_server.requests[0].body_complete


def test_writer_raises_the_upload_error_when_the_upload_died_mid_stream():
    """The upload thread can end (the runner answered and dropped the
    connection) while the queue is full and nothing will ever drain it. A
    blocking put would hang the task forever; the writer must notice the dead
    thread and raise its error -- from then on, from every write."""

    def upload(body):
        next(iter(body))  # takes one chunk...
        time.sleep(0.5)  # ...lets the producer fill the queue and park on a put...
        raise ArtifactTooLargeError("E_ARTIFACT_TOO_LARGE", "too big", max_bytes=1)

    writer = ArtifactWriter(upload)

    def produce():
        for _ in range(64):
            writer.write(b"x" * MIB)

    _, error = _run_with_deadline(produce, seconds=10)

    assert isinstance(error, ArtifactTooLargeError)
    with pytest.raises(ArtifactTooLargeError) as again:
        writer.write(b"y")
    assert again.value is error
    _, close_error = _run_with_deadline(writer.close, seconds=10)
    assert close_error is error
    _, abort_error = _run_with_deadline(writer.abort, seconds=10)  # the with-block exit path
    assert abort_error is None


# -- content type -----------------------------------------------------------


def test_content_type_defaults_to_the_declared_one_then_octet_stream(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, *[_created() for _ in range(4)])
    ctx = _ctx(
        relay_server,
        tmp_path,
        outputs=[
            OutputDecl(
                task="scan", name="report.json", artifact=True, contentType="application/json"
            ),
            OutputDecl(task="other", name="elsewhere.txt", artifact=True, contentType="text/csv"),
            OutputDecl(
                task="scan", name="logs", artifact=True, wildcard=True, contentType="text/x-log"
            ),
        ],
    )

    for name in ("report.json", "elsewhere.txt", "logs/a.log", "unknown.bin"):
        with ctx.artifacts.open(name) as writer:
            writer.write(b"x")

    types = [r.header("Content-Type") for r in relay_server.requests]
    assert types == [
        "application/json",
        "application/octet-stream",
        "text/x-log",
        "application/octet-stream",
    ]


def test_an_explicit_content_type_wins_over_the_declared_one(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created())
    ctx = _ctx(
        relay_server,
        tmp_path,
        outputs=[OutputDecl(task="scan", name="r", artifact=True, contentType="application/json")],
    )

    with ctx.artifacts.open("r", "text/plain") as writer:
        writer.write(b"x")

    assert relay_server.requests[0].header("Content-Type") == "text/plain"


# -- add_file ---------------------------------------------------------------


def _file(tmp_path: Path, size: int, name: str = "data.bin") -> tuple[Path, str]:
    path = tmp_path / name
    data = bytes(range(251)) * (size // 251 + 1)
    path.write_bytes(data[:size])
    return path, hashlib.sha256(data[:size]).hexdigest()


def test_add_file_uploads_and_returns_ref(relay_server, tmp_path):
    path, digest = _file(tmp_path, 3 * MIB + 17)
    relay_server.script("PUT", PUT_PREFIX, _created("art_file", size=3 * MIB + 17, sha256=digest))
    ctx = _ctx(relay_server, tmp_path)

    ref = ctx.artifacts.add_file(path, "data.bin", "application/x-test")

    assert ref == ArtifactRef(ref="art_file", size_bytes=3 * MIB + 17, sha256=digest)
    (seen,) = relay_server.requests
    assert seen.path == PUT_PREFIX + "data.bin"
    assert seen.body_sha256 == digest
    assert seen.body_length == 3 * MIB + 17
    assert seen.chunked and seen.body_complete
    assert seen.header("Content-Type") == "application/x-test"
    assert seen.header("X-RW-Task") == "scan"


def test_add_file_accepts_a_str_path_and_an_empty_file(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created())
    (tmp_path / "empty").write_bytes(b"")
    ctx = _ctx(relay_server, tmp_path)

    ctx.artifacts.add_file(str(tmp_path / "empty"), "empty")

    (seen,) = relay_server.requests
    assert seen.body == b"" and seen.body_complete


def test_add_file_retries_busy_then_succeeds(relay_server, tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "BUSY_RETRY_DELAY", 0)
    path, digest = _file(tmp_path, 2 * MIB)
    busy = Response(503, json={"code": "E_ARTIFACT_BUSY"}, read_limit=0)
    relay_server.script("PUT", PUT_PREFIX, busy, busy, _created("art_third", size=2 * MIB))
    ctx = _ctx(relay_server, tmp_path)

    ref = ctx.artifacts.add_file(path, "data.bin")

    assert ref.ref == "art_third"
    seen = relay_server.requests
    assert len(seen) == 3
    assert [r.body_length for r in seen[:2]] == [0, 0]  # rejected before any body was read
    # The file was reopened each time: the accepted attempt got every byte.
    assert (seen[2].body_length, seen[2].body_sha256) == (2 * MIB, digest)
    assert seen[2].body_complete


def test_add_file_waits_between_busy_attempts_and_gives_up_after_four(
    relay_server, tmp_path, monkeypatch
):
    sleeps: list[float] = []
    monkeypatch.setattr(artifacts, "_sleep", sleeps.append)
    path, _ = _file(tmp_path, 1000)
    busy = Response(503, json={"code": "E_ARTIFACT_BUSY"}, read_limit=0)
    relay_server.script("PUT", PUT_PREFIX, busy, busy, busy, busy, busy)
    ctx = _ctx(relay_server, tmp_path)

    with pytest.raises(ArtifactBusyError):
        ctx.artifacts.add_file(path, "data.bin")

    assert len(relay_server.requests) == 4  # the first try and three retries
    assert sleeps == [5, 5, 5]  # BUSY_RETRY_DELAY between attempts, none after the last


def test_add_file_does_not_retry_other_errors(relay_server, tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "BUSY_RETRY_DELAY", 0)
    path, _ = _file(tmp_path, 1000)
    relay_server.script("PUT", PUT_PREFIX, Response(409, json={"code": "E_ARTIFACT_EXISTS"}))
    ctx = _ctx(relay_server, tmp_path)

    with pytest.raises(ArtifactExistsError):
        ctx.artifacts.add_file(path, "data.bin")

    assert len(relay_server.requests) == 1


def test_add_file_missing_file_fails_before_any_request(relay_server, tmp_path):
    ctx = _ctx(relay_server, tmp_path)

    with pytest.raises(FileNotFoundError):
        ctx.artifacts.add_file(tmp_path / "nope", "nope")

    assert relay_server.requests == []


# -- from_process -----------------------------------------------------------

_EMIT = """
import sys
out = sys.stdout.buffer
block = bytes(range(256)) * 4096  # 1 MiB
for i in range({count}):
    out.write(block[i:] + block[:i])
sys.stderr.write("boom\\n")
sys.exit(3)
"""


def _emitted(count: int) -> tuple[int, str]:
    """What _EMIT writes to stdout: its length and sha256."""
    block = bytes(range(256)) * 4096
    digest = hashlib.sha256()
    for i in range(count):
        digest.update(block[i:] + block[:i])
    return count * len(block), digest.hexdigest()


def test_from_process_streams_stdout_and_returns_returncode(relay_server, tmp_path):
    """A child prints 20 MiB; the server must get exactly that, a non-zero exit
    still completes the artifact, and the client's memory stays far below the
    output size.

    The tracemalloc peak counts every thread, the fixture server's included.
    Measured at 8.3 MiB (the 4 MiB queue, the chunk being sent, the 1 MiB
    read in hand and its copy, the fixture's 1 MiB retention); the bound has
    headroom for allocator noise yet is well under the 20 MiB a buffering
    client would hold."""
    total, digest = _emitted(20)
    relay_server.script("PUT", PUT_PREFIX, _created("art_proc", size=total, sha256=digest))
    ctx = _ctx(relay_server, tmp_path)

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        result = ctx.artifacts.from_process(
            [sys.executable, "-c", _EMIT.format(count=20)], "out.txt"
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert isinstance(result, ProcessArtifact)
    assert result.returncode == 3
    assert result.stderr == "boom\n"
    assert result.ref == ArtifactRef(ref="art_proc", size_bytes=total, sha256=digest)
    (seen,) = relay_server.requests
    assert (seen.body_length, seen.body_sha256) == (total, digest)
    assert seen.body_complete
    assert seen.header("Content-Type") == "text/plain"  # from_process' default
    assert peak < 14 * MIB, f"peak {peak / MIB:.1f} MiB for a 20 MiB output"


def test_from_process_runs_in_the_workdir_with_the_env_merged_over_the_parent(
    relay_server, tmp_path, monkeypatch
):
    monkeypatch.setenv("RW_TEST_PARENT", "from-parent")
    relay_server.script("PUT", PUT_PREFIX, _created())
    ctx = _ctx(relay_server, tmp_path)
    script = (
        "import os; print(os.getcwd(), os.environ['RW_TEST_PARENT'], os.environ['RW_TEST_ADDED'])"
    )

    result = ctx.artifacts.from_process(
        [sys.executable, "-c", script], "out.txt", env={"RW_TEST_ADDED": "added"}
    )

    assert result.returncode == 0
    (seen,) = relay_server.requests
    assert seen.body.decode().split() == [
        str(ctx.workdir.resolve()),
        "from-parent",
        "added",
    ]


def test_from_process_caps_stderr_and_decodes_it_leniently(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created())
    ctx = _ctx(relay_server, tmp_path)
    script = (
        "import sys; sys.stderr.buffer.write(b'bad:\\xff:' + b'e' * (2 * 1024 * 1024)); print('ok')"
    )

    result = ctx.artifacts.from_process([sys.executable, "-c", script], "out.txt")

    assert result.returncode == 0
    assert result.stderr.startswith("bad:�:eee")
    assert len(result.stderr) <= MIB
    assert relay_server.requests[0].body == b"ok\n"


def test_from_process_timeout_kills_the_group_and_aborts_the_upload(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_should_not_exist"))
    ctx = _ctx(relay_server, tmp_path)
    script = (
        "import os, subprocess, sys, time\n"
        "grandchild = subprocess.Popen(['sleep', '60'])\n"
        "open('pids', 'w').write(f'{os.getpid()} {grandchild.pid}')\n"
        "print('partial output', flush=True)\n"
        "time.sleep(60)\n"
    )

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        ctx.artifacts.from_process([sys.executable, "-c", script], "out.txt", timeout=2)
    elapsed = time.monotonic() - started

    assert elapsed < 15
    assert excinfo.value.timeout == 2
    (seen,) = _wait_for_requests(relay_server, 1)
    assert not seen.body_complete  # an abort, not a finished artifact
    pids = [int(p) for p in (ctx.workdir / "pids").read_text().split()]
    deadline = time.monotonic() + 5
    for pid in pids:  # the child (reaped by from_process) and its grandchild
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"process {pid} survived the timeout")


def test_from_process_without_a_timeout_runs_until_the_child_exits(
    relay_server, tmp_path, monkeypatch
):
    """`timeout=None` is no SDK-side limit (a multi-GB dump may legitimately
    take long; the request's own deadline bounds the run), not ctx.run's
    default: a child outliving that default is not cut short."""
    monkeypatch.setattr(context_module, "DEFAULT_RUN_TIMEOUT", 0.2)
    relay_server.script("PUT", PUT_PREFIX, _created("art_slow"))
    ctx = _ctx(relay_server, tmp_path)
    script = "import time; time.sleep(0.5); print('done')"

    result = ctx.artifacts.from_process([sys.executable, "-c", script], "out.txt")

    assert result.returncode == 0
    assert result.ref.ref == "art_slow"
    (seen,) = relay_server.requests
    assert seen.body == b"done\n"
    assert seen.body_complete


def test_from_process_busy_relay_raises_busy_kills_the_child_and_does_not_retry(
    relay_server, tmp_path, monkeypatch
):
    """The runner says busy before it reads a byte and closes; the child is
    an endless writer that would otherwise run to the timeout."""
    monkeypatch.setattr(relay_module, "_DRAIN_TOTAL_TIMEOUT", 0.3)  # the runner closes soon
    relay_server.script(
        "PUT", PUT_PREFIX, Response(503, json={"code": "E_ARTIFACT_BUSY"}, read_limit=0)
    )
    ctx = _ctx(relay_server, tmp_path)
    script = (
        "import os, sys, time\n"
        "open('pid', 'w').write(str(os.getpid()))\n"
        "out = sys.stdout.buffer\n"
        "while True:\n"
        "    out.write(b'x' * 65536)\n"
        "    time.sleep(0.001)\n"
    )

    def run():
        return ctx.artifacts.from_process([sys.executable, "-c", script], "out.txt", timeout=60)

    started = time.monotonic()
    _, error = _run_with_deadline(run, seconds=30)

    assert isinstance(error, ArtifactBusyError)
    assert time.monotonic() - started < 25  # nowhere near the 60 s timeout
    assert len(relay_server.requests) == 1  # never retried
    pid = int((ctx.workdir / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_from_process_missing_program_raises_and_uploads_nothing(relay_server, tmp_path):
    ctx = _ctx(relay_server, tmp_path)

    with pytest.raises(FileNotFoundError):
        ctx.artifacts.from_process(["/nonexistent/program"], "out.txt")

    assert relay_server.requests == []


# -- spill ------------------------------------------------------------------

_TASKS = """
from pathlib import Path

from runwhen_capability import setup, task


@task(outputs={"blob": "text", "note": "text"})
def sized(ctx, length):
    return {"blob": "x" * length, "note": "hi"}


@task(outputs={"files": "json"})
def paths(ctx, count):
    return {"files": [Path(f"/srv/data/{i:06d}") for i in range(count)]}


@setup(outputs=[])
def leaky_setup(ctx):
    writer = ctx.artifacts.open("from-setup")
    writer.write(b"x" * (2 * 1024 * 1024))  # in flight; never closed


@task(outputs={"note": "text"})
def leaky(ctx):
    writer = ctx.artifacts.open("forgotten")
    writer.write(b"x" * (2 * 1024 * 1024))  # in flight; never closed
    return {"note": "returned without closing"}


@task(outputs={"note": "text"})
def leaky_then_raise(ctx):
    writer = ctx.artifacts.open("forgotten")
    writer.write(b"x" * (2 * 1024 * 1024))
    raise RuntimeError("failed with the writer open")


@task(outputs={"report": "artifact"})
def publish(ctx):
    path = ctx.workdir / "report.txt"
    path.write_text("findings\\n")
    return {"report": ctx.artifacts.add_file(path, "report")}
"""


def _capability(tmp_path: Path):
    cap_dir = tmp_path / "cap"
    cap_dir.mkdir(exist_ok=True)
    (cap_dir / "manifest.yaml").write_text('capability: rw-artifacts-fixture\nversion: "0.0.0"\n')
    (cap_dir / "tasks.py").write_text(_TASKS)
    return load_capability(cap_dir)


def _request(task: str, **inputs) -> RequestEnvelope:
    return RequestEnvelope.model_validate(
        {"version": 1, "tasks": [{"task": task, "inputs": inputs}]}
    )


def _run(relay_server, tmp_path, request, outputs):
    scope = tmp_path / "scope"
    scope.mkdir(exist_ok=True)
    return run_request(
        _capability(tmp_path),
        request,
        credentials={},
        scope_dir=scope,
        relay=_relay(relay_server, tmp_path),
        outputs=outputs,
    )


def test_spill_auto_moves_large_output_to_artifact(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_spilled", size=300 * 1024 + 2))
    length = 300 * 1024

    result = _run(
        relay_server,
        tmp_path,
        _request("sized", length=length),
        [OutputDecl(task="sized", name="blob", spill=True, maxBytes=10 * MIB)],
    )

    (task_result,) = result.tasks
    assert task_result.status == "ok"
    assert task_result.outputs == {"blob": {"$artifact": "art_spilled"}, "note": "hi"}
    (seen,) = relay_server.requests
    assert seen.path == PUT_PREFIX + "blob"
    assert seen.header("Content-Type") == "application/json"
    assert seen.header("X-RW-Task") == "sized"
    assert seen.body == json.dumps("x" * length, separators=(",", ":")).encode()
    assert seen.body_complete


def test_spill_threshold_is_on_the_serialised_json_size(relay_server, tmp_path):
    threshold = artifacts.SPILL_THRESHOLD_BYTES
    assert threshold == 256 * 1024
    relay_server.script("PUT", PUT_PREFIX, _created("art_over"))
    decls = [OutputDecl(task="sized", name="blob", spill=True)]

    # A JSON string is its length plus two quotes: exactly the threshold stays inline...
    at = _run(relay_server, tmp_path, _request("sized", length=threshold - 2), decls)
    assert at.tasks[0].outputs["blob"] == "x" * (threshold - 2)
    assert relay_server.requests == []

    # ...and one byte more spills.
    over = _run(relay_server, tmp_path, _request("sized", length=threshold - 1), decls)
    assert over.tasks[0].outputs["blob"] == {"$artifact": "art_over"}
    assert len(relay_server.requests) == 1


def test_spill_serialises_the_value_before_measuring_it(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_paths"))
    count = 20_000  # 20k Paths of ~17 characters: well past the threshold once serialised

    result = _run(
        relay_server,
        tmp_path,
        _request("paths", count=count),
        [OutputDecl(task="paths", name="files", spill=True)],
    )

    assert result.tasks[0].outputs == {"files": {"$artifact": "art_paths"}}
    (seen,) = relay_server.requests
    uploaded = json.loads(seen.body.decode())
    assert uploaded[:2] == ["/srv/data/000000", "/srv/data/000001"]
    assert len(uploaded) == count


def test_spill_never_keeps_inline(relay_server, tmp_path):
    """Without a spill declaration for the output (none at all, one for another
    task, one that is not `spill`, or one for another name) a big output stays
    in the result and nothing is uploaded."""
    length = 300 * 1024
    undeclared = [
        [],
        [OutputDecl(task="other", name="blob", spill=True)],
        [OutputDecl(task="sized", name="blob", artifact=True)],
        [OutputDecl(task="sized", name="elsewhere", spill=True)],
    ]

    for decls in undeclared:
        result = _run(relay_server, tmp_path, _request("sized", length=length), decls)
        assert result.tasks[0].outputs["blob"] == "x" * length

    assert relay_server.requests == []


def test_spill_leaves_small_outputs_inline(relay_server, tmp_path):
    result = _run(
        relay_server,
        tmp_path,
        _request("sized", length=1000),
        [OutputDecl(task="sized", name="blob", spill=True)],
    )

    assert result.tasks[0].outputs == {"blob": "x" * 1000, "note": "hi"}
    assert relay_server.requests == []


def test_a_failed_spill_upload_fails_the_task(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, Response(409, json={"code": "E_ARTIFACT_EXISTS"}))

    result = _run(
        relay_server,
        tmp_path,
        _request("sized", length=300 * 1024),
        [OutputDecl(task="sized", name="blob", spill=True)],
    )

    assert result.tasks[0].status == "failed"
    assert "E_ARTIFACT_EXISTS" in result.tasks[0].error


def _upload_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "rw-artifact-upload" and t.is_alive()]


def test_a_writer_the_task_left_open_is_aborted_when_the_task_returns(relay_server, tmp_path):
    """A forgotten writer would keep its upload thread (parked on an empty
    queue), its socket and a few MiB alive for the life of the pod."""
    relay_server.script("PUT", PUT_PREFIX, _created("art_should_not_exist"))
    before = _upload_threads()

    result = _run(
        relay_server,
        tmp_path,
        _request("leaky"),
        [OutputDecl(task="leaky", name="forgotten", artifact=True)],
    )

    assert result.tasks[0].status == "ok"
    assert [t for t in _upload_threads() if t not in before] == []  # gone, not merely idle
    (seen,) = _wait_for_requests(relay_server, 1)
    assert not seen.body_complete  # an abandoned upload, never a finished artifact


def test_a_writer_left_open_by_a_task_that_raises_is_aborted_too(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_should_not_exist"))
    before = _upload_threads()

    result = _run(
        relay_server,
        tmp_path,
        _request("leaky_then_raise"),
        [OutputDecl(task="leaky_then_raise", name="forgotten", artifact=True)],
    )

    assert result.tasks[0].status == "failed"
    assert "failed with the writer open" in result.tasks[0].error
    assert [t for t in _upload_threads() if t not in before] == []
    (seen,) = _wait_for_requests(relay_server, 1)
    assert not seen.body_complete


def test_a_writer_left_open_by_setup_is_aborted(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_should_not_exist"))
    before = _upload_threads()
    scope = tmp_path / "scope"
    scope.mkdir()

    result = run_request(
        _capability(tmp_path),
        RequestEnvelope.model_validate({"version": 1, "setup": {"task": "leaky_setup"}}),
        credentials={},
        scope_dir=scope,
        relay=_relay(relay_server, tmp_path),
        outputs=[OutputDecl(task="leaky_setup", name="from-setup", artifact=True)],
    )

    assert result.setup.status == "ok"
    assert [t for t in _upload_threads() if t not in before] == []
    (seen,) = _wait_for_requests(relay_server, 1)
    assert not seen.body_complete


def test_abort_unclosed_leaves_closed_and_aborted_writers_alone(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_done"))
    ctx = _ctx(relay_server, tmp_path)

    with ctx.artifacts.open("done.txt") as writer:
        writer.write(b"x")
    ctx.artifacts.abort_unclosed()

    assert writer.ref.ref == "art_done"  # a finished artifact stays finished
    assert len(relay_server.requests) == 1


def test_artifact_types_are_exported_for_task_authors():
    import runwhen_capability

    assert runwhen_capability.ArtifactRef is ArtifactRef
    assert runwhen_capability.ProcessArtifact is ProcessArtifact
    assert {"ArtifactRef", "ProcessArtifact"} <= set(runwhen_capability.__all__)


def test_a_task_can_return_an_artifact_ref_as_an_output(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_report", size=9))

    result = _run(
        relay_server,
        tmp_path,
        _request("publish"),
        [OutputDecl(task="publish", name="report", artifact=True)],
    )

    assert result.tasks[0].status == "ok"
    assert result.tasks[0].outputs == {"report": {"$artifact": "art_report"}}
    assert relay_server.requests[0].body == b"findings\n"


# -- the local sink ---------------------------------------------------------


def test_local_sink_used_without_relay(tmp_path):
    ctx = _local_ctx(tmp_path)
    digest = hashlib.sha256(b"hello world").hexdigest()

    with ctx.artifacts.open("reports/out.txt", "text/plain") as writer:
        writer.write(b"hello ")
        writer.write(b"world")

    assert (ctx.workdir / ".rw-artifacts" / "reports" / "out.txt").read_bytes() == b"hello world"
    assert writer.ref == ArtifactRef(ref="local_" + digest[:32], size_bytes=11, sha256=digest)
    assert writer.ref.model_dump(mode="json") == {"$artifact": "local_" + digest[:32]}
    # Nothing but the artifact is left behind (no partial file).
    assert [p.name for p in (ctx.workdir / ".rw-artifacts" / "reports").iterdir()] == ["out.txt"]


def test_local_sink_add_file_and_from_process(tmp_path):
    ctx = _local_ctx(tmp_path)
    path, digest = _file(tmp_path, 2 * MIB + 5)

    file_ref = ctx.artifacts.add_file(path, "copy.bin")
    proc = ctx.artifacts.from_process(
        [sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err')"], "proc.txt"
    )

    root = ctx.workdir / ".rw-artifacts"
    assert file_ref == ArtifactRef(
        ref="local_" + digest[:32], size_bytes=2 * MIB + 5, sha256=digest
    )
    assert (root / "copy.bin").read_bytes() == path.read_bytes()
    assert (root / "proc.txt").read_bytes() == b"out\n"
    assert (proc.returncode, proc.stderr) == (0, "err")
    assert proc.ref.size_bytes == 4


def test_local_sink_abort_leaves_no_file(tmp_path):
    ctx = _local_ctx(tmp_path)

    with pytest.raises(RuntimeError):
        with ctx.artifacts.open("a.txt") as writer:
            writer.write(b"x" * (2 * MIB))
            raise RuntimeError("half-way")

    assert writer.ref is None
    root = ctx.workdir / ".rw-artifacts"
    assert not (root / "a.txt").exists()
    assert not any(p.is_file() for p in root.rglob("*"))


@pytest.mark.parametrize("name", ["", "/etc/passwd", "../escape", "a/../../escape", "a/.."])
def test_local_sink_rejects_names_that_could_leave_its_root(tmp_path, name):
    ctx = _local_ctx(tmp_path)

    with pytest.raises(ArtifactError) as excinfo:
        with ctx.artifacts.open(name) as writer:
            writer.write(b"x")

    assert excinfo.value.code == "E_ARTIFACT_PATH"
    assert not (tmp_path / "escape").exists()


def test_local_sink_root_is_a_parameter(tmp_path):
    ctx = _local_ctx(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    ctx.artifacts = ArtifactsClient(ctx, local_sink=LocalArtifactSink(elsewhere))

    with ctx.artifacts.open("a.txt") as writer:
        writer.write(b"x")

    assert (elsewhere / "a.txt").read_bytes() == b"x"
    assert not (ctx.workdir / ".rw-artifacts").exists()


def test_spill_works_through_the_local_sink(tmp_path):
    """No relay but a spill declaration (a test harness, say): the same path."""
    cap = _capability(tmp_path)
    (tmp_path / "scope").mkdir()

    result = run_request(
        cap,
        _request("sized", length=300 * 1024),
        credentials={},
        scope_dir=tmp_path / "scope",
        outputs=[OutputDecl(task="sized", name="blob", spill=True)],
    )

    ref = result.tasks[0].outputs["blob"]["$artifact"]
    assert ref.startswith("local_")
    stored = (tmp_path / "scope" / ".rw-artifacts" / "blob").read_bytes()
    assert json.loads(stored) == "x" * (300 * 1024)
