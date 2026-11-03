"""ctx.run's environment merge -- the default (inherit the parent process's
environment) and `inherit_env=False` (an exact allow-list, no `os.environ`
in the child at all).
"""

from __future__ import annotations

import json
import sys

import pytest

from runwhen_capability import Context
from runwhen_capability.models import InputArtifactDecl, OutputDecl
from runwhen_capability.rwerrors import RwCommandError, RwError

# Interpreter startup itself may add a variable or two to its OWN environ
# (e.g. CPython's locale coercion, or a macOS CoreFoundation runtime var) --
# an artifact of the fresh process, not a leak of the parent's env. So the
# assertion below checks the one thing `inherit_env=False` actually
# promises: the passed mapping arrives, and a parent-only variable does not.
_CHECK_SCRIPT = "import os; print(os.environ.get('ONLY_ME', ''), 'RW_CHECKS_SECRET' in os.environ)"


def test_run_inherit_env_false_gives_the_child_exactly_the_passed_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("RW_CHECKS_SECRET", "leaked-if-inherited")
    ctx = Context(capability="rw-checks", operation="test", workdir=tmp_path)

    proc = ctx.run([sys.executable, "-c", _CHECK_SCRIPT], env={"ONLY_ME": "1"}, inherit_env=False)

    assert proc.stdout.strip() == "1 False"


def test_run_default_still_inherits_the_parent_environment(tmp_path, monkeypatch):
    """Most ctx.run callers rely on this -- inherit_env's default must stay
    True."""
    monkeypatch.setenv("RW_CHECKS_MARKER", "present")
    ctx = Context(capability="rw-worktree", operation="test", workdir=tmp_path)

    proc = ctx.run(
        [sys.executable, "-c", "import os; print(os.environ.get('RW_CHECKS_MARKER', ''))"],
        env={"EXTRA": "1"},
    )

    assert proc.stdout.strip() == "present"


def test_storage_put_points_to_artifacts(tmp_path):
    ctx = Context(capability="rw-checks", operation="test", workdir=tmp_path)

    with pytest.raises(NotImplementedError, match=r"use ctx\.artifacts"):
        ctx.storage.put("report.txt", b"data")


def test_context_artifact_wiring_defaults_to_none(tmp_path):
    ctx = Context(capability="rw-checks", operation="test", workdir=tmp_path)

    assert ctx.relay is None
    assert ctx.task is None
    assert ctx.outputs == []
    assert ctx.input_artifacts == []
    assert ctx.artifacts is not None


def test_context_carries_the_relay_task_outputs_and_input_artifacts(tmp_path):
    relay = object()
    outputs = [OutputDecl(task="scan", name="report.txt", artifact=True)]
    inputs = [InputArtifactDecl(input="prior", sizeBytes=3, sha256="ab" * 32)]

    ctx = Context(
        capability="rw-checks",
        operation="scan",
        workdir=tmp_path,
        relay=relay,
        task="scan",
        outputs=outputs,
        input_artifacts=inputs,
    )

    assert ctx.relay is relay
    assert ctx.task == "scan"
    assert ctx.outputs == outputs
    assert ctx.input_artifacts == inputs


# -- CAP-5: ctx.dry_run / resource / settings / error ------------------------------


def _ctx(tmp_path):
    return Context(capability="c", operation="t", workdir=tmp_path)


def test_dry_run_is_false_unless_rw_dry_run_is_exactly_1(tmp_path, monkeypatch):
    monkeypatch.delenv("RW_DRY_RUN", raising=False)
    assert _ctx(tmp_path).dry_run is False
    for value in ("0", "", "true", "yes"):
        monkeypatch.setenv("RW_DRY_RUN", value)
        assert _ctx(tmp_path).dry_run is False
    monkeypatch.setenv("RW_DRY_RUN", "1")
    assert _ctx(tmp_path).dry_run is True


def test_resource_is_none_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("RW_RESOURCE_JSON", raising=False)
    assert _ctx(tmp_path).resource is None


def test_resource_exposes_path_type_and_identity_as_attributes(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RW_RESOURCE_JSON",
        json.dumps(
            {
                "path": "kubernetes/clusters/prod/namespaces/web",
                "type": "kubernetes/namespace",
                "cluster": "prod",
                "namespace": "web",
            }
        ),
    )
    resource = _ctx(tmp_path).resource
    assert resource.path == "kubernetes/clusters/prod/namespaces/web"
    assert resource.type == "kubernetes/namespace"
    assert (resource.cluster, resource.namespace) == ("prod", "web")
    assert resource["cluster"] == "prod"
    assert dict(resource)["namespace"] == "web"
    with pytest.raises(AttributeError, match="no_such"):
        resource.no_such
    with pytest.raises(AttributeError):
        resource.cluster = "other"


