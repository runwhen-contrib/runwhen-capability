"""Script auth plans run in the executor before the task script (lane C's CON-5, CONTRACTS.md
"Auth plan"): a lease credential whose `authPlan.method` is a `script` fetches the integration
plugin's archive, writes the runner's `materialized[].reference` to `$RW_AUTH_REF_DIR/<key>`,
runs `integration/<path>` with `RW_AUTH_INPUT_*` and `RW_AUTH_OUT_FILE` / `RW_AUTH_OUT_ENV`,
and gives the task what it produced. A failure fails the task before its script starts."""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import pytest
import requests
from auth_plugin import (
    KEY_JSON,
    KEY_MARKER,
    SCRIPTS,
    TOKEN,
    build_task_plugin,
    integration_archive,
)
from relay_server import Response

from runwhen_capability.contracts import TreeEntry, archive, archive_sha256
from runwhen_capability.contracts import validate as validate_contract
from runwhen_capability.plugin import compile_tree, load_dir
from runwhen_capability.plugin.lease import run_lease
from runwhen_capability.relay import RelayClient
from runwhen_capability.serve import serve

ECHO = Path(__file__).resolve().parent / "fixtures" / "capabilities" / "echo"
DEPLOYMENT = "kubernetes/clusters/kind/namespaces/runwhen/deployments/runwhen-papi"
TARGET = {
    "path": DEPLOYMENT,
    "type": "kubernetes/deployment",
    "identity": {"cluster": "kind", "namespace": "runwhen", "name": "runwhen-papi"},
}
HOST_ONLY = {
    "RELAY_URL": "http://relay.invalid",
    "POOL_ID": "pool-9",
    "EXECUTOR_TOKEN_FILE": "/var/run/executor/token",
    "RW_EXECUTOR_TOKEN_FILE": "/run/rw/executor/token",
}
VAULT = {"kind": "vault", "ref": "secret/data/rw/gcp-prod-sa", "content": "gcp-sa-key"}
REFERENCE = {
    "credentialType": "kubernetes/kubeconfig",
    "reference": {
        "files": [
            {"key": "key.json", "content": KEY_JSON},
            {"key": "token", "content": TOKEN},
        ]
    },
}
GKE_INPUTS = {"project": "acme-prod", "location": "us-east1", "cluster": "prod-east"}
FILE_TARGET = {"file": {"env": "KUBECONFIG", "mode": "0600"}}
ENV_REFERENCE = {
    "credentialType": "generic/env",
    "reference": {"files": [{"key": "value", "content": "hunter2-db-password"}]},
}
ENV_INPUTS = {"dbUser": "reader-7"}
ENV_TARGET = {"env": ["PGUSER", "PGPASSWORD"]}


def file_plan(script="auth/fake.sh", *, archive_info, inputs=None, mode="0600", reference=VAULT):
    return {
        "connection": "prod-east",
        "integration": "authint@0.1.0",
        "authMethod": "gke",
        "credentialType": "kubernetes/kubeconfig",
        "reference": reference,
        "method": {"script": {"path": script, "archive": archive_info}},
        "inputs": GKE_INPUTS if inputs is None else inputs,
        "materialize": {"file": {"env": "KUBECONFIG", "mode": mode}},
    }


def env_plan(script="auth/envpy.py", *, archive_info, inputs=None, names=None, non_secret=None):
    plan = {
        "connection": "prod-db",
        "integration": "authint@0.1.0",
        "authMethod": "login",
        "credentialType": "generic/env",
        "reference": {"kind": "k8s-secret", "ref": "k8s:secret@ns/db"},
        "method": {"script": {"path": script, "archive": archive_info}},
        "inputs": ENV_INPUTS if inputs is None else inputs,
        "materialize": {"env": names or ["PGUSER", "PGPASSWORD"]},
    }
    if non_secret:
        plan["materialize"]["nonSecret"] = non_secret
    return plan


PG_PASSWORD = "hunter2-db-password"
PG_NAMES = ["PGHOST", "PGDATABASE", "PGPASSWORD"]


