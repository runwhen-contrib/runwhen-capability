"""host.run_request: ${setup.<name>} resolution, camelCase input -> snake_case
kwarg mapping, and the "one task raising must not lose the others" guarantee.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from runwhen_capability.host import run_request
from runwhen_capability.loader import load_capability
from runwhen_capability.models import RequestEnvelope

FIXTURES = Path(__file__).parent / "fixtures" / "capabilities"


def test_setup_output_resolution_and_camel_case_inputs(tmp_path):
    capability = load_capability(FIXTURES / "echo")
    request = RequestEnvelope.model_validate(
        {
            "version": 1,
            "setup": {"task": "prep", "inputs": {"userName": "world"}},
            "tasks": [
                {
                    "task": "echo",
                    "inputs": {"greeting": "${setup.greeting}", "items": "${setup.items}"},
                }
            ],
        }
    )

    result = run_request(capability, request, credentials={}, scope_dir=tmp_path)

    assert result.setup.status == "ok"
    assert result.setup.outputs == {"greeting": "hello world", "items": ["a", "b"]}

    assert len(result.tasks) == 1
    assert result.tasks[0].status == "ok"
    assert result.tasks[0].outputs == {"result": "hello world:a,b"}


def test_unresolved_placeholder_fails_only_that_task(tmp_path):
    capability = load_capability(FIXTURES / "echo")
    request = RequestEnvelope.model_validate(
        {
            "version": 1,
            "setup": {"task": "prep", "inputs": {"userName": "world"}},
            "tasks": [
                {"task": "echo", "inputs": {"greeting": "${setup.nope}", "items": "${setup.items}"}}
            ],
        }
    )

    result = run_request(capability, request, credentials={}, scope_dir=tmp_path)

    assert result.setup.status == "ok"
    assert result.tasks[0].status == "failed"
    assert "nope" in result.tasks[0].error


def test_a_task_raising_becomes_a_failed_entry_and_the_loop_survives(tmp_path):
    capability = load_capability(FIXTURES / "fails")
    request = RequestEnvelope.model_validate(
        {
            "version": 1,
            "tasks": [
                {"task": "boom", "inputs": {}},
                {"task": "good", "inputs": {}},
            ],
        }
    )

    result = run_request(capability, request, credentials={}, scope_dir=tmp_path)

    assert result.setup is None
    assert len(result.tasks) == 2

    boom, good = result.tasks
    assert boom.task == "boom"
    assert boom.status == "failed"
    assert "kaboom" in boom.error

    assert good.task == "good"
    assert good.status == "ok"
    assert good.outputs == {"ok": "fine"}


def test_unknown_task_becomes_a_failed_entry(tmp_path):
    capability = load_capability(FIXTURES / "fails")
    request = RequestEnvelope.model_validate(
        {"version": 1, "tasks": [{"task": "does-not-exist", "inputs": {}}]}
    )

    result = run_request(capability, request, credentials={}, scope_dir=tmp_path)

    assert result.tasks[0].status == "failed"
    assert "unknown task" in result.tasks[0].error


def test_unresolved_required_credential_fails_the_task_naming_it(tmp_path):
    """Part 1 fix: an unresolved, non-optional credential is a hard
    failure -- the task's error must name the credential, and the request
    must not silently proceed anonymously."""
    capability = load_capability(FIXTURES / "needs_credential")
    request = RequestEnvelope.model_validate(
        {"version": 1, "tasks": [{"task": "need_token", "inputs": {}}]}
    )

    result = run_request(capability, request, credentials={}, scope_dir=tmp_path)

    assert result.tasks[0].status == "failed"
    assert "token" in result.tasks[0].error


def test_allow_anonymous_credentials_degrades_an_unresolved_credential(tmp_path):
    """rwtask run --allow-anonymous (run_request(allow_anonymous_credentials=True))
    is the only path that may turn an unresolved REQUIRED credential into
    None instead of failing the task -- a deliberate local-dev override."""
    capability = load_capability(FIXTURES / "needs_credential")
    request = RequestEnvelope.model_validate(
        {"version": 1, "tasks": [{"task": "need_token", "inputs": {}}]}
    )

    result = run_request(
        capability,
        request,
        credentials={},
        scope_dir=tmp_path,
        allow_anonymous_credentials=True,
    )

    assert result.tasks[0].status == "ok"
    assert result.tasks[0].outputs == {"token": None}


# -- setup-output caching: a synchronous call against a warm scope ---------

_WORKTREE_REQUEST = {
    "version": 1,
    "setup": {
        "task": "open",
        "inputs": {"repoUrl": "https://example.invalid/repo.git", "sha": "deadbeef"},
    },
    "tasks": [{"task": "read", "inputs": {"tree": "${setup.tree}"}}],
}


def test_sync_request_reuses_cached_setup_and_does_not_re_run_it(tmp_path):
    """A sync-shaped request (setup present, no credentials) against a
    scope whose setup already ran (the leased row's own request, which
    does carry credentials) must reuse the cached outputs rather than
    re-running setup -- re-running would hard-fail on the missing
    credential (Context.credential() has no other behaviour)."""
    capability = load_capability(FIXTURES / "worktree_sync")
    request = RequestEnvelope.model_validate(_WORKTREE_REQUEST)

    leased = run_request(capability, request, credentials={"repo": "tok"}, scope_dir=tmp_path)
    assert leased.setup.status == "ok"

    synced = run_request(capability, request, credentials={}, scope_dir=tmp_path)

    assert synced.setup.status == "cached"
    assert synced.tasks[0].status == "ok"
    assert synced.tasks[0].outputs == {
        "content": "hello from https://example.invalid/repo.git@deadbeef"
    }


def test_sync_request_against_a_fresh_scope_fails_not_materialized(tmp_path):
    """No prior request has materialised this scope (a fresh or evicted
    pod) -- setup must actually attempt to run, and since the sync path
    carries no credentials, it must fail with the distinct
    'not_materialized' status the platform recovers from by retrying via
    a credentialed run -- never a generic failure, and never a
    leaked exception-class string."""
    capability = load_capability(FIXTURES / "worktree_sync")
    request = RequestEnvelope.model_validate(_WORKTREE_REQUEST)

    result = run_request(capability, request, credentials={}, scope_dir=tmp_path)

    assert result.setup.status == "not_materialized"
    assert "CredentialNotFoundError" not in result.setup.error
    assert result.tasks[0].status == "failed"


def test_cache_miss_when_the_request_names_a_different_repo_or_sha(tmp_path):
    """A cached setup for one (repoUrl, sha) must never be served to a
    request naming a different one -- that would be plausible content from
    the wrong commit. A mismatch is a cache miss like any other: setup is
    attempted again, which (with no credentials on this call) fails
    not_materialized rather than silently reusing the wrong tree."""
    capability = load_capability(FIXTURES / "worktree_sync")
    first = RequestEnvelope.model_validate(_WORKTREE_REQUEST)
    leased = run_request(capability, first, credentials={"repo": "tok"}, scope_dir=tmp_path)
    assert leased.setup.status == "ok"

    other = RequestEnvelope.model_validate(
        {
            "version": 1,
            "setup": {
                "task": "open",
                "inputs": {"repoUrl": "https://example.invalid/repo.git", "sha": "other-sha"},
            },
            "tasks": [{"task": "read", "inputs": {"tree": "${setup.tree}"}}],
        }
    )

    result = run_request(capability, other, credentials={}, scope_dir=tmp_path)

    assert result.setup.status == "not_materialized"


def test_cache_miss_when_the_cached_tree_has_been_deleted(tmp_path):
    """A cache recording outputs.tree as an absolute path that
    no longer exists (pod replaced, scope reaped, fresh volume) must be
    treated as a miss, not served as 'cached' -- a `cached` result whose
    tree is gone is exactly the state that lets a grep over it report a
    false 'no matches' and mislead the agent into claiming code
    didn't exist. With no credentials on the sync path, the retry must
    fail not_materialized rather than lie about a stale cache being good."""
    capability = load_capability(FIXTURES / "worktree_sync")
    request = RequestEnvelope.model_validate(_WORKTREE_REQUEST)

    leased = run_request(capability, request, credentials={"repo": "tok"}, scope_dir=tmp_path)
    assert leased.setup.status == "ok"

    shutil.rmtree(tmp_path / "tree")

    synced = run_request(capability, request, credentials={}, scope_dir=tmp_path)

    assert synced.setup.status == "not_materialized"


# -- TreeNotMaterializedError -> setup.status == "not_materialized" -------
# (The platform only recovers from setup.status == "not_materialized" --
# never a task's own status. A task that hits an unmaterialised tree must
# flip the envelope, not just fail that one task, or the platform has
# nothing to recover from and the caller gets a hard error instead of an
# invisible re-host.) -------------------------------------------------------


def test_a_task_hitting_an_unmaterialized_tree_reports_the_envelope_as_a_miss(tmp_path):
    """A tree that vanishes between setup and a later task in the same
    request (eviction, a reaped scope) must flip the whole envelope's
    setup.status to not_materialized -- the signal the platform already
    understands and recovers from by queueing a credentialed run and
    retrying -- while the task itself is still recorded failed."""
    capability = load_capability(FIXTURES / "worktree_sync")
    request = RequestEnvelope.model_validate(
        {
            "version": 1,
            "setup": {
                "task": "open",
                "inputs": {"repoUrl": "https://example.invalid/repo.git", "sha": "deadbeef"},
            },
            "tasks": [
                {"task": "wreck", "inputs": {"tree": "${setup.tree}"}},
                {"task": "grep", "inputs": {"tree": "${setup.tree}", "pattern": "hello"}},
            ],
        }
    )

    result = run_request(capability, request, credentials={"repo": "tok"}, scope_dir=tmp_path)

    assert result.tasks[1].task == "grep"
    assert result.tasks[1].status == "failed"
    assert result.setup is not None
    assert result.setup.status == "not_materialized"


def test_an_ordinary_task_exception_does_not_flip_setup_to_not_materialized(tmp_path):
    """Only TreeNotMaterializedError gets the not_materialized conversion
    -- an arbitrary task bug must stay a plain task failure with setup left
    untouched, or the platform would re-materialise (and re-run) forever on an
    ordinary bug instead of surfacing it."""
    capability = load_capability(FIXTURES / "fails")
    request = RequestEnvelope.model_validate(
        {"version": 1, "tasks": [{"task": "boom", "inputs": {}}]}
    )

    result = run_request(capability, request, credentials={}, scope_dir=tmp_path)

    assert result.tasks[0].status == "failed"
    assert "kaboom" in result.tasks[0].error
    assert result.setup is None


def test_a_genuine_zero_match_grep_on_a_real_tree_is_still_an_ordinary_ok_result(tmp_path):
    """A real, materialised tree with no matching lines is an ordinary
    empty result (the rw.repo_grep.v1 shape) -- setup.status must stay ok,
    never conflated with the tree itself being gone."""
    capability = load_capability(FIXTURES / "worktree_sync")
    request = RequestEnvelope.model_validate(
        {
            "version": 1,
            "setup": {
                "task": "open",
                "inputs": {"repoUrl": "https://example.invalid/repo.git", "sha": "deadbeef"},
            },
            "tasks": [
                {"task": "grep", "inputs": {"tree": "${setup.tree}", "pattern": "no-such-text"}}
            ],
        }
    )

    result = run_request(capability, request, credentials={"repo": "tok"}, scope_dir=tmp_path)

    assert result.setup.status == "ok"
    assert result.tasks[0].status == "ok"
    assert result.tasks[0].outputs == {
        "result": {
            "matches": [],
            "truncated": False,
            "unreadable": [],
            "unreadableTruncated": False,
        }
    }


def test_stateless_leased_requests_are_unaffected_by_setup_caching(tmp_path):
    """rw-checks-shaped usage: each leased request carries its own setup
    and credentials against its own scope. Setup-output caching must not
    change that -- two independent scopes never share a cache, so both
    still execute setup fresh."""
    capability = load_capability(FIXTURES / "echo")
    request = RequestEnvelope.model_validate(
        {
            "version": 1,
            "setup": {"task": "prep", "inputs": {"userName": "world"}},
            "tasks": [
                {
                    "task": "echo",
                    "inputs": {"greeting": "${setup.greeting}", "items": "${setup.items}"},
                }
            ],
        }
    )
    scope_a = tmp_path / "a"
    scope_a.mkdir()
    scope_b = tmp_path / "b"
    scope_b.mkdir()

    result_a = run_request(capability, request, credentials={}, scope_dir=scope_a)
    result_b = run_request(capability, request, credentials={}, scope_dir=scope_b)

    assert result_a.setup.status == "ok"
    assert result_b.setup.status == "ok"


def test_an_unserialisable_setup_output_does_not_turn_a_successful_setup_into_a_failure(
    tmp_path, monkeypatch
):
    """The setup-output cache is best-effort by contract. If writing it
    raises -- json.dumps rejects any output _to_jsonable cannot convert --
    the setup that already SUCCEEDED must still report `ok` with its real
    outputs. Reporting `failed` for a checkout that actually materialised
    is the same lie, pointed the other way."""
    capability = load_capability(FIXTURES / "echo")
    request = RequestEnvelope.model_validate(
        {"version": 1, "setup": {"task": "prep", "inputs": {"userName": "world"}}, "tasks": []}
    )

    def _explode(_payload, *args, **kwargs):
        raise TypeError("Object of type set is not JSON serializable")

    monkeypatch.setattr("runwhen_capability.host.json.dumps", _explode)

    result = run_request(capability, request, credentials={}, scope_dir=tmp_path)

    assert result.setup.status == "ok"
    assert result.setup.outputs == {"greeting": "hello world", "items": ["a", "b"]}


# -- task-level skipped status ----------------------------------------------


def _skips_request(*task_names: str, setup_inputs: dict | None = None) -> RequestEnvelope:
    return RequestEnvelope.model_validate(
        {
            "version": 1,
            "setup": {"task": "prep", "inputs": setup_inputs or {}},
            "tasks": [{"task": name, "inputs": {}} for name in task_names],
        }
    )


def test_a_task_raising_skiptask_is_recorded_skipped_with_its_reason(tmp_path):
    capability = load_capability(FIXTURES / "skips")

    result = run_request(
        capability, _skips_request("nothing_to_do"), credentials={}, scope_dir=tmp_path
    )

    task = result.tasks[0]
    assert task.status == "skipped"
    assert task.reason == "no matching files in the diff"
    assert task.error is None
    assert task.outputs == {}


def test_a_skipped_task_does_not_stop_the_tasks_after_it(tmp_path):
    capability = load_capability(FIXTURES / "skips")

    result = run_request(
        capability, _skips_request("nothing_to_do", "good"), credentials={}, scope_dir=tmp_path
    )

    assert [t.status for t in result.tasks] == ["skipped", "ok"]
    assert result.tasks[1].outputs == {"ok": "fine"}
    assert result.tasks[1].reason is None


def test_skiptask_without_a_reason_records_an_empty_reason(tmp_path):
    """Decided by status, never by the reason's truthiness: an empty reason
    is still a skip."""
    capability = load_capability(FIXTURES / "skips")

    result = run_request(
        capability, _skips_request("no_reason"), credentials={}, scope_dir=tmp_path
    )

    assert result.tasks[0].status == "skipped"
    assert result.tasks[0].reason == ""


def test_a_skipped_task_serialises_its_status_and_reason(tmp_path):
    capability = load_capability(FIXTURES / "skips")

    result = run_request(
        capability, _skips_request("nothing_to_do"), credentials={}, scope_dir=tmp_path
    )

    assert result.model_dump(mode="json")["tasks"][0] == {
        "task": "nothing_to_do",
        "status": "skipped",
        "outputs": {},
        "error": None,
        "reason": "no matching files in the diff",
        # errors/logTail are bundle-mode only (see bundle.py); a packaged
        # capability's task always carries their empty/None defaults.
        "errors": [],
        "logTail": None,
    }


def test_setup_cannot_be_skipped_and_fails_instead(tmp_path):
    """`skipped` is a task status only: a setup that raises SkipTask has not
    materialised anything the tasks could use, so it is an ordinary setup
    failure."""
    capability = load_capability(FIXTURES / "skips")

    result = run_request(
        capability,
        _skips_request("good", setup_inputs={"skip": True}),
        credentials={},
        scope_dir=tmp_path,
    )

    assert result.setup.status == "failed"
    assert "setup has nothing to do" in result.setup.error
