"""run_bundle_request -- bundle execution: hash verification, invalid
manifests, the ok/skipped/failed/schema-error/timeout/redaction paths for
both Python and bash tasks, and setup."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

import pytest
import requests
from bundle_fixtures import load_bundle
from relay_server import Response

from runwhen_capability import artifacts as artifacts_module
from runwhen_capability import bundle as bundle_module
from runwhen_capability import relay as relay_module
from runwhen_capability.bundle import (
    MAX_OUTPUT_BYTES,
    _apply_size_caps,
    _ArtifactPathError,
    _collect_artifacts,
    _dir_identity,
    _EventStream,
    _open_artifact,
    _parse_event,
    _stamp_artifacts_dir,
    _truncate_list_to_budget,
    _upload_artifacts,
    run_bundle_request,
)
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
        (
            Response(422, json={"code": "E_ARTIFACTS_UNSUPPORTED", "message": "none here"}),
            "E_ARTIFACTS_UNSUPPORTED",
        ),
    ],
)
def test_an_upload_error_fails_the_task_with_its_code(relay_server, tmp_path, response, code):
    relay_server.script("PUT", PUT_PREFIX, response)

    result, _ = _run_artifacts(["dump-dir"], tmp_path, relay_server)

    [task] = result.tasks
    assert task.status == "failed"
    assert task.errors[0].startswith(f"{code}: artifact 'dump': "), task.errors
    assert task.errors[0].count(code) == 1  # "E_X: artifact 'n': <message>", never "E_X: ... E_X:"
    assert task.error.startswith(f"{code}: ")
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


@pytest.mark.parametrize("code", ["E_ARTIFACTS_DISABLED", "E_ARTIFACTS_UNSUPPORTED"])
def test_a_spill_falls_back_to_the_caps_when_artifacts_are_unavailable(
    relay_server, tmp_path, caplog, code
):
    """The platform declares every schema output of a custom bundle as spill,
    even while artifacts are switched off (the default). A spill the relay
    cannot take is capped as it was before spill existed -- a list is cut to
    fit, with a note; anything else fails E_OUTPUT_TOO_LARGE -- instead of
    failing the task with the artifact error. One log line per task says why."""
    relay_server.default = Response(422, json={"code": code, "message": "artifacts are off"})
    decls = [_spill("spill-scalar", "blob"), _spill("spill-list", "items")]

    with caplog.at_level(logging.WARNING):
        result, _ = _run_artifacts(
            ["spill-scalar", "spill-list"], tmp_path, relay_server, decls=decls
        )

    scalar, items = result.tasks
    assert scalar.status == "failed"
    assert scalar.error.startswith("E_OUTPUT_TOO_LARGE outputs.blob"), scalar.error
    assert not any(code in e for e in scalar.errors)
    assert items.status == "ok", items.error
    assert 0 < len(items.outputs["items"]) < 100000
    assert any("truncated" in e for e in items.errors)
    assert len(relay_server.requests) == 2  # one try per task
    lines = [r.getMessage() for r in caplog.records if "spill" in r.getMessage()]
    assert len(lines) == 2 and all(code in line for line in lines), lines


def test_a_busy_relay_is_retried_for_a_spill(relay_server, tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts_module, "_sleep", lambda seconds: None)
    busy = Response(503, json={"code": "E_ARTIFACT_BUSY"}, read_limit=0)
    relay_server.script("PUT", PUT_PREFIX, busy, _created("art_spill"))

    result, _ = _run_artifacts(
        ["spill-scalar"], tmp_path, relay_server, decls=[_spill("spill-scalar", "blob")]
    )

    [task] = result.tasks
    assert task.status == "ok", task.error
    assert task.outputs == {"blob": {"$artifact": "art_spill"}}
    first, second = relay_server.requests
    assert first.body_length == 0
    assert second.body == _compact("x" * (300 * 1024)) and second.body_complete


def test_outputs_are_measured_as_compact_json_like_the_packaged_spill():
    """Both hosts measure an output the way the result carries it: compact
    JSON. A list whose compact form fits the cap stays whole even though its
    spaced form (", " between items) would not."""
    fits = [1] * 131070 + [10]
    assert len(_compact(fits)) == MAX_OUTPUT_BYTES
    assert len(json.dumps(fits).encode()) > MAX_OUTPUT_BYTES

    inline, errors, spilled = _apply_size_caps({"items": fits}, frozenset({"items"}))
    assert (inline, errors, spilled) == ({"items": fits}, [], {})

    over = [*fits, 7]
    inline, errors, spilled = _apply_size_caps({"items": over}, frozenset({"items"}))
    assert (inline, errors, spilled) == ({}, [], {"items": _compact(over)})


def test_a_truncated_list_is_the_longest_prefix_whose_compact_json_fits():
    value = list(range(100000))

    kept = _truncate_list_to_budget(value, MAX_OUTPUT_BYTES)

    assert kept == value[: len(kept)]
    assert len(_compact(kept)) <= MAX_OUTPUT_BYTES
    assert len(_compact(value[: len(kept) + 1])) > MAX_OUTPUT_BYTES


# -- the host opens each artifact file once, safely ---------------------------------------


def _within(seconds: float, fn, *args, **kwargs):
    """fn(*args, **kwargs) on a thread; fails, instead of hanging the suite, if it has
    not returned in `seconds`."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 -- re-raised below, on the test's thread
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"did not finish within {seconds}s: it is blocked"
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _scope(tmp_path: Path) -> tuple[Path, Path]:
    scope = tmp_path / "scope"
    (scope / "sub").mkdir(parents=True)
    (scope / "sub" / "f.txt").write_bytes(b"inside")
    return scope, scope / ".secrets-test"


