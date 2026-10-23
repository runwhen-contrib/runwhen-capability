"""run_bundle_request -- bundle execution: hash verification, invalid
manifests, the ok/skipped/failed/schema-error/timeout/redaction paths for
both Python and bash tasks, and setup."""

from __future__ import annotations

import hashlib
import json
import time

import pytest
import requests
from bundle_fixtures import load_bundle
from relay_server import Response

from runwhen_capability import artifacts as artifacts_module
from runwhen_capability.bundle import _EventStream, _parse_event, run_bundle_request
from runwhen_capability.custom import validate
from runwhen_capability.custom.hashing import content_hash
from runwhen_capability.models import (
    Bundle,
    BundleFile,
    BundleRequestEnvelope,
    BundleTarget,
    OutputDecl,
)
from runwhen_capability.relay import RelayClient

EXEC = load_bundle("exec")
PGBOUNCER = load_bundle("pgbouncer-health")
ARTIFACTS = load_bundle("artifacts")


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


# -- artifact outputs: rw_artifact, the artifacts dir, host-side upload, spill ----------

PUT_PREFIX = "/v1/tasks/req-1/artifacts/"
SECRET = "super-secret-token"


def _created(ref: str, size: int = 0, sha256: str = "0" * 64) -> Response:
    body = {"artifactRef": ref, "sizeBytes": size, "sha256": sha256, "lineCount": 1, "isText": True}
    return Response(201, json=body)


def _run_artifacts(
    tasks, tmp_path, relay_server=None, *, decls=(), credentials=None, inputs=None, deadline=30
):
    """Runs tasks of the artifacts fixture bundle. The scope is a subdirectory, so the
    relay's token file (in tmp_path) is never inside it. Without `relay_server` there is
    no relay, like `rwtask run`."""
    relay = None
    if relay_server is not None:
        token_file = tmp_path / "token"
        token_file.write_text("tok-1")
        relay = RelayClient(relay_server.url, token_file, "req-1", requests.Session())
    scope = tmp_path / "scope"
    request = BundleRequestEnvelope(
        bundle=_bundle(ARTIFACTS), tasks=tasks, inputs=inputs or {}, target=None
    )
    result = run_bundle_request(
        request,
        credentials=credentials or {},
        scope_dir=scope,
        deadline_seconds=deadline,
        relay=relay,
        outputs=list(decls),
    )
    return result, scope


def _seq_digest(last: int) -> tuple[int, str]:
    """(byte count, sha256) of `seq 1 <last>`'s output, computed in pieces."""
    digest = hashlib.sha256()
    total = 0
    for start in range(1, last + 1, 100_000):
        piece = "".join(f"{n}\n" for n in range(start, min(start + 100_000, last + 1))).encode()
        digest.update(piece)
        total += len(piece)
    return total, digest.hexdigest()


def test_bash_rw_artifact_pipe_uploads(relay_server, tmp_path):
    size, sha256 = _seq_digest(2_000_000)
    assert size == 14_888_896
    relay_server.script("PUT", PUT_PREFIX, _created("art_dump", size, sha256))

    result, _ = _run_artifacts(["dump-pipe"], tmp_path, relay_server)

    [task] = result.tasks
    assert task.status == "ok", task.error
    assert task.outputs == {"dump": {"$artifact": "art_dump"}}
    [put] = relay_server.requests
    assert (put.method, put.path) == ("PUT", PUT_PREFIX + "dump")
    assert put.body_length == 14_888_896
    assert put.body_sha256 == sha256
    assert put.body_complete and put.chunked
    assert put.body is None  # too big for the fixture to keep: it held no bytes
    assert put.header("content-type") == "text/plain"
    assert put.header("x-rw-task") == "dump-pipe"
    assert put.header("authorization") == "Bearer tok-1"


def test_files_in_artifacts_dir_uploaded(relay_server, tmp_path):
    relay_server.script(
        "PUT", PUT_PREFIX, _created("art_dump"), _created("art_report")
    )  # in request order: dump, then report

    result, _ = _run_artifacts(["dump-dir"], tmp_path, relay_server)

    [task] = result.tasks
    assert task.status == "ok", task.error
    assert task.outputs == {
        "dump": {"$artifact": "art_dump"},
        "report": {"$artifact": "art_report"},
    }
    dump, report = relay_server.requests  # `extra` is no declared output: never uploaded
    assert (dump.path, dump.body) == (PUT_PREFIX + "dump", b"first dump\n")
    assert dump.header("content-type") == "application/octet-stream"  # the default
    assert (report.path, report.body) == (PUT_PREFIX + "report", b'{"ok": true}\n')
    assert report.header("content-type") == "application/json"


