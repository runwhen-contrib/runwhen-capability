"""rwtask serve runs a `runwhen.com/lease/v1` request (plugin/lease.py): validated
against the lease contract, its code pinned by sha256, run through the child runner, and
completed with the bare result envelope. A fake relay (`responses`), as in
test_serve_bundle.py."""

from __future__ import annotations

import base64
import json
import threading
from pathlib import Path

import pytest
import requests
import responses
from relay_server import Response, refused_url

from runwhen_capability.contracts import TreeEntry, archive, archive_sha256
from runwhen_capability.contracts import validate as validate_contract
from runwhen_capability.plugin import compile_tree, load_dir
from runwhen_capability.plugin.lease import LeaseError, load_code
from runwhen_capability.relay import RelayClient
from runwhen_capability.serve import _is_safe_scope_id, serve

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
    monkeypatch.setenv("RW_ARCHIVE_CACHE", str(tmp_path / "archives"))
    monkeypatch.setenv("RW_CREDENTIALS_DIR", str(tmp_path / "creds"))

    def go(request, *, allow_bundles=True, **extra):
        token.write_text("test-token")  # serve reads it once and unlinks it
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
                allow_bundles=allow_bundles,
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


def test_an_inline_bash_lease_runs_with_a_base64_file(relayed):
    script = TASKS["sh-ok"]["script"]
    lease = make_lease(
        "sh-ok", code=inline_code(base64_path=script), inputs={"count": 1, "note": "hi"}
    )
    decl = {"task": "sh-ok", "name": "events", "artifact": True, "contentType": "application/json"}
    body, [put] = relayed(lease, created(ref="art_events1", size=60), [decl])
    envelope = envelope_of(body)
    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["outputs"]["probe"] == [
        {"namespace": "runwhen", "name": "runwhen-papi", "threshold": 1500, "note": "hi"}
    ]
    assert envelope["outputs"]["events"]["artifact"] == "art_events1"
    assert put.path.endswith("/artifacts/events")


def test_a_lease_is_refused_unless_plugin_code_is_allowed(poll):
    """Hash verification proves integrity, not trust: the same opt-in as a bundle."""
    body = poll(make_lease("py-ok"), allow_bundles=False, materialized=materialized())
    envelope = envelope_of(body)
    assert envelope["status"] == "failed"
    [error] = envelope["errors"]
    assert error["code"] == "invalid_input"
    assert error["message"] == (
        "this executor does not run plugin code (start with --allow-bundles, or RW_ALLOW_BUNDLES=1)"
    )
    assert envelope["outputs"] == {}
    # nothing was written or run: no scope, no credential directory
    assert not (poll.workdir / "scope-1").exists()
    assert not poll.creds.exists()


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


def test_an_archive_the_relay_does_not_have_is_a_dependency_failure(fetch):
    data = archive.build(entries())
    sha = archive_sha256(data)
    lease = make_lease("py-ok", code={"archive": {"sha256": sha, "size": len(data)}})
    [error] = envelope_of(fetch(lease, Response(404, json={"code": "E_NO_CODE"})))["errors"]
    assert error["code"] == "dependency"
    assert sha in error["message"]
    assert "DLV-9" not in error["message"]
    assert not any(fetch.archives.glob("*"))


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


# -- code archives fetched from the relay (I3) ---------------------------------------------------


@pytest.fixture
def fetch(relay_server, tmp_path, monkeypatch):
    """Serves one polled lease against the real local relay, which scripts the answers of
    `GET /v1/tasks/req-1/code`. Returns the POSTed result body; the relay's recorded
    requests are `relay_server.requests`."""
    token = tmp_path / "token"
    monkeypatch.setenv("RW_ARCHIVE_CACHE", str(tmp_path / "archives"))
    monkeypatch.setenv("RW_CREDENTIALS_DIR", str(tmp_path / "creds"))

    def go(lease, *code: Response):
        token.write_text("test-token")  # serve reads it once and unlinks it
        relay_server.script(
            "POST",
            "/v1/tasks/next",
            Response(
                200,
                json={
                    "requestId": "req-1",
                    "request": lease,
                    "credentials": {},
                    "scopeId": "scope-1",
                    "deadlineMs": 30000,
                },
            ),
        )
        if code:
            relay_server.script("GET", "/v1/tasks/req-1/code", *code)
        relay_server.script("POST", "/v1/tasks/req-1/result", Response(200, json={}))
        before = len(relay_server.requests)
        serve(
            relay=relay_server.url,
            pool_id="pool-1",
            workdir=tmp_path / "work",
            capability_dir=ECHO,
            token_file=token,
            max_iterations=1,
            allow_bundles=True,
        )
        [result] = [r for r in relay_server.requests[before:] if r.path.endswith("/result")]
        return json.loads(result.body)

    go.archives = tmp_path / "archives"
    go.code_requests = lambda: [r for r in relay_server.requests if r.path.endswith("/code")]
    return go


