"""plugin/fixtures.py (scenario files, the expect matcher) and plugin/shims.py (PATH shims,
record and replay), without a plugin: the shims are driven by hand through a shell."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from runwhen_capability.contracts import load_yaml
from runwhen_capability.plugin.fixtures import (
    FixtureError,
    dump_scenario,
    load_scenario,
    match_expect,
)
from runwhen_capability.plugin.shims import DEFAULT_SHIMS, ShimSession

# -- scenario files -------------------------------------------------------------------------

GOOD = """\
resource: kubernetes/clusters/kind/namespaces/web/deployments/api
inputs: {limit: 3}
settings: {label: x}
identity: {cluster: kind}
calls:
  - argv: [kubectl, get, pods]
    rc: 0
    stdout: |
      {"items": []}
    stderr: ""
expect:
  status: partial
  errorCodes: [permission_denied]
  outputs:
    summary: {count: 0}
"""


def write(tmp_path: Path, text: str, name: str = "s.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def test_a_scenario_loads_with_every_field(tmp_path):
    scenario = load_scenario(write(tmp_path, GOOD))
    assert scenario.name == "s"
    assert scenario.resource.endswith("deployments/api")
    assert scenario.inputs == {"limit": 3}
    assert scenario.settings == {"label": "x"}
    assert scenario.identity == {"cluster": "kind"}
    assert scenario.calls == [
        {"argv": ["kubectl", "get", "pods"], "rc": 0, "stdout": '{"items": []}\n', "stderr": ""}
    ]
    assert scenario.expect["status"] == "partial"


def test_optional_parts_default(tmp_path):
    scenario = load_scenario(
        write(tmp_path, "calls: [{argv: [git, status]}]\nexpect: {status: ok}\n")
    )
    assert scenario.resource is None
    assert scenario.inputs == scenario.settings == scenario.identity == {}
    assert scenario.calls == [{"argv": ["git", "status"], "rc": 0, "stdout": "", "stderr": ""}]


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("calls: []\n", "(scenario): 'expect' is a required property"),
        ("expect: {status: ok}\nbogus: 1\n", "bogus"),
        ("expect: {status: great}\n", "expect.status"),
        ("expect: {status: ok, errorCodes: [nope]}\n", "expect.errorCodes[0]"),
        ("expect: {status: ok}\ncalls: [{rc: 0}]\n", "calls[0]: 'argv' is a required property"),
        ("expect: {status: ok}\ncalls: [{argv: []}]\n", "calls[0].argv"),
        ("expect: {status: ok}\ncalls: [{argv: [a], rc: x}]\n", "calls[0].rc"),
        ("expect: {status: ok}\ninputs: [1]\n", "inputs"),
        ("expect: {status: ok}\nidentity: {cluster: 3}\n", "identity.cluster"),
        ("- a\n", "a mapping"),
        ("a: [\n", "not valid YAML"),
    ],
)
def test_a_bad_scenario_says_what_is_wrong(tmp_path, text, needle):
    with pytest.raises(FixtureError) as exc:
        load_scenario(write(tmp_path, text))
    assert needle in "\n".join(exc.value.errors)
    assert str(tmp_path / "s.yaml") in str(exc.value)


def test_dump_round_trips_awkward_text_and_uses_block_literals(tmp_path):
    awkward = "line one\n  indented\n\ttabbed\ntrailing space \n\nlast"
    text = dump_scenario(
        resource="a/b/c",
        inputs={},
        settings={},
        identity={},
        calls=[
            {
                "argv": ["kubectl", "get", "yes"],
                "stdin": "x\ny\n",
                "rc": 1,
                "stdout": awkward,
                "stderr": "forbidden\n",
            }
        ],
        expect={"status": "failed", "errorCodes": ["internal"]},
        header="# outputs: add them",
    )
    assert text.startswith("# outputs: add them\n")
    assert "stderr: |\n" in text or "stderr: |-\n" in text  # multi-line text is a literal block
    assert "\\n" not in text.split("stdout:")[0]
    path = write(tmp_path, text)
    scenario = load_scenario(path)
    assert scenario.calls[0]["stdout"] == awkward
    assert scenario.calls[0]["stdin"] == "x\ny\n"
    assert scenario.calls[0]["argv"] == ["kubectl", "get", "yes"]
    assert load_yaml(text)["expect"] == {"status": "failed", "errorCodes": ["internal"]}


# -- expect matcher ---------------------------------------------------------------------------


def envelope(status="ok", outputs=None, codes=()):
    return {
        "status": status,
        "outputs": outputs or {},
        "errors": [{"code": c, "message": "m"} for c in codes],
    }


@pytest.mark.parametrize(
    ("outputs", "expected", "ok"),
    [
        ({"a": 1, "b": 2}, {"a": 1}, True),
        ({"a": 1}, {"a": 2}, False),
        ({"a": {"x": 1, "y": 2}}, {"a": {"x": 1}}, True),
        ({"a": {"x": 1}}, {"a": {"x": 1, "y": 2}}, False),
        ({"a": [1, 2, 3]}, {"a": [1, 2, 3]}, True),
        ({"a": [1, 2, 3]}, {"a": [1, 2]}, False),  # lists compare whole
        ({"a": [{"x": 1, "y": 2}]}, {"a": [{"x": 1}]}, False),  # not subset-matched inside lists
        ({"a": "1"}, {"a": 1}, False),
        ({"a": True}, {"a": 1}, False),  # bool is not an int
        ({"a": 1}, {"a": True}, False),
        ({"a": 1.0}, {"a": 1}, True),
        ({"a": None}, {"a": None}, True),
        ({}, {"a": None}, False),  # a missing key is not null
        ({"a": {"b": {"c": [1]}}}, {"a": {"b": {"c": [1]}}}, True),
        ({"a": 1}, {}, True),
    ],
)
def test_outputs_are_a_subset_match(outputs, expected, ok):
    problems = match_expect(envelope(outputs=outputs), {"status": "ok", "outputs": expected})
    assert (problems == []) is ok, problems


def test_mismatch_messages_name_the_path():
    problems = match_expect(
        envelope(outputs={"a": {"x": 1}, "l": [1]}),
        {"status": "ok", "outputs": {"a": {"x": 2, "z": 3}, "l": [2], "gone": 1}},
    )
    assert "outputs.a.x: expected 2, got 1" in problems
    assert "outputs.a.z: missing" in problems
    assert "outputs.l: expected [2], got [1]" in problems
    assert "outputs.gone: missing" in problems


def test_status_must_match():
    assert match_expect(envelope("failed"), {"status": "ok"}) == ["status: expected ok, got failed"]


def test_error_codes_compare_as_a_sorted_unique_set_when_given():
    got = envelope("partial", codes=["unreachable", "internal", "unreachable"])
    assert match_expect(got, {"status": "partial", "errorCodes": ["internal", "unreachable"]}) == []
    assert match_expect(got, {"status": "partial", "errorCodes": ["internal"]}) == [
        "errorCodes: expected ['internal'], got ['internal', 'unreachable']"
    ]
    assert match_expect(got, {"status": "partial"}) == []  # not asserted when absent
    assert match_expect(envelope(), {"status": "ok", "errorCodes": []}) == []


# -- shims ------------------------------------------------------------------------------------


@pytest.fixture
def fake_bin(tmp_path):
    """A real `kubectl` (and `jq` stand-in, never shimmed) on a PATH of its own."""
    bin_dir = tmp_path / "realbin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(
        "#!/bin/sh\n"
        'echo "{\\"args\\": \\"$*\\"}"\n'
        'echo "warn: $1" >&2\n'
        'if [ "$1" = fail ]; then exit 4; fi\n'
        'if [ "$1" = stdin ]; then cat; fi\n'
        "exit 0\n"
    )
    kubectl.chmod(0o755)
    return bin_dir


def sh(session, script, *, path_tail="", **kw):
    env = {
        "PATH": os.pathsep.join(
            p for p in [*session.path_prepend, path_tail, "/usr/bin", "/bin"] if p
        ),
        **session.env,
    }
    return subprocess.run(["/bin/sh", "-c", script], env=env, capture_output=True, text=True, **kw)


def test_the_default_shim_set_is_the_documented_one():
    assert set(DEFAULT_SHIMS) == {
        "kubectl", "helm", "flux", "argocd", "git", "psql", "redis-cli", "curl", "az",
        "gcloud", "aws",
    }  # fmt: skip
    assert not {"jq", "yq", "python", "python3", "bash", "sh", "cat", "grep"} & set(DEFAULT_SHIMS)


def test_record_runs_the_real_tool_and_logs_every_call(fake_bin):
    with ShimSession.record(["kubectl"], path=str(fake_bin)) as session:
        done = sh(
            session,
            'kubectl get pods; kubectl fail x; echo hi | kubectl stdin; echo "rc=$?"',
            path_tail=str(fake_bin),
        )
        calls = session.recorded()
    assert '{"args": "get pods"}' in done.stdout  # passed through
    assert "warn: get" in done.stderr
    assert [c["argv"] for c in calls] == [
        ["kubectl", "get", "pods"],
        ["kubectl", "fail", "x"],
        ["kubectl", "stdin"],
    ]
    assert [c["rc"] for c in calls] == [0, 4, 0]
    assert calls[0]["stdout"] == '{"args": "get pods"}\n'
    assert calls[0]["stderr"] == "warn: get\n"
    assert "stdin" not in calls[0]
    assert calls[2]["stdin"] == "hi\n"
    assert calls[2]["stdout"].endswith("hi\n")


def test_record_only_shims_tools_that_exist(fake_bin):
    with ShimSession.record(["kubectl", "nonesuch-tool"], path=str(fake_bin)) as session:
        assert session.missing == ["nonesuch-tool"]
        names = sorted(p.name for p in Path(session.path_prepend[0]).iterdir() if p.is_file())
        assert "kubectl" in names
        assert "nonesuch-tool" not in names


def test_record_never_resolves_to_its_own_shim(fake_bin):
    with ShimSession.record(["kubectl"], path=str(fake_bin)) as session:
        done = sh(session, "kubectl get pods", path_tail=str(fake_bin))
        assert done.returncode == 0
        assert len(session.recorded()) == 1


CALLS = [
    {"argv": ["kubectl", "get", "pods"], "rc": 0, "stdout": "pods\n", "stderr": ""},
    {"argv": ["kubectl", "fail"], "rc": 4, "stdout": "", "stderr": "nope\n"},
]


def test_replay_answers_from_the_calls_in_order_without_the_real_tool():
    with ShimSession.replay(CALLS, ["kubectl"]) as session:
        done = sh(session, 'kubectl get pods; kubectl fail; echo "rc=$?"')
        assert done.stdout == "pods\nrc=4\n"
        assert done.stderr == "nope\n"
        assert session.mismatches() == []
        assert session.unconsumed() == 0


def test_replay_a_different_argv_is_a_loud_mismatch():
    with ShimSession.replay(CALLS, ["kubectl"]) as session:
        done = sh(session, "kubectl get services")
        assert done.returncode == 97
        assert done.stderr.strip() == (
            "E_FIXTURE_MISMATCH: expected kubectl get pods got kubectl get services"
        )
        (mismatch,) = session.mismatches()
        assert mismatch["index"] == 0
        assert mismatch["expected"] == ["kubectl", "get", "pods"]
        assert mismatch["got"] == ["kubectl", "get", "services"]
        assert session.unconsumed() == 2  # a mismatch consumes nothing


def test_replay_more_calls_than_recorded_is_a_mismatch():
    with ShimSession.replay(CALLS[:1], ["kubectl"]) as session:
        done = sh(session, "kubectl get pods; kubectl get pods")
        assert done.returncode == 97
        assert "E_FIXTURE_MISMATCH: expected no further call got kubectl get pods" in done.stderr
        assert session.mismatches()[0]["expected"] is None


def test_replay_reports_calls_that_were_never_made():
    with ShimSession.replay(CALLS, ["kubectl"]) as session:
        sh(session, "kubectl get pods")
        assert session.unconsumed() == 1


def test_replay_checks_stdin_only_when_the_call_has_it():
    calls = [
        {"argv": ["kubectl", "apply", "-f", "-"], "stdin": "kind: Pod\n", "rc": 0},
        {"argv": ["kubectl", "apply", "-f", "-"], "rc": 0},
    ]
    with ShimSession.replay(calls, ["kubectl"]) as session:
        assert sh(session, "echo 'kind: Pod' | kubectl apply -f -").returncode == 0
        assert sh(session, "echo anything | kubectl apply -f -").returncode == 0
    with ShimSession.replay(calls, ["kubectl"]) as session:
        done = sh(session, "echo 'kind: Service' | kubectl apply -f -")
        assert done.returncode == 97
        assert "E_FIXTURE_MISMATCH" in done.stderr
        assert "stdin" in done.stderr


def test_replay_handles_unicode_and_odd_arguments():
    calls = [{"argv": ["kubectl", "get", "naïve name", "-o", "jsonpath={.a}"], "stdout": "é\n"}]
    with ShimSession.replay(calls, ["kubectl"]) as session:
        done = sh(session, "kubectl get 'naïve name' -o 'jsonpath={.a}'")
        assert (done.returncode, done.stdout) == (0, "é\n")


def test_shim_env_names_are_reserved_and_the_dir_is_cleaned_up():
    from runwhen_capability.custom.manifest import is_reserved_env_name

    with ShimSession.replay(CALLS, ["kubectl"]) as session:
        assert session.env
        assert all(
            name.startswith("RW_SHIM_") and is_reserved_env_name(name) for name in session.env
        )
        shim_dir = Path(session.path_prepend[0])
        assert shim_dir.is_dir()
    assert not shim_dir.exists()


def test_a_tool_name_cannot_escape_the_shim_dir():
    for bad in ("../x", "a/b", "", ".", "..", "a b"):
        with pytest.raises(ValueError, match="tool name"):
            ShimSession.replay([], [bad])


def test_the_calls_file_is_valid_json(tmp_path):
    with ShimSession.replay(CALLS, ["kubectl"]) as session:
        assert json.loads(Path(session.env["RW_SHIM_CALLS"]).read_text()) == CALLS


# -- execute's shim channels -------------------------------------------------------------------


def _run_nothing(tmp_path, **kw):
    from runwhen_capability.plugin import compile_tree, load_dir
    from runwhen_capability.plugin.run import RunSpec, execute

    tree = load_dir(Path(__file__).parent / "fixtures" / "run-mini")
    task = next(t for t in compile_tree(tree)["tasks"] if t["name"] == "py-nothing")
    spec = RunSpec(task=task, files=tree.files, executable=tree.executable)
    return execute(spec, scope_dir=tmp_path / "scope", **kw)


def test_execute_only_takes_absolute_path_dirs_and_shim_names(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        _run_nothing(tmp_path, path_prepend=["relative/dir"])
    with pytest.raises(ValueError, match="shim_env may not set"):
        _run_nothing(tmp_path, shim_env={"RW_OUT": "/x"})
    with pytest.raises(ValueError, match="extra_env may not set"):
        _run_nothing(tmp_path, extra_env={"RW_SHIM_MODE": "replay"})
    with pytest.raises(ValueError, match="extra_env may not set"):
        _run_nothing(tmp_path, extra_env={"PATH": "/x"})


# -- $contains / $containsWhere -------------------------------------------------------------

ROWS = [{"name": "a", "n": 1, "tags": ["x"]}, {"name": "b", "n": 2, "tags": []}, 7, "s"]


@pytest.mark.parametrize(
    ("expected", "ok"),
    [
        # $contains: each item subset-matches some element, in any order
        ({"$contains": [{"name": "b"}, {"name": "a"}]}, True),
        ({"$contains": [{"name": "a"}, {"name": "a", "n": 1}]}, True),  # same element twice
        ({"$contains": [{"name": "a"}, {"name": "zzz"}]}, False),
        ({"$contains": [7, "s"]}, True),  # scalars whole
        ({"$contains": [8]}, False),
        ({"$contains": [{"tags": ["x"]}]}, True),  # a list inside an item is compared whole
        ({"$contains": [{"tags": []}]}, True),
        ({"$contains": [{"tags": ["y"]}]}, False),
        ({"$contains": []}, True),
        ({"$contains": [True]}, False),  # a bool is not the int 1/7
        ({"$contains": [{"name": "a", "n": 2}]}, False),
        # $containsWhere: some dict element subset-matches
        ({"$containsWhere": {"name": "b"}}, True),
        ({"$containsWhere": {"name": "b", "n": 2}}, True),
        ({"$containsWhere": {"name": "b", "n": 1}}, False),  # one element must match all
        ({"$containsWhere": {"name": "nope"}}, False),
        ({"$containsWhere": {"tags": []}}, True),
        # both on one dict: a conjunction
        ({"$contains": [7], "$containsWhere": {"name": "a"}}, True),
        ({"$contains": [7], "$containsWhere": {"name": "zzz"}}, False),
    ],
)
def test_list_matchers(expected, ok):
    problems = match_expect(
        envelope(outputs={"rows": ROWS}), {"status": "ok", "outputs": {"rows": expected}}
    )
    assert (problems == []) is ok, problems


def test_matcher_failure_lines_name_the_path_matcher_and_first_missing_item():
    got = envelope(outputs={"a": {"rows": ROWS}})
    problems = match_expect(
        got,
        {
            "status": "ok",
            "outputs": {"a": {"rows": {"$contains": [{"name": "a"}, {"name": "q"}, 9]}}},
        },
    )
    assert problems == ["outputs.a.rows: $contains: no element matches {'name': 'q'}"]
    problems = match_expect(
        got, {"status": "ok", "outputs": {"a": {"rows": {"$containsWhere": {"name": "q"}}}}}
    )
    assert problems == ["outputs.a.rows: $containsWhere: no element matches {'name': 'q'}"]


def test_matchers_need_a_list_to_look_in():
    got = envelope(outputs={"x": {"a": 1}, "s": "text"})
    problems = match_expect(
        got,
        {
            "status": "ok",
            "outputs": {"x": {"$contains": [1]}, "s": {"$containsWhere": {"a": 1}}},
        },
    )
    assert problems == [
        "outputs.x: $contains: expected a list, got {'a': 1}",
        "outputs.s: $containsWhere: expected a list, got 'text'",
    ]
    assert match_expect(envelope(), {"status": "ok", "outputs": {"gone": {"$contains": [1]}}}) == [
        "outputs.gone: missing"
    ]


def test_matchers_nest_at_any_depth_and_inside_items():
    got = envelope(
        outputs={"a": {"b": {"c": [{"k": [1, 2], "inner": [{"z": 1, "y": 2}]}], "d": 1}}}
    )
    expect = {
        "status": "ok",
        "outputs": {
            "a": {
                "b": {
                    "c": {"$contains": [{"inner": [{"z": 1, "y": 2}]}]},
                    "d": 1,
                }
            }
        },
    }
    assert match_expect(got, expect) == []
    # a matcher inside an item of $contains
    nested = {"status": "ok", "outputs": {"a": {"b": {"c": {"$containsWhere": {"k": [1, 2]}}}}}}
    assert match_expect(got, nested) == []
    deeper = {
        "status": "ok",
        "outputs": {"a": {"b": {"c": {"$contains": [{"inner": {"$containsWhere": {"z": 1}}}]}}}},
    }
    assert match_expect(got, deeper) == []
    deeper["outputs"]["a"]["b"]["c"]["$contains"][0]["inner"]["$containsWhere"] = {"z": 2}
    assert len(match_expect(got, deeper)) == 1


@pytest.mark.parametrize(
    ("outputs", "needle"),
    [
        ({"x": {"$contians": [1]}}, "expect.outputs.x: unknown matcher $contians"),
        ({"x": {"$": 1}}, "unknown matcher $"),
        ({"x": {"a": {"$nope": 1}}}, "expect.outputs.x.a: unknown matcher $nope"),
        ({"x": {"$contains": [1], "name": "a"}}, "expect.outputs.x: mixes"),
        ({"x": {"name": "a", "$containsWhere": {"a": 1}}}, "expect.outputs.x: mixes"),
        ({"x": {"$contains": 1}}, "expect.outputs.x: $contains wants a list"),
        ({"x": {"$containsWhere": [1]}}, "expect.outputs.x: $containsWhere wants an object"),
        ({"x": {"$containsWhere": {}}}, "expect.outputs.x: $containsWhere wants an object"),
        ({"x": {"$contains": [{"a": {"$zzz": 1}}]}}, "expect.outputs.x[0].a: unknown matcher $zzz"),
        ({"x": {"$containsWhere": {"a": {"$zzz": 1}}}}, "expect.outputs.x.a: unknown matcher $zzz"),
        ({"x": [{"$contains": [1]}]}, "expect.outputs.x[0]: a matcher inside a list"),
    ],
)
def test_a_bad_matcher_is_a_fixture_error(tmp_path, outputs, needle):
    import yaml as _yaml

    text = _yaml.safe_dump({"expect": {"status": "ok", "outputs": outputs}})
    with pytest.raises(FixtureError) as exc:
        load_scenario(write(tmp_path, text))
    assert needle in "\n".join(exc.value.errors)


def test_good_matchers_load(tmp_path):
    text = (
        "expect:\n  status: ok\n  outputs:\n    a: {$contains: [{x: 1}]}\n"
        "    b: {c: {$containsWhere: {y: 2}}}\n"
    )
    assert load_scenario(write(tmp_path, text)).expect["outputs"]["a"] == {"$contains": [{"x": 1}]}


# -- expect.artifacts -------------------------------------------------------------------------


def check_artifacts(spec, texts, declared=None):
    return match_expect(
        envelope(),
        {"status": "ok", "artifacts": spec},
        artifacts=texts,
        artifact_outputs=declared if declared is not None else list(texts),
    )


@pytest.mark.parametrize(
    ("spec", "ok"),
    [
        ({"contains": ["alpha", "beta"]}, True),
        ({"contains": ["alpha", "gamma"]}, False),
        ({"notContains": ["gamma"]}, True),
        ({"notContains": ["alpha"]}, False),
        ({"lines": {"min": 3, "max": 3}}, True),
        ({"lines": {"min": 4}}, False),
        ({"lines": {"max": 2}}, False),
        ({"lines": {"max": 10}}, True),
        ({"contains": ["alpha"], "notContains": ["zzz"], "lines": {"min": 1}}, True),
        ({}, True),  # just: it exists
    ],
)
def test_artifact_assertions(spec, ok):
    assert (check_artifacts({"dump": spec}, {"dump": "alpha\nbeta\nlast"}) == []) is ok


def test_artifact_failure_lines_say_what_and_where():
    texts = {"dump": "alpha\nbeta\n"}
    assert check_artifacts({"dump": {"contains": ["alpha", "gamma", "delta"]}}, texts) == [
        "artifacts.dump: missing text 'gamma'",
        "artifacts.dump: missing text 'delta'",
    ]
    assert check_artifacts({"dump": {"notContains": ["beta"]}}, texts) == [
        "artifacts.dump: contains forbidden text 'beta'"
    ]
    assert check_artifacts({"dump": {"lines": {"min": 5}}}, texts) == [
        "artifacts.dump: 2 lines, expected at least 5"
    ]
    assert check_artifacts({"dump": {"lines": {"max": 1}}}, texts) == [
        "artifacts.dump: 2 lines, expected at most 1"
    ]


def test_a_declared_artifact_the_task_did_not_produce_fails():
    assert check_artifacts({"dump": {}}, {}, declared=["dump"]) == [
        "artifacts.dump: the task did not produce this artifact"
    ]


def test_a_name_that_is_not_an_artifact_output_fails():
    assert check_artifacts({"nope": {}}, {"dump": "x"}, declared=["dump"]) == [
        "artifacts.nope: not an artifact output of this task (artifact outputs: dump)"
    ]
    assert check_artifacts({"nope": {}}, {}, declared=[]) == [
        "artifacts.nope: not an artifact output of this task (artifact outputs: none)"
    ]


@pytest.mark.parametrize(
    ("artifacts", "needle"),
    [
        ({"a": {"contains": "x"}}, "expect.artifacts.a.contains"),
        ({"a": {"nope": []}}, "unknown key(s) nope"),
        ({"a": {"lines": {"min": -1}}}, "expect.artifacts.a.lines.min"),
        ({"a": {"lines": {"low": 1}}}, "unknown key(s) low"),
        ({"a": {"lines": {"min": 5, "max": 2}}}, "expect.artifacts.a.lines: min 5 is above max 2"),
        ({"a": []}, "expect.artifacts.a"),
    ],
)
def test_a_bad_artifact_assertion_is_a_fixture_error(tmp_path, artifacts, needle):
    import yaml as _yaml

    text = _yaml.safe_dump({"expect": {"status": "ok", "artifacts": artifacts}})
    with pytest.raises(FixtureError) as exc:
        load_scenario(write(tmp_path, text))
    assert needle in "\n".join(exc.value.errors)


def test_artifact_text_is_compared_as_given_even_for_unicode():
    assert check_artifacts({"d": {"contains": ["é"]}}, {"d": "café\n"}) == []


# -- dryRun ---------------------------------------------------------------------------------------


def test_dry_run_loads_dumps_and_defaults_false(tmp_path):
    assert load_scenario(write(tmp_path, "expect: {status: ok}\n")).dry_run is False
    scenario = load_scenario(write(tmp_path, "dryRun: true\nexpect: {status: ok}\n", "d.yaml"))
    assert scenario.dry_run is True
    with pytest.raises(FixtureError, match="dryRun"):
        load_scenario(write(tmp_path, "dryRun: yes-please\nexpect: {status: ok}\n", "e.yaml"))
    kw = dict(resource=None, inputs={}, settings={}, identity={}, calls=[], expect={"status": "ok"})
    assert "dryRun" not in dump_scenario(**kw)
    text = dump_scenario(**kw, dry_run=True)
    assert "dryRun: true\n" in text
    assert load_scenario(write(tmp_path, text, "f.yaml")).dry_run is True


# -- now (the run's reference time) --------------------------------------------------------------


def test_now_loads_dumps_and_defaults_to_none(tmp_path):
    assert load_scenario(write(tmp_path, "expect: {status: ok}\n")).now is None
    text = 'now: "2026-11-04T09:14:02.123Z"\nexpect: {status: ok}\n'
    scenario = load_scenario(write(tmp_path, text, "n.yaml"))
    assert scenario.now == datetime(2026, 11, 4, 9, 14, 2, 123000, tzinfo=UTC)
    kw = dict(resource=None, inputs={}, settings={}, identity={}, calls=[], expect={"status": "ok"})
    assert "now" not in dump_scenario(**kw)
    dumped = dump_scenario(**kw, now=scenario.now)
    assert (
        'now: "2026-11-04T09:14:02.123Z"\n' in dumped
        or "now: '2026-11-04T09:14:02.123Z'\n" in dumped
    )
    assert load_scenario(write(tmp_path, dumped, "d.yaml")).now == scenario.now


def test_an_unquoted_now_is_still_text(tmp_path):
    # the contracts loader is YAML 1.2 core: no timestamp type, so quoting is optional
    text = "now: 2026-11-04T09:14:02.123Z\nexpect: {status: ok}\n"
    assert load_scenario(write(tmp_path, text)).now.year == 2026


@pytest.mark.parametrize(
    "value",
    ["yesterday", "2026-11-04T09:14:02", "2026-11-04T09:14:02+02:00", "2026-13-04T09:14:02Z"],
)
def test_now_must_be_a_utc_rfc3339_timestamp(tmp_path, value):
    with pytest.raises(FixtureError, match="now"):
        load_scenario(write(tmp_path, f'now: "{value}"\nexpect: {{status: ok}}\n'))


# -- k8s-proxy-get calls ---------------------------------------------------------------------

PROXY_CALL = {
    "type": "k8s-proxy-get",
    "request": {
        "namespace": "vault",
        "kind": "services",
        "name": "vault",
        "port": 8200,
        "path": "/v1/sys/health",
        "scheme": "http",
    },
    "response": {
        "status": 200,
        "headers": {"content-type": "application/json"},
        "body": '{"sealed":false}',
        "truncated": False,
        "location": None,
    },
}
KUBECTL_CALL = {"argv": ["kubectl", "get", "pods"], "rc": 0, "stdout": "pods\n", "stderr": ""}

MIXED = """\
calls:
  - argv: [kubectl, get, pods]
    stdout: "pods\\n"
  - type: k8s-proxy-get
    request: {namespace: vault, kind: services, name: vault, port: 8200, path: /v1/sys/health}
    response: {status: 200, body: "{}"}
  - type: k8s-proxy-get
    request: {namespace: vault, kind: pods, name: v-0, port: metrics, path: m, scheme: https}
    error: {code: unreachable, message: no route}
  - argv: [kubectl, get, events]
