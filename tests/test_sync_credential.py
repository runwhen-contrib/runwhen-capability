"""The platform-minted `runwhen/sync` credential reaches a task as the JSON file at
$RW_SYNC_CREDENTIAL.

On a runner it arrives as an ordinary `materialized[]` entry the runner resolved from the
lease's `{type: runwhen/sync, minted: {ref}}`: `files: [{env: RW_SYNC_CREDENTIAL, ...}]`. That
one name is the only RW_* variable a credential may set, and only as a file of a `runwhen/sync`
entry (plugin/run.py PLATFORM_MINTED_FILE_ENV); everywhere else the RW_* names stay reserved.
Locally (`rwtask run`, `rwtask test --record`) there is no runner, so the caller's own
$RW_SYNC_CREDENTIAL is passed through to a task that declares `runwhen/sync`.
"""

from __future__ import annotations

import json
import os

import pytest
from sync_plugin import SYNC_DOC, SYNC_TOKEN, build_sync_plugin

from runwhen_capability.cli import main
from runwhen_capability.contracts import TreeEntry, archive, archive_sha256, load_yaml
from runwhen_capability.contracts import validate as validate_contract
from runwhen_capability.plugin import compile_tree, load_dir
from runwhen_capability.plugin.lease import run_lease
from runwhen_capability.plugin.run import (
    PLATFORM_MINTED_FILE_ENV,
    RunSpec,
    check_extra_env,
    execute,
    resolve_target,
)

DEPLOYMENT = "kubernetes/clusters/kind/namespaces/runwhen/deployments/runwhen-papi"
TARGET = {
    "path": DEPLOYMENT,
    "type": "kubernetes/deployment",
    "identity": {"cluster": "kind", "namespace": "runwhen", "name": "runwhen-papi"},
}
SYNC_JSON = json.dumps(SYNC_DOC)
HOST_ONLY = {
    "RELAY_URL": "http://relay.invalid",
    "POOL_ID": "pool-9",
    "EXECUTOR_TOKEN_FILE": "/var/run/executor/token",
    "RW_EXECUTOR_TOKEN_FILE": "/run/rw/executor/token",
}


def sync_entry(env="RW_SYNC_CREDENTIAL", credential_type="runwhen/sync", **extra) -> dict:
    return {
        "credentialType": credential_type,
        "files": [{"env": env, "mode": "0600", "content": SYNC_JSON}],
        **extra,
    }


# -- on a runner: a lease with a materialized runwhen/sync entry ------------------------------


@pytest.fixture
def lease_for(tmp_path):
    """`run(task, materialized)` runs a lease for `task` ("py-sync" | "sh-sync") of a copy of
    the run-mini plugin and returns its envelope; `run.creds` is the credentials directory."""

    def build(task: str) -> dict:
        tree = load_dir(build_sync_plugin(tmp_path / f"plugin-{task}", task))
        compiled = {t["name"]: t for t in compile_tree(tree)["tasks"]}[task]
        entries = [TreeEntry(p, d, p in tree.executable) for p, d in tree.files.items()]
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
            "inputs": {},
            "settings": {n: s["default"] for n, s in compiled["settings"].items()},
            "runtime": {
                "name": "generic",
                "major": 1,
                "image": "i",
                "digest": "sha256:" + "a" * 64,
            },
            "code": {
                "inline": {
                    "sha256": archive_sha256(archive.build(entries)),
                    "files": [
                        {"path": e.path, "content": e.data.decode(), "executable": e.executable}
                        for e in entries
                    ],
                }
            },
            "credentials": [
                {"type": "runwhen/sync", "minted": {"ref": "sync/lease_1", "ttlSeconds": 900}}
            ],
            "limits": {"timeout": "30s"},
            "outputs": compiled["outputs"],
        }

    def run(task, materialized):
        creds = tmp_path / "creds"
        envelope = run_lease(
            build(task),
            scope_dir=tmp_path / f"scope-{task}",
            materialized=materialized,
            secrets_root=creds,
            archive_cache=tmp_path / "archives",
        )
        assert validate_contract("result-envelope", envelope) == []
        run.creds = creds
        return envelope

    return run


