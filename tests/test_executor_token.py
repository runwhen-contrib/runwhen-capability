"""S1 (SEC-2), SDK half: `rwtask serve` reads the executor token ONCE, keeps it in
memory, unlinks the file, and makes itself non-dumpable, so a task that runs as the same
uid in the same pod cannot read the token off disk or out of the serve process.

The unit tests drive serve.py's helpers and the loop against `responses`; the
end-to-end test (Linux only) runs a real `rwtask serve` against a local fake relay and
has a bash task try to read the token from the file and from /proc."""

from __future__ import annotations

import errno
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import responses

import runwhen_capability.serve as serve_mod
from runwhen_capability import bundle
from runwhen_capability.custom.hashing import content_hash
from runwhen_capability.plugin import run as plugin_run
from runwhen_capability.serve import (
    DEFAULT_TOKEN_FILE,
    _load_executor_token,
    _resolve_token_file,
    serve,
)

# conftest.py stubs the module attribute for every other test (calling it for real would
# make the pytest process itself non-dumpable); these tests exercise the real function.
REAL_MAKE_NON_DUMPABLE = serve_mod._make_non_dumpable

FIXTURES = Path(__file__).parent / "fixtures" / "capabilities"
RELAY = "http://relay.example.internal"
LOG = logging.getLogger("test-executor-token")


# -- path resolution ----------------------------------------------------------------------


def test_the_token_path_comes_from_the_flag_first(tmp_path, monkeypatch):
    monkeypatch.setenv("RW_EXECUTOR_TOKEN_FILE", "/rw/token")
    monkeypatch.setenv("EXECUTOR_TOKEN_FILE", "/legacy/token")
    assert _resolve_token_file(tmp_path / "flag") == tmp_path / "flag"


def test_then_rw_executor_token_file(monkeypatch):
    monkeypatch.setenv("RW_EXECUTOR_TOKEN_FILE", "/rw/token")
    monkeypatch.setenv("EXECUTOR_TOKEN_FILE", "/legacy/token")
    assert _resolve_token_file(None) == Path("/rw/token")


def test_then_the_legacy_executor_token_file(monkeypatch):
    monkeypatch.delenv("RW_EXECUTOR_TOKEN_FILE", raising=False)
    monkeypatch.setenv("EXECUTOR_TOKEN_FILE", "/legacy/token")
    assert _resolve_token_file(None) == Path("/legacy/token")


def test_then_the_default_path(monkeypatch):
    monkeypatch.delenv("RW_EXECUTOR_TOKEN_FILE", raising=False)
    monkeypatch.delenv("EXECUTOR_TOKEN_FILE", raising=False)
    assert _resolve_token_file(None) == Path(DEFAULT_TOKEN_FILE)


def test_an_empty_rw_executor_token_file_falls_through(monkeypatch):
    monkeypatch.setenv("RW_EXECUTOR_TOKEN_FILE", "")
    monkeypatch.setenv("EXECUTOR_TOKEN_FILE", "/legacy/token")
    assert _resolve_token_file(None) == Path("/legacy/token")


# -- loading the token --------------------------------------------------------------------


def test_the_token_is_stripped_and_the_file_unlinked(tmp_path):
    path = tmp_path / "token"
    path.write_text("  secret-token\n")
    assert _load_executor_token(path, LOG) == "secret-token"
    assert not path.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can unlink from a read-only directory")
def test_an_unlink_refused_by_a_read_only_directory_is_tolerated_and_logged(tmp_path, caplog):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    path = secrets / "token"
    path.write_text("tok")
    secrets.chmod(0o555)
    try:
        with caplog.at_level(logging.INFO, logger=LOG.name):
            assert _load_executor_token(path, LOG) == "tok"
    finally:
        secrets.chmod(0o755)
    assert path.exists()
    assert caplog.text.count("executor token file left in place (read-only mount)") == 1
    assert str(path) in caplog.text
    assert "tok" not in caplog.text.replace("token", "")


def test_an_unlink_that_fails_with_erofs_is_tolerated_and_logged_once(
    tmp_path, monkeypatch, caplog
):
    path = tmp_path / "token"
    path.write_text("tok")

    def refuse(_path):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(serve_mod.os, "unlink", refuse)
    with caplog.at_level(logging.INFO, logger=LOG.name):
        assert _load_executor_token(path, LOG) == "tok"
    assert caplog.text.count("executor token file left in place (read-only mount)") == 1