def native_pg_plan(non_secret=None):
    """A native passthrough plan of a postgres-like env credential type (nothing to run)."""
    plan = {
        "connection": "prod-db",
        "integration": "postgres@1.0.0",
        "authMethod": "password",
        "credentialType": "postgres/connection",
        "reference": {"kind": "k8s-secret", "ref": "k8s:secret@ns/db"},
        "method": {"native": "passthrough"},
        "materialize": {"env": PG_NAMES},
    }
    if non_secret:
        plan["materialize"]["nonSecret"] = non_secret
    return plan


def native_pg(*, database="postgres", **extra):
    """The runner's materialized[] entry for native_pg_plan."""
    env = {"PGHOST": "db.prod.internal", "PGDATABASE": database, "PGPASSWORD": PG_PASSWORD}
    return {"credentialType": "postgres/connection", "env": env, **extra}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """`harness.run(plans, materialized=...)` runs a lease for the `auth-report` task of a
    copy of run-mini, whose credentials are `plans`' script auth plans, and returns its
    envelope. The task creates `harness.marker` when its script runs."""
    tree = load_dir(build_task_plugin(tmp_path / "plugin"))
    compiled = {t["name"]: t for t in compile_tree(tree)["tasks"]}["auth-report"]
    entries = [TreeEntry(p, d, p in tree.executable) for p, d in tree.files.items()]
    task_archive = archive.build(entries)
    cache = tmp_path / "archives"
    cache.mkdir()

    class Harness:
        marker = tmp_path / "task-ran"
        task_archive_bytes = task_archive
        creds = tmp_path / "creds"
        cache_dir = cache
        scope = tmp_path / "scope"

        def install(self, extra=None) -> dict:
            """The auth archive in the cache; its `{sha256, size}` for a plan."""
            data = integration_archive(extra)
            sha = archive_sha256(data)
            (cache / f"{sha.removeprefix('sha256:')}.tar").write_bytes(data)
            return {"sha256": sha, "size": len(data)}

        def lease(self, plans, names="") -> dict:
            sha = archive_sha256(task_archive)
            (cache / f"{sha.removeprefix('sha256:')}.tar").write_bytes(task_archive)
            return {
                "apiVersion": "runwhen.com/lease/v1",
                "leaseId": "lease-1",
                "taskRun": "run-1",
                "task": {
                    "ref": compiled["ref"],
                    "plugin": "runmini",
                    "version": "0.1.0",
                    "skill": compiled["skill"],
                    "name": compiled["name"],
                    "hash": compiled["hash"],
                    "script": compiled["script"],
                    "readOnly": compiled["readOnly"],
                    "dryRun": compiled["dryRun"],
                },
                "target": TARGET,
                "inputs": {"marker": str(self.marker), "names": names},
                "settings": {n: s["default"] for n, s in compiled["settings"].items()},
                "runtime": {
                    "name": "generic",
                    "major": 1,
                    "image": "i",
                    "digest": "sha256:" + "a" * 64,
                },
                "code": {"archive": {"sha256": sha, "size": len(task_archive)}},
                "credentials": [
                    {"type": p["credentialType"], "connection": p["connection"], "authPlan": p}
                    for p in plans
                ],
                "limits": {"timeout": "30s"},
                "outputs": compiled["outputs"],
            }

        def run(self, plans, *, materialized=(), names="", **kw) -> dict:
            envelope = run_lease(
                self.lease(plans, names),
                scope_dir=self.scope,
                materialized=list(materialized),
                secrets_root=self.creds,
                archive_cache=cache,
                **kw,
            )
            assert validate_contract("result-envelope", envelope) == []
            return envelope

        def assert_clean(self) -> None:
            """Nothing of the task's secrets is left: no task directory, no reference file."""
            assert not self.creds.exists() or list(self.creds.iterdir()) == []

    return Harness()


def only_error(envelope: dict) -> dict:
    assert envelope["status"] == "failed", envelope
    [error] = envelope["errors"]
    return error


# -- a file-type credential (the GKE shape) ----------------------------------------------------


