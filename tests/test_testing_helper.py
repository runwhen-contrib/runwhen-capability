"""runwhen_capability.testing: a real Context, built the way the V13 executor builds
it, for unit-testing skill scripts without a runner."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from runwhen_capability import RwCommandError, RwError
from runwhen_capability.contracts import check
from runwhen_capability.testing import make_context, run_main, stub_commands

RESOURCE = {
    "path": "kubernetes/clusters/kind/namespaces/web/deployments/api",
    "type": "kubernetes/deployment",
    "cluster": "kind",
    "namespace": "web",
    "name": "api",
}
GET_PODS = ("kubectl", "get", "pods", "-n", "web", "-o", "json")


def main(ctx, limit=5):
    """A skill script touching resource, settings, error and run."""
    out = ctx.run(list(GET_PODS))
    pods = json.loads(out.stdout)["items"][:limit]
    if len(pods) > ctx.settings.max_pods:
        ctx.error("internal", f"too many pods in {ctx.resource.namespace}", scope=ctx.resource.path)
    return {
        "pods": [p["name"] for p in pods],
        "pattern": ctx.settings.patterns["connRefused"],
        "dry": ctx.dry_run,
    }


def _tc(**kw):
    kw.setdefault("resource", RESOURCE)
    kw.setdefault("settings", {"maxPods": 1, "patterns": {"connRefused": "refused"}})
    return make_context(**kw)


def test_a_script_gets_the_right_envelope_from_resource_settings_error_and_run():
    pods = json.dumps({"items": [{"name": "a"}, {"name": "b"}]})
    with _tc() as tc:
        stub_commands(tc, {GET_PODS: (0, pods, "")})
        env = run_main(main, tc, limit=2)
    check("result-envelope", env)
    assert env["status"] == "partial"
    assert env["outputs"] == {"pods": ["a", "b"], "pattern": "refused", "dry": False}
    assert env["errors"] == [
        {
            "code": "internal",
            "message": "too many pods in web",
            "scope": "kubernetes/clusters/kind/namespaces/web/deployments/api",
        }
    ]


def test_settings_are_snake_cased_at_the_top_level_only_and_resource_is_verbatim():
    with _tc(settings={"latencyThresholdMs": 5, "patterns": {"http5xx": "x"}}) as tc:
        ctx = tc.ctx
        assert ctx.settings.latency_threshold_ms == 5
        assert ctx.settings.patterns.http5xx == "x"
        assert ctx.resource.path == RESOURCE["path"]
        assert ctx.resource.type == "kubernetes/deployment"
        assert ctx.resource.namespace == "web"


def test_no_resource_means_ctx_resource_is_none_even_if_the_outer_env_has_one(monkeypatch):
    monkeypatch.setenv("RW_RESOURCE_JSON", json.dumps({"path": "p", "type": "t"}))
    with make_context() as tc:
        assert tc.ctx.resource is None
        assert len(tc.ctx.settings) == 0


def test_dry_run_flows_to_ctx_and_the_envelope():
    with _tc(dry_run=True) as tc:
        assert tc.ctx.dry_run is True
        assert tc.envelope({})["dryRun"] is True
    with _tc() as tc:
        assert "dryRun" not in tc.envelope({})


def test_an_rwcommanderror_ends_the_run_with_its_classified_code():
    def script(ctx):
        ctx.run(["kubectl", "get", "secrets"])
        return {"unreached": True}

    with _tc() as tc:
        stub_commands(
            tc,
            {("kubectl", "get", "secrets"): (1, "", 'Error: secrets is forbidden: User "x"')},
        )
        env = run_main(script, tc)
        assert tc.errors == env["errors"]
    check("result-envelope", env)
    assert env["status"] == "failed"
    assert env["outputs"] == {}
    [error] = env["errors"]
    assert error["code"] == "permission_denied"
    assert "kubectl exited 1" in error["message"]


def test_an_rwerror_is_recorded_like_the_entry_point_does():
    def script(ctx):
        raise RwError("not_found", "no such deployment", scope="ns/web", hint="check the name")

    with _tc() as tc:
        env = run_main(script, tc)
    assert env["status"] == "failed"
    assert env["errors"] == [
        {
            "code": "not_found",
            "message": "no such deployment",
            "scope": "ns/web",
            "hint": "check the name",
        }
    ]


def test_ctx_skip_gives_a_skipped_envelope():
    def script(ctx):
        ctx.skip("nothing to check")

    with _tc() as tc:
        env = run_main(script, tc)
    assert env["status"] == "skipped"
    assert env["reason"] == "nothing to check"


def test_any_other_exception_propagates():
    def script(ctx):
        raise ValueError("kaboom")

    with _tc() as tc, pytest.raises(ValueError, match="kaboom"):
        run_main(script, tc)


def test_an_unstubbed_command_is_an_assertion_error_naming_it():
    with _tc() as tc:
        stub_commands(tc, {("kubectl", "version"): (0, "", "")})
        with pytest.raises(AssertionError, match=r"kubectl.*get.*nodes"):
            tc.ctx.run(["kubectl", "get", "nodes"])


def test_stub_commands_never_spawn_a_process(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    with _tc() as tc:
        calls = stub_commands(tc, {("echo", "hi"): (0, "hi\n", "")})
        proc = tc.ctx.run(["echo", "hi"])
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "hi\n", "")
    assert calls == [("echo", "hi")]


def test_v13_ctx_run_raises_on_failure_and_explicit_check_false_returns():
    with _tc() as tc:
        stub_commands(tc, {("false",): (1, "", "")})
        with pytest.raises(RwCommandError) as info:
            tc.ctx.run(["false"])
        assert info.value.returncode == 1
        assert tc.ctx.run(["false"], check=False).returncode == 1


def test_without_v13_ctx_run_does_not_raise_by_default():
    with _tc(v13=False) as tc:
        stub_commands(tc, {("false",): (1, "out", "err")})
        proc = tc.ctx.run(["false"])
        assert (proc.returncode, proc.stdout, proc.stderr) == (1, "out", "err")
        with pytest.raises(RwCommandError):
            tc.ctx.run(["false"], check=True)


def test_artifact_lookup_follows_the_executors_rule():
    with _tc() as tc:
        assert tc.artifact("dump") is None
        (tc.out_dir / "dump.txt").write_text("a dump")
        (tc.out_dir / "scratch.txt").write_text("scratch")
        assert tc.artifact("dump") == tc.out_dir / "dump.txt"
        assert tc.artifact("scratch") == tc.out_dir / "scratch.txt"
        (tc.out_dir / "dump.json").write_text("{}")
        assert tc.artifact("dump") is None  # two candidates: ambiguous
        (tc.out_dir / "dump").write_text("exact")
        assert tc.artifact("dump") == tc.out_dir / "dump"  # the exact name wins


def test_a_script_can_write_its_artifact_through_rw_out():
    def script(ctx):
        Path(os.environ["RW_OUT"], "report.txt").write_text("hello")
        return {}

    with _tc() as tc:
        run_main(script, tc)
        assert tc.artifact("report").read_text() == "hello"


def test_the_error_side_channel_is_the_source_of_tc_errors():
    with _tc() as tc:
        tc.ctx.error("timeout", "slow")
        lines = (tc.out_dir.parent / "errors.jsonl").read_text().splitlines()
        assert [json.loads(x) for x in lines] == [{"code": "timeout", "message": "slow"}]
        assert tc.errors == [{"code": "timeout", "message": "slow"}]
        env = tc.envelope({"x": 1})
        assert env["status"] == "partial"
        assert env["outputs"] == {"x": 1}


def test_a_failing_envelope_with_no_errors_gets_an_internal_one():
    with _tc() as tc:
        env = tc.envelope({}, exit_code=3)
    assert env["status"] == "failed"
    assert env["errors"] == [{"code": "internal", "message": "process exited 3"}]


def test_the_environment_is_restored_after_make_context_and_run_main(monkeypatch):
    monkeypatch.setenv("RW_SETTINGS_JSON", '{"keep": 1}')
    monkeypatch.delenv("RW_DRY_RUN", raising=False)
    monkeypatch.setenv("RW_TASK_CONTRACT", "classic")
    before = dict(os.environ)
    seen = {}

    def script(ctx):
        seen.update({k: v for k, v in os.environ.items() if k.startswith("RW_")})
        return {}

    with _tc(dry_run=True) as tc:
        assert dict(os.environ) == before
        run_main(script, tc)
        assert dict(os.environ) == before
        with tc.environ():
            assert os.environ["RW_TASK_CONTRACT"] == "v13"
            assert os.environ["RW_OUT"] == str(tc.out_dir)
        assert dict(os.environ) == before
    assert dict(os.environ) == before
    # what the script saw while it ran
    assert seen["RW_TASK_CONTRACT"] == "v13"
    assert seen["RW_DRY_RUN"] == "1"
    assert json.loads(seen["RW_SETTINGS_JSON"])["maxPods"] == 1
    assert seen["RW_OUT"] == str(tc.out_dir)


def test_the_environment_is_restored_when_the_script_raises(monkeypatch):
    monkeypatch.delenv("RW_OUT", raising=False)
    before = dict(os.environ)

    def script(ctx):
        raise ValueError("boom")

    with _tc() as tc, pytest.raises(ValueError):
        run_main(script, tc)
    assert dict(os.environ) == before


def test_the_temp_directory_goes_away_with_the_context_manager_and_workdir_is_honoured(tmp_path):
    with make_context(workdir=tmp_path) as tc:
        assert tc.ctx.workdir == tmp_path
        out_dir = tc.out_dir
        assert out_dir.is_dir()
    assert not out_dir.exists()
    assert tmp_path.exists()
