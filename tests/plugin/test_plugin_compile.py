"""runwhen_capability.plugin.compile and `rwtask compile`."""

from __future__ import annotations

import json
import random
import re
import shutil

import pytest
from _plugin_fixtures import (
    EXAMPLES,
    KUBE_MINI,
    drop,
    edit,
    kube_entries,
    platform_ops_entries,
    put,
)
from jsonschema import Draft202012Validator

from runwhen_capability import __version__
from runwhen_capability.cli import main
from runwhen_capability.contracts import (
    TreeEntry,
    archive,
    check,
    load_yaml,
    permission_hash,
    task_hash,
)
from runwhen_capability.plugin import (
    PluginInvalid,
    compile_entries,
    compile_tree,
    load_dir,
    load_entries,
)

HEALTH = "skills/k8s-workload-health/"
BASE = "skills/k8s-base/"
SINCE = "      since: { type: duration, default: 30m, description: Look-back window }\n"


def compiled(entries=None, **kw):
    return compile_entries(entries if entries is not None else kube_entries(), **kw)


def task(doc, ref):
    return next(t for t in doc["tasks"] if t["ref"].endswith(":" + ref))


def with_inputs(*lines: str):
    block = "".join(f"      {line}\n" for line in lines)
    return edit(kube_entries(), HEALTH + "tasks.yaml", SINCE, block)


def input_schema(*lines: str, extra=()):
    entries = with_inputs(*lines)
    for path, text in extra:
        entries = put(entries, path, text)
    return task(compiled(entries), "k8s-workload-health/rollout-status")["inputSchema"]


def test_kube_mini_compiles_and_checks_against_the_schema():
    doc = compiled()
    check("compiled-plugin", doc)
    assert doc["apiVersion"] == "runwhen.com/compiled-plugin/v1"
    assert doc["compiler"] == {"name": "runwhen-capability", "version": __version__}
    assert doc["plugin"]["name"] == "kubernetes"
    assert [s["name"] for s in doc["skills"]] == ["k8s-base", "k8s-workload-health"]
    assert [t["ref"] for t in doc["tasks"]] == [
        "kubernetes:k8s-base/discover",
        "kubernetes:k8s-base/kubectl",
        "kubernetes:k8s-workload-health/rollout-status",
        "kubernetes:k8s-workload-health/restart",
    ]


def test_sdk_version_override():
    assert compiled(sdk_version="9.9.9")["compiler"]["version"] == "9.9.9"


def test_platform_ops_compiles():
    doc = compiled(platform_ops_entries())
    check("compiled-plugin", doc)
    assert doc["integration"] is None
    assert task(doc, "papi-health/api-probe")["credentials"] == [{"type": "kubernetes/kubeconfig"}]


def test_skills_entries():
    skill = compiled()["skills"][1]
    assert skill["runtime"] == "kubernetes@1"
    assert skill["taskNames"] == ["rollout-status", "restart"]
    assert skill["files"] == sorted(skill["files"])
    assert skill["files"][0] == HEALTH + "SKILL.md"
    assert HEALTH + "scripts/restart.sh" in skill["files"]
    md = next(e for e in kube_entries() if e.path == HEALTH + "SKILL.md").data
    import hashlib

    assert skill["skillMdSha256"] == "sha256:" + hashlib.sha256(md).hexdigest()
    assert skill["frontmatter"]["runwhen"]["role"] == "health"


def test_integration_inlines_permission_and_facet_schemas():
    entries = put(
        kube_entries(),
        "integration/facets.yaml",
        "facets:\n  - key: replicas\n    title: Replicas\n    appliesTo: { types: [deployment] }\n"
        "    populator: { kind: agent }\n    volatility: state\n"
        "    schema: ./schemas/replicas.v1.json\n",
    )
    entries = put(entries, "integration/schemas/replicas.v1.json", '{"type": "integer"}')
    integration = compiled(entries)["integration"]
    assert integration["definition"]["platform"] == "kubernetes"
    assert integration["definition"]["facets"][0]["key"] == "replicas"
    assert integration["permissionSchema"]["required"] == ["verbs"]
    assert integration["facetSchemas"] == {"replicas": {"type": "integer"}}
    assert "facetSchemas" not in compiled()["integration"]


