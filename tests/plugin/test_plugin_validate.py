"""runwhen_capability.plugin.validate: schema and semantic checks, one test per code."""

from __future__ import annotations

import shutil

from _plugin_fixtures import (
    KUBE_MINI,
    drop,
    edit,
    kube_entries,
    platform_ops_entries,
    put,
)

from runwhen_capability.plugin import (
    PluginInvalid,
    load_dir,
    load_entries,
    load_skill_dir,
    validate_entries,
    validate_tree,
)

TASKS = "skills/k8s-base/tasks.yaml"
HEALTH_TASKS = "skills/k8s-workload-health/tasks.yaml"
HEALTH_SKILL = "skills/k8s-workload-health/SKILL.md"
INTEGRATION = "integration/integration.yaml"
TYPES = "integration/types.yaml"


def found(entries, code):
    return [d for d in validate_entries(entries) if d.code == code]


def only(entries, code):
    hits = found(entries, code)
    assert len(hits) == 1, validate_entries(entries)
    return hits[0]


def errors(entries):
    return [d for d in validate_entries(entries) if d.severity == "error"]


def test_the_kube_mini_fixture_validates_clean():
    assert validate_entries(kube_entries()) == []
    assert validate_tree(load_dir(KUBE_MINI)) == []


def test_platform_ops_validates_with_only_unchecked_type_warnings():
    diags = validate_entries(platform_ops_entries())
    assert not [d for d in diags if d.severity == "error"], diags
    assert diags, "kubernetes types and credentials are not checkable without AUT-1"
    assert {d.code for d in diags} == {"W_TYPE_UNCHECKED"}
    assert all(d.severity == "warning" and d.file for d in diags)
    assert any(d.path == "/runwhen/appliesTo/types/0" for d in diags)
    assert any("kubernetes/kubeconfig" in d.message for d in diags)


def test_loader_diagnostics_are_part_of_the_result():
    entries = put(kube_entries(), "stray.txt", "x")
    assert [d.code for d in validate_entries(entries)] == ["E_PATH_NOT_ALLOWED"]


def test_a_broken_file_is_reported_once_without_cascading_schema_errors():
    entries = put(kube_entries(), "plugin.yaml", "name: [unclosed\n")
    assert [d.code for d in validate_entries(entries)] == ["E_YAML"]
    entries = put(kube_entries(), HEALTH_TASKS, "tasks: [\n")
    assert [d.code for d in validate_entries(entries)] == ["E_YAML"]
    entries = put(kube_entries(), HEALTH_SKILL, "no frontmatter\n")
    assert [d.code for d in validate_entries(entries)] == ["E_YAML"]


def test_missing_plugin_yaml():
    d = only(drop(kube_entries(), "plugin.yaml"), "E_PLUGIN_MISSING")
    assert d.file == "plugin.yaml"


def test_a_workspace_skill_tree_needs_no_plugin_yaml():
    entries = [e for e in kube_entries() if e.path.startswith("skills/k8s-workload-health/")]
    assert not errors(entries)


def test_e_schema_carries_file_and_json_pointer():
    d = only(edit(kube_entries(), "plugin.yaml", "0.1.0", "latest"), "E_SCHEMA")
    assert (d.file, d.path, d.severity) == ("plugin.yaml", "/version", "error")
    hits = found(edit(kube_entries(), TASKS, "readOnly: true", "readonly: true"), "E_SCHEMA")
    assert hits and all(d.file == TASKS and d.path.startswith("/tasks/0") for d in hits)


def test_integration_schema_violations_name_the_file_the_key_came_from():
    entries = edit(
        kube_entries(),
        TYPES,
        "pod: { parent: namespace, plural",
        "pod: { parent: namespace, pluralx",
    )
    hits = found(entries, "E_SCHEMA")
    assert hits and all(d.file == TYPES and d.path.startswith("/types/pod") for d in hits)
    d = only(edit(kube_entries(), INTEGRATION, "title: Kubernetes", "title: ''"), "E_SCHEMA")
    assert (d.file, d.path) == (INTEGRATION, "/title")


def test_a_missing_integration_platform_is_reported_on_integration_yaml():
    entries = edit(kube_entries(), INTEGRATION, "platform: kubernetes\n", "")
    d = only(entries, "E_SCHEMA")
    assert d.file == INTEGRATION and "platform" in d.message