def code_response(data: bytes, sha: str | None = None, **headers) -> Response:
    sha = sha or archive_sha256(data)
    return Response(200, body=data, headers={"X-RW-Sha256": sha, **headers})


def archive_lease(data: bytes, size: int | None = None) -> dict:
    code = {
        "archive": {"sha256": archive_sha256(data), "size": len(data) if size is None else size}
    }
    return make_lease("py-ok", code=code)


def test_an_archive_missing_from_the_cache_is_fetched_verified_cached_and_run(fetch):
    data = archive.build(entries())
    envelope = envelope_of(fetch(archive_lease(data), code_response(data)))
    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["outputs"]["summary"]["lib"] == "from-lib"
    [request] = fetch.code_requests()
    assert request.method == "GET"
    assert request.header("authorization") == "Bearer test-token"
    hex_digest = archive_sha256(data).removeprefix("sha256:")
    assert [p.name for p in fetch.archives.iterdir()] == [f"{hex_digest}.tar"]  # no temp file left
    assert (fetch.archives / f"{hex_digest}.tar").read_bytes() == data


def test_a_cached_archive_is_not_fetched_again(fetch):
    data = archive.build(entries())
    lease = archive_lease(data)
    assert envelope_of(fetch(lease, code_response(data)))["status"] == "ok"
    assert envelope_of(fetch(lease))["status"] == "ok"  # the relay has no more answers
    assert len(fetch.code_requests()) == 1


def test_fetched_bytes_that_do_not_hash_to_the_lease_are_refused_and_not_cached(fetch):
    data = archive.build(entries())
    wrong = bytes([data[0] ^ 1]) + data[1:]  # same length, other bytes
    lease = archive_lease(data)
    [error] = envelope_of(fetch(lease, code_response(wrong, sha=archive_sha256(data))))["errors"]
    assert error["code"] == "dependency"
    assert archive_sha256(data) in error["message"]
    assert not any(fetch.archives.glob("*"))
    # the failure is not remembered: the next request fetches again, and succeeds
    assert envelope_of(fetch(lease, code_response(data)))["status"] == "ok"
    assert len(fetch.code_requests()) == 2


def test_an_x_rw_sha256_header_that_names_another_archive_is_refused(fetch):
    data = archive.build(entries())
    other = "sha256:" + "0" * 64
    [error] = envelope_of(fetch(archive_lease(data), code_response(data, sha=other)))["errors"]
    assert error["code"] == "dependency"
    assert not any(fetch.archives.glob("*"))


@pytest.mark.parametrize("delta", [-1, 1])
def test_a_body_whose_length_is_not_the_leases_size_is_refused(fetch, delta):
    data = archive.build(entries())
    lease = archive_lease(data, size=len(data) + delta)
    [error] = envelope_of(fetch(lease, code_response(data)))["errors"]
    assert error["code"] == "dependency"
    assert not any(fetch.archives.glob("*"))


def test_the_size_cap_is_enforced_while_reading_not_after(fetch):
    """A relay that keeps sending past the lease's size is cut off, not buffered."""
    data = archive.build(entries())
    endless = Response(200, stream=iter([data] + [b"x" * 65536] * 8), content_length=1 << 30)
    [error] = envelope_of(fetch(archive_lease(data), endless))["errors"]
    assert error["code"] == "dependency"
    assert "more than" in error["message"]
    assert not any(fetch.archives.glob("*"))