def test_a_script_plan_writes_the_kubeconfig_the_task_reads(harness, monkeypatch):
    for name, value in HOST_ONLY.items():
        monkeypatch.setenv(name, value)
    info = harness.install()
    envelope = harness.run([file_plan(archive_info=info)], materialized=[REFERENCE])

    assert envelope["status"] == "ok", envelope["errors"]
    report = envelope["outputs"]["report"]
    kubeconfig = report["kubeconfig"]
    assert kubeconfig["mentionsProject"] is True  # the script read RW_AUTH_INPUT_PROJECT
    assert kubeconfig["keyReadable"] is True  # the reference key is still there for the task
    assert kubeconfig["keyHasMarker"] is True
    assert kubeconfig["mode"] == "0o600"
    assert kubeconfig["keyMode"] == "0o600"
    assert kubeconfig["keyDirMode"] == "0o700"
    assert kubeconfig["content"] == "***REDACTED***"  # the produced file is scrubbed
    assert report["authVars"] == []  # RW_AUTH_* belongs to the auth script only
    assert harness.marker.read_text() == "ran"
    text = json.dumps(envelope)
    # nothing the script printed is task output, and no secret got into the envelope
    for secret in (KEY_MARKER, TOKEN, "chatty", "keyfile:"):
        assert secret not in text
    harness.assert_clean()  # reference, output and the extracted archive are gone
    assert envelope["provenance"]["connection"] == "prod-east"


def test_the_materialize_mode_is_applied_to_the_produced_file(harness):
    envelope = harness.run(
        [file_plan(archive_info=harness.install(), mode="0640")], materialized=[REFERENCE]
    )
    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["outputs"]["report"]["kubeconfig"]["mode"] == "0o640"


def test_a_reference_file_may_be_base64(harness):
    entry = {
        "credentialType": "kubernetes/kubeconfig",
        "reference": {
            "files": [
                {"key": "key.json", "content": base64.b64encode(KEY_JSON.encode()).decode()}
                | {"encoding": "base64"},
                {"key": "token", "content": TOKEN},
            ]
        },
    }
    envelope = harness.run([file_plan(archive_info=harness.install())], materialized=[entry])
    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["outputs"]["report"]["kubeconfig"]["keyHasMarker"] is True


# -- an env-type credential ----------------------------------------------------------------------


def test_an_env_plan_exports_the_scripts_pairs_scrubbed(harness):
    envelope = harness.run(
        [env_plan(archive_info=harness.install())],
        materialized=[ENV_REFERENCE],
        names="PGUSER,PGPASSWORD",
    )
    assert envelope["status"] == "ok", envelope["errors"]
    report = envelope["outputs"]["report"]
    assert report["env"] == {"PGUSER": "***REDACTED***", "PGPASSWORD": "***REDACTED***"}
    assert report["authVars"] == []
    assert "hunter2-db-password" not in json.dumps(envelope)  # nor on stdout
    harness.assert_clean()


# -- non-secret names (contracts-v5 materialize.nonSecret) -----------------------------------------

PG_NON_SECRET = ["PGHOST", "PGDATABASE"]


def pg_env(envelope) -> dict:
    assert envelope["status"] == "ok", envelope["errors"]
    return envelope["outputs"]["report"]["env"]


def test_a_native_env_value_is_redacted_unless_its_name_is_non_secret(harness):
    names = ",".join(PG_NAMES)
    envelope = harness.run([native_pg_plan()], materialized=[native_pg()], names=names)
    assert pg_env(envelope) == dict.fromkeys(PG_NAMES, "***REDACTED***")  # as before the field

    envelope = harness.run([native_pg_plan(PG_NON_SECRET)], materialized=[native_pg()], names=names)
    assert pg_env(envelope) == {
        "PGHOST": "db.prod.internal",
        "PGDATABASE": "postgres",
        "PGPASSWORD": "***REDACTED***",
    }
    assert PG_PASSWORD not in json.dumps(envelope)


def test_non_secret_values_stay_readable_inside_other_text(harness):
    # "postgres" is the database name and part of the host: both survive when non-secret
    entry = native_pg()
    entry["env"]["PGHOST"] = "postgres.prod.internal"
    envelope = harness.run(
        [native_pg_plan(PG_NON_SECRET)], materialized=[entry], names=",".join(PG_NAMES)
    )
    assert pg_env(envelope)["PGHOST"] == "postgres.prod.internal"


