"""A real local HTTP server standing in for the runner's relay, for tests that
need to see actual bytes on the wire (streamed/chunked uploads, early
responses). `responses` cannot be shown to intercept a streamed PUT, so every
artifact test uses this instead. The `relay_server` pytest fixture lives in
conftest.py; this module holds the pieces so a test can also import the
helper types (`Response`):

    def test_something(relay_server):
        relay_server.script("PUT", "/v1/tasks/r1/artifacts/", Response(201, json={...}))
        ... point a RelayClient at relay_server.url ...
        assert relay_server.requests[0].body == b"..."

What it does:

- Listens on 127.0.0.1 on a free port (`.url`), one thread per connection.
- Records every request as a `RecordedRequest`: method, path, headers, the
  body length and sha256, whether the body arrived complete, and -- only while
  small -- the body bytes themselves (see "Body retention").
- Decodes `Transfer-Encoding: chunked` request bodies itself
  (BaseHTTPRequestHandler does not), so a chunked upload is seen exactly as the
  runner would see it, including whether the terminating 0-length chunk
  arrived (`body_complete`).
- Answers from a script: `script(method, path_prefix, *responses)` queues
  responses for requests whose method matches and whose path starts with
  `path_prefix`; they are used in order, and once a script is used up (or no
  script matches) the `default` response answers.

A silent runner. A `Response` with `delay=S` reads the whole request body, then waits S
seconds before it answers: a runner that takes the body and never replies (to a client
whose read timeout is shorter than S).

Early responses. The real runner answers `413` after reading only
`maxBytes + 1` body bytes, and `503` before reading any. A `Response` with
`read_limit=N` emulates that: the handler reads at most N body bytes, sends
the response, half-closes its side (`shutdown(SHUT_WR)`), then briefly
drains and discards whatever the client is still sending before closing.
That drain mimics Go's lingering close; without it the server's close()
with unread input makes the kernel send an RST, which can destroy the
response before the client reads it and makes a test flaky.

Body retention. Later tests upload tens of MiB, and the fixture must not
hold 50 MiB per request by default. A request's bytes are kept only while the
body is at most `keep_limit` (1 MiB by default); a bigger body leaves
`body` as None and `body_length`/`body_sha256` as the way to check it.
A test that wants the bytes of a bigger body sets `relay_server.keep_limit`
before sending.

Slow reader. `relay_server.read_delay = 0.001` makes the handler sleep that
long after every read it makes (64 KiB at most; the discarding drain of an
early response included), so a producer that is faster than the "network"
backs up deterministically -- what a bounded-queue test needs. Zero (the
default) is full speed.

A stall. `relay_server.stall_after = N` with `relay_server.stall_seconds = S`
makes each request's handler stop reading for S seconds once, right after the
body bytes read so far reach N -- a runner that pauses reading (to flush a
part to storage, say). A client sending more than the socket buffers hold
blocks in send for the whole pause.

An aborted upload. A client that drops the connection mid-body leaves the
handler a body that never reached its terminating chunk (`body_complete`
False); the answer it then tries to send has nobody to read it, and that
write failing is swallowed rather than printed as a server traceback.

Live progress. A request is recorded only once its body has been read, so a
test that must see bytes arrive WHILE an upload is still going on reads two
live counters instead: `started` lists (method, path) for every request whose
headers have arrived, before any of its body is read, and `bytes_received` is
the body bytes read so far across all requests.

Memory-bound tests. `PieceStream` produces a large body lazily, one piece at
a time, and keeps only its length and sha256 -- so a test can upload tens of
MiB, compare `RecordedRequest.body_length`/`body_sha256` against the stream's,
and measure (tracemalloc) that the code under test never held the whole body.

A streamed download. A `Response` with `stream=` sends the pieces of an
iterable as its body, pulling each from the iterator only when it is about to
write it (so the fixture, like the client under test, never holds the whole
body). The declared Content-Length is `content_length`, else the stream's
`total` (`PieceStream` has one). A `content_length` larger than what the
stream yields makes the connection end short of its length -- an interrupted
download.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_READ_CHUNK = 64 * 1024
# How long an early response keeps discarding the client's remaining input
# (each recv, and overall) before the connection is closed anyway.
_DRAIN_RECV_TIMEOUT = 0.5
_DRAIN_TOTAL_TIMEOUT = 5.0


@dataclass
class Response:
    """One scripted answer. `json` is serialised as the body; `body` (bytes)
    is sent verbatim instead (use it for a non-JSON error body or a download).
    `read_limit` makes this an early response -- see the module docstring: the
    request body is read for at most that many bytes, then the response goes
    out. None (the default) reads the whole body first, like an ordinary
    handler. `stream` (with `content_length`) sends a lazily produced body
    instead of `json`/`body` -- see "A streamed download"."""

    status: int = 200
    json: object = None
    body: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)
    read_limit: int | None = None
    stream: Iterable[bytes] | None = None
    content_length: int | None = None
    delay: float = 0.0  # seconds to wait, after the request is read, before answering


@dataclass
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]  # lower-cased names
    body: bytes | None  # None when the body outgrew keep_limit; see the module docstring
    body_length: int  # bytes actually read (less than sent, for an early response)
    body_sha256: str  # of the bytes actually read
    # The whole body arrived: Content-Length bytes, or a chunked body through
    # its terminating 0-length chunk. False for an early response that cut
    # reading short.
    body_complete: bool
    chunked: bool

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


class RelayServer:
    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.started: list[tuple[str, str]] = []  # see "Live progress"
        self.bytes_received = 0
        self.keep_limit = 1 << 20
        self.read_delay = 0.0  # seconds slept after each body read; see "Slow reader"
        self.stall_after: int | None = None  # see "A stall"
        self.stall_seconds = 0.0
        self.default = Response(404, json={"code": "E_UNSCRIPTED"})
        self._scripts: list[tuple[str, str, list[Response]]] = []
        self._lock = threading.Lock()
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def script(self, method: str, path_prefix: str, *responses: Response) -> None:
        """Queue `responses` for requests with this method and a path
        starting with `path_prefix`. Scripts are tried in the order they
        were registered; a script with nothing left is skipped."""
        with self._lock:
            self._scripts.append((method.upper(), path_prefix, list(responses)))

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> RelayServer:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- used by the handler ----------------------------------------------

    def _next_response(self, method: str, path: str) -> Response:
        with self._lock:
            for script_method, prefix, queue in self._scripts:
                if queue and script_method == method and path.startswith(prefix):
                    return queue.pop(0)
        return self.default

    def _pace(self, sink: _BodySink | None = None) -> None:
        if (
            sink is not None
            and self.stall_after is not None
            and not sink.stalled
            and sink.length >= self.stall_after
        ):
            sink.stalled = True
            time.sleep(self.stall_seconds)
        if self.read_delay:
            time.sleep(self.read_delay)

    def _record(self, request: RecordedRequest) -> None:
        with self._lock:
            self.requests.append(request)

    def _started(self, method: str, path: str) -> None:
        with self._lock:
            self.started.append((method, path))

    def _received(self, count: int) -> None:
        with self._lock:
            self.bytes_received += count


def _make_handler(server: RelayServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.1 like the real runner; every response says `Connection:
        # close`, so one connection serves exactly one request.
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):  # noqa: A002 -- silence per-request stderr noise
            pass

        def _handle(self) -> None:
            method, path = self.command, self.path
            server._started(method, path)
            response = server._next_response(method, path)

            chunked = "chunked" in self.headers.get("Transfer-Encoding", "").lower()
            keep = server.keep_limit
            sink = _BodySink(keep)
            if chunked:
                complete = self._read_chunked(sink, response.read_limit)
            else:
                complete = self._read_sized(sink, response.read_limit)

            server._record(
                RecordedRequest(
                    method=method,
                    path=path,
                    headers={k.lower(): v for k, v in self.headers.items()},
                    body=sink.bytes(),
                    body_length=sink.length,
                    body_sha256=sink.digest.hexdigest(),
                    body_complete=complete,
                    chunked=chunked,
                )
            )

            if response.delay:
                time.sleep(response.delay)
            payload = response.body
            if payload is None and response.json is not None:
                payload = json.dumps(response.json).encode()
            payload = payload or b""
            length = len(payload)
            if response.stream is not None:
                length = response.content_length
                if length is None:
                    length = response.stream.total  # type: ignore[attr-defined]
            self.close_connection = True
            try:
                self.send_response(response.status)
                if response.json is not None and response.body is None:
                    self.send_header("Content-Type", "application/json")
                for name, value in response.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(length))
                self.send_header("Connection", "close")
                self.end_headers()
                if response.stream is not None:
                    for piece in response.stream:
                        self.wfile.write(piece)
                else:
                    self.wfile.write(payload)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return  # the client is gone (an aborted upload); the request is recorded

            if not complete:
                self._linger()

        # An early response left body bytes unread. Half-close our side so the
        # client sees the response end, then swallow whatever it is still
        # sending until it finishes, closes, or the drain times out.
        def _linger(self) -> None:
            conn = self.connection
            try:
                conn.shutdown(socket.SHUT_WR)
                conn.settimeout(_DRAIN_RECV_TIMEOUT)
                deadline = time.monotonic() + _DRAIN_TOTAL_TIMEOUT
                while time.monotonic() < deadline:
                    if not conn.recv(_READ_CHUNK):
                        break
                    server._pace()
            except OSError:
                pass  # reset or timed out: the response was already delivered

        def _read_sized(self, sink: _BodySink, limit: int | None) -> bool:
            remaining = int(self.headers.get("Content-Length") or 0)
            while remaining > 0:
                want = min(remaining, _READ_CHUNK)
                if limit is not None:
                    want = min(want, limit - sink.length)
                    if want <= 0:
                        return False
                data = self.rfile.read(want)
                if not data:
                    return False
                sink.add(data)
                server._received(len(data))
                remaining -= len(data)
                server._pace(sink)
            return True

        def _read_chunked(self, sink: _BodySink, limit: int | None) -> bool:
            """Decodes a chunked body. True only if the terminating 0-length
            chunk (and its trailer section) was read."""
            while True:
                if limit is not None and sink.length >= limit:
                    return False
                size_line = self.rfile.readline(1024)
                if not size_line:
                    return False
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    # Trailers, up to the blank line that ends the body.
                    while self.rfile.readline(1024).strip():
                        pass
                    return True
                remaining = size
                while remaining > 0:
                    want = min(remaining, _READ_CHUNK)
                    if limit is not None:
                        want = min(want, limit - sink.length)
                        if want <= 0:
                            return False
                    data = self.rfile.read(want)
                    if not data:
                        return False
                    sink.add(data)
                    server._received(len(data))
                    remaining -= len(data)
                    server._pace(sink)
                self.rfile.readline(8)  # the CRLF after the chunk data

        do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = _handle

    return Handler


class PieceStream:
    """An iterator of `total` bytes in pieces of `piece_size`, each created
    only when asked for and dropped once yielded, so the stream itself holds
    one piece at a time. Piece `i` is filled with the byte `i % 251`, so a
    lost, repeated or reordered piece changes the sha256. After (or while)
    it is consumed, `.sent` is the bytes yielded so far and `.sha256` their
    hex digest -- what the server should have recorded in `body_length` and
    `body_sha256`."""

    def __init__(self, total: int, piece_size: int = 64 * 1024) -> None:
        self.total = total
        self.piece_size = piece_size
        self.sent = 0
        self._index = 0
        self._digest = hashlib.sha256()

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    def __iter__(self) -> PieceStream:
        return self

    def __next__(self) -> bytes:
        if self.sent >= self.total:
            raise StopIteration
        size = min(self.piece_size, self.total - self.sent)
        piece = bytes([self._index % 251]) * size
        self._index += 1
        self.sent += size
        self._digest.update(piece)
        return piece


class _BodySink:
    """Counts and hashes every body byte, but keeps the bytes only while the
    total stays within `keep` -- see "Body retention" in the module docstring."""

    def __init__(self, keep: int) -> None:
        self._keep = keep
        self._buf: bytearray | None = bytearray()
        self.length = 0
        self.digest = hashlib.sha256()
        self.stalled = False  # the one stall (RelayServer.stall_after) already happened

    def add(self, data: bytes) -> None:
        self.length += len(data)
        self.digest.update(data)
        if self._buf is not None:
            if self.length > self._keep:
                self._buf = None
            else:
                self._buf += data

    def bytes(self) -> bytes | None:
        return None if self._buf is None else bytes(self._buf)