def test_frontmatter_and_knowledge_schema_violations():
    entries = edit(
        kube_entries(), "skills/k8s-base/SKILL.md", "role: [discovery, cli]", "role: nope"
    )
    assert only(entries, "E_SCHEMA").file == "skills/k8s-base/SKILL.md"
    entries = edit(
        kube_entries(),
        "rules/one.md",
        "description: Prefer",
        "appliesTo: {types: [a/b]}\ndescription: Prefer",
    )
    assert only(entries, "E_SCHEMA").file == "rules/one.md"
    entries = edit(
        kube_entries(), "knowledge/one.md", "tags: [restarts]", "tags: [restarts]\nbogus: 1"
    )
    assert only(entries, "E_SCHEMA").file == "knowledge/one.md"


def test_connections_yaml_is_validated():
    entries = put(kube_entries(), "connections.yaml", "connections: not-a-list\n")
    d = only(entries, "E_SCHEMA")
    assert d.file == "connections.yaml"
    entries = put(kube_entries(), "connections.yaml", "a: [\n")
    assert only(entries, "E_YAML").file == "connections.yaml"


def test_skill_name_must_equal_the_folder():
    d = only(
        edit(kube_entries(), HEALTH_SKILL, "name: k8s-workload-health", "name: other"),
        "E_SKILL_NAME_MISMATCH",
    )
    assert (d.file, d.path) == (HEALTH_SKILL, "/name")


def test_rule_and_knowledge_name_must_equal_the_file_stem():
    d = only(edit(kube_entries(), "rules/one.md", "name: one", "name: two"), "E_FILE_NAME_MISMATCH")
    assert (d.file, d.path) == ("rules/one.md", "/name")
    d = only(
        edit(kube_entries(), "knowledge/one.md", "name: one", "name: two"), "E_FILE_NAME_MISMATCH"
    )
    assert d.file == "knowledge/one.md"


def test_script_must_exist():
    d = only(drop(kube_entries(), "skills/k8s-base/scripts/kubectl.sh"), "E_SCRIPT_MISSING")
    assert (d.file, d.path) == (TASKS, "/tasks/1/script")


def test_script_may_not_leave_scripts_dir():
    entries = edit(
        kube_entries(), TASKS, "script: scripts/kubectl.sh", "script: scripts/../lib/x.sh"
    )
    entries = put(entries, "skills/k8s-base/lib/x.sh", "x")
    assert only(entries, "E_SCRIPT_MISSING").path == "/tasks/1/script"


def test_output_schema_file_must_exist():
    d = only(
        drop(kube_entries(), "skills/k8s-workload-health/schemas/rollout.v1.json"),
        "E_OUTPUT_SCHEMA_MISSING",
    )
    assert (d.file, d.path) == (HEALTH_TASKS, "/tasks/0/outputs/rollout/schema")


def test_compact_output_schema_must_compile():
    entries = edit(
        kube_entries(), HEALTH_TASKS, "{ stuck: boolean, reason: string? }", "{ stuck: bool"
    )
    d = only(entries, "E_SCHEMA_NOTATION")
    assert (d.file, d.path) == (HEALTH_TASKS, "/tasks/0/outputs/summary/schema")


def test_output_schema_file_must_be_named_name_v_n_json():
    entries = edit(kube_entries(), HEALTH_TASKS, "rollout.v1.json", "rollout.json")
    entries = [
        e.__class__("skills/k8s-workload-health/schemas/rollout.json", e.data)
        if e.path.endswith("rollout.v1.json")
        else e
        for e in entries
    ]
    d = only(entries, "E_SCHEMA_FILE_NAME")
    assert d.path == "/tasks/0/outputs/rollout/schema"
    assert not found(entries, "E_OUTPUT_SCHEMA_MISSING")


def test_entry_point_task_must_be_in_this_plugin():
    d = only(
        edit(kube_entries(), INTEGRATION, "task: k8s-base/kubectl", "task: k8s-base/nope"),
        "E_ENTRY_POINT_UNKNOWN",
    )
    assert (d.file, d.path) == (INTEGRATION, "/entryPoints/cli/task")
    entries = edit(kube_entries(), INTEGRATION, "task: k8s-base/discover", "task: ghost/discover")
    assert only(entries, "E_ENTRY_POINT_UNKNOWN").path == "/entryPoints/discovery/task"


def test_entry_point_into_a_skill_with_a_broken_tasks_file_is_not_reported_twice():
    entries = put(kube_entries(), TASKS, "tasks: [\n")
    assert {d.code for d in validate_entries(entries)} == {"E_YAML"}