def test_a_relay_that_cuts_the_download_short_is_a_dependency_failure(fetch):
    data = archive.build(entries())
    cut = Response(200, stream=iter([data[:100]]), content_length=len(data))
    [error] = envelope_of(fetch(archive_lease(data), cut))["errors"]
    assert error["code"] == "dependency"
    assert archive_sha256(data) in error["message"]
    assert not any(fetch.archives.glob("*"))


def test_a_relay_error_names_the_archive_but_not_the_relay(fetch, relay_server):
    data = archive.build(entries())
    [error] = envelope_of(fetch(archive_lease(data), Response(500, body=b"boom")))["errors"]
    assert error["code"] == "dependency"
    assert archive_sha256(data) in error["message"]
    assert relay_server.url not in error["message"]
    assert "test-token" not in error["message"]


def test_an_unreachable_relay_fails_without_naming_it(tmp_path):
    data = archive.build(entries())
    sha = archive_sha256(data)
    client = RelayClient(refused_url(), None, "req-1", requests.Session(), token="s3cret")
    with pytest.raises(LeaseError) as raised:
        load_code({"archive": {"sha256": sha, "size": len(data)}}, tmp_path / "cache", client)
    assert raised.value.code == "dependency"
    assert sha in raised.value.message
    assert "127.0.0.1" not in raised.value.message
    assert "s3cret" not in raised.value.message
    assert not (tmp_path / "cache").exists()


def test_without_a_relay_a_missing_archive_is_a_clear_dependency_failure(tmp_path):
    sha = archive_sha256(archive.build(entries()))
    with pytest.raises(LeaseError) as raised:
        load_code({"archive": {"sha256": sha, "size": 1}}, tmp_path / "cache")
    assert raised.value.code == "dependency"
    assert sha in raised.value.message
    assert "relay" in raised.value.message