def test_a_non_secret_value_equal_to_a_secret_one_stays_redacted(harness):
    names = ",".join(PG_NAMES)
    entry = native_pg(database=PG_PASSWORD)  # PGDATABASE is non-secret, but it is the password
    envelope = harness.run([native_pg_plan(PG_NON_SECRET)], materialized=[entry], names=names)
    assert pg_env(envelope) == {
        "PGHOST": "db.prod.internal",
        "PGDATABASE": "***REDACTED***",
        "PGPASSWORD": "***REDACTED***",
    }


def test_a_non_secret_value_equal_to_a_file_content_stays_redacted(harness):
    entry = native_pg(
        database="some-file-content", files=[{"env": "PGPASSFILE", "content": "some-file-content"}]
    )
    envelope = harness.run(
        [native_pg_plan(PG_NON_SECRET)], materialized=[entry], names=",".join(PG_NAMES)
    )
    assert pg_env(envelope)["PGDATABASE"] == "***REDACTED***"


def test_a_script_env_plan_redacts_only_the_names_not_marked_non_secret(harness):
    names = ",".join(PG_NAMES)
    plan = env_plan(
        "auth/pgenv.py",
        archive_info=harness.install(),
        inputs={"database": "postgres"},
        names=PG_NAMES,
        non_secret=PG_NON_SECRET,
    )
    envelope = harness.run([plan], materialized=[ENV_REFERENCE], names=names)
    assert pg_env(envelope) == {
        "PGHOST": "db.prod.internal",
        "PGDATABASE": "postgres",
        "PGPASSWORD": "***REDACTED***",
    }
    assert PG_PASSWORD not in json.dumps(envelope)
    harness.assert_clean()


def test_a_script_env_plan_without_non_secret_redacts_every_value(harness):
    plan = env_plan(
        "auth/pgenv.py",
        archive_info=harness.install(),
        inputs={"database": "postgres"},
        names=PG_NAMES,
    )
    envelope = harness.run([plan], materialized=[ENV_REFERENCE], names=",".join(PG_NAMES))
    assert pg_env(envelope) == dict.fromkeys(PG_NAMES, "***REDACTED***")


def test_a_script_non_secret_value_equal_to_the_reference_stays_redacted(harness):
    plan = env_plan(
        "auth/pgenv.py",
        archive_info=harness.install(),
        inputs={"database": PG_PASSWORD},
        names=PG_NAMES,
        non_secret=PG_NON_SECRET,
    )
    envelope = harness.run([plan], materialized=[ENV_REFERENCE], names=",".join(PG_NAMES))
    assert pg_env(envelope)["PGDATABASE"] == "***REDACTED***"
    assert pg_env(envelope)["PGHOST"] == "db.prod.internal"


# -- failures ------------------------------------------------------------------------------------


def failing_plan(harness, script="auth/fail.sh", **inputs):
    return file_plan(script, archive_info=harness.install(), inputs={**GKE_INPUTS, **inputs})


@pytest.mark.parametrize(
    "code", ["permission_denied", "not_found", "unreachable", "rate_limited", "timeout"]
)
def test_the_scripts_error_code_fails_the_task_before_its_script(harness, code):
    envelope = harness.run([failing_plan(harness, code=code)], materialized=[REFERENCE])
    error = only_error(envelope)
    assert (error["code"], error["scope"]) == (code, "auth")
    assert error["message"] == "denied: ***REDACTED***"  # the secret in its message is scrubbed
    assert not harness.marker.exists()  # the task script never ran
    assert envelope["outputs"] == {}
    harness.assert_clean()


def test_an_error_code_outside_the_closed_set_is_internal(harness):
    envelope = harness.run([failing_plan(harness, code="nope")], materialized=[REFERENCE])
    error = only_error(envelope)
    assert (error["code"], error["scope"]) == ("internal", "auth")
    assert "nope" in error["message"]
    assert TOKEN not in json.dumps(envelope)
    assert not harness.marker.exists()


