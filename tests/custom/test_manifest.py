"""The manifest models: an output is exactly one of `schema` or `artifact`, and an
artifact's `maxSize` is a quantity the platform's parser accepts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from runwhen_capability.custom import compile_manifest, validate
from runwhen_capability.custom.diagnostics import E_MANIFEST_SCHEMA
from runwhen_capability.custom.manifest import (
    ArtifactSpec,
    Manifest,
    OutputSpec,
    is_valid_artifact_name,
)


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


@pytest.mark.parametrize(("given", "held"), [(1024, "1024"), (0, "0"), (10**12, "1000000000000")])
def test_max_size_accepts_a_plain_byte_count_written_as_a_number(given, held):
    # `maxSize: 1024`, unquoted in YAML, is an integer; it is held (and compiled) as a string
    assert ArtifactSpec(maxSize=given).maxSize == held


@pytest.mark.parametrize("given", [-1, True, False, 1.5, 1024.0, ["1Gi"], {"a": 1}])
def test_max_size_refuses_other_non_strings(given):
    with pytest.raises(ValidationError, match="maxSize"):
        ArtifactSpec(maxSize=given)


def test_a_bare_integer_max_size_in_yaml_validates_and_compiles_as_a_string():
    files = {
        "capability.yaml": (
            "apiVersion: runwhen.com/custom-capability/v1\n"
            "name: x\n"
            "tasks:\n"
            "  - name: t\n"
            "    file: tasks/t.sh\n"
            "    effects: [Test fixture]\n"
            "    outputs:\n"
            "      dump: { artifact: { maxSize: 1048576 } }\n"
        ),
        "tasks/t.sh": "echo hi\n",
    }
    assert [d for d in validate(files) if d.severity == "error"] == []
    [task] = compile_manifest(files)["tasks"]
    assert task["outputs"]["dump"] == {
        "artifact": {"contentType": "application/octet-stream", "maxSize": "1048576"}
    }
    files["capability.yaml"] = files["capability.yaml"].replace("1048576", "-5")
    (diag,) = [d for d in validate(files) if d.code == E_MANIFEST_SCHEMA]
    assert "not a quantity" in diag.message


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


VALID_ARTIFACT_NAMES = ["dump", "report.txt", "a-b_c", "v2.tar.gz", "_x", "0day", "a" * 255]
INVALID_ARTIFACT_NAMES = [
    "my report.txt",
    "dump:v2",
    "-x",
    "a/b",
    ".hidden",
    "..",
    ".",
    "../up",
    "/abs",
    "a/",
    "a//b",
    "a\\b",
    "a\nb",
    "a\x00b",
    "caf\u00e9",
    "",
    "a" * 256,
]


@pytest.mark.parametrize("name", VALID_ARTIFACT_NAMES)
def test_a_valid_artifact_name_is_one_plain_segment(name):
    assert is_valid_artifact_name(name)


@pytest.mark.parametrize("name", INVALID_ARTIFACT_NAMES)
def test_an_invalid_artifact_name_is_refused(name):
    assert not is_valid_artifact_name(name)


def _named_output_manifest(name: str) -> dict[str, str]:
    key = json.dumps(name)  # a JSON string is a valid YAML double-quoted scalar
    return {
        "capability.yaml": (
            "apiVersion: runwhen.com/custom-capability/v1\n"
            "name: x\n"
            "tasks:\n"
            "  - name: t\n"
            "    file: tasks/t.sh\n"
            "    effects: [Test fixture]\n"
            "    outputs:\n"
            f"      {key}: {{ artifact: {{}} }}\n"
        ),
        "tasks/t.sh": "echo hi\n",
    }


@pytest.mark.parametrize("name", [n for n in INVALID_ARTIFACT_NAMES if n and "\x00" not in n])
def test_validate_flags_an_artifact_output_name_that_is_not_one_plain_segment(name):
    (diag,) = [d for d in validate(_named_output_manifest(name)) if d.code == E_MANIFEST_SCHEMA]
    assert "one segment" in diag.message
    assert diag.line == 8


@pytest.mark.parametrize("name", VALID_ARTIFACT_NAMES)
def test_validate_accepts_a_plain_artifact_output_name(name):
    files = _named_output_manifest(name)
    assert [d for d in validate(files) if d.code == E_MANIFEST_SCHEMA] == []


def test_a_schema_output_may_still_have_any_name():
    # the artifact name rule is about files; a value's name is not a path
    files = _named_output_manifest("a/b").copy()
    files["capability.yaml"] = files["capability.yaml"].replace(
        "{ artifact: {} }", '{ schema: "string" }'
    )
    assert [d for d in validate(files) if d.code == E_MANIFEST_SCHEMA] == []