def test_settings_are_empty_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("RW_SETTINGS_JSON", raising=False)
    settings = _ctx(tmp_path).settings
    assert len(settings) == 0
    assert not settings
    with pytest.raises(AttributeError):
        settings.anything


def test_settings_use_snake_case_attribute_names(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RW_SETTINGS_JSON",
        json.dumps({"latencyThresholdMs": 250, "already_snake": True, "nested": {"maxWait": "5s"}}),
    )
    settings = _ctx(tmp_path).settings
    assert settings.latency_threshold_ms == 250
    assert settings.already_snake is True
    assert settings["latency_threshold_ms"] == 250
    assert set(settings) == {"latency_threshold_ms", "already_snake", "nested"}


def test_settings_snake_case_only_the_top_level_names(tmp_path, monkeypatch):
    patterns = {"connRefused": "refused|ECONNREFUSED", "http5xx": "HTTP/1.1 5\\d\\d"}
    monkeypatch.setenv(
        "RW_SETTINGS_JSON",
        json.dumps({"latencyThresholdMs": 250, "errorPatterns": patterns}),
    )
    settings = _ctx(tmp_path).settings
    assert settings.latency_threshold_ms == 250
    assert settings.error_patterns["connRefused"] == patterns["connRefused"]
    assert settings.error_patterns.connRefused == patterns["connRefused"]
    assert settings.error_patterns.http5xx == patterns["http5xx"]
    assert set(settings.error_patterns) == {"connRefused", "http5xx"}
    assert dict(settings.error_patterns) == patterns
    with pytest.raises(AttributeError):
        settings.error_patterns.conn_refused
    with pytest.raises(AttributeError):
        settings.errorPatterns


def test_settings_keep_every_key_below_the_top_level_verbatim(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RW_SETTINGS_JSON",
        json.dumps(
            {
                "rulesList": [{"matchOn": "a", "maxCount": 1}, {"matchOn": "b", "deep": {"xY": 2}}],
                "labels": {"app.kubernetes.io/name": "web", "someKey": {"innerKey": [{"kK": 1}]}},
                "plainList": ["camelCase", 3],
            }
        ),
    )
    settings = _ctx(tmp_path).settings
    assert [dict(r) for r in settings.rules_list[:1]] == [{"matchOn": "a", "maxCount": 1}]
    assert settings.rules_list[1].matchOn == "b"
    assert settings.rules_list[1].deep.xY == 2
    assert settings.labels["app.kubernetes.io/name"] == "web"
    assert settings.labels.someKey.innerKey[0].kK == 1
    assert settings.plain_list == ["camelCase", 3]


