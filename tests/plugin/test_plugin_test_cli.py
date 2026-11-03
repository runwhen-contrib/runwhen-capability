"""`rwtask test <skill>[/<task>] [--record]` (D43): record once against a fake kubectl,
replay without it, and every way a replay can fail. The plugin is a tmp copy of run-mini
(`kube-count`, `kube-count-py`), so recording never writes into the repo."""

from __future__ import annotations

import os
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from runwhen_capability.cli import main
from runwhen_capability.contracts import load_yaml

HERE = Path(__file__).resolve().parent
RUN_MINI = HERE / "fixtures" / "run-mini"
BUNDLES = HERE.parent / "fixtures" / "bundles"
DEPLOYMENT = "kubernetes/clusters/kind/namespaces/runwhen/deployments/runwhen-papi"
needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="kube-count counts with jq")

FAKE_KUBECTL = """\
#!/bin/sh
echo "$*" >> "$FAKE_KUBECTL_LOG"
case "$*" in
  *"get pods"*) printf '{\\n  "items": [{}, {}, {}]\\n}\\n' ;;
  *"get events"*) echo "Error: events is Forbidden" >&2; exit 1 ;;
  *"annotate"*) echo "deployment.apps/runwhen-papi annotated" ;;
  *"rollout restart"*"--dry-run=server"*) echo "deployment.apps/runwhen-papi restarted (dry run)" ;;
  *"rollout restart"*) echo "deployment.apps/runwhen-papi restarted" ;;
  *) echo "unexpected: $*" >&2; exit 9 ;;
esac
"""


@pytest.fixture
def plugin(tmp_path):
    dest = tmp_path / "plugin"
    shutil.copytree(RUN_MINI, dest)
    return dest


