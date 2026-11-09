"""`rwtask run <skill>/<task> --resource PATH` (D43) against the run-mini fixture plugin."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from runwhen_capability.cli import main
from runwhen_capability.contracts import load_yaml
from runwhen_capability.contracts import validate as validate_contract

HERE = Path(__file__).resolve().parent
RUN_MINI = HERE / "fixtures" / "run-mini"
DEPLOYMENT = "kubernetes/clusters/kind/namespaces/runwhen/deployments/runwhen-papi"


@pytest.fixture
def rwtask(tmp_path, capsys):
    out = tmp_path / "out"

    def go(task, *args, resource=DEPLOYMENT, plugin_dir=RUN_MINI, out_dir=out, expect=None):
        argv = ["run", task, "--plugin-dir", str(plugin_dir), "--out", str(out_dir)]
        if resource is not None:
            argv += ["--resource", resource]
        code = main([*argv, *args])
        captured = capsys.readouterr()
        result = out_dir / "result.yaml"
        envelope = None
        if result.is_file():
            envelope = load_yaml(result.read_text())
            assert validate_contract("result-envelope", envelope) == []
        if expect is not None:
            assert code == expect, captured.err
        return code, envelope, captured.err

    go.out = out
    return go


def test_a_successful_run_writes_result_yaml_and_exits_0(rwtask):
    code, envelope, err = rwtask(
        "probe/py-ok", "--input", "since=1h", "--input", "limit=7", "--setting", 'hosts=["a"]'
    )
    assert code == 0
    assert envelope["status"] == "ok"
    summary = envelope["outputs"]["summary"]
    assert summary["since"] == "1h"
    assert summary["limit"] == 7  # coerced from "7" by the declared type
    assert summary["hosts"] == ["a"]
    assert summary["threshold"] == 1500
    assert summary["name"] == "runwhen-papi"
    assert "probe/py-ok: ok" in err


def test_the_envelope_carries_provenance_from_the_compiled_task(rwtask):
    from runwhen_capability.plugin import compile_tree, load_dir

    compiled = compile_tree(load_dir(RUN_MINI))
    task = next(t for t in compiled["tasks"] if t["ref"].endswith(":probe/py-ok"))
    for name, expect in (("probe/py-ok", 0), ("probe/py-raise", 1)):
        _, envelope, _ = rwtask(name, expect=expect)
        provenance = envelope["provenance"]
        assert provenance["task"].endswith(f":{name}")
        assert provenance["plugin"] == compiled["plugin"]["name"]
        assert provenance["version"] == compiled["plugin"]["version"]
        assert provenance["taskHash"].startswith("sha256:")
    _, envelope, _ = rwtask("probe/py-ok", expect=0)
    assert envelope["provenance"] == {
        "task": task["ref"],
        "plugin": compiled["plugin"]["name"],
        "version": compiled["plugin"]["version"],
        "taskHash": task["hash"],
    }


def test_a_string_input_stays_raw_even_if_it_looks_like_json(rwtask):
    _, envelope, _ = rwtask("probe/sh-ok", "--input", "note=5", expect=0)
    assert envelope["outputs"]["probe"][0]["note"] == "5"


def test_a_value_that_is_not_json_for_its_type_is_refused_before_running(rwtask):
    code, envelope, err = rwtask("probe/py-ok", "--input", "limit=many")
    assert code == 2
    assert envelope is None
    assert "--input limit: expected JSON for type integer" in err


def test_a_malformed_key_value_is_a_usage_error(rwtask):
    with pytest.raises(SystemExit) as exc:
        rwtask("probe/py-ok", "--input", "nokey")
    assert exc.value.code == 2


def test_a_failed_run_exits_1_names_the_codes_and_shows_the_script_output(rwtask):
    code, envelope, err = rwtask("probe/sh-exit3")
    assert code == 1
    assert envelope["status"] == "failed"
    assert "[internal]" in err
    assert "boom: could not frobnicate" in err


def test_partial_and_skipped_runs_exit_0(rwtask):
    code, envelope, err = rwtask("probe/py-partial")
    assert (code, envelope["status"]) == (0, "partial")
    assert "[unreachable]" in err
    code, envelope, err = rwtask("probe/py-skip")
    assert (code, envelope["status"]) == (0, "skipped")
    assert "nothing to check" in err


def test_a_missing_required_input_fails_the_run(rwtask):
    code, envelope, _ = rwtask("probe/py-need")
    assert code == 1
    assert envelope["errors"][0]["code"] == "invalid_input"


def test_a_write_task_is_refused_without_allow_write(rwtask):
    code, envelope, err = rwtask("probe/sh-write-no-dry")
    assert code == 2
    assert envelope is None
    assert "--allow-write" in err
    assert "Pretends to restart something" in err
    code, envelope, _ = rwtask("probe/sh-write-no-dry", "--allow-write")
    assert (code, envelope["status"]) == (0, "ok")
    assert envelope["outputs"]["result"] == {"dry": False}


def test_dry_run_is_refused_unless_the_task_declares_it(rwtask):
    code, envelope, err = rwtask("probe/py-nothing", "--dry-run")
    assert code == 2
    assert envelope is None
    assert "dryRun: true" in err
    # a write task that is not dryRun is refused for writing first
    code, _, err = rwtask("probe/sh-write-no-dry", "--dry-run")
    assert code == 2
    assert "--allow-write" in err


def test_dry_run_of_a_dryrun_task_needs_no_allow_write(rwtask):
    code, envelope, _ = rwtask("probe/sh-write", "--dry-run")
    assert code == 0
    assert envelope["outputs"]["result"] == {"dry": True}
    assert envelope["dryRun"] is True
    code, envelope, _ = rwtask("probe/sh-write")
    assert code == 2  # writing for real still needs --allow-write


def test_artifacts_land_next_to_result_yaml_and_stale_ones_go(rwtask):
    stale = rwtask.out / "dump"
    stale.parent.mkdir(parents=True)
    stale.write_text("from an earlier run")
    code, envelope, _ = rwtask("probe/py-artifact")
    assert code == 0
    assert envelope["outputs"]["dump"] == {"artifact": "art_local_dump"}
    assert (rwtask.out / "dump").read_text() == "a dump\n"
    assert not (rwtask.out / "scratch.txt").exists()
    # a declared artifact the task did not write this time must not linger from before
    (rwtask.out / "declaredButNeverWritten").write_text("stale")
    rwtask("probe/py-artifact", expect=0)
    assert not (rwtask.out / "declaredButNeverWritten").exists()


def test_identity_overrides_and_foreign_paths_warn(rwtask):
    _, envelope, err = rwtask(
        "probe/py-ok",
        "--identity",
        "namespace=other",
        resource="github/orgs/acme/repos/web",
        expect=0,
    )
    assert envelope["outputs"]["summary"]["namespace"] == "other"
    assert "warning: platform 'github' is not this plugin's" in err
    assert "applies to kubernetes/deployment, not github/repo" in err


def test_a_resource_path_that_is_not_one_exits_2(rwtask):
    code, envelope, err = rwtask("probe/py-ok", resource="kubernetes/clusters")
    assert code == 2
    assert envelope is None
    assert "not a resource path" in err


def test_a_task_that_applies_to_a_type_needs_a_resource(rwtask):
    code, _, err = rwtask("probe/py-ok", resource=None)
    assert code == 2
    assert "--resource" in err


def test_an_unknown_task_lists_the_plugins_tasks(rwtask):
    code, _, err = rwtask("probe/nope")
    assert code == 2
    assert "no task 'probe/nope'" in err
    assert "probe/py-ok" in err


def test_a_plugin_prefix_is_accepted(rwtask):
    assert rwtask("runmini:probe/py-ok")[0] == 0


def test_an_invalid_plugin_prints_diagnostics_and_exits_1(rwtask, tmp_path):
    broken = tmp_path / "broken"
    shutil.copytree(RUN_MINI, broken)
    (broken / "skills" / "probe" / "scripts" / "py_ok.py").unlink()
    code, envelope, err = rwtask("probe/py-ok", plugin_dir=broken)
    assert code == 1
    assert envelope is None
    assert "py_ok.py" in err


def test_a_directory_without_plugin_yaml_exits_1(rwtask, tmp_path):
    code, _, err = rwtask("probe/py-ok", plugin_dir=tmp_path)
    assert code == 1
    assert "has no plugin.yaml" in err


def test_defaults_are_the_current_directory_and_out(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(RUN_MINI)
    target = tmp_path / "elsewhere"
    code = main(["run", "probe/py-ok", "--resource", DEPLOYMENT, "--out", str(target)])
    assert code == 0, capsys.readouterr().err
    scratch = tmp_path / "cwd"
    scratch.mkdir()
    shutil.copytree(RUN_MINI, scratch / "plugin")
    monkeypatch.chdir(scratch / "plugin")
    assert main(["run", "probe/py-ok", "--resource", DEPLOYMENT]) == 0
    assert (scratch / "plugin" / "out" / "result.yaml").is_file()


def test_the_scope_is_removed_unless_kept(rwtask, tmp_path):
    _, _, err = rwtask("probe/py-nothing", "--workdir", str(tmp_path / "scope"))
    assert (tmp_path / "scope" / "plugin").is_dir()  # a given workdir is the caller's own
    assert "scope kept at" in err


def test_the_callers_home_and_environment_are_the_credential_but_host_secrets_are_not(
    rwtask, monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path / "me"))
    monkeypatch.setenv("RELAY_URL", "http://relay.invalid")
    _, envelope, _ = rwtask("probe/sh-env", expect=0)
    seen = envelope["outputs"]["env"]
    assert seen["home"] == str(tmp_path / "me")  # ~/.kube, ~/.aws are where the caller's are
    assert seen["relay"] == "unset"
    assert seen["contract"] == "v13"


# -- the classic forms are untouched -------------------------------------------------------


def test_an_existing_directory_with_a_slash_is_still_a_classic_capability_dir(tmp_path, capsys):
    classic = tmp_path / "a" / "b"
    classic.mkdir(parents=True)
    with pytest.raises(SystemExit) as exc:
        main(["run", str(classic)])
    assert exc.value.code == 2
    assert "run needs capability_dir and --request" in capsys.readouterr().err


def test_resource_needs_a_skill_and_task(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["run", "justaskill", "--resource", DEPLOYMENT])
    assert exc.value.code == 2
    assert "<skill>/<task>" in capsys.readouterr().err


def test_v13_flags_refuse_the_classic_ones(rwtask):
    with pytest.raises(SystemExit) as exc:
        rwtask("probe/py-ok", "--request", "r.json")
    assert exc.value.code == 2


# -- relative credential paths (lane F): the child runs in its scope, not the caller's cwd ------


@pytest.fixture
def cred_env(monkeypatch, tmp_path):
    cwd = tmp_path / "caller"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    for name in ("KUBECONFIG", "PGPASSFILE", "SSL_CERT_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return cwd


def paths_of(rwtask):
    _, envelope, _ = rwtask("probe/sh-paths", expect=0)
    assert envelope["status"] == "ok", envelope
    return envelope["outputs"]["paths"]


def test_a_relative_kubeconfig_reaches_the_task_absolute(rwtask, cred_env, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "../x.kubeconfig")
    monkeypatch.setenv("PGPASSFILE", ".pgpass")
    paths = paths_of(rwtask)
    assert paths["kubeconfig"] == str(cred_env / ".." / "x.kubeconfig")
    assert paths["pgpassfile"] == str(cred_env / ".pgpass")
    assert paths["sslCertFile"] == "unset"  # not set: not invented


def test_absolute_and_home_relative_values_are_left_as_they_are_or_expanded(
    rwtask, cred_env, monkeypatch, tmp_path
):
    monkeypatch.setenv("KUBECONFIG", "/etc/kube/config")
    monkeypatch.setenv("PGPASSFILE", "~/.pgpass")
    monkeypatch.setenv("SSL_CERT_FILE", "")
    paths = paths_of(rwtask)
    assert paths["kubeconfig"] == "/etc/kube/config"
    assert paths["pgpassfile"] == str(tmp_path / "home" / ".pgpass")  # the REAL home
    assert paths["sslCertFile"] == ""


def test_every_entry_of_a_kubeconfig_list_is_resolved(rwtask, cred_env, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", f"a.yaml:/abs/b.yaml::~/c.yaml:{cred_env}/d.yaml")
    home = os.environ["HOME"]
    assert paths_of(rwtask)["kubeconfig"] == os.pathsep.join(
        [str(cred_env / "a.yaml"), "/abs/b.yaml", "", f"{home}/c.yaml", f"{cred_env}/d.yaml"]
    )


def test_the_credential_path_variables_are_one_documented_constant():
    from runwhen_capability.plugin.run import CREDENTIAL_PATH_VARS

    assert CREDENTIAL_PATH_VARS == (
        "KUBECONFIG",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "CLOUDSDK_CONFIG",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "AZURE_CONFIG_DIR",
        "PGPASSFILE",
        "PGSSLROOTCERT",
        "PGSSLCERT",
        "PGSSLKEY",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
    )
