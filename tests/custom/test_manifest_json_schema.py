"""manifest_json_schema(): the document papi serves as /capabilities/schema.json."""

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
