"""rwtask serve's bundle-mode dispatch: a polled request whose `request`
carries a `bundle` runs through bundle.run_bundle_request instead of
host.run_request, and its scope is always wiped afterwards (see serve.py's
_poll_once)."""

from __future__ import annotations

import json
from pathlib import Path

import responses
from bundle_fixtures import load_bundle

from runwhen_capability.custom.hashing import content_hash
from runwhen_capability.serve import serve

FIXTURES = Path(__file__).parent / "fixtures" / "capabilities"
RELAY = "http://relay.example.internal"


def _token_file(tmp_path: Path, token: str = "test-token") -> Path:
    token_file = tmp_path / "token"
    token_file.write_text(token)
    return token_file


@responses.activate
def test_serve_dispatches_a_bundle_request_and_wipes_its_scope(tmp_path):
    files = load_bundle("pgbouncer-health")
    responses.add(
        responses.POST,
        f"{RELAY}/v1/tasks/next",
        json={
            "requestId": "req-1",
            "request": {
                "bundle": {
                    "hash": content_hash(files),
                    "files": [{"path": p, "content": c} for p, c in files.items()],
                },
                "tasks": ["pool-errors"],
                "inputs": {"since": "30m", "maxWait": 20},
                "target": {"urn": "urn:resource:pgbouncer"},
            },
            "credentials": {"kubeconfig": "kubeconfig-value"},
            "scopeId": "scope-1",
            "deadlineMs": 30000,
        },
        status=200,
    )
    responses.add(responses.POST, f"{RELAY}/v1/tasks/req-1/result", json={}, status=200)

    workdir = tmp_path / "work"
    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=workdir,
        capability_dir=FIXTURES / "echo",
        token_file=_token_file(tmp_path),
        max_iterations=1,
    )

    assert len(responses.calls) == 2
    result_body = json.loads(responses.calls[1].request.body)
    assert result_body["status"] == "ok"
    [task] = result_body["result"]["tasks"]
    assert task["task"] == "pool-errors"
    assert task["status"] == "ok"
    assert task["outputs"]["summary"] == {"windowMinutes": 30, "pods": 2, "total": 3}

    # Bundle mode has no execution.mode/stateful concept -- always wiped.
    assert not (workdir / "scope-1").exists()


@responses.activate
def test_serve_bundle_request_hash_mismatch_reports_a_failed_setup(tmp_path):
    files = load_bundle("pgbouncer-health")
    responses.add(
        responses.POST,
        f"{RELAY}/v1/tasks/next",
        json={
            "requestId": "req-2",
            "request": {
                "bundle": {
                    "hash": "sha256:" + "0" * 64,
                    "files": [{"path": p, "content": c} for p, c in files.items()],
                },
                "tasks": ["pool-errors"],
            },
            "credentials": {},
            "scopeId": "scope-2",
            "deadlineMs": 30000,
        },
        status=200,
    )
    responses.add(responses.POST, f"{RELAY}/v1/tasks/req-2/result", json={}, status=200)

    workdir = tmp_path / "work"
    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=workdir,
        capability_dir=FIXTURES / "echo",
        token_file=_token_file(tmp_path),
        max_iterations=1,
    )

    result_body = json.loads(responses.calls[1].request.body)
    assert result_body["status"] == "ok"
    assert result_body["result"]["setup"]["status"] == "failed"
    assert "hash mismatch" in result_body["result"]["setup"]["error"]


def test_serve_startup_removes_bundle_scopes_a_crashed_run_left_but_keeps_others(tmp_path):
    workdir = tmp_path / "work"
    stale = workdir / "req-crashed"
    (stale / ".rw-sdk").mkdir(parents=True)
    (stale / ".rw-sdk" / "rw.sh").write_text("# rw.sh")
    (stale / ".secrets-abc").mkdir()
    (stale / ".secrets-abc" / "token").write_text("left-behind-secret")
    warm = workdir / "rk-stateful"
    warm.mkdir()
    (warm / "checkout.txt").write_text("a packaged capability's warm scope")

    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=workdir,
        capability_dir=FIXTURES / "echo",
        token_file=_token_file(tmp_path),
        max_iterations=0,
    )

    assert not stale.exists()
    assert (warm / "checkout.txt").exists()