def test_task_resolution_defaults_and_inheritance():
    t = task(compiled(), "k8s-workload-health/rollout-status")
    assert t["script"] == HEALTH + "scripts/rollout_status.py"
    assert t["runtime"] == "kubernetes@1"
    assert t["role"] == "health"
    assert t["appliesTo"] == {"types": ["kubernetes/deployment"]}
    assert t["readOnly"] is True and "effects" not in t and "destructive" not in t
    assert (t["idempotent"], t["dryRun"]) == (False, False)
    assert t["credentials"] == [{"type": "kubernetes/kubeconfig"}]
    assert t["timeout"] == "120s" and "resources" not in t
    assert t["tags"] == ["rollout"]
    restart = task(compiled(), "k8s-workload-health/restart")
    assert restart["effects"] and restart["destructive"] is False and restart["idempotent"] is True


def test_task_values_override_defaults_and_missing_ones_are_omitted():
    entries = edit(
        kube_entries(),
        HEALTH + "tasks.yaml",
        "    script: scripts/rollout_status.py\n",
        "    script: scripts/rollout_status.py\n    timeout: 5s\n    resources: { cpu: 250m }\n"
        "    tags: [mine]\n    dryRun: true\n",
    )
    t = task(compiled(entries), "k8s-workload-health/rollout-status")
    assert (t["timeout"], t["resources"], t["tags"], t["dryRun"]) == (
        "5s",
        {"cpu": "250m"},
        ["mine"],
        True,
    )
    base = task(compiled(), "k8s-base/kubectl")
    assert "timeout" not in base and "resources" not in base and base["tags"] == []
    assert "role" in base and "appliesTo" in base  # from k8s-base's SKILL.md


def test_role_and_applies_to_only_when_the_skill_has_them():
    entries = edit(
        kube_entries(),
        BASE + "SKILL.md",
        "runwhen:\n  role: [discovery, cli]\n  appliesTo:\n    types: [kubernetes/cluster]\n",
        "",
    )
    t = task(compiled(entries), "k8s-base/kubectl")
    assert "role" not in t and "appliesTo" not in t
    entries = edit(entries, HEALTH + "SKILL.md", "  tags: [rollout]\n", "")
    assert task(compiled(entries), "k8s-workload-health/restart")["tags"] == []


def test_credentials_are_normalized_to_a_list_of_type_and_vars():
    discover = task(compiled(), "k8s-base/discover")
    assert discover["credentials"] == [{"type": "kubernetes/kubeconfig"}, {"type": "runwhen/sync"}]
    entries = edit(
        kube_entries(),
        HEALTH + "tasks.yaml",
        "credentials: kubernetes/kubeconfig",
        "credentials: [kubernetes/kubeconfig, {type: generic/env, vars: [A, B]}]",
    )
    t = task(compiled(entries), "k8s-workload-health/restart")
    assert t["credentials"] == [
        {"type": "kubernetes/kubeconfig"},
        {"type": "generic/env", "vars": ["A", "B"]},
    ]
    entries = edit(
        kube_entries(), HEALTH + "tasks.yaml", "  credentials: kubernetes/kubeconfig\n", ""
    )
    assert task(compiled(entries), "k8s-workload-health/restart")["credentials"] == []


def test_permission_hash_equals_contracts_permission_hash():
    for t in compiled()["tasks"]:
        for p in t["permissions"]:
            assert p["hash"] == permission_hash(p["rule"])
    kubectl = task(compiled(), "k8s-base/kubectl")
    assert kubectl["permissions"][0]["rule"] == {
        "apiGroups": ["*"],
        "resources": ["*"],
        "verbs": ["get", "list", "watch"],
    }


def test_settings_merge_file_level_then_task_level_task_wins():
    entries = edit(
        kube_entries(),
        HEALTH + "tasks.yaml",
        "defaults:\n",
        "settings:\n  threshold: { type: integer, default: 5 }\n"
        "  window: { type: duration, default: 1h }\n"
        "defaults:\n",
    )
    entries = edit(
        entries,
        HEALTH + "tasks.yaml",
        "    script: scripts/rollout_status.py\n",
        "    script: scripts/rollout_status.py\n    settings:\n"
        "      threshold: { type: integer, default: 9 }\n"
        "      extra: { type: boolean, default: true }\n",
    )
    doc = compiled(entries)
    assert task(doc, "k8s-workload-health/rollout-status")["settings"] == {
        "threshold": {"type": "integer", "default": 9},
        "window": {"type": "duration", "default": "1h"},
        "extra": {"type": "boolean", "default": True},
    }
    assert task(doc, "k8s-workload-health/restart")["settings"] == {
        "threshold": {"type": "integer", "default": 5},
        "window": {"type": "duration", "default": "1h"},
    }
    assert task(doc, "k8s-base/discover")["settings"]["namespaces"]["default"] == []


