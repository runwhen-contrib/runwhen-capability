"""ctx.artifacts.input / input_path -- reading the request's input artifacts,
and the sha256-keyed cache `input_path` spools them into. Run against the real
local relay (tests/relay_server.py), so the downloads are real HTTP streams.

A test that needs two "requests" builds two Contexts that share one
`artifact_cache_dir` (what `rwtask serve` does: the cache sits above the
per-request scope directories). One Context is one task: it is what an entry
handed out by `input_path` is protected for.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
import tracemalloc
from pathlib import Path

import pytest
import requests
import urllib3
from relay_server import PieceStream, Response

from runwhen_capability import Context, artifacts
from runwhen_capability import _artifact_cache as cache_module
from runwhen_capability import relay as relay_module
from runwhen_capability.errors import ArtifactError, ArtifactUndeclaredError
from runwhen_capability.host import run_request
from runwhen_capability.loader import load_capability
from runwhen_capability.models import InputArtifactDecl, RequestEnvelope
from runwhen_capability.relay import RelayClient

MIB = 1 << 20
IN_PREFIX = "/v1/tasks/req-1/artifacts/in/"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decl(name: str, data: bytes) -> InputArtifactDecl:
    return InputArtifactDecl(input=name, sizeBytes=len(data), sha256=_sha(data))


def _served(data: bytes, **headers: str) -> Response:
    """What the relay answers a GET with: the bytes and their sha256."""
    return Response(200, body=data, headers={"X-RW-Sha256": _sha(data), **headers})


def _relay(relay_server, tmp_path: Path) -> RelayClient:
    token_file = tmp_path / "token"
    token_file.write_text("tok-1")
    return RelayClient(relay_server.url, token_file, "req-1", requests.Session())


def _ctx(
    relay_server,
    tmp_path: Path,
    inputs=(),
    *,
    cache_dir: Path | None = None,
    scope: str = "work",
) -> Context:
    workdir = tmp_path / scope
    workdir.mkdir(exist_ok=True)
    return Context(
        "rw-fixture",
        "scan",
        workdir,
        relay=_relay(relay_server, tmp_path),
        task="scan",
        input_artifacts=inputs,
        artifact_cache_dir=cache_dir,
    )


def _local_ctx(tmp_path: Path) -> Context:
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    return Context("rw-fixture", "scan", workdir, task="scan")


_scope_counter = iter(range(1_000_000))


def _fetch(
    relay_server, tmp_path: Path, cache: Path, name: str, data: bytes, *, serve: bool = True
) -> Path:
    """One whole request: its own Context (scope), the relay scripted to
    serve `data` for input `name` (unless `serve` is False: a request that
    must not need the relay), and `input_path(name)`."""
    if serve:
        relay_server.script("GET", IN_PREFIX, _served(data))
    ctx = _ctx(
        relay_server,
        tmp_path,
        [_decl(name, data)],
        cache_dir=cache,
        scope=f"scope-{next(_scope_counter)}",
    )
    return ctx.artifacts.input_path(name)


def _age(path: Path, seconds: float) -> None:
    """Makes `path` look last used `seconds` ago."""
    then = time.time() - seconds
    os.utime(path, (then, then))


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


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


def _wait_for_requests(relay_server, count: int, timeout: float = 10.0) -> None:
    """The handler records a GET as soon as it has read the request, before it
    answers it."""
    deadline = time.monotonic() + timeout
    while len(relay_server.requests) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(relay_server.requests) >= count, f"server saw {len(relay_server.requests)} requests"


def _wait_for(condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert condition(), f"not true after {timeout}s"


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# -- input(): a stream straight from the relay ----------------------------------


def test_input_streams_bytes(relay_server, tmp_path):
    data = b"line one\nline two\n" * 1000
    relay_server.script("GET", IN_PREFIX, _served(data))
    ctx = _ctx(relay_server, tmp_path, [_decl("report", data)])

    fh = ctx.artifacts.input("report")
    try:
        assert fh.read(10) == data[:10]
        assert fh.read() == data[10:]
    finally:
        fh.close()

    (seen,) = relay_server.requests
    assert seen.method == "GET"
    assert seen.path == IN_PREFIX + "report"
    assert seen.header("Authorization") == "Bearer tok-1"
    assert not (ctx.workdir / ".rw-artifact-cache").exists()  # a stream does not touch the cache


def test_input_and_input_path_hand_back_the_stored_bytes_not_a_decoded_copy(relay_server, tmp_path):
    """The sha256 is of the stored bytes. If the relay (or a proxy) labels
    the body `Content-Encoding: gzip`, decoding it would change the bytes
    and break every hash check."""
    stored = gzip.compress(b"hello " * 500)
    for _ in range(2):
        relay_server.script("GET", IN_PREFIX, _served(stored, **{"Content-Encoding": "gzip"}))
    ctx = _ctx(relay_server, tmp_path, [_decl("blob", stored)])

    with ctx.artifacts.input("blob") as fh:
        assert fh.read() == stored
    assert ctx.artifacts.input_path("blob").read_bytes() == stored


def test_unknown_input_raises_undeclared(relay_server, tmp_path):
    data = b"declared"
    ctx = _ctx(relay_server, tmp_path, [_decl("report", data)])

    with pytest.raises(ArtifactUndeclaredError) as streamed:
        ctx.artifacts.input("nope")
    with pytest.raises(ArtifactUndeclaredError):
        ctx.artifacts.input_path("nope")

    assert streamed.value.code == "E_ARTIFACT_UNDECLARED"
    assert "nope" in str(streamed.value)
    assert relay_server.requests == []  # refused before any HTTP


def test_a_relay_404_for_a_declared_input_is_undeclared_too(relay_server, tmp_path):
    relay_server.script("GET", IN_PREFIX, Response(404))
    ctx = _ctx(relay_server, tmp_path, [_decl("report", b"x")])

    with pytest.raises(ArtifactUndeclaredError):
        ctx.artifacts.input_path("report")
    assert _names(ctx.workdir / ".rw-artifact-cache") == []


# -- input_path(): the cache --------------------------------------------------


def test_input_path_caches_by_sha_and_reuses(relay_server, tmp_path):
    data = b"cached bytes\n" * 5000
    relay_server.script("GET", IN_PREFIX, _served(data))
    ctx = _ctx(relay_server, tmp_path, [_decl("report", data)])

    first = ctx.artifacts.input_path("report")

    assert first == ctx.workdir / ".rw-artifact-cache" / _sha(data)
    assert first.read_bytes() == data
    assert _names(first.parent) == [_sha(data)]  # no part file, no lock left behind
    again = ctx.artifacts.input_path("report")
    assert again == first
    assert len(relay_server.requests) == 1  # the second call made no HTTP request


def test_the_cache_lives_outside_the_scope_when_the_host_says_so(relay_server, tmp_path):
    """`rwtask serve` wipes a stateless scope after every request, so the
    cache cannot sit in it: the host hands every Context the same directory."""
    data = b"shared across requests\n" * 100
    relay_server.script("GET", IN_PREFIX, _served(data))
    shared = tmp_path / "cache"
    first_ctx = _ctx(relay_server, tmp_path, [_decl("report", data)], cache_dir=shared, scope="s1")
    first = first_ctx.artifacts.input_path("report")

    shutil.rmtree(first_ctx.workdir)  # the stateless wipe

    second_ctx = _ctx(relay_server, tmp_path, [_decl("report", data)], cache_dir=shared, scope="s2")
    second = second_ctx.artifacts.input_path("report")

    assert first == second == shared / _sha(data)
    assert second.read_bytes() == data
    assert len(relay_server.requests) == 1
    assert not (second_ctx.workdir / ".rw-artifact-cache").exists()


def test_input_path_rejects_sha_mismatch(relay_server, tmp_path):
    data = b"what the relay actually sent"
    claimed = _sha(b"what the platform recorded")
    relay_server.script(
        "GET", IN_PREFIX, Response(200, body=data, headers={"X-RW-Sha256": claimed})
    )
    decl = InputArtifactDecl(input="report", sizeBytes=len(data), sha256=claimed)
    ctx = _ctx(relay_server, tmp_path, [decl])

    with pytest.raises(ArtifactError) as excinfo:
        ctx.artifacts.input_path("report")

    assert excinfo.value.code == "E_ARTIFACT_CORRUPT"
    assert claimed in str(excinfo.value)  # expected
    assert _sha(data) in str(excinfo.value)  # actual
    assert _names(ctx.workdir / ".rw-artifact-cache") == []  # no entry, no part file, no lock


def test_a_failed_download_is_not_cached_and_the_next_call_retries(relay_server, tmp_path):
    good = b"the real bytes"
    relay_server.script("GET", IN_PREFIX, Response(200, body=b"garbled in transit!"), _served(good))
    ctx = _ctx(relay_server, tmp_path, [_decl("report", good)])

    with pytest.raises(ArtifactError) as excinfo:
        ctx.artifacts.input_path("report")
    assert excinfo.value.code == "E_ARTIFACT_CORRUPT"

    assert ctx.artifacts.input_path("report").read_bytes() == good
    assert len(relay_server.requests) == 2


def test_an_interrupted_download_leaves_nothing_behind(relay_server, tmp_path):
    data = b"y" * 100_000
    short = Response(
        200,
        stream=iter([data[:50_000]]),
        content_length=len(data),
        headers={"X-RW-Sha256": _sha(data)},
    )
    relay_server.script("GET", IN_PREFIX, short, _served(data))
    ctx = _ctx(relay_server, tmp_path, [_decl("report", data)])

    with pytest.raises(urllib3.exceptions.HTTPError):  # the HTTP layer's own error, unchanged
        ctx.artifacts.input_path("report")

    cache = ctx.workdir / ".rw-artifact-cache"
    assert _names(cache) == []  # no half file, and the lock is released
    assert ctx.artifacts.input_path("report").read_bytes() == data


def test_a_malformed_sha_is_refused_before_it_can_name_a_file(relay_server, tmp_path):
    decl = InputArtifactDecl(input="report", sizeBytes=3, sha256="../../escape")
    ctx = _ctx(relay_server, tmp_path, [decl])

    with pytest.raises(ArtifactError) as excinfo:
        ctx.artifacts.input_path("report")

    assert excinfo.value.code == "E_ARTIFACT_RESPONSE"
    assert relay_server.requests == []
    assert not (tmp_path / "escape").exists()


def test_without_a_declared_sha_the_response_header_names_and_verifies_the_entry(
    relay_server, tmp_path
):
    data = b"no sha on the lease\n" * 100
    for _ in range(2):
        relay_server.script("GET", IN_PREFIX, _served(data))
    decl = InputArtifactDecl(input="report", sizeBytes=len(data))  # sha256 ""
    ctx = _ctx(relay_server, tmp_path, [decl])

    first = ctx.artifacts.input_path("report")
    inode = first.stat().st_ino
    again = ctx.artifacts.input_path("report")

    assert first == ctx.workdir / ".rw-artifact-cache" / _sha(data)
    assert first.read_bytes() == data
    assert again == first
    assert again.stat().st_ino == inode  # the cached entry was kept, not rewritten
    assert len(relay_server.requests) == 2  # the key is only known from the answer


def test_without_a_declared_sha_a_lying_header_is_corrupt(relay_server, tmp_path):
    data = b"payload"
    relay_server.script(
        "GET", IN_PREFIX, Response(200, body=data, headers={"X-RW-Sha256": _sha(b"other")})
    )
    ctx = _ctx(relay_server, tmp_path, [InputArtifactDecl(input="report")])

    with pytest.raises(ArtifactError) as excinfo:
        ctx.artifacts.input_path("report")

    assert excinfo.value.code == "E_ARTIFACT_CORRUPT"
    assert _names(ctx.workdir / ".rw-artifact-cache") == []


def test_without_any_sha_the_entry_is_keyed_by_the_computed_hash(relay_server, tmp_path):
    data = b"nobody told us the hash"
    relay_server.script("GET", IN_PREFIX, Response(200, body=data))
    ctx = _ctx(relay_server, tmp_path, [InputArtifactDecl(input="report")])

    path = ctx.artifacts.input_path("report")

    assert path == ctx.workdir / ".rw-artifact-cache" / _sha(data)
    assert path.read_bytes() == data
    assert _names(path.parent) == [_sha(data)]


# -- eviction -----------------------------------------------------------------


def test_cache_evicts_lru_over_budget(relay_server, tmp_path, monkeypatch):
    monkeypatch.setenv("RW_ARTIFACT_CACHE_MAX_BYTES", "2500")
    cache = tmp_path / "cache"
    blobs = {name: name.encode() * 1000 for name in "abc"}  # 1000 bytes each
    a = _fetch(relay_server, tmp_path, cache, "a", blobs["a"])
    b = _fetch(relay_server, tmp_path, cache, "b", blobs["b"])
    _age(a, 2000)
    _age(b, 1000)
    assert a.exists() and b.exists()  # 2000 bytes: inside the budget

    c = _fetch(relay_server, tmp_path, cache, "c", blobs["c"])  # 3000: one must go

    assert not a.exists()  # the least recently used
    assert b.exists() and c.exists()


def test_a_cache_hit_counts_as_a_use(relay_server, tmp_path, monkeypatch):
    """LRU cannot lean on the kernel updating atime (noatime and relatime
    mounts mostly do not): a hit refreshes the entry itself."""
    monkeypatch.setenv("RW_ARTIFACT_CACHE_MAX_BYTES", "2500")
    cache = tmp_path / "cache"
    blobs = {name: name.encode() * 1000 for name in "abc"}
    a = _fetch(relay_server, tmp_path, cache, "a", blobs["a"])
    b = _fetch(relay_server, tmp_path, cache, "b", blobs["b"])
    _age(a, 2000)  # a was created first and is the older...
    _age(b, 1000)
    requests_before = len(relay_server.requests)

    hit = _fetch(relay_server, tmp_path, cache, "a", blobs["a"], serve=False)  # ...until used again

    assert hit == a
    assert len(relay_server.requests) == requests_before  # served from the cache
    assert time.time() - a.stat().st_atime < 60
    assert time.time() - a.stat().st_mtime < 60
    c = _fetch(relay_server, tmp_path, cache, "c", blobs["c"])
    assert a.exists() and c.exists()
    assert not b.exists()  # b is now the one nobody used for the longest


def test_an_entry_handed_to_the_running_task_is_not_evicted(relay_server, tmp_path, monkeypatch):
    """The task may not have opened the path it was given yet, so the entries
    this task holds stay -- and the cache stays over budget rather than pull a
    file out from under it. A different task's cache has no such claim."""
    monkeypatch.setenv("RW_ARTIFACT_CACHE_MAX_BYTES", "1500")
    cache = tmp_path / "cache"
    a_data, b_data, c_data = (name.encode() * 1000 for name in "abc")
    for data in (a_data, b_data):
        relay_server.script("GET", IN_PREFIX, _served(data))
    ctx = _ctx(relay_server, tmp_path, [_decl("a", a_data), _decl("b", b_data)], cache_dir=cache)

    a = ctx.artifacts.input_path("a")
    _age(a, 2000)
    b = ctx.artifacts.input_path("b")

    assert a.exists() and b.exists()  # 2000 bytes against a 1500 budget: both are this task's

    c = _fetch(relay_server, tmp_path, cache, "c", c_data)  # another request
    assert not a.exists() and not b.exists()  # no longer anyone's, oldest first
    assert c.exists()  # just added: never the victim, whatever the budget


