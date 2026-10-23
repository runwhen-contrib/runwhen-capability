"""manifest_json_schema(): the document the platform serves as /capabilities/schema.json."""

from __future__ import annotations

from runwhen_capability.custom import manifest_json_schema
from runwhen_capability.custom.manifest import Manifest


def test_it_is_the_model_schema_with_a_header():
    doc = manifest_json_schema()
    model = Manifest.model_json_schema()
    assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert doc["title"] == "Manifest"
    assert doc["description"].startswith("JSON Schema for capability.yaml")
    assert {k: v for k, v in doc.items() if k not in ("$schema", "description")} == model


def test_it_documents_effects():
    task = manifest_json_schema()["$defs"]["TaskSpec"]["properties"]
    assert task["effects"]["type"] == "array"


def test_it_documents_artifact_outputs():
    defs = manifest_json_schema()["$defs"]
    assert set(defs["OutputSpec"]["properties"]) == {"schema", "artifact"}
    assert set(defs["ArtifactSpec"]["properties"]) == {"contentType", "maxSize"}
    assert (
        defs["ArtifactSpec"]["properties"]["contentType"]["default"] == "application/octet-stream"
    )


def test_it_states_the_max_size_rule_for_editors():
    import re

    max_size = manifest_json_schema()["$defs"]["ArtifactSpec"]["properties"]["maxSize"]
    (string_type,) = [branch for branch in max_size["anyOf"] if branch.get("type") == "string"]
    pattern = re.compile(string_type["pattern"])
    # a bare YAML integer is a byte count too
    assert {"type": "integer", "minimum": 0} in max_size["anyOf"]
    for good in ("0", "1024", "512Mi", "2Gi", "1Ti", "3Ki"):
        assert pattern.search(good), good
    for bad in ("5GB", "1.5Gi", "-1", "1gi", "Gi", "1Pi", ""):
        assert not pattern.search(bad), bad
