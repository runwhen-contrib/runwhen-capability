"""run_bundle_request -- bundle execution: hash verification, invalid
manifests, the ok/skipped/failed/schema-error/timeout/redaction paths for
both Python and bash tasks, and setup."""

from __future__ import annotations

from bundle_fixtures import load_bundle

from runwhen_capability.bundle import run_bundle_request
from runwhen_capability.custom.hashing import content_hash
from runwhen_capability.models import Bundle, BundleFile, BundleRequestEnvelope, BundleTarget

EXEC = load_bundle("exec")
PGBOUNCER = load_bundle("pgbouncer-health")


def _bundle(files: dict[str, str]) -> Bundle:
    return Bundle(
        hash=content_hash(files), files=[BundleFile(path=p, content=c) for p, c in files.items()]
    )


def _run(
    files, tasks, tmp_path, *, inputs=None, target=None, credentials=None, deadline_seconds=30
):
    request = BundleRequestEnvelope(
        bundle=_bundle(files),
        tasks=tasks,
        inputs=inputs or {},
        target=BundleTarget(**target) if target else None,
    )
    return run_bundle_request(
        request,
        credentials=credentials or {},
        scope_dir=tmp_path,
        deadline_seconds=deadline_seconds,
    )


def test_a_corrupted_bundle_fails_the_whole_request_before_running_anything(tmp_path):
    request = BundleRequestEnvelope(
        bundle=Bundle(
            hash="sha256:" + "0" * 64,
            files=[BundleFile(path=p, content=c) for p, c in EXEC.items()],
        ),
        tasks=["ok-py"],
    )
    result = run_bundle_request(request, credentials={}, scope_dir=tmp_path)
    assert result.setup.status == "failed"
    assert "hash mismatch" in result.setup.error
    assert result.tasks == []


def test_an_invalid_bundle_fails_closed_with_no_execution(tmp_path):
    files = {"capability.yaml": "apiVersion: runwhen.com/custom-capability/v1\n"}  # no name
    result = _run(files, ["anything"], tmp_path)
    assert result.setup.status == "failed"
    assert "invalid bundle" in result.setup.error
    assert result.tasks == []


def test_ok_python_task(tmp_path):
    result = _run(EXEC, ["ok-py"], tmp_path)
    [task] = result.tasks
    assert task.status == "ok"
    assert task.outputs == {"greeting": "hello from python"}
    assert task.errors == []


def test_ok_bash_task(tmp_path):
    result = _run(EXEC, ["ok-bash"], tmp_path)
    [task] = result.tasks
    assert task.status == "ok"
    assert task.outputs == {"greeting": "hello from bash"}


def test_skipped_python_task(tmp_path):
    result = _run(EXEC, ["skip-py"], tmp_path)
    [task] = result.tasks
    assert task.status == "skipped"
    assert task.reason == "nothing to check"
    assert task.outputs == {}


def test_skipped_bash_task(tmp_path):
    result = _run(EXEC, ["skip-bash"], tmp_path)
    [task] = result.tasks
    assert task.status == "skipped"
    assert task.reason == "nothing to check"


def test_failed_python_task_carries_the_traceback_in_log_tail(tmp_path):
    result = _run(EXEC, ["fail-py"], tmp_path)
    [task] = result.tasks
    assert task.status == "failed"
    assert "process exited 1" in task.error
    assert "RuntimeError: boom" in task.logTail


def test_failed_bash_task_carries_its_exit_code_and_stderr(tmp_path):
    result = _run(EXEC, ["fail-bash"], tmp_path)
    [task] = result.tasks
    assert task.status == "failed"
    assert "process exited 3" in task.error
    assert "boom" in task.logTail


def test_output_schema_violation_fails_the_task_with_the_json_path(tmp_path):
    result = _run(EXEC, ["schema-bad"], tmp_path)
    [task] = result.tasks
    assert task.status == "failed"
    assert any("E_OUTPUT_SCHEMA" in e and "outputs.count" in e for e in task.errors)
    # The offending output still travels with the result -- a caller
    # debugging why validation failed needs to see what was actually produced.
    assert task.outputs == {"count": "not-an-integer"}