@pytest.mark.parametrize("name", ["RW_RESOURCE_JSON", "RW_SETTINGS_JSON"])
@pytest.mark.parametrize("value", ["not json", "[1]", '"s"'])
def test_a_malformed_json_env_var_is_a_clear_error(tmp_path, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        _ctx(tmp_path)


def test_error_appends_to_ctx_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("RW_ERRORS_FILE", raising=False)
    ctx = _ctx(tmp_path)
    assert ctx.errors == []
    ctx.error("not_found", "no pod", scope="ns/a", hint="check", details={"n": 1})
    ctx.error("timeout", "slow")
    assert ctx.errors == [
        {
            "code": "not_found",
            "message": "no pod",
            "scope": "ns/a",
            "hint": "check",
            "details": {"n": 1},
        },
        {"code": "timeout", "message": "slow"},
    ]


def test_ctx_errors_is_a_copy(tmp_path, monkeypatch):
    monkeypatch.delenv("RW_ERRORS_FILE", raising=False)
    ctx = _ctx(tmp_path)
    ctx.error("timeout", "slow")
    ctx.errors.append({"code": "internal", "message": "forged"})
    ctx.errors[0]["message"] = "changed"
    assert ctx.errors == [{"code": "timeout", "message": "slow"}]


def test_error_writes_one_json_line_per_error_to_the_side_channel(tmp_path, monkeypatch):
    errors_file = tmp_path / "errors.jsonl"
    monkeypatch.setenv("RW_ERRORS_FILE", str(errors_file))
    ctx = _ctx(tmp_path)
    ctx.error("rate_limited", "slow down", scope="api")
    ctx.error("storage", "disk full\nsecond line")
    lines = errors_file.read_text().splitlines()
    assert [json.loads(line) for line in lines] == [
        {"code": "rate_limited", "message": "slow down", "scope": "api"},
        {"code": "storage", "message": "disk full\nsecond line"},
    ]


def test_an_invalid_code_is_recorded_as_internal_with_a_hint(tmp_path, monkeypatch):
    errors_file = tmp_path / "errors.jsonl"
    monkeypatch.setenv("RW_ERRORS_FILE", str(errors_file))
    ctx = _ctx(tmp_path)
    ctx.error("E_BOOM", "it broke")
    [error] = ctx.errors
    assert error["code"] == "internal"
    assert error["message"] == "it broke"
    assert "E_BOOM" in error["hint"]
    assert "permission_denied" in error["hint"]
    assert json.loads(errors_file.read_text()) == error


def test_error_with_an_unwritable_side_channel_still_records_in_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("RW_ERRORS_FILE", str(tmp_path / "no" / "such" / "dir" / "e.jsonl"))
    ctx = _ctx(tmp_path)
    ctx.error("timeout", "slow")
    assert ctx.errors == [{"code": "timeout", "message": "slow"}]


# -- CAP-5: ctx.run raising RwCommandError -------------------------------------------


def test_run_check_raises_a_classified_rw_command_error(tmp_path):
    ctx = _ctx(tmp_path)
    script = (
        "import sys; print('partial'); "
        "sys.stderr.write('Error from server (Forbidden): no\\n'); sys.exit(3)"
    )
    with pytest.raises(RwCommandError) as excinfo:
        ctx.run([sys.executable, "-c", script], check=True)
    err = excinfo.value
    assert isinstance(err, RwError)
    assert err.code == "permission_denied"
    assert err.returncode == 3
    assert "Forbidden" in err.stderr
    assert err.argv[0] == sys.executable
    assert "exited 3" in err.message
    assert "Forbidden" in err.message


def test_run_check_returns_normally_on_success(tmp_path):
    proc = _ctx(tmp_path).run([sys.executable, "-c", "print('ok')"], check=True)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "ok"


def test_run_without_check_still_returns_the_failed_process(tmp_path):
    """The existing contract: a non-zero exit is returned, not raised."""
    proc = _ctx(tmp_path).run([sys.executable, "-c", "raise SystemExit(5)"])
    assert proc.returncode == 5


def test_run_check_classifies_a_missing_binary_as_dependency(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(RwCommandError) as excinfo:
        ctx.run(["sh", "-c", "definitely-not-a-real-tool-xyz"], check=True)
    assert excinfo.value.code == "dependency"


def test_run_default_is_unchanged_without_the_v13_contract(tmp_path, monkeypatch):
    monkeypatch.delenv("RW_TASK_CONTRACT", raising=False)
    assert _ctx(tmp_path).run([sys.executable, "-c", "raise SystemExit(3)"]).returncode == 3
    monkeypatch.setenv("RW_TASK_CONTRACT", "v12")
    assert _ctx(tmp_path).run([sys.executable, "-c", "raise SystemExit(7)"]).returncode == 7


def test_run_raises_by_default_under_the_v13_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("RW_TASK_CONTRACT", "v13")
    script = (
        "import sys; sys.stderr.write('Error from server (NotFound): pods x not found'); "
        "sys.exit(1)"
    )
    with pytest.raises(RwCommandError) as excinfo:
        _ctx(tmp_path).run([sys.executable, "-c", script])
    assert excinfo.value.code == "not_found"
    assert excinfo.value.returncode == 1


def test_run_under_v13_returns_a_successful_process(tmp_path, monkeypatch):
    monkeypatch.setenv("RW_TASK_CONTRACT", "v13")
    assert _ctx(tmp_path).run([sys.executable, "-c", "print('hi')"]).stdout.strip() == "hi"


def test_an_explicit_check_wins_under_either_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("RW_TASK_CONTRACT", "v13")
    proc = _ctx(tmp_path).run([sys.executable, "-c", "raise SystemExit(4)"], check=False)
    assert proc.returncode == 4
    monkeypatch.delenv("RW_TASK_CONTRACT")
    with pytest.raises(RwCommandError):
        _ctx(tmp_path).run([sys.executable, "-c", "raise SystemExit(4)"], check=True)