def test_applies_to_type_of_this_integration_must_be_declared():
    entries = edit(kube_entries(), HEALTH_SKILL, "kubernetes/deployment", "kubernetes/widget")
    d = only(entries, "E_UNKNOWN_TYPE")
    assert (d.file, d.path) == (HEALTH_SKILL, "/runwhen/appliesTo/types/0")
    entries = edit(kube_entries(), "knowledge/one.md", "kubernetes/deployment", "kubernetes/widget")
    d = only(entries, "E_UNKNOWN_TYPE")
    assert (d.file, d.path) == ("knowledge/one.md", "/appliesTo/types/0")


def test_a_type_of_a_required_platform_is_a_warning_not_an_error():
    entries = edit(
        kube_entries(), "plugin.yaml", "license: Apache-2.0", "requires: { redis: ^0.1 }"
    )
    entries = edit(entries, HEALTH_SKILL, "kubernetes/deployment", "redis/instance")
    d = only(entries, "W_TYPE_UNCHECKED")
    assert d.severity == "warning" and d.path == "/runwhen/appliesTo/types/0"
    assert not errors(entries)


def test_a_type_of_an_extended_platform_is_a_warning_not_an_error():
    entries = edit(
        kube_entries(), "plugin.yaml", "license: Apache-2.0", "extends: { openshift: ^0.1 }"
    )
    entries = edit(entries, HEALTH_SKILL, "kubernetes/deployment", "openshift/route")
    assert only(entries, "W_TYPE_UNCHECKED").severity == "warning"
    assert not errors(entries)


def test_a_type_of_an_unrelated_platform_is_an_error():
    entries = edit(kube_entries(), HEALTH_SKILL, "kubernetes/deployment", "mystery/thing")
    d = only(entries, "E_UNKNOWN_PLATFORM")
    assert d.path == "/runwhen/appliesTo/types/0" and "mystery" in d.message


def test_a_workspace_skill_cannot_check_types_so_it_warns(tmp_path):
    skill = tmp_path / "k8s-workload-health"
    shutil.copytree(KUBE_MINI / "skills" / "k8s-workload-health", skill)
    diags = validate_tree(load_skill_dir(skill))
    assert not [d for d in diags if d.severity == "error"], diags
    assert {d.code for d in diags} == {"W_TYPE_UNCHECKED"}


def test_credential_type_must_be_declared_by_this_integration():
    entries = edit(
        kube_entries(),
        HEALTH_TASKS,
        "credentials: kubernetes/kubeconfig",
        "credentials: kubernetes/token",
    )
    ds = found(entries, "E_CREDENTIAL_TYPE_UNKNOWN")
    assert [(d.file, d.path) for d in ds] == [(HEALTH_TASKS, "/defaults/credentials")]


def test_credential_of_an_unrelated_platform_is_an_error_a_required_one_a_warning():
    entries = kube_entries()
    entries = edit(
        entries,
        TASKS,
        'permissions: [{ apiGroups: ["*"]',
        'credentials: [{ type: other/cred }]\n    permissions: [{ apiGroups: ["*"]',
    )
    d = only(entries, "E_CREDENTIAL_TYPE_UNKNOWN")
    assert d.path == "/tasks/1/credentials/0"
    entries = edit(entries, "plugin.yaml", "license: Apache-2.0", "requires: { other: ^1 }")
    assert not errors(entries)
    assert [d.code for d in validate_entries(entries)] == ["W_TYPE_UNCHECKED"]


def test_generic_credential_types_are_always_known():
    entries = edit(
        kube_entries(),
        HEALTH_TASKS,
        "credentials: kubernetes/kubeconfig",
        "credentials: [generic/bearer, {type: generic/env, vars: [A]}]",
    )
    assert validate_entries(entries) == []


def test_runwhen_sync_is_allowed_only_on_the_discovery_entry_point():
    d = only(
        edit(
            kube_entries(),
            TASKS,
            "script: scripts/kubectl.sh",
            "script: scripts/kubectl.sh\n    credentials: [kubernetes/kubeconfig, runwhen/sync]",
        ),
        "E_CREDENTIAL_TYPE_UNKNOWN",
    )
    assert d.path == "/tasks/1/credentials/1" and "runwhen/sync" in d.message
    entries = edit(
        kube_entries(),
        INTEGRATION,
        'discovery: { task: k8s-base/discover, schedule: "*/15 * * * *" }',
        "cli2: {}",
    )
    assert found(entries, "E_CREDENTIAL_TYPE_UNKNOWN")


