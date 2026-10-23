"""The manifest models: an output is exactly one of `schema` or `artifact`, and an
artifact's `maxSize` is a quantity the platform's parser accepts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from runwhen_capability.custom import validate
from runwhen_capability.custom.diagnostics import E_MANIFEST_SCHEMA
from runwhen_capability.custom.manifest import ArtifactSpec, Manifest, OutputSpec


def test_output_needs_exactly_one_of_schema_or_artifact():
    assert OutputSpec.model_validate({"schema": "string"}).artifact is None
    assert OutputSpec.model_validate({"artifact": {}}).schema_ is None
    with pytest.raises(ValidationError, match="exactly one of"):
        OutputSpec.model_validate({})
    with pytest.raises(ValidationError, match="exactly one of"):
        OutputSpec.model_validate({"schema": "string", "artifact": {}})


def test_a_null_artifact_is_not_an_artifact():
    with pytest.raises(ValidationError, match="exactly one of"):
        OutputSpec.model_validate({"artifact": None})


def test_an_artifact_defaults_to_binary_with_no_size_cap():
    spec = OutputSpec.model_validate({"artifact": {}}).artifact
    assert spec == ArtifactSpec(contentType="application/octet-stream", maxSize=None)


def test_an_unknown_key_in_an_artifact_is_ignored_like_everywhere_else():
    spec = OutputSpec.model_validate({"artifact": {"contentType": "text/csv", "future": 1}})
    assert spec.artifact.contentType == "text/csv"


@pytest.mark.parametrize("size", ["0", "1024", "3Ki", "10Mi", "5Gi", "1Ti", "007Gi"])
def test_max_size_accepts_the_quantities_the_platform_parses(size):
    assert ArtifactSpec(maxSize=size).maxSize == size


@pytest.mark.parametrize(
    "size",
    ["5GB", "5G", "1.5Gi", "-1", "-1Gi", "Gi", "", " 1Gi", "1 Gi", "1Gi ", "1gi", "1Pi", "1e3"]
    + ["1Gi\n", "５Gi", "0x10"],
)
def test_max_size_rejects_anything_else(size):
    with pytest.raises(ValidationError, match="maxSize"):
        ArtifactSpec(maxSize=size)


def test_max_size_must_be_a_string():
    with pytest.raises(ValidationError):
        ArtifactSpec(maxSize=1024)


_MANIFEST = """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.sh
    effects: [Test fixture]
    outputs:
      out: %s
"""


def _diagnostics(output: str):
    files = {"capability.yaml": _MANIFEST % output, "tasks/t.sh": "echo hi\n"}
    return [d for d in validate(files) if d.code == E_MANIFEST_SCHEMA]


def test_validate_accepts_a_schema_output_and_an_artifact_output():
    assert _diagnostics('{ schema: "string" }') == []
    assert _diagnostics("{ artifact: { contentType: text/plain, maxSize: 2Gi } }") == []


def test_validate_flags_an_output_with_both_or_neither_at_its_line():
    for output in ('{ schema: "string", artifact: {} }', "{}"):
        (diag,) = _diagnostics(output)
        assert diag.path == "tasks[0].outputs.out"
        assert diag.line == 8
        assert "exactly one of" in diag.message


def test_validate_flags_a_max_size_the_platform_would_refuse():
    (diag,) = _diagnostics("{ artifact: { maxSize: 5GB } }")
    assert diag.path == "tasks[0].outputs.out.artifact.maxSize"
    assert "5GB" in diag.message


def test_manifest_model_round_trips_an_artifact_output():
    manifest = Manifest.model_validate(
        {
            "apiVersion": "runwhen.com/custom-capability/v1",
            "name": "x",
            "tasks": [
                {"name": "t", "file": "tasks/t.sh", "outputs": {"dump": {"artifact": {}}}},
            ],
        }
    )
    assert manifest.tasks[0].outputs["dump"].artifact is not None


@pytest.mark.parametrize("name", ["../up", "/abs", "a/../b", "a//b", "a/./b", "a/", ".", "a\\b"])
def test_validate_flags_an_artifact_output_name_that_could_leave_the_artifacts_dir(name):
    files = {
        "capability.yaml": (
            "apiVersion: runwhen.com/custom-capability/v1\n"
            "name: x\n"
            "tasks:\n"
            "  - name: t\n"
            "    file: tasks/t.sh\n"
            "    effects: [Test fixture]\n"
            "    outputs:\n"
            f'      "{name.replace(chr(92), chr(92) * 2)}": {{ artifact: {{}} }}\n'
        ),
        "tasks/t.sh": "echo hi\n",
    }
    (diag,) = [d for d in validate(files) if d.code == E_MANIFEST_SCHEMA]
    assert "relative path" in diag.message
    assert diag.line == 8


def test_validate_accepts_dotted_and_nested_artifact_output_names():
    for name in ("report.txt", "files/deep/a.bin", "a-b_c"):
        files = {
            "capability.yaml": (
                "apiVersion: runwhen.com/custom-capability/v1\n"
                "name: x\n"
                "tasks:\n"
                "  - name: t\n"
                "    file: tasks/t.sh\n"
                "    effects: [Test fixture]\n"
                "    outputs:\n"
                f'      "{name}": {{ artifact: {{}} }}\n'
            ),
            "tasks/t.sh": "echo hi\n",
        }
        assert [d for d in validate(files) if d.code == E_MANIFEST_SCHEMA] == []