@pytest.mark.parametrize("content", ["", "  \n\t\n"])
def test_an_empty_token_file_is_a_clear_startup_error(tmp_path, content):
    path = tmp_path / "token"
    path.write_text(content)
    with pytest.raises(SystemExit) as excinfo:
        _load_executor_token(path, LOG)
    assert "empty" in str(excinfo.value)
    assert str(path) in str(excinfo.value)


def test_a_missing_token_file_is_a_clear_startup_error(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        _load_executor_token(tmp_path / "nope", LOG)
    assert "could not read executor token" in str(excinfo.value)


# -- serve(): read once -------------------------------------------------------------------


def _serve(tmp_path, token_file, **kwargs):
    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=tmp_path / "work",
        capability_dir=FIXTURES / "echo",
        token_file=token_file,
        **kwargs,
    )


@responses.activate
def test_serve_keeps_using_the_startup_token_whatever_happens_to_the_file(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("token-a")

    def poll(request):
        # after start-up the file is changed (here: recreated with other content)
        token_file.write_text("token-b")
        return (204, {}, "")

    responses.add_callback(responses.POST, f"{RELAY}/v1/tasks/next", callback=poll)
    _serve(tmp_path, token_file, max_iterations=3)

    assert [c.request.headers["Authorization"] for c in responses.calls] == ["Bearer token-a"] * 3


@responses.activate
def test_serve_keeps_working_when_the_file_is_deleted_after_start(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("token-a")
    responses.add(responses.POST, f"{RELAY}/v1/tasks/next", status=204)

    _serve(tmp_path, token_file, max_iterations=2)

    assert not token_file.exists()  # serve unlinked it ...
    assert len(responses.calls) == 2  # ... and still polled with the in-memory token
    assert responses.calls[1].request.headers["Authorization"] == "Bearer token-a"


@responses.activate
def test_serve_unlinks_the_token_file_and_tolerates_a_failed_unlink(tmp_path, monkeypatch, caplog):
    token_file = tmp_path / "token"
    token_file.write_text("token-a")
    monkeypatch.setattr(
        serve_mod.os, "unlink", lambda _p: (_ for _ in ()).throw(PermissionError(errno.EACCES, "x"))
    )
    responses.add(responses.POST, f"{RELAY}/v1/tasks/next", status=204)

    with caplog.at_level(logging.INFO):
        _serve(tmp_path, token_file, max_iterations=1)

    assert token_file.exists()
    assert "executor token file left in place (read-only mount)" in caplog.text
    assert responses.calls[0].request.headers["Authorization"] == "Bearer token-a"


@responses.activate
def test_the_relay_client_serve_hands_a_request_carries_the_startup_token(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("token-a")
    seen = []
    real = serve_mod.RelayClient

    def spy(*args, **kwargs):
        client = real(*args, **kwargs)
        seen.append(client._auth())
        return client

    monkeypatch.setattr(serve_mod, "RelayClient", spy)
    responses.add(
        responses.POST,
        f"{RELAY}/v1/tasks/next",
        json={
            "requestId": "req-1",
            "request": {"version": 1, "tasks": [{"task": "echo", "inputs": {}}]},
            "credentials": {},
            "scopeId": "scope-1",
            "deadlineMs": 60000,
        },
    )
    responses.add(responses.POST, f"{RELAY}/v1/tasks/req-1/result", json={})

    _serve(tmp_path, token_file, max_iterations=1)

    assert seen == [{"Authorization": "Bearer token-a"}]


def test_serve_makes_itself_non_dumpable_before_it_reads_the_token(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(serve_mod, "_make_non_dumpable", lambda *a, **k: calls.append("dumpable"))

    def load(path, log):
        calls.append("token")
        return "tok"

    monkeypatch.setattr(serve_mod, "_load_executor_token", load)
    with responses.RequestsMock():
        _serve(tmp_path, tmp_path / "token", max_iterations=0)

    assert calls == ["dumpable", "token"]


# -- _make_non_dumpable -------------------------------------------------------------------


class _FakeLibc:
    def __init__(self, result=0):
        self.calls = []
        self.result = result

    def prctl(self, *args):
        self.calls.append(args)
        return self.result


def _fake_cdll(monkeypatch, libc, platform="linux"):
    opened = []

    def cdll(name, use_errno=False):
        opened.append((name, use_errno))
        return libc

    monkeypatch.setattr(serve_mod.sys, "platform", platform)
    monkeypatch.setattr(serve_mod.ctypes, "CDLL", cdll)
    return opened


def test_make_non_dumpable_calls_prctl_pr_set_dumpable_zero_on_linux(monkeypatch):
    libc = _FakeLibc(0)
    opened = _fake_cdll(monkeypatch, libc)
    assert REAL_MAKE_NON_DUMPABLE() is True
    assert libc.calls == [(4, 0, 0, 0, 0)]
    assert opened == [(None, True)]


def test_make_non_dumpable_warns_with_the_errno_and_carries_on(monkeypatch, caplog):
    libc = _FakeLibc(-1)
    _fake_cdll(monkeypatch, libc)
    monkeypatch.setattr(serve_mod.ctypes, "get_errno", lambda: errno.EPERM)
    with caplog.at_level(logging.WARNING):
        assert REAL_MAKE_NON_DUMPABLE() is False
    assert "PR_SET_DUMPABLE" in caplog.text
    assert f"errno {errno.EPERM}" in caplog.text


def test_make_non_dumpable_does_nothing_off_linux(monkeypatch):
    libc = _FakeLibc(0)
    opened = _fake_cdll(monkeypatch, libc, platform="darwin")
    assert REAL_MAKE_NON_DUMPABLE() is False
    assert libc.calls == []
    assert opened == []


def test_make_non_dumpable_survives_a_libc_that_cannot_be_loaded(monkeypatch, caplog):
    def boom(name, use_errno=False):
        raise OSError("no libc")

    monkeypatch.setattr(serve_mod.sys, "platform", "linux")
    monkeypatch.setattr(serve_mod.ctypes, "CDLL", boom)
    with caplog.at_level(logging.WARNING):
        assert REAL_MAKE_NON_DUMPABLE() is False
    assert "no libc" in caplog.text


# -- the token path never reaches a child ----------------------------------------------------


def test_rw_executor_token_file_is_host_only():
    assert {"RW_EXECUTOR_TOKEN_FILE", "EXECUTOR_TOKEN_FILE", "RELAY_URL", "POOL_ID"} <= (
        bundle._HOST_ONLY_ENV
    )


def test_a_child_does_not_inherit_the_token_path_variables(monkeypatch):
    for name in ("RW_EXECUTOR_TOKEN_FILE", "EXECUTOR_TOKEN_FILE", "RELAY_URL", "POOL_ID"):
        monkeypatch.setenv(name, "/some/path")
    inherited = bundle._inherited_env()
    assert not {"RW_EXECUTOR_TOKEN_FILE", "EXECUTOR_TOKEN_FILE", "RELAY_URL", "POOL_ID"} & set(
        inherited
    )


def test_extra_env_may_not_set_rw_executor_token_file():
    with pytest.raises(ValueError, match="extra_env may not set"):
        plugin_run.check_extra_env({"RW_EXECUTOR_TOKEN_FILE": "/x"})


# (the bundle child and the V13 lease child are covered in test_bundle_security.py and
# test_serve_lease.py, next to the other host-only variables)


def test_no_child_spawn_leaks_file_descriptors():
    """Children are spawned with close_fds (the Popen default) and pass_fds limited to the
    output pipe; the token is read in a `with` block and never in argv or env."""
    import inspect

    from runwhen_capability import artifacts, context

    for module in (bundle, context, artifacts, serve_mod):
        source = inspect.getsource(module)
        assert "close_fds=False" not in source, module.__name__
        assert "preexec_fn" not in source, module.__name__
    assert inspect.getsource(bundle).count("pass_fds=(write_fd,)") == 1
    assert "pass_fds" not in inspect.getsource(context)
    assert "pass_fds" not in inspect.getsource(artifacts)


# -- end to end: a real `rwtask serve` and a task that tries to read the token ---------------


linux_only = pytest.mark.skipif(sys.platform != "linux", reason="/proc and prctl are Linux-only")

_MANIFEST = """\
apiVersion: runwhen.com/custom-capability/v1
name: probe
tasks:
  - name: t
    file: tasks/t.sh
    effects: [Test fixture]
    inputs:
      tokenPath: { type: string, runtime: true }
      servePid: { type: string, runtime: true }
      siblingPid: { type: string, runtime: true }
    outputs:
      o: { schema: "string" }
"""

_TASK = """\
source "$RW_SDK/rw.sh"

# ok = the read worked; denied = EACCES/EPERM; missing = no such file
try_read() {
  local err rc=0
  err=$(cat "$1" 2>&1 >/dev/null) || rc=$?
  if [ "$rc" -eq 0 ]; then echo ok
  elif [[ "$err" == *"Permission denied"* || "$err" == *"Operation not permitted"* ]]; then
    echo denied
  elif [[ "$err" == *"No such file"* ]]; then echo missing
  else echo "other:$err"; fi
}

file=$(try_read "$TOKEN_PATH")
environ=$(try_read "/proc/$SERVE_PID/environ")
mem=$(try_read "/proc/$SERVE_PID/mem")
sibling=$(try_read "/proc/$SIBLING_PID/environ")
fds=$(ls -l /proc/$$/fd 2>/dev/null | grep -c "$TOKEN_PATH" || true)
rw_set o "\\"file=$file environ=$environ mem=$mem sibling=$sibling tokenfds=$fds\\""
"""


@linux_only
def test_a_task_cannot_read_the_token_from_the_file_or_from_the_serve_process(
    relay_server, tmp_path
):
    from relay_server import Response

    token_file = tmp_path / "executor" / "token"
    token_file.parent.mkdir()
    token_file.write_text("super-secret-executor-token\n")

    files = {"capability.yaml": _MANIFEST, "tasks/t.sh": _TASK}
    # a same-uid process that stays dumpable: proves the /proc reads are denied because of
    # serve's non-dumpable flag, not because this sandbox forbids all /proc access
    sibling = subprocess.Popen(["sleep", "120"], env={"FOO": "bar"})
    serve_proc = None
    try:
        relay_server.default = Response(204)
        # the pid of the serve process is only known once it is started, so the poll
        # answer is scripted after the fact (the first poll is answered 204 until then)
        serve_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "runwhen_capability.cli",
                "serve",
                "--relay",
                relay_server.url,
                "--pool",
                "pool-1",
                "--workdir",
                str(tmp_path / "work"),
                "--capability-dir",
                str(FIXTURES / "echo"),
                "--token-file",
                str(token_file),
                "--allow-bundles",
            ],
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        _wait_for(lambda: not token_file.exists(), "serve never unlinked the token file")
        relay_server.script(
            "POST",
            "/v1/tasks/next",
            Response(
                200,
                json={
                    "requestId": "req-1",
                    "request": {
                        "bundle": {
                            "hash": content_hash(files),
                            "files": [{"path": p, "content": c} for p, c in files.items()],
                        },
                        "tasks": ["t"],
                        "inputs": {
                            "tokenPath": str(token_file),
                            "servePid": str(serve_proc.pid),
                            "siblingPid": str(sibling.pid),
                        },
                    },
                    "credentials": {},
                    "scopeId": "scope-1",
                    "deadlineMs": 60000,
                },
            ),
        )
        relay_server.script("POST", "/v1/tasks/req-1/result", Response(200, json={}))
        _wait_for(
            lambda: any(r.path.endswith("/result") for r in relay_server.requests),
            "serve never posted a result",
        )
    finally:
        sibling.kill()
        if serve_proc is not None:
            serve_proc.terminate()
            _, stderr = serve_proc.communicate(timeout=30)
    [result] = [r for r in relay_server.requests if r.path.endswith("/result")]

    body = json.loads(result.body)
    assert body["status"] == "ok", body
    [task] = body["result"]["tasks"]
    assert task["status"] == "ok", task
    assert task["outputs"]["o"] == (
        "file=missing environ=denied mem=denied sibling=ok tokenfds=0"
    ), task
    assert b"super-secret-executor-token" not in result.body
    # every relay call was authenticated with the in-memory token
    auth = {r.header("Authorization") for r in relay_server.requests}
    assert auth == {"Bearer super-secret-executor-token"}, stderr


def _wait_for(condition, message: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.1)
    raise AssertionError(message)
