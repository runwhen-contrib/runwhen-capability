"""runwhen_capability.testing: a real Context, built the way the V13 executor builds
it, for unit-testing skill scripts without a runner."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
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


def test_make_context_sets_the_reference_time_once():
    before = datetime.now(UTC)
    with make_context() as tc:
        first = tc.ctx.now()
        assert first == tc.ctx.now()
        assert before.replace(microsecond=0) <= first <= datetime.now(UTC)
        with tc.environ():
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", os.environ["RW_NOW"])
            assert os.environ["RW_NOW_EPOCH"] == str(int(first.timestamp()))


def test_make_context_takes_an_explicit_now_and_a_script_sees_it():
    fixed = datetime(2026, 11, 4, 9, 14, 2, 123456, tzinfo=UTC)
    seen = {}

    def script(ctx):
        seen.update(env=os.environ["RW_NOW"], epoch=os.environ["RW_NOW_EPOCH"], now=ctx.now())
        return {}

    with make_context(now=fixed) as tc:
        run_main(script, tc)
    assert seen == {
        "env": "2026-11-04T09:14:02.123Z",
        "epoch": "1793783642",
        "now": datetime(2026, 11, 4, 9, 14, 2, 123000, tzinfo=UTC),
    }


def test_make_context_refuses_a_naive_now():
    with pytest.raises(ValueError, match="timezone"):
        make_context(now=datetime(2026, 11, 4, 9, 0, 0))


def test_the_reference_time_does_not_leak_into_the_environment(monkeypatch):
    monkeypatch.delenv("RW_NOW", raising=False)
    monkeypatch.delenv("RW_NOW_EPOCH", raising=False)
    with make_context(now=datetime(2026, 11, 4, tzinfo=UTC)):
        assert "RW_NOW" not in os.environ and "RW_NOW_EPOCH" not in os.environ


# -- stubbing ctx.k8s_proxy_get --------------------------------------------------------------

HEALTH_REQUEST = {
    "namespace": "web",
    "kind": "services",
    "name": "vault",
    "port": 8200,
    "path": "/v1/sys/health",
}


def proxy_main(ctx):
    """A script that probes twice and records what the helper said."""
    first = ctx.k8s_proxy_get("web", "services", "vault", 8200, "/v1/sys/health")
    second = ctx.k8s_proxy_get("web", "services", "vault", 8200, "/v1/sys/seal-status", timeout=5)
    return {"statuses": [first["status"], second["status"]], "body": first["body"]}


def test_k8s_proxy_get_is_answered_from_declared_calls_in_order_without_a_proxy():
    calls = [
        {"request": HEALTH_REQUEST, "response": {"status": 200, "body": '{"sealed":false}'}},
        {
            "request": {**HEALTH_REQUEST, "path": "/v1/sys/seal-status", "scheme": "http"},
            "response": {"status": 503, "headers": {"x": "y"}, "truncated": True},
        },
    ]
    with _tc() as tc:
        proxy = stub_commands(tc, k8s_proxy_get=calls)
        env = run_main(proxy_main, tc)
        assert [r["path"] for r in tc.k8s_proxy_requests] == [
            "/v1/sys/health",
            "/v1/sys/seal-status",
        ]
        assert tc.k8s_proxy_unconsumed() == 0
    assert proxy == []  # the argv list ctx.run was called with
    assert env["status"] == "ok"
    assert env["outputs"] == {"statuses": [200, 503], "body": '{"sealed":false}'}


def test_a_stubbed_call_returns_the_full_dict_with_defaults():
    got = {}

    def main(ctx):
        got.update(ctx.k8s_proxy_get("web", "services", "vault", 8200, "/v1/sys/health"))
        return {}

    with _tc() as tc:
        stub_commands(tc, k8s_proxy_get=[{"request": HEALTH_REQUEST, "response": {"status": 302}}])
        run_main(main, tc)
    assert got == {
        "status": 302,
        "headers": {},
        "body": "",
        "truncated": False,
        "location": None,
    }


def test_a_stubbed_error_is_raised_like_a_real_one():
    error = {"code": "permission_denied", "message": "the API server refused", "scope": "web/x"}
    with _tc() as tc:
        stub_commands(tc, k8s_proxy_get=[{"request": HEALTH_REQUEST, "error": error}])
        env = run_main(proxy_main, tc)
    assert env["status"] == "failed"
    assert [e["code"] for e in env["errors"]] == ["permission_denied"]


def test_a_different_or_extra_proxy_request_is_an_assertion_error_naming_both():
    other = {**HEALTH_REQUEST, "path": "/v1/sys/leader"}
    with _tc() as tc:
        stub_commands(tc, k8s_proxy_get=[{"request": other, "response": {"status": 200}}])
        with tc.environ(), pytest.raises(AssertionError) as raised:
            tc.ctx.k8s_proxy_get("web", "services", "vault", 8200, "/v1/sys/health")
        assert "k8s-proxy-get http web/services/vault:8200 /v1/sys/leader" in str(raised.value)
        assert "k8s-proxy-get http web/services/vault:8200 /v1/sys/health" in str(raised.value)
        assert tc.k8s_proxy_unconsumed() == 1  # a mismatch consumes nothing
    with _tc() as tc:
        stub_commands(tc, k8s_proxy_get=[])
        with pytest.raises(AssertionError, match="no further"):
            tc.ctx.k8s_proxy_get("web", "services", "vault", 8200, "/v1/sys/health")


def test_cli_stubs_alone_leave_ctx_k8s_proxy_get_to_the_real_helper(
    monkeypatch,
):
    with _tc() as tc:
        stub_commands(tc, {GET_PODS: (0, '{"items": []}', "")})  # CLI stubs only
        called = []
        monkeypatch.setattr(
            "runwhen_capability.k8s_proxy.task_get", lambda *a, **k: called.append(a) or {}
        )
        tc.ctx.k8s_proxy_get("web", "services", "vault", 8200, "x")
    assert called  # not stubbed, so the real helper would have run


def test_proxy_stubs_may_be_added_later_and_cli_stubs_still_work():
    with _tc() as tc:
        stub_commands(tc, {GET_PODS: (0, '{"items": []}', "")})
        stub_commands(tc, k8s_proxy_get=[{"request": HEALTH_REQUEST, "response": {"status": 200}}])
        assert tc.ctx.run(list(GET_PODS)).returncode == 0
        assert (
            tc.ctx.k8s_proxy_get("web", "services", "vault", 8200, "/v1/sys/health")["status"]
            == 200
        )


@pytest.mark.parametrize(
    "bad",
    [
        "not a dict",
        {"response": {"status": 200}},
        {"request": {"namespace": "web"}, "response": {"status": 200}},
        {"request": HEALTH_REQUEST},
        {"request": HEALTH_REQUEST, "response": {"status": 200}, "error": {"code": "timeout"}},
        {"request": HEALTH_REQUEST, "response": {"body": "no status"}},
        {"request": HEALTH_REQUEST, "error": {"code": "nope", "message": "m"}},
    ],
)
def test_a_malformed_proxy_stub_is_a_type_error(bad):
    with _tc() as tc, pytest.raises(TypeError):
        stub_commands(tc, k8s_proxy_get=[bad])


def test_the_scenario_call_shape_is_accepted_as_it_is():
    """A recorded `type: k8s-proxy-get` call can be pasted in as a stub."""
    call = {
        "type": "k8s-proxy-get",
        "request": {**HEALTH_REQUEST, "scheme": "http"},
        "response": {
            "status": 200,
            "headers": {},
            "body": "",
            "truncated": False,
            "location": None,
        },
    }
    with _tc() as tc:
        stub_commands(tc, k8s_proxy_get=[call])
        assert (
            tc.ctx.k8s_proxy_get("web", "services", "vault", 8200, "/v1/sys/health")["status"]
            == 200
        )


# -- stubbing ctx.k8s_proxy_request, and headers -----------------------------------------------

LOKI = ("web", "services", "loki", 3100)


def loki_request(method="PUT", path="/ring/forget", **extra):
    return {
        "namespace": "web",
        "kind": "services",
        "name": "loki",
        "port": 3100,
        "path": path,
        "method": method,
        **extra,
    }


def test_k8s_proxy_declares_requests_of_any_method_matched_in_order():
    calls = [
        {"request": loki_request("GET", "/ring"), "response": {"status": 200, "body": "ring"}},
        {
            "request": loki_request(
                body='{"id":"a"}', headers={"Content-Type": "application/json"}
            ),
            "response": {"status": 204},
        },
        {"request": loki_request("DELETE", "/ring/a"), "response": {"status": 202}},
    ]
    with _tc() as tc:
        proxy = stub_commands(tc, k8s_proxy=calls)
        assert proxy == []
        ctx = tc.ctx
        got = [
            ctx.k8s_proxy_get(*LOKI, "/ring")["body"],
            ctx.k8s_proxy_request(
                "PUT",
                *LOKI,
                "/ring/forget",
                body=b'{"id":"a"}',
                headers={"content-type": "application/json"},
            )["status"],
            ctx.k8s_proxy_request("DELETE", *LOKI, "/ring/a")["status"],
        ]
        assert got == ["ring", 204, 202]
        assert tc.k8s_proxy_unconsumed() == 0
        assert [r.get("method") for r in tc.k8s_proxy_requests] == [None, "PUT", "DELETE"]


def test_k8s_proxy_get_is_the_same_keyword_and_the_two_cannot_be_mixed_in_one_call():
    with _tc() as tc:
        stub_commands(
            tc,
            k8s_proxy_get=[{"request": loki_request("PUT"), "response": {"status": 204}}],
        )
        assert tc.ctx.k8s_proxy_request("PUT", *LOKI, "/ring/forget")["status"] == 204
    with _tc() as tc, pytest.raises(TypeError, match="not both"):
        stub_commands(tc, k8s_proxy=[], k8s_proxy_get=[])


def test_a_declared_get_answers_either_function_and_a_get_with_a_header_must_match_it():
    declared = {
        "request": loki_request("GET", "/ring", headers={"Accept": "application/json"}),
        "response": {"status": 200},
    }
    with _tc() as tc:
        stub_commands(tc, k8s_proxy=[declared, declared, declared])
        assert tc.ctx.k8s_proxy_get(*LOKI, "/ring", headers={"accept": "application/json"})
        assert tc.ctx.k8s_proxy_request(
            "GET", *LOKI, "/ring", headers={"ACCEPT": "application/json"}
        )
        with pytest.raises(AssertionError) as raised:
            tc.ctx.k8s_proxy_get(*LOKI, "/ring")  # the header is missing
        assert 'headers={"accept": "application/json"}' in str(raised.value)
        assert tc.k8s_proxy_unconsumed() == 1


@pytest.mark.parametrize(
    ("call", "needle"),
    [
        (
            lambda c: c.k8s_proxy_request("POST", *LOKI, "/ring/forget", body="{}"),
            "got k8s-proxy-request POST http web/services/loki:3100 /ring/forget body=",
        ),
        (
            lambda c: c.k8s_proxy_request("PUT", *LOKI, "/ring/forget", body="{ }"),
            'body="{ }"',
        ),
        (
            lambda c: c.k8s_proxy_request(
                "PUT", *LOKI, "/ring/forget", body="{}", headers={"Content-Type": "text/plain"}
            ),
            'headers={"content-type": "text/plain"}',
        ),
        (
            lambda c: c.k8s_proxy_request("PUT", *LOKI, "/ring/forget"),
            "got k8s-proxy-request PUT http web/services/loki:3100 /ring/forget",
        ),
        (
            lambda c: c.k8s_proxy_get(*LOKI, "/ring/forget"),
            "got k8s-proxy-get http web/services/loki:3100 /ring/forget",
        ),
    ],
)
def test_a_changed_method_body_or_header_is_an_assertion_error_naming_the_method(call, needle):
    declared = {
        "request": loki_request(body="{}", headers={"Content-Type": "application/json"}),
        "response": {"status": 204},
    }
    with _tc() as tc:
        stub_commands(tc, k8s_proxy=[declared])
        with pytest.raises(AssertionError) as raised:
            call(tc.ctx)
        message = str(raised.value)
        assert "expected k8s-proxy-request PUT http web/services/loki:3100 /ring/forget" in message
        assert needle in message
        assert tc.k8s_proxy_unconsumed() == 1  # a mismatch consumes nothing


def test_a_body_may_be_declared_as_base64_and_matches_the_bytes():
    declared = {
        "request": loki_request(bodyBase64="/wA="),  # b"\xff\x00"
        "response": {"status": 204},
    }
    with _tc() as tc:
        stub_commands(tc, k8s_proxy=[declared, declared])
        assert tc.ctx.k8s_proxy_request("PUT", *LOKI, "/ring/forget", body=b"\xff\x00")
        with pytest.raises(AssertionError):
            tc.ctx.k8s_proxy_request("PUT", *LOKI, "/ring/forget", body=b"\xff")


def test_the_stub_refuses_what_the_real_call_refuses_before_matching():
    with _tc() as tc:
        stub_commands(tc, k8s_proxy=[{"request": loki_request("GET"), "response": {"status": 200}}])
        for call in (
            lambda c: c.k8s_proxy_get(*LOKI, "/ring", headers={"Authorization": "x"}),
            lambda c: c.k8s_proxy_request("GET", *LOKI, "/ring", body="x"),
            lambda c: c.k8s_proxy_request("TRACE", *LOKI, "/ring"),
        ):
            with pytest.raises(RwError) as raised:
                call(tc.ctx)
            assert raised.value.code == "invalid_input"
        assert tc.k8s_proxy_unconsumed() == 1


@pytest.mark.parametrize(
    "request_extra",
    [
        {"method": "TRACE"},
        {"method": "GET", "body": "x"},
        {"headers": {"Authorization": "x"}},
        {"body": "x", "bodyBase64": "eA=="},
        {"bodyBase64": "***"},
        {"body": 5},
    ],
)
def test_a_malformed_request_stub_is_a_type_error(request_extra):
    bad = {"request": loki_request(**request_extra), "response": {"status": 200}}
    with _tc() as tc, pytest.raises(TypeError):
        stub_commands(tc, k8s_proxy=[bad])


def test_a_k8s_proxy_get_typed_stub_cannot_be_a_write():
    bad = {"type": "k8s-proxy-get", "request": loki_request("PUT"), "response": {"status": 200}}
    with _tc() as tc, pytest.raises(TypeError):
        stub_commands(tc, k8s_proxy=[bad])
    with _tc() as tc, pytest.raises(TypeError):
        stub_commands(tc, k8s_proxy=[{**bad, "type": "curl"}])


def test_the_recorded_request_call_shape_is_accepted_as_it_is():
    call = {
        "type": "k8s-proxy-request",
        "request": {**loki_request(body="{}"), "scheme": "http"},
        "response": {
            "status": 204,
            "headers": {},
            "body": "",
            "truncated": False,
            "location": None,
        },
    }
    with _tc() as tc:
        stub_commands(tc, k8s_proxy=[call])
        assert tc.ctx.k8s_proxy_request("PUT", *LOKI, "/ring/forget", body="{}")["status"] == 204