def test_a_script_that_fails_silently_is_internal_with_a_redacted_tail(harness):
    envelope = harness.run([failing_plan(harness, "auth/crash.sh")], materialized=[REFERENCE])
    error = only_error(envelope)
    assert (error["code"], error["scope"]) == ("internal", "auth")
    assert "exited 2" in error["message"]
    assert "going down with ***REDACTED***" in error["message"]
    assert TOKEN not in json.dumps(envelope)
    assert not harness.marker.exists()
    harness.assert_clean()


@pytest.mark.parametrize("script", ["auth/nothing.sh"])
def test_a_script_that_exits_0_without_output_is_internal(harness, script):
    error = only_error(harness.run([failing_plan(harness, script)], materialized=[REFERENCE]))
    assert (error["code"], error["scope"]) == ("internal", "auth")
    assert "RW_AUTH_OUT_FILE" in error["message"]
    assert not harness.marker.exists()


def test_a_symlink_as_the_output_file_is_refused(harness):
    error = only_error(
        harness.run([failing_plan(harness, "auth/symlink.sh")], materialized=[REFERENCE])
    )
    assert error["code"] == "internal"
    assert not harness.marker.exists()


def test_a_script_that_runs_past_the_deadline_is_a_timeout_and_the_task_never_runs(harness):
    started = time.monotonic()
    envelope = harness.run(
        [failing_plan(harness, "auth/slow.sh")], materialized=[REFERENCE], deadline_s=2
    )
    error = only_error(envelope)
    assert (error["code"], error["scope"]) == ("timeout", "auth")
    assert time.monotonic() - started < 15
    assert not harness.marker.exists()
    harness.assert_clean()


@pytest.mark.parametrize(
    ("lines", "problem"),
    [
        ("PATH=/evil\n", "reserved"),
        ("HOME=/x\n", "reserved"),
        ("RW_AUTH_OUT_FILE=/x\n", "reserved"),
        ("RW_SYNC_CREDENTIAL=x\n", "reserved"),
        ("LD_PRELOAD=/x\n", "reserved"),
        ("PYTHONPATH=/x\n", "reserved"),
        ("OTHER=1\n", "does not list"),
        ("PGUSER=a\nPGUSER=b\n", "twice"),
        ("PGUSER\n", "not NAME=value"),
        ("lower=1\n", "not NAME=value"),
        ("\n\n", "no variable"),
    ],
)
def test_an_env_plan_cannot_export_a_reserved_or_unlisted_name(harness, lines, problem):
    plan = env_plan("auth/lines.py", archive_info=harness.install(), inputs={"lines": lines})
    envelope = harness.run([plan], materialized=[ENV_REFERENCE], names="PGUSER")
    error = only_error(envelope)
    assert (error["code"], error["scope"]) == ("internal", "auth")
    assert problem in error["message"]
    assert not harness.marker.exists()
    harness.assert_clean()


def test_a_file_plan_cannot_name_a_reserved_variable(harness):
    plan = file_plan(archive_info=harness.install())
    plan["materialize"] = {"file": {"env": "RW_OUT"}}
    error = only_error(harness.run([plan], materialized=[REFERENCE]))
    assert (error["code"], error["scope"]) == ("internal", "auth")
    assert "reserved" in error["message"]
    assert not harness.marker.exists()


def test_a_plan_whose_variable_is_already_set_is_refused(harness):
    already = {
        "credentialType": "aws/credentials",
        "files": [{"env": "KUBECONFIG", "content": "apiVersion: v1"}],
    }
    error = only_error(
        harness.run([file_plan(archive_info=harness.install())], materialized=[REFERENCE, already])
    )
    assert error["code"] == "internal"
    assert "KUBECONFIG" in error["message"]
    assert not harness.marker.exists()


# -- reference material --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["k8s-secret", "vault", "file"])
def test_a_secret_reference_without_material_is_invalid_input(harness, kind):
    plan = file_plan(archive_info=harness.install(), reference={"kind": kind, "ref": "x/y"})
    error = only_error(harness.run([plan]))
    assert error["code"] == "invalid_input"
    assert error["message"] == "no reference material for kubernetes/kubeconfig"
    assert not harness.marker.exists()


