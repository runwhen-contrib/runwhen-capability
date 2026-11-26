"""rwtask serve with several poll workers (RW_MAX_CONCURRENT / --concurrency): parallel
task runs in one pod, per-run tool caches, the per-run memory guard and the poll's
memoryPeakBytes. No relay and no container: a fake session, a fake cgroup directory."""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from pathlib import Path

import pytest
from test_bundle_security import _files, _run

import runwhen_capability.serve as serve_mod
from runwhen_capability import bundle
from runwhen_capability.models import ResultEnvelope, SetupResult
from runwhen_capability.serve import (
    _ACTIVE_SCOPES,
    _retain_or_wipe_scope,
    cgroup_memory_limit,
    cgroup_memory_peak,
    resolve_concurrency,
    serve,
)

FIXTURES = Path(__file__).parent / "fixtures" / "capabilities"
RELAY = "http://relay.example.internal"


class _Resp:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _request_body(n: int) -> dict:
    return {
        "requestId": f"req-{n}",
        "request": {
            "version": 1,
            "setup": {"task": "prep", "inputs": {"userName": "w"}},
            "tasks": [{"task": "echo", "inputs": {"greeting": "g", "items": []}}],
        },
        "credentials": {},
        "scopeId": f"scope-{n}",
        "deadlineMs": 60000,
    }


class _FakeSession:
    def __init__(self, requests_to_serve: int):
        self._lock = threading.Lock()
        self._left = requests_to_serve
        self._n = 0
        self.polls: list[dict] = []
        self.results: dict[str, dict] = {}

    def post(self, url, json=None, data=None, timeout=None, headers=None):
        with self._lock:
            if url.endswith("/v1/tasks/next"):
                self.polls.append(json)
                if self._left <= 0:
                    return _Resp(204)
                self._left -= 1
                self._n += 1
                return _Resp(200, _request_body(self._n))
            request_id = url.split("/")[-2]
            self.results[request_id] = _loads(data)
            return _Resp(200, {})


def _loads(data: bytes) -> dict:
    return json.loads(data)


def _token(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text("tok")
    return path


def _cgroup(tmp_path: Path, **files: str) -> Path:
    directory = tmp_path / "cgroup"
    directory.mkdir(exist_ok=True)
    for name, text in files.items():
        (directory / name.replace("_", ".")).write_text(text)
    return directory


def test_resolve_concurrency_flag_then_env_then_one_and_never_below_one(monkeypatch):
    monkeypatch.delenv("RW_MAX_CONCURRENT", raising=False)
    assert resolve_concurrency() == 1
    monkeypatch.setenv("RW_MAX_CONCURRENT", "4")
    assert resolve_concurrency() == 4
    assert resolve_concurrency(2) == 2
    monkeypatch.setenv("RW_MAX_CONCURRENT", "0")
    assert resolve_concurrency() == 1
    monkeypatch.setenv("RW_MAX_CONCURRENT", "lots")
    assert resolve_concurrency() == 1
    assert resolve_concurrency(-3) == 1


def test_two_workers_run_two_requests_at_the_same_time(tmp_path, monkeypatch):
    """Both runs must be inside run_request together to pass the barrier."""
    barrier = threading.Barrier(2, timeout=10)
    scopes: list[Path] = []

    def fake_run_request(capability, request, credentials, scope_dir, **kwargs):
        scopes.append(scope_dir)
        barrier.wait()
        return ResultEnvelope(setup=SetupResult(status="ok", outputs={}), tasks=[])

    monkeypatch.setattr(serve_mod, "run_request", fake_run_request)
    session = _FakeSession(2)
    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=tmp_path / "work",
        capability_dir=FIXTURES / "echo",
        token_file=_token(tmp_path),
        session=session,
        max_iterations=1,
        concurrency=2,
        cgroup_dir=_cgroup(tmp_path),
    )
    assert {r["status"] for r in session.results.values()} == {"ok"}
    assert len(session.results) == 2
    assert len({s.name for s in scopes}) == 2  # each request had its own scope dir


def test_the_token_is_read_once_before_the_workers_start(tmp_path):
    token = _token(tmp_path)
    session = _FakeSession(0)
    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=tmp_path / "work",
        capability_dir=FIXTURES / "echo",
        token_file=token,
        session=session,
        max_iterations=2,
        concurrency=3,
        cgroup_dir=_cgroup(tmp_path),
    )
    assert not token.exists()  # unlinked by the one read
    assert len(session.polls) == 6  # and every worker still polled, with the shared token


def test_the_poll_body_carries_memory_peak_bytes_when_readable(tmp_path):
    session = _FakeSession(0)
    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=tmp_path / "work",
        capability_dir=FIXTURES / "echo",
        token_file=_token(tmp_path),
        session=session,
        max_iterations=1,
        cgroup_dir=_cgroup(tmp_path, memory_peak="12345\n", memory_current="99\n"),
    )
    assert session.polls == [{"poolId": "pool-1", "memoryPeakBytes": 12345}]


