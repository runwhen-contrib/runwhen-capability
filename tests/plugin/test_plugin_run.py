"""plugin/run.py: resolve_target, and execute() against the run-mini fixture plugin.
Every envelope is checked against the result-envelope contract."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from runwhen_capability.contracts import validate as validate_contract
from runwhen_capability.plugin import compile_tree, load_dir
from runwhen_capability.plugin.run import RunSpec, coerce_value, execute, resolve_target

HERE = Path(__file__).resolve().parent
RUN_MINI = HERE / "fixtures" / "run-mini"
KUBE_MINI = HERE / "fixtures" / "kube-mini"
BASH_MAJOR = int(
    subprocess.run(
        ["bash", "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True, check=True
    ).stdout
)
DEPLOYMENT = "kubernetes/clusters/kind/namespaces/runwhen/deployments/runwhen-papi"


@pytest.fixture(scope="module")
def plugin():
    tree = load_dir(RUN_MINI)
    compiled = compile_tree(tree)
    return tree, {t["name"]: t for t in compiled["tasks"]}


@pytest.fixture
def run(plugin, tmp_path):
    tree, tasks = plugin

    def go(name, *, resource=DEPLOYMENT, inputs=None, settings=None, **kw):
        target = (
            resolve_target(resource, tree, identity_overrides={}) if resource is not None else None
        )
        if target is not None:
            target.pop("warnings", None)
        spec = RunSpec(
            task=tasks[name],
            files=tree.files,
            executable=tree.executable,
            target=target,
            inputs=inputs or {},
            settings=settings or {},
            dry_run=kw.pop("dry_run", False),
            timeout_s=kw.pop("timeout_s", None),
        )
        outcome = execute(spec, scope_dir=tmp_path / "scope", **kw)
        assert validate_contract("result-envelope", outcome.envelope) == []
        return outcome

    return go


def codes(envelope):
    return [e["code"] for e in envelope["errors"]]


# -- resolve_target ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def kube_tree():
    return load_dir(KUBE_MINI)


def test_resolves_a_deployment_through_the_plugins_own_types(kube_tree):
    target = resolve_target(DEPLOYMENT, kube_tree, identity_overrides={})
    assert target == {
        "path": DEPLOYMENT,
        "type": "kubernetes/deployment",
        "identity": {"cluster": "kind", "namespace": "runwhen", "name": "runwhen-papi"},
    }


def test_the_root_type_has_only_its_name(kube_tree):
    target = resolve_target("kubernetes/clusters/kind", kube_tree, identity_overrides={})
    assert target["type"] == "kubernetes/cluster"
    assert target["identity"] == {"name": "kind"}


def test_names_are_percent_decoded_after_the_split(kube_tree):
    path = "kubernetes/clusters/a%2Fb/namespaces/my%20ns/deployments/web"
    target = resolve_target(path, kube_tree, identity_overrides={})
    assert target["identity"] == {"cluster": "a/b", "namespace": "my ns", "name": "web"}
    assert target["path"] == path


def test_identity_overrides_replace_and_add_fields(kube_tree):
    target = resolve_target(
        DEPLOYMENT, kube_tree, identity_overrides={"cluster": "prod", "region": "eu"}
    )
    assert target["identity"] == {
        "cluster": "prod",
        "namespace": "runwhen",
        "name": "runwhen-papi",
        "region": "eu",
    }


@pytest.mark.parametrize(
    ("plural", "type_name"),
    [
        ("repositories", "repositorie"),  # the rule is only "s"/"es": a guess, and a warned one
        ("deployments", "deployment"),
        ("ingresses", "ingress"),
        ("batches", "batch"),
        ("dishes", "dish"),
        ("boxes", "box"),
        ("data", "data"),
    ],
)
def test_a_foreign_platform_guesses_the_type_from_the_plural(kube_tree, plural, type_name):
    target = resolve_target(
        f"github/organizations/acme/{plural}/web", kube_tree, identity_overrides={}
    )
    assert target["type"] == f"github/{type_name}"
    assert target["identity"] == {"organization": "acme", "name": "web"}
    assert any("not this plugin's" in w for w in target["warnings"])


def test_an_unknown_plural_on_the_plugins_own_platform_is_guessed_and_warned(kube_tree):
    target = resolve_target(
        "kubernetes/clusters/kind/namespaces/a/widgets/w", kube_tree, identity_overrides={}
    )
    assert target["type"] == "kubernetes/widget"
    assert any("'widgets'" in w and "guessed" in w for w in target["warnings"])


@pytest.mark.parametrize(
    "path",
    [
        "kubernetes",
        "kubernetes/clusters",
        "kubernetes/clusters/kind/namespaces",
        "kubernetes/clusters//namespaces/x",
        "kubernetes/namespaces/x",  # not a root
        "kubernetes/clusters/kind/deployments/x",  # a deployment is not a child of a cluster
    ],
)
def test_a_path_that_cannot_be_a_resource_is_refused(kube_tree, path):
    with pytest.raises(ValueError):
        resolve_target(path, kube_tree, identity_overrides={})


def test_coerce_value_parses_json_types_and_keeps_text():
    assert coerce_value("integer", "5") == 5
    assert coerce_value("number", "1.5") == 1.5
    assert coerce_value("boolean", "true") is True
    assert coerce_value("string[]", '["a","b"]') == ["a", "b"]
    assert coerce_value("object", '{"a":1}') == {"a": 1}
    assert coerce_value("string", "5") == "5"
    assert coerce_value("duration", "15m") == "15m"
    assert coerce_value(None, "x") == "x"
    with pytest.raises(ValueError, match="expected JSON for type integer"):
        coerce_value("integer", "five")


# -- what a script sees ---------------------------------------------------------------------


def test_a_python_task_sees_resource_settings_inputs_and_lib(run):
    env = run("py-ok", inputs={"since": "1h"}, settings={"hosts": ["a", "b"]}).envelope
    assert env["status"] == "ok"
    assert env["errors"] == []
    assert env["outputs"]["summary"] == {
        "namespace": "runwhen",
        "name": "runwhen-papi",
        "path": DEPLOYMENT,
        "threshold": 1500,  # the compiled default
        "since": "1h",  # the caller's value over the default
        "limit": 3,
        "label": "plain",
        "hosts": ["a", "b"],
        "dry": False,
        "lib": "from-lib",
    }


def test_a_bash_task_sees_rw_resource_setting_input_and_out(run):
    outcome = run("sh-ok", inputs={"count": 2, "note": "hi"}, settings={"latencyThresholdMs": 99})
    env = outcome.envelope
    assert env["status"] == "ok"
    row = {"namespace": "runwhen", "name": "runwhen-papi", "threshold": 99, "note": "hi"}
    assert env["outputs"]["probe"] == [row, row]
    # the file the script wrote at $RW_OUT/events.json is the `events` artifact
    assert env["outputs"]["events"] == {"artifact": "art_local_events"}
    assert set(outcome.artifacts) == {"events"}
    assert '"type":"kubernetes/deployment"' in outcome.artifacts["events"].read_text()
    assert f'"path":"{DEPLOYMENT}"' in outcome.artifacts["events"].read_text()


def test_a_dry_run_sets_rw_dry_run_and_the_envelope_says_so(run):
    env = run("sh-write", dry_run=True).envelope
    assert env["status"] == "ok"
    assert env["outputs"]["result"] == {"dry": True}
    assert env["dryRun"] is True
    live = run("sh-write").envelope
    assert live["outputs"]["result"] == {"dry": False}
    assert "dryRun" not in live


def test_a_python_task_sees_ctx_dry_run(run):
    env = run("py-ok", dry_run=True).envelope
    assert env["outputs"]["summary"]["dry"] is True


def test_dry_run_is_refused_for_a_task_that_is_not_dryrun(run):
    env = run("py-nothing", dry_run=True).envelope
    assert env["status"] == "failed"
    assert codes(env) == ["invalid_input"]
    assert "dry run" in env["errors"][0]["message"]


def test_a_workspace_wide_task_has_no_resource(run):
    # py-ok reads ctx.resource, which is None without a target: that is the script's crash
    env = run("py-ok", resource=None).envelope
    assert env["status"] == "failed"
    assert codes(env) == ["internal"]


# -- inputs and settings ---------------------------------------------------------------------


def test_a_missing_required_input_fails_without_starting_the_process(run, tmp_path):
    outcome = run("py-need")
    env = outcome.envelope
    assert env["status"] == "failed"
    assert codes(env) == ["invalid_input"]
    assert "'reason'" in env["errors"][0]["message"]
    assert env["errors"][0]["scope"] == "reason"
    assert env["outputs"] == {}
    assert not (tmp_path / "scope").exists()  # no scope was even prepared


def test_a_required_input_is_accepted_when_given(run):
    assert run("py-need", inputs={"reason": "because"}).envelope["status"] == "ok"


def test_a_wrongly_typed_or_unknown_input_is_invalid_input(run):
    env = run("py-ok", inputs={"limit": "many", "nope": 1}).envelope
    assert env["status"] == "failed"
    assert codes(env) == ["invalid_input", "invalid_input"]
    messages = " ".join(e["message"] for e in env["errors"])
    assert "limit" in messages
    assert "nope" in messages


def test_an_unknown_or_mistyped_setting_is_invalid_input(run):
    env = run("py-ok", settings={"latencyThresholdMs": "slow", "ghost": 1}).envelope
    assert env["status"] == "failed"
    assert codes(env) == ["invalid_input", "invalid_input"]


# -- how a run ends ------------------------------------------------------------------------


def test_ctx_error_then_return_is_partial(run):
    env = run("py-partial").envelope
    assert env["status"] == "partial"
    assert env["outputs"] == {"pools": []}
    assert env["errors"] == [
        {"code": "unreachable", "message": "could not reach pgbouncer", "scope": "pgbouncer:6432"}
    ]


def test_an_uncaught_rw_error_fails_with_its_code(run):
    env = run("py-raise").envelope
    assert env["status"] == "failed"
    assert env["errors"] == [{"code": "not_found", "message": "no such deployment", "scope": "web"}]


def test_ctx_run_of_a_failing_command_fails_under_v13(run):
    env = run("py-run", inputs={"snippet": "false"}).envelope
    assert env["status"] == "failed"
    assert codes(env) == ["internal"]


def test_ctx_run_classifies_the_commands_stderr(run):
    env = run(
        "py-run", inputs={"snippet": "echo 'Error from server (Forbidden): pods' >&2; exit 1"}
    ).envelope
    assert env["status"] == "failed"
    assert codes(env) == ["permission_denied"]


def test_bash_exit_3_without_rw_error_is_failed_with_the_last_stderr_line(run):
    outcome = run("sh-exit3")
    env = outcome.envelope
    assert env["status"] == "failed"
    assert env["errors"] == [{"code": "internal", "message": "boom: could not frobnicate"}]
    assert "boom" in outcome.log_tail


def test_rw_error_in_bash_is_partial_and_a_bad_code_becomes_internal(run):
    env = run("sh-rw-error").envelope
    assert env["status"] == "partial"
    assert [e["code"] for e in env["errors"]] == ["permission_denied", "internal"]
    assert env["errors"][0]["scope"] == "ns/web"
    assert "not_a_code" in env["errors"][1]["hint"]


@pytest.mark.skipif(BASH_MAJOR < 4, reason="command_not_found_handle needs bash 4 (macOS has 3.2)")
def test_a_missing_tool_is_a_dependency_error(run):
    env = run("sh-missing-tool").envelope
    assert env["status"] == "failed"
    assert codes(env) == ["dependency"]
    assert "definitely-not-a-tool-xyz" in env["errors"][0]["message"]


def test_a_python_crash_is_internal_with_the_exception_line(run):
    env = run("py-crash").envelope
    assert env["status"] == "failed"
    assert env["errors"] == [{"code": "internal", "message": "ValueError: kaboom"}]


def test_a_skipped_task_is_skipped_with_its_reason(run):
    env = run("py-skip").envelope
    assert env["status"] == "skipped"
    assert env["reason"] == "nothing to check"
    assert env["errors"] == []


def test_a_task_that_writes_nothing_is_ok_with_empty_outputs(run):
    # Review focus 3: outputs are optional, even a declared one
    env = run("py-nothing").envelope
    assert env["status"] == "ok"
    assert env["outputs"] == {}
    assert env["errors"] == []


def test_a_task_past_its_deadline_is_failed_with_timeout(run):
    env = run("py-slow", timeout_s=1).envelope
    assert env["status"] == "failed"
    assert codes(env) == ["timeout"]


def test_the_compiled_timeout_is_the_default_deadline(run, plugin, monkeypatch):
    tree, tasks = plugin
    seen = {}
    real = __import__("runwhen_capability.bundle", fromlist=["_execute"])._execute

    def spy(**kw):
        seen["deadline"] = kw["deadline_seconds"]
        return real(**kw)

    monkeypatch.setattr("runwhen_capability.bundle._execute", spy)
    run("py-nothing")
    assert seen["deadline"] == 30.0  # `timeout: 30s` in defaults


# -- outputs -------------------------------------------------------------------------------


def test_an_output_that_breaks_its_schema_is_dropped_and_named(run):
    env = run("py-bad-output").envelope
    assert env["status"] == "partial"
    assert env["outputs"] == {"good": {"n": 1}}
    [error] = env["errors"]
    assert error["code"] == "internal"
    assert error["scope"] == "summary"
    assert "'summary'" in error["message"]
    assert "namespace" in error["message"]


def test_an_undeclared_output_is_dropped_with_an_internal_error(run):
    env = run("py-undeclared").envelope
    assert env["status"] == "partial"
    assert env["outputs"] == {"summary": {"n": 1}}
    assert [e["code"] for e in env["errors"]] == ["internal"]
    assert "'extra'" in env["errors"][0]["message"]
    assert "not declared" in env["errors"][0]["message"]


def test_an_artifact_is_the_file_in_rw_out_and_scratch_files_are_not_outputs(run):
    outcome = run("py-artifact")
    env = outcome.envelope
    assert env["status"] == "ok"
    assert env["outputs"] == {"dump": {"artifact": "art_local_dump"}, "n": {"v": 1}}
    assert outcome.artifacts["dump"].read_text() == "a dump\n"
    assert set(outcome.artifacts) == {"dump"}  # scratch.txt and the unwritten output: not here


# -- the child's environment (SEC-2) ----------------------------------------------------------


def test_host_only_variables_never_reach_a_v13_task(run, monkeypatch):
    monkeypatch.setenv("RELAY_URL", "http://relay.invalid")
    monkeypatch.setenv("POOL_ID", "pool-1")
    monkeypatch.setenv("EXECUTOR_TOKEN_FILE", "/var/run/executor/token")
    env = run("sh-env").envelope
    assert env["status"] == "ok"
    seen = env["outputs"]["env"]
    assert seen["relay"] == "unset"
    assert seen["pool"] == "unset"
    assert seen["token"] == "unset"
    assert seen["contract"] == "v13"


def test_host_only_names_cannot_be_smuggled_in_through_extra_env(run):
    for name in ("RELAY_URL", "POOL_ID", "EXECUTOR_TOKEN_FILE", "RW_SDK", "HOME", "PATH"):
        with pytest.raises(ValueError, match="extra_env may not set"):
            run("py-nothing", extra_env={name: "x"})


def test_the_task_gets_a_private_home_unless_the_caller_keeps_its_own(run, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    private = run("sh-env").envelope["outputs"]["env"]["home"]
    assert private == str(tmp_path / "scope" / ".rw-home")
    assert run("sh-env", inherit_home=True).envelope["outputs"]["env"]["home"] == str(
        tmp_path / "real-home"
    )


def test_extra_env_reaches_the_task_and_is_redacted_from_what_comes_back(run):
    secret = "s3cr3t-value-12345"
    env = run("py-secret", extra_env={"MY_API_TOKEN": secret}).envelope
    assert env["status"] == "ok"
    assert env["outputs"]["leaked"] == {"token": "***REDACTED***"}
    assert secret not in str(env)


def test_a_shell_the_script_starts_is_not_forced_into_strict_mode(run):
    env = run("sh-nested").envelope
    assert env["status"] == "ok"
    assert env["outputs"]["nested"] == {"ok": True}


def test_plugin_files_are_materialized_with_their_executable_bit(plugin, tmp_path):
    tree, tasks = plugin
    script = tasks["sh-ok"]["script"]
    files = {**tree.files, "skills/probe/scripts/tool.sh": b"#!/bin/sh\n"}
    spec = RunSpec(
        task=tasks["py-nothing"],
        files=files,
        executable=frozenset({"skills/probe/scripts/tool.sh"}),
    )
    execute(spec, scope_dir=tmp_path / "s")
    tool = tmp_path / "s" / "plugin" / "skills" / "probe" / "scripts" / "tool.sh"
    assert tool.stat().st_mode & stat.S_IXUSR
    assert not (tmp_path / "s" / "plugin" / script).stat().st_mode & stat.S_IXUSR


def test_a_path_that_would_leave_the_scope_is_refused(plugin, tmp_path):
    tree, tasks = plugin
    spec = RunSpec(task=tasks["py-nothing"], files={**tree.files, "../evil": b"x"})
    env = execute(spec, scope_dir=tmp_path / "s").envelope
    assert env["status"] == "failed"
    assert codes(env) == ["internal"]
    assert "refusing to write" in env["errors"][0]["message"]
    assert not (tmp_path / "evil").exists()


# -- the wiki 42b scripts, as written --------------------------------------------------------


def _stub(directory: Path, name: str, body: str) -> None:
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


@pytest.mark.skipif(shutil.which("jq") is None, reason="the 42b script uses jq")
def test_the_documented_api_probe_script_runs_with_the_documented_names(run, tmp_path, monkeypatch):
    calls = tmp_path / "calls.log"
    _stub(
        tmp_path / "bin",
        "kubectl",
        f'echo "$@" >> {calls}\n'
        'case "$*" in\n'
        "  *healthz) echo ok ;;\n"
        "  *livez) echo 'Error from server (ServiceUnavailable)' >&2; exit 1 ;;\n"
        "  *) echo '{\"items\":[]}' ;;\n"
        "esac\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin:/usr/local/bin")
    monkeypatch.setenv("FAKE_LATENCY_MS", "2000")
    env = run("api-probe").envelope
    assert env["status"] == "ok", env
    probes = env["outputs"]["probe"]
    assert [(p["path"], p["status"], p["slow"]) for p in probes] == [
        ("healthz", 200, True),
        ("livez", 503, True),
    ]
    assert all(p["latencyMs"] >= 2000 for p in probes)  # SECONDS ticks in whole seconds
    logged = calls.read_text()
    assert "get --raw /api/v1/namespaces/runwhen/services/runwhen-papi:8000/proxy/healthz" in logged
    assert (
        "-n runwhen get events --field-selector involvedObject.name=runwhen-papi,type=Warning"
        in logged
    )


@pytest.mark.skipif(shutil.which("jq") is None, reason="the 42b script uses jq")
def test_the_documented_api_probe_honours_a_setting_override(run, tmp_path, monkeypatch):
    _stub(tmp_path / "bin", "kubectl", "echo ok\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin:/usr/local/bin")
    monkeypatch.setenv("FAKE_LATENCY_MS", "2000")
    env = run("api-probe", settings={"latencyThresholdMs": 5000}).envelope
    assert [p["slow"] for p in env["outputs"]["probe"]] == [False, False]


def test_the_documented_pool_saturation_script_reports_unreachable_not_healthy(
    run, tmp_path, monkeypatch
):
    _stub(
        tmp_path / "bin",
        "psql",
        "echo 'psql: error: connection to server failed: Connection refused' >&2\nexit 2\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin:/usr/local/bin")
    env = run("pool-saturation").envelope
    assert env["status"] == "partial"
    assert env["outputs"] == {"pools": []}
    [error] = env["errors"]
    assert error["code"] == "unreachable"  # the script chose it; the message is ctx.run's
    assert error["scope"] == "runwhen-papi.runwhen.svc:5432"


def test_the_documented_pool_saturation_script_reads_settings_in_snake_case(
    run, tmp_path, monkeypatch
):
    _stub(tmp_path / "bin", "psql", "echo 7,0\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin:/usr/local/bin")
    env = run("pool-saturation", settings={"host": "localhost:6432"}).envelope
    assert env["status"] == "ok"
    assert env["outputs"]["pools"] == [{"cl_waiting": 7, "sv_idle": 0, "saturated": True}]
    env = run("pool-saturation", settings={"waitingClientsThreshold": 9}).envelope
    assert env["outputs"]["pools"][0]["saturated"] is False
