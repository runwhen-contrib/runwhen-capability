"""k8s_proxy.proxy_get (S17): a proxied GET through `kubectl proxy` that never follows a
redirect. A fake `kubectl` (tests/k8s_proxy_fake.py) stands in for the API server's proxy;
"another host" is a real local listener that must never hear from the helper."""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from k8s_proxy_fake import install_fake_kubectl

from runwhen_capability import k8s_proxy
from runwhen_capability._rw_sh import RW_SH
from runwhen_capability.context import Context
from runwhen_capability.k8s_proxy import (
    DEFAULT_MAX_BODY,
    METHODS,
    ProxyResponse,
    proxy_get,
    proxy_request,
    task_get,
    task_request,
)
from runwhen_capability.rwerrors import ERROR_CODES, RwCommandError, RwError

HEALTH = "/api/v1/namespaces/vault/services/vault:8200/proxy/v1/sys/health"


@pytest.fixture(autouse=True)
def _no_shim_mode(monkeypatch):
    for name in ("RW_SHIM_MODE", "RW_SHIM_LOG", "RW_SHIM_CALLS", "RW_SHIM_STATE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def other_host():
    """A listener standing in for the redirect target; records every request it gets."""
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append(
                {"method": self.command, "path": self.path, "headers": dict(self.headers.items())}
            )
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_HEAD = do_PUT = do_POST = do_DELETE = do_PATCH = do_GET

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.seen = seen  # type: ignore[attr-defined]
    yield server
    server.shutdown()
    server.server_close()


def vault(tmp_path, monkeypatch, routes=None, **config):
    routes = {HEALTH: {"status": 200, "body": '{"sealed":false}'}} if routes is None else routes
    return install_fake_kubectl(tmp_path, monkeypatch, routes=routes, **config)


@pytest.fixture
def short_dir():
    """A directory with a path short enough for a unix socket (tmp_path is not)."""
    with tempfile.TemporaryDirectory(prefix="rwt-", dir="/tmp") as directory:
        yield Path(directory)


def get_health(**kwargs) -> ProxyResponse:
    return proxy_get("vault", "services", "vault", 8200, "/v1/sys/health", **kwargs)


# -- a plain answer ------------------------------------------------------------------------


def test_a_200_returns_status_headers_and_body(tmp_path, monkeypatch):
    fake = vault(
        tmp_path,
        monkeypatch,
        routes={HEALTH: {"status": 200, "headers": {"X-Thing": "A"}, "body": '{"sealed":false}'}},
    )
    got = get_health()
    assert isinstance(got, ProxyResponse)
    assert got.status == 200
    assert got.body == b'{"sealed":false}'
    assert got.truncated is False
    assert got.location is None
    assert got.headers["x-thing"] == "A"
    assert all(key == key.lower() for key in got.headers)
    assert [r["path"] for r in fake.requests()] == [HEALTH]


def test_per_request_api_server_headers_are_dropped_from_the_answer(tmp_path, monkeypatch):
    # the fake's http.server also adds its own `Date` header to every answer
    vault(
        tmp_path,
        monkeypatch,
        routes={
            HEALTH: {
                "headers": {
                    "Audit-Id": "7a1d1c0e-0000-4000-8000-000000000000",
                    "Date": "Tue, 04 Nov 2026 10:00:00 GMT",
                    "X-Kubernetes-Pf-Flowschema-Uid": "f-uid",
                    "X-Kubernetes-Pf-Prioritylevel-Uid": "p-uid",
                    "Content-Type": "application/json",
                    "X-Backend": "kept",
                },
                "body": "{}",
            }
        },
    )
    got = get_health()
    assert got.headers["content-type"] == "application/json"
    assert got.headers["x-backend"] == "kept"
    assert "audit-id" not in got.headers and "date" not in got.headers
    assert not [name for name in got.headers if name.startswith("x-kubernetes-pf-")]
    assert "audit-id" not in got.to_task_dict()["headers"]


def test_strip_noise_headers_matches_names_in_any_case():
    kept = k8s_proxy.strip_noise_headers(
        {
            "Audit-ID": "a",
            "DATE": "d",
            "X-Kubernetes-PF-Flowschema-UID": "f",
            "x-kubernetes-pf-anything": "p",
            "x-kubernetes-other": "kept",
            "content-type": "text/plain",
        }
    )
    assert kept == {"x-kubernetes-other": "kept", "content-type": "text/plain"}


def test_kubectl_proxy_serves_on_a_private_unix_socket_with_a_host_filter(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    get_health()
    (command,) = fake.commands()
    assert command[0] == "proxy" and len(command) == 3
    assert command[1].startswith("--unix-socket=/")  # no TCP port: --port is refused with it
    assert command[2] == r"--accept-hosts=^127\.0\.0\.1$"
    sock = command[1].split("=", 1)[1]
    (serving,) = fake.serving()
    assert serving["socket"] == sock
    assert serving["dir_mode"] == 0o700
    assert len(sock) < 100
    assert not Path(sock).parent.exists()  # the directory is removed afterwards


def test_the_socket_directory_is_removed_after_a_failure_too(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch, api_status=status_body(403))
    with pytest.raises(RwError):
        get_health()
    (serving,) = fake.serving()
    assert not Path(serving["socket"]).parent.exists()


def test_a_long_temporary_directory_falls_back_to_a_short_one(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    deep = tmp_path / ("d" * 40) / ("e" * 40)
    deep.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(deep))
    assert get_health().status == 200
    assert len(fake.serving()[0]["socket"]) < 100


def test_a_proxy_that_prints_nothing_is_used_once_its_socket_exists(tmp_path, monkeypatch):
    vault(tmp_path, monkeypatch, silent=True)
    assert get_health().status == 200


def test_no_credential_header_is_sent(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")  # an environment proxy is not used
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    get_health()
    (request,) = fake.requests()
    names = {k.lower() for k in request["headers"]}
    assert not names & {"authorization", "cookie", "proxy-authorization", "x-api-key"}
    assert request["host"] == "127.0.0.1"


def test_https_backends_use_the_scheme_prefix_and_named_ports_work(tmp_path, monkeypatch):
    path = "/api/v1/namespaces/ns-1/pods/https:p-0:metrics/proxy/a/b"
    fake = install_fake_kubectl(tmp_path, monkeypatch, routes={path: {"body": "ok"}})
    got = proxy_get("ns-1", "pods", "p-0", "metrics", "a/b", scheme="https")
    assert got.body == b"ok"
    assert [r["path"] for r in fake.requests()] == [path]


def test_a_numeric_string_port_is_a_number(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    proxy_get("vault", "services", "vault", "8200", "v1/sys/health")
    assert [r["path"] for r in fake.requests()] == [HEALTH]


def test_a_non_2xx_answer_is_data(tmp_path, monkeypatch):
    vault(
        tmp_path,
        monkeypatch,
        routes={HEALTH: {"status": 404, "body": '{"errors":["no such path"]}'}},
    )
    got = get_health()
    assert got.status == 404
    assert got.body == b'{"errors":["no such path"]}'


def test_a_backend_403_without_an_api_status_body_is_data(tmp_path, monkeypatch):
    vault(tmp_path, monkeypatch, routes={HEALTH: {"status": 403, "body": "forbidden by vault"}})
    got = get_health()
    assert (got.status, got.body) == (403, b"forbidden by vault")


def test_a_503_is_data_too(tmp_path, monkeypatch):
    vault(tmp_path, monkeypatch, routes={HEALTH: {"status": 503, "body": "sealed"}})
    assert get_health().status == 503


# -- redirects are never followed ----------------------------------------------------------


def redirect(location: str, status: int = 302) -> dict:
    return {HEALTH: {"status": status, "headers": {"Location": location}, "body": "moved"}}


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_to_another_host_is_not_followed_and_not_reported_as_a_location(
    tmp_path, monkeypatch, other_host, status
):
    port = other_host.server_address[1]
    fake = vault(tmp_path, monkeypatch, routes=redirect(f"http://127.0.0.1:{port}/steal", status))
    got = get_health()
    assert got.status == status
    assert got.location is None
    assert got.headers["x-rw-redirect-dropped"] == f"127.0.0.1:{port}"
    assert "location" not in got.headers
    assert got.body == b"moved"
    assert other_host.seen == []
    assert len(fake.requests()) == 1


@pytest.mark.parametrize(
    ("location", "host"),
    [
        ("http://localhost:{p}/x", "localhost:{p}"),
        ("//evil.example/x", "evil.example"),
        ("https://sso.example.com/login?next=1", "sso.example.com"),
        ("http://user:pw@evil.example:8080/x", "evil.example:8080"),
        ("ftp://files.example/x", "files.example"),
        ("javascript:alert(1)", "javascript:"),
    ],
)
def test_other_absolute_locations_are_dropped(tmp_path, monkeypatch, other_host, location, host):
    port = other_host.server_address[1]
    vault(tmp_path, monkeypatch, routes=redirect(location.format(p=port)))
    got = get_health()
    assert got.location is None
    assert got.headers["x-rw-redirect-dropped"] == host.format(p=port)
    assert "pw" not in got.headers["x-rw-redirect-dropped"]
    assert other_host.seen == []


PREFIX = "/api/v1/namespaces/vault/services/vault:8200/proxy"


@pytest.mark.parametrize(
    ("sent", "location"),
    [
        ("/ui/", "/ui/"),
        ("/somewhere/else?x=1", "/somewhere/else?x=1"),  # the fake prefixes it, like the API server
        (PREFIX + "/ui/?a=1", "/ui/?a=1"),  # already in the proxy path space
        (PREFIX, "/"),
        (PREFIX + "/", "/"),
        (PREFIX + "?a=1", "/?a=1"),
        ("http://{proxy}" + PREFIX + "/ui/?a=1", "/ui/?a=1"),  # absolute, back into this proxy
        ("ui/", "ui/"),  # relative without a slash is left as it is
    ],
)
def test_a_redirect_within_the_target_is_reported_as_a_backend_relative_path(
    tmp_path, monkeypatch, sent, location
):
    fake = vault(tmp_path, monkeypatch, routes=redirect(sent))
    got = get_health()
    assert (got.status, got.location) == (302, location)
    assert "x-rw-redirect-dropped" not in got.headers
    assert len(fake.requests()) == 1  # not followed


def test_the_raw_location_header_is_kept_only_for_a_kept_redirect(tmp_path, monkeypatch):
    vault(tmp_path, monkeypatch, routes=redirect("/ui/?a=1"))
    got = get_health()
    assert got.location == "/ui/?a=1"
    assert got.headers["location"] == PREFIX + "/ui/?a=1"  # the raw value, as received


def test_a_location_can_be_passed_straight_back_as_the_path(tmp_path, monkeypatch):
    routes = redirect("/ui/?a=1")
    routes[PREFIX + "/ui/?a=1"] = {"body": "landed"}
    fake = vault(tmp_path, monkeypatch, routes=routes)
    first = get_health()
    second = proxy_get("vault", "services", "vault", 8200, first.location)
    assert second.body == b"landed", [r["path"] for r in fake.requests()]


@pytest.mark.parametrize(
    ("sent", "dropped"),
    [
        ("/api/v1/namespaces/other/services/vault:8200/proxy/x", "other/services/vault:8200"),
        ("/api/v1/namespaces/vault/pods/vault:8200/proxy/x", "vault/pods/vault:8200"),
        ("/api/v1/namespaces/vault/services/vault:9999/proxy/x", "vault/services/vault:9999"),
        (
            "/api/v1/namespaces/vault/services/https:vault:8200/proxy",
            "vault/services/https:vault:8200",
        ),
        ("http://{proxy}/api/v1/namespaces/other/pods/p:80/proxy/x", "other/pods/p:80"),
    ],
)
def test_a_redirect_to_another_pod_service_or_port_is_dropped(tmp_path, monkeypatch, sent, dropped):
    fake = vault(tmp_path, monkeypatch, routes=redirect(sent))
    got = get_health()
    assert got.location is None
    assert got.headers["x-rw-redirect-dropped"] == dropped
    assert "location" not in got.headers
    assert len(fake.requests()) == 1


def test_a_redirect_to_the_proxy_host_but_another_path_is_dropped(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch, routes=redirect("http://{proxy}/api/v1/secrets"))
    got = get_health()
    assert got.location is None
    assert got.headers["x-rw-redirect-dropped"] == "127.0.0.1"
    assert len(fake.requests()) == 1


# -- errors --------------------------------------------------------------------------------


def status_body(code: int, reason: str = "Forbidden", message: str = "denied") -> dict:
    body = {
        "kind": "Status",
        "apiVersion": "v1",
        "status": "Failure",
        "message": message,
        "reason": reason,
        "code": code,
    }
    return {"status": code, "body": json.dumps(body)}


@pytest.mark.parametrize("code", [401, 403])
def test_an_api_server_refusal_is_permission_denied(tmp_path, monkeypatch, code):
    fake = vault(
        tmp_path,
        monkeypatch,
        api_status=status_body(code, message='services "vault" is forbidden: cannot get proxy'),
    )
    with pytest.raises(RwError) as raised:
        get_health()
    assert raised.value.code == "permission_denied"
    assert "forbidden" in raised.value.message
    assert fake.proxy_is_gone()


def api_status(code: int, reason: str, message: str, api_version: str | None = "v1") -> dict:
    body = {
        "kind": "Status",
        "status": "Failure",
        "message": message,
        "reason": reason,
        "code": code,
    }
    if api_version:
        body["apiVersion"] = api_version
    return {"status": code, "body": json.dumps(body)}


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (api_status(404, "NotFound", 'services "vault" not found'), "not_found"),
        (api_status(404, "NotFound", 'pods "vault-0" not found'), "not_found"),
        (
            api_status(
                503,
                "ServiceUnavailable",
                "error trying to reach service: dial tcp 10.0.0.1:8200: connection refused",
            ),
            "unreachable",
        ),
        (api_status(503, "ServiceUnavailable", "anything"), "unreachable"),
        (api_status(503, "Whatever", 'no endpoints available for service "vault"'), "unreachable"),
        (api_status(500, "InternalError", "error trying to reach service: EOF"), "unreachable"),
        (api_status(401, "Unauthorized", "Unauthorized"), "permission_denied"),
    ],
)
def test_an_api_server_status_is_mapped_to_a_code(tmp_path, monkeypatch, status, code):
    fake = vault(tmp_path, monkeypatch, api_status=status)
    with pytest.raises(RwError) as raised:
        get_health()
    assert raised.value.code == code
    assert raised.value.scope == "vault/services/vault:8200"
    assert fake.proxy_is_gone()


@pytest.mark.parametrize(
    "route",
    [
        {"status": 503, "body": '{"errors":[]}'},  # Vault sealed: the backend's own answer
        {"status": 503, "body": '{"errors":["Vault is sealed"]}'},
        {"status": 404, "body": '{"errors":[]}'},
        {"status": 500, "body": "internal error"},
        api_status(500, "InternalError", "something else broke"),  # a Status, but not mapped
        api_status(404, "Gone", "not the NotFound reason"),
        api_status(403, "Forbidden", "no apiVersion", api_version=None),  # not v1: not ours
        api_status(503, "ServiceUnavailable", "wrong apiVersion", api_version="v2"),
        {"status": 503, "body": '[{"kind":"Status","apiVersion":"v1"}]'},
        {"status": 503, "body": "error trying to reach service"},  # text, not a Status
    ],
)
def test_other_error_answers_are_data(tmp_path, monkeypatch, route):
    vault(tmp_path, monkeypatch, routes={HEALTH: route})
    got = get_health()
    assert got.status == route["status"]
    assert got.body == route["body"].encode()


def test_kubectl_missing_is_a_dependency_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(RwError) as raised:
        get_health()
    assert raised.value.code == "dependency"
    assert "kubectl" in raised.value.message


def test_a_proxy_that_exits_at_once_is_an_error_with_a_code(tmp_path, monkeypatch):
    install_fake_kubectl(
        tmp_path,
        monkeypatch,
        mode="fail",
        stderr="Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout\n",
    )
    with pytest.raises(RwCommandError) as raised:
        get_health()
    assert raised.value.code == "unreachable"
    assert raised.value.argv[1] == "proxy"
    assert raised.value.returncode == 1
    assert "Unable to connect" in raised.value.stderr


def test_a_proxy_that_exits_with_an_unknown_message_is_still_a_coded_error(tmp_path, monkeypatch):
    install_fake_kubectl(tmp_path, monkeypatch, mode="fail", stderr="something odd\n")
    with pytest.raises(RwError) as raised:
        get_health()
    assert raised.value.code in ERROR_CODES


def test_a_proxy_that_never_says_it_is_serving_times_out_and_is_killed(tmp_path, monkeypatch):
    fake = install_fake_kubectl(tmp_path, monkeypatch, mode="hang")
    with pytest.raises(RwError) as raised:
        get_health(timeout=1)
    assert raised.value.code == "timeout"
    assert fake.proxy_is_gone()


def test_a_slow_backend_times_out(tmp_path, monkeypatch, short_dir):
    """A proxy that accepts the connection and never answers."""
    fake = vault(tmp_path, monkeypatch)
    dead_end = short_dir / "dead.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(dead_end))
    listener.listen(5)
    real_start = k8s_proxy._start_proxy

    def start_with_a_dead_end(*args, **kwargs):
        proc, _sock, workdir, err = real_start(*args, **kwargs)
        return proc, str(dead_end), workdir, err

    monkeypatch.setattr(k8s_proxy, "_start_proxy", start_with_a_dead_end)
    try:
        with pytest.raises(RwError) as raised:
            get_health(timeout=1)
    finally:
        listener.close()
    assert raised.value.code == "timeout"
    assert fake.proxy_is_gone()


@pytest.mark.parametrize("missing", [True, False])
def test_a_socket_nobody_serves_is_unreachable(tmp_path, monkeypatch, short_dir, missing):
    fake = vault(tmp_path, monkeypatch)
    stale = short_dir / "stale.sock"
    if not missing:  # a socket file with nothing listening: connection refused
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(stale))
        s.close()
    real_start = k8s_proxy._start_proxy

    def start_with_a_wrong_socket(*args, **kwargs):
        proc, _sock, workdir, err = real_start(*args, **kwargs)
        return proc, str(stale), workdir, err

    monkeypatch.setattr(k8s_proxy, "_start_proxy", start_with_a_wrong_socket)
    with pytest.raises(RwError) as raised:
        get_health()
    assert raised.value.code == "unreachable"
    assert fake.proxy_is_gone()


# -- input validation ----------------------------------------------------------------------

GOOD = ("vault", "services", "vault", 8200, "v1/sys/health")


@pytest.mark.parametrize(
    "bad",
    [
        {"namespace": "Vault"},
        {"namespace": "a/b"},
        {"namespace": "a:b"},
        {"namespace": ""},
        {"namespace": "x" * 64},
        {"namespace": "-a"},
        {"name": "a/b"},
        {"name": "a:b"},
        {"name": "A"},
        {"name": ""},
        {"name": "a..b"},
        {"name": "x" * 254},
        {"kind": "nodes"},
        {"kind": "pods/exec"},
        {"port": 0},
        {"port": 65536},
        {"port": -1},
        {"port": True},
        {"port": 1.5},
        {"port": ""},
        {"port": "Bad_Port"},
        {"port": "a" * 64},
        {"port": "-a"},
        {"port": "a-"},
        {"port": "a--b", "kind": "pods"},
        {"port": "80/x"},
        {"path": "../x"},
        {"path": "a/../b"},
        {"path": "a/.."},
        {"path": ".."},
        {"path": "%2e%2e/x"},
        {"path": "a/%2E%2e/b"},
        {"path": "a/%252e%252e/b"},
        {"path": "a%2f..%2fb"},
        {"path": "a\\b"},
        {"path": "a\nb"},
        {"path": "a\rb"},
        {"path": "a\x00b"},
        {"path": "a%00b"},
        {"path": "a%0d%0ab"},
        {"path": 5},
        {"scheme": "ftp"},
        {"scheme": "HTTP"},
        {"timeout": 0},
        {"timeout": -1},
        {"max_body": 0},
    ],
)
def test_bad_input_is_invalid_input_and_starts_nothing(tmp_path, monkeypatch, bad):
    fake = vault(tmp_path, monkeypatch)
    args = dict(zip(("namespace", "kind", "name", "port", "path"), GOOD, strict=True))
    options = {k: bad.pop(k) for k in ("scheme", "timeout", "max_body") if k in bad}
    args.update(bad)
    with pytest.raises(RwError) as raised:
        proxy_get(**args, **options)
    assert raised.value.code == "invalid_input"
    assert fake.commands() == []


@pytest.mark.parametrize(
    ("path", "sent"),
    [
        ("/v1/sys/health", "v1/sys/health"),
        ("v1/sys/health", "v1/sys/health"),
        ("//double", "/double"),  # only ONE leading slash is stripped
        ("", ""),
        ("/", ""),
        ("a b/c", "a%20b/c"),
        ("a%20b/c", "a%20b/c"),  # an escape is kept, not escaped again
        ("100%/x", "100%25/x"),  # a bare percent is
        ("a#b", "a%23b"),
        ("v1/q?x=1 2&y=%41&z=é", "v1/q?x=1%202&y=%41&z=%C3%A9"),
        ("?only=query", "?only=query"),
        ("a/./b", "a/./b"),
        ("a..b/c...", "a..b/c..."),
        ("sys/health:x@y", "sys/health:x@y"),
    ],
)
def test_the_path_is_normalised_and_percent_encoded(tmp_path, monkeypatch, path, sent):
    prefix = "/api/v1/namespaces/vault/services/vault:8200/proxy/"
    fake = vault(tmp_path, monkeypatch, routes={prefix + sent: {"body": "hit"}})
    got = proxy_get("vault", "services", "vault", 8200, path)
    assert got.body == b"hit", [r["path"] for r in fake.requests()]


# -- body limits ---------------------------------------------------------------------------


def test_a_body_over_the_limit_is_cut_and_marked(tmp_path, monkeypatch):
    vault(tmp_path, monkeypatch, routes={HEALTH: {"body": "x" * 5000}})
    got = get_health(max_body=1000)
    assert len(got.body) == 1000
    assert got.truncated is True
    assert got.status == 200


def test_a_body_exactly_at_the_limit_is_whole(tmp_path, monkeypatch):
    vault(tmp_path, monkeypatch, routes={HEALTH: {"body": "x" * 1000}})
    got = get_health(max_body=1000)
    assert len(got.body) == 1000
    assert got.truncated is False


def test_an_error_body_is_cut_the_same_way(tmp_path, monkeypatch):
    vault(tmp_path, monkeypatch, routes={HEALTH: {"status": 500, "body": "y" * 3000}})
    got = get_health(max_body=10)
    assert (got.status, len(got.body), got.truncated) == (500, 10, True)


def test_the_default_limit_is_4_mib():
    assert DEFAULT_MAX_BODY == 4 * 1024 * 1024


# -- the proxy process ---------------------------------------------------------------------


def test_the_proxy_process_is_gone_afterwards(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    get_health()
    assert fake.proxy_is_gone()


def test_the_proxy_is_gone_after_a_failed_request_too(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch, routes={HEALTH: {"status": 500, "body": "boom"}})
    get_health()
    assert fake.proxy_is_gone()


def test_the_proxy_leads_its_own_process_group(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    seen = {}
    real_stop = k8s_proxy._stop_proxy

    def spy(proc):
        seen["pgid"] = os.getpgid(proc.pid)
        seen["pid"] = proc.pid
        real_stop(proc)

    monkeypatch.setattr(k8s_proxy, "_stop_proxy", spy)
    get_health()
    assert seen["pgid"] == seen["pid"] != os.getpgrp()
    assert fake.proxy_is_gone()


def test_a_proxy_that_ignores_sigterm_is_killed(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    script = Path(shutil.which("kubectl"))
    text = script.read_text().replace(
        "args = sys.argv[1:]",
        "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)\nargs = sys.argv[1:]",
    )
    script.write_text(text)
    monkeypatch.setattr(k8s_proxy, "_TERM_GRACE", 0.3)
    get_health()
    assert fake.proxy_is_gone()


def test_a_kubectl_given_by_path_is_used(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", "/nonexistent")
    got = get_health(
        kubectl=str(Path(shutil.which("kubectl", path=str(tmp_path / "fake-kubectl-bin"))))
    )
    assert got.status == 200
    assert fake.proxy_is_gone()


def test_a_shim_directory_is_never_used_to_start_the_proxy(tmp_path, monkeypatch):
    """In `rwtask test --record` a kubectl shim is first on PATH; the proxy must start the
    real kubectl, or `kubectl proxy` would be recorded as a call."""
    fake = vault(tmp_path, monkeypatch)
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    (shim_dir / ".rw-shim.py").write_text("")
    (shim_dir / "kubectl").write_text("#!/bin/sh\necho SHIM >&2\nexit 5\n")
    (shim_dir / "kubectl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    assert get_health().status == 200
    assert fake.proxy_is_gone()


# -- the task API --------------------------------------------------------------------------


def test_task_get_returns_the_dict_a_task_sees(tmp_path, monkeypatch):
    body = base64.b64encode(b"ok \xff end").decode()
    vault(tmp_path, monkeypatch, routes={HEALTH: {"headers": {"X-A": "1"}, "body_b64": body}})
    got = task_get("vault", "services", "vault", 8200, "/v1/sys/health")
    assert got == {
        "status": 200,
        "headers": got["headers"],
        "body": "ok � end",
        "truncated": False,
        "location": None,
    }
    assert got["headers"]["x-a"] == "1"


def test_ctx_k8s_proxy_get_is_the_task_api(tmp_path, monkeypatch):
    vault(tmp_path, monkeypatch)
    ctx = Context(capability="c", operation="t", workdir=tmp_path)
    got = ctx.k8s_proxy_get("vault", "services", "vault", 8200, "/v1/sys/health", timeout=10)
    assert got["status"] == 200 and got["body"] == '{"sealed":false}'
    with pytest.raises(RwError) as raised:
        ctx.k8s_proxy_get("vault", "services", "vault", 8200, "../x")
    assert raised.value.code == "invalid_input"


# -- the command line (bash's rw_k8s_proxy_get) ----------------------------------------------


def run_cli(*args, env=None, cwd=None):
    environ = {**os.environ, **(env or {})}
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(Path(k8s_proxy.__file__).parents[1]), environ.get("PYTHONPATH", "")]
    )
    return subprocess.run(
        [sys.executable, "-m", "runwhen_capability.k8s_proxy", *args],
        capture_output=True,
        text=True,
        env=environ,
        cwd=cwd,
        check=False,
    )


def test_the_command_prints_the_task_dict_as_one_json_line(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    done = run_cli("get", "vault", "services", "vault", "8200", "/v1/sys/health")
    assert done.returncode == 0, done.stderr
    assert len(done.stdout.splitlines()) == 1
    doc = json.loads(done.stdout)
    assert doc["status"] == 200 and doc["body"] == '{"sealed":false}'
    assert set(doc) == {"status", "headers", "body", "truncated", "location"}
    assert fake.proxy_is_gone()


def test_the_command_takes_scheme_and_timeout(tmp_path, monkeypatch):
    path = "/api/v1/namespaces/vault/services/https:vault:8200/proxy/x"
    install_fake_kubectl(tmp_path, monkeypatch, routes={path: {"body": "tls"}})
    done = run_cli(
        "get", "vault", "services", "vault", "8200", "x", "--scheme", "https", "--timeout", "20"
    )
    assert json.loads(done.stdout)["body"] == "tls", done.stderr


def test_a_failing_command_exits_non_zero_and_records_the_error(tmp_path, monkeypatch):
    install_fake_kubectl(tmp_path, monkeypatch, api_status=status_body(403))
    errors = tmp_path / "errors.jsonl"
    done = run_cli(
        "get", "vault", "services", "vault", "8200", "x", env={"RW_ERRORS_FILE": str(errors)}
    )
    assert done.returncode == 1
    assert done.stdout == ""
    assert "permission_denied" in done.stderr
    (line,) = errors.read_text().splitlines()
    entry = json.loads(line)
    assert entry["code"] == "permission_denied"
    assert "message" in entry


def test_a_failing_command_without_an_errors_file_still_exits_non_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    done = run_cli("get", "vault", "services", "vault", "8200", "x", env={"PATH": str(tmp_path)})
    assert done.returncode == 1
    assert "dependency" in done.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_rw_k8s_proxy_get_in_bash(tmp_path, monkeypatch):
    vault(tmp_path, monkeypatch)
    rw_sh = tmp_path / "rw.sh"
    rw_sh.write_text(RW_SH)
    errors = tmp_path / "errors.jsonl"
    env = {
        **os.environ,
        "RW_PYTHON": sys.executable,
        "PYTHONPATH": str(Path(k8s_proxy.__file__).parents[1]),
        "RW_ERRORS_FILE": str(errors),
    }
    script = (
        f'source "{rw_sh}"\n'
        "out=$(rw_k8s_proxy_get vault services vault 8200 /v1/sys/health --timeout 20)\n"
        'printf "%s" "$out"\n'
        'rw_k8s_proxy_get vault nodes vault 8200 x || echo "failed rc=$?" >&2\n'
    )
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, check=False
    )
    assert json.loads(done.stdout)["body"] == '{"sealed":false}', done.stderr
    assert "failed rc=1" in done.stderr
    assert json.loads(errors.read_text().splitlines()[0])["code"] == "invalid_input"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_rw_k8s_proxy_get_with_too_few_arguments_is_a_usage_error(tmp_path):
    rw_sh = tmp_path / "rw.sh"
    rw_sh.write_text(RW_SH)
    done = subprocess.run(
        ["bash", "-c", f'source "{rw_sh}"\nrw_k8s_proxy_get vault services'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2
    assert "usage" in done.stderr.lower()


# -- other methods and a body ---------------------------------------------------------------

WRITE_METHODS = ["PUT", "POST", "PATCH", "DELETE"]
PREFIX_SLASH = PREFIX + "/"


def request_health(method, **kwargs) -> ProxyResponse:
    return proxy_request(method, "vault", "services", "vault", 8200, "/v1/sys/health", **kwargs)


def test_the_methods_are_the_six_and_proxy_get_is_a_get(tmp_path, monkeypatch):
    assert METHODS == ("GET", "HEAD", "PUT", "POST", "DELETE", "PATCH")
    fake = vault(tmp_path, monkeypatch)
    get_health()
    assert [r["method"] for r in fake.requests()] == ["GET"]


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_a_body_reaches_the_backend_with_its_method(tmp_path, monkeypatch, method):
    fake = vault(tmp_path, monkeypatch)
    got = request_health(method, body=b'{"a": "\xc3\xa9"}')
    assert got.status == 200
    (request,) = fake.requests()
    assert (request["method"], request["path"]) == (method, HEALTH)
    assert fake.bodies() == [b'{"a": "\xc3\xa9"}']
    assert request["headers"]["Content-Length"] == str(len(fake.bodies()[0]))


def test_a_str_body_is_sent_as_utf_8_and_binary_bodies_are_kept(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    request_health("PUT", body="caf\u00e9")
    request_health("POST", body=bytes(range(256)))
    request_health("PUT", body=bytearray(b"ba"))
    assert fake.bodies() == ["caf\u00e9".encode(), bytes(range(256)), b"ba"]


def test_a_request_without_a_body_sends_none(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    for method in WRITE_METHODS:
        request_health(method)
        request_health(method, body=b"")
    assert fake.bodies() == [b""] * 8
    assert all("Content-Type" not in r["headers"] for r in fake.requests())


def test_a_body_without_a_content_type_is_labelled_octet_stream_not_a_form(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    request_health("PUT", body=b"x")
    request_health("PUT", body=b"{}", headers={"content-type": "application/json"})
    first, second = fake.requests()
    assert first["headers"]["Content-Type"] == "application/octet-stream"
    assert second["headers"]["Content-Type"] == "application/json"


def test_the_answer_depends_on_the_method(tmp_path, monkeypatch):
    routes = {
        HEALTH: {"body": "read"},
        "PUT " + HEALTH: {"status": 201, "body": "written"},
        "DELETE " + HEALTH: {"status": 204},
    }
    vault(tmp_path, monkeypatch, routes=routes)
    got = [get_health(), request_health("PUT", body="x"), request_health("DELETE")]
    assert [(r.status, r.body) for r in got] == [(200, b"read"), (201, b"written"), (204, b"")]


def test_head_returns_the_headers_and_no_body(tmp_path, monkeypatch):
    fake = vault(
        tmp_path, monkeypatch, routes={HEALTH: {"headers": {"X-Thing": "A"}, "body": "hello"}}
    )
    got = request_health("HEAD")
    assert (got.status, got.body, got.truncated) == (200, b"", False)
    assert got.headers["x-thing"] == "A"
    assert got.headers["content-length"] == "5"
    assert [r["method"] for r in fake.requests()] == ["HEAD"]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_only_put_post_patch_and_delete_may_carry_a_body(tmp_path, monkeypatch, method):
    fake = vault(tmp_path, monkeypatch)
    for body in (b"x", "x", bytearray(b"x")):
        with pytest.raises(RwError) as raised:
            request_health(method, body=body)
        assert raised.value.code == "invalid_input" and method in raised.value.message
    assert fake.commands() == []
    assert request_health(method, body=b"").status == 200  # nothing to send is not a body


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["PUT", "POST", "PATCH", "DELETE", "HEAD"])
def test_a_redirect_on_any_method_is_not_followed(
    tmp_path, monkeypatch, other_host, method, status
):
    port = other_host.server_address[1]
    routes = redirect(f"http://127.0.0.1:{port}/steal", status)
    fake = vault(tmp_path, monkeypatch, routes=routes)
    body = b"payload" if method != "HEAD" else None
    got = request_health(method, body=body)
    assert got.status == status
    assert got.location is None
    assert got.headers["x-rw-redirect-dropped"] == f"127.0.0.1:{port}"
    assert "location" not in got.headers
    assert other_host.seen == []
    assert [r["method"] for r in fake.requests()] == [method]  # one request, not repeated


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_a_location_that_cannot_be_parsed_is_dropped_not_an_error(tmp_path, monkeypatch, method):
    vault(tmp_path, monkeypatch, routes=redirect("http://[::1/x", 307))
    got = request_health(method, body="x" if method == "PUT" else None)
    assert (got.status, got.location, got.body) == (307, None, b"moved")
    assert got.headers["x-rw-redirect-dropped"] == "?"
    assert "location" not in got.headers


@pytest.mark.parametrize("method", ["PUT", "POST", "DELETE"])
def test_a_307_into_the_same_target_is_reported_as_a_path_and_not_followed(
    tmp_path, monkeypatch, method
):
    fake = vault(tmp_path, monkeypatch, routes=redirect("/v2/health", 307))
    got = request_health(method, body="x")
    assert (got.status, got.location) == (307, "/v2/health")
    assert len(fake.requests()) == 1


@pytest.mark.parametrize(
    "method", ["get", "Put", "TRACE", "OPTIONS", "CONNECT", "", "GET ", "PUT\r\nX: y", None, 5]
)
def test_any_other_method_is_invalid_input_and_starts_nothing(tmp_path, monkeypatch, method):
    fake = vault(tmp_path, monkeypatch)
    with pytest.raises(RwError) as raised:
        request_health(method)
    assert raised.value.code == "invalid_input"
    assert "GET, HEAD, PUT, POST, DELETE, PATCH" in raised.value.message
    assert fake.commands() == []


@pytest.mark.parametrize("body", [5, ["a"], {"a": 1}, True, "\ud800"])
def test_a_body_must_be_bytes_or_encodable_text(tmp_path, monkeypatch, body):
    fake = vault(tmp_path, monkeypatch)
    with pytest.raises(RwError) as raised:
        request_health("PUT", body=body)
    assert raised.value.code == "invalid_input"
    assert fake.commands() == []


def test_the_request_body_is_capped_at_4_mib(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    limit = 4 * 1024 * 1024
    assert request_health("PUT", body=b"x" * limit).status == 200
    assert len(fake.bodies()[0]) == limit
    for body in (b"x" * (limit + 1), "y" * (limit + 1), "\u00e9" * (limit // 2 + 1)):
        with pytest.raises(RwError) as raised:
            request_health("PUT", body=body)
        assert raised.value.code == "invalid_input" and "4 MiB" in raised.value.message
    assert len(fake.requests()) == 1


# -- request headers ------------------------------------------------------------------------


def test_an_allowed_header_reaches_the_backend_and_ours_stay(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    get_health(headers={"Accept": "application/json"})
    (request,) = fake.requests()
    assert request["headers"]["Accept"] == "application/json"
    assert request["headers"]["User-Agent"] == "runwhen-capability-k8s-proxy-get"
    assert request["host"] == "127.0.0.1"


def test_without_an_accept_header_the_default_is_any(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    get_health()
    get_health(headers={})
    get_health(headers=None)
    assert [r["headers"]["Accept"] for r in fake.requests()] == ["*/*"] * 3


ALLOWED = {
    "Accept": "application/json",
    "Content-Type": "application/merge-patch+json",
    "Content-MD5": "Q2hlY2sgSW50ZWdyaXR5IQ==",
    "If-Match": '"abc"',
    "If-None-Match": "*",
    "Range": "bytes=0-9",
}


def test_every_allowed_header_in_any_case_is_sent(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    cased = {name.upper(): value for name, value in ALLOWED.items()}
    request_health("PATCH", body="{}", headers=cased)
    (request,) = fake.requests()
    got = {k.lower(): v for k, v in request["headers"].items()}
    assert {k.lower(): v for k, v in ALLOWED.items()}.items() <= got.items()


@pytest.mark.parametrize(
    "name",
    [
        "Authorization",
        "authorization",
        "Proxy-Authorization",
        "Proxy-Anything",
        "Cookie",
        "Set-Cookie",
        "Host",
        "X-Forwarded-For",
        "X-Forwarded-Host",
        "X-Remote-User",
        "Impersonate-User",
        "Impersonate-Group",
        "Impersonate-Extra-Scopes",
        "Connection",
        "Upgrade",
        "Content-Length",
        "Transfer-Encoding",
        "User-Agent",
        "X-Custom",
        "Accept-Encoding",
        "",
    ],
)
def test_a_header_off_the_allowlist_is_invalid_input_naming_it(tmp_path, monkeypatch, name):
    fake = vault(tmp_path, monkeypatch)
    with pytest.raises(RwError) as raised:
        get_health(headers={"Accept": "*/*", name: "x"})
    assert raised.value.code == "invalid_input"
    assert repr(name) in raised.value.message
    assert "Accept, Content-Type, Content-MD5, If-Match, If-None-Match, Range" in (
        raised.value.message
    )
    assert fake.commands() == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Accept": "a\r\nAuthorization: Bearer x"},
        {"Accept": "a\nb"},
        {"Accept": "a\rb"},
        {"Accept": "a\x00b"},
        {"Accept\r\nX": "a"},
        {"Accept\n": "a"},
        {"Accept\x00": "a"},
        {"Accept": "caf\u00e9"},
        {"Accept": 5},
        {"Accept": None},
        {5: "a"},
        {"Accept": "a", "accept": "b"},
        ["Accept: a"],
        "Accept: a",
    ],
)
def test_a_malformed_header_is_invalid_input(tmp_path, monkeypatch, headers):
    fake = vault(tmp_path, monkeypatch)
    with pytest.raises(RwError) as raised:
        get_health(headers=headers)
    assert raised.value.code == "invalid_input"
    assert fake.commands() == []


def test_a_header_value_may_hold_a_tab_or_space(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    get_health(headers={"Accept": "text/plain;\tq=0.5, */*"})
    assert fake.requests()[0]["headers"]["Accept"] == "text/plain;\tq=0.5, */*"


# -- port names -----------------------------------------------------------------------------


def port_route(kind, name, port):
    path = f"/api/v1/namespaces/ns/{kind}/{name}:{port}/proxy/x"
    return {path: {"body": "named"}}


def test_a_service_port_name_is_a_dns_label_of_up_to_63_characters(tmp_path, monkeypatch):
    chart = "http-artifactory"  # the Artifactory chart's service port
    longest = "a" * 62 + "1"
    fake = install_fake_kubectl(tmp_path, monkeypatch)
    assert len(chart) == 16 and len(longest) == 63
    for port in (chart, longest, "a--b", "9a"):
        fake.configure(routes=port_route("services", "s", port))
        assert proxy_get("ns", "services", "s", port, "x").body == b"named", port


@pytest.mark.parametrize("port", ["a" * 64, "-a", "a-", "A", "a_b", "a.b", "a" * 100])
def test_a_bad_service_port_name_is_invalid_input(tmp_path, monkeypatch, port):
    fake = vault(tmp_path, monkeypatch)
    with pytest.raises(RwError) as raised:
        proxy_get("ns", "services", "s", port, "x")
    assert raised.value.code == "invalid_input" and "port" in raised.value.message
    assert fake.commands() == []


def test_a_pod_port_name_is_an_iana_service_name_of_up_to_15_characters(tmp_path, monkeypatch):
    fake = install_fake_kubectl(tmp_path, monkeypatch)
    for port in ("a" * 15, "http-metrics", "9a", "a-b-c", "8080-x"):
        fake.configure(routes=port_route("pods", "p", port))
        assert proxy_get("ns", "pods", "p", port, "x").body == b"named", port


@pytest.mark.parametrize(
    "port", ["a" * 16, "http-artifactory", "1-2", "a--b", "-a", "a-", "A", "a_b", "a.b"]
)
def test_a_bad_pod_port_name_is_invalid_input(tmp_path, monkeypatch, port):
    fake = vault(tmp_path, monkeypatch)
    with pytest.raises(RwError) as raised:
        proxy_get("ns", "pods", "p", port, "x")
    assert raised.value.code == "invalid_input" and "port" in raised.value.message
    assert fake.commands() == []


@pytest.mark.parametrize("kind", ["pods", "services"])
def test_port_numbers_work_for_both_kinds(tmp_path, monkeypatch, kind):
    fake = install_fake_kubectl(
        tmp_path, monkeypatch, routes={**port_route(kind, "s", 1), **port_route(kind, "s", 65535)}
    )
    for port in (1, 65535, "1", "65535"):
        assert proxy_get("ns", kind, "s", port, "x").body == b"named"
    assert len(fake.requests()) == 4


# -- the task API for requests ---------------------------------------------------------------


def test_task_request_returns_the_dict_a_task_sees(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch, routes={"PUT " + HEALTH: {"status": 201, "body": "ok"}})
    got = task_request("PUT", "vault", "services", "vault", 8200, "/v1/sys/health", body="x")
    assert got["status"] == 201 and got["body"] == "ok"
    assert set(got) == {"status", "headers", "body", "truncated", "location"}
    assert fake.bodies() == [b"x"]


def test_ctx_k8s_proxy_request_and_get_headers(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    ctx = Context(capability="c", operation="t", workdir=tmp_path)
    got = ctx.k8s_proxy_request(
        "POST", "vault", "services", "vault", 8200, "/v1/sys/health", body="{}", timeout=10
    )
    assert got["status"] == 200
    ctx.k8s_proxy_get(
        "vault", "services", "vault", 8200, "/v1/sys/health", headers={"Accept": "application/json"}
    )
    first, second = fake.requests()
    assert (first["method"], fake.bodies()[0]) == ("POST", b"{}")
    assert (second["method"], second["headers"]["Accept"]) == ("GET", "application/json")
    with pytest.raises(RwError) as raised:
        ctx.k8s_proxy_request("TRACE", "vault", "services", "vault", 8200, "x")
    assert raised.value.code == "invalid_input"


# -- the command line for requests -----------------------------------------------------------


def run_request(*args, env=None):
    return run_cli("request", *args, env=env)


def test_the_request_command_sends_method_data_and_headers(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    done = run_request(
        "PUT", "vault", "services", "vault", "8200", "/v1/sys/health",
        "--data", '{"a":"\u00e9"}',
        "--header", "Content-Type: application/json",
        "--header", "Accept:application/json",
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["status"] == 200
    (request,) = fake.requests()
    assert request["method"] == "PUT"
    assert fake.bodies() == ['{"a":"\u00e9"}'.encode()]
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["headers"]["Accept"] == "application/json"


def test_the_request_command_reads_a_data_file_and_dash_data_is_not_an_option(
    tmp_path, monkeypatch
):
    fake = vault(tmp_path, monkeypatch)
    payload = tmp_path / "payload.bin"
    payload.write_bytes(bytes(range(256)))
    args = ("vault", "services", "vault", "8200", "/v1/sys/health")
    assert run_request("POST", *args, "--data-file", str(payload)).returncode == 0
    assert run_request("POST", *args, "--data", "--not-an-option").returncode == 0
    assert run_request("DELETE", *args).returncode == 0
    assert fake.bodies() == [bytes(range(256)), b"--not-an-option", b""]


@pytest.mark.parametrize(
    ("method", "extra"),
    [
        ("PUT", ["--header", "no colon here"]),
        ("PUT", ["--header", "Authorization: Bearer x"]),
        ("PUT", ["--header", "Accept: a\nb"]),
        ("PUT", ["--data-file", "/nonexistent/file"]),
        ("TRACE", []),
        ("GET", ["--data", "x"]),
    ],
)
def test_the_request_command_reports_bad_input_and_records_the_error(
    tmp_path, monkeypatch, method, extra
):
    fake = vault(tmp_path, monkeypatch)
    errors = tmp_path / "errors.jsonl"
    args = ("vault", "services", "vault", "8200", "/v1/sys/health")
    done = run_request(method, *args, *extra, env={"RW_ERRORS_FILE": str(errors)})
    assert done.returncode == 1, done.stderr
    assert done.stdout == ""
    assert json.loads(errors.read_text().splitlines()[0])["code"] == "invalid_input"
    assert fake.commands() == []


def test_data_and_data_file_exclude_each_other(tmp_path, monkeypatch):
    vault(tmp_path, monkeypatch)
    done = run_request(
        "PUT", "vault", "services", "vault", "8200", "x", "--data", "a", "--data-file", "/dev/null"
    )
    assert done.returncode == 2 and "not allowed with" in done.stderr


def test_the_request_command_refuses_a_data_file_over_4_mib(tmp_path, monkeypatch):
    vault(tmp_path, monkeypatch)
    big = tmp_path / "big"
    big.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    done = run_request("PUT", "vault", "services", "vault", "8200", "x", "--data-file", str(big))
    assert done.returncode == 1 and "4 MiB" in done.stderr


def test_the_get_command_takes_headers(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    done = run_cli(
        "get", "vault", "services", "vault", "8200", "/v1/sys/health",
        "--header", "Accept: application/json", "--header", "Range: bytes=0-4",
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    headers = fake.requests()[0]["headers"]
    assert (headers["Accept"], headers["Range"]) == ("application/json", "bytes=0-4")
    bad = run_cli("get", "vault", "services", "vault", "8200", "x", "--header", "Cookie: a=b")
    assert bad.returncode == 1 and "Cookie" in bad.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_rw_k8s_proxy_request_in_bash(tmp_path, monkeypatch):
    fake = vault(tmp_path, monkeypatch)
    rw_sh = tmp_path / "rw.sh"
    rw_sh.write_text(RW_SH)
    errors = tmp_path / "errors.jsonl"
    env = {
        **os.environ,
        "RW_PYTHON": sys.executable,
        "PYTHONPATH": str(Path(k8s_proxy.__file__).parents[1]),
        "RW_ERRORS_FILE": str(errors),
    }
    script = (
        f'source "{rw_sh}"\n'
        "out=$(rw_k8s_proxy_request PUT vault services vault 8200 /v1/sys/health "
        "--data '{\"a\":1}' --header 'Content-Type: application/json' --timeout 20)\n"
        'printf "%s" "$out"\n'
        'rw_k8s_proxy_request TRACE vault services vault 8200 x || echo "failed rc=$?" >&2\n'
        'rw_k8s_proxy_request PUT vault services || echo "usage rc=$?" >&2\n'
    )
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, check=False
    )
    assert json.loads(done.stdout)["status"] == 200, done.stderr
    assert fake.bodies() == [b'{"a":1}']
    assert fake.requests()[0]["headers"]["Content-Type"] == "application/json"
    assert "failed rc=1" in done.stderr and "usage rc=2" in done.stderr
    assert json.loads(errors.read_text().splitlines()[0])["code"] == "invalid_input"


# -- recording a request ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "field", "value"),
    [
        ("café\nline two\r\n\ttab", "body", "café\nline two\r\n\ttab"),
        (b'{"a":1}', "body", '{"a":1}'),
        (b"\xff\x00", "bodyBase64", "/wA="),  # not UTF-8
        ("a\x85b", "bodyBase64", "YcKFYg=="),  # NEL: a scenario file would not keep it
        ("a\x00b", "bodyBase64", "YQBi"),
        ("﻿bom", "bodyBase64", "77u/Ym9t"),
    ],
)
def test_a_recorded_body_is_text_when_utf_8_and_a_scenario_keeps_it_else_base64(
    tmp_path, monkeypatch, body, field, value
):
    from runwhen_capability.contracts import load_yaml
    from runwhen_capability.plugin.fixtures import dump_scenario

    vault(tmp_path, monkeypatch)
    log = tmp_path / "shim.log"
    monkeypatch.setenv("RW_SHIM_MODE", "record")
    monkeypatch.setenv("RW_SHIM_LOG", str(log))
    task_request(
        "PUT",
        "vault",
        "services",
        "vault",
        8200,
        "/v1/sys/health",
        body=body,
        headers={"Content-Type": "text/plain", "ACCEPT": "A/B"},
    )
    (call,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert call["type"] == "k8s-proxy-request"
    request = call["request"]
    assert request["method"] == "PUT"
    assert request["headers"] == {"content-type": "text/plain", "accept": "A/B"}  # lower-cased
    assert request[field] == value
    assert ("bodyBase64" in request) != ("body" in request)
    # ... and a scenario file keeps it: dump, load, same request
    text = dump_scenario(
        resource=None, inputs={}, settings={}, identity={}, calls=[call], expect={"status": "ok"}
    )
    assert load_yaml(text)["calls"][0]["request"] == request


def test_a_get_is_recorded_as_k8s_proxy_get_without_a_method_and_headers_only_when_given(
    tmp_path, monkeypatch
):
    vault(tmp_path, monkeypatch)
    log = tmp_path / "shim.log"
    monkeypatch.setenv("RW_SHIM_MODE", "record")
    monkeypatch.setenv("RW_SHIM_LOG", str(log))
    task_get("vault", "services", "vault", 8200, "/v1/sys/health")
    task_get("vault", "services", "vault", 8200, "/v1/sys/health", headers={"Accept": "a/b"})
    first, second = [json.loads(line) for line in log.read_text().splitlines()]
    base = {
        "namespace": "vault",
        "kind": "services",
        "name": "vault",
        "port": 8200,
        "path": "/v1/sys/health",
        "scheme": "http",
    }
    assert (first["type"], first["request"]) == ("k8s-proxy-get", base)
    assert (second["type"], second["request"]) == (
        "k8s-proxy-get",
        {**base, "headers": {"accept": "a/b"}},
    )


def test_a_bad_method_header_or_body_is_raised_in_every_mode_and_is_not_a_call(
    tmp_path, monkeypatch
):
    vault(tmp_path, monkeypatch)
    log = tmp_path / "shim.log"
    monkeypatch.setenv("RW_SHIM_MODE", "record")
    monkeypatch.setenv("RW_SHIM_LOG", str(log))
    calls = (
        lambda: task_get("vault", "services", "vault", 8200, "x", headers={"Authorization": "x"}),
        lambda: task_request("GET", "vault", "services", "vault", 8200, "x", body="x"),
        lambda: task_request("TRACE", "vault", "services", "vault", 8200, "x"),
    )
    for call in calls:
        with pytest.raises(RwError) as raised:
            call()
        assert raised.value.code == "invalid_input"
    assert not log.exists()
