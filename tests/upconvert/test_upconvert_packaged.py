"""upconvert_packaged: a capability/v1 directory (manifest.yaml + tasks.py) -> a V13 skill."""

from __future__ import annotations

import pytest
from upconvert_helpers import (
    CAPABILITIES,
    codes,
    tasks_doc,
    validation_errors,
    with_code,
    write_tree,
)

from runwhen_capability.contracts import split_frontmatter
from runwhen_capability.upconvert import UpconvertError, upconvert_packaged

CAPABILITY_NAMES = sorted(p.name for p in CAPABILITIES.iterdir())


@pytest.mark.parametrize("capability", CAPABILITY_NAMES)
def test_every_packaged_fixture_converts_to_a_valid_skill(capability, tmp_path):
    result = upconvert_packaged(CAPABILITIES / capability)
    assert validation_errors(result, tmp_path) == []
    assert "lib/tasks.py" in result.files
    assert result.files["lib/tasks.py"] == (CAPABILITIES / capability / "tasks.py").read_bytes()
    assert all(d.severity == "warning" for d in result.diagnostics)


def test_echo_golden():
    result = upconvert_packaged(CAPABILITIES / "echo")
    assert result.skill_name == "rw-echo-fixture"
    assert sorted(result.files) == ["SKILL.md", "lib/tasks.py", "scripts/echo.py", "tasks.yaml"]
    assert result.files["scripts/echo.py"].decode() == (
        "from tasks import echo\n\n\ndef main(ctx, **inputs):\n    return echo(ctx, **inputs)\n"
    )
    doc = tasks_doc(result)
    assert doc["apiVersion"] == "runwhen.com/tasks/v1"
    assert doc["runtime"] == "generic@1"
    [task] = doc["tasks"]
    assert task["name"] == "echo"
    assert task["script"] == "scripts/echo.py"
    assert task["readOnly"] is False  # packaged tasks default to read-write
    assert task["destructive"] is False and task["effects"]
    meta, _ = split_frontmatter(result.files["SKILL.md"].decode())
    assert meta["name"] == "rw-echo-fixture"
    assert "runwhen" not in meta  # the manifest has no appliesTo
    [setup] = with_code(result, "W_UPCONVERT_SETUP_DROPPED")
    assert setup.file == "manifest.yaml" and setup.path == "setup"


def test_a_task_name_with_an_underscore_becomes_a_slug_and_the_shim_imports_the_function():
    result = upconvert_packaged(CAPABILITIES / "needs_credential")
    [task] = tasks_doc(result)["tasks"]
    assert task["name"] == "need-token"
    assert task["script"] == "scripts/need_token.py"
    assert b"from tasks import need_token\n" in result.files["scripts/need_token.py"]
    assert any("need_token" in d.message for d in with_code(result, "W_UPCONVERT_REVIEW"))


def test_worktree_sync_lossy_execution_and_every_task_has_a_shim():
    result = upconvert_packaged(CAPABILITIES / "worktree_sync")
    assert {"scripts/read.py", "scripts/grep.py", "scripts/wreck.py"} <= set(result.files)
    lossy = with_code(result, "W_UPCONVERT_LOSSY")
    assert [d.path for d in lossy] == ["execution.mode"]


FULL = """\
apiVersion: capability/v1
capability: rw-checks
version: "0.3.0"
description: Static checks for source repositories
execution:
  mode: stateless
  maxPodsPerPool: 3
  requestTimeoutSeconds: 600
  resources: {cpuRequest: 250m, cpuLimit: "1", memoryRequest: 512Mi, memoryLimit: 1Gi}
appliesTo:
  - {platform: github, type: repository, where: {labels.team: core}}
needs:
  credentials:
    - {name: repo, kind: github.installation, access: "contents:read"}
    - {name: kube, kind: k8s.kubeconfig, access: read, optional: true}
  egress: [github.com]
setup:
  task: checkout
  inputs: {repoUrl: {from: request}, sha: {from: request}}
  outputs: {tree: {type: path}}
tasks:
  - name: ruff
    description: Python lint and static analysis
    readOnly: true
    invocation: queued
    inputs:
      tree: {from: "${setup.tree}"}
      baseSha: {from: request, optional: true, description: Compare against this}
      level: {from: request, type: "enum(a|b)"}
    outputs:
      findings: {kind: rw.findings.v1, schema: ./schemas/findings.v1.json}
      report: {kind: artifact, contentType: text/plain, maxSize: 10Mi, preview: true}
      blob: {kind: rw.blob.v1, schema: ./schemas/blob.json, spill: auto}
      stats: {kind: rw.stats.v1, schema: "{ files: integer }"}
retention: {pod: on_failure, result: 30d}
"""
TASKS_PY = (
    "from runwhen_capability import setup, task\n\n\n"
    "@task(outputs={})\ndef ruff(ctx, base_sha=None):\n    return {}\n"
)


