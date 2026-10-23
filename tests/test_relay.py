"""RelayClient against a real local server (tests/relay_server.py): the
chunked artifact PUT, the input GET, and the mapping of the relay's error
responses onto the artifact exceptions."""

from __future__ import annotations

import hashlib
import time
import tracemalloc
from pathlib import Path

import pytest
import requests
from relay_server import PieceStream, Response

from runwhen_capability import relay as relay_module
from runwhen_capability.errors import (
    ArtifactBusyError,
    ArtifactError,
    ArtifactExistsError,
    ArtifactLeaseLostError,
    ArtifactQuotaError,
    ArtifactsUnavailableError,
    ArtifactTooLargeError,
    ArtifactUndeclaredError,
    ResultTooLargeError,
)
from runwhen_capability.relay import RelayClient

CREATED = {
    "artifactRef": "art_abc",
    "sizeBytes": 9,
    "sha256": "0" * 64,
    "lineCount": 1,
    "isText": True,
}


def _client(relay_server, tmp_path: Path, token: str = "tok-1") -> RelayClient:
    token_file = tmp_path / "token"
    token_file.write_text(token)
    return RelayClient(relay_server.url, token_file, "req-1", requests.Session())


def _put(client: RelayClient, body, name: str = "report.txt") -> dict:
    return client.put_artifact(task="scan", name=name, content_type="text/plain", body=body)


def test_put_artifact_streams_chunked_body(relay_server, tmp_path):
    relay_server.script("PUT", "/v1/tasks/req-1/artifacts/", Response(201, json=CREATED))
    pieces = [b"abc", b"d" * 100_000, b"", b"efg"]  # the empty piece must not end the body early

    created = _put(_client(relay_server, tmp_path), iter(pieces))

    assert created == CREATED
    (seen,) = relay_server.requests
    assert seen.method == "PUT"
    assert seen.path == "/v1/tasks/req-1/artifacts/report.txt"
    assert seen.body == b"".join(pieces)
    assert seen.body_complete  # the terminating 0-length chunk arrived
    assert seen.chunked
    assert seen.header("Transfer-Encoding") == "chunked"
    assert seen.header("Content-Length") is None
    assert seen.header("X-RW-Task") == "scan"
    assert seen.header("Content-Type") == "text/plain"
    assert seen.header("Authorization") == "Bearer tok-1"


def test_put_artifact_streams_without_buffering_the_body(relay_server, tmp_path):
    """Pins put_artifact's buffer bound: 32 MiB goes up in 64 KiB pieces while
    the client's Python allocations peak at a few MiB, not at the body size.

    Guards against any regression that materialises the body -- `list(body)`,
    `b"".join(body)`, reading the iterator to size it -- which "chunked, no
    Content-Length" alone would not catch. The measured peak counts every
    thread, the fixture server's too (it keeps at most 1 MiB of body); a
    buffering client would hold the full 32 MiB. Later upload tasks reuse
    this shape for their own paths."""
    total = 32 * (1 << 20)
    relay_server.script("PUT", "/", Response(201, json=CREATED))
    stream = PieceStream(total, piece_size=64 * 1024)
    client = _client(relay_server, tmp_path)

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        _put(client, stream)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    (seen,) = relay_server.requests
    assert stream.sent == total
    assert seen.body_length == total
    assert seen.body_sha256 == stream.sha256
    assert seen.body is None  # over the fixture's keep_limit: it did not retain it either
    assert seen.chunked
    assert seen.body_complete
    assert peak < 8 * (1 << 20), f"peak {peak / (1 << 20):.1f} MiB while uploading 32 MiB"


def test_send_timeout_outlasts_the_runners_stall_timeout():
    """The runner may stop reading for up to its stall timeout (2 minutes)
    while it waits for an upload slot or flushes a part to storage, and a
    chunk waiting to be sent must outlive that. Guards against a regression to
    a small value; the next test pins what the value means."""
    assert relay_module._SEND_TIMEOUT >= 120


def test_response_timeout_outlasts_the_runners_whole_finalisation():
    """After the last body byte the runner finishes its final parts and completes the
    upload: three attempts of two minutes each, plus backoff, with its stall watchdog
    already stopped. The wait for its answer must outlast all of that."""
    assert relay_module._RESPONSE_TIMEOUT >= 420
    assert relay_module._RESPONSE_TIMEOUT > relay_module._SEND_TIMEOUT


def test_input_read_timeout_outlasts_the_runners_stall_timeout():
    """A download has a per-read timeout (a stalled connection must fail, not
    hang the task), and it must be longer than the runner's 2-minute stall
    timeout, or a runner that merely pauses would be cut off."""
    assert relay_module._READ_TIMEOUT >= 120


