"""The runner's attempt token (`dispatch`, from `POST /v1/tasks/next`) is echoed as
`X-RW-Dispatch` on the per-task routes a RelayClient talks to -- artifact PUT, input GET,
code GET (plain and `?sha256=`); the result POST is covered in test_serve_lease.py. Run
against the real local relay (relay_server.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
import requests
from relay_server import Response

from runwhen_capability.models import TaskHostRequest, parse_dispatch
from runwhen_capability.relay import DISPATCH_HEADER, RelayClient, task_headers

CREATED = {
    "artifactRef": "art_abc",
    "sizeBytes": 3,
    "sha256": "0" * 64,
    "lineCount": 1,
    "isText": True,
}
SHA = "sha256:" + "ab" * 32
MAX_UINT64 = "18446744073709551615"


def _client(relay_server, tmp_path: Path, dispatch=None) -> RelayClient:
    token_file = tmp_path / "token"
    token_file.write_text("tok-1")
    return RelayClient(relay_server.url, token_file, "req-1", requests.Session(), dispatch=dispatch)


def _use_every_route(relay_server, client: RelayClient) -> list:
    relay_server.script("PUT", "/v1/tasks/req-1/artifacts/", Response(201, json=CREATED))
    relay_server.script("GET", "/v1/tasks/req-1/artifacts/in/", Response(200, body=b"in"))
    relay_server.script("GET", "/v1/tasks/req-1/code", Response(200, body=b"c1"))
    relay_server.script("GET", "/v1/tasks/req-1/code", Response(200, body=b"c2"))
    client.put_artifact(task="scan", name="out.txt", content_type="text/plain", body=[b"abc"])
    with client.open_input("report") as stream:
        assert stream.read() == b"in"
    with client.open_code() as stream:
        assert stream.read() == b"c1"
    with client.open_code(SHA) as stream:
        assert stream.read() == b"c2"
    return relay_server.requests


def test_the_dispatch_is_sent_on_every_route(relay_server, tmp_path):
    seen = _use_every_route(relay_server, _client(relay_server, tmp_path, MAX_UINT64))
    assert [(r.method, r.path.split("?")[0]) for r in seen] == [
        ("PUT", "/v1/tasks/req-1/artifacts/out.txt"),
        ("GET", "/v1/tasks/req-1/artifacts/in/report"),
        ("GET", "/v1/tasks/req-1/code"),
        ("GET", "/v1/tasks/req-1/code"),
    ]
    assert seen[3].path.startswith("/v1/tasks/req-1/code?sha256=")  # the by-hash form
    assert [r.header("x-rw-dispatch") for r in seen] == [MAX_UINT64] * 4
    assert all(r.header("authorization") == "Bearer tok-1" for r in seen)


def test_no_dispatch_sends_no_header(relay_server, tmp_path):
    seen = _use_every_route(relay_server, _client(relay_server, tmp_path))
    assert len(seen) == 4
    assert [r.header("x-rw-dispatch") for r in seen] == [None] * 4


@pytest.mark.parametrize("bad", ["abc", "-1", "", " 1", "1.0", "0x10", "٣", "1" + MAX_UINT64])
def test_an_invalid_dispatch_given_to_the_client_is_dropped(relay_server, tmp_path, bad):
    seen = _use_every_route(relay_server, _client(relay_server, tmp_path, bad))
    assert [r.header("x-rw-dispatch") for r in seen] == [None] * 4


def test_task_headers_is_the_one_place_the_header_is_built():
    assert task_headers("t", None) == {"Authorization": "Bearer t"}
    assert task_headers("t", "5") == {"Authorization": "Bearer t", DISPATCH_HEADER: "5"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", "0"),
        ("1234567890", "1234567890"),
        ("007", "007"),
        (MAX_UINT64, MAX_UINT64),
        ("18446744073709551616", None),
        ("abc", None),
        ("-1", None),
        ("+1", None),
        ("", None),
        (" 1", None),
        ("1\n", None),
        ("٣", None),
        (12, None),
        (True, None),
        (None, None),
        ([], None),
    ],
)
def test_parse_dispatch(value, expected):
    assert parse_dispatch(value) == expected


def _host_request(**extra) -> dict:
    return {
        "requestId": "req-1",
        "request": {"version": 1, "tasks": [{"task": "echo", "inputs": {}}]},
        "scopeId": "scope-1",
        "deadlineMs": 1000,
        **extra,
    }


def test_task_host_request_reads_the_dispatch_and_tolerates_its_absence_or_garbage():
    assert TaskHostRequest.model_validate(_host_request(dispatch="42")).dispatch == "42"
    assert TaskHostRequest.model_validate(_host_request()).dispatch is None
    for bad in ("abc", "-1", "", 7, None, {"x": 1}):
        assert TaskHostRequest.model_validate(_host_request(dispatch=bad)).dispatch is None