expect: {status: ok}
"""


def test_a_scenario_mixes_cli_calls_and_proxy_calls_with_defaults(tmp_path):
    scenario = load_scenario(write(tmp_path, MIXED))
    first, second, third, fourth = scenario.calls
    assert first == {"argv": ["kubectl", "get", "pods"], "rc": 0, "stdout": "pods\n", "stderr": ""}
    assert second == {
        "type": "k8s-proxy-get",
        "request": {
            "namespace": "vault",
            "kind": "services",
            "name": "vault",
            "port": 8200,
            "path": "/v1/sys/health",
            "scheme": "http",
        },
        "response": {
            "status": 200,
            "headers": {},
            "body": "{}",
            "truncated": False,
            "location": None,
        },
    }
    assert third["request"]["scheme"] == "https"
    assert third["error"] == {"code": "unreachable", "message": "no route"}
    assert "response" not in third
    assert fourth["argv"] == ["kubectl", "get", "events"]


def test_proxy_calls_round_trip_through_dump_and_load(tmp_path):
    calls = [KUBECTL_CALL, PROXY_CALL]
    text = dump_scenario(
        resource=None, inputs={}, settings={}, identity={}, calls=calls, expect={"status": "ok"}
    )
    assert "type: k8s-proxy-get" in text
    assert load_scenario(write(tmp_path, text)).calls == calls


@pytest.mark.parametrize(
    ("call", "needle"),
    [
        ("{type: k8s-proxy-get, response: {status: 200}}", "calls[0]: 'request' is a required"),
        (
            "{type: k8s-proxy-get, request: {namespace: a, kind: pods, name: b, port: 1}}",
            "calls[0].request: 'path' is a required",
        ),
        (
            "{type: k8s-proxy-get, request: {namespace: a, kind: nodes, name: b, port: 1, path: x},"
            " response: {status: 200}}",
            "calls[0].request.kind",
        ),
        (
            "{type: k8s-proxy-get, request: {namespace: a, kind: pods, name: b, port: 1, path: x,"
            " extra: 1}, response: {status: 200}}",
            "unknown key(s) extra",
        ),
        (
            "{type: k8s-proxy-get, request: {namespace: a, kind: pods, name: b, port: 1, path: x}}",
            "needs a response or an error",
        ),
        (
            "{type: k8s-proxy-get, request: {namespace: a, kind: pods, name: b, port: 1, path: x},"
            " response: {status: 200}, error: {code: timeout, message: m}}",
            "not both",
        ),
        (
            "{type: k8s-proxy-get, request: {namespace: a, kind: pods, name: b, port: 1, path: x},"
            " response: {status: ok}}",
            "calls[0].response.status",
        ),
        (
            "{type: k8s-proxy-get, request: {namespace: a, kind: pods, name: b, port: 1, path: x},"
            " error: {code: nope, message: m}}",
            "calls[0].error.code",
        ),
        ("{type: curl, argv: [x]}", "unknown key(s) type"),
        ("{type: k8s-proxy-get, argv: [kubectl]}", "unknown key(s) argv"),
    ],
)
def test_a_bad_proxy_call_says_what_is_wrong(tmp_path, call, needle):
    with pytest.raises(FixtureError) as exc:
        load_scenario(write(tmp_path, f"calls: [{call}]\nexpect: {{status: ok}}\n"))
    assert needle in "\n".join(exc.value.errors)


def proxy_cli(session, *args, path_tail="/usr/bin:/bin"):
    """`python -m runwhen_capability.k8s_proxy get ...` under the session's environment."""
    src = str(Path(__import__("runwhen_capability").__file__).parents[1])
    env = {
        "PATH": os.pathsep.join([*session.path_prepend, path_tail]),
        "PYTHONPATH": src,
        **{k: v for k, v in os.environ.items() if k.startswith("FAKE_KUBECTL")},
        **session.env,
    }
    return subprocess.run(
        [sys.executable, "-m", "runwhen_capability.k8s_proxy", "get", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_replay_serves_a_mixed_sequence_in_order_and_counts_both_kinds():
    calls = [KUBECTL_CALL, PROXY_CALL, {**KUBECTL_CALL, "argv": ["kubectl", "get", "events"]}]
    with ShimSession.replay(calls, ["kubectl"]) as session:
        assert sh(session, "kubectl get pods").stdout == "pods\n"
        assert session.unconsumed() == 2
        done = proxy_cli(session, "vault", "services", "vault", "8200", "/v1/sys/health")
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout) == PROXY_CALL["response"]
        assert session.unconsumed() == 1
        assert sh(session, "kubectl get events").returncode == 0
        assert session.mismatches() == []
        assert session.unconsumed() == 0


def test_replay_starts_no_proxy_and_needs_no_kubectl_on_path():
    with ShimSession.replay([PROXY_CALL], []) as session:
        done = proxy_cli(
            session, "vault", "services", "vault", "8200", "/v1/sys/health", path_tail=""
        )
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout)["body"] == '{"sealed":false}'


