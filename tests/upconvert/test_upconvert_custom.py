"""upconvert_custom: a runwhen.com/custom-capability/v1 bundle -> a V13 skill directory."""

from __future__ import annotations

import os

import pytest
from upconvert_helpers import (
    BUNDLES,
    codes,
    tasks_doc,
    validation_errors,
    with_code,
    write_tree,
)

from runwhen_capability.contracts import load_yaml, split_frontmatter
from runwhen_capability.upconvert import UpconvertError, upconvert_custom

BUNDLE_NAMES = sorted(p.name for p in BUNDLES.iterdir())


def frontmatter(result):
    return split_frontmatter(result.files["SKILL.md"].decode())


@pytest.mark.parametrize("bundle", BUNDLE_NAMES)
def test_every_fixture_bundle_converts_to_a_valid_skill(bundle, tmp_path):
    result = upconvert_custom(BUNDLES / bundle)
    assert validation_errors(result, tmp_path) == []
    assert set(result.files) >= {"SKILL.md", "tasks.yaml"}
    assert all(d.severity == "warning" for d in result.diagnostics)


def test_pgbouncer_health_golden_tasks_yaml():
    result = upconvert_custom(BUNDLES / "pgbouncer-health")
    assert result.skill_name == "pgbouncer-health"
    assert tasks_doc(result) == {
        "apiVersion": "runwhen.com/tasks/v1",
        "runtime": "kubernetes@1",
        "defaults": {
            "credentials": [
                "kubernetes/kubeconfig",
                {"type": "generic/env", "vars": ["STATS_DSN"]},
            ]
        },
        "tasks": [
            {
                "name": "pool-errors",
                "title": "Pool Errors",
                "description": "Count pool-exhaustion errors in the last window",
                "script": "scripts/pool_errors.sh",
                "readOnly": True,
                "inputs": {
                    "since": {"type": "duration", "default": "30m"},
                    "maxWait": {"type": "integer", "default": 20},
                },
                "outputs": {
                    "errorsDetail": {
                        "schema": "{ pattern: string, count: integer, firstSeen: datetime?, "
                        "lastSeen: datetime?, sample: string? }[]"
                    },
                    "summary": {
                        "schema": "{ windowMinutes: integer, pods: integer, total: integer }"
                    },
                    "detail": {"schema": "./schemas/detail.v1.json"},
                },
            }
        ],
    }


def test_pgbouncer_health_files_and_skill_md():
    result = upconvert_custom(BUNDLES / "pgbouncer-health")
    assert sorted(result.files) == [
        "SKILL.md",
        "lib/setup.py",
        "schemas/detail.v1.json",
        "scripts/pool_errors.sh",
        "tasks.yaml",
    ]
    assert (
        result.files["scripts/pool_errors.sh"]
        == (BUNDLES / "pgbouncer-health/tasks/pool_errors.sh").read_bytes()
    )
    meta, body = frontmatter(result)
    assert meta == {
        "name": "pgbouncer-health",
        "description": "Pool health for pgbouncer",
        "runwhen": {"appliesTo": {"types": ["kubernetes/deployment"]}},
    }
    assert body.strip().startswith("## When to use")
    assert "Pool health for pgbouncer" in body


def test_pgbouncer_health_findings():
    result = upconvert_custom(BUNDLES / "pgbouncer-health")
    lossy = with_code(result, "W_UPCONVERT_LOSSY")
    assert sorted(d.path for d in lossy) == ["appliesTo[0].where.namespace", "inputs.target"]
    assert {d.file for d in result.diagnostics} == {"capability.yaml", "scripts/pool_errors.sh"}
    [script] = [d for d in result.diagnostics if d.file == "scripts/pool_errors.sh"]
    assert (
        "$SINCE -> $RW_INPUT_SINCE" in script.message
    )  # the script reads inputs as custom tasks did
    [setup] = with_code(result, "W_UPCONVERT_SETUP_DROPPED")
    assert setup.path == "setup"
    credentials = with_code(result, "W_UPCONVERT_CREDENTIAL")
    assert sorted(d.path for d in credentials) == ["inputs.kubeconfig", "inputs.statsDsn"]
    review = " ".join(d.message for d in with_code(result, "W_UPCONVERT_REVIEW"))
    assert "title" in review
    assert "'errors' is renamed 'errorsDetail'" in review
    assert "schemas/detail.v1.json" in review