def test_open_artifact_returns_the_regular_file_by_absolute_or_scope_relative_path(tmp_path):
    scope, secrets = _scope(tmp_path)
    for raw in (str(scope / "sub" / "f.txt"), "sub/f.txt", "./sub/../sub/f.txt"):
        with _open_artifact(scope, secrets, raw) as handle:
            assert handle.read() == b"inside"


def test_open_artifact_accepts_the_scope_by_its_resolved_name(tmp_path):
    scope, secrets = _scope(tmp_path)
    link = tmp_path / "link-to-scope"
    link.symlink_to(scope, target_is_directory=True)
    # The host was given the scope through a symlink; the child's $PWD names it resolved.
    with _open_artifact(link, secrets, str(scope.resolve() / "sub" / "f.txt")) as handle:
        assert handle.read() == b"inside"


def _make_symlink_leaf(scope: Path) -> str:
    (scope / "leaf").symlink_to(scope / "sub" / "f.txt")
    return "leaf"


def _make_symlink_dir(scope: Path) -> str:
    (scope / "linked").symlink_to(scope / "sub", target_is_directory=True)
    return "linked/f.txt"


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (_make_symlink_leaf, "symbolic link"),
        (_make_symlink_dir, "symbolic link"),
        (lambda scope: "sub", "not a regular file"),
        (lambda scope: "missing.txt", "cannot be opened"),
        (lambda scope: "sub/missing/f.txt", "cannot be opened"),
        (lambda scope: "../outside.txt", "outside the task's scope"),
        (lambda scope: "/etc/passwd", "outside the task's scope"),
        (lambda scope: ".secrets-test/token", "credentials directory"),
        (lambda scope: "", "empty"),
        (lambda scope: "sub/f\x00.txt", "NUL"),
    ],
)
def test_open_artifact_refuses_what_is_not_a_plain_file_inside_the_scope(tmp_path, make, message):
    scope, secrets = _scope(tmp_path)
    (tmp_path / "outside.txt").write_text("outside")
    (secrets).mkdir()
    (secrets / "token").write_text("secret")
    raw = make(scope)

    with pytest.raises(_ArtifactPathError, match=message):
        _open_artifact(scope, secrets, raw)


def test_open_artifact_refuses_a_character_device_without_reading_it():
    # the same would be true of /dev/zero reached through a path inside a scope: it opens,
    # and fstat on the descriptor says it is no regular file
    with pytest.raises(_ArtifactPathError, match="not a regular file"):
        _open_artifact(Path("/dev"), Path("/dev/.secrets-test"), "zero")


def test_open_artifact_refuses_a_fifo_without_blocking(tmp_path):
    scope, secrets = _scope(tmp_path)
    os.mkfifo(scope / "pipe")

    with pytest.raises(_ArtifactPathError, match="not a regular file"):
        _within(10, _open_artifact, scope, secrets, "pipe")


def test_open_artifact_reads_the_file_it_opened_when_the_path_is_swapped_afterwards(tmp_path):
    scope, secrets = _scope(tmp_path)
    (tmp_path / "other.txt").write_bytes(b"something else")
    target = scope / "sub" / "f.txt"

    handle = _open_artifact(scope, secrets, str(target))
    target.unlink()
    target.symlink_to(tmp_path / "other.txt")  # swapped after the open

    with handle:
        assert handle.read() == b"inside"