def test_replay_a_proxy_call_where_a_cli_call_is_recorded_is_a_mismatch():
    with ShimSession.replay([KUBECTL_CALL, PROXY_CALL], ["kubectl"]) as session:
        done = proxy_cli(session, "vault", "services", "vault", "8200", "/v1/sys/health")
        assert done.returncode == 97
        assert "E_FIXTURE_MISMATCH: expected kubectl get pods got k8s-proxy-get" in done.stderr
        (mismatch,) = session.mismatches()
        assert mismatch["index"] == 0
        assert session.unconsumed() == 2  # a mismatch consumes nothing


def test_replay_a_cli_call_where_a_proxy_call_is_recorded_is_a_mismatch():
    with ShimSession.replay([PROXY_CALL, KUBECTL_CALL], ["kubectl"]) as session:
        done = sh(session, "kubectl get pods")
        assert done.returncode == 97
        assert (
            "E_FIXTURE_MISMATCH: expected k8s-proxy-get http vault/services/vault:8200 "
            "/v1/sys/health got kubectl get pods"
        ) in done.stderr
        (mismatch,) = session.mismatches()
        assert mismatch["index"] == 0 and "k8s-proxy-get" in mismatch["expected"]
        assert mismatch["got"] == ["kubectl", "get", "pods"]
        assert session.unconsumed() == 2


