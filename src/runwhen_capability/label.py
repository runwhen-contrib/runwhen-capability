"""`rwtask label` -- the OCI label values a capability image carries, so a
catalog can read a capability straight off the pushed image:

- com.runwhen.capability.manifest.v1: base64 of the capability's
  manifest.yaml, verbatim (encode_manifest).
- com.runwhen.capability.schemas.v1: base64 of a compact, sorted-key JSON
  object holding EVERY file under the capability's schemas/ directory --
  not only the ones the current manifest references -- keyed
  "schemas/<filename>" (build_schemas_map + encode_schemas). An image thus
  carries every schema version it has ever published, so an old run's
  output stays resolvable after the manifest moves a `schema:` ref on.

CI computes both at build time and passes them in as build args. Neither is
a separate build artifact.

The `image:` guard and the `schema:` ref scan are line/text scans, not a
YAML parse: they judge the manifest bytes exactly as they will ship.
"""

from __future__ import annotations

import base64
import json
import posixpath
import re
from pathlib import Path

# Matches a top-level `image:` key only -- not one indented under another
# key, and not `images:`. A manifest cannot name its own image: the image
# is built from the manifest, so it cannot know its own digest. Also matches
# a bare `image:` line (a block value on the following lines) and a quoted
# key.
TOP_LEVEL_IMAGE_KEY = re.compile(r"^(image|\"image\"|'image')\s*:(\s|$)")

# Every `schema:` value in a manifest, block or flow style alike (e.g.
# `findings: { kind: rw.findings.v1, schema: ./schemas/findings.v1.json }`).
# The negative lookbehind keeps this from matching an identifier that merely
# ends in "schema".
SCHEMA_REF = re.compile(r"(?<![\w])schema\s*:\s*([^\s,}]+)")


class ManifestLabelError(RuntimeError):
    """A capability directory can't be turned into a label value: no
    manifest, a manifest with a top-level `image:` key, or a schemas/ file
    or manifest `schema:` ref that can't be resolved."""


def _manifest_path(capability_dir: Path) -> Path:
    manifest_path = Path(capability_dir) / "manifest.yaml"
    if not manifest_path.is_file():
        raise ManifestLabelError(f"{manifest_path}: no such file")
    return manifest_path


def encode_manifest(capability_dir: Path) -> str:
    """Base64 (standard alphabet, padded, no line breaks) of
    capability_dir/manifest.yaml's bytes, verbatim. Raises
    ManifestLabelError if the file is missing or still has a top-level
    `image:` key."""
    manifest_path = _manifest_path(capability_dir)
    raw = manifest_path.read_bytes()
    if any(TOP_LEVEL_IMAGE_KEY.match(line) for line in raw.decode("utf-8").splitlines()):
        raise ManifestLabelError(
            f"{manifest_path}: has a top-level 'image:' key -- an image cannot know its "
            "own digest; remove it (the manifest rides the image as a label instead)"
        )
    return base64.b64encode(raw).decode("ascii")


def find_schema_refs(manifest_text: str) -> list[str]:
    """Every raw `schema:` value in a manifest's text, in appearance order."""
    return [match.group(1).strip("\"'") for match in SCHEMA_REF.finditer(manifest_text)]


def normalize_schema_ref(ref: str) -> str:
    """posixpath.normpath's a manifest `schema:` ref (e.g.
    "./schemas/findings.v1.json" -> "schemas/findings.v1.json"). Raises
    ManifestLabelError if the ref is absolute or escapes the capability
    directory via a '..' segment."""
    key = posixpath.normpath(ref)
    if posixpath.isabs(key) or key == ".." or key.startswith("../"):
        raise ManifestLabelError(
            f"schema ref {ref!r} must be relative and stay under the capability directory"
        )
    return key


def build_schemas_map(capability_dir: Path) -> dict[str, dict]:
    """Every file in capability_dir/schemas/, keyed
    posixpath.normpath("schemas/<filename>"), parsed as JSON -- each must be
    a JSON object. Every `schema:` ref in the capability's manifest.yaml
    (normalised) must be among these keys. Raises ManifestLabelError naming
    the offending file/ref otherwise. No schemas/ dir and no manifest ref ->
    {}."""
    capability_dir = Path(capability_dir)
    schemas: dict[str, dict] = {}
    schemas_dir = capability_dir / "schemas"
    if schemas_dir.is_dir():
        for path in sorted(schemas_dir.iterdir()):
            if not path.is_file():
                continue
            key = normalize_schema_ref(f"schemas/{path.name}")
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ManifestLabelError(f"{path}: not valid JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ManifestLabelError(
                    f"{path}: must be a JSON object, got {type(parsed).__name__}"
                )
            schemas[key] = parsed

    manifest_path = capability_dir / "manifest.yaml"
    if manifest_path.is_file():
        for ref in find_schema_refs(manifest_path.read_text(encoding="utf-8")):
            key = normalize_schema_ref(ref)
            if key not in schemas:
                raise ManifestLabelError(
                    f"{manifest_path}: references schema {ref!r} ({key!r}), which is not "
                    f"a file under {schemas_dir} -- every manifest schema: ref must exist on disk"
                )
    return schemas


def encode_schemas(schemas: dict[str, dict]) -> str:
    """Base64 (standard alphabet, padded) of the schemas map, compactly
    serialised with sorted keys so the same schemas/ directory always
    produces the same value. No schemas -> empty string, i.e. an empty
    label."""
    if not schemas:
        return ""
    payload = json.dumps(schemas, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(payload).decode("ascii")


def schemas_label(capability_dir: Path) -> str:
    """The com.runwhen.capability.schemas.v1 value for capability_dir.
    Requires the manifest to exist, like encode_manifest."""
    _manifest_path(capability_dir)
    return encode_schemas(build_schemas_map(capability_dir))