@pytest.mark.parametrize(("pause", "completes"), [(0.3, True), (2.0, False)])
def test_put_artifact_survives_a_relay_pause_shorter_than_the_send_timeout(
    relay_server, tmp_path, monkeypatch, pause, completes
):
    """urllib3 keeps the connect timeout (the first of the timeout tuple) on
    the socket while it sends the body, and sendall treats it as a limit on
    the whole call -- so it is the longest the relay may stop reading mid-body.
    With it at 1 s, a 0.3 s pause is fine and a 2 s one fails the upload."""
    monkeypatch.setattr(relay_module, "_SEND_TIMEOUT", 1)
    relay_server.stall_after = 1 << 20
    relay_server.stall_seconds = pause
    relay_server.script("PUT", "/", Response(201, json=CREATED))
    total = 64 * (1 << 20)  # far more than the socket buffers hold, so the sender blocks
    stream = PieceStream(total, piece_size=1 << 20)
    client = _client(relay_server, tmp_path)

    if completes:
        assert _put(client, stream) == CREATED
        (seen,) = relay_server.requests
        assert (seen.body_length, seen.body_sha256) == (total, stream.sha256)
        assert seen.body_complete
    else:
        with pytest.raises(requests.ConnectionError):
            _put(client, stream)


def test_put_artifact_gives_up_on_a_relay_that_takes_the_body_and_never_answers(
    relay_server, tmp_path, monkeypatch
):
    """The read slot starts when the body is sent. It is finite, or a relay that
    goes silent at that point would hang the caller for ever."""
    monkeypatch.setattr(relay_module, "_RESPONSE_TIMEOUT", 0.5)
    relay_server.script("PUT", "/", Response(201, json=CREATED, delay=6))
    started = time.monotonic()

    with pytest.raises(requests.Timeout):
        _put(_client(relay_server, tmp_path), [b"all", b"the body"])

    assert time.monotonic() - started < 4
    (seen,) = relay_server.requests
    assert seen.body == b"allthe body" and seen.body_complete  # it did take all of it


def test_put_artifact_waits_for_a_runner_that_is_slow_to_finalise_after_the_body(
    relay_server, tmp_path, monkeypatch
):
    """The response wait is its own, longer limit: a runner that takes longer than the
    per-send limit to finish the upload after the last byte is still waited for."""
    monkeypatch.setattr(relay_module, "_SEND_TIMEOUT", 0.5)
    monkeypatch.setattr(relay_module, "_RESPONSE_TIMEOUT", 8)
    relay_server.script("PUT", "/", Response(201, json=CREATED, delay=2))

    assert _put(_client(relay_server, tmp_path), [b"body"]) == CREATED


def test_put_artifact_does_not_count_a_slow_producer_against_the_read_timeout(
    relay_server, tmp_path, monkeypatch
):
    monkeypatch.setattr(relay_module, "_SEND_TIMEOUT", 1.5)
    relay_server.script("PUT", "/", Response(201, json=CREATED))

    def slow():
        for piece in (b"a", b"b", b"c"):
            time.sleep(0.8)  # 2.4 s in all: more than the timeout, in pieces less than it
            yield piece

    assert _put(_client(relay_server, tmp_path), slow()) == CREATED


def test_put_artifact_accepts_a_list_of_chunks(relay_server, tmp_path):
    """`body` is any Iterable[bytes]; requests would form-encode a bare list."""
    relay_server.script("PUT", "/", Response(201, json=CREATED))

    _put(_client(relay_server, tmp_path), [b"one", b"two"])

    assert relay_server.requests[0].body == b"onetwo"
    assert relay_server.requests[0].chunked


def test_put_artifact_empty_body(relay_server, tmp_path):
    relay_server.script("PUT", "/", Response(201, json=CREATED))

    _put(_client(relay_server, tmp_path), iter(()))

    (seen,) = relay_server.requests
    assert seen.body == b""
    assert seen.body_complete


def test_put_artifact_quotes_the_name_but_keeps_slashes(relay_server, tmp_path):
    relay_server.script("PUT", "/", Response(201, json=CREATED))

    _put(_client(relay_server, tmp_path), [b"x"], name="out dir/a b#1.txt")

    assert relay_server.requests[0].path == "/v1/tasks/req-1/artifacts/out%20dir/a%20b%231.txt"


def test_put_artifact_rereads_the_token_file_on_every_call(relay_server, tmp_path):
    relay_server.script("PUT", "/", Response(201, json=CREATED), Response(201, json=CREATED))
    client = _client(relay_server, tmp_path, token="tok-old")

    _put(client, [b"a"])
    (tmp_path / "token").write_text("tok-new\n")
    _put(client, [b"b"])

    assert [r.header("Authorization") for r in relay_server.requests] == [
        "Bearer tok-old",
        "Bearer tok-new",
    ]


def test_put_artifact_maps_413_to_too_large(relay_server, tmp_path):
    """The runner answers 413 after reading maxBytes+1 bytes, while the
    client is still mid-upload; the client must surface the mapped error,
    not a connection error."""
    max_bytes = 1024
    relay_server.script(
        "PUT",
        "/",
        Response(
            413,
            json={"code": "E_ARTIFACT_TOO_LARGE", "maxBytes": max_bytes},
            read_limit=max_bytes + 1,
        ),
    )
    megabyte = b"x" * (1 << 20)
    body = (megabyte for _ in range(16))  # far more than the socket buffers hold

    with pytest.raises(ArtifactTooLargeError) as excinfo:
        _put(_client(relay_server, tmp_path), body)

    assert excinfo.value.code == "E_ARTIFACT_TOO_LARGE"
    assert excinfo.value.max_bytes == max_bytes
    assert str(max_bytes) in excinfo.value.message
    (seen,) = relay_server.requests
    assert seen.body_length == max_bytes + 1  # the server stopped reading there
    assert not seen.body_complete