def test_required_is_resolved_for_inputs():
    t = task(
        compiled(
            with_inputs(
                "a: { type: string }",
                "b: { type: string, default: x }",
                "c: { type: string, required: false }",
                "d: { type: integer, default: 1, required: true }",
            )
        ),
        "k8s-workload-health/rollout-status",
    )
    assert {k: v["required"] for k, v in t["inputs"].items()} == {
        "a": True,
        "b": False,
        "c": False,
        "d": True,
    }
    assert t["inputSchema"]["required"] == ["a", "d"]


def test_input_schema_envelope():
    schema = input_schema("a: { type: string }")
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    Draft202012Validator.check_schema(schema)
    assert not task(compiled(), "k8s-base/discover")["inputs"]
    assert task(compiled(), "k8s-base/discover")["inputSchema"]["properties"] == {}


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("{ type: string }", {"type": "string"}),
        ("{ type: integer }", {"type": "integer"}),
        ("{ type: number }", {"type": "number"}),
        ("{ type: boolean }", {"type": "boolean"}),
        ("{ type: datetime }", {"type": "string", "format": "date-time"}),
        ("{ type: enum, enum: [a, b] }", {"enum": ["a", "b"]}),
        ("{ type: 'string[]' }", {"type": "array", "items": {"type": "string"}}),
        ("{ type: 'enum[]', enum: [a, b] }", {"type": "array", "items": {"enum": ["a", "b"]}}),
        (
            "{ type: integer, min: 1, max: 9, description: How many, default: 3, examples: [2] }",
            {
                "type": "integer",
                "minimum": 1,
                "maximum": 9,
                "description": "How many",
                "default": 3,
                "examples": [2],
            },
        ),
        (
            "{ type: 'integer[]', min: 0, default: [1] }",
            {"type": "array", "items": {"type": "integer", "minimum": 0}, "default": [1]},
        ),
        ("{ type: string, pattern: '^a+$' }", {"type": "string", "pattern": "^a+$"}),
        (
            "{ type: object, schema: { type: object, properties: { x: { type: integer } } } }",
            {"type": "object", "properties": {"x": {"type": "integer"}}},
        ),
        (
            "{ type: object, schema: '{ x: integer, y: string? }' }",
            {
                "type": "object",
                "properties": {"x": {"type": "integer"}, "y": {"type": "string"}},
                "required": ["x"],
            },
        ),
        (
            "{ type: 'object[]', schema: { type: object } }",
            {"type": "array", "items": {"type": "object"}},
        ),
    ],
)
def test_input_type_mapping(spec, expected):
    schema = input_schema(f"v: {spec}")
    assert schema["properties"]["v"] == expected
    Draft202012Validator.check_schema(schema)


def test_duration_input_gets_the_common_duration_pattern():
    prop = input_schema("v: { type: duration, default: 30m }")["properties"]["v"]
    assert prop["type"] == "string" and prop["default"] == "30m"
    assert re.fullmatch(prop["pattern"], "1h30m") and not re.fullmatch(prop["pattern"], "soon")
    prop = input_schema("v: { type: 'duration[]' }")["properties"]["v"]
    assert prop["type"] == "array" and prop["items"]["type"] == "string"
    prop = input_schema("v: { type: duration, pattern: '^[0-9]+m$' }")["properties"]["v"]
    assert prop["allOf"] == [{"pattern": "^[0-9]+m$"}]


def test_object_input_from_a_schema_file():
    schema = input_schema(
        "v: { type: object, schema: ./schemas/filter.v1.json }",
        extra=[(HEALTH + "schemas/filter.v1.json", '{"type": "object", "title": "Filter"}')],
    )
    assert schema["properties"]["v"] == {"type": "object", "title": "Filter"}
    validator = Draft202012Validator(input_schema("a: { type: integer, min: 1 }"))
    assert validator.is_valid({"a": 2}) and not validator.is_valid({"a": 0})
    assert not validator.is_valid({}) and not validator.is_valid({"a": 1, "zz": 1})