def test_the_entry_just_added_survives_even_when_it_alone_exceeds_the_budget(
    relay_server, tmp_path, monkeypatch
):
    monkeypatch.setenv("RW_ARTIFACT_CACHE_MAX_BYTES", "10")
    cache = tmp_path / "cache"

    big = _fetch(relay_server, tmp_path, cache, "big", b"z" * 5000)

    assert big.read_bytes() == b"z" * 5000


def test_orphaned_part_files_are_swept_but_a_recent_one_is_left(relay_server, tmp_path):
    """A host killed mid-download (an OOM kill, a container restart onto the
    same volume) leaves its part file; nothing else would ever remove it."""
    cache = tmp_path / "cache"
    cache.mkdir()
    orphan = cache / ("a" * 64 + ".part-deadbeef")
    live = cache / ("b" * 64 + ".part-cafe")
    for part in (orphan, live):
        part.write_bytes(b"x" * 100)
    _age(orphan, 7200)

    _fetch(relay_server, tmp_path, cache, "report", b"fresh")

    assert not orphan.exists()
    assert live.exists()  # might be another download in flight


def test_the_cache_budget_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("RW_ARTIFACT_CACHE_MAX_BYTES", raising=False)
    assert cache_module.max_bytes_from_env() == 1 << 30

    monkeypatch.setenv("RW_ARTIFACT_CACHE_MAX_BYTES", "123456")
    assert cache_module.max_bytes_from_env() == 123456

    for junk in ("", "lots", "-5", "1.5"):
        monkeypatch.setenv("RW_ARTIFACT_CACHE_MAX_BYTES", junk)
        assert cache_module.max_bytes_from_env() == 1 << 30


