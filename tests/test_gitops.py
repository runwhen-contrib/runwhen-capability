"""`rwtask plan/apply/export/test` -- the GitOps client, mocked with
`responses` (the same tool test_serve.py uses for the relay). Covers the
local-validation gate, the plan/apply/export happy paths and their exit
codes, the platform API's `{errors: [...]}` shape, `--prune`, apply's
conflict/confirmation gates, and export's overwrite refusal.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import responses

from runwhen_capability.cli import main

API = "https://api.example.internal"
WORKSPACE = "demo"
BUNDLES = Path(__file__).parent / "fixtures" / "bundles"

PLAN_URL = f"{API}/api/v4/workspaces/{WORKSPACE}/custom-capabilities:plan"
APPLY_URL = f"{API}/api/v4/workspaces/{WORKSPACE}/custom-capabilities:apply"
EXPORT_URL = f"{API}/api/v4/workspaces/{WORKSPACE}/custom-capabilities:export"

CONFIG_ARGS = ["--api-url", API, "--token", "test-token", "--workspace", WORKSPACE]


def _write_minimal_capability(root: Path, name: str) -> Path:
    """A capability.yaml + task that passes validate() cleanly -- for
    tests that only exercise plan/apply's request/response plumbing, not
    task execution."""
    capability_dir = root / name
    (capability_dir / "tasks").mkdir(parents=True)
    (capability_dir / "capability.yaml").write_text(
        f"""apiVersion: runwhen.com/custom-capability/v1
name: {name}
tasks:
  - name: hello
    file: tasks/hello.py
    outputs:
      greeting: {{ schema: "string" }}