def test_memory_peak_falls_back_to_current_and_is_omitted_when_unreadable(tmp_path):
    assert cgroup_memory_peak(_cgroup(tmp_path, memory_current="77\n")) == 77
    assert cgroup_memory_peak(tmp_path / "nowhere") is None
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "memory.peak").write_text("max\n")
    assert cgroup_memory_peak(junk) is None
    session = _FakeSession(0)
    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=tmp_path / "work",
        capability_dir=FIXTURES / "echo",
        token_file=_token(tmp_path),
        session=session,
        max_iterations=1,
        cgroup_dir=tmp_path / "nowhere",
    )
    assert session.polls == [{"poolId": "pool-1"}]


@pytest.mark.parametrize(
    ("workers", "max_text", "expected"),
    [(1, "1000\n", None), (4, "4000\n", 1000), (4, "max\n", None), (4, None, None)],
)
def test_the_per_run_memory_limit_is_the_cgroup_limit_over_the_concurrency(
    tmp_path, workers, max_text, expected
):
    cgroup = _cgroup(tmp_path, **({"memory_max": max_text} if max_text else {}))
    assert cgroup_memory_limit(cgroup) == (
        int(max_text) if max_text and max_text[0] != "m" else None
    )
    try:
        serve(
            relay=RELAY,
            pool_id="pool-1",
            workdir=tmp_path / "work",
            capability_dir=FIXTURES / "echo",
            token_file=_token(tmp_path),
            session=_FakeSession(0),
            max_iterations=1,
            concurrency=workers,
            cgroup_dir=cgroup,
        )
        assert bundle._default_memory_limit_bytes == expected
    finally:
        bundle.set_default_memory_limit(None)


def test_an_explicit_smaller_run_limit_wins_over_the_default():
    try:
        bundle.set_default_memory_limit(1000)
        assert bundle._effective_memory_limit(None) == 1000
        assert bundle._effective_memory_limit(400) == 400
        assert bundle._effective_memory_limit(5000) == 1000
        bundle.set_default_memory_limit(None)
        assert bundle._effective_memory_limit(None) is None
        assert bundle._effective_memory_limit(5000) == 5000
    finally:
        bundle.set_default_memory_limit(None)


def test_the_workdir_root_is_made_0711(tmp_path):
    workdir = tmp_path / "work"
    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=workdir,
        capability_dir=FIXTURES / "echo",
        token_file=_token(tmp_path),
        session=_FakeSession(0),
        max_iterations=1,
        cgroup_dir=_cgroup(tmp_path),
    )
    assert workdir.stat().st_mode & 0o777 == 0o711


def test_stateful_lru_eviction_is_safe_across_threads_and_spares_running_scopes(tmp_path):
    class Cap:
        execution_mode = "stateful"

    lru: OrderedDict[str, None] = OrderedDict()
    for i in range(20):
        (tmp_path / f"s{i}").mkdir()
    _ACTIVE_SCOPES["s0"] = 1  # a worker is running in s0: it must not be evicted
    try:
        threads = [
            threading.Thread(
                target=_retain_or_wipe_scope,
                args=(Cap(), tmp_path / f"s{i}", tmp_path, lru, 4, serve_mod.logging.getLogger()),
            )
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        _ACTIVE_SCOPES.pop("s0", None)
    assert len(lru) <= 4 + 1
    assert (tmp_path / "s0").exists()
    for name in lru:
        assert (tmp_path / name).exists()


_ENV_PROBE = """\
import json, os


def main(ctx):
    names = [
        "HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "KUBECACHEDIR",
        "HELM_CACHE_HOME", "HELM_CONFIG_HOME", "HELM_DATA_HOME", "AZURE_CONFIG_DIR",
        "CLOUDSDK_CONFIG", "PIP_CACHE_DIR", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
    ]
    return {"o": json.dumps({n: os.environ.get(n) for n in names})}
"""


def test_a_run_gets_its_tool_caches_inside_its_home_over_the_images_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDSDK_CONFIG", "/work/.config/gcloud")
    monkeypatch.setenv("XDG_CACHE_HOME", "/shared/cache")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/shared/aws")
    result = _run(_files("t.py", _ENV_PROBE), tmp_path)
    [task] = result.tasks
    assert task.status == "ok", task
    env = json.loads(task.outputs["o"])
    home = env["HOME"]
    assert home == str(tmp_path / ".rw-home")
    for name, value in env.items():
        if name != "HOME":
            assert value is not None and value.startswith(home + "/"), (name, value)
    assert env["CLOUDSDK_CONFIG"] == f"{home}/.config/gcloud"
    assert env["AWS_CONFIG_FILE"] == f"{home}/.aws/config"


def test_credential_env_wins_over_the_tool_cache_defaults():
    env = bundle.tool_cache_env("/h")
    assert env["KUBECACHEDIR"] == "/h/.kube/cache"
    assert {**env, "AWS_CONFIG_FILE": "/run/rw/credentials/x/aws"}["AWS_CONFIG_FILE"] == (
        "/run/rw/credentials/x/aws"
    )


def test_tool_cache_names_are_on_the_caller_env_deny_list_except_credential_files():
    from runwhen_capability.custom.manifest import is_reserved_env_name

    cache_names = set(bundle.tool_cache_env("/h")) - {
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
    }
    assert all(is_reserved_env_name(n) for n in cache_names)
    # a materialized AWS credential file sets these, and wins: they stay allowed
    assert not is_reserved_env_name("AWS_SHARED_CREDENTIALS_FILE")
    assert not is_reserved_env_name("KUBECONFIG")