def test_exec_bundle_write_tasks_secret_and_resource_inputs(tmp_path):
    result = upconvert_custom(BUNDLES / "exec")
    doc = tasks_doc(result)
    assert doc["runtime"] == "kubernetes@1"  # a kubeconfig credential input
    assert doc["defaults"]["credentials"] == [
        "kubernetes/kubeconfig",
        {"type": "generic/env", "vars": ["TOKEN"]},
    ]
    by_name = {t["name"]: t for t in doc["tasks"]}
    ok_py = by_name["ok-py"]
    assert ok_py["readOnly"] is False
    assert ok_py["effects"] == ["Test fixture"]
    assert ok_py["destructive"] is False
    assert ok_py["script"] == "scripts/ok.py"
    assert "inputs" not in ok_py  # target (resource), token, kubeconfig all became credentials
    reviews = [d for d in with_code(result, "W_UPCONVERT_REVIEW") if "destructive" in d.message]
    assert len(reviews) == len(doc["tasks"])
    assert {d.path for d in with_code(result, "W_UPCONVERT_CREDENTIAL")} == {
        "inputs.token",
        "inputs.kubeconfig",
        "tasks[8].inputs.token",
        "tasks[11].inputs.token",
    }
    assert [d.path for d in with_code(result, "W_UPCONVERT_LOSSY")] == ["inputs.target"]
    assert validation_errors(result, tmp_path) == []


def test_a_script_that_reads_a_dropped_resource_input_gets_a_review():
    result = upconvert_custom(BUNDLES / "exec")
    files = {d.file for d in with_code(result, "W_UPCONVERT_REVIEW")}
    assert "scripts/target_echo.py" in files  # def main(ctx, target)
    assert "scripts/ok.py" not in files


def test_a_secret_input_is_a_generic_env_credential_on_the_tasks_that_declare_it():
    result = upconvert_custom(BUNDLES / "artifacts")
    doc = tasks_doc(result)
    assert doc["runtime"] == "generic@1"
    with_creds = {t["name"]: t["credentials"] for t in doc["tasks"] if "credentials" in t}
    assert with_creds == {"spill-secret": {"type": "generic/env", "vars": ["TOKEN"]}} or (
        with_creds == {"spill-secret": [{"type": "generic/env", "vars": ["TOKEN"]}]}
    )
    assert "defaults" not in doc


def test_artifact_outputs_and_snake_case_inputs():
    result = upconvert_custom(BUNDLES / "artifacts")
    by_name = {t["name"]: t for t in tasks_doc(result)["tasks"]}
    assert by_name["dump-pipe"]["outputs"] == {
        "dump": {"artifact": {"contentType": "text/plain", "maxSize": "2Gi"}}
    }
    assert by_name["dump-dir"]["outputs"]["dump"] == {
        "artifact": {"contentType": "application/octet-stream"}
    }
    assert by_name["spill-and-inline"]["inputs"] == {
        "inlineCount": {"type": "integer", "default": 3}
    }
    renamed = [d for d in with_code(result, "W_UPCONVERT_REVIEW") if "inline_count" in d.message]
    assert len(renamed) == 1


def test_tests_directory_and_unrelated_files_are_copied():
    result = upconvert_custom(BUNDLES / "testable")
    assert {"tests/boom.json", "tests/greet-bash.json", "tests/greet-py.json"} <= set(result.files)
    assert result.files["tests/boom.json"] == (BUNDLES / "testable/tests/boom.json").read_bytes()


def test_testable_golden_greet_task():
    doc = tasks_doc(upconvert_custom(BUNDLES / "testable"))
    greet = next(t for t in doc["tasks"] if t["name"] == "greet-py")
    assert greet == {
        "name": "greet-py",
        "title": "Greet Py",
        "description": "Greet Py",
        "script": "scripts/greet.py",
        "readOnly": False,
        "effects": ["Test fixture"],
        "destructive": False,
        "inputs": {"name": {"type": "string", "default": "world"}},
        "outputs": {"greeting": {"schema": "string"}},
    }


MINIMAL = """\
apiVersion: runwhen.com/custom-capability/v1
name: mini-check
description: A small read-only check
appliesTo:
  - {platform: Kubernetes, type: Deployment, where: {labels.app: web, labels.tier: front}}
  - {platform: kubernetes, type: statefulset, where: {labels.tier: front, labels.app: web}}
inputs:
  level: {type: "enum(low|high)", default: low}
  window: {type: duration, optional: true}
  pods: {type: "string[]", optional: true, default: []}
tasks:
  - name: look
    file: tasks/look.sh
    description: Look around
    readOnly: true
    outputs:
      seen: {schema: "integer"}
"""