@pytest.mark.parametrize("kind", ["cloud-identity", "in-cluster-sa"])
def test_an_ambient_reference_runs_without_material(harness, tmp_path, kind):
    report = tmp_path / "report"
    plan = file_plan(
        "auth/probe.sh",
        archive_info=harness.install(),
        inputs={"report": str(report)},
        reference={"kind": kind},
    )
    envelope = harness.run([plan])
    assert envelope["status"] == "ok", envelope["errors"]
    text = report.read_text()
    assert "RW_AUTH_REF_DIR=" in text  # the directory exists, empty
    assert text.split("== ref\n")[1] == ""


@pytest.mark.parametrize(
    "bad",
    [
        {"credentialType": "x/y", "reference": {"files": []}},
        {"credentialType": "x/y", "reference": {}},
        {"credentialType": "x/y", "reference": "nope"},
        {"credentialType": "x/y", "reference": {"files": [{"key": "a", "content": "x"}], "x": 1}},
        {"credentialType": "x/y", "reference": {"files": [{"key": "../a", "content": "x"}]}},
        {"credentialType": "x/y", "reference": {"files": [{"key": "a/b", "content": "x"}]}},
        {"credentialType": "x/y", "reference": {"files": [{"key": "..", "content": "x"}]}},
        {"credentialType": "x/y", "reference": {"files": [{"key": "", "content": "x"}]}},
        {"credentialType": "x/y", "reference": {"files": [{"key": "a"}]}},
        {"credentialType": "x/y", "reference": {"files": [{"key": "a", "content": 1}]}},
        {
            "credentialType": "x/y",
            "reference": {"files": [{"key": "a", "content": "!!", "encoding": "base64"}]},
        },
        {
            "credentialType": "x/y",
            "reference": {"files": [{"key": "a", "content": "x", "encoding": "rot13"}]},
        },
        {
            "credentialType": "x/y",
            "reference": {"files": [{"key": "a", "content": "x"}, {"key": "a", "content": "y"}]},
        },
        {
            "credentialType": "x/y",
            "reference": {"files": [{"key": "a", "content": "x"}]},
            "files": [{"env": "A_B", "content": "x"}],
        },
        {
            "credentialType": "x/y",
            "reference": {"files": [{"key": "a", "content": "x"}]},
            "env": {"A_B": "1"},
        },
    ],
)
def test_a_malformed_reference_entry_fails_the_request(harness, bad):
    error = only_error(harness.run([file_plan(archive_info=harness.install())], materialized=[bad]))
    assert error["code"] == "invalid_input"
    assert error["message"].startswith("materialized[0]")
    assert not harness.marker.exists()


def test_two_references_for_one_credential_type_are_refused(harness):
    error = only_error(
        harness.run(
            [file_plan(archive_info=harness.install())], materialized=[REFERENCE, REFERENCE]
        )
    )
    assert error["code"] == "invalid_input"
    assert "second reference" in error["message"]


# -- the script, its archive and its path ---------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["../fake.sh", "auth/../../x.sh", "auth//fake.sh", "auth/./fake.sh"]
)
def test_a_script_path_that_leaves_integration_is_invalid_input(harness, path):
    error = only_error(
        harness.run([file_plan(path, archive_info=harness.install())], materialized=[REFERENCE])
    )
    assert (error["code"], error["scope"]) == ("invalid_input", "auth")
    assert "integration/" in error["message"]
    assert not harness.marker.exists()


@pytest.mark.parametrize(
    ("path", "problem"),
    [("auth/missing.sh", "has no integration/auth/missing.sh"), ("auth/fake.rb", "has no")],
)
def test_a_script_the_archive_does_not_have_is_internal(harness, path, problem):
    error = only_error(
        harness.run([file_plan(path, archive_info=harness.install())], materialized=[REFERENCE])
    )
    assert (error["code"], error["scope"]) == ("internal", "auth")
    assert problem in error["message"]


