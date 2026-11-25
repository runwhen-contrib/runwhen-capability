"""Offline JSON Schemas for the editor and CI (AUT-1).

`write_editor_schemas(plugin_dir)` copies the contract schemas that describe a plugin's
files into `<plugin_dir>/.runwhen/schemas/` and a README saying how to point
yaml-language-server at them. The copies are self-contained: the packaged schemas `$ref`
each other by https://schemas.runwhen.com/... ids, which an editor would try to fetch, so
the copies reference their siblings by relative path and carry no `$id`.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import __version__
from ..contracts import schema

EDITOR_DIR = ".runwhen"
SCHEMAS_DIR = "schemas"
EDITOR_SCHEMAS = (
    "tasks",
    "skill-frontmatter",
    "plugin",
    "integration",
    "types",
    "facets",
    "dependencies",
    "rule-frontmatter",
    "knowledge-frontmatter",
    "common",
)
# file glob (from the plugin root) -> schema, for the editor mapping in the README
YAML_FILES = {
    "plugin.yaml": "plugin",
    "skills/*/tasks.yaml": "tasks",
    "integration/integration.yaml": "integration",
    "integration/types.yaml": "types",
    "integration/facets.yaml": "facets",
    "integration/dependencies.yaml": "dependencies",
}


def _localized(node, ids: dict[str, str], used: set[str]):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "$id":
                continue
            if key == "$ref" and isinstance(value, str):
                base, hash_, fragment = value.partition("#")
                if base in ids:
                    used.add(ids[base])
                    value = f"./{ids[base]}.schema.json{hash_}{fragment}"
            out[key] = _localized(value, ids, used)
        return out
    if isinstance(node, list):
        return [_localized(item, ids, used) for item in node]
    return node


def write_editor_schemas(plugin_dir: Path) -> Path:
    """Write `.runwhen/schemas/*.schema.json` and `.runwhen/README` under `plugin_dir`;
    the schemas directory. Existing copies are replaced."""
    from ..contracts import SCHEMA_NAMES as ALL

    ids = {schema(name)["$id"]: name for name in ALL if "$id" in schema(name)}
    out_dir = Path(plugin_dir) / EDITOR_DIR / SCHEMAS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    pending, written = list(EDITOR_SCHEMAS), set()
    while pending:
        name = pending.pop()
        if name in written:
            continue
        written.add(name)
        used: set[str] = set()
        doc = _localized(schema(name), ids, used)
        pending.extend(sorted(used - written))
        (out_dir / f"{name}.schema.json").write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    (Path(plugin_dir) / EDITOR_DIR / "README").write_text(_readme(), encoding="utf-8")
    return out_dir


def _readme() -> str:
    mapping = ",\n".join(
        f'    ".runwhen/schemas/{schema_name}.schema.json": "{glob}"'
        for glob, schema_name in YAML_FILES.items()
    )
    return (
        f"Written by `rwtask pull` (runwhen-capability {__version__}); rerun it to refresh.\n"
        "\n"
        "schemas/ holds the plugin file schemas, offline. Point yaml-language-server at them\n"
        "(VS Code with the YAML extension, .vscode/settings.json):\n"
        "\n"
        '  "yaml.schemas": {\n'
        f"{mapping}\n"
        "  }\n"
        "\n"
        "or add a modeline to the first line of a file, e.g. in plugin.yaml:\n"
        "\n"
        "  # yaml-language-server: $schema=.runwhen/schemas/plugin.schema.json\n"
        "\n"
        "SKILL.md, rules/*.md and knowledge/*.md frontmatter use skill-frontmatter, "
        "rule-frontmatter and\n"
        "knowledge-frontmatter; validate them with `rwtask validate .`. CI needs no network "
        "for validation: it runs\n"
        "`rwtask pull --frozen` and then `rwtask validate .`.\n"
        "Commit or ignore .runwhen/ as you prefer.\n"
    )
