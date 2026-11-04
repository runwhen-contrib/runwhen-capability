"""A fake `kubectl` for k8s_proxy tests: `kubectl proxy` serves a tiny local HTTP server that
emulates the API server's pod/service proxy path from a JSON config; any other kubectl
command prints a canned answer. Everything it does is appended to a log, one JSON line per
event, so a test can see what was asked of it.

    fake = install_fake_kubectl(tmp_path, monkeypatch, routes={"<request path>": {...}})
    fake.requests()    # [{"path", "headers", "host"}, ...] the proxy was asked for
    fake.commands()    # [argv, ...] every kubectl invocation after the program name
    fake.proxy_pid()   # the pid of the last fake proxy (for "it is gone afterwards")

A route is `{"status": 200, "headers": {"X": "y"}, "body": "text"}` (or `body_b64`); a header
value may use `{proxy}` for `127.0.0.1:<the proxy's port>`. `mode` is `serve` (default),
`hang` (never prints the "Starting to serve" line) or `fail` (prints `stderr` and exits `rc`).
`api_status` answers every request itself, as the API server would.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

SCRIPT = textwrap.dedent(
    """\
    #!{python}
    import base64, json, os, sys, time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    cfg = json.load(open(os.environ["FAKE_KUBECTL_CONFIG"]))


    def note(entry):
        with open(cfg["log"], "a") as handle:
            handle.write(json.dumps(entry) + "\\n")


    args = sys.argv[1:]
    note({{"kubectl": args}})
    if args[:1] != ["proxy"]:
        sys.stdout.write(cfg.get("other_stdout", ""))
        sys.exit(cfg.get("other_rc", 0))
    mode = cfg.get("mode", "serve")
    if mode == "fail":
        sys.stderr.write(cfg.get("stderr", ""))
        sys.exit(cfg.get("rc", 1))
    with open(cfg["pidfile"], "w") as handle:
        handle.write(str(os.getpid()))
    if mode == "hang":
        time.sleep(600)


    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def do_GET(self):
            note({{"path": self.path, "headers": dict(self.headers.items()),
                  "host": self.headers.get("Host")}})
            route = cfg.get("api_status") or cfg.get("routes", {{}}).get(self.path)
            if route is None:
                route = {{"status": 404, "body": "no route " + self.path}}
            body = base64.b64decode(route["body_b64"]) if "body_b64" in route else \\
                route.get("body", "").encode()
            self.send_response(route.get("status", 200))
            for key, value in route.get("headers", {{}}).items():
                self.send_header(key, value.replace("{{proxy}}", "127.0.0.1:%d" % port))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    sys.stdout.write("Starting to serve on 127.0.0.1:%d\\n" % port)
    sys.stdout.flush()
    server.serve_forever()
    """
)


class FakeKubectl:
    def __init__(self, config_path: Path, log: Path, pidfile: Path) -> None:
        self.config_path = config_path
        self.log = log
        self.pidfile = pidfile

    def configure(self, **changes) -> None:
        cfg = json.loads(self.config_path.read_text())
        cfg.update(changes)
        self.config_path.write_text(json.dumps(cfg))

    def _lines(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def requests(self) -> list[dict]:
        return [e for e in self._lines() if "path" in e]

    def commands(self) -> list[list[str]]:
        return [e["kubectl"] for e in self._lines() if "kubectl" in e]

    def proxy_pid(self) -> int:
        return int(self.pidfile.read_text())

    def proxy_is_gone(self, wait: float = 3.0) -> bool:
        pid = self.proxy_pid()
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False


def install_fake_kubectl(tmp_path: Path, monkeypatch, **config) -> FakeKubectl:
    """A fake kubectl first on PATH; `config` is the JSON the fake reads (see the module)."""
    bin_dir = tmp_path / "fake-kubectl-bin"
    bin_dir.mkdir()
    script = bin_dir / "kubectl"
    script.write_text(SCRIPT.format(python=sys.executable))
    script.chmod(0o755)
    log = tmp_path / "fake-kubectl.log"
    pidfile = tmp_path / "fake-kubectl.pid"
    config_path = tmp_path / "fake-kubectl.json"
    config_path.write_text(json.dumps({"log": str(log), "pidfile": str(pidfile), **config}))
    monkeypatch.setenv("FAKE_KUBECTL_CONFIG", str(config_path))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return FakeKubectl(config_path, log, pidfile)