@pytest.fixture
def kubectl(tmp_path, monkeypatch):
    """A fake kubectl first on PATH that logs each real invocation."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "kubectl"
    script.write_text(FAKE_KUBECTL)
    script.chmod(0o755)
    log = tmp_path / "kubectl.log"
    monkeypatch.setenv("FAKE_KUBECTL_LOG", str(log))
    original = os.environ["PATH"]
    monkeypatch.setenv("PATH", f"{bin_dir}:{original}")

    class Fake:
        def calls(self):
            return log.read_text().splitlines() if log.exists() else []

        def remove(self):
            script.unlink()
            monkeypatch.setenv("PATH", original)

    return Fake()


@pytest.fixture
def rwtask(plugin, capsys):
    def go(*args, plugin_dir=plugin, expect=None):
        code = main(["test", *args, "--plugin-dir", str(plugin_dir)])
        captured = capsys.readouterr()
        if expect is not None:
            assert code == expect, captured.out + captured.err
        return code, captured.out, captured.err

    return go


def fixture_path(plugin, task="kube-count", scenario="basic"):
    return plugin / "skills" / "probe" / "tests" / task / f"{scenario}.yaml"


def record(rwtask, *args, task="kube-count", scenario="basic", expect=0):
    return rwtask(
        f"probe/{task}",
        "--record",
        "--resource",
        DEPLOYMENT,
        "--scenario",
        scenario,
        *args,
        expect=expect,
    )


def add_outputs(path, text):
    path.write_text(path.read_text() + text)


# -- record, then replay -----------------------------------------------------------------------


@needs_jq
def test_record_then_replay_without_the_tool(rwtask, plugin, kubectl):
    _, _, err = record(rwtask)
    assert "recorded 2 calls" in err
    assert kubectl.calls() == [
        "-n runwhen get pods -o json",
        "-n runwhen get events -o json",
    ]
    path = fixture_path(plugin)
    doc = load_yaml(path.read_text())
    assert doc["resource"] == DEPLOYMENT
    assert [c["argv"] for c in doc["calls"]] == [
        ["kubectl", "-n", "runwhen", "get", "pods", "-o", "json"],
        ["kubectl", "-n", "runwhen", "get", "events", "-o", "json"],
    ]
    assert doc["calls"][0]["stdout"] == '{\n  "items": [{}, {}, {}]\n}\n'
    assert doc["calls"][1] == {
        "argv": ["kubectl", "-n", "runwhen", "get", "events", "-o", "json"],
        "rc": 1,
        "stdout": "",
        "stderr": "Error: events is Forbidden\n",
    }
    assert doc["expect"] == {"status": "partial", "errorCodes": ["permission_denied"]}
    assert "outputs" in path.read_text().split("calls:")[0]  # the comment tells the author
    assert "stdout: |\n" in path.read_text()  # a block literal, so it diffs line by line
    assert not any(c["argv"][0] == "jq" for c in doc["calls"])  # jq is never shimmed

    add_outputs(path, "  outputs:\n    pods: {namespace: runwhen, count: 3}\n")
    kubectl.remove()
    _, out, _ = rwtask("probe/kube-count", expect=0)
    assert "PASS probe/kube-count/basic" in out
    assert kubectl.calls() == [
        "-n runwhen get pods -o json",
        "-n runwhen get events -o json",
    ]  # replay never ran the tool


def test_the_whole_skill_is_replayed_without_naming_a_task(rwtask, plugin, kubectl):
    if shutil.which("jq") is None:
        pytest.skip("kube-count counts with jq")
    record(rwtask)
    record(rwtask, task="kube-count-py")
    kubectl.remove()
    _, out, _ = rwtask("probe", expect=0)
    assert "PASS probe/kube-count/basic" in out
    assert "PASS probe/kube-count-py/basic" in out
    assert "2 passed, 0 failed" in out


def test_a_python_task_records_and_replays_through_ctx_run(rwtask, plugin, kubectl):
    record(rwtask, task="kube-count-py")
    doc = load_yaml(fixture_path(plugin, "kube-count-py").read_text())
    assert doc["expect"] == {"status": "ok", "errorCodes": []}
    assert len(doc["calls"]) == 1
    add_outputs(fixture_path(plugin, "kube-count-py"), "  outputs:\n    pods: {count: 3}\n")
    kubectl.remove()
    _, out, _ = rwtask("probe/kube-count-py", expect=0)
    assert "PASS probe/kube-count-py/basic" in out


def test_record_persists_typed_inputs_settings_and_identity(rwtask, plugin):
    rwtask(
        "probe/py-ok",
        "--record",
        "--resource",
        DEPLOYMENT,
        "--scenario",
        "typed",
        "--input",
        "limit=7",
        "--input",
        "since=2h",
        "--setting",
        'hosts=["a"]',
        "--identity",
        "cluster=prod",
        expect=0,
    )
    doc = load_yaml(fixture_path(plugin, "py-ok", "typed").read_text())
    assert doc["inputs"] == {"limit": 7, "since": "2h"}
    assert doc["settings"] == {"hosts": ["a"]}
    assert doc["identity"] == {"cluster": "prod"}
    assert doc["calls"] == []
    add_outputs(
        fixture_path(plugin, "py-ok", "typed"),
        "  outputs:\n    summary: {limit: 7, since: 2h, hosts: [a], namespace: runwhen}\n",
    )
    _, out, _ = rwtask("probe/py-ok", expect=0)
    assert "PASS probe/py-ok/typed" in out


@needs_jq
def test_extra_shims_are_recorded_with_their_stdin(rwtask, plugin, kubectl):
    record(rwtask, "--shim", "jq")
    doc = load_yaml(fixture_path(plugin).read_text())
    jq_calls = [c for c in doc["calls"] if c["argv"][0] == "jq"]
    assert len(jq_calls) == 2
    assert jq_calls[0]["stdin"] == '{\n  "items": [{}, {}, {}]\n}\n'
    assert jq_calls[0]["stdout"] == "3\n"
    # replayed only with the same --shim: jq is the task's own logic otherwise
    kubectl.remove()
    _, out, _ = rwtask("probe/kube-count", "--shim", "jq", expect=0)
    assert "PASS probe/kube-count/basic" in out
    code, out, _ = rwtask("probe/kube-count")
    assert code == 1  # jq now runs for real and is asked for fewer calls than recorded
    assert "FAIL probe/kube-count/basic" in out


# -- replay failures ------------------------------------------------------------------------------


@pytest.fixture
def recorded(rwtask, plugin, kubectl):
    if shutil.which("jq") is None:
        pytest.skip("kube-count counts with jq")
    record(rwtask)
    kubectl.remove()
    return fixture_path(plugin)


def test_a_different_argv_fails_with_the_first_differing_call(rwtask, recorded):
    doc = load_yaml(recorded.read_text())
    doc["calls"][1]["argv"][4] = "nodes"
    recorded.write_text(yaml.safe_dump(doc))
    code, out, _ = rwtask("probe/kube-count")
    assert code == 1
    assert "FAIL probe/kube-count/basic" in out
    assert (
        "E_FIXTURE_MISMATCH call 2: expected kubectl -n runwhen get nodes -o json "
        "got kubectl -n runwhen get events -o json"
    ) in out
    assert "0 passed, 1 failed" in out


def test_recorded_calls_the_task_never_makes_fail_the_test(rwtask, recorded):
    doc = load_yaml(recorded.read_text())
    doc["calls"].append({"argv": ["kubectl", "get", "nodes"], "rc": 0, "stdout": "", "stderr": ""})
    recorded.write_text(yaml.safe_dump(doc))
    code, out, _ = rwtask("probe/kube-count")
    assert code == 1
    assert "1 recorded calls not made" in out


def test_more_calls_than_recorded_fail_the_test(rwtask, recorded):
    doc = load_yaml(recorded.read_text())
    doc["calls"].pop()
    recorded.write_text(yaml.safe_dump(doc))
    code, out, _ = rwtask("probe/kube-count")
    assert code == 1
    assert (
        "E_FIXTURE_MISMATCH call 2: expected no further call got kubectl -n runwhen get events"
        in out
    )


def test_expect_status_mismatch_fails(rwtask, recorded):
    recorded.write_text(recorded.read_text().replace("status: partial", "status: ok"))
    code, out, _ = rwtask("probe/kube-count")
    assert code == 1
    assert "status: expected ok, got partial" in out


def test_expect_outputs_mismatch_fails_and_a_subset_passes(rwtask, recorded):
    add_outputs(recorded, "  outputs:\n    pods: {count: 4}\n")
    code, out, _ = rwtask("probe/kube-count")
    assert code == 1
    assert "outputs.pods.count: expected 4, got 3" in out
    recorded.write_text(recorded.read_text().replace("count: 4", "count: 3"))
    assert rwtask("probe/kube-count")[0] == 0


def test_a_scenario_that_cannot_be_loaded_fails_naming_the_problem(rwtask, plugin):
    path = fixture_path(plugin, scenario="broken")
    path.parent.mkdir(parents=True)
    path.write_text("calls: []\nexpect: {status: great}\n")
    code, out, _ = rwtask("probe/kube-count")
    assert code == 1
    assert "FAIL probe/kube-count/broken" in out
    assert "expect.status" in out


def test_no_fixtures_is_a_failure_that_says_so(rwtask):
    for ref in ("probe/kube-count", "probe"):
        code, out, err = rwtask(ref)
        assert code == 1
        assert "no fixtures" in out + err


def test_a_fixture_directory_for_a_task_that_does_not_exist_fails(rwtask, plugin):
    stale = plugin / "skills" / "probe" / "tests" / "renamed-task"
    stale.mkdir(parents=True)
    (stale / "a.yaml").write_text("expect: {status: ok}\n")
    code, out, _ = rwtask("probe")
    assert code == 1
    assert "probe/renamed-task" in out and "no such task" in out


def test_an_unknown_skill_or_task_is_a_usage_error(rwtask):
    assert rwtask("nope/x")[0] == 2
    code, _, err = rwtask("probe/nonesuch")
    assert code == 2
    assert "no task" in err


# -- record guards ------------------------------------------------------------------------------


@needs_jq
def test_record_refuses_to_overwrite_without_force(rwtask, plugin, kubectl):
    record(rwtask)
    path = fixture_path(plugin)
    path.write_text(path.read_text() + "# hand-written\n")
    code, _, err = record(rwtask, expect=2)
    assert "already exists" in err and "--force" in err
    assert path.read_text().endswith("# hand-written\n")
    record(rwtask, "--force", expect=0)
    assert "hand-written" not in path.read_text()


def test_record_refuses_a_write_task_without_allow_write(rwtask, plugin):
    code, _, err = rwtask("probe/sh-write", "--record", "--resource", DEPLOYMENT, "--scenario", "w")
    assert code == 2
    assert "not read-only" in err and "--allow-write" in err
    assert not fixture_path(plugin, "sh-write", "w").exists()
    rwtask(
        "probe/sh-write", "--record", "--resource", DEPLOYMENT, "--scenario", "w",
        "--allow-write", expect=0,
    )  # fmt: skip
    assert fixture_path(plugin, "sh-write", "w").is_file()


@pytest.mark.parametrize(
    "extra",
    [
        [],  # no --scenario
        ["--scenario", "../x"],
        ["--scenario", "a b"],
    ],
)
def test_record_needs_a_sane_scenario_name(rwtask, extra):
    code, _, err = rwtask("probe/py-ok", "--record", "--resource", DEPLOYMENT, *extra)
    assert code == 2
    assert "scenario" in err


def test_record_needs_a_task_not_a_whole_skill(rwtask):
    code, _, err = rwtask("probe", "--record", "--resource", DEPLOYMENT, "--scenario", "a")
    assert code == 2
    assert "<skill>/<task>" in err


def test_the_record_flags_are_refused_when_replaying(rwtask):
    with pytest.raises(SystemExit):
        rwtask("probe/py-ok", "--input", "limit=1")
    with pytest.raises(SystemExit):
        rwtask("probe/py-ok", "--scenario", "x")


# -- classic `rwtask test <dir>` is untouched -----------------------------------------------------


def test_a_classic_capability_dir_still_runs_its_tests(capsys):
    assert main(["test", str(BUNDLES / "testable")]) == 0
    out = capsys.readouterr().out
    assert "ok    testable/greet-py" in out and "3 passed, 0 failed, 1 no test" in out


def test_a_classic_dir_with_a_slash_is_not_taken_for_a_skill_task(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no skills/ here
    assert main(["test", str(BUNDLES / "testable"), "--task", "greet-py"]) == 0
    assert "ok" in capsys.readouterr().out


# -- artifacts and matchers in a replayed scenario ---------------------------------------------


def record_sh_ok(rwtask, plugin):
    rwtask("probe/sh-ok", "--record", "--resource", DEPLOYMENT, "--scenario", "art", expect=0)
    return fixture_path(plugin, "sh-ok", "art")


def expect_block(path, block):
    path.write_text(path.read_text() + block)


def test_a_list_output_is_matched_with_contains_matchers(rwtask, plugin):
    path = record_sh_ok(rwtask, plugin)
    expect_block(
        path,
        "  outputs:\n    probe:\n      $contains: [{namespace: runwhen}, {note: hello}]\n",
    )
    _, out, _ = rwtask("probe/sh-ok", expect=0)
    assert "PASS probe/sh-ok/art" in out
    path.write_text(path.read_text().replace("{note: hello}", "{note: nope}"))
    code, out, _ = rwtask("probe/sh-ok")
    assert code == 1
    assert "outputs.probe: $contains: no element matches {'note': 'nope'}" in out


def test_a_bad_matcher_fails_the_scenario_not_the_run(rwtask, plugin):
    path = record_sh_ok(rwtask, plugin)
    expect_block(path, "  outputs:\n    probe: {$contian: []}\n")
    code, out, _ = rwtask("probe/sh-ok")
    assert code == 1
    assert "FAIL probe/sh-ok/art" in out and "unknown matcher $contian" in out


@pytest.mark.parametrize(
    ("block", "ok", "needle"),
    [
        ("  artifacts:\n    events: {contains: ['kubernetes/deployment']}\n", True, ""),
        ("  artifacts:\n    events: {contains: ['no-such-text']}\n", False, "missing text"),
        ("  artifacts:\n    events: {notContains: ['no-such-text']}\n", True, ""),
        ("  artifacts:\n    events: {notContains: ['runwhen']}\n", False, "forbidden text"),
        ("  artifacts:\n    events: {lines: {min: 1, max: 1}}\n", True, ""),
        ("  artifacts:\n    events: {lines: {min: 2}}\n", False, "1 lines, expected at least 2"),
        ("  artifacts:\n    nope: {}\n", False, "not an artifact output of this task"),
    ],
)
def test_artifact_assertions_pass_and_fail_through_the_cli(rwtask, plugin, block, ok, needle):
    expect_block(record_sh_ok(rwtask, plugin), block)
    code, out, _ = rwtask("probe/sh-ok")
    assert (code == 0) is ok, out
    assert needle in out
    assert ("PASS" if ok else "FAIL") in out


def test_a_declared_artifact_that_was_not_produced_fails(rwtask, plugin):
    rwtask("probe/py-artifact", "--record", "--resource", DEPLOYMENT, "--scenario", "a", expect=0)
    path = fixture_path(plugin, "py-artifact", "a")
    expect_block(
        path,
        "  artifacts:\n    dump: {contains: ['a dump']}\n    declaredButNeverWritten: {}\n",
    )
    code, out, _ = rwtask("probe/py-artifact")
    assert code == 1
    assert "artifacts.declaredButNeverWritten: the task did not produce this artifact" in out
    assert "artifacts.dump" not in out  # the one that exists is fine


# -- dry-run scenarios -----------------------------------------------------------------------------


def test_a_dry_run_is_recorded_and_replayed(rwtask, plugin, kubectl):
    _, _, err = rwtask(
        "probe/kube-restart", "--record", "--resource", DEPLOYMENT, "--scenario", "dry",
        "--dry-run", expect=0,
    )  # fmt: skip
    assert kubectl.calls() == [
        "-n runwhen rollout restart deployment/runwhen-papi --dry-run=server"
    ]
    path = fixture_path(plugin, "kube-restart", "dry")
    doc = load_yaml(path.read_text())
    assert doc["dryRun"] is True
    assert doc["calls"][0]["argv"][-1] == "--dry-run=server"
    assert doc["expect"] == {"status": "ok", "errorCodes": []}
    add_outputs(path, "  outputs:\n    result: {dry: true}\n")
    kubectl.remove()
    _, out, _ = rwtask("probe/kube-restart", expect=0)
    assert "PASS probe/kube-restart/dry" in out


def test_a_real_run_of_a_write_task_is_not_what_a_dry_scenario_replays(rwtask, plugin, kubectl):
    rwtask(
        "probe/kube-restart", "--record", "--resource", DEPLOYMENT, "--scenario", "dry",
        "--dry-run", expect=0,
    )  # fmt: skip
    path = fixture_path(plugin, "kube-restart", "dry")
    path.write_text(path.read_text().replace("dryRun: true\n", ""))
    kubectl.remove()
    code, out, _ = rwtask("probe/kube-restart")
    assert code == 1  # without RW_DRY_RUN the task calls kubectl without --dry-run: a mismatch
    assert "E_FIXTURE_MISMATCH call 1" in out


def test_record_dry_run_needs_no_allow_write_but_a_real_write_still_does(rwtask, plugin, kubectl):
    code, _, err = rwtask(
        "probe/kube-restart", "--record", "--resource", DEPLOYMENT, "--scenario", "real"
    )
    assert code == 2 and "--allow-write" in err
    assert not fixture_path(plugin, "kube-restart", "real").exists()
    assert kubectl.calls() == []


def test_record_dry_run_is_refused_for_a_task_without_dry_run(rwtask, plugin):
    for task in ("sh-write-no-dry", "py-partial"):
        code, _, err = rwtask(
            f"probe/{task}", "--record", "--resource", DEPLOYMENT, "--scenario", "d", "--dry-run"
        )
        assert code == 2, task
        assert "does not declare dryRun: true" in err
        assert not fixture_path(plugin, task, "d").exists()


def test_replaying_a_dry_run_scenario_of_a_task_without_dry_run_fails(rwtask, plugin):
    path = fixture_path(plugin, "sh-write-no-dry", "d")
    path.parent.mkdir(parents=True)
    path.write_text("dryRun: true\nexpect: {status: ok}\n")
    code, out, _ = rwtask("probe/sh-write-no-dry")
    assert code == 1
    assert "FAIL probe/sh-write-no-dry/d" in out
    assert "dryRun: true, but the task does not declare dryRun: true" in out


def test_dry_run_only_applies_with_record(rwtask):
    with pytest.raises(SystemExit):
        rwtask("probe/kube-restart", "--dry-run")


# -- the run's reference time (RW_NOW) -------------------------------------------------------------


def record_annotate(rwtask, scenario="stamp"):
    return rwtask(
        "probe/kube-annotate", "--record", "--resource", DEPLOYMENT, "--scenario", scenario,
        "--allow-write", expect=0,
    )  # fmt: skip


def test_record_stores_the_reference_time_the_run_used(rwtask, plugin, kubectl):
    before = datetime.now(UTC).replace(microsecond=0)
    record_annotate(rwtask)
    doc = load_yaml(fixture_path(plugin, "kube-annotate", "stamp").read_text())
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", doc["now"]), doc["now"]
    assert before <= datetime.fromisoformat(doc["now"]) <= datetime.now(UTC)
    # the time the task put in its command line is the one that was recorded
    assert doc["calls"][0]["argv"][-2:] == [f"restarted-at={doc['now']}", "--overwrite"]
    assert kubectl.calls() == [
        f"-n runwhen annotate deployment/runwhen-papi restarted-at={doc['now']} --overwrite"
    ]


def test_replay_reproduces_an_argv_that_carries_the_time(rwtask, plugin, kubectl):
    record_annotate(rwtask)
    path = fixture_path(plugin, "kube-annotate", "stamp")
    stamp = load_yaml(path.read_text())["now"]
    add_outputs(path, f"  outputs:\n    result: {{stamped: '{stamp}'}}\n")
    kubectl.remove()
    time.sleep(0.01)  # a fresh clock reading would differ from the recorded one
    _, out, _ = rwtask("probe/kube-annotate", expect=0)
    assert "PASS probe/kube-annotate/stamp" in out


def test_a_scenario_without_now_replays_with_a_fresh_time(rwtask, plugin, kubectl):
    record_annotate(rwtask)
    path = fixture_path(plugin, "kube-annotate", "stamp")
    path.write_text(re.sub(r"(?m)^now: .*\n", "", path.read_text()))
    kubectl.remove()
    code, out, _ = rwtask("probe/kube-annotate")
    assert code == 1  # the recorded argv names a time the fresh run does not have
    assert "E_FIXTURE_MISMATCH call 1" in out
    assert "restarted-at=" in out


@needs_jq
def test_an_old_scenario_without_now_still_passes(rwtask, plugin, kubectl):
    record(rwtask)
    path = fixture_path(plugin)
    text = path.read_text()
    assert "\nnow: " in text  # every recording carries one now
    path.write_text(
        re.sub(r"(?m)^now: .*\n", "", text)
        + "  outputs:\n    pods: {namespace: runwhen, count: 3}\n"
    )
    kubectl.remove()
    _, out, _ = rwtask("probe/kube-count", expect=0)
    assert "PASS probe/kube-count/basic" in out


def test_replay_runs_the_task_at_the_scenarios_now(rwtask, plugin, kubectl):
    path = fixture_path(plugin, "kube-annotate", "fixed")
    path.parent.mkdir(parents=True)
    path.write_text(
        "resource: " + DEPLOYMENT + "\n"
        "now: '2020-02-29T23:59:58.500Z'\n"
        "calls:\n"
        "  - argv: [kubectl, -n, runwhen, annotate, deployment/runwhen-papi,\n"
        "           'restarted-at=2020-02-29T23:59:58.500Z', --overwrite]\n"
        "expect:\n"
        "  status: ok\n"
        "  outputs: {result: {stamped: '2020-02-29T23:59:58.500Z'}}\n"
    )
    _, out, _ = rwtask("probe/kube-annotate", expect=0)
    assert "PASS probe/kube-annotate/fixed" in out


def test_a_malformed_now_in_a_scenario_is_reported_with_the_file(rwtask, plugin):
    path = fixture_path(plugin, "kube-annotate", "bad")
    path.parent.mkdir(parents=True)
    path.write_text("now: yesterday\nexpect: {status: ok}\n")
    code, out, _ = rwtask("probe/kube-annotate")
    assert code == 1
    assert "FAIL probe/kube-annotate/bad" in out and "now" in out