def test_a_file_registered_by_path_is_uploaded_in_place(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_report"))

    result, scope = _run_artifacts(["dump-path"], tmp_path, relay_server)

    [task] = result.tasks
    assert task.status == "ok", task.error
    assert task.outputs == {"report": {"$artifact": "art_report"}}
    [put] = relay_server.requests
    assert (put.path, put.body) == (PUT_PREFIX + "report", b"registered in place\n")
    assert put.header("content-type") == "text/plain"
    assert (scope / "out" / "report.txt").is_file()
    assert not (scope / "artifacts" / "report").exists()  # registered, never copied


@pytest.mark.parametrize("task", ["where", "where-py"])
def test_artifacts_dir_is_exported_to_bash_and_python_children(tmp_path, task):
    result, scope = _run_artifacts([task], tmp_path)
    [outcome] = result.tasks
    assert outcome.status == "ok", outcome.error
    assert outcome.outputs == {"dir": str(scope / "artifacts")}


def test_each_task_gets_a_fresh_artifacts_dir(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_dump"), _created("art_report"))

    # dynamic-name declares `dump` too but writes none: what dump-dir left must not be its
    result, _ = _run_artifacts(["dump-dir", "dynamic-name"], tmp_path, relay_server)

    assert [t.status for t in result.tasks] == ["ok", "ok"]
    assert result.tasks[1].outputs == {"count": 1}
    assert len(relay_server.requests) == 2


@pytest.mark.parametrize("task", ["symlink-file", "symlink-component", "path-outside"])
def test_artifact_symlink_escape_rejected(relay_server, tmp_path, task):
    relay_server.script("PUT", PUT_PREFIX, _created("art_never"))

    result, _ = _run_artifacts([task], tmp_path, relay_server)

    [outcome] = result.tasks
    assert outcome.status == "failed"
    assert outcome.error.startswith("E_ARTIFACT_PATH"), outcome.error
    assert outcome.outputs == {}
    assert relay_server.requests == []


@pytest.mark.parametrize(
    ("task", "status"),
    [
        ("fail-after-dump", "failed"),
        ("skip-after-dump", "skipped"),
        ("bad-schema-after-dump", "failed"),
    ],
)
def test_a_task_that_did_not_succeed_uploads_nothing(relay_server, tmp_path, task, status):
    relay_server.script("PUT", PUT_PREFIX, _created("art_never"))

    result, _ = _run_artifacts([task], tmp_path, relay_server)

    [outcome] = result.tasks
    assert outcome.status == status
    assert "dump" not in outcome.outputs
    assert relay_server.requests == []


def test_a_timed_out_task_uploads_nothing(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_never"))

    result, _ = _run_artifacts(["slow-after-dump"], tmp_path, relay_server, deadline=1)

    [outcome] = result.tasks
    assert outcome.status == "timeout"
    assert relay_server.requests == []


@pytest.mark.parametrize("task", ["dynamic-name", "dynamic-schema-name"])
def test_a_runtime_undeclared_rw_artifact_name_is_dropped(relay_server, tmp_path, task):
    # A name only known at run time slips past the static check; the host drops it like any
    # other undeclared output -- a schema output's name included -- and the task still passes.
    relay_server.script("PUT", PUT_PREFIX, _created("art_never"))

    result, _ = _run_artifacts([task], tmp_path, relay_server)

    [outcome] = result.tasks
    assert outcome.status == "ok", outcome.error
    assert outcome.outputs == {"count": 1}
    assert relay_server.requests == []


def test_a_python_task_writes_its_artifact_through_ctx_artifacts(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_py"))

    result, scope = _run_artifacts(["py-dump"], tmp_path, relay_server)

    [task] = result.tasks
    assert task.status == "ok", task.error
    # the relay's ref, not the local_... one the child's sink answered with
    assert task.outputs == {"dump": {"$artifact": "art_py"}}
    [put] = relay_server.requests
    assert (put.path, put.body) == (PUT_PREFIX + "dump", b"written by a python task\n")
    assert put.header("content-type") == "text/plain"
    assert put.header("x-rw-task") == "py-dump"


def test_a_ref_a_task_returns_for_nothing_it_uploaded_is_dropped(relay_server, tmp_path):
    result, _ = _run_artifacts(["py-forged-ref"], tmp_path, relay_server)

    [task] = result.tasks
    assert task.status == "ok", task.error
    assert task.outputs == {}
    assert relay_server.requests == []


def test_without_a_relay_artifacts_land_in_the_local_sink(tmp_path):
    result, scope = _run_artifacts(["dump-dir", "py-dump"], tmp_path)

    dump_dir, py_dump = result.tasks
    assert dump_dir.status == py_dump.status == "ok"
    sha = hashlib.sha256(b"first dump\n").hexdigest()
    assert dump_dir.outputs["dump"] == {"$artifact": "local_" + sha[:32]}
    assert (scope / ".rw-artifacts" / "dump").read_bytes() == b"written by a python task\n"
    assert py_dump.outputs["dump"]["$artifact"].startswith("local_")


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (
            Response(413, json={"code": "E_ARTIFACT_TOO_LARGE", "maxBytes": 5}),
            "E_ARTIFACT_TOO_LARGE",
        ),
        (Response(422, json={"code": "E_ARTIFACT_QUOTA", "message": "full"}), "E_ARTIFACT_QUOTA"),
        (Response(404, json={"code": "E_ARTIFACT_UNDECLARED"}), "E_ARTIFACT_UNDECLARED"),
        (Response(409, json={"code": "E_ARTIFACT_EXISTS"}), "E_ARTIFACT_EXISTS"),
        (
            Response(422, json={"code": "E_ARTIFACTS_DISABLED", "message": "off"}),
            "E_ARTIFACTS_DISABLED",
        ),
    ],
)
def test_an_upload_error_fails_the_task_with_its_code(relay_server, tmp_path, response, code):
    relay_server.script("PUT", PUT_PREFIX, response)

    result, _ = _run_artifacts(["dump-dir"], tmp_path, relay_server)

    [task] = result.tasks
    assert task.status == "failed"
    assert task.errors[0].startswith(f"{code}: "), task.errors
    assert task.error.startswith(f"{code}: ")
    assert "dump" in task.errors[0]
    assert task.outputs == {}
    assert len(relay_server.requests) == 1  # the second artifact is not even tried


def test_an_upload_error_without_an_artifact_code_is_reported_as_an_upload_failure(
    relay_server, tmp_path
):
    relay_server.script("PUT", PUT_PREFIX, Response(401, json={"message": "bad token"}))

    result, _ = _run_artifacts(["dump-path"], tmp_path, relay_server)

    [task] = result.tasks
    assert task.status == "failed"
    assert task.error.startswith("E_ARTIFACT_UPLOAD: artifact 'report': HTTP_401"), task.error


def test_a_busy_runner_is_retried_for_a_host_side_upload(relay_server, tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts_module, "_sleep", lambda seconds: None)
    busy = Response(503, json={"code": "E_ARTIFACT_BUSY"})
    relay_server.script("PUT", PUT_PREFIX, busy, busy, _created("art_report"))

    result, _ = _run_artifacts(["dump-path"], tmp_path, relay_server)

    [task] = result.tasks
    assert task.status == "ok", task.error
    assert task.outputs == {"report": {"$artifact": "art_report"}}
    assert len(relay_server.requests) == 3


def test_a_runner_that_stays_busy_fails_the_task(relay_server, tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts_module, "_sleep", lambda seconds: None)
    relay_server.default = Response(503, json={"code": "E_ARTIFACT_BUSY"})

    result, _ = _run_artifacts(["dump-path"], tmp_path, relay_server)

    [task] = result.tasks
    assert task.status == "failed"
    assert task.error.startswith("E_ARTIFACT_BUSY: ")
    assert len(relay_server.requests) == 4  # the first try and three retries


@pytest.mark.parametrize(
    "event",
    [
        b'{"op":"artifact","name":"d","path":"/p","extra":1}',
        b'{"op":"artifact","name":"d"}',
        b'{"op":"artifact","path":"/p"}',
        b'{"op":"artifact","name":1,"path":"/p"}',
        b'{"op":"artifact","name":"d","path":["/p"]}',
        b'{"op":"artifact","name":"d","path":"/p","value":1}',
    ],
)
def test_an_artifact_event_must_be_exactly_op_name_and_path(event):
    stream = _EventStream()
    _parse_event(event, stream)
    assert (stream.events, stream.malformed) == ([], 1)


def test_a_well_formed_artifact_event_is_kept():
    stream = _EventStream()
    _parse_event(b'{"op":"artifact","name":"d","path":"/p"}', stream)
    assert stream.events == [{"op": "artifact", "name": "d", "path": "/p"}]
    assert stream.malformed == 0


# -- spill ---------------------------------------------------------------------------------


def _spill(task: str, name: str) -> OutputDecl:
    return OutputDecl(task=task, name=name, spill=True, maxBytes=64 << 20)


def _compact(value) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def test_large_schema_output_spills_instead_of_failing(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_spill"))

    result, _ = _run_artifacts(
        ["spill-scalar"], tmp_path, relay_server, decls=[_spill("spill-scalar", "blob")]
    )

    [task] = result.tasks
    assert task.status == "ok", task.error
    assert task.outputs == {"blob": {"$artifact": "art_spill"}}
    assert task.errors == []
    [put] = relay_server.requests
    assert put.path == PUT_PREFIX + "blob"
    assert put.header("content-type") == "application/json"
    assert put.header("x-rw-task") == "spill-scalar"
    assert put.body == _compact("x" * (300 * 1024))


def test_a_spilled_list_is_not_truncated(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_items"))

    result, _ = _run_artifacts(
        ["spill-list"], tmp_path, relay_server, decls=[_spill("spill-list", "items")]
    )

    [task] = result.tasks
    assert task.status == "ok", task.error
    assert task.outputs == {"items": {"$artifact": "art_items"}}
    assert task.errors == []  # no "truncated" note
    assert relay_server.requests[0].body == _compact(list(range(100000)))


def test_without_a_spill_declaration_the_caps_are_unchanged(relay_server, tmp_path):
    # declarations for another task's output, or another output of the same task
    decls = [
        _spill("other-task", "blob"),
        _spill("spill-scalar", "other"),
        _spill("other-task", "items"),
        _spill("spill-list", "other"),
    ]
    result, _ = _run_artifacts(["spill-scalar", "spill-list"], tmp_path, relay_server, decls=decls)

    scalar, items = result.tasks
    assert scalar.status == "failed"
    assert any("E_OUTPUT_TOO_LARGE" in e for e in scalar.errors)
    assert items.status == "ok"  # a list is cut to fit, as it always was
    assert len(items.outputs["items"]) < 100000
    assert any("truncated" in e for e in items.errors)
    assert relay_server.requests == []


def test_a_small_output_with_a_spill_declaration_stays_inline(relay_server, tmp_path):
    result, _ = _run_artifacts(
        ["spill-small"], tmp_path, relay_server, decls=[_spill("spill-small", "blob")]
    )

    [task] = result.tasks
    assert task.status == "ok", task.error
    assert task.outputs == {"blob": "small"}
    assert relay_server.requests == []


def test_spilled_output_is_redacted_before_it_is_uploaded(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_spill"))

    result, _ = _run_artifacts(
        ["spill-secret"],
        tmp_path,
        relay_server,
        decls=[_spill("spill-secret", "blob")],
        credentials={"token": SECRET},
    )

    [task] = result.tasks
    assert task.status == "ok", task.error
    assert task.outputs == {"blob": {"$artifact": "art_spill"}}
    [put] = relay_server.requests
    assert SECRET.encode() not in put.body
    assert b"token is ***REDACTED***" in put.body


def test_the_result_cap_still_applies_to_what_stays_inline(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_spill"))

    result, _ = _run_artifacts(
        ["spill-and-inline"],
        tmp_path,
        relay_server,
        decls=[_spill("spill-and-inline", "blob")],
        inputs={"inline_count": 3},
    )

    [task] = result.tasks
    assert task.status == "ok", task.error
    assert task.outputs["blob"] == {"$artifact": "art_spill"}
    assert sorted(k for k in task.outputs if k != "blob") == ["a", "b", "c"]
    assert len(relay_server.requests) == 1


def test_a_result_over_the_cap_uploads_no_spill(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_never"))

    result, _ = _run_artifacts(
        ["spill-and-inline"],
        tmp_path,
        relay_server,
        decls=[_spill("spill-and-inline", "blob")],
        inputs={"inline_count": 5},
    )

    [task] = result.tasks
    assert task.status == "failed"
    assert any("E_OUTPUT_TOO_LARGE" in e and "per-result limit" in e for e in task.errors)
    assert task.outputs == {}
    assert relay_server.requests == []


def test_a_failed_task_does_not_spill(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_never"))

    result, _ = _run_artifacts(
        ["spill-then-fail"], tmp_path, relay_server, decls=[_spill("spill-then-fail", "blob")]
    )

    [task] = result.tasks
    assert task.status == "failed"
    assert "process exited 1" in task.error
    assert relay_server.requests == []


def test_a_spill_upload_error_fails_the_task_with_its_code(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, Response(413, json={"code": "E_ARTIFACT_TOO_LARGE"}))

    result, _ = _run_artifacts(
        ["spill-scalar"], tmp_path, relay_server, decls=[_spill("spill-scalar", "blob")]
    )

    [task] = result.tasks
    assert task.status == "failed"
    assert task.error.startswith("E_ARTIFACT_TOO_LARGE: ")
    assert "blob" in task.error
    assert task.outputs == {}