def test_a_task_permission_must_satisfy_the_integration_permission_schema():
    entries = edit(kube_entries(), HEALTH_TASKS, "verbs: [get, list] }", "verbs: [explode] }")
    d = only(entries, "E_PERMISSION_SCHEMA")
    assert d.file == HEALTH_TASKS and d.path.startswith("/tasks/0/permissions/0")
    entries = edit(
        kube_entries(),
        HEALTH_TASKS,
        "{ apiGroups: [apps], resources: [deployments], verbs: [patch] }",
        "{ apiGroups: [apps], verbs: [patch], extra: 1 }",
    )
    assert found(entries, "E_PERMISSION_SCHEMA")


def test_the_permission_schema_file_must_exist_and_be_a_json_schema():
    d = only(drop(kube_entries(), "integration/schemas/rbac-rule.json"), "E_PERMISSION_SCHEMA")
    assert (d.file, d.path) == (INTEGRATION, "/permissions/schema")
    d = only(
        put(kube_entries(), "integration/schemas/rbac-rule.json", "{not json"),
        "E_PERMISSION_SCHEMA",
    )
    assert d.path == "/permissions/schema"
    d = only(
        put(kube_entries(), "integration/schemas/rbac-rule.json", '{"type": 12}'),
        "E_PERMISSION_SCHEMA",
    )
    assert d.path == "/permissions/schema"


def test_skills_relying_on_a_required_integration_are_not_checked_against_ours():
    entries = edit(kube_entries(), "plugin.yaml", "license: Apache-2.0", "requires: { other: ^1 }")
    entries = edit(
        entries, HEALTH_TASKS, "credentials: kubernetes/kubeconfig", "credentials: other/token"
    )
    entries = edit(entries, HEALTH_TASKS, "verbs: [get, list] }", "verbs: [explode] }")
    assert not errors(entries)


def test_type_chain_unknown_parent():
    d = only(
        edit(
            kube_entries(),
            TYPES,
            "parent: namespace, plural: deployments",
            "parent: ghost, plural: deployments",
        ),
        "E_TYPE_CHAIN",
    )
    assert (d.file, d.path) == (TYPES, "/types/deployment/parent")


def test_type_chain_cycle():
    entries = put(
        kube_entries(),
        TYPES,
        "types:\n  cluster: { root: true, rootKey: server, plural: clusters }\n"
        "  a: { parent: b, plural: as }\n  b: { parent: a, plural: bs }\n",
    )
    hits = [d for d in found(entries, "E_TYPE_CHAIN") if "cycle" in d.message]
    assert {d.path for d in hits} == {"/types/a/parent", "/types/b/parent"}


def test_type_chain_needs_exactly_one_root():
    entries = edit(
        kube_entries(),
        TYPES,
        "pod: { parent: namespace, plural: pods, aliases: [po], ephemeral: true }",
        "pod: { root: true, rootKey: x, plural: pods }",
    )
    d = only(entries, "E_TYPE_CHAIN")
    assert d.path == "/types" and "one root" in d.message
    entries = put(kube_entries(), TYPES, "types:\n  a: { parent: a, plural: as }\n")
    assert any(d.path == "/types" for d in found(entries, "E_TYPE_CHAIN"))


def test_a_type_may_have_an_extended_platforms_type_as_parent():
    route_only = "types:\n  route: { parent: other/namespace, plural: routes }\n"
    entries = edit(kube_entries(), "plugin.yaml", "license: Apache-2.0", "extends: { other: ^1 }")
    entries = put(entries, TYPES, route_only)
    entries = edit(entries, HEALTH_SKILL, "kubernetes/deployment", "kubernetes/route")
    entries = edit(entries, "skills/k8s-base/SKILL.md", "kubernetes/cluster", "kubernetes/route")
    entries = edit(entries, "knowledge/one.md", "kubernetes/deployment", "kubernetes/route")
    assert not errors(entries)
    # without `extends`, the parent's platform is unknown
    assert found(put(kube_entries(), TYPES, route_only), "E_UNKNOWN_PLATFORM")


FACETS = (
    "facets:\n  - key: replicas\n    title: Replicas\n    appliesTo: { types: [deployment] }\n"
    "    populator: { kind: agent }\n    volatility: state\n"
    "    schema: ./schemas/replicas.v1.json\n"
)


def test_facet_schema_file_must_exist():
    entries = put(kube_entries(), "integration/facets.yaml", FACETS)
    d = only(entries, "E_FACET_SCHEMA_MISSING")
    assert (d.file, d.path) == ("integration/facets.yaml", "/facets/0/schema")
    entries = put(entries, "integration/schemas/replicas.v1.json", "{}")
    assert validate_entries(entries) == []


