"""`rwtask label` -- the OCI label values a capability image carries.

- `rwtask label <capability-dir>`: base64 (no line breaks) of manifest.yaml,
  verbatim, for com.runwhen.capability.manifest.v1. Refuses a manifest with
  a top-level `image:` key -- an image cannot know its own digest.
- `rwtask label --schemas <capability-dir>`: base64 of a compact, sorted-key
  JSON object holding every file under schemas/, keyed "schemas/<name>",
  for com.runwhen.capability.schemas.v1. Every manifest `schema:` ref must
  be one of those files.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from runwhen_capability.cli import main
from runwhen_capability.label import ManifestLabelError, encode_manifest


def run_label(capsys, *args: str) -> tuple[int, str, str]:
    try:
        code = main(["label", *args])
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def make_capability(root: Path, manifest: str) -> Path:
    capability_dir = root / "capabilities" / "fake"
    capability_dir.mkdir(parents=True)
    (capability_dir / "manifest.yaml").write_text(manifest, encoding="utf-8")
    return capability_dir


def make_schema_capability(root: Path, schema_ref: str | None) -> Path:
    """A manifest that references schema_ref (verbatim, unquoted) in flow
    style, or declares no schema output at all when schema_ref is None."""
    if schema_ref is None:
        return make_capability(root, "capability: fake\n")
    return make_capability(
        root,
        "capability: fake\n"
        "tasks:\n"
        "  - name: t\n"
        "    outputs:\n"
        f"      result: {{ kind: rw.fake.v1, schema: {schema_ref} }}\n",
    )


# --- manifest label ------------------------------------------------------------


def test_manifest_label_round_trips_the_manifest_bytes(tmp_path, capsys):
    manifest = "# a comment kept verbatim\ncapability: fake\ndescription: naïve — ok\n"
    capability_dir = make_capability(tmp_path, manifest)

    code, out, err = run_label(capsys, str(capability_dir))

    assert code == 0, err
    assert "\n" not in out, "output must be base64 with no line breaks"
    assert base64.b64decode(out) == manifest.encode("utf-8")


def test_manifest_label_fails_on_a_missing_manifest(tmp_path, capsys):
    code, out, err = run_label(capsys, str(tmp_path / "capabilities" / "does-not-exist"))

    assert code == 1
    assert out == ""
    assert "no such file" in err


@pytest.mark.parametrize(
    "image_line",
    [
        "image: ghcr.io/example/fake@sha256:deadbeef\n",
        "image:\n  repository: ghcr.io/example/fake\n",
        '"image": ghcr.io/example/fake@sha256:deadbeef\n',
        "'image': ghcr.io/example/fake@sha256:deadbeef\n",
    ],
)
def test_manifest_label_refuses_a_top_level_image_key(tmp_path, capsys, image_line):
    capability_dir = make_capability(tmp_path, f"{image_line}capability: fake\n")

    code, out, err = run_label(capsys, str(capability_dir))

    assert code == 1
    assert out == ""
    assert "image" in err


def test_manifest_label_ignores_a_nested_or_lookalike_image_key(tmp_path, capsys):
    capability_dir = make_capability(tmp_path, "capability: fake\nsetup:\n  image: x\nimages: []\n")

    code, _, err = run_label(capsys, str(capability_dir))

    assert code == 0, err


def test_encode_manifest_is_the_same_value_the_cli_prints(tmp_path, capsys):
    capability_dir = make_capability(tmp_path, "capability: fake\n")

    _, out, _ = run_label(capsys, str(capability_dir))

    assert encode_manifest(capability_dir) == out


def test_encode_manifest_raises_on_an_image_key(tmp_path):
    capability_dir = make_capability(tmp_path, "image: x\ncapability: fake\n")

    with pytest.raises(ManifestLabelError, match="image"):
        encode_manifest(capability_dir)


def test_label_without_a_capability_dir_is_a_usage_error(capsys):
    code, _, err = run_label(capsys)

    assert code == 2
    assert "capability_dir" in err


# --- schemas label ----------------------------------------------------------------


def test_schemas_label_includes_every_file_including_unreferenced(tmp_path, capsys):
    capability_dir = make_schema_capability(tmp_path, "./schemas/a.v1.json")
    schemas_dir = capability_dir / "schemas"
    schemas_dir.mkdir()
    (schemas_dir / "a.v1.json").write_text('{"type": "object"}')
    (schemas_dir / "b.v1.json").write_text('{"type": "object", "unreferenced": true}')

    code, out, err = run_label(capsys, "--schemas", str(capability_dir))

    assert code == 0, err
    assert json.loads(base64.b64decode(out)) == {
        "schemas/a.v1.json": {"type": "object"},
        "schemas/b.v1.json": {"type": "object", "unreferenced": True},
    }


def test_schemas_label_is_compact_sorted_json(tmp_path, capsys):
    capability_dir = make_schema_capability(tmp_path, None)
    schemas_dir = capability_dir / "schemas"
    schemas_dir.mkdir()
    (schemas_dir / "b.v1.json").write_text('{\n  "z": 1,\n  "a": [1, 2]\n}\n')
    (schemas_dir / "a.v1.json").write_text('{"k": "v"}')

    _, out, _ = run_label(capsys, "--schemas", str(capability_dir))

    assert base64.b64decode(out) == (
        b'{"schemas/a.v1.json":{"k":"v"},"schemas/b.v1.json":{"a":[1,2],"z":1}}'
    )


def test_schemas_label_fails_on_a_referenced_but_missing_file(tmp_path, capsys):
    capability_dir = make_schema_capability(tmp_path, "./schemas/missing.v1.json")
    (capability_dir / "schemas").mkdir()

    code, out, err = run_label(capsys, "--schemas", str(capability_dir))

    assert code == 1
    assert out == ""
    assert "missing.v1.json" in err


@pytest.mark.parametrize("bad_ref", ["/etc/passwd", "../escape.json", "../../etc/passwd"])
def test_schemas_label_rejects_absolute_or_escaping_refs(tmp_path, capsys, bad_ref):
    capability_dir = make_schema_capability(tmp_path, bad_ref)
    (capability_dir / "schemas").mkdir()

    code, _, err = run_label(capsys, "--schemas", str(capability_dir))

    assert code == 1
    assert "relative" in err


def test_schemas_label_fails_on_non_object_json(tmp_path, capsys):
    capability_dir = make_schema_capability(tmp_path, None)
    schemas_dir = capability_dir / "schemas"
    schemas_dir.mkdir()
    (schemas_dir / "a.v1.json").write_text("[1, 2, 3]")

    code, _, err = run_label(capsys, "--schemas", str(capability_dir))

    assert code == 1
    assert "a.v1.json" in err


def test_schemas_label_fails_on_invalid_json(tmp_path, capsys):
    capability_dir = make_schema_capability(tmp_path, None)
    schemas_dir = capability_dir / "schemas"
    schemas_dir.mkdir()
    (schemas_dir / "a.v1.json").write_text("{not json")

    code, _, err = run_label(capsys, "--schemas", str(capability_dir))

    assert code == 1
    assert "a.v1.json" in err
    assert "not valid JSON" in err


def test_schemas_label_with_no_schemas_is_an_empty_value(tmp_path, capsys):
    capability_dir = make_schema_capability(tmp_path, None)

    code, out, err = run_label(capsys, "--schemas", str(capability_dir))

    assert code == 0, err
    assert out == ""


def test_schemas_label_is_deterministic(tmp_path, capsys):
    capability_dir = make_schema_capability(tmp_path, None)
    schemas_dir = capability_dir / "schemas"
    schemas_dir.mkdir()
    (schemas_dir / "b.v1.json").write_text('{"b": 1}')
    (schemas_dir / "a.v1.json").write_text('{"a": 2}')

    _, first, _ = run_label(capsys, "--schemas", str(capability_dir))
    _, second, _ = run_label(capsys, "--schemas", str(capability_dir))

    assert first == second


def test_schemas_label_needs_the_manifest_too(tmp_path, capsys):
    code, _, err = run_label(capsys, "--schemas", str(tmp_path / "capabilities" / "nope"))

    assert code == 1
    assert "no such file" in err