def test_labels_and_input_mapping(tmp_path):
    root = write_tree(
        tmp_path / "mini",
        {
            "capability.yaml": MINIMAL,
            "tasks/look.sh": 'source "$RW_SDK/rw.sh"\nrw_set seen 1\n',
            "README.md": "# Mini check\n\nLong form docs.\n",
        },
    )
    result = upconvert_custom(root)
    meta, body = frontmatter(result)
    assert meta["runwhen"]["appliesTo"] == {
        "types": ["kubernetes/deployment", "kubernetes/statefulset"],
        "labels": {"app": "web", "tier": "front"},
    }
    assert body.strip().startswith("# Mini check")  # the README is the body
    [task] = tasks_doc(result)["tasks"]
    assert task["inputs"] == {
        "level": {"type": "enum", "enum": ["low", "high"], "default": "low"},
        "window": {"type": "duration", "required": False},
        "pods": {"type": "string[]", "default": []},
    }
    assert "effects" not in task and "destructive" not in task
    assert not with_code(result, "W_UPCONVERT_LOSSY")
    assert validation_errors(result, tmp_path) == []


def test_entries_that_differ_in_labels_keep_only_the_types():
    manifest = MINIMAL.replace("labels.tier: front, labels.app: web", "labels.app: api")
    result = upconvert_custom_text(manifest)
    meta, _ = frontmatter(result)
    assert meta["runwhen"]["appliesTo"] == {
        "types": ["kubernetes/deployment", "kubernetes/statefulset"]
    }
    [lossy] = with_code(result, "W_UPCONVERT_LOSSY")
    assert lossy.path == "appliesTo[1].where"


def upconvert_custom_text(manifest: str):
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = write_tree(
            Path(tmp) / "mini",
            {"capability.yaml": manifest, "tasks/look.sh": "rw_set seen 1\n"},
        )
        return upconvert_custom(root)


def test_a_bash_script_reading_a_kept_input_bare_gets_a_review(tmp_path):
    root = write_tree(
        tmp_path / "mini",
        {"capability.yaml": MINIMAL, "tasks/look.sh": 'echo "$LEVEL" ${WINDOW}\n'},
    )
    result = upconvert_custom(root)
    [review] = [d for d in with_code(result, "W_UPCONVERT_REVIEW") if d.file == "scripts/look.sh"]
    assert "RW_INPUT_LEVEL" in review.message and "RW_INPUT_WINDOW" in review.message


def test_the_exec_bit_is_kept(tmp_path):
    root = write_tree(
        tmp_path / "mini",
        {"capability.yaml": MINIMAL, "tasks/look.sh": "x\n", "lib/helper.sh": "y\n"},
    )
    os.chmod(root / "tasks/look.sh", 0o755)
    result = upconvert_custom(root)
    assert result.executable == frozenset({"scripts/look.sh"})


def test_a_missing_description_gets_a_stand_in_and_a_review(tmp_path):
    manifest = MINIMAL.replace("description: A small read-only check\n", "")
    root = write_tree(tmp_path / "mini", {"capability.yaml": manifest, "tasks/look.sh": "x\n"})
    result = upconvert_custom(root)
    meta, body = frontmatter(result)
    assert meta["description"] == "Mini Check"
    assert validation_errors(result, tmp_path) == []
    assert any("description" in d.message for d in with_code(result, "W_UPCONVERT_REVIEW"))


def test_the_name_argument_overrides_the_skill_name(tmp_path):
    result = upconvert_custom(BUNDLES / "testable", name="renamed")
    assert result.skill_name == "renamed"
    assert frontmatter(result)[0]["name"] == "renamed"
    assert validation_errors(result, tmp_path) == []


def test_codes_are_only_the_four_upconvert_warnings():
    allowed = {
        "W_UPCONVERT_LOSSY",
        "W_UPCONVERT_REVIEW",
        "W_UPCONVERT_CREDENTIAL",
        "W_UPCONVERT_SETUP_DROPPED",
    }
    for bundle in BUNDLE_NAMES:
        assert set(codes(upconvert_custom(BUNDLES / bundle))) <= allowed


def test_a_directory_without_capability_yaml_is_refused(tmp_path):
    with pytest.raises(UpconvertError, match="capability.yaml"):
        upconvert_custom(tmp_path)


def test_a_manifest_of_another_api_version_is_refused(tmp_path):
    root = write_tree(
        tmp_path / "x",
        {"capability.yaml": "apiVersion: something/else\nname: x\ntasks: []\n"},
    )
    with pytest.raises(UpconvertError, match="apiVersion"):
        upconvert_custom(root)


def test_a_task_file_that_is_not_there_is_refused(tmp_path):
    root = write_tree(tmp_path / "x", {"capability.yaml": MINIMAL})
    with pytest.raises(UpconvertError, match="tasks/look.sh"):
        upconvert_custom(root)


def test_the_result_is_not_written_by_the_converter(tmp_path):
    before = sorted(p.name for p in BUNDLES.iterdir())
    upconvert_custom(BUNDLES / "testable")
    assert sorted(p.name for p in BUNDLES.iterdir()) == before
    assert load_yaml("a: 1") == {"a": 1}
