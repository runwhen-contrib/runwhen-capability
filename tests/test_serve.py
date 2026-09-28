"""rwtask serve's long-poll loop against a fake relay, mocked with
`responses`. Exercises the exact wire contract in serve.py's module
docstring: POST {relay}/v1/tasks/next -> 200|204, POST
{relay}/v1/tasks/{requestId}/result.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import responses

from runwhen_capability.serve import serve

FIXTURES = Path(__file__).parent / "fixtures" / "capabilities"
RELAY = "http://relay.example.internal"


def _token_file(tmp_path: Path, token: str = "test-token") -> Path:
    token_file = tmp_path / "token"
    token_file.write_text(token)
    return token_file


@responses.activate
def test_serve_processes_one_request_and_posts_the_result(tmp_path):
    responses.add(
        responses.POST,
        f"{RELAY}/v1/tasks/next",
        json={
            "requestId": "req-1",
            "request": {
                "version": 1,
                "setup": {"task": "prep", "inputs": {"userName": "world"}},
                "tasks": [
                    {
                        "task": "echo",
                        "inputs": {"greeting": "${setup.greeting}", "items": "${setup.items}"},
                    }
                ],
            },
            "credentials": {},
            "scopeId": "scope-1",
            "deadlineMs": 60000,
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
    next_call, result_call = responses.calls

    assert next_call.request.url == f"{RELAY}/v1/tasks/next"
    import json as _json

    assert _json.loads(next_call.request.body) == {"poolId": "pool-1"}

    result_body = _json.loads(result_call.request.body)
    assert result_body["status"] == "ok"
    assert result_body["result"]["setup"]["outputs"] == {
        "greeting": "hello world",
        "items": ["a", "b"],
    }
    assert result_body["result"]["tasks"][0]["outputs"] == {"result": "hello world:a,b"}

    # The "echo" fixture declares no `execution` block, so it defaults to
    # stateless -- the scope directory is wiped after every request. See
    # test_serve_stateful_capability_keeps_the_scope_warm_across_requests
    # below for the sibling `stateful` behaviour, which must NOT wipe.
    assert not (workdir / "scope-1").exists()


@responses.activate
def test_serve_idle_204_polls_again_without_posting_a_result(tmp_path):
    responses.add(responses.POST, f"{RELAY}/v1/tasks/next", status=204)

    workdir = tmp_path / "work"
    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=workdir,
        capability_dir=FIXTURES / "echo",
        token_file=_token_file(tmp_path),
        max_iterations=1,
    )

    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == f"{RELAY}/v1/tasks/next"


@responses.activate
def test_serve_a_failing_task_still_posts_status_ok_with_the_failed_entry(tmp_path):
    responses.add(
        responses.POST,
        f"{RELAY}/v1/tasks/next",
        json={
            "requestId": "req-2",
            "request": {"version": 1, "tasks": [{"task": "boom", "inputs": {}}]},
            "credentials": {},
            "scopeId": "scope-2",
            "deadlineMs": 60000,
        },
        status=200,
    )
    responses.add(responses.POST, f"{RELAY}/v1/tasks/req-2/result", json={}, status=200)

    workdir = tmp_path / "work"
    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=workdir,
        capability_dir=FIXTURES / "fails",
        token_file=_token_file(tmp_path),
        max_iterations=1,
    )

    import json as _json

    result_body = _json.loads(responses.calls[1].request.body)
    assert result_body["status"] == "ok"
    assert result_body["result"]["tasks"][0]["status"] == "failed"
    assert "kaboom" in result_body["result"]["tasks"][0]["error"]


@responses.activate
def test_serve_sends_bearer_token_from_the_token_file_on_both_relay_calls(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret-token\n")

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
        status=200,
    )
    responses.add(responses.POST, f"{RELAY}/v1/tasks/req-1/result", json={}, status=200)

    workdir = tmp_path / "work"
    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=workdir,
        capability_dir=FIXTURES / "echo",
        token_file=token_file,
        max_iterations=1,
    )

    next_call, result_call = responses.calls
    assert next_call.request.headers["Authorization"] == "Bearer secret-token"
    assert result_call.request.headers["Authorization"] == "Bearer secret-token"


@responses.activate
def test_serve_rereads_the_token_file_on_each_poll_so_a_rotation_is_picked_up(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("token-a")
    workdir = tmp_path / "work"

    responses.add(responses.POST, f"{RELAY}/v1/tasks/next", status=204)
    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=workdir,
        capability_dir=FIXTURES / "echo",
        token_file=token_file,
        max_iterations=1,
    )
    assert responses.calls[0].request.headers["Authorization"] == "Bearer token-a"

    token_file.write_text("token-b")
    responses.add(responses.POST, f"{RELAY}/v1/tasks/next", status=204)
    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=workdir,
        capability_dir=FIXTURES / "echo",
        token_file=token_file,
        max_iterations=1,
    )
    assert responses.calls[1].request.headers["Authorization"] == "Bearer token-b"


@responses.activate
def test_serve_missing_token_file_logs_and_keeps_polling_without_a_request(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr("runwhen_capability.serve.time.sleep", lambda _seconds: None)

    workdir = tmp_path / "work"
    missing_token_file = tmp_path / "does-not-exist" / "token"

    with caplog.at_level(logging.ERROR):
        serve(
            relay=RELAY,
            pool_id="pool-1",
            workdir=workdir,
            capability_dir=FIXTURES / "echo",
            token_file=missing_token_file,
            max_iterations=1,
        )

    # No relay call is made -- serve() never falls back to an unauthenticated request.
    assert len(responses.calls) == 0
    assert "could not read executor token" in caplog.text


# --- execution.mode: stateful ------------------------------------------------
#
# The "worktree_sync" fixture mimics rw-worktree: `execution.mode: stateful`
# in its manifest, a setup ("open") that requires a "repo" credential and
# writes a file into the scope, and a task ("read") that reads it back.


def _worktree_next_response(request_id, scope_id, sha, credentials):
    return {
        "requestId": request_id,
        "request": {
            "version": 1,
            "setup": {
                "task": "open",
                "inputs": {"repoUrl": "https://example.test/repo.git", "sha": sha},
            },
            "tasks": [{"task": "read", "inputs": {"tree": "${setup.tree}"}}],
        },
        "credentials": credentials,
        "scopeId": scope_id,
        "deadlineMs": 60000,
    }


@responses.activate
def test_serve_stateful_capability_keeps_the_scope_warm_across_requests(tmp_path):
    """A second request against the same scopeId must find its setup
    cached and its materialised tree still present -- the whole point of
    `stateful` mode (a warm sticky pod). This is the real `rwtask serve`
    poll-loop path, not `rwtask run`'s single-shot one; it fails against
    the unconditional shutil.rmtree in serve.py before this fix."""
    responses.add(
        responses.POST,
        f"{RELAY}/v1/tasks/next",
        json=_worktree_next_response("req-1", "scope-shared", "sha-1", {"repo": "token-1"}),
        status=200,
    )
    responses.add(responses.POST, f"{RELAY}/v1/tasks/req-1/result", json={}, status=200)
    responses.add(
        responses.POST,
        f"{RELAY}/v1/tasks/next",
        # Same scopeId, same setup inputs, no credentials -- a synchronous
        # call's shape. Only recoverable via a cache hit.
        json=_worktree_next_response("req-2", "scope-shared", "sha-1", {}),
        status=200,
    )
    responses.add(responses.POST, f"{RELAY}/v1/tasks/req-2/result", json={}, status=200)

    workdir = tmp_path / "work"
    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=workdir,
        capability_dir=FIXTURES / "worktree_sync",
        token_file=_token_file(tmp_path),
        max_iterations=2,
    )

    import json as _json

    first_result = _json.loads(responses.calls[1].request.body)
    second_result = _json.loads(responses.calls[3].request.body)

    assert first_result["result"]["setup"]["status"] == "ok"
    assert second_result["result"]["setup"]["status"] == "cached"
    assert second_result["result"]["tasks"][0]["outputs"] == {
        "content": "hello from https://example.test/repo.git@sha-1"
    }

    # The scope was NOT wiped between requests, and is still there now.
    assert (workdir / "scope-shared" / "tree" / "hello.txt").exists()


@responses.activate
def test_serve_stateful_capability_does_not_share_scopes_across_scope_ids(tmp_path):
    """Two different scopeIds, even with identical setup inputs, must not
    see each other's cache or files -- a scope is only ever warm for the
    scopeId it was created under."""
    responses.add(
        responses.POST,
        f"{RELAY}/v1/tasks/next",
        json=_worktree_next_response("req-1", "scope-a", "sha-1", {"repo": "token-1"}),
        status=200,
    )
    responses.add(responses.POST, f"{RELAY}/v1/tasks/req-1/result", json={}, status=200)
    responses.add(
        responses.POST,
        f"{RELAY}/v1/tasks/next",
        # A different scopeId, same setup inputs, no credentials -- if
        # scope-b could see scope-a's cache this would come back "cached".
        json=_worktree_next_response("req-2", "scope-b", "sha-1", {}),
        status=200,
    )
    responses.add(responses.POST, f"{RELAY}/v1/tasks/req-2/result", json={}, status=200)

    workdir = tmp_path / "work"
    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=workdir,
        capability_dir=FIXTURES / "worktree_sync",
        token_file=_token_file(tmp_path),
        max_iterations=2,
    )

    import json as _json

    first_result = _json.loads(responses.calls[1].request.body)
    second_result = _json.loads(responses.calls[3].request.body)

    assert first_result["result"]["setup"]["status"] == "ok"
    # Not cached, and not credentialed -- scope-b has nothing of its own to
    # fall back on, which is exactly the point: it never inherited scope-a's.
    assert second_result["result"]["setup"]["status"] == "not_materialized"

    assert (workdir / "scope-a" / "tree" / "hello.txt").exists()
    assert not (workdir / "scope-b" / "tree").exists()


@responses.activate
def test_serve_stateful_lru_eviction_wipes_the_evicted_scope_from_disk(tmp_path):
    """Bounding growth (`max_stateful_scopes`) evicts the least-recently-used
    scope, and eviction means the directory is actually removed from disk,
    not just forgotten by the LRU."""
    for i, scope_id in enumerate(["scope-1", "scope-2", "scope-3"]):
        request_id = f"req-{i + 1}"
        responses.add(
            responses.POST,
            f"{RELAY}/v1/tasks/next",
            json=_worktree_next_response(request_id, scope_id, f"sha-{i + 1}", {"repo": "token"}),
            status=200,
        )
        responses.add(responses.POST, f"{RELAY}/v1/tasks/{request_id}/result", json={}, status=200)

    workdir = tmp_path / "work"
    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=workdir,
        capability_dir=FIXTURES / "worktree_sync",
        token_file=_token_file(tmp_path),
        max_iterations=3,
        max_stateful_scopes=2,
    )

    # scope-1 is the least-recently-used once scope-3 arrives with the cap
    # at 2 -- evicted, and its directory wiped from disk.
    assert not (workdir / "scope-1").exists()
    assert (workdir / "scope-2").exists()
    assert (workdir / "scope-3" / "tree" / "hello.txt").exists()


# --- scopeId hygiene and result delivery ----------------------------------


@responses.activate
@pytest.mark.parametrize("bad_scope_id", ["", ".", "..", "../escape", "/absolute", "a/b"])
def test_serve_refuses_a_scope_id_that_is_not_a_single_path_segment(
    tmp_path, monkeypatch, caplog, bad_scope_id
):
    """`workdir / scopeId` is created and later rmtree'd, so a scopeId that
    is not one path segment would delete something that is not a scope
    (`Path("/work") / "/x"` is `/x`; `Path("/work") / ""` is the workdir
    itself). The request is refused before anything touches disk, and the
    runner is told why rather than being left to time the lease out."""
    monkeypatch.setattr("runwhen_capability.serve.time.sleep", lambda _seconds: None)
    responses.add(
        responses.POST,
        f"{RELAY}/v1/tasks/next",
        json={
            "requestId": "req-bad",
            "request": {"version": 1, "tasks": [{"task": "echo", "inputs": {}}]},
            "credentials": {},
            "scopeId": bad_scope_id,
            "deadlineMs": 60000,
        },
        status=200,
    )
    responses.add(responses.POST, f"{RELAY}/v1/tasks/req-bad/result", json={}, status=200)

    workdir = tmp_path / "work"
    canary = tmp_path / "canary.txt"
    canary.write_text("must survive")

    with caplog.at_level(logging.ERROR):
        serve(
            relay=RELAY,
            pool_id="pool-1",
            workdir=workdir,
            capability_dir=FIXTURES / "echo",
            token_file=_token_file(tmp_path),
            max_iterations=1,
        )

    assert workdir.exists() and canary.exists()
    assert "refusing scopeId" in caplog.text

    import json as _json

    result_body = _json.loads(responses.calls[1].request.body)
    assert result_body["status"] == "failed"
    assert "invalid scopeId" in result_body["error"]


@responses.activate
def test_serve_rereads_the_token_immediately_before_posting_the_result(tmp_path, monkeypatch):
    """A request can run up to requestTimeoutSeconds (600s for rw-checks)
    between the poll and the result POST -- long enough for the token to
    rotate mid-request. Re-reading only once per poll (at the top) would
    make the POST carry a now-stale token and lose an already-computed
    result to a 401; the POST must pick up the token as it stands
    immediately before it fires, not the one the poll started with."""
    import runwhen_capability.serve as serve_mod
    from runwhen_capability.host import run_request as real_run_request

    token_file = tmp_path / "token"
    token_file.write_text("token-old")

    def rotate_after_running(capability, request, credentials, scope_dir, log=None):
        result = real_run_request(capability, request, credentials, scope_dir, log=log)
        # The rotation happens AFTER the request ran but BEFORE the result
        # is posted -- exactly the window _post_result must re-read across.
        token_file.write_text("token-new")
        return result

    monkeypatch.setattr(serve_mod, "run_request", rotate_after_running)

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
        status=200,
    )
    responses.add(responses.POST, f"{RELAY}/v1/tasks/req-1/result", json={}, status=200)

    serve(
        relay=RELAY,
        pool_id="pool-1",
        workdir=tmp_path / "work",
        capability_dir=FIXTURES / "echo",
        token_file=token_file,
        max_iterations=1,
    )

    next_call, result_call = responses.calls
    assert next_call.request.headers["Authorization"] == "Bearer token-old"
    assert result_call.request.headers["Authorization"] == "Bearer token-new"


@responses.activate
def test_serve_falls_back_to_the_polls_token_when_the_token_file_is_unreadable_at_post_time(
    tmp_path, monkeypatch, caplog
):
    """A result already in hand must never be discarded because the token
    file happens to be briefly unreadable right at post time -- fall back
    to the token the poll already had, and say so in the log, rather than
    posting with no Authorization header at all or dropping the result."""
    import logging

    import runwhen_capability.serve as serve_mod
    from runwhen_capability.host import run_request as real_run_request

    token_file = tmp_path / "token"
    token_file.write_text("token-old")

    def remove_token_after_running(capability, request, credentials, scope_dir, log=None):
        result = real_run_request(capability, request, credentials, scope_dir, log=log)
        token_file.unlink()  # transient unreadable-ness right before the POST
        return result

    monkeypatch.setattr(serve_mod, "run_request", remove_token_after_running)

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
        status=200,
    )
    responses.add(responses.POST, f"{RELAY}/v1/tasks/req-1/result", json={}, status=200)

    with caplog.at_level(logging.WARNING):
        serve(
            relay=RELAY,
            pool_id="pool-1",
            workdir=tmp_path / "work",
            capability_dir=FIXTURES / "echo",
            token_file=token_file,
            max_iterations=1,
        )

    next_call, result_call = responses.calls
    assert next_call.request.headers["Authorization"] == "Bearer token-old"
    # Falls back to the poll's own token -- the result still gets posted,
    # not dropped -- and the fallback is logged, not silent.
    assert result_call.request.headers["Authorization"] == "Bearer token-old"
    assert "falling back to the token this poll started with" in caplog.text


@responses.activate
def test_serve_logs_a_non_2xx_on_the_result_post_instead_of_assuming_delivery(tmp_path, caplog):
    """A 500 (or a 401 from a rotated token) on PutResult means the result
    never landed. Silently treating the POST as delivered is the same
    "looks fine, isn't" shape everything else here guards against."""
    responses.add(
        responses.POST,
        f"{RELAY}/v1/tasks/next",
        json={
            "requestId": "req-3",
            "request": {"version": 1, "tasks": [{"task": "echo", "inputs": {}}]},
            "credentials": {},
            "scopeId": "scope-3",
            "deadlineMs": 60000,
        },
        status=200,
    )
    responses.add(responses.POST, f"{RELAY}/v1/tasks/req-3/result", json={}, status=500)

    with caplog.at_level(logging.ERROR):
        serve(
            relay=RELAY,
            pool_id="pool-1",
            workdir=tmp_path / "work",
            capability_dir=FIXTURES / "echo",
            token_file=_token_file(tmp_path),
            max_iterations=1,
        )

    assert "result not delivered" in caplog.text