def test_open_artifact_checks_the_scope_is_the_directory_that_was_prepared(tmp_path):
    scope, secrets = _scope(tmp_path)
    prepared = _dir_identity(scope)

    with _open_artifact(scope, secrets, "sub/f.txt", prepared) as handle:
        assert handle.read() == b"inside"
    with pytest.raises(_ArtifactPathError, match="not the one that was prepared"):
        _open_artifact(scope, secrets, "sub/f.txt", (prepared[0], prepared[1] + 1))


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_a_scope_renamed_away_and_replaced_is_refused_not_walked(tmp_path, replacement):
    scope, secrets = _scope(tmp_path)
    prepared = _dir_identity(scope)
    other = tmp_path / "other"
    (other / "sub").mkdir(parents=True)
    (other / "sub" / "f.txt").write_bytes(b"someone else's")

    scope.rename(tmp_path / "moved")
    if replacement == "symlink":
        scope.symlink_to(other, target_is_directory=True)
    else:
        (scope / "sub").mkdir(parents=True)
        (scope / "sub" / "f.txt").write_bytes(b"planted")

    with pytest.raises(_ArtifactPathError, match="not the one that was prepared"):
        _open_artifact(scope, secrets, str(scope / "sub" / "f.txt"), prepared)


def test_a_scope_symlink_is_not_followed_by_the_walk_itself(tmp_path):
    # even with no recorded identity the starting point is opened with O_NOFOLLOW, on
    # the resolved name -- which cannot be a link unless it was swapped just now
    scope, secrets = _scope(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(scope, target_is_directory=True)

    with _open_artifact(link, secrets, str(scope / "sub" / "f.txt")) as handle:
        assert handle.read() == b"inside"


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_a_scope_replaced_after_the_task_uploads_nothing(
    relay_server, tmp_path, monkeypatch, replacement
):
    """A process that outlived the task renames the scope away and puts something else in
    its place before the host collects the artifacts. Nothing of it is uploaded."""
    relay_server.script("PUT", PUT_PREFIX, _created("art_never"))
    other = tmp_path / "other"
    (other / "artifacts").mkdir(parents=True)
    (other / "artifacts" / "dump").write_bytes(b"not the task's")
    real = bundle_module._collect_artifacts

    def replace_scope_then_collect(scope_dir, *rest):
        scope_dir.rename(tmp_path / "moved")
        if replacement == "symlink":
            scope_dir.symlink_to(other, target_is_directory=True)
        else:
            (scope_dir / "artifacts").mkdir(parents=True)
            (scope_dir / "artifacts" / "dump").write_bytes(b"planted")
        return real(scope_dir, *rest)

    monkeypatch.setattr(bundle_module, "_collect_artifacts", replace_scope_then_collect)

    result, _ = _run_artifacts(["dump-dir"], tmp_path, relay_server)

    [task] = result.tasks
    assert task.status == "failed"
    assert task.error.startswith("E_ARTIFACT_PATH"), task.error
    assert "not the one that was prepared" in task.error
    assert relay_server.requests == []


def test_the_artifact_uploaded_is_the_file_that_was_opened_not_what_the_path_names_later(
    relay_server, tmp_path, monkeypatch
):
    """The seam between opening and reading, closed: a process that outlived the task
    replaces the file with a symlink at the worst moment -- here, right after the host
    opened it. What is uploaded is what was opened."""
    relay_server.script("PUT", PUT_PREFIX, _created("art_report"))
    real = bundle_module._open_artifact

    def open_then_swap(scope_dir, secrets_dir, raw, *rest):
        handle = real(scope_dir, secrets_dir, raw, *rest)
        os.unlink(raw)
        os.symlink("/etc/passwd", raw)
        return handle

    monkeypatch.setattr(bundle_module, "_open_artifact", open_then_swap)

    result, scope = _run_artifacts(["dump-path"], tmp_path, relay_server)

    [task] = result.tasks
    assert task.status == "ok", task.error
    assert (scope / "out" / "report.txt").is_symlink()  # the swap did happen
    [put] = relay_server.requests
    assert put.body == b"registered in place\n"


@pytest.mark.parametrize("task", ["fifo-in-dir", "fifo-registered"])
def test_a_fifo_registered_as_an_artifact_fails_fast_and_does_not_wedge_the_host(
    relay_server, tmp_path, task
):
    relay_server.script("PUT", PUT_PREFIX, _created("art_never"))

    result, _ = _within(20, _run_artifacts, [task], tmp_path, relay_server)

    [outcome] = result.tasks
    assert outcome.status == "failed"
    assert outcome.error.startswith("E_ARTIFACT_PATH"), outcome.error
    assert "not a regular file" in outcome.error
    assert relay_server.requests == []


def test_a_collected_file_is_closed_when_another_one_fails(tmp_path, monkeypatch):
    scope, secrets = _scope(tmp_path)
    opened: list = []
    real = bundle_module._open_artifact

    def remember(*args):
        handle = real(*args)
        opened.append(handle)
        return handle

    monkeypatch.setattr(bundle_module, "_open_artifact", remember)
    specs = {"a": {"contentType": "x"}, "b": {"contentType": "x"}}

    files, errors = _collect_artifacts(
        scope, secrets, specs, {"a": str(scope / "sub" / "f.txt"), "b": "missing.txt"}
    )

    assert files == {}
    assert len(errors) == 1 and errors[0].startswith("E_ARTIFACT_PATH: artifact 'b'")
    assert opened and all(handle.closed for handle in opened)


def test_a_path_the_task_chose_is_cut_in_the_error_that_quotes_it(tmp_path):
    scope, secrets = _scope(tmp_path)

    _, errors = _collect_artifacts(
        scope, secrets, {"dump": {"contentType": "x"}}, {"dump": "/" + "a" * 5000}
    )

    [error] = errors
    assert error.startswith("E_ARTIFACT_PATH: artifact 'dump': /aaa")
    assert len(error) == bundle_module.MAX_SCHEMA_ERROR_CHARS


def test_a_long_relay_message_is_cut_in_the_upload_error(relay_server, tmp_path):
    body = {"code": "E_ARTIFACT_QUOTA", "message": "x" * 5000}
    relay_server.script("PUT", PUT_PREFIX, Response(422, json=body))

    result, _ = _run_artifacts(["dump-path"], tmp_path, relay_server)

    [task] = result.tasks
    assert task.status == "failed"
    [error] = task.errors
    assert error.startswith("E_ARTIFACT_QUOTA: artifact 'report': ")
    assert len(error) == bundle_module.MAX_SCHEMA_ERROR_CHARS


# -- a directory of the user's own named artifacts/ ---------------------------------------


def test_a_users_own_artifacts_directory_is_never_deleted(relay_server, tmp_path):
    mine = tmp_path / "scope" / "artifacts"
    mine.mkdir(parents=True)
    (mine / "mine.txt").write_text("user data")

    result, _ = _run_artifacts(["dump-dir"], tmp_path, relay_server)

    assert result.setup.status == "failed"
    assert "artifacts" in result.setup.error and "refusing to replace" in result.setup.error
    assert result.tasks == []
    assert (mine / "mine.txt").read_text() == "user data"
    assert relay_server.requests == []


def test_an_artifacts_directory_an_earlier_run_left_is_replaced(tmp_path):
    first, scope = _run_artifacts(["dump-dir"], tmp_path)
    assert first.tasks[0].status == "ok"
    assert (scope / "artifacts" / "dump").is_file()

    second, scope = _run_artifacts(["where"], tmp_path)  # same scope: `rwtask run --workdir`

    assert second.tasks[0].status == "ok", second
    assert not (scope / "artifacts" / "dump").exists()  # what the first run left is gone


def test_an_empty_artifacts_directory_and_a_symlink_named_artifacts_are_replaced(tmp_path):
    (tmp_path / "scope" / "artifacts").mkdir(parents=True)
    result, scope = _run_artifacts(["where"], tmp_path)
    assert result.tasks[0].status == "ok", result

    outside = tmp_path / "outside"
    outside.mkdir()
    (scope / "artifacts").rename(scope / "artifacts-old")
    (scope / "artifacts").symlink_to(outside, target_is_directory=True)
    result, scope = _run_artifacts(["where"], tmp_path)
    assert result.tasks[0].status == "ok", result
    assert list(outside.iterdir()) == []  # the link was removed, not followed
    assert not (scope / "artifacts").is_symlink()


@pytest.mark.parametrize("task", ["remove-then-pipe", "replace-dir"])
def test_the_artifacts_directory_carries_its_marker_when_the_request_ends(tmp_path, task):
    """A task that removes `$RW_ARTIFACTS_DIR` and has it made again (rw_artifact does)
    would leave one that is not marked as ours; the next `rwtask run --workdir` on the
    same scope must not take it for the user's."""
    result, scope = _run_artifacts([task], tmp_path)
    assert result.tasks[0].status == "ok", result
    assert (scope / "artifacts" / ".rw-artifacts-dir").is_file()

    again, _ = _run_artifacts(["where"], tmp_path)

    assert again.setup is None or again.setup.status != "failed", again.setup
    assert again.tasks[0].status == "ok", again


def test_stamping_the_artifacts_directory_never_writes_through_a_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "artifacts").symlink_to(outside, target_is_directory=True)

    _stamp_artifacts_dir(tmp_path)

    assert list(outside.iterdir()) == []
    (tmp_path / "artifacts").unlink()
    _stamp_artifacts_dir(tmp_path)  # nothing there: nothing to do
    (tmp_path / "artifacts").mkdir()
    _stamp_artifacts_dir(tmp_path)
    _stamp_artifacts_dir(tmp_path)  # idempotent
    assert [p.name for p in (tmp_path / "artifacts").iterdir()] == [".rw-artifacts-dir"]