def test_concurrent_requests_for_one_archive_both_succeed_and_leave_one_file(
    relay_server, tmp_path
):
    data = archive.build(entries())
    sha = archive_sha256(data)
    code = {"archive": {"sha256": sha, "size": len(data)}}
    cache = tmp_path / "cache"
    # each answer is held back, so both fetches are in flight before either finishes
    relay_server.script(
        "GET",
        "/v1/tasks/",
        *[Response(200, body=data, headers={"X-RW-Sha256": sha}, delay=0.3) for _ in range(2)],
    )
    results: list = []

    def run(request_id: str) -> None:
        client = RelayClient(relay_server.url, None, request_id, requests.Session(), token="t")
        try:
            results.append(load_code(code, cache, client))
        except Exception as exc:  # noqa: BLE001 -- reported by the assertion below
            results.append(exc)

    threads = [threading.Thread(target=run, args=(f"req-{n}",)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 2
    assert all(isinstance(r, tuple) for r in results), results
    assert results[0] == results[1]
    assert len([r for r in relay_server.requests if r.path.endswith("/code")]) == 2
    assert [p.name for p in cache.iterdir()] == [f"{sha.removeprefix('sha256:')}.tar"]
    assert (cache / f"{sha.removeprefix('sha256:')}.tar").read_bytes() == data


@pytest.mark.parametrize("scope_id", [".archives", ".rw-artifact-cache", "", "..", "a/b"])
def test_the_archive_cache_directory_is_not_a_scope_id(scope_id):
    assert not _is_safe_scope_id(scope_id)


def test_an_ordinary_scope_id_is_still_safe():
    assert _is_safe_scope_id("scope-1")


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
    monkeypatch.setenv("RW_EXECUTOR_TOKEN_FILE", "/run/rw/executor/token")
    envelope = envelope_of(poll(make_lease("sh-env")))
    seen = envelope["outputs"]["env"]
    assert (seen["relay"], seen["pool"], seen["token"], seen["rwtoken"]) == ("unset",) * 4
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
    body = poll(request, allow_bundles=False)  # refused exactly as before
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
    "name",
    [
        "RELAY_URL",
        "POOL_ID",
        "EXECUTOR_TOKEN_FILE",
        "RW_EXECUTOR_TOKEN_FILE",
        "RW_SDK",
        "HOME",
        "PATH",
        "BASH_ENV",
    ],
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
    monkeypatch.setenv("RW_EXECUTOR_TOKEN_FILE", "/run/rw/executor/token")
    envelope = envelope_of(poll(make_lease("sh-env"), materialized=materialized()))
    seen = envelope["outputs"]["env"]
    assert (seen["relay"], seen["pool"], seen["token"], seen["rwtoken"]) == ("unset",) * 4


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


# -- artifact outputs through the relay -----------------------------------------------------------


@pytest.fixture
def relayed(relay_server, tmp_path, monkeypatch):
    """Serves one polled lease against the real local relay, which scripts the artifact PUT.
    Returns (the POSTed result body, the recorded artifact PUTs)."""
    from relay_server import Response

    token = tmp_path / "token"
    monkeypatch.setenv("RW_CREDENTIALS_DIR", str(tmp_path / "creds"))

    def go(lease, put: Response | None, decls=None):
        token.write_text("test-token")  # serve reads it once and unlinks it
        relay_server.script(
            "POST",
            "/v1/tasks/next",
            Response(
                200,
                json={
                    "requestId": "req-1",
                    "request": lease,
                    "credentials": {},
                    "scopeId": "scope-1",
                    "deadlineMs": 30000,
                    "outputs": decls or [],
                },
            ),
        )
        if put is not None:
            relay_server.script("PUT", "/v1/tasks/req-1/artifacts/", put)
        relay_server.script("POST", "/v1/tasks/req-1/result", Response(200, json={}))
        serve(
            relay=relay_server.url,
            pool_id="pool-1",
            workdir=tmp_path / "work",
            capability_dir=ECHO,
            token_file=token,
            max_iterations=1,
            allow_bundles=True,
        )
        [result] = [r for r in relay_server.requests if r.path.endswith("/result")]
        puts = [r for r in relay_server.requests if r.method == "PUT"]
        return json.loads(result.body), puts

    return go


def created(ref="art_dump1", size=7, **extra):
    from relay_server import Response

    return Response(201, json={"artifactRef": ref, "sizeBytes": size, "sha256": "ab" * 32, **extra})


DUMP_DECL = {"task": "py-artifact", "name": "dump", "artifact": True, "contentType": "text/plain"}


def test_an_artifact_output_is_uploaded_through_the_relay(relayed):
    body, [put] = relayed(make_lease("py-artifact"), created(), [DUMP_DECL])
    envelope = envelope_of(body)
    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["outputs"]["dump"] == {
        "artifact": "art_dump1",
        "contentType": "text/plain",
        "size": 7,
    }
    assert envelope["outputs"]["n"] == {"v": 1}
    assert put.path == "/v1/tasks/req-1/artifacts/dump"
    assert put.body == b"a dump\n"  # the file, not the scratch file next to it
    assert put.header("content-type") == "text/plain"
    assert put.header("x-rw-task") == "py-artifact"
    assert put.header("authorization") == "Bearer test-token"
    assert "declaredButNeverWritten" not in envelope["outputs"]


def test_the_relays_task_name_comes_from_the_requests_own_declaration(relayed):
    decl = {**DUMP_DECL, "task": "runmini:probe/py-artifact"}
    body, [put] = relayed(make_lease("py-artifact"), created(), [decl])
    assert envelope_of(body)["status"] == "ok"
    assert put.header("x-rw-task") == "runmini:probe/py-artifact"


def test_a_truncated_artifact_is_marked_truncated(relayed):
    body, _ = relayed(make_lease("py-artifact"), created(truncated=True), [DUMP_DECL])
    assert envelope_of(body)["outputs"]["dump"]["truncated"] is True


def test_a_relay_ref_that_is_not_an_artifact_ref_is_an_internal_error(relayed):
    body, _ = relayed(make_lease("py-artifact"), created(ref="01HZX-not-art-prefixed"), [DUMP_DECL])
    envelope = envelope_of(body)
    assert envelope["status"] == "partial"
    assert "dump" not in envelope["outputs"]
    [error] = envelope["errors"]
    assert error["code"] == "internal"
    assert error["scope"] == "dump"
    assert "01HZX-not-art-prefixed" in error["message"]
    assert "^art_" in error["message"]


def test_artifacts_switched_off_on_the_runner_is_a_storage_error(relayed):
    from relay_server import Response

    off = Response(422, json={"code": "E_ARTIFACTS_DISABLED", "message": "artifacts are disabled"})
    body, _ = relayed(make_lease("py-artifact"), off, [DUMP_DECL])
    envelope = envelope_of(body)
    assert envelope["status"] == "partial"
    assert [e["code"] for e in envelope["errors"]] == ["storage"]
    assert "E_ARTIFACTS_DISABLED" in envelope["errors"][0]["message"]
    assert envelope["outputs"] == {"n": {"v": 1}}


def test_an_artifact_over_the_relays_limit_is_a_storage_error(relayed):
    from relay_server import Response

    big = Response(413, json={"code": "E_ARTIFACT_TOO_LARGE", "maxBytes": 3})
    envelope = envelope_of(relayed(make_lease("py-artifact"), big, [DUMP_DECL])[0])
    assert [e["code"] for e in envelope["errors"]] == ["storage"]
    assert "E_ARTIFACT_TOO_LARGE" in envelope["errors"][0]["message"]


def test_nothing_is_uploaded_for_a_task_that_failed_or_wrote_no_artifact(relayed):
    body, puts = relayed(make_lease("py-nothing"), None, [DUMP_DECL])
    assert envelope_of(body)["status"] == "ok"
    assert puts == []


# -- lease.dryRun (V2-7) ------------------------------------------------------------------


def dry_run_lease(task_name: str, **kw) -> dict:
    return {**make_lease(task_name, **kw), "dryRun": True}


def test_a_lease_without_dry_run_runs_for_real_even_when_the_task_supports_it(poll):
    envelope = envelope_of(poll(make_lease("sh-write")))
    assert envelope["status"] == "ok"
    assert envelope["outputs"]["result"] == {"dry": False}
    assert "dryRun" not in envelope
    envelope = envelope_of(poll({**make_lease("sh-write"), "dryRun": False}))
    assert envelope["outputs"]["result"] == {"dry": False} and "dryRun" not in envelope


def test_a_dry_run_lease_sets_rw_dry_run_and_marks_the_envelope(poll):
    envelope = envelope_of(poll(dry_run_lease("sh-write")))
    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["outputs"]["result"] == {"dry": True}
    assert envelope["dryRun"] is True


def test_a_python_task_sees_ctx_dry_run_under_a_dry_run_lease(poll):
    envelope = envelope_of(poll(dry_run_lease("py-ok")))
    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["outputs"]["summary"]["dry"] is True
    assert envelope["dryRun"] is True


def test_a_dry_run_lease_for_a_task_without_dry_run_fails_invalid_input_and_runs_nothing(poll):
    envelope = envelope_of(poll(dry_run_lease("sh-write-no-dry")))
    assert envelope["status"] == "failed"
    assert envelope["outputs"] == {}
    [error] = envelope["errors"]
    assert error["code"] == "invalid_input"
    assert error["message"] == "task does not support dry run"


def test_a_dry_run_lease_must_say_a_boolean(poll):
    envelope = envelope_of(poll({**make_lease("sh-write"), "dryRun": "yes"}))
    [error] = envelope["errors"]
    assert error["code"] == "invalid_input" and "dryRun" in error["message"]


# -- provenance (VER-6) -------------------------------------------------------------------------


def expected_provenance(lease: dict, **extra) -> dict:
    """What a lease's envelope must carry, built by hand from the lease."""
    code = lease["code"]
    return {
        "task": lease["task"]["ref"],
        "plugin": "runmini",
        "version": "0.1.0",
        "taskHash": lease["task"]["hash"],
        "archiveSha256": (code.get("archive") or code["inline"])["sha256"],
        "runtime": {"image": "img", "digest": "sha256:" + "a" * 64},
        "leaseId": "lease-1",
        "taskRun": "run-1",
        **extra,
    }


def test_an_ok_envelope_carries_the_leases_provenance(poll):
    lease = make_lease("py-ok")
    envelope = envelope_of(poll(lease))
    assert envelope["status"] == "ok"
    assert envelope["provenance"] == expected_provenance(lease)
    assert envelope["provenance"]["taskHash"] == TASKS["py-ok"]["hash"]


def test_a_failed_and_a_timed_out_envelope_carry_provenance(poll):
    for name, timeout in (("py-raise", "30s"), ("py-slow", "1s")):
        lease = make_lease(name, timeout=timeout)
        envelope = envelope_of(poll(lease))
        assert envelope["status"] == "failed"
        assert envelope["provenance"] == expected_provenance(lease)


def test_a_refusal_after_the_lease_validated_carries_provenance(poll):
    mismatch = make_lease("py-ok")
    mismatch["code"]["inline"]["sha256"] = "sha256:" + "0" * 64
    envelope = envelope_of(poll(mismatch))
    assert "code hash mismatch" in envelope["errors"][0]["message"]
    # the lease's own claim, even though it is the one that was wrong
    assert envelope["provenance"] == expected_provenance(mismatch)

    dry = dry_run_lease("sh-write-no-dry")
    envelope = envelope_of(poll(dry))
    assert envelope["errors"][0]["message"] == "task does not support dry run"
    assert envelope["provenance"] == expected_provenance(dry)


def test_an_archive_that_could_not_be_fetched_carries_provenance(fetch):
    sha = archive_sha256(archive.build(entries()))
    missing = make_lease("py-ok", code={"archive": {"sha256": sha, "size": 123}})
    envelope = envelope_of(fetch(missing, Response(404, json={"code": "E_NO_CODE"})))
    assert envelope["errors"][0]["code"] == "dependency"
    assert envelope["provenance"] == expected_provenance(missing)
    assert envelope["provenance"]["archiveSha256"] == sha


def test_a_lease_refused_for_the_opt_in_carries_provenance_when_it_validates(poll):
    lease = make_lease("py-ok")
    envelope = envelope_of(poll(lease, allow_bundles=False))
    assert envelope["errors"][0]["code"] == "invalid_input"
    assert envelope["provenance"] == expected_provenance(lease)
    broken = make_lease("py-ok")
    del broken["limits"]
    assert "provenance" not in envelope_of(poll(broken, allow_bundles=False))


def test_an_invalid_lease_gets_no_provenance(poll):
    lease = make_lease("py-ok")
    del lease["limits"]
    envelope = envelope_of(poll(lease))
    assert envelope["errors"][0]["code"] == "invalid_input"
    assert "provenance" not in envelope


def test_provenance_names_the_first_connection_of_the_credentials(poll):
    lease = make_lease("py-ok")
    lease["credentials"] = [
        {"type": "runwhen/sync", "minted": {"ref": "m1", "ttlSeconds": 60}},
        {"type": "kubernetes/kubeconfig", "connection": "prod-kube", "authPlan": AUTH_PLAN},
        {"type": "kubernetes/kubeconfig", "connection": "other", "authPlan": AUTH_PLAN},
    ]
    envelope = envelope_of(poll(lease))
    assert envelope["provenance"] == expected_provenance(lease, connection="prod-kube")
    assert "connection" not in envelope_of(poll(make_lease("py-ok")))["provenance"]


# -- CAP-7: limits.resources.memory ---------------------------------------------------------------


def test_a_leases_memory_limit_becomes_the_runspecs_and_cpu_is_not_read():
    from runwhen_capability.plugin.lease import runspec_from_lease

    lease = make_lease("py-ok")
    assert runspec_from_lease(lease).memory is None
    lease["limits"]["resources"] = {"cpu": "500m", "memory": "256Mi"}
    assert runspec_from_lease(lease).memory == "256Mi"


def test_a_lease_with_room_in_its_memory_limit_runs_normally(poll):
    lease = make_lease("py-ok")
    lease["limits"]["resources"] = {"cpu": "1", "memory": "2Gi"}
    envelope = envelope_of(poll(lease))
    assert envelope["status"] == "ok", envelope["errors"]
    assert "provenance" in envelope
