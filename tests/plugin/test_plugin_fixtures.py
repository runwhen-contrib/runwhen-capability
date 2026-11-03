"""plugin/fixtures.py (scenario files, the expect matcher) and plugin/shims.py (PATH shims,
record and replay), without a plugin: the shims are driven by hand through a shell."""

from __future__ import annotations

import json
import os
import subprocess
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