"""
    )
    (capability_dir / "tasks" / "hello.py").write_text(
        'def main(ctx):\n    return {"greeting": "hi"}\n'
    )
    return capability_dir


def _plan_response(name: str, action: str, **extra) -> dict:
    return {
        "capabilities": [
            {
                "name": name,
                "action": action,
                "content_hash": "sha256:abc",
                "diagnostics": [],
                "files": [{"path": "capability.yaml", "change": "added", "diff": "+name: " + name}],
                **extra,
            }
        ]
    }


# -- find_capability_dirs --------------------------------------------------------


def test_find_capability_dirs_finds_every_bundle_recursively(tmp_path):
    from runwhen_capability.gitops import find_capability_dirs

    _write_minimal_capability(tmp_path, "widget-a")
    _write_minimal_capability(tmp_path / "nested", "widget-b")

    dirs = find_capability_dirs(tmp_path)

    assert dirs == sorted([tmp_path / "widget-a", tmp_path / "nested" / "widget-b"])


# -- plan --------------------------------------------------------------------


@responses.activate
def test_plan_prints_actions_and_diffs_and_exits_2_on_changes(tmp_path, capsys):
    _write_minimal_capability(tmp_path, "widget-a")
    responses.add(responses.POST, PLAN_URL, json=_plan_response("widget-a", "create"), status=200)

    exit_code = main(["plan", str(tmp_path), *CONFIG_ARGS])

    assert exit_code == 2
    out = capsys.readouterr().out
    assert "widget-a: create" in out
    assert "added capability.yaml" in out
    assert "+name: widget-a" in out

    [call] = responses.calls
    assert call.request.headers["Authorization"] == "Bearer test-token"
    body = json.loads(call.request.body)
    assert body["prune"] is False
    [capability] = body["capabilities"]
    assert capability["name"] == "widget-a"
    assert {f["path"] for f in capability["files"]} == {"capability.yaml", "tasks/hello.py"}


@responses.activate
def test_plan_exits_0_when_nothing_changes(tmp_path, capsys):
    _write_minimal_capability(tmp_path, "widget-a")
    responses.add(
        responses.POST, PLAN_URL, json=_plan_response("widget-a", "unchanged"), status=200
    )

    exit_code = main(["plan", str(tmp_path), *CONFIG_ARGS])

    assert exit_code == 0
    assert "widget-a: unchanged" in capsys.readouterr().out


@responses.activate
def test_plan_sends_the_prune_flag(tmp_path):
    _write_minimal_capability(tmp_path, "widget-a")
    responses.add(
        responses.POST, PLAN_URL, json=_plan_response("widget-a", "unchanged"), status=200
    )

    main(["plan", str(tmp_path), "--prune", *CONFIG_ARGS])

    body = json.loads(responses.calls[0].request.body)
    assert body["prune"] is True


@responses.activate
def test_plan_local_validation_error_exits_1_and_never_calls_the_api(tmp_path, capsys):
    capability_dir = tmp_path / "broken"
    capability_dir.mkdir()
    (capability_dir / "capability.yaml").write_text(
        "apiVersion: runwhen.com/custom-capability/v1\nname: Not-A-Slug!\n"
    )

    exit_code = main(["plan", str(tmp_path), *CONFIG_ARGS])

    assert exit_code == 1
    assert len(responses.calls) == 0
    err = capsys.readouterr().err
    assert "E_MANIFEST_SCHEMA" in err
    assert "capability.yaml" in err


@responses.activate
def test_plan_maps_the_platform_api_error_shape(tmp_path, capsys):
    _write_minimal_capability(tmp_path, "widget-a")
    responses.add(
        responses.POST,
        PLAN_URL,
        json={
            "errors": [
                {
                    "code": "E_LIMIT",
                    "file": "capability.yaml",
                    "line": 3,
                    "message": "bundle is over the file-count limit",
                }
            ]
        },
        status=422,
    )

    exit_code = main(["plan", str(tmp_path), *CONFIG_ARGS])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "capability.yaml:3 E_LIMIT bundle is over the file-count limit" in err


def test_plan_needs_api_url_token_and_workspace(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("RW_API_URL", raising=False)
    monkeypatch.delenv("RW_API_TOKEN", raising=False)
    monkeypatch.delenv("RW_WORKSPACE", raising=False)
    _write_minimal_capability(tmp_path, "widget-a")

    with pytest.raises(SystemExit):
        main(["plan", str(tmp_path)])
    err = capsys.readouterr().err
    assert "RW_API_URL" in err and "RW_API_TOKEN" in err and "RW_WORKSPACE" in err


# -- apply --------------------------------------------------------------------


@responses.activate
def test_apply_conflict_without_adopt_refuses_and_never_calls_apply(tmp_path, capsys):
    _write_minimal_capability(tmp_path, "widget-a")
    responses.add(responses.POST, PLAN_URL, json=_plan_response("widget-a", "conflict"), status=200)

    exit_code = main(["apply", str(tmp_path), "-m", "msg", "--yes", *CONFIG_ARGS])

    assert exit_code == 1
    assert "--adopt" in capsys.readouterr().err
    assert len(responses.calls) == 1  # only :plan, never :apply


@responses.activate
def test_apply_with_adopt_and_yes_applies_and_exits_0(tmp_path, capsys):
    _write_minimal_capability(tmp_path, "widget-a")
    responses.add(responses.POST, PLAN_URL, json=_plan_response("widget-a", "conflict"), status=200)
    responses.add(responses.POST, APPLY_URL, json=_plan_response("widget-a", "create"), status=200)

    exit_code = main(
        ["apply", str(tmp_path), "-m", "took it over", "--adopt", "--yes", *CONFIG_ARGS]
    )

    assert exit_code == 0
    [plan_call, apply_call] = responses.calls
    assert plan_call.request.url == PLAN_URL
    assert apply_call.request.url == APPLY_URL
    body = json.loads(apply_call.request.body)
    assert body["message"] == "took it over"
    assert body["adopt"] is True


@responses.activate
def test_apply_skips_the_apply_call_when_nothing_changed(tmp_path, capsys):
    _write_minimal_capability(tmp_path, "widget-a")
    responses.add(
        responses.POST, PLAN_URL, json=_plan_response("widget-a", "unchanged"), status=200
    )

    exit_code = main(["apply", str(tmp_path), "-m", "msg", "--yes", *CONFIG_ARGS])

    assert exit_code == 0
    assert "nothing to apply" in capsys.readouterr().out
    assert len(responses.calls) == 1  # only :plan


class _FakeTTY:
    def isatty(self) -> bool:
        return True


@responses.activate
def test_apply_prompts_and_cancels_when_the_answer_is_no(tmp_path, capsys, monkeypatch):
    _write_minimal_capability(tmp_path, "widget-a")
    responses.add(responses.POST, PLAN_URL, json=_plan_response("widget-a", "create"), status=200)
    monkeypatch.setattr(sys, "stdin", _FakeTTY())
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    exit_code = main(["apply", str(tmp_path), "-m", "msg", *CONFIG_ARGS])

    assert exit_code == 1
    assert "apply cancelled" in capsys.readouterr().out
    assert len(responses.calls) == 1  # only :plan -- never applied


@responses.activate
def test_apply_prompts_and_proceeds_when_the_answer_is_yes(tmp_path, capsys, monkeypatch):
    _write_minimal_capability(tmp_path, "widget-a")
    responses.add(responses.POST, PLAN_URL, json=_plan_response("widget-a", "create"), status=200)
    responses.add(responses.POST, APPLY_URL, json=_plan_response("widget-a", "create"), status=200)
    monkeypatch.setattr(sys, "stdin", _FakeTTY())
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    exit_code = main(["apply", str(tmp_path), "-m", "msg", *CONFIG_ARGS])

    assert exit_code == 0
    assert len(responses.calls) == 2


@responses.activate
def test_apply_local_validation_error_exits_1_and_calls_nothing(tmp_path, capsys):
    capability_dir = tmp_path / "broken"
    capability_dir.mkdir()
    (capability_dir / "capability.yaml").write_text(
        "apiVersion: runwhen.com/custom-capability/v1\nname: Not-A-Slug!\n"
    )

    exit_code = main(["apply", str(tmp_path), "-m", "msg", "--yes", *CONFIG_ARGS])

    assert exit_code == 1
    assert len(responses.calls) == 0


# -- export -------------------------------------------------------------------


def _export_response() -> dict:
    return {
        "capabilities": [
            {
                "name": "widget-a",
                "managed_by": "git",
                "version": 3,
                "files": [
                    {
                        "path": "capability.yaml",
                        "content": "apiVersion: runwhen.com/custom-capability/v1\n",
                    }
                ],
            }
        ]
    }


@responses.activate
def test_export_writes_files_under_dir_name(tmp_path, capsys):
    responses.add(responses.GET, EXPORT_URL, json=_export_response(), status=200)

    exit_code = main(["export", str(tmp_path), *CONFIG_ARGS])

    assert exit_code == 0
    written = tmp_path / "widget-a" / "capability.yaml"
    assert written.read_text() == "apiVersion: runwhen.com/custom-capability/v1\n"
    assert "wrote" in capsys.readouterr().out


@responses.activate
def test_export_refuses_to_overwrite_a_changed_local_file(tmp_path, capsys):
    responses.add(responses.GET, EXPORT_URL, json=_export_response(), status=200)
    local = tmp_path / "widget-a" / "capability.yaml"
    local.parent.mkdir(parents=True)
    local.write_text("a hand edit that differs from the server\n")

    exit_code = main(["export", str(tmp_path), *CONFIG_ARGS])

    assert exit_code == 1
    assert "local changes" in capsys.readouterr().err
    assert local.read_text() == "a hand edit that differs from the server\n"  # untouched


@responses.activate
def test_export_force_overwrites_a_changed_local_file(tmp_path):
    responses.add(responses.GET, EXPORT_URL, json=_export_response(), status=200)
    local = tmp_path / "widget-a" / "capability.yaml"
    local.parent.mkdir(parents=True)
    local.write_text("a hand edit that differs from the server\n")

    exit_code = main(["export", str(tmp_path), "--force", *CONFIG_ARGS])

    assert exit_code == 0
    assert local.read_text() == "apiVersion: runwhen.com/custom-capability/v1\n"


@responses.activate
def test_export_unchanged_local_file_is_not_a_conflict(tmp_path):
    responses.add(responses.GET, EXPORT_URL, json=_export_response(), status=200)
    local = tmp_path / "widget-a" / "capability.yaml"
    local.parent.mkdir(parents=True)
    local.write_text("apiVersion: runwhen.com/custom-capability/v1\n")  # identical already

    exit_code = main(["export", str(tmp_path), *CONFIG_ARGS])

    assert exit_code == 0


@responses.activate
def test_export_name_flags_are_sent_as_a_comma_joined_query_param(tmp_path):
    responses.add(responses.GET, EXPORT_URL, json=_export_response(), status=200)

    main(["export", str(tmp_path), "--name", "widget-a", "--name", "widget-b", *CONFIG_ARGS])

    [call] = responses.calls
    query = parse_qs(urlsplit(call.request.url).query)
    assert query["names"] == ["widget-a,widget-b"]


# -- test -----------------------------------------------------------------------


def test_rwtask_test_runs_bash_and_python_fixtures_and_reports_a_summary(capsys):
    exit_code = main(["test", str(BUNDLES / "testable")])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "ok    testable/greet-py" in out
    assert "ok    testable/greet-bash" in out
    assert "ok    testable/boom" in out
    assert "testable/untested: no test" in out
    assert "3 passed, 0 failed, 1 no test" in out


def test_rwtask_test_task_flag_filters_to_one_task_across_capabilities(capsys):
    exit_code = main(["test", str(BUNDLES / "testable"), "--task", "greet-py"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "testable/greet-py" in out
    assert "greet-bash" not in out
    assert "boom" not in out


def test_rwtask_test_fails_and_exits_1_when_status_does_not_match_expect(tmp_path):
    import shutil

    bundle = tmp_path / "testable"
    shutil.copytree(BUNDLES / "testable", bundle)
    (bundle / "tests" / "boom.json").write_text(
        json.dumps({"inputs": {}, "expect": {"status": "ok"}})
    )

    exit_code = main(["test", str(bundle), "--task", "boom"])

    assert exit_code == 1