# -- locking ------------------------------------------------------------------


def _gated(data: bytes, release: threading.Event):
    """The body in two halves; the second is not sent until `release` is set,
    so a download can be held in flight for as long as a test wants."""
    half = len(data) // 2
    yield data[:half]
    assert release.wait(20), "the test never released the download"
    yield data[half:]


def test_concurrent_calls_for_one_sha_download_it_once(relay_server, tmp_path):
    data = b"one download, many readers\n" * 4000
    release = threading.Event()
    relay_server.script(
        "GET",
        IN_PREFIX,
        Response(
            200,
            stream=_gated(data, release),
            content_length=len(data),
            headers={"X-RW-Sha256": _sha(data)},
        ),
    )
    cache = tmp_path / "cache"
    ctxs = [
        _ctx(relay_server, tmp_path, [_decl("report", data)], cache_dir=cache, scope=f"s{i}")
        for i in range(4)
    ]
    paths: list = [None] * 4
    errors: list = []

    def work(i: int) -> None:
        try:
            paths[i] = ctxs[i].artifacts.input_path("report")
        except BaseException as exc:  # noqa: BLE001 -- asserted below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(4)]
    for thread in threads:
        thread.start()
    _wait_for_requests(relay_server, 1)
    time.sleep(0.5)  # the other three are now queued behind the first one's lock

    assert len(relay_server.requests) == 1
    release.set()
    for thread in threads:
        thread.join(20)
        assert not thread.is_alive()

    assert errors == []
    assert len(relay_server.requests) == 1  # one download served all four
    assert paths == [cache / _sha(data)] * 4
    assert paths[0].read_bytes() == data
    assert _names(cache) == [_sha(data)]  # and the lock is gone