@pytest.mark.parametrize(
    "args",
    [
        ("vault", "services", "vault", "8200", "/v1/sys/seal-status"),
        ("vault", "pods", "vault", "8200", "/v1/sys/health"),
        ("other", "services", "vault", "8200", "/v1/sys/health"),
        ("vault", "services", "vault", "8201", "/v1/sys/health"),
        ("vault", "services", "vault", "8200", "/v1/sys/health", "--scheme", "https"),
    ],
)
def test_replay_a_different_proxy_request_is_a_mismatch(args):
    with ShimSession.replay([PROXY_CALL], []) as session:
        done = proxy_cli(session, *args)
        assert done.returncode == 97
        assert "E_FIXTURE_MISMATCH: expected k8s-proxy-get" in done.stderr
        assert "got k8s-proxy-get" in done.stderr
        assert len(session.mismatches()) == 1
        assert session.unconsumed() == 1


def test_replay_more_proxy_calls_than_recorded_is_a_mismatch():
    with ShimSession.replay([PROXY_CALL], []) as session:
        args = ("vault", "services", "vault", "8200", "/v1/sys/health")
        assert proxy_cli(session, *args).returncode == 0
        done = proxy_cli(session, *args)
        assert done.returncode == 97
        assert "expected no further call" in done.stderr