@pytest.mark.parametrize("task", ["py-sync", "sh-sync"])
def test_a_lease_task_reads_the_minted_sync_credential_and_the_token_is_scrubbed(
    lease_for, monkeypatch, task
):
    for name, value in HOST_ONLY.items():
        monkeypatch.setenv(name, value)

    envelope = lease_for(task, [sync_entry()])

    assert envelope["status"] == "ok", envelope["errors"]
    seen = envelope["outputs"]["sync"]
    assert seen["apiBaseUrl"] == SYNC_DOC["apiBaseUrl"]  # the JSON was read and parsed
    assert seen["workspace"] == SYNC_DOC["workspace"]
    assert seen["mode"] in ("0o600", "0600")
    assert seen["hostOnly"] == 0  # the host-only names are still stripped
    assert SYNC_TOKEN not in json.dumps(envelope)  # ... and the token is scrubbed
    assert seen["token"] != SYNC_TOKEN
    assert not lease_for.creds.exists() or list(lease_for.creds.iterdir()) == []  # file deleted


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(sync_entry(credential_type="generic/env"), id="other-credential-type"),
        pytest.param(sync_entry(credential_type="aws/credentials"), id="other-type-2"),
        pytest.param(
            {"credentialType": "runwhen/sync", "env": {"RW_SYNC_CREDENTIAL": SYNC_TOKEN}},
            id="as-an-env-key",
        ),
        pytest.param(
            {"credentialType": "generic/env", "env": {"RW_SYNC_CREDENTIAL": SYNC_TOKEN}},
            id="as-an-env-key-of-another-type",
        ),
        pytest.param(sync_entry(env="RW_FOO"), id="another-RW-name-under-runwhen-sync"),
        pytest.param(sync_entry(env="RW_SDK"), id="a-reserved-name-under-runwhen-sync"),
        pytest.param(sync_entry(env="RELAY_URL"), id="host-only-under-runwhen-sync"),
        pytest.param(sync_entry(env="EXECUTOR_TOKEN_FILE"), id="legacy-token-file-name"),
        pytest.param(sync_entry(env="RW_EXECUTOR_TOKEN_FILE"), id="rw-token-file-name"),
    ],
)
def test_the_sync_name_is_allowed_nowhere_else_and_no_other_rw_name_is_allowed_at_all(
    lease_for, entry
):
    envelope = lease_for("py-sync", [entry])

    assert envelope["status"] == "failed"
    [error] = envelope["errors"]
    assert error["code"] == "invalid_input"
    assert error["message"].startswith("materialized[0]")
    assert SYNC_TOKEN not in json.dumps(envelope)
    assert not lease_for.creds.exists() or list(lease_for.creds.iterdir()) == []


def test_the_sync_file_must_not_be_set_twice(lease_for):
    envelope = lease_for("py-sync", [sync_entry(), sync_entry()])
    assert [e["code"] for e in envelope["errors"]] == ["invalid_input"]


# -- the allow set is narrow: check_extra_env / execute -----------------------------------------


def test_the_minted_names_are_exactly_one():
    assert PLATFORM_MINTED_FILE_ENV == {"runwhen/sync": "RW_SYNC_CREDENTIAL"}


def test_check_extra_env_refuses_the_sync_name_unless_it_is_explicitly_allowed():
    with pytest.raises(ValueError, match="extra_env may not set"):
        check_extra_env({"RW_SYNC_CREDENTIAL": "/x"})
    assert check_extra_env({"RW_SYNC_CREDENTIAL": "/x"}, allow={"RW_SYNC_CREDENTIAL"})
    with pytest.raises(ValueError, match="extra_env may not set"):
        check_extra_env({"RW_FOO": "/x"}, allow={"RW_SYNC_CREDENTIAL"})


def test_a_caller_cannot_pass_the_sync_name_as_extra_env(tmp_path):
    spec, _ = _spec(tmp_path, "py-sync")
    with pytest.raises(ValueError, match="extra_env may not set"):
        execute(spec, scope_dir=tmp_path / "scope", extra_env={"RW_SYNC_CREDENTIAL": "/x"})


# -- locally: rwtask run / rwtask test --record pass the caller's own credential -----------------


def _spec(tmp_path, task: str):
    tree = load_dir(build_sync_plugin(tmp_path / f"local-{task}", task))
    tasks = {t["name"]: t for t in compile_tree(tree)["tasks"]}
    target = resolve_target(DEPLOYMENT, tree, identity_overrides={})
    target.pop("warnings", None)
    spec = RunSpec(
        task=tasks[task],
        files=tree.files,
        executable=tree.executable,
        target=target,
        inputs={},
        settings={},
    )
    return spec, tasks


@pytest.mark.parametrize("task", ["py-sync", "sh-sync"])
def test_a_local_run_passes_the_callers_sync_credential_to_a_task_that_declares_it(
    tmp_path, monkeypatch, task
):
    cred = tmp_path / "sync.json"
    cred.write_text(SYNC_JSON)
    monkeypatch.setenv("RW_SYNC_CREDENTIAL", str(cred))
    spec, _ = _spec(tmp_path, task)

    envelope = execute(spec, scope_dir=tmp_path / "scope", inherit_home=True).envelope

    assert envelope["status"] == "ok", envelope["errors"]
    seen = envelope["outputs"]["sync"]
    assert seen["path"] == str(cred)
    assert seen["apiBaseUrl"] == SYNC_DOC["apiBaseUrl"]
    assert SYNC_TOKEN not in json.dumps(envelope)  # the file's token is scrubbed locally too