def _lock_path(cache: Path, data: bytes) -> Path:
    return cache / f"{_sha(data)}.lock"


def test_a_live_lock_is_waited_for_until_it_is_gone(relay_server, tmp_path):
    data = b"someone else is downloading this"
    relay_server.script("GET", IN_PREFIX, _served(data))
    cache = tmp_path / "cache"
    cache.mkdir()
    lock = _lock_path(cache, data)
    lock.write_text(str(os.getpid()))  # a live owner: this very process
    ctx = _ctx(relay_server, tmp_path, [_decl("report", data)], cache_dir=cache)
    outcome: list = []
    thread = threading.Thread(
        target=lambda: outcome.append(ctx.artifacts.input_path("report")), daemon=True
    )

    thread.start()
    time.sleep(0.4)
    assert thread.is_alive()
    assert relay_server.requests == []  # held back by the lock

    lock.unlink()
    thread.join(10)

    assert outcome == [cache / _sha(data)]
    assert outcome[0].read_bytes() == data


def test_a_lock_left_by_a_dead_process_is_broken(relay_server, tmp_path):
    """A container restarted onto the same volume must not wait forever on a
    lock whose owner died with the old container."""
    data = b"the previous process died mid-download"
    relay_server.script("GET", IN_PREFIX, _served(data))
    cache = tmp_path / "cache"
    cache.mkdir()
    lock = _lock_path(cache, data)
    lock.write_text(str(_dead_pid()))
    ctx = _ctx(relay_server, tmp_path, [_decl("report", data)], cache_dir=cache)

    path, error = _run_with_deadline(lambda: ctx.artifacts.input_path("report"), 10)

    assert error is None
    assert path.read_bytes() == data
    assert not lock.exists()


