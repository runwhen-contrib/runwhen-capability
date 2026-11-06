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
import threading
import time
from pathlib import Path

import pytest
import responses

import runwhen_capability.serve as serve_mod
from runwhen_capability import bundle
from runwhen_capability.custom.hashing import content_hash
from runwhen_capability.plugin import run as plugin_run
from runwhen_capability.relay import TokenNotPresent
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


def test_the_mode_is_logged_once_when_the_file_is_removed(tmp_path, caplog):
    path = tmp_path / "token"
    path.write_text("tok")
    with caplog.at_level(logging.INFO, logger=LOG.name):
        _load_executor_token(path, LOG)
    assert caplog.text.count("executor token mode:") == 1
    assert "held in memory only" in caplog.text


def test_the_token_is_stripped_and_the_file_unlinked(tmp_path):
    path = tmp_path / "token"
    path.write_text("  secret-token\n")
    tokens = _load_executor_token(path, LOG)
    assert tokens.get() == "secret-token"
    assert not tokens.rereads_file
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
            assert _load_executor_token(path, LOG).get() == "tok"
    finally:
        secrets.chmod(0o755)
    assert path.exists()
    assert caplog.text.count("executor token file left in place (read-only mount)") == 1
    assert caplog.text.count("executor token mode:") == 1
    assert "re-reading" in caplog.text
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
        assert _load_executor_token(path, LOG).get() == "tok"
    assert caplog.text.count("executor token file left in place (read-only mount)") == 1


@pytest.mark.parametrize("content", ["", "  \n\t\n"])
def test_an_empty_token_file_is_pending_not_an_error(tmp_path, caplog, content):
    path = tmp_path / "token"
    path.write_text(content)
    with caplog.at_level(logging.INFO, logger=LOG.name):
        tokens = _load_executor_token(path, LOG)
    assert tokens.pending
    assert path.exists()  # nothing to unlink yet
    assert caplog.text.count(f"executor token not present yet at {path}; waiting for it") == 1
    assert caplog.records[0].levelno == logging.WARNING
    with pytest.raises(TokenNotPresent):
        tokens.get()


def test_a_missing_token_file_is_pending_not_an_error(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger=LOG.name):
        tokens = _load_executor_token(tmp_path / "nope", LOG)
    assert tokens.pending
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    with pytest.raises(TokenNotPresent):
        tokens.get()
    assert caplog.text.count("executor token not present yet") == 1  # waiting is not re-logged


def test_a_pending_source_adopts_the_token_when_the_file_appears_and_unlinks_it(tmp_path, caplog):
    path = tmp_path / "token"
    tokens = _load_executor_token(path, LOG)
    path.write_text("")
    with pytest.raises(TokenNotPresent):
        tokens.get()  # still empty: still pending
    path.write_text(" late-token\n")

    with caplog.at_level(logging.INFO, logger=LOG.name):
        assert tokens.get() == "late-token"

    assert not path.exists()
    assert not tokens.pending
    assert not tokens.rereads_file
    assert caplog.text.count("executor token mode:") == 1
    assert "held in memory only" in caplog.text
    path.write_text("other")  # whatever happens to the file next changes nothing
    assert tokens.get() == "late-token"
    assert caplog.text.count("executor token mode:") == 1


def test_a_pending_source_that_cannot_unlink_becomes_a_re_reading_one(
    tmp_path, monkeypatch, caplog
):
    path = tmp_path / "token"
    tokens = _load_executor_token(path, LOG)
    path.write_text("late-token")
    monkeypatch.setattr(serve_mod.os, "unlink", _erofs)

    with caplog.at_level(logging.INFO, logger=LOG.name):
        assert tokens.get() == "late-token"
        path.write_text("rotated")
        assert tokens.get() == "rotated"

    assert path.exists()
    assert tokens.rereads_file
    assert caplog.text.count("executor token mode:") == 1
    assert caplog.text.count("left in place (read-only mount)") == 1


