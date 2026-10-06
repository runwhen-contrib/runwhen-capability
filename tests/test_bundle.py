"""run_bundle_request -- bundle execution: hash verification, invalid
manifests, the ok/skipped/failed/schema-error/timeout/redaction paths for
both Python and bash tasks, and setup."""

from __future__ import annotations

import time

from bundle_fixtures import load_bundle

from runwhen_capability.bundle import run_bundle_request
from runwhen_capability.custom import validate
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


def test_a_published_bundle_without_read_only_or_effects_still_runs(tmp_path):
    # E_EFFECTS_REQUIRED gates authoring; the runtime must not refuse an
    # immutable bundle published before it existed.
    files = {
        "capability.yaml": (
            "apiVersion: runwhen.com/custom-capability/v1\n"
            "name: legacy\n"
            "appliesTo:\n"
            "  - { platform: kubernetes, type: statefulset }\n"
            "tasks:\n"
            "  - name: hello\n"
            "    file: tasks/hello.py\n"
        ),
        "tasks/hello.py": "def main(ctx):\n    return {}\n",
    }
    assert "E_EFFECTS_REQUIRED" in [d.code for d in validate(files)]
    result = _run(files, ["hello"], tmp_path)
    assert result.setup is None or result.setup.status != "failed"
    [task] = result.tasks
    assert task.status == "ok"


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


def test_the_deadline_is_one_budget_for_the_whole_request(tmp_path):
    """Two slow tasks under a 1s request deadline: the first uses it up and
    is killed, the second never starts -- the request as a whole never runs
    past the deadline the runner gave it."""
    started = time.monotonic()
    result = _run(EXEC, ["slow", "slow", "ok-py"], tmp_path, deadline_seconds=1)
    assert time.monotonic() - started < 3
    assert [task.status for task in result.tasks] == ["timeout", "timeout", "timeout"]
    assert "before this started" in result.tasks[1].error


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


def test_a_runtime_input_of_the_wrong_type_fails_with_e_input_type(tmp_path):
    result = _run(
        PGBOUNCER,
        ["pool-errors"],
        tmp_path,
        inputs={"since": "45m", "maxWait": "not-an-integer"},
        target={"urn": "urn:resource:pgbouncer"},
        credentials={"kubeconfig": "kubeconfig-value"},
    )
    [task] = result.tasks
    assert task.status == "failed"
    assert any("E_INPUT_TYPE" in e and "maxWait" in e for e in task.errors)


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


_UPPER_INPUT_BUNDLE = {
    "capability.yaml": (
        "apiVersion: runwhen.com/custom-capability/v1\n"
        "name: legacy-upper\n"
        "appliesTo:\n"
        "  - { platform: kubernetes, type: statefulset }\n"
        "tasks:\n"
        "  - name: show\n"
        "    file: tasks/show.sh\n"
        "    readOnly: true\n"
        "    inputs:\n"
        "      THRESHOLD: { type: number, default: 1, runtime: true }\n"
        "      ENV: { type: string, default: prod, runtime: true }\n"
        "    outputs:\n"
        '      seen: { schema: "string" }\n'
    ),
    # Written against the pre-0.3.0 mapping: THRESHOLD -> T_H_R_E_S_H_O_L_D, ENV -> E_N_V.
    "tasks/show.sh": (
        'source "$RW_SDK/rw.sh"\nrw_set seen "\\"t=$T_H_R_E_S_H_O_L_D e=$E_N_V\\""\n'
    ),
}


def test_a_published_bundle_reading_a_legacy_mangled_env_name_still_runs(tmp_path):
    # 0.3.0 changed input_env_name; bundles published before it read the per-letter
    # names (the old validator demanded them) and must keep running unchanged.
    result = _run(_UPPER_INPUT_BUNDLE, ["show"], tmp_path, inputs={"THRESHOLD": 7, "ENV": "stg"})
    assert result.setup is None or result.setup.status != "failed", result.setup
    [task] = result.tasks
    assert task.status == "ok", task
    assert task.outputs["seen"] == "t=7 e=stg"