def test_a_lock_stays_valid_longer_than_a_download_may_sit_silent():
    """A live owner waiting on a paused relay touches nothing for up to the
    input GET's read timeout; a lock judged stale sooner would be broken under
    it and the artifact downloaded twice."""
    assert cache_module._LOCK_STALE_SECONDS > relay_module._READ_TIMEOUT


def test_a_live_owners_quiet_lock_is_waited_for_past_a_minute(relay_server, tmp_path):
    data = b"the relay is slow, the owner is alive"
    relay_server.script("GET", IN_PREFIX, _served(data))
    cache = tmp_path / "cache"
    cache.mkdir()
    lock = _lock_path(cache, data)
    lock.write_text(str(os.getpid()))  # a live owner
    _age(lock, 100)  # silent for 100 s: within what a paused relay can cause
    ctx = _ctx(relay_server, tmp_path, [_decl("report", data)], cache_dir=cache)
    outcome: list = []
    thread = threading.Thread(
        target=lambda: outcome.append(ctx.artifacts.input_path("report")), daemon=True
    )

    thread.start()
    time.sleep(0.4)
    assert thread.is_alive()
    assert relay_server.requests == []  # the lock was respected, not broken

    lock.unlink()
    thread.join(10)
    assert outcome == [cache / _sha(data)]


def test_an_unreadable_old_lock_is_broken(relay_server, tmp_path):
    data = b"the lock file is empty"
    relay_server.script("GET", IN_PREFIX, _served(data))
    cache = tmp_path / "cache"
    cache.mkdir()
    lock = _lock_path(cache, data)
    lock.write_text("")
    _age(lock, 600)
    ctx = _ctx(relay_server, tmp_path, [_decl("report", data)], cache_dir=cache)

    path, error = _run_with_deadline(lambda: ctx.artifacts.input_path("report"), 10)

    assert error is None
    assert path.read_bytes() == data


def test_a_lock_nobody_has_touched_for_a_long_time_is_broken_even_if_its_pid_is_alive(
    relay_server, tmp_path
):
    """A restarted container's processes get the same small pids as the old
    ones, so a pid that looks alive proves nothing about a lock this old: a
    downloading holder keeps touching its lock."""
    data = b"a pid that was reused"
    relay_server.script("GET", IN_PREFIX, _served(data))
    cache = tmp_path / "cache"
    cache.mkdir()
    lock = _lock_path(cache, data)
    lock.write_text(str(os.getpid()))
    _age(lock, 3600)
    ctx = _ctx(relay_server, tmp_path, [_decl("report", data)], cache_dir=cache)

    path, error = _run_with_deadline(lambda: ctx.artifacts.input_path("report"), 10)

    assert error is None
    assert path.read_bytes() == data


