"""rwtask serve runs a `runwhen.com/lease/v1` request (plugin/lease.py): validated
against the lease contract, its code pinned by sha256, run through the child runner, and
completed with the bare result envelope. A fake relay (`responses`), as in
test_serve_bundle.py."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import responses

from runwhen_capability.contracts import TreeEntry, archive, archive_sha256
from runwhen_capability.contracts import validate as validate_contract
from runwhen_capability.plugin import compile_tree, load_dir
from runwhen_capability.serve import serve

HERE = Path(__file__).resolve().parent
RUN_MINI = HERE / "plugin" / "fixtures" / "run-mini"
ECHO = HERE / "fixtures" / "capabilities" / "echo"
RELAY = "http://relay.example.internal"
DEPLOYMENT = "kubernetes/clusters/kind/namespaces/runwhen/deployments/runwhen-papi"
TARGET = {
    "path": DEPLOYMENT,
    "type": "kubernetes/deployment",
    "identity": {"cluster": "kind", "namespace": "runwhen", "name": "runwhen-papi"},
}

AUTH_PLAN = {
    "credentialType": "kubernetes/kubeconfig",
    "reference": {"kind": "file", "ref": "k8s:file@secret/kc:kubeconfig"},
    "method": {"native": "passthrough"},
    "materialize": {"file": {"env": "KUBECONFIG", "mode": "0600"}},
}

TREE = load_dir(RUN_MINI)
TASKS = {t["name"]: t for t in compile_tree(TREE)["tasks"]}


def entries() -> list[TreeEntry]:
    return [TreeEntry(p, d, p in TREE.executable) for p, d in TREE.files.items()]


def inline_code(base64_path: str | None = None) -> dict:
    files = []
    for entry in entries():
        item = {"path": entry.path, "content": entry.data.decode(), "executable": entry.executable}
        if entry.path == base64_path:
            item["content"] = base64.b64encode(entry.data).decode()
            item["encoding"] = "base64"
        files.append(item)
    return {"inline": {"sha256": archive_sha256(archive.build(entries())), "files": files}}


def make_lease(task_name: str, *, code=None, inputs=None, settings=None, timeout="30s") -> dict:
    task = TASKS[task_name]
    return {
        "apiVersion": "runwhen.com/lease/v1",
        "leaseId": "lease-1",
        "taskRun": "run-1",
        "task": {
            "ref": task["ref"],
            "plugin": "runmini",
            "version": "0.1.0",
            "skill": task["skill"],
            "name": task["name"],
            "hash": task["hash"],
            "script": task["script"],
            "readOnly": task["readOnly"],
            "dryRun": task["dryRun"],
        },
        "target": TARGET,
        "inputs": inputs or {},
        # papi resolves settings: every declared one, defaulted
        "settings": {
            **{name: spec["default"] for name, spec in task["settings"].items()},
            **(settings or {}),
        },
        "runtime": {"name": "generic", "major": 1, "image": "img", "digest": "sha256:" + "a" * 64},
        "code": code or inline_code(),
        "credentials": [],
        "limits": {"timeout": timeout},
        "outputs": task["outputs"],
    }


@pytest.fixture
def poll(tmp_path, monkeypatch):
    """Serves one polled request and returns (the POSTed result body, workdir)."""
    token = tmp_path / "token"
    token.write_text("test-token")
    monkeypatch.setenv("RW_ARCHIVE_CACHE", str(tmp_path / "archives"))
    monkeypatch.setenv("RW_CREDENTIALS_DIR", str(tmp_path / "creds"))

    def go(request, **extra):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                f"{RELAY}/v1/tasks/next",
                json={
                    "requestId": "req-1",
                    "request": request,
                    "credentials": extra.pop("credentials", {}),
                    "scopeId": "scope-1",
                    "deadlineMs": extra.pop("deadlineMs", 30000),
                    **extra,
                },
            )
            rsps.add(responses.POST, f"{RELAY}/v1/tasks/req-1/result", json={})
            serve(
                relay=RELAY,
                pool_id="pool-1",
                workdir=tmp_path / "work",
                capability_dir=ECHO,
                token_file=token,
                max_iterations=1,
            )
            assert len(rsps.calls) == 2
            body = json.loads(rsps.calls[1].request.body)
        return body

    go.workdir = tmp_path / "work"
    go.archives = tmp_path / "archives"
    go.creds = tmp_path / "creds"
    return go


def envelope_of(body: dict) -> dict:
    assert body["status"] == "ok", body
    envelope = body["result"]
    assert validate_contract("result-envelope", envelope) == []
    assert "tasks" not in envelope and "setup" not in envelope  # not the classic shape
    return envelope


def test_an_inline_python_lease_runs_end_to_end_and_completes_with_the_bare_envelope(poll):
    lease = make_lease("py-ok", inputs={"since": "1h", "limit": 2})
    envelope = envelope_of(poll(lease))
    assert envelope["status"] == "ok"
    summary = envelope["outputs"]["summary"]
    assert summary["name"] == "runwhen-papi"
    assert summary["namespace"] == "runwhen"
    assert summary["since"] == "1h"
    assert summary["limit"] == 2
    assert summary["threshold"] == 1500  # settings as the lease gave them
    assert summary["lib"] == "from-lib"
    assert not (poll.workdir / "scope-1").exists()  # a lease's scope is always wiped


def test_an_inline_bash_lease_runs_with_a_base64_file_and_needs_no_allow_bundles(poll):
    script = TASKS["sh-ok"]["script"]
    lease = make_lease(
        "sh-ok", code=inline_code(base64_path=script), inputs={"count": 1, "note": "hi"}
    )
    envelope = envelope_of(poll(lease))  # serve() was started without allow_bundles
    assert envelope["status"] == "ok"
    assert envelope["outputs"]["probe"] == [
        {"namespace": "runwhen", "name": "runwhen-papi", "threshold": 1500, "note": "hi"}
    ]


def test_a_task_that_fails_is_a_failed_envelope_in_an_ok_request(poll):
    envelope = envelope_of(poll(make_lease("py-raise")))
    assert envelope["status"] == "failed"
    assert [e["code"] for e in envelope["errors"]] == ["not_found"]


def test_a_task_past_limits_timeout_is_failed_with_timeout(poll):
    envelope = envelope_of(poll(make_lease("py-slow", timeout="1s")))
    assert envelope["status"] == "failed"
    assert [e["code"] for e in envelope["errors"]] == ["timeout"]


def test_the_requests_deadline_caps_limits_timeout(poll):
    envelope = envelope_of(poll(make_lease("py-slow", timeout="30s"), deadlineMs=1000))
    assert [e["code"] for e in envelope["errors"]] == ["timeout"]


def test_an_inline_hash_mismatch_runs_nothing(poll):
    lease = make_lease("py-ok")
    lease["code"]["inline"]["sha256"] = "sha256:" + "0" * 64
    envelope = envelope_of(poll(lease))
    assert envelope["status"] == "failed"
    [error] = envelope["errors"]
    assert error["code"] == "internal"
    assert "code hash mismatch" in error["message"]
    assert envelope["outputs"] == {}


def test_a_tampered_inline_file_is_a_hash_mismatch(poll):
    lease = make_lease("py-ok")
    for item in lease["code"]["inline"]["files"]:
        if item["path"].endswith("py_ok.py"):
            item["content"] += "\nimport os; os.system('id')\n"
    [error] = envelope_of(poll(lease))["errors"]
    assert "code hash mismatch" in error["message"]


def test_an_invalid_lease_is_failed_with_invalid_input_naming_the_violation(poll):
    lease = make_lease("py-ok")
    del lease["limits"]
    envelope = envelope_of(poll(lease))
    assert envelope["status"] == "failed"
    [error] = envelope["errors"]
    assert error["code"] == "invalid_input"
    assert "limits" in error["message"]


def test_an_archive_that_is_not_in_the_cache_is_a_dependency_failure(poll):
    sha = archive_sha256(archive.build(entries()))
    lease = make_lease("py-ok", code={"archive": {"sha256": sha, "size": 123}})
    [error] = envelope_of(poll(lease))["errors"]
    assert error["code"] == "dependency"
    assert "DLV-9" in error["message"]


def test_an_archive_in_the_cache_is_verified_and_run(poll):
    data = archive.build(entries())
    sha = archive_sha256(data)
    poll.archives.mkdir()
    (poll.archives / f"{sha.removeprefix('sha256:')}.tar").write_bytes(data)
    lease = make_lease("py-ok", code={"archive": {"sha256": sha, "size": len(data)}})
    envelope = envelope_of(poll(lease))
    assert envelope["status"] == "ok"
    assert envelope["outputs"]["summary"]["lib"] == "from-lib"


def test_a_corrupt_archive_in_the_cache_is_a_hash_mismatch(poll):
    data = archive.build(entries())
    sha = archive_sha256(data)
    poll.archives.mkdir()
    (poll.archives / f"{sha.removeprefix('sha256:')}.tar").write_bytes(data + b"\0" * 512)
    lease = make_lease("py-ok", code={"archive": {"sha256": sha, "size": len(data)}})
    [error] = envelope_of(poll(lease))["errors"]
    assert error["code"] == "internal"


def test_a_lease_whose_code_lacks_its_script_fails(poll):
    lease = make_lease("py-ok")
    lease["task"]["script"] = "skills/probe/scripts/missing.py"
    [error] = envelope_of(poll(lease))["errors"]
    assert error["code"] == "internal"
    assert "missing.py" in error["message"]


def test_host_only_variables_stay_out_of_a_lease_task(poll, monkeypatch):
    monkeypatch.setenv("RELAY_URL", "http://relay.invalid")
    monkeypatch.setenv("POOL_ID", "pool-9")
    monkeypatch.setenv("EXECUTOR_TOKEN_FILE", "/var/run/executor/token")
    envelope = envelope_of(poll(make_lease("sh-env")))
    seen = envelope["outputs"]["env"]
    assert (seen["relay"], seen["pool"], seen["token"]) == ("unset", "unset", "unset")
    assert seen["contract"] == "v13"


def test_a_failed_result_that_is_too_large_is_still_capped_by_the_transport(poll):
    body = poll(make_lease("py-ok"), maxResultBytes=50)
    assert body["status"] == "failed"
    assert "E_RESULT_TOO_LARGE" in body["error"]


# -- credentials, the old way ------------------------------------------------------------------


def test_the_credentials_map_sets_kubeconfig_to_a_private_file_and_is_redacted(poll):
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: kind-secret-cluster}]\n"
    lease = make_lease("py-creds", inputs={"names": "KUBECONFIG"})
    lease["credentials"] = [
        {
            "type": "kubernetes/kubeconfig",
            "connection": "main",
            "authPlan": AUTH_PLAN,
        }
    ]
    envelope = envelope_of(poll(lease, credentials={"kubeconfig": kubeconfig}))
    assert envelope["status"] == "ok", envelope["errors"]
    seen = envelope["outputs"]["creds"]["KUBECONFIG"]
    assert seen["len"] == len(kubeconfig)
    assert seen["mode"] == "0o600"
    assert seen["content"] == "***REDACTED***"
    assert "kind-secret-cluster" not in json.dumps(envelope)
    assert list(poll.creds.iterdir()) == []  # the file is gone


def test_a_classic_bundle_request_is_unchanged_next_to_leases(poll):
    from bundle_fixtures import load_bundle

    from runwhen_capability.custom.hashing import content_hash

    files = load_bundle("pgbouncer-health")
    request = {
        "bundle": {
            "hash": content_hash(files),
            "files": [{"path": p, "content": c} for p, c in files.items()],
        },
        "tasks": ["pool-errors"],
        "inputs": {"since": "30m", "maxWait": 20},
        "target": {"urn": "urn:resource:pgbouncer"},
    }
    body = poll(request)  # allow_bundles is off: refused exactly as before
    assert body["status"] == "failed"
    assert "--allow-bundles" in body["error"]


# -- materialized credentials (lane C's contract) ----------------------------------------------

SECRET_FILE = "-----BEGIN KEY-----\nsupersecretkeymaterial123\n-----END KEY-----\n"
SECRET_ENV = "dbuser-secret-value"


def creds_lease(names: str, task: str = "py-creds", **kw) -> dict:
    return make_lease(task, inputs={"names": names}, **kw)


def materialized() -> list[dict]:
    return [
        {
            "credentialType": "generic/env",
            "files": [{"env": "MY_KEY_FILE", "content": SECRET_FILE}],
            "env": {"PGUSER": SECRET_ENV},
        },
        {
            "credentialType": "aws/credentials",
            "files": [
                {
                    "env": "AWS_SHARED_CREDENTIALS_FILE",
                    "mode": "0640",
                    "encoding": "base64",
                    "content": base64.b64encode(
                        b"[default]\naws_secret_access_key=abcdef123456\n"
                    ).decode(),
                }
            ],
        },
    ]


def test_materialized_files_are_private_visible_by_env_scrubbed_and_deleted(poll):
    names = "MY_KEY_FILE,PGUSER,AWS_SHARED_CREDENTIALS_FILE"
    body = poll(creds_lease(names), materialized=materialized())
    envelope = envelope_of(body)
    assert envelope["status"] == "ok", envelope["errors"]
    seen = envelope["outputs"]["creds"]
    assert seen["MY_KEY_FILE"]["len"] == len(SECRET_FILE)  # the task read the real file
    assert seen["MY_KEY_FILE"]["mode"] == "0o600"  # the default
    assert seen["MY_KEY_FILE"]["dirMode"] == "0o700"
    assert seen["AWS_SHARED_CREDENTIALS_FILE"]["mode"] == "0o640"  # the entry's
    assert seen["PGUSER"] == {"value": "***REDACTED***"}  # an env{} pair is exported, and scrubbed
    assert seen["MY_KEY_FILE"]["content"] == "***REDACTED***"
    text = json.dumps(body)
    for secret in ("supersecretkeymaterial123", SECRET_ENV, "abcdef123456"):
        assert secret not in text
    assert list(poll.creds.iterdir()) == []  # deleted with the task
    assert not (poll.workdir / "scope-1").exists()


def test_materialized_files_are_deleted_after_a_failure_and_after_a_timeout(poll):
    envelope = envelope_of(poll(creds_lease("", "py-crash"), materialized=materialized()))
    assert envelope["status"] == "failed"
    assert list(poll.creds.iterdir()) == []
    envelope = envelope_of(
        poll(creds_lease("", "py-slow", timeout="1s"), materialized=materialized())
    )
    assert [e["code"] for e in envelope["errors"]] == ["timeout"]
    assert list(poll.creds.iterdir()) == []


@pytest.mark.parametrize(
    "name", ["RELAY_URL", "POOL_ID", "EXECUTOR_TOKEN_FILE", "RW_SDK", "HOME", "PATH", "BASH_ENV"]
)
def test_materialized_cannot_name_a_reserved_or_host_only_variable(poll, name):
    for entry in (
        {"credentialType": "generic/env", "env": {name: "planted-secret-value"}},
        {"credentialType": "generic/env", "files": [{"env": name, "content": "planted-secret"}]},
    ):
        envelope = envelope_of(poll(creds_lease(""), materialized=[entry]))
        assert envelope["status"] == "failed"
        [error] = envelope["errors"]
        assert error["code"] == "invalid_input"
        assert "reserved" in error["message"]
        assert "planted-secret" not in json.dumps(envelope)
        assert not poll.creds.exists() or list(poll.creds.iterdir()) == []


@pytest.mark.parametrize(
    "bad",
    [
        "a string",
        {"files": []},
        {"credentialType": "x/y", "surprise": 1},
        {"credentialType": "x/y", "files": [{"env": "A_B", "content": "x", "mode": "600"}]},
        {"credentialType": "x/y", "files": [{"env": "A_B", "content": "!!", "encoding": "base64"}]},
        {"credentialType": "x/y", "files": [{"env": "lower-case", "content": "x"}]},
        {"credentialType": "x/y", "env": {"A_B": "1"}, "files": [{"env": "A_B", "content": "x"}]},
    ],
)
def test_a_malformed_materialized_entry_fails_the_request_not_the_poll(poll, bad):
    envelope = envelope_of(poll(creds_lease(""), materialized=[bad]))
    assert envelope["status"] == "failed"
    assert [e["code"] for e in envelope["errors"]] == ["invalid_input"]
    assert envelope["errors"][0]["message"].startswith("materialized[0]")


def test_host_only_variables_stay_unset_when_credentials_are_materialized(poll, monkeypatch):
    monkeypatch.setenv("RELAY_URL", "http://relay.invalid")
    monkeypatch.setenv("POOL_ID", "pool-9")
    monkeypatch.setenv("EXECUTOR_TOKEN_FILE", "/var/run/executor/token")
    envelope = envelope_of(poll(make_lease("sh-env"), materialized=materialized()))
    seen = envelope["outputs"]["env"]
    assert (seen["relay"], seen["pool"], seen["token"]) == ("unset", "unset", "unset")


def test_startup_removes_credential_directories_a_crashed_run_left(poll):
    stale = poll.creds / "task-crashed"
    stale.mkdir(parents=True)
    (stale / "KEY").write_text("left-behind-secret")
    other = poll.creds / "not-ours"
    other.mkdir()
    envelope_of(poll(make_lease("py-nothing")))
    assert not stale.exists()
    assert other.exists()


def test_materialized_is_ignored_for_a_classic_request(poll):
    # materialized[] is for V13 leases; a packaged request ignores it and writes nothing
    body = poll(
        {"tasks": []}, materialized=materialized()
    )  # a packaged request against the echo capability
    assert body["status"] == "ok"
    assert not poll.creds.exists()  # nothing was written for it