def test_an_unsupported_script_type_is_internal(harness):
    info = harness.install({"auth/tool.rb": "puts 1\n"})
    error = only_error(
        harness.run([file_plan("auth/tool.rb", archive_info=info)], materialized=[REFERENCE])
    )
    assert (error["code"], error["scope"]) == ("internal", "auth")
    assert "unsupported" in error["message"]


def test_a_cached_archive_that_fails_its_hash_is_internal_and_nothing_runs(harness):
    info = harness.install()
    path = harness.cache_dir / f"{info['sha256'].removeprefix('sha256:')}.tar"
    data = path.read_bytes()
    path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    error = only_error(harness.run([file_plan(archive_info=info)], materialized=[REFERENCE]))
    assert (error["code"], error["scope"]) == ("internal", "auth")
    assert info["sha256"] in error["message"]
    assert not harness.marker.exists()
    harness.assert_clean()


def test_an_auth_archive_that_is_not_cached_and_has_no_relay_is_a_dependency_failure(harness):
    data = integration_archive()
    info = {"sha256": archive_sha256(data), "size": len(data)}
    error = only_error(harness.run([file_plan(archive_info=info)], materialized=[REFERENCE]))
    assert (error["code"], error["scope"]) == ("dependency", "auth")
    assert not harness.marker.exists()


def test_the_auth_archive_is_fetched_by_hash_from_the_relay_and_cached(harness, relay_server):
    data = integration_archive()
    sha = archive_sha256(data)
    info = {"sha256": sha, "size": len(data)}
    relay_server.script(
        "GET", "/v1/tasks/req-1/code", Response(200, body=data, headers={"X-RW-Sha256": sha})
    )
    relay = RelayClient(relay_server.url, None, "req-1", requests.Session(), token="t0ken")
    envelope = harness.run([file_plan(archive_info=info)], materialized=[REFERENCE], relay=relay)
    assert envelope["status"] == "ok", envelope["errors"]
    [request] = [r for r in relay_server.requests if "/code" in r.path]
    assert request.path.split("?")[1] == f"sha256={sha.replace(':', '%3A')}"
    assert (harness.cache_dir / f"{sha.removeprefix('sha256:')}.tar").read_bytes() == data
    # cached: a second run asks the relay for nothing
    assert harness.run([file_plan(archive_info=info)], materialized=[REFERENCE])["status"] == "ok"
    assert len([r for r in relay_server.requests if "/code" in r.path]) == 1


def test_the_relay_sending_another_archive_is_refused(harness, relay_server):
    data = integration_archive()
    sha = archive_sha256(data)
    relay_server.script(
        "GET",
        "/v1/tasks/req-1/code",
        Response(200, body=data, headers={"X-RW-Sha256": "sha256:" + "0" * 64}),
    )
    relay = RelayClient(relay_server.url, None, "req-1", requests.Session(), token="t0ken")
    envelope = harness.run(
        [file_plan(archive_info={"sha256": sha, "size": len(data)})],
        materialized=[REFERENCE],
        relay=relay,
    )
    error = only_error(envelope)
    assert (error["code"], error["scope"]) == ("dependency", "auth")
    assert not harness.marker.exists()


# -- what the script sees -------------------------------------------------------------------------