def full_capability(tmp_path):
    return write_tree(
        tmp_path / "rw-checks",
        {
            "manifest.yaml": FULL,
            "tasks.py": TASKS_PY,
            "tools/__init__.py": "",
            "tools/run.py": "X = 1\n",
            "schemas/findings.v1.json": '{"type": "object"}',
            "schemas/blob.json": '{"type": "string"}',
        },
    )


def test_full_manifest_golden(tmp_path):
    result = upconvert_packaged(full_capability(tmp_path))
    assert tasks_doc(result) == {
        "apiVersion": "runwhen.com/tasks/v1",
        "runtime": "kubernetes@1",
        "defaults": {
            "credentials": [
                "kubernetes/kubeconfig",
                {"type": "generic/env", "vars": ["REPO"]},
            ],
            "timeout": "600s",
            "resources": {"cpu": "250m", "memory": "512Mi"},
        },
        "tasks": [
            {
                "name": "ruff",
                "title": "Ruff",
                "description": "Python lint and static analysis",
                "script": "scripts/ruff.py",
                "readOnly": True,
                "inputs": {
                    "baseSha": {
                        "type": "string",
                        "required": False,
                        "description": "Compare against this",
                    },
                    "level": {"type": "enum", "enum": ["a", "b"]},
                },
                "outputs": {
                    "findings": {"schema": "./schemas/findings.v1.json"},
                    "report": {"artifact": {"contentType": "text/plain", "maxSize": "10Mi"}},
                    "blob": {"schema": "./schemas/blob.v1.json"},
                    "stats": {"schema": "{ files: integer }"},
                },
            }
        ],
    }


def test_full_manifest_files_and_findings(tmp_path):
    result = upconvert_packaged(full_capability(tmp_path))
    assert sorted(result.files) == [
        "SKILL.md",
        "lib/tasks.py",
        "lib/tools/__init__.py",
        "lib/tools/run.py",
        "schemas/blob.v1.json",
        "schemas/findings.v1.json",
        "scripts/ruff.py",
        "tasks.yaml",
    ]
    meta, _ = split_frontmatter(result.files["SKILL.md"].decode())
    assert meta["runwhen"]["appliesTo"] == {
        "types": ["github/repository"],
        "labels": {"team": "core"},
    }
    lossy = sorted(d.path for d in with_code(result, "W_UPCONVERT_LOSSY"))
    assert lossy == [
        "execution.maxPodsPerPool",
        "execution.mode",
        "execution.resources",
        "needs.egress",
        "retention",
        "tasks[0].invocation",
    ]
    assert {d.path for d in with_code(result, "W_UPCONVERT_CREDENTIAL")} == {
        "needs.credentials[0]",
        "needs.credentials[1]",
    }
    setup = with_code(result, "W_UPCONVERT_SETUP_DROPPED")
    assert sorted(d.path for d in setup) == ["setup", "tasks[0].inputs.tree"]
    review = " ".join(d.message for d in with_code(result, "W_UPCONVERT_REVIEW"))
    assert "D63: no findings" in review


def test_full_manifest_validates_except_for_nothing_but_warnings(tmp_path):
    result = upconvert_packaged(full_capability(tmp_path))
    assert validation_errors(result, tmp_path) == []


def test_resource_sync_maps_to_runwhen_sync(tmp_path):
    manifest = (
        "capability: syncer\nneeds:\n  credentials:\n"
        "    - {name: sync, kind: runwhen.resourceSync, access: write}\n"
        "tasks:\n  - name: go\n    readOnly: true\n    description: Sync\n"
    )
    root = write_tree(
        tmp_path / "syncer", {"manifest.yaml": manifest, "tasks.py": "def go(ctx): ...\n"}
    )
    result = upconvert_packaged(root)
    assert tasks_doc(result)["defaults"]["credentials"] == "runwhen/sync"
    [warn] = with_code(result, "W_UPCONVERT_CREDENTIAL")
    assert "discovery" in warn.message  # valid only on an integration's discovery entry point


def test_a_name_argument_overrides_the_skill_name():
    result = upconvert_packaged(CAPABILITIES / "echo", name="my-echo")
    assert result.skill_name == "my-echo"
    meta, _ = split_frontmatter(result.files["SKILL.md"].decode())
    assert meta["name"] == "my-echo"


def test_only_the_four_upconvert_codes_are_emitted():
    allowed = {
        "W_UPCONVERT_LOSSY",
        "W_UPCONVERT_REVIEW",
        "W_UPCONVERT_CREDENTIAL",
        "W_UPCONVERT_SETUP_DROPPED",
    }
    for name in CAPABILITY_NAMES:
        assert set(codes(upconvert_packaged(CAPABILITIES / name))) <= allowed


def test_a_directory_without_a_manifest_or_tasks_py_is_refused(tmp_path):
    with pytest.raises(UpconvertError, match="manifest.yaml"):
        upconvert_packaged(tmp_path)
    root = write_tree(tmp_path / "x", {"manifest.yaml": "capability: x\ntasks: []\n"})
    with pytest.raises(UpconvertError, match="tasks.py"):
        upconvert_packaged(root)