def test_auth_script_must_exist_under_integration():
    method = (
        "  gke:\n    produces: kubernetes/kubeconfig\n    accepts: [gcp-sa-key]\n"
        "    script: auth/gke.py\n"
    )
    entries = edit(kube_entries(), INTEGRATION, "permissions:", method + "permissions:")
    d = only(entries, "E_AUTH_SCRIPT_MISSING")
    assert (d.file, d.path) == (INTEGRATION, "/authMethods/gke/script")
    assert validate_entries(put(entries, "integration/auth/gke.py", "x")) == []


def rule(name: str, words: int) -> str:
    return f"---\nname: {name}\ndescription: A rule\n---\n" + " ".join(["word"] * words) + "\n"


def test_rule_body_budget_is_80_words():
    assert validate_entries(put(kube_entries(), "rules/one.md", rule("one", 80))) == []
    d = only(put(kube_entries(), "rules/one.md", rule("one", 81)), "E_RULE_BUDGET")
    assert (d.file, d.path) == ("rules/one.md", None) and "81" in d.message


def test_a_plugin_has_at_most_10_rules():
    entries = kube_entries()
    for i in range(9):
        entries = put(entries, f"rules/r{i:02d}.md", rule(f"r{i:02d}", 5))
    assert validate_entries(entries) == []  # one.md + 9 = 10
    entries = put(entries, "rules/r09.md", rule("r09", 5))
    d = only(entries, "E_RULE_BUDGET")
    assert "11" in d.message


def test_task_names_are_unique_within_a_skill():
    entries = edit(kube_entries(), TASKS, "- name: kubectl", "- name: discover")
    d = only(entries, "E_TASK_NAME_DUPLICATE")
    assert (d.file, d.path) == (TASKS, "/tasks/1/name")


def test_validate_entries_equals_validate_tree_of_load_entries():
    entries = edit(kube_entries(), HEALTH_SKILL, "kubernetes/deployment", "mystery/thing")
    assert validate_entries(entries) == validate_tree(load_entries(entries))


def test_plugin_invalid_carries_the_error_diagnostics_only():
    diags = validate_entries(
        edit(kube_entries(), HEALTH_SKILL, "kubernetes/deployment", "mystery/thing")
    )
    warn = validate_entries(platform_ops_entries())
    exc = PluginInvalid(diags + warn)
    assert isinstance(exc, ValueError)
    assert [d.code for d in exc.diagnostics] == ["E_UNKNOWN_PLATFORM"]
    assert "E_UNKNOWN_PLATFORM" in str(exc)


def test_skill_body_is_at_most_500_lines():
    body = "line\n" * 499
    entries = edit(kube_entries(), HEALTH_SKILL, "`rollout-status` first.", body)
    assert not found(entries, "E_SKILL_BODY_TOO_LONG")
    entries = edit(kube_entries(), HEALTH_SKILL, "`rollout-status` first.", body + "one more\n")
    d = only(entries, "E_SKILL_BODY_TOO_LONG")
    assert d.file == HEALTH_SKILL and "501" in d.message


def test_object_input_schema_file_must_exist_and_be_json():
    inputs = "      v: { type: object, schema: ./schemas/filter.v1.json }\n"
    since = "      since: { type: duration, default: 30m, description: Look-back window }\n"
    entries = edit(kube_entries(), HEALTH_TASKS, since, inputs)
    d = only(entries, "E_INPUT_SCHEMA_MISSING")
    assert (d.file, d.path) == (HEALTH_TASKS, "/tasks/0/inputs/v/schema")
    ok = put(entries, "skills/k8s-workload-health/schemas/filter.v1.json", "{}")
    assert validate_entries(ok) == []
    bad = put(entries, "skills/k8s-workload-health/schemas/filter.v1.json", "[1]")
    assert only(bad, "E_SCHEMA_FILE_INVALID").path == "/tasks/0/inputs/v/schema"
    compact = edit(
        kube_entries(), HEALTH_TASKS, since, "      v: { type: object, schema: '{ x: nope' }\n"
    )
    assert only(compact, "E_SCHEMA_NOTATION").path == "/tasks/0/inputs/v/schema"


def test_schema_files_must_be_json_objects():
    entries = put(kube_entries(), "skills/k8s-workload-health/schemas/rollout.v1.json", "{nope")
    d = only(entries, "E_SCHEMA_FILE_INVALID")
    assert d.path == "/tasks/0/outputs/rollout/schema"
    entries = put(kube_entries(), "integration/facets.yaml", FACETS)
    entries = put(entries, "integration/schemas/replicas.v1.json", "42")
    assert only(entries, "E_SCHEMA_FILE_INVALID").path == "/facets/0/schema"