def test_a_waiter_that_queued_with_an_open_response_asks_again_if_the_owner_failed(
    relay_server, tmp_path
):
    """With no declared sha the key is only known once the relay has
    answered, so a second caller holds an open response while it waits for
    the first one's lock. If the first fails, the second must not read from a
    response that sat idle all that time: it asks again."""
    good = b"the bytes that hash to the header\n" * 100
    garbled = b"x" * len(good)
    release = threading.Event()
    headers = {"X-RW-Sha256": _sha(good)}
    relay_server.script(
        "GET",
        IN_PREFIX,
        Response(
            200, stream=_gated(garbled, release), content_length=len(garbled), headers=headers
        ),
        Response(200, body=good, headers=headers),
        Response(200, body=good, headers=headers),
    )
    cache = tmp_path / "cache"
    decl = InputArtifactDecl(input="report")  # no sha256
    first, second = (
        _ctx(relay_server, tmp_path, [decl], cache_dir=cache, scope=f"s{i}") for i in range(2)
    )
    outcome: dict = {}

    def run(key: str, ctx: Context) -> None:
        try:
            outcome[key] = ctx.artifacts.input_path("report")
        except BaseException as exc:  # noqa: BLE001 -- asserted below
            outcome[key] = exc

    owner = threading.Thread(target=run, args=("first", first), daemon=True)
    owner.start()
    # With no declared sha the owner sends its GET before it takes the lock, so
    # a request on the wire proves nothing: wait for the lock itself, which it
    # holds from there until its download ends (gated here).
    _wait_for(lambda: _lock_path(cache, good).exists())
    waiter = threading.Thread(target=run, args=("second", second), daemon=True)
    waiter.start()
    _wait_for_requests(relay_server, 2)
    time.sleep(0.4)  # the second has its answer and is queued on the lock

    release.set()
    owner.join(20)
    waiter.join(20)

    assert isinstance(outcome["first"], ArtifactError)
    assert outcome["first"].code == "E_ARTIFACT_CORRUPT"
    assert outcome["second"] == cache / _sha(good)
    assert outcome["second"].read_bytes() == good
    assert len(relay_server.requests) == 3  # the waiter's second request
    assert _names(cache) == [_sha(good)]


def test_a_declared_sha_in_capitals_names_the_same_entry(relay_server, tmp_path):
    data = b"hex is case-insensitive"
    relay_server.script("GET", IN_PREFIX, _served(data))
    decl = InputArtifactDecl(input="report", sizeBytes=len(data), sha256=_sha(data).upper())
    ctx = _ctx(relay_server, tmp_path, [decl])

    path = ctx.artifacts.input_path("report")

    assert path.name == _sha(data)
    assert path.read_bytes() == data


# -- fix round 1 ----------------------------------------------------------------


@pytest.mark.parametrize(("pause", "completes"), [(0.3, True), (2.0, False)])
def test_a_relay_that_goes_quiet_mid_body_fails_the_read_instead_of_hanging(
    relay_server, tmp_path, monkeypatch, pause, completes
):
    """The input GET has a per-read timeout, so a half-open relay connection
    cannot hang a task (and serve's sequential loop with it) for ever. With it
    at 1 s, a 0.3 s pause mid-body is fine and a 2 s one fails the read -- with
    no entry published and no lock left behind."""
    monkeypatch.setattr(relay_module, "_READ_TIMEOUT", 1)
    data = b"q" * (2 * MIB)

    def pausing():
        yield data[:MIB]
        time.sleep(pause)
        yield data[MIB:]

    relay_server.script(
        "GET",
        IN_PREFIX,
        Response(
            200, stream=pausing(), content_length=len(data), headers={"X-RW-Sha256": _sha(data)}
        ),
    )
    ctx = _ctx(relay_server, tmp_path, [_decl("report", data)])
    cache = ctx.workdir / ".rw-artifact-cache"

    if completes:
        assert ctx.artifacts.input_path("report").read_bytes() == data
    else:
        with pytest.raises(urllib3.exceptions.HTTPError):
            ctx.artifacts.input_path("report")
        assert _names(cache) == []