def test_put_artifact_maps_409_exists(relay_server, tmp_path):
    relay_server.script("PUT", "/", Response(409, json={"code": "E_ARTIFACT_EXISTS"}))

    with pytest.raises(ArtifactExistsError) as excinfo:
        _put(_client(relay_server, tmp_path), [b"x"])

    assert excinfo.value.code == "E_ARTIFACT_EXISTS"


def test_put_artifact_maps_503_busy_sent_before_any_body_is_read(relay_server, tmp_path):
    relay_server.script("PUT", "/", Response(503, json={"code": "E_ARTIFACT_BUSY"}, read_limit=0))
    megabyte = b"x" * (1 << 20)

    with pytest.raises(ArtifactBusyError):
        _put(_client(relay_server, tmp_path), (megabyte for _ in range(8)))

    assert relay_server.requests[0].body_length == 0


@pytest.mark.parametrize(
    ("status", "code", "exc_type"),
    [
        (404, "E_ARTIFACT_UNDECLARED", ArtifactUndeclaredError),
        (409, "E_ARTIFACT_EXISTS", ArtifactExistsError),
        (409, "E_ARTIFACT_LEASE_LOST", ArtifactLeaseLostError),
        (413, "E_ARTIFACT_TOO_LARGE", ArtifactTooLargeError),
        (422, "E_ARTIFACT_QUOTA", ArtifactQuotaError),
        (422, "E_ARTIFACTS_DISABLED", ArtifactsUnavailableError),
        (422, "E_ARTIFACTS_UNSUPPORTED", ArtifactsUnavailableError),
        (503, "E_ARTIFACT_BUSY", ArtifactBusyError),
    ],
)
def test_raise_for_maps_each_relay_code_to_its_exception(status, code, exc_type):
    resp = _response(status, f'{{"code": "{code}", "message": "from the relay"}}')

    with pytest.raises(exc_type) as excinfo:
        RelayClient._raise_for(resp)

    assert type(excinfo.value) is exc_type
    assert excinfo.value.code == code
    assert excinfo.value.message == "from the relay"
    assert isinstance(excinfo.value, ArtifactError)


def test_raise_for_404_without_a_code_is_undeclared():
    """The input GET's 404 has no JSON body."""
    with pytest.raises(ArtifactUndeclaredError) as excinfo:
        RelayClient._raise_for(_response(404, ""))

    assert excinfo.value.code == "E_ARTIFACT_UNDECLARED"


def test_raise_for_413_keeps_max_bytes():
    resp = _response(413, '{"code": "E_ARTIFACT_TOO_LARGE", "maxBytes": 2048}')

    with pytest.raises(ArtifactTooLargeError) as excinfo:
        RelayClient._raise_for(resp)

    assert excinfo.value.max_bytes == 2048


@pytest.mark.parametrize(
    ("status", "text", "code"),
    [
        (401, "", "HTTP_401"),
        (500, "<html>boom</html>", "HTTP_500"),
        (409, '{"code": "E_SOMETHING_NEW"}', "E_SOMETHING_NEW"),
        (422, '{"message": "no code here"}', "HTTP_422"),
        (502, "[1, 2]", "HTTP_502"),
    ],
)
def test_raise_for_anything_else_is_the_base_error_naming_the_status(status, text, code):
    with pytest.raises(ArtifactError) as excinfo:
        RelayClient._raise_for(_response(status, text))

    err = excinfo.value
    assert type(err) is ArtifactError
    assert err.code == code
    assert str(status) in err.message


def test_result_too_large_error_is_an_ordinary_exception():
    assert issubclass(ResultTooLargeError, Exception)
    assert not issubclass(ResultTooLargeError, ArtifactError)


def test_open_input_returns_a_streaming_response(relay_server, tmp_path):
    payload = b"input-bytes" * 1000
    relay_server.script(
        "GET",
        "/v1/tasks/req-1/artifacts/in/",
        Response(200, body=payload, headers={"X-RW-Sha256": hashlib.sha256(payload).hexdigest()}),
    )

    resp = _client(relay_server, tmp_path).open_input("prior")

    with resp:
        assert resp.headers["X-RW-Sha256"] == hashlib.sha256(payload).hexdigest()
        assert b"".join(resp.iter_content(chunk_size=4096)) == payload
    (seen,) = relay_server.requests
    assert seen.path == "/v1/tasks/req-1/artifacts/in/prior"
    assert seen.header("Authorization") == "Bearer tok-1"


def test_open_input_404_is_undeclared(relay_server, tmp_path):
    relay_server.script("GET", "/", Response(404, body=b"not declared"))

    with pytest.raises(ArtifactUndeclaredError):
        _client(relay_server, tmp_path).open_input("nope")


def _response(status: int, text: str) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = text.encode()
    return resp