def test_replay_raises_a_recorded_proxy_error_again(tmp_path):
    error_call = {
        "type": "k8s-proxy-get",
        "request": PROXY_CALL["request"],
        "error": {"code": "permission_denied", "message": "the API server refused", "scope": "s"},
    }
    errors = tmp_path / "errors.jsonl"
    with ShimSession.replay([error_call], []) as session:
        src = str(Path(__import__("runwhen_capability").__file__).parents[1])
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "runwhen_capability.k8s_proxy",
                "get",
                "vault",
                "services",
                "vault",
                "8200",
                "/v1/sys/health",
            ],  # fmt: skip
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": src,
                "RW_ERRORS_FILE": str(errors),
                **session.env,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 1
        assert json.loads(errors.read_text())["code"] == "permission_denied"
        assert session.unconsumed() == 0


def test_record_logs_proxy_calls_between_cli_calls_in_order(fake_bin, tmp_path, monkeypatch):
    from k8s_proxy_fake import install_fake_kubectl

    path = "/api/v1/namespaces/vault/services/vault:8200/proxy/v1/sys/health"
    install_fake_kubectl(tmp_path, monkeypatch, routes={path: {"body": "{}"}})
    real_kubectl_dir = str(tmp_path / "fake-kubectl-bin")
    with ShimSession.record(["kubectl"], path=real_kubectl_dir) as session:
        sh(session, "kubectl get pods", path_tail=real_kubectl_dir)
        done = proxy_cli(
            session,
            "vault", "services", "vault", "8200", "/v1/sys/health",
            path_tail=f"{real_kubectl_dir}:/usr/bin:/bin",
        )  # fmt: skip
        assert done.returncode == 0, done.stderr
        sh(session, "kubectl get events", path_tail=real_kubectl_dir)
        calls = session.recorded()
    assert [c.get("type") or c["argv"][-1] for c in calls] == ["pods", "k8s-proxy-get", "events"]
    assert calls[1]["request"] == {
        "namespace": "vault",
        "kind": "services",
        "name": "vault",
        "port": "8200",
        "path": "/v1/sys/health",
        "scheme": "http",
    }
    assert calls[1]["response"]["status"] == 200 and calls[1]["response"]["body"] == "{}"
    # the proxy itself is not a recorded call: kubectl was started past the shim
    assert not any(c.get("argv", [""])[1:2] == ["proxy"] for c in calls)