def test_outputs_compact_file_artifact_and_kind():
    doc = compiled()
    outputs = task(doc, "k8s-workload-health/rollout-status")["outputs"]
    assert outputs["summary"] == {
        "jsonSchema": {
            "type": "object",
            "properties": {"stuck": {"type": "boolean"}, "reason": {"type": "string"}},
            "required": ["stuck"],
        }
    }
    assert outputs["rollout"]["schemaRef"] == HEALTH + "schemas/rollout.v1.json"
    assert outputs["rollout"]["jsonSchema"]["type"] == "array"
    assert outputs["rollout"]["description"] == "Rollout state per ReplicaSet"
    assert task(doc, "k8s-base/discover")["outputs"] == {
        "summary": {"kind": "rw.discovery.summary.v1"}
    }
    entries = edit(
        kube_entries(),
        HEALTH + "tasks.yaml",
        '      summary: { schema: "{ stuck: boolean, reason: string? }" }\n',
        "      events: { artifact: { contentType: application/x-ndjson, maxSize: 50Mi } }\n",
    )
    t = task(compiled(entries), "k8s-workload-health/rollout-status")
    assert t["outputs"]["events"] == {
        "artifact": {"contentType": "application/x-ndjson", "maxSize": "50Mi"}
    }


def test_rules_and_knowledge():
    doc = compiled()
    assert doc["rules"] == [
        {
            "name": "one",
            "description": "Prefer read-only kubectl commands",
            "body": "Use read-only kubectl commands unless the user asks for a change.\n",
            "words": 11,
            "sha256": doc["rules"][0]["sha256"],
        }
    ]
    import hashlib

    raw = next(e.data for e in kube_entries() if e.path == "rules/one.md")
    assert doc["rules"][0]["sha256"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    k = doc["knowledge"][0]
    assert k["name"] == "one" and k["appliesTo"] == {"types": ["kubernetes/deployment"]}
    assert k["tags"] == ["restarts"] and "resources" not in k and k["body"].startswith("Payments")


def test_rules_and_knowledge_are_sorted_by_name():
    entries = kube_entries()
    for name in ("b", "a"):
        entries = put(
            entries, f"rules/{name}.md", f"---\nname: {name}\ndescription: d\n---\nbody\n"
        )
    assert [r["name"] for r in compiled(entries)["rules"]] == ["a", "b", "one"]


# -- the task hash ------------------------------------------------------------------


def hash_of(entries, ref="k8s-workload-health/rollout-status"):
    return task(compiled(entries), ref)["hash"]


def test_task_hash_is_the_contracts_task_hash_over_script_lib_and_schemas():
    entries = put(kube_entries(), HEALTH + "lib/helper.py", "X = 1\n")
    t = task(compiled(entries), "k8s-workload-health/rollout-status")
    files = {
        p: next(e.data for e in entries if e.path == p)
        for p in (
            HEALTH + "scripts/rollout_status.py",
            HEALTH + "lib/helper.py",
            HEALTH + "schemas/rollout.v1.json",
        )
    }
    assert t["hash"] == task_hash(t, files)
    restart = task(compiled(entries), "k8s-workload-health/restart")
    assert restart["hash"] == task_hash(
        restart,
        {
            HEALTH + "scripts/restart.sh": next(
                e.data for e in entries if e.path == HEALTH + "scripts/restart.sh"
            ),
            HEALTH + "lib/helper.py": b"X = 1\n",
        },
    )


@pytest.mark.parametrize(
    ("path", "text"),
    [
        (HEALTH + "scripts/rollout_status.py", "def main(ctx, since='30m'):\n    return {}\n"),
        (HEALTH + "schemas/rollout.v1.json", '{"type": "array"}'),
    ],
)
def test_task_hash_changes_with_the_script_and_referenced_schemas(path, text):
    assert hash_of(put(kube_entries(), path, text)) != hash_of(kube_entries())


def test_task_hash_changes_with_a_lib_file():
    base = put(kube_entries(), HEALTH + "lib/helper.py", "X = 1\n")
    assert hash_of(put(base, HEALTH + "lib/helper.py", "X = 2\n")) != hash_of(base)
    assert hash_of(base) != hash_of(kube_entries())


def test_task_hash_changes_when_the_task_definition_changes():
    entries = edit(kube_entries(), HEALTH + "tasks.yaml", "timeout: 120s", "timeout: 121s")
    assert hash_of(entries) != hash_of(kube_entries())


def test_task_hash_ignores_the_skill_body_and_other_skills():
    before = hash_of(kube_entries())
    entries = edit(
        kube_entries(),
        HEALTH + "SKILL.md",
        "`rollout-status` first.",
        "Start with `rollout-status`.",
    )
    assert hash_of(entries) == before
    entries = put(
        kube_entries(), BASE + "scripts/kubectl.sh", "#!/usr/bin/env bash\necho changed\n"
    )
    assert hash_of(entries) == before
    entries = put(kube_entries(), BASE + "lib/helper.py", "X = 1\n")
    assert hash_of(entries) == before
    entries = put(kube_entries(), "rules/one.md", "---\nname: one\ndescription: d\n---\nx\n")
    assert hash_of(entries) == before


def test_task_hash_ignores_other_tasks_in_the_same_skill():
    entries = put(
        kube_entries(), HEALTH + "scripts/restart.sh", "#!/usr/bin/env bash\necho changed\n"
    )
    assert hash_of(entries) == hash_of(kube_entries())
    assert hash_of(entries, "k8s-workload-health/restart") != hash_of(
        kube_entries(), "k8s-workload-health/restart"
    )


# -- determinism, validity, entry points ------------------------------------------------


def dump(doc) -> str:
    return json.dumps(doc, sort_keys=True)


def test_compile_is_deterministic_regardless_of_entry_order():
    first = kube_entries()
    results = []
    for seed in range(4):
        shuffled = list(first)
        random.Random(seed).shuffle(shuffled)
        results.append((dump(compiled(shuffled)), archive.build(shuffled)))
    assert len({r[0] for r in results}) == 1
    assert len({r[1] for r in results}) == 1
    assert dump(compiled()) == dump(compiled())


def test_an_invalid_tree_raises_plugin_invalid_with_its_diagnostics():
    entries = drop(kube_entries(), HEALTH + "scripts/restart.sh")
    with pytest.raises(PluginInvalid) as info:
        compiled(entries)
    assert [d.code for d in info.value.diagnostics] == ["E_SCRIPT_MISSING"]
    with pytest.raises(PluginInvalid):
        compile_tree(load_entries(put(kube_entries(), "plugin.yaml", "name: [x\n")))


def test_warnings_do_not_block_compiling():
    assert compiled(platform_ops_entries())["plugin"]["name"] == "runwhen-platform-ops"


def test_a_skill_only_tree_has_no_compiled_form_without_a_workspace_revision():
    only = [e for e in kube_entries() if e.path.startswith(HEALTH)]
    with pytest.raises(PluginInvalid) as info:
        compiled(only)
    assert info.value.diagnostics[0].code == "E_PLUGIN_MISSING"


def workspace_entries():
    """The tree behind contracts/examples/compiled-plugin/synthetic-workspace-skill.yaml."""
    base = "skills/nightly-export-check/"
    return [
        TreeEntry(
            base + "SKILL.md",
            b"---\nname: nightly-export-check\n"
            b"description: Check that last night's export job wrote a fresh file to the shared "
            b"volume.\nrunwhen:\n  role: cli\n  tags: [exports]\n---\n"
            b"Run `export-fresh` when the morning report is empty.\n",
        ),
        TreeEntry(
            base + "tasks.yaml",
            b"apiVersion: runwhen.com/tasks/v1\nruntime: generic@1\ntasks:\n"
            b"  - name: export-fresh\n    title: Export file is fresh\n"
            b"    description: Reports the age of the newest file in the export directory.\n"
            b"    script: scripts/export_fresh.sh\n    readOnly: true\n    inputs:\n"
            b"      directory: { type: string, default: /data/exports, "
            b"description: Directory to inspect }\n    outputs:\n"
            b'      newest: { schema: "{ file: string, ageMinutes: integer }" }\n',
        ),
        TreeEntry(
            base + "scripts/export_fresh.sh",
            b'#!/usr/bin/env bash\nset -euo pipefail\nls -t "$RW_INPUT_DIRECTORY" | head -n 1\n',
            True,
        ),
    ]


def test_a_workspace_skill_revision_compiles_as_the_workspace_plugin():
    doc = compiled(workspace_entries(), workspace_revision=7)
    check("compiled-plugin", doc)
    assert doc["plugin"] == {
        "name": "workspace",
        "version": "0.0.0-rev.7",
        "description": "Workspace skill nightly-export-check",
    }
    assert doc["integration"] is None
    assert [t["ref"] for t in doc["tasks"]] == ["workspace:nightly-export-check/export-fresh"]
    assert doc["tasks"][0]["script"] == "skills/nightly-export-check/scripts/export_fresh.sh"
    assert doc["rules"] == [] and doc["knowledge"] == []


def test_the_workspace_example_is_what_compile_emits():
    example = load_yaml(
        (EXAMPLES / "compiled-plugin" / "synthetic-workspace-skill.yaml").read_text()
    )
    doc = compiled(workspace_entries(), workspace_revision=3)
    assert {**doc, "compiler": example["compiler"]} == example


def test_workspace_description_names_every_skill_sorted():
    entries = workspace_entries()
    other = [
        TreeEntry(e.path.replace("nightly-export-check", "a-skill"), e.data, e.executable)
        for e in entries
    ]
    other = [
        TreeEntry(e.path, e.data.replace(b"nightly-export-check", b"a-skill"), e.executable)
        for e in other
    ]
    doc = compiled(entries + other, workspace_revision=1)
    assert doc["plugin"]["description"] == "Workspace skill a-skill, nightly-export-check"
    assert doc["plugin"]["version"] == "0.0.0-rev.1"


def test_workspace_revision_is_a_positive_integer():
    for bad in (0, -1, True, "3", 1.5):
        with pytest.raises(ValueError, match="workspace_revision"):
            compiled(workspace_entries(), workspace_revision=bad)


def test_workspace_revision_is_refused_for_a_tree_with_a_plugin_yaml():
    with pytest.raises(ValueError, match="plugin.yaml"):
        compiled(workspace_revision=1)


def test_a_workspace_revision_with_rules_is_still_not_a_workspace_skill_tree():
    entries = put(workspace_entries(), "rules/x.md", "---\nname: x\ndescription: y\n---\nz\n")
    with pytest.raises(PluginInvalid) as info:
        compiled(entries, workspace_revision=1)
    assert info.value.diagnostics[0].code == "E_PLUGIN_MISSING"


def test_an_empty_tree_does_not_compile_even_with_a_workspace_revision():
    with pytest.raises(PluginInvalid):
        compiled([], workspace_revision=1)


def test_compile_entries_of_an_archive_equals_compile_tree_of_the_directory():
    tar = archive.build(kube_entries())
    assert compiled(archive.read(tar)) == compile_tree(load_dir(KUBE_MINI))


# -- rwtask compile -----------------------------------------------------------------------


def test_cli_writes_the_archive_and_compiled_json_and_prints_the_sha(tmp_path, capsys):
    out = tmp_path / "dist"
    assert main(["compile", str(KUBE_MINI), "--out", str(out)]) == 0
    printed = capsys.readouterr().out.strip().splitlines()[-1]
    tar = (out / "kubernetes-0.1.0.tar").read_bytes()
    assert printed == archive.archive_sha256(tar)
    assert tar == archive.build(kube_entries())
    text = (out / "kubernetes-0.1.0.compiled.json").read_text()
    assert (
        text.endswith("}\n")
        and text
        == json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    assert json.loads(text) == compile_tree(load_dir(KUBE_MINI))
    check("compiled-plugin", json.loads(text))
    executable = {e.path for e in archive.read(tar) if e.executable}
    assert executable == {BASE + "scripts/kubectl.sh", HEALTH + "scripts/restart.sh"}


def test_cli_default_out_is_dist_in_the_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["compile", str(KUBE_MINI)]) == 0
    assert (tmp_path / "dist" / "kubernetes-0.1.0.tar").is_file()


def test_cli_excludes_root_tests_and_dotfiles_from_the_archive(tmp_path, capsys):
    root = tmp_path / "plug"
    shutil.copytree(KUBE_MINI, root)
    (root / "tests").mkdir()
    (root / "tests" / "big.yaml").write_text("x: 1")
    (root / ".github").mkdir()
    (root / ".github" / "ci.yaml").write_text("x: 1")
    assert main(["compile", str(root), "--out", str(tmp_path / "o")]) == 0
    names = [e.path for e in archive.read((tmp_path / "o" / "kubernetes-0.1.0.tar").read_bytes())]
    assert not [n for n in names if n.startswith(("tests/", "."))]


def test_cli_prints_diagnostics_and_writes_nothing_on_errors(tmp_path, capsys):
    root = tmp_path / "plug"
    shutil.copytree(KUBE_MINI, root)
    (root / "skills" / "k8s-base" / "scripts" / "kubectl.sh").unlink()
    assert main(["compile", str(root), "--out", str(tmp_path / "o")]) == 1
    assert "E_SCRIPT_MISSING" in capsys.readouterr().out
    assert not (tmp_path / "o").exists()


def test_cli_needs_a_plugin_directory(tmp_path, capsys):
    assert main(["compile", str(tmp_path)]) == 1
    assert "no plugin.yaml" in capsys.readouterr().err
