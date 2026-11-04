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
from runwhen_capability.k8s_proxy import DEFAULT_MAX_BODY, ProxyResponse, proxy_get, task_get
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
            seen.append({"path": self.path, "headers": dict(self.headers.items())})
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

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
        {"port": "a" * 16},
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