def _evict_after(monkeypatch, method: str) -> None:
    """Makes ArtifactCache.`method` run the cache's trim right after it
    returns -- what another thread of the same task, finishing its own
    download in that gap, would do."""
    original = getattr(cache_module.ArtifactCache, method)

    def with_a_trim(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        self._evict()
        return result

    monkeypatch.setattr(cache_module.ArtifactCache, method, with_a_trim)


def test_an_entry_just_published_cannot_be_trimmed_before_it_is_handed_out(
    relay_server, tmp_path, monkeypatch
):
    monkeypatch.setenv("RW_ARTIFACT_CACHE_MAX_BYTES", "10")  # every entry is over budget
    _evict_after(monkeypatch, "_publish")
    cache = tmp_path / "cache"

    path = _fetch(relay_server, tmp_path, cache, "a", b"a" * 1000)

    assert path.read_bytes() == b"a" * 1000


def test_an_entry_just_published_without_a_declared_sha_is_pinned_too(
    relay_server, tmp_path, monkeypatch
):
    monkeypatch.setenv("RW_ARTIFACT_CACHE_MAX_BYTES", "10")
    _evict_after(monkeypatch, "_publish")
    data = b"keyed by what arrived"
    relay_server.script("GET", IN_PREFIX, Response(200, body=data))
    ctx = _ctx(relay_server, tmp_path, [InputArtifactDecl(input="report")])

    assert ctx.artifacts.input_path("report").read_bytes() == data


def test_an_entry_just_touched_cannot_be_trimmed_before_it_is_handed_out(
    relay_server, tmp_path, monkeypatch
):
    monkeypatch.setenv("RW_ARTIFACT_CACHE_MAX_BYTES", "10")
    cache = tmp_path / "cache"
    first = _fetch(relay_server, tmp_path, cache, "a", b"a" * 1000)
    _evict_after(monkeypatch, "_touch")

    again = _fetch(relay_server, tmp_path, cache, "a", b"a" * 1000, serve=False)

    assert again == first
    assert again.read_bytes() == b"a" * 1000


def test_the_lock_is_touched_while_a_download_goes_on(relay_server, tmp_path, monkeypatch):
    """Waiters judge a lock alive by its mtime (see the module docstring), so
    the downloader must keep it fresh. The first MiB is sent only after the
    lock has been aged; the rest is held back until the test has seen the
    lock touched."""
    monkeypatch.setattr(cache_module, "_LOCK_HEARTBEAT_SECONDS", 0)
    data = b"h" * (2 * MIB)
    first_mib, rest = threading.Event(), threading.Event()

    def gated():
        assert first_mib.wait(20)
        yield data[:MIB]
        assert rest.wait(20)
        yield data[MIB:]

    relay_server.script(
        "GET",
        IN_PREFIX,
        Response(
            200, stream=gated(), content_length=len(data), headers={"X-RW-Sha256": _sha(data)}
        ),
    )
    cache = tmp_path / "cache"
    ctx = _ctx(relay_server, tmp_path, [_decl("report", data)], cache_dir=cache)
    lock = _lock_path(cache, data)
    outcome: list = []
    thread = threading.Thread(
        target=lambda: outcome.append(ctx.artifacts.input_path("report")), daemon=True
    )

    thread.start()
    _wait_for(lock.exists)
    _age(lock, 30)
    first_mib.set()
    _wait_for(lambda: time.time() - lock.stat().st_mtime < 5)  # touched, mid-download
    assert thread.is_alive()

    rest.set()
    thread.join(20)
    assert outcome == [cache / _sha(data)]


def test_housekeeping_failures_do_not_fail_a_good_download(
    relay_server, tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("RW_ARTIFACT_CACHE_MAX_BYTES", "1500")
    cache = tmp_path / "cache"
    old = _fetch(relay_server, tmp_path, cache, "a", b"a" * 1000)
    _age(old, 1000)
    real_unlink = Path.unlink

    def refuse_the_old_entry(self, *args, **kwargs):
        if self == old:
            raise PermissionError(13, "Operation not permitted", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_the_old_entry)

    with caplog.at_level("WARNING"):
        new = _fetch(relay_server, tmp_path, cache, "b", b"b" * 1000)  # over budget: evicts `old`

    assert new.read_bytes() == b"b" * 1000
    assert old.exists()  # it could not be removed, and that was only noted
    assert "Operation not permitted" in caplog.text


def test_a_cache_directory_that_cannot_be_listed_does_not_fail_a_good_download(
    relay_server, tmp_path, monkeypatch, caplog
):
    def refuse(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(cache_module.os, "scandir", refuse)
    cache = tmp_path / "cache"

    with caplog.at_level("WARNING"):
        path = _fetch(relay_server, tmp_path, cache, "a", b"a" * 1000)

    assert path.read_bytes() == b"a" * 1000
    assert "Permission denied" in caplog.text


def test_housekeeping_removes_locks_whose_owner_is_gone_and_keeps_live_ones(relay_server, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    dead = cache / ("c" * 64 + ".lock")
    dead.write_text(str(_dead_pid()))
    live = cache / ("d" * 64 + ".lock")
    live.write_text(str(os.getpid()))

    _fetch(relay_server, tmp_path, cache, "a", b"a" * 1000)

    assert not dead.exists()
    assert live.exists()


def test_published_entries_are_read_only(relay_server, tmp_path):
    """A task that edits its input in place would poison the entry for every
    later request that finds it by hash; the mode keeps it from doing so by
    accident. (Eviction needs the directory's permission, not the file's.)"""
    data = b"do not edit me"
    relay_server.script("GET", IN_PREFIX, _served(data))
    ctx = _ctx(relay_server, tmp_path, [_decl("report", data)])

    path = ctx.artifacts.input_path("report")

    assert path.stat().st_mode & 0o222 == 0
    assert path.read_bytes() == data
    assert ctx.artifacts.input_path("report").stat().st_mode & 0o222 == 0  # and on a hit


def test_a_read_only_entry_can_still_be_evicted(relay_server, tmp_path, monkeypatch):
    monkeypatch.setenv("RW_ARTIFACT_CACHE_MAX_BYTES", "1500")
    cache = tmp_path / "cache"
    old = _fetch(relay_server, tmp_path, cache, "a", b"a" * 1000)
    _age(old, 1000)

    new = _fetch(relay_server, tmp_path, cache, "b", b"b" * 1000)

    assert not old.exists()
    assert new.exists()


# -- memory -------------------------------------------------------------------


def test_input_path_memory_stays_bounded(relay_server, tmp_path):
    """Downloads 32 MiB and checks the whole path -- client, hashing, the
    fixture's own sender -- never holds the body. The fixture serves the body
    lazily (`PieceStream`: 64 KiB pieces made on demand, only the count and
    digest kept), so the tracemalloc peak, which counts every thread, is the
    client's alone plus one piece.

    Measured peak: 2.2 MiB, the same on every run (the 1 MiB read and the
    HTTP layer's working copy of it). The bound is 5 MiB: over twice the
    measurement for allocator noise and library versions, and a sixth of the
    32 MiB a buffering client would hold."""
    total = 32 * MIB
    reference = PieceStream(total)
    for _ in reference:
        pass
    sha = reference.sha256
    served = PieceStream(total)
    relay_server.script(
        "GET", IN_PREFIX, Response(200, stream=served, headers={"X-RW-Sha256": sha})
    )
    ctx = _ctx(
        relay_server, tmp_path, [InputArtifactDecl(input="big", sizeBytes=total, sha256=sha)]
    )
    assert served.sent == 0  # nothing was produced when the response was scripted

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        path = ctx.artifacts.input_path("big")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    print(f"input_path tracemalloc peak for {total // MIB} MiB: {peak / MIB:.2f} MiB")
    assert served.sent == total  # every piece was pulled on demand, none held back
    assert path.stat().st_size == total
    assert path == ctx.workdir / ".rw-artifact-cache" / sha
    assert peak < 5 * MIB, f"peak {peak / MIB:.1f} MiB for a {total // MIB} MiB download"


# -- local mode ---------------------------------------------------------------


def test_local_mode_reads_inputs_dir(tmp_path):
    ctx = _local_ctx(tmp_path)
    inputs = ctx.workdir / ".rw-artifacts" / "inputs"
    (inputs / "sub").mkdir(parents=True)
    (inputs / "data.csv").write_bytes(b"a,b\n1,2\n")
    (inputs / "sub" / "deep.txt").write_bytes(b"deep")

    with ctx.artifacts.input("data.csv") as fh:
        assert fh.read() == b"a,b\n1,2\n"
    assert ctx.artifacts.input("sub/deep.txt").read() == b"deep"

    path = ctx.artifacts.input_path("data.csv")
    assert path == inputs / "data.csv"  # as is: no copy, no cache
    assert path.read_bytes() == b"a,b\n1,2\n"
    assert not (ctx.workdir / ".rw-artifact-cache").exists()


def test_local_mode_missing_input_is_undeclared(tmp_path):
    ctx = _local_ctx(tmp_path)
    (ctx.workdir / ".rw-artifacts" / "inputs" / "adir").mkdir(parents=True)

    for name in ("absent.csv", "adir"):
        with pytest.raises(ArtifactUndeclaredError) as excinfo:
            ctx.artifacts.input(name)
        assert name in str(excinfo.value)
        with pytest.raises(ArtifactUndeclaredError):
            ctx.artifacts.input_path(name)


@pytest.mark.parametrize("bad", ["", "/etc/passwd", "../outside", "a/../b", "a//b", "./x"])
def test_local_mode_rejects_unsafe_names(tmp_path, bad):
    ctx = _local_ctx(tmp_path)

    with pytest.raises(ArtifactError) as excinfo:
        ctx.artifacts.input(bad)
    assert excinfo.value.code == "E_ARTIFACT_PATH"
    with pytest.raises(ArtifactError) as excinfo:
        ctx.artifacts.input_path(bad)
    assert excinfo.value.code == "E_ARTIFACT_PATH"


# -- host wiring --------------------------------------------------------------

_TASKS = """
from runwhen_capability import task


@task(outputs={"path": "text"})
def where(ctx):
    return {"path": str(ctx.artifacts.input_path("data"))}
"""


def test_run_request_gives_tasks_the_inputs_and_the_cache_dir(relay_server, tmp_path):
    cap_dir = tmp_path / "cap"
    cap_dir.mkdir()
    (cap_dir / "manifest.yaml").write_text('capability: rw-inputs-fixture\nversion: "0.0.0"\n')
    (cap_dir / "tasks.py").write_text(_TASKS)
    data = b"bound to the task's input"
    relay_server.script("GET", IN_PREFIX, _served(data))
    scope = tmp_path / "scope"
    scope.mkdir()
    cache = tmp_path / "cache-above-the-scopes"

    result = run_request(
        load_capability(cap_dir),
        RequestEnvelope.model_validate({"version": 1, "tasks": [{"task": "where", "inputs": {}}]}),
        credentials={},
        scope_dir=scope,
        relay=_relay(relay_server, tmp_path),
        input_artifacts=[_decl("data", data)],
        artifact_cache_dir=cache,
    )

    (task_result,) = result.tasks
    assert task_result.status == "ok", task_result.error
    assert task_result.outputs == {"path": str(cache / _sha(data))}
    assert (cache / _sha(data)).read_bytes() == data


def test_the_default_cache_dir_name_is_the_documented_one():
    assert artifacts.ARTIFACT_CACHE_DIR == ".rw-artifact-cache"