def test_a_task_that_replaces_the_artifacts_directory_does_not_stop_the_next_one(tmp_path):
    # what a task leaves in a directory it replaced is this request's own, not a user's:
    # the next task starts with a fresh one regardless of the marker
    result, scope = _run_artifacts(["replace-dir", "where"], tmp_path)

    assert [t.status for t in result.tasks] == ["ok", "ok"]
    assert not (scope / "artifacts" / "junk").exists()


# -- uploads are bounded -----------------------------------------------------------------


def test_an_upload_to_a_relay_that_never_answers_ends_the_task_not_the_host(
    relay_server, tmp_path, monkeypatch
):
    monkeypatch.setattr(relay_module, "_RESPONSE_TIMEOUT", 0.5)
    relay_server.script("PUT", PUT_PREFIX, Response(201, json={"artifactRef": "x"}, delay=6))
    started = time.monotonic()

    result, _ = _within(30, _run_artifacts, ["dump-path"], tmp_path, relay_server)

    [task] = result.tasks
    assert task.status == "failed"
    assert task.error.startswith("E_ARTIFACT_UPLOAD: artifact 'report': "), task.error
    assert time.monotonic() - started < 5
    assert task.outputs == {}


def test_no_upload_starts_once_the_request_deadline_has_passed(relay_server, tmp_path, monkeypatch):
    relay_server.script("PUT", PUT_PREFIX, _created("art_never"))
    real = bundle_module._collect_artifacts

    def collect_slowly(*args):
        collected = real(*args)
        time.sleep(2.2)  # the task finished in time; collecting its files ran past the deadline
        return collected

    monkeypatch.setattr(bundle_module, "_collect_artifacts", collect_slowly)

    result, _ = _run_artifacts(["dump-path"], tmp_path, relay_server, deadline=2)

    [task] = result.tasks
    assert task.status == "timeout"
    assert task.error.startswith("E_TIMEOUT: "), task.error
    assert "artifact 'report'" in task.error
    assert relay_server.requests == []


def test_a_spilled_output_is_not_uploaded_after_the_deadline_either(relay_server, tmp_path):
    relay_server.script("PUT", PUT_PREFIX, _created("art_blob"))
    args = dict(
        capability="c",
        operation="t",
        scope_dir=tmp_path,
        relay=RelayClient(relay_server.url, _token(tmp_path), "req-1", requests.Session()),
        output_decls=[],
        artifact_specs={},
        files={},
        spilled={"blob": b'"x"'},
    )

    refs, errors, unavailable = _upload_artifacts(**args, deadline_at=time.monotonic() - 1)

    assert refs == {} and unavailable is None
    [error] = errors
    assert error.startswith("E_TIMEOUT: ") and "artifact 'blob'" in error
    assert relay_server.requests == []

    refs, errors, unavailable = _upload_artifacts(**args, deadline_at=time.monotonic() + 60)
    assert errors == [] and refs == {"blob": {"$artifact": "art_blob"}} and unavailable is None


def _token(tmp_path: Path) -> Path:
    token_file = tmp_path / "token"
    token_file.write_text("tok-1")
    return token_file