def _erofs(_path):
    raise OSError(errno.EROFS, "Read-only file system")


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
def test_serve_skips_polls_while_pending_then_reads_unlinks_and_uses_a_late_token(
    tmp_path, monkeypatch, caplog
):
    token_file = tmp_path / "late" / "token"
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:  # the token turns up while serve is waiting
            token_file.parent.mkdir()
            token_file.write_text("late-token\n")

    monkeypatch.setattr(serve_mod.time, "sleep", sleep)
    responses.add(responses.POST, f"{RELAY}/v1/tasks/next", status=204)

    with caplog.at_level(logging.INFO):
        _serve(tmp_path, token_file, max_iterations=4)

    assert len(sleeps) == 2  # the first two iterations were skipped
    assert [c.request.headers["Authorization"] for c in responses.calls] == [
        "Bearer late-token"
    ] * 2
    assert not token_file.exists()  # read once, then unlinked
    assert caplog.text.count("executor token not present yet") == 1
    assert caplog.text.count("held in memory only") == 1
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@responses.activate
def test_an_empty_token_file_keeps_serve_waiting(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("\n")
    monkeypatch.setattr(serve_mod.time, "sleep", lambda _s: None)

    _serve(tmp_path, token_file, max_iterations=2)

    assert len(responses.calls) == 0
    assert token_file.exists()


def _refuse_unlink(monkeypatch):
    def refuse(_path):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(serve_mod.os, "unlink", refuse)


# -- the token file cannot be removed (a read-only Secret mount: today's runner) -----------
#
# The kubelet refreshes a mounted Secret in place on rotation, so here the file stays and
# is re-read before every relay call, with the last token read as the fallback.


@responses.activate
def test_serve_tolerates_a_failed_unlink_and_logs_the_mode(tmp_path, monkeypatch, caplog):
    token_file = tmp_path / "token"
    token_file.write_text("token-a")
    _refuse_unlink(monkeypatch)
    responses.add(responses.POST, f"{RELAY}/v1/tasks/next", status=204)

    with caplog.at_level(logging.INFO):
        _serve(tmp_path, token_file, max_iterations=1)

    assert token_file.exists()
    assert "executor token file left in place (read-only mount)" in caplog.text
    assert caplog.text.count("executor token mode:") == 1
    assert responses.calls[0].request.headers["Authorization"] == "Bearer token-a"


@responses.activate
def test_a_token_file_that_stays_is_re_read_on_each_poll_so_a_rotation_is_picked_up(
    tmp_path, monkeypatch
):
    token_file = tmp_path / "token"
    token_file.write_text("token-a")
    _refuse_unlink(monkeypatch)

    def poll(request):
        token_file.write_text("token-b")  # the Secret is refreshed after the first poll
        return (204, {}, "")

    responses.add_callback(responses.POST, f"{RELAY}/v1/tasks/next", callback=poll)
    _serve(tmp_path, token_file, max_iterations=2)

    assert [c.request.headers["Authorization"] for c in responses.calls] == [
        "Bearer token-a",
        "Bearer token-b",
    ]


@responses.activate
def test_a_token_file_that_stays_is_re_read_immediately_before_the_result_post(
    tmp_path, monkeypatch
):
    from runwhen_capability.host import run_request as real_run_request

    token_file = tmp_path / "token"
    token_file.write_text("token-old")
    _refuse_unlink(monkeypatch)

    def rotate_after_running(capability, request, credentials, scope_dir, log=None, **kwargs):
        result = real_run_request(capability, request, credentials, scope_dir, log=log, **kwargs)
        token_file.write_text("token-new")  # rotates between the poll and the POST
        return result

    monkeypatch.setattr(serve_mod, "run_request", rotate_after_running)
    _one_request(responses)

    _serve(tmp_path, token_file, max_iterations=1)

    next_call, result_call = responses.calls
    assert next_call.request.headers["Authorization"] == "Bearer token-old"
    assert result_call.request.headers["Authorization"] == "Bearer token-new"


@responses.activate
def test_a_token_file_that_stays_falls_back_to_the_last_token_when_unreadable_at_post_time(
    tmp_path, monkeypatch, caplog
):
    from runwhen_capability.host import run_request as real_run_request

    token_file = tmp_path / "token"
    token_file.write_text("token-old")
    _refuse_unlink(monkeypatch)

    def remove_after_running(capability, request, credentials, scope_dir, log=None, **kwargs):
        result = real_run_request(capability, request, credentials, scope_dir, log=log, **kwargs)
        token_file.write_text("")  # a refresh caught half-way: empty, then ...
        return result

    monkeypatch.setattr(serve_mod, "run_request", remove_after_running)
    _one_request(responses)

    with caplog.at_level(logging.WARNING):
        _serve(tmp_path, token_file, max_iterations=1)

    next_call, result_call = responses.calls
    assert result_call.request.headers["Authorization"] == "Bearer token-old"
    assert "using the last token read" in caplog.text


@responses.activate
def test_a_token_file_that_stays_and_then_vanishes_keeps_the_last_token(
    tmp_path, monkeypatch, caplog
):
    token_file = tmp_path / "token"
    token_file.write_text("token-a")
    _refuse_unlink(monkeypatch)

    def poll(request):
        if token_file.exists():
            os.remove(token_file)  # (Path.unlink is the patched os.unlink)
        return (204, {}, "")

    responses.add_callback(responses.POST, f"{RELAY}/v1/tasks/next", callback=poll)
    with caplog.at_level(logging.WARNING):
        _serve(tmp_path, token_file, max_iterations=2)

    assert [c.request.headers["Authorization"] for c in responses.calls] == ["Bearer token-a"] * 2
    assert "could not re-read executor token" in caplog.text


def _one_request(rsps):
    rsps.add(
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
    rsps.add(responses.POST, f"{RELAY}/v1/tasks/req-1/result", json={})


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


@responses.activate
def test_the_relay_client_of_a_request_follows_a_rotation_when_the_file_stays(
    tmp_path, monkeypatch
):
    token_file = tmp_path / "token"
    token_file.write_text("token-a")
    _refuse_unlink(monkeypatch)
    seen = []
    real = serve_mod.RelayClient

    def spy(*args, **kwargs):
        client = real(*args, **kwargs)
        token_file.write_text("token-b")  # rotated after the client was made
        seen.append(client._auth())
        return client

    monkeypatch.setattr(serve_mod, "RelayClient", spy)
    _one_request(responses)

    _serve(tmp_path, token_file, max_iterations=1)

    assert seen == [{"Authorization": "Bearer token-b"}]


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
@pytest.mark.parametrize("mode", ["removable", "read_only"])
def test_a_task_cannot_read_the_token_from_the_file_or_from_the_serve_process(
    relay_server, tmp_path, mode
):
    """`removable`: the runner's new tmpfs copy -- serve unlinks it and holds the token in
    memory. `read_only`: today's Secret mount -- the unlink is refused, the file has to stay
    (so a task CAN read it; only the new runner closes that), serve re-reads it per call so a
    rotation in place is picked up, and the /proc reads are still denied."""
    from relay_server import Response

    read_only = mode == "read_only"
    if read_only and os.geteuid() == 0:
        pytest.skip("root can unlink from a read-only directory")
    token_file = tmp_path / "executor" / "token"
    token_file.parent.mkdir()
    token_file.write_text("super-secret-executor-token\n")
    if read_only:
        token_file.parent.chmod(0o555)

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
        _wait_for(
            lambda: any(r.path == "/v1/tasks/next" for r in relay_server.requests),
            "serve never polled",
        )
        assert token_file.exists() == read_only
        if read_only:
            token_file.write_text("rotated-executor-token\n")  # the Secret is refreshed
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
        token_file.parent.chmod(0o755)
        sibling.kill()
        if serve_proc is not None:
            serve_proc.terminate()
            _, stderr = serve_proc.communicate(timeout=30)
    [result] = [r for r in relay_server.requests if r.path.endswith("/result")]

    body = json.loads(result.body)
    assert body["status"] == "ok", body
    [task] = body["result"]["tasks"]
    assert task["status"] == "ok", task
    file_state = "ok" if read_only else "missing"
    assert task["outputs"]["o"] == (
        f"file={file_state} environ=denied mem=denied sibling=ok tokenfds=0"
    ), task
    assert b"super-secret-executor-token" not in result.body
    auth = {r.header("Authorization") for r in relay_server.requests}
    if read_only:
        assert result.header("Authorization") == "Bearer rotated-executor-token", stderr
        assert auth == {"Bearer super-secret-executor-token", "Bearer rotated-executor-token"}
    else:
        # every relay call was authenticated with the in-memory token
        assert auth == {"Bearer super-secret-executor-token"}, stderr


@linux_only
def test_serve_with_no_token_file_loads_its_capability_and_picks_the_token_up_later(
    relay_server, tmp_path
):
    """What an image smoke test sees: `rwtask serve` with no token file gets as far as
    loading its capability, logs no error, and when a token appears it polls with it and
    removes the file."""
    from relay_server import Response

    token_file = tmp_path / "executor" / "token"
    token_file.parent.mkdir()
    relay_server.default = Response(204)
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
        ],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: list[str] = []
    reader = threading.Thread(
        target=lambda: lines.extend(iter(serve_proc.stderr.readline, "")), daemon=True
    )
    reader.start()
    try:
        # Wait for serve to say it is waiting and has loaded its capability: a fixed sleep
        # raced a slow interpreter start on CI, so the token existed before serve looked.
        _wait_for(
            lambda: (
                any("executor token not present yet" in line for line in lines)
                and any("serving capability" in line for line in lines)
            ),
            "serve never logged that it is waiting for the token and serving",
            60,
        )
        assert serve_proc.poll() is None, "serve exited without a token"
        assert relay_server.requests == []  # nothing sent without a token
        token_file.write_text("late-executor-token\n")
        _wait_for(lambda: relay_server.requests, "serve never polled with the late token", 30)
    finally:
        serve_proc.terminate()
        serve_proc.wait(timeout=30)
        reader.join(timeout=10)
    stderr = "".join(lines)

    assert {r.header("Authorization") for r in relay_server.requests} == {
        "Bearer late-executor-token"
    }
    assert not token_file.exists()
    assert " ERROR " not in stderr, stderr


def _wait_for(condition, message: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.1)
    raise AssertionError(message)