def test_a_relative_sync_path_is_made_absolute_against_the_callers_directory(tmp_path, monkeypatch):
    (tmp_path / "creds").mkdir()
    (tmp_path / "creds" / "sync.json").write_text(SYNC_JSON)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RW_SYNC_CREDENTIAL", "creds/sync.json")
    spec, _ = _spec(tmp_path, "py-sync")

    envelope = execute(spec, scope_dir=tmp_path / "scope", inherit_home=True).envelope

    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["outputs"]["sync"]["path"] == str(tmp_path / "creds" / "sync.json")
    assert envelope["outputs"]["sync"]["apiBaseUrl"] == SYNC_DOC["apiBaseUrl"]


def test_a_local_run_does_not_pass_it_to_a_task_that_does_not_declare_it(tmp_path, monkeypatch):
    cred = tmp_path / "sync.json"
    cred.write_text(SYNC_JSON)
    monkeypatch.setenv("RW_SYNC_CREDENTIAL", str(cred))
    spec, tasks = _spec(tmp_path, "py-sync")
    spec.task = tasks["py-sync-undeclared"]  # the same script, declares kubeconfig only

    envelope = execute(spec, scope_dir=tmp_path / "scope", inherit_home=True).envelope

    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["outputs"]["sync"]["path"] is None


def test_a_replay_without_inherit_home_does_not_pass_it(tmp_path, monkeypatch):
    """`rwtask test` without --record replays from shims and never passes the credential."""
    cred = tmp_path / "sync.json"
    cred.write_text(SYNC_JSON)
    monkeypatch.setenv("RW_SYNC_CREDENTIAL", str(cred))
    spec, _ = _spec(tmp_path, "py-sync")

    envelope = execute(spec, scope_dir=tmp_path / "scope").envelope

    assert envelope["outputs"]["sync"]["path"] is None


def test_a_declared_task_with_no_credential_in_the_callers_environment_just_sees_none(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("RW_SYNC_CREDENTIAL", raising=False)
    spec, _ = _spec(tmp_path, "py-sync")

    envelope = execute(spec, scope_dir=tmp_path / "scope", inherit_home=True).envelope

    assert envelope["outputs"]["sync"]["path"] is None


def test_rwtask_run_passes_it_to_a_declared_task(tmp_path, monkeypatch, capsys):
    plugin = build_sync_plugin(tmp_path / "plugin", "py-sync")
    (tmp_path / "sync.json").write_text(SYNC_JSON)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RW_SYNC_CREDENTIAL", "sync.json")  # relative to where rwtask is run

    code = main(
        ["run", "probe/py-sync", "--plugin-dir", str(plugin), "--resource", DEPLOYMENT]
        + ["--out", str(tmp_path / "out")]
    )

    assert code == 0, capsys.readouterr().err
    text = (tmp_path / "out" / "result.yaml").read_text()
    assert load_yaml(text)["outputs"]["sync"]["path"] == str(tmp_path / "sync.json")
    assert SYNC_TOKEN not in text


def test_the_other_rw_variables_of_the_callers_environment_are_still_stripped(
    tmp_path, monkeypatch
):
    cred = tmp_path / "sync.json"
    cred.write_text(SYNC_JSON)
    monkeypatch.setenv("RW_SYNC_CREDENTIAL", str(cred))
    monkeypatch.setenv("RW_FOO", "x")
    for name, value in HOST_ONLY.items():
        monkeypatch.setenv(name, value)
    spec, tasks = _spec(tmp_path, "py-sync")
    spec.task = tasks["py-sync-undeclared"]

    envelope = execute(spec, scope_dir=tmp_path / "scope", inherit_home=True).envelope

    assert envelope["outputs"]["sync"] == {"path": None, "hostOnly": 0}
    assert os.environ["RW_FOO"] == "x"


def test_rwtask_test_record_passes_it_and_a_replay_does_not(tmp_path, monkeypatch, capsys):
    import runwhen_capability.plugin.run as run_mod

    plugin = build_sync_plugin(tmp_path / "plugin", "py-sync")
    (tmp_path / "sync.json").write_text(SYNC_JSON)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RW_SYNC_CREDENTIAL", "sync.json")
    seen = []
    real = run_mod.execute

    def spy(spec, **kwargs):
        outcome = real(spec, **kwargs)
        seen.append(outcome.envelope["outputs"]["sync"]["path"])
        return outcome

    monkeypatch.setattr(run_mod, "execute", spy)

    record = ["test", "probe/py-sync", "--plugin-dir", str(plugin), "--record"]
    code = main([*record, "--resource", DEPLOYMENT, "--scenario", "s"])
    assert code == 0, capsys.readouterr().err
    scenario = plugin / "skills" / "probe" / "tests" / "py-sync" / "s.yaml"
    assert SYNC_TOKEN not in scenario.read_text()

    code = main(["test", "probe/py-sync", "--plugin-dir", str(plugin)])
    assert code == 0, capsys.readouterr().err
    assert seen == [str(tmp_path / "sync.json"), None]  # recorded with it, replayed without