def test_an_oversized_list_output_is_truncated_but_the_task_still_ok(tmp_path):
    result = _run(EXEC, ["big-list"], tmp_path)
    [task] = result.tasks
    assert task.status == "ok"
    assert len(task.outputs["items"]) < 100000
    assert any("truncated" in e for e in task.errors)


def test_an_oversized_non_list_output_fails_the_task(tmp_path):
    result = _run(EXEC, ["big-scalar"], tmp_path)
    [task] = result.tasks
    assert task.status == "failed"
    assert any("E_OUTPUT_TOO_LARGE" in e for e in task.errors)
    assert "blob" not in task.outputs


def test_timeout_kills_the_whole_process_group(tmp_path):
    result = _run(EXEC, ["slow"], tmp_path, deadline_seconds=0.5)
    [task] = result.tasks
    assert task.status == "timeout"
    assert "E_TIMEOUT" in task.error


def test_redacts_a_secret_from_task_outputs(tmp_path):
    result = _run(EXEC, ["secret-echo"], tmp_path, credentials={"token": "super-secret-token"})
    [task] = result.tasks
    assert task.status == "ok"
    assert "super-secret-token" not in task.outputs["leaked"]
    assert "REDACTED" in task.outputs["leaked"]


def test_redacts_a_secret_from_the_log_tail(tmp_path):
    result = _run(EXEC, ["secret-log"], tmp_path, credentials={"token": "super-secret-token"})
    [task] = result.tasks
    assert "super-secret-token" not in task.logTail
    assert "REDACTED" in task.logTail


def test_redacts_a_credential_and_auto_sets_kubeconfig(tmp_path):
    result = _run(
        EXEC, ["kubeconfig-echo"], tmp_path, credentials={"kubeconfig": "kubeconfig-secret-value"}
    )
    [task] = result.tasks
    assert task.status == "ok"
    assert "kubeconfig-secret-value" not in task.outputs["content"]
    assert "REDACTED" in task.outputs["content"]


def test_resource_input_is_filled_from_the_request_target(tmp_path):
    result = _run(
        EXEC, ["target-echo"], tmp_path, target={"urn": "urn:resource:x", "kind": "deployment"}
    )
    [task] = result.tasks
    assert task.status == "ok"
    assert task.outputs == {"urn": "urn:resource:x"}


def test_missing_required_secret_fails_before_running(tmp_path):
    result = _run(EXEC, ["secret-echo"], tmp_path, credentials={})
    [task] = result.tasks
    assert task.status == "failed"
    assert any("E_INPUT_TYPE" in e for e in task.errors)


def test_an_unknown_requested_task_fails_only_that_entry(tmp_path):
    result = _run(EXEC, ["ok-py", "no-such-task"], tmp_path)
    assert result.tasks[0].status == "ok"
    assert result.tasks[1].status == "failed"
    assert "no-such-task" in result.tasks[1].error


def test_runtime_input_reaches_a_bash_task_as_an_env_var(tmp_path):
    result = _run(
        PGBOUNCER,
        ["pool-errors"],
        tmp_path,
        inputs={"since": "45m", "maxWait": 5},
        target={"urn": "urn:resource:pgbouncer"},
        credentials={"kubeconfig": "kubeconfig-value"},
    )
    [task] = result.tasks
    assert task.status == "ok"
    assert task.outputs["summary"] == {"windowMinutes": 30, "pods": 2, "total": 3}
    assert task.outputs["errors"] == [{"pattern": "pool exhausted", "count": 3}]
    assert task.outputs["detail"] == {"ok": True}


def test_a_setup_runs_once_before_the_requested_tasks(tmp_path):
    result = _run(
        PGBOUNCER,
        ["pool-errors"],
        tmp_path,
        target={"urn": "urn:resource:pgbouncer"},
        credentials={"kubeconfig": "kubeconfig-value"},
    )
    assert result.setup.status == "ok"
    assert result.tasks[0].status == "ok"