def test_the_script_sees_its_variables_and_none_of_the_hosts_secrets(
    harness, tmp_path, monkeypatch
):
    for name, value in {
        **HOST_ONLY,
        "RW_CREDENTIALS_DIR": "/run/rw/credentials",
        "RW_ARCHIVE_CACHE": "/work/.archives",
        "XDG_CONFIG_HOME": "/shared/xdg",
        "BASH_ENV": "/host/bash_env",
    }.items():
        monkeypatch.setenv(name, value)
    report = tmp_path / "report"
    inputs = {
        "report": str(report),
        "gkeLocation": "us-east1",
        "zones": ["a", "b"],
        "private": True,
        "replicas": 3,
    }
    envelope = harness.run(
        [file_plan("auth/probe.sh", archive_info=harness.install(), inputs=inputs)],
        materialized=[REFERENCE],
    )
    assert envelope["status"] == "ok", envelope["errors"]
    sections = report.read_text().split("== ")
    env_lines = dict(
        line.split("=", 1)
        for line in next(s for s in sections if s.startswith("env\n")).splitlines()[1:]
        if "=" in line
    )
    for hidden in (
        *HOST_ONLY,
        "RW_CREDENTIALS_DIR",
        "RW_ARCHIVE_CACHE",
        "XDG_CONFIG_HOME",
        "BASH_ENV",
        "RW_OUTPUT_FD",
        "RW_ERRORS_FILE",
    ):
        assert hidden not in env_lines, hidden
    assert env_lines["RW_AUTH_INPUT_GKE_LOCATION"] == "us-east1"
    assert env_lines["RW_AUTH_INPUT_ZONES"] == '["a", "b"]'
    assert env_lines["RW_AUTH_INPUT_PRIVATE"] == "true"
    assert env_lines["RW_AUTH_INPUT_REPLICAS"] == "3"
    assert "RW_AUTH_OUT_FILE" in env_lines and "RW_AUTH_OUT_ERROR" in env_lines
    assert "RW_AUTH_OUT_ENV" not in env_lines  # a file type is only given its file
    ref_dir = Path(env_lines["RW_AUTH_REF_DIR"])
    assert not ref_dir.exists()  # deleted with the task
    # the working directory is the plugin root, and integration/ is under it
    listed = next(s for s in sections if s.startswith("integration\n")).split()[1:]
    assert sorted(listed) == sorted(Path(p).name for p in SCRIPTS if p.startswith("auth/"))
    assert sorted(next(s for s in sections if s.startswith("ref\n")).split()[1:]) == [
        "key.json",
        "token",
    ]
    assert env_lines["HOME"] != os.environ["HOME"]
    assert Path(env_lines["HOME"]).parts[: len(harness.creds.parts)] == harness.creds.parts


def test_an_env_plan_is_given_rw_auth_out_env_and_not_the_file(harness, tmp_path):
    plan = env_plan("auth/lines.py", archive_info=harness.install(), inputs={"lines": "PGUSER=a\n"})
    envelope = harness.run([plan], materialized=[ENV_REFERENCE], names="PGUSER")
    assert envelope["status"] == "ok", envelope["errors"]


# -- through rwtask serve ------------------------------------------------------------------------


def test_served_end_to_end_both_archives_come_from_the_relay(
    harness, relay_server, tmp_path, monkeypatch
):
    """The poll response's `materialized[].reference` reaches the script; the task's archive
    and the auth archive are each fetched from the relay (the second by hash)."""
    data = integration_archive()
    sha = archive_sha256(data)
    lease = harness.lease([file_plan(archive_info={"sha256": sha, "size": len(data)})])
    for cached in harness.cache_dir.iterdir():
        cached.unlink()  # the lease() call cached the task's archive
    monkeypatch.setenv("RW_ARCHIVE_CACHE", str(harness.cache_dir))
    monkeypatch.setenv("RW_CREDENTIALS_DIR", str(harness.creds))
    token = tmp_path / "token"
    token.write_text("test-token")
    task_data = harness.task_archive_bytes
    relay_server.script(
        "POST",
        "/v1/tasks/next",
        Response(
            200,
            json={
                "requestId": "req-1",
                "request": lease,
                "credentials": {},
                "materialized": [REFERENCE],
                "scopeId": "scope-1",
                "deadlineMs": 30000,
            },
        ),
    )
    relay_server.script(
        "GET",
        "/v1/tasks/req-1/code",
        Response(200, body=task_data, headers={"X-RW-Sha256": archive_sha256(task_data)}),
        Response(200, body=data, headers={"X-RW-Sha256": sha}),
    )
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
    body = json.loads(result.body)
    assert body["status"] == "ok", body
    envelope = body["result"]
    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["outputs"]["report"]["kubeconfig"]["mentionsProject"] is True
    assert [r.path.partition("?")[2] for r in relay_server.requests if "/code" in r.path] == [
        "",
        f"sha256={sha.replace(':', '%3A')}",
    ]
    harness.assert_clean()
    assert not (tmp_path / "work" / "scope-1").exists()
