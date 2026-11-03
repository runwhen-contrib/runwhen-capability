"""`rwtask port`: classic Robot bundle -> V13 skill draft + porting brief (CNT-2).

Fixtures: two real classic bundles under fixtures/ (Apache-2.0, same org) and the
matching CNT-1 inventory entries. Bundles that need scripts, an SLI or another
plugin are built in tmp_path.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("robot")

from runwhen_capability import cli  # noqa: E402
from runwhen_capability.contracts import validate  # noqa: E402
from runwhen_capability.contracts._yaml12 import load_yaml, split_frontmatter  # noqa: E402
from runwhen_capability.port import draft as draft_mod  # noqa: E402
from runwhen_capability.port import robot_model  # noqa: E402
from runwhen_capability.port.draft import (  # noqa: E402
    PORTING_CHECKLIST,
    clean_title,
    draft_skill,
    load_inventory_entry,
    task_names,
)
from runwhen_capability.port.robot_model import ClassicTask, read_bundle  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
REDIS = FIXTURES / "k8s-redis-healthcheck"
RESTART = FIXTURES / "k8s-restart-resource"
INVENTORY = FIXTURES / "inventory.yaml"

SCRIPT_SECRET_LINE = 'echo "distinctive-classic-script-line-7f3a" | jq .items'

ROBOT_HEADER = """\
*** Settings ***
Documentation       {doc}
Metadata            Display Name    {display}
Library             RW.Core
Suite Setup         Suite Initialization

"""

ROBOT_KEYWORDS = """
*** Keywords ***
Suite Initialization
    ${kubeconfig}=    RW.Core.Import Secret
    ...    kubeconfig
    ...    type=string
    ${NAMESPACE}=    RW.Core.Import User Variable    NAMESPACE
    ...    type=string
    ...    description=The namespace.
    ${CONTAINER_RESTART_THRESHOLD}=    RW.Core.Import User Variable    CONTAINER_RESTART_THRESHOLD
    ...    type=string
    ...    description=Restarts that count as unhealthy.
    ...    default=1
    ${RATIO}=    RW.Core.Import User Variable    RATIO
    ...    description=A ratio.
    ...    default=0.5
    ${STRICT}=    RW.Core.Import User Variable    STRICT
    ...    default=True
    ${REDIS_HOST}=    RW.Core.Import User Variable    REDIS_HOST
    ...    description=Host = name:port
"""


def write_bundle(
    root: Path,
    name: str = "demo-bundle",
    *,
    runbook: str | None = None,
    sli: str | None = None,
    keywords: str = ROBOT_KEYWORDS,
    rules: str | None = None,
    readme: str | None = None,
) -> Path:
    bundle = root / name
    bundle.mkdir(parents=True)
    head = ROBOT_HEADER.format(doc="Checks the demo workload.", display="Demo Health")
    if runbook is not None:
        (bundle / "runbook.robot").write_text(head + runbook + keywords)
    if sli is not None:
        (bundle / "sli.robot").write_text(head + sli + keywords)
    if rules is not None:
        rules_dir = bundle / ".runwhen" / "generation-rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "rules.yaml").write_text(rules)
    if readme is not None:
        (bundle / "README.md").write_text(readme)
    (bundle / "check_things.sh").write_text(f"#!/bin/bash\n{SCRIPT_SECRET_LINE}\n")
    return bundle


RUNBOOK = """\
*** Tasks ***
Check Things for `${NAMESPACE}` Restarts
    [Documentation]    Checks things.
    [Tags]    access:read-only  data:config  restarts
    ${out}=    RW.CLI.Run Cli
    ...    cmd_override=./check_things.sh "${NAMESPACE}"
    ...    bash_file=check_things.sh

Wipe Things
    [Tags]    access:read-write
    No Operation
"""

SLI = """\
*** Tasks ***
Get Things Score
    [Tags]    access:read-only
    ${out}=    RW.CLI.Run Cli    cmd=bash check_things.sh
"""

RULES = """\
apiVersion: runwhen.com/v1
kind: GenerationRules
spec:
  generationRules:
    - resourceTypes:
        - deployment
        - statefulset
        - helmreleases.helm.toolkit.fluxcd.io
    - resourceTypes: [deployment, mystery.example.com]
"""


def check_valid(draft) -> tuple[dict, dict]:
    """Rule 7: both files validate; returns (frontmatter, tasks doc)."""
    fm, _body = split_frontmatter(draft.files["SKILL.md"])
    assert validate("skill-frontmatter", fm) == []
    tasks = load_yaml(draft.files["tasks.yaml"])
    assert validate("tasks", tasks) == []
    return fm, tasks


def redis_draft(**kwargs):
    inventory = load_inventory_entry(INVENTORY, "k8s-redis-healthcheck")
    return draft_skill(read_bundle(REDIS), "kubernetes", inventory=inventory, **kwargs)


# --- robot_model ------------------------------------------------------------


def test_read_bundle_reads_the_redis_runbook():
    bundle = read_bundle(REDIS)
    assert bundle.name == "k8s-redis-healthcheck"
    assert bundle.display_name == "Kubernetes Redis Healthcheck"
    assert bundle.doc.startswith("This taskset collects information on your redis workload")
    assert bundle.readme_summary.startswith("A set of tasks which performs a health check")
    assert bundle.secrets == ["kubeconfig"]
    assert bundle.resource_types == ["deployment"]
    assert bundle.sli_tasks == []
    assert [(t.title, t.line) for t in bundle.tasks] == [
        ("Ping `${DEPLOYMENT_NAME}` Redis Workload", 20),
        ("Verify `${DEPLOYMENT_NAME}` Redis Read Write Operation in Kubernetes", 44),
    ]
    assert bundle.tasks[0].doc.startswith("Verifies that a PING")
    assert "access:read-only" in bundle.tasks[0].tags
    by_name = {v.name: v for v in bundle.user_vars}
    assert list(by_name) == [
        "KUBERNETES_DISTRIBUTION_BINARY",
        "CONTEXT",
        "NAMESPACE",
        "DEPLOYMENT_NAME",
        "REDIS_HEALTHCHECK_KEY",
    ]
    assert by_name["KUBERNETES_DISTRIBUTION_BINARY"].default == "kubectl"
    assert by_name["CONTEXT"].default is None
    assert by_name["NAMESPACE"].default == ""
    assert by_name["CONTEXT"].pattern == r"\w*"
    assert by_name["REDIS_HEALTHCHECK_KEY"].description.startswith("The key used")


def test_read_bundle_tolerates_no_runbook_and_no_rules(tmp_path):
    bundle = read_bundle(write_bundle(tmp_path, sli=SLI))
    assert bundle.tasks == []
    assert [t.title for t in bundle.sli_tasks] == ["Get Things Score"]
    assert bundle.sli_tasks[0].file == "sli.robot"
    assert bundle.sli_tasks[0].scripts == ["check_things.sh"]
    assert bundle.resource_types == []
    assert bundle.display_name == "Demo Health"
    assert bundle.readme_summary == ""


def test_read_bundle_extracts_scripts_and_values_with_equals(tmp_path):
    bundle = read_bundle(write_bundle(tmp_path, runbook=RUNBOOK, rules=RULES))
    assert bundle.tasks[0].scripts == ["check_things.sh"]
    assert bundle.tasks[1].scripts == []
    host = next(v for v in bundle.user_vars if v.name == "REDIS_HOST")
    assert host.description == "Host = name:port"
    assert bundle.resource_types == [
        "deployment",
        "statefulset",
        "helmreleases.helm.toolkit.fluxcd.io",
        "mystery.example.com",
    ]


def test_read_bundle_readme_summary_is_the_first_paragraph(tmp_path):
    readme = "# Title\n\nFirst line\nsecond line.\n\nSecond paragraph.\n"
    bundle = read_bundle(write_bundle(tmp_path, runbook=RUNBOOK, readme=readme))
    assert bundle.readme_summary == "First line second line."


def test_missing_robotframework_is_a_clear_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "robot" or name.startswith("robot."):
            raise ImportError("No module named 'robot'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(robot_model.PortDependencyError, match=r"runwhen-capability\[port\]"):
        read_bundle(REDIS)


# --- rule 1: task names -----------------------------------------------------


def test_task_name_strips_variables_and_dangling_words():
    title = "Check Deployment Replica Status for `${DEPLOYMENT_NAME}` in Namespace `${NAMESPACE}`"
    assert task_names([ClassicTask(title=title)]) == ["check-deployment-replica-status"]
    assert clean_title("Restart Resource with Labels `${LABELS}` in `${CONTEXT}`") == (
        "Restart Resource with Labels"
    )
    assert clean_title("Ping `${DEPLOYMENT_NAME}` Redis Workload") == "Ping Redis Workload"
    # A title that never had a variable is left alone.
    assert clean_title("Top Pods in Namespace") == "Top Pods in Namespace"


def test_task_names_are_unique_and_at_most_64_chars():
    long = "Inspect " + "very long words " * 12 + "`${X}`"
    names = task_names(
        [
            ClassicTask(title="Check `${A}`"),
            ClassicTask(title="Check `${B}`"),
            ClassicTask(title="Check `${C}`"),
            ClassicTask(title=long),
            ClassicTask(title=long),
        ]
    )
    assert names[:3] == ["check", "check-2", "check-3"]
    assert len(set(names)) == 5
    assert all(len(n) <= 64 for n in names)
    assert all(n == n.strip("-") and "--" not in n for n in names)


# --- rule 2: identity vars never inputs -------------------------------------


def test_kubernetes_identity_variables_are_dropped_and_secrets_are_not_inputs():
    draft = redis_draft()
    _, tasks = check_valid(draft)
    for task in tasks["tasks"]:
        assert list(task["inputs"]) == ["redisHealthcheckKey"]
    brief = draft.brief
    for dropped in ("NAMESPACE", "CONTEXT", "DEPLOYMENT_NAME", "KUBERNETES_DISTRIBUTION_BINARY"):
        assert f"| `{dropped}` | " in brief
        assert f"`{dropped}` | " in brief and "dropped" in brief
    assert "## Credential" in brief and "`kubeconfig`" in brief


def test_identity_name_rule_covers_k8s_kinds_only(tmp_path):
    keywords = "\n*** Keywords ***\nSuite Initialization\n" + "".join(
        f"    ${{{v}}}=    RW.Core.Import User Variable    {v}\n"
        for v in (
            "STATEFULSET_NAME",
            "DAEMONSET_NAME",
            "WORKLOAD_NAME",
            "PVC_NAME",
            "INGRESS_NAME",
            "SERVICE_NAME",
            "kubeconfig",
            "KUBECONFIG",
            "CONTAINER_NAME",
            "LABELS",
        )
    )
    path = write_bundle(tmp_path, runbook=RUNBOOK, keywords=keywords)
    draft = draft_skill(read_bundle(path), "kubernetes")
    _, tasks = check_valid(draft)
    assert list(tasks["tasks"][0]["inputs"]) == ["containerName", "labels"]


def test_other_plugins_keep_every_variable_and_list_them_in_the_brief(tmp_path):
    path = write_bundle(tmp_path, runbook=RUNBOOK)
    draft = draft_skill(read_bundle(path), "postgres")
    _, tasks = check_valid(draft)
    assert list(tasks["tasks"][0]["inputs"]) == [
        "namespace",
        "containerRestartThreshold",
        "ratio",
        "strict",
        "redisHost",
    ]
    for var in ("NAMESPACE", "CONTAINER_RESTART_THRESHOLD", "RATIO", "STRICT", "REDIS_HOST"):
        assert f"`{var}`" in draft.brief
    assert "dropped" not in draft.brief.split("## Classic variables")[1].split("## Credential")[0]


# --- rule 3: inputs ---------------------------------------------------------


def test_inputs_are_camel_case_typed_and_carry_defaults(tmp_path):
    path = write_bundle(tmp_path, runbook=RUNBOOK)
    draft = draft_skill(read_bundle(path), "kubernetes")
    _, tasks = check_valid(draft)
    inputs = tasks["tasks"][0]["inputs"]
    assert inputs["containerRestartThreshold"] == {
        "type": "integer",
        "default": 1,
        "description": "Restarts that count as unhealthy.",
    }
    assert inputs["ratio"] == {"type": "number", "default": 0.5, "description": "A ratio."}
    assert inputs["strict"] == {"type": "boolean", "default": True}
    assert inputs["redisHost"]["type"] == "string"


def test_input_without_default_is_not_required_and_warns(tmp_path):
    path = write_bundle(tmp_path, runbook=RUNBOOK)
    draft = draft_skill(read_bundle(path), "kubernetes")
    _, tasks = check_valid(draft)
    host = tasks["tasks"][0]["inputs"]["redisHost"]
    assert host["required"] is False and "default" not in host
    assert any("REDIS_HOST has no default" in w for w in draft.warnings)
    # an empty default is a default
    redis = redis_draft()
    key = load_yaml(redis.files["tasks.yaml"])["tasks"][0]["inputs"]["redisHealthcheckKey"]
    assert key["default"] == "runwhen_task_rw_healthcheck" and "required" not in key


# --- rule 4: readOnly, effects, permissions ---------------------------------


def test_read_only_comes_from_the_inventory_then_the_tags():
    draft = redis_draft()
    _, tasks = check_valid(draft)
    ping, rw = tasks["tasks"]
    # inventory wins over the classic access:read-only tag
    assert ping["readOnly"] is True and "effects" not in ping
    assert rw["readOnly"] is False
    assert rw["effects"] == ["TODO: describe the change"]
    assert rw["destructive"] is False
    verify = "verify-redis-read-write-operation-in-kubernetes"
    assert any(f"{verify} writes" in w for w in draft.warnings)
    assert {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create"]} in ping[
        "permissions"
    ]


def test_read_only_falls_back_to_tags_and_permissions_are_omitted(tmp_path):
    path = write_bundle(tmp_path, runbook=RUNBOOK)
    draft = draft_skill(read_bundle(path), "kubernetes")
    _, tasks = check_valid(draft)
    check, wipe = tasks["tasks"]
    assert check["readOnly"] is True and wipe["readOnly"] is False
    assert wipe["effects"] and wipe["destructive"] is False
    assert "permissions" not in check and "permissions" not in wipe


def test_inventory_permissions_must_be_rbac_shaped(tmp_path):
    path = write_bundle(tmp_path, runbook=RUNBOOK)
    entry = {
        "bundle": "demo-bundle",
        "tasks": [
            {"title": "Check Things", "readOnly": True, "permissions": ["get pods"]},
            {"title": "Wipe Things", "readOnly": False, "permissions": [{"verbs": ["patch"]}]},
        ],
    }
    draft = draft_skill(read_bundle(path), "kubernetes", inventory=entry)
    _, tasks = check_valid(draft)
    assert "permissions" not in tasks["tasks"][0]
    assert tasks["tasks"][1]["permissions"] == [{"verbs": ["patch"]}]


def test_inventory_matches_titles_ignoring_variables():
    inventory = load_inventory_entry(INVENTORY, "k8s-restart-resource")
    draft = draft_skill(read_bundle(RESTART), "kubernetes", inventory=inventory)
    _, tasks = check_valid(draft)
    restart = tasks["tasks"][2]
    assert restart["name"] == "restart-resource-with-labels"
    assert restart["readOnly"] is False
    assert restart["permissions"] == [
        {"apiGroups": ["apps"], "resources": ["daemonsets", "deployments", "statefulsets"],
         "verbs": ["get", "list", "patch"]}
    ]  # fmt: skip


# --- rule 5: names, scripts, tasks.yaml header ------------------------------


def test_task_script_title_and_description():
    _, tasks = check_valid(redis_draft())
    assert tasks["apiVersion"] == "runwhen.com/tasks/v1"
    assert tasks["runtime"] == "kubernetes@1"
    assert tasks["defaults"] == {"credentials": "kubernetes/kubeconfig"}
    first = tasks["tasks"][0]
    assert first["name"] == "ping-redis-workload"
    assert first["title"] == "Ping Redis Workload"
    assert first["script"] == "scripts/ping_redis_workload.py"
    assert first["description"].startswith("Verifies that a PING")
    second = tasks["tasks"][1]
    assert second["script"] == "scripts/verify_redis_read_write_operation_in_kubernetes.py"


def test_description_falls_back_to_the_title_and_titles_are_capped(tmp_path):
    runbook = "*** Tasks ***\n" + ("Very Long Title " * 8).strip() + "\n    No Operation\n"
    draft = draft_skill(read_bundle(write_bundle(tmp_path, runbook=runbook)), "kubernetes")
    _, tasks = check_valid(draft)
    task = tasks["tasks"][0]
    assert len(task["title"]) <= 80
    assert task["description"] == task["title"]


def test_other_plugins_use_generic_runtime_without_credentials_and_warn(tmp_path):
    draft = draft_skill(read_bundle(write_bundle(tmp_path, runbook=RUNBOOK)), "postgres")
    _, tasks = check_valid(draft)
    assert tasks["runtime"] == "generic@1"
    assert "defaults" not in tasks
    assert any("credential" in w for w in draft.warnings)


# --- rule 6: SKILL.md -------------------------------------------------------


def test_skill_frontmatter_and_body():
    draft = redis_draft()
    fm, body = split_frontmatter(draft.files["SKILL.md"])
    assert fm["name"] == "k8s-redis-healthcheck"
    assert fm["runwhen"]["role"] == "health"
    assert fm["runwhen"]["appliesTo"] == {"types": ["kubernetes/deployment"]}
    assert fm["runwhen"]["tags"] and all("access" not in t for t in fm["runwhen"]["tags"])
    desc = fm["description"]
    assert len(desc) <= 1024
    assert desc.startswith("Kubernetes Redis Healthcheck.")
    assert "This taskset collects information" in desc and "Use when TODO" in desc
    for heading in (
        "## When to use",
        "## What each task tells you",
        "## How to read the result",
        "## What to try next",
    ):
        assert heading in body
    assert body.count("TODO") >= 4
    assert "- **ping-redis-workload** -- TODO" in body
    assert "- **verify-redis-read-write-operation-in-kubernetes** -- TODO" in body


def test_skill_name_override():
    draft = redis_draft(skill_name="redis-health")
    fm, _ = check_valid(draft)
    assert fm["name"] == "redis-health" and draft.skill_name == "redis-health"


def test_applies_to_maps_known_types_and_warns_on_unknown(tmp_path):
    path = write_bundle(tmp_path, runbook=RUNBOOK, rules=RULES)
    draft = draft_skill(read_bundle(path), "kubernetes")
    fm, _ = check_valid(draft)
    assert fm["runwhen"]["appliesTo"]["types"] == [
        "kubernetes/deployment",
        "kubernetes/statefulset",
        "kubernetes/helmrelease",
    ]
    assert any("mystery.example.com" in w for w in draft.warnings)


def test_every_documented_kubernetes_type_maps():
    expected = {
        "deployment": "deployment",
        "statefulset": "statefulset",
        "stateful_set": "statefulset",
        "daemonset": "daemonset",
        "daemon_set": "daemonset",
        "namespace": "namespace",
        "cluster": "cluster",
        "persistentvolumeclaim": "persistentvolumeclaim",
        "ingress": "ingress",
        "service": "service",
        "job": "job",
        "cronjob": "cronjob",
        "helmreleases.helm.toolkit.fluxcd.io": "helmrelease",
        "kustomizations.kustomize.toolkit.fluxcd.io": "kustomization",
        "applications.argoproj.io": "application",
        "certificates.cert-manager.io": "certificate",
        "prometheuses.monitoring.coreos.com": "prometheus",
        "postgresclusters.postgres-operator.crunchydata.com": "postgrescluster",
        "postgresqls.acid.zalan.do": "postgresql",
    }
    assert draft_mod._K8S_TYPES == expected


def test_no_resource_types_means_no_applies_to_and_a_warning():
    draft = draft_skill(read_bundle(RESTART), "kubernetes")
    fm, _ = check_valid(draft)
    assert "appliesTo" not in fm["runwhen"]
    assert any("appliesTo" in w for w in draft.warnings)


def test_other_plugin_types_are_not_guessed(tmp_path):
    path = write_bundle(tmp_path, runbook=RUNBOOK, rules=RULES)
    draft = draft_skill(read_bundle(path), "postgres")
    fm, _ = check_valid(draft)
    assert "appliesTo" not in fm["runwhen"]
    assert any("no resource type mapping" in w for w in draft.warnings)


# --- rule 7: both drafts validate -------------------------------------------


@pytest.mark.parametrize(
    "bundle_dir, name", [(REDIS, "k8s-redis-healthcheck"), (RESTART, "k8s-restart-resource")]
)
def test_fixture_drafts_validate(bundle_dir, name):
    inventory = load_inventory_entry(INVENTORY, name)
    check_valid(draft_skill(read_bundle(bundle_dir), "kubernetes", inventory=inventory))
    check_valid(draft_skill(read_bundle(bundle_dir), "kubernetes"))


def test_a_bundle_with_only_an_sli_cannot_be_drafted(tmp_path):
    bundle = read_bundle(write_bundle(tmp_path, sli=SLI))
    with pytest.raises(ValueError, match="no runbook tasks"):
        draft_skill(bundle, "kubernetes")


# --- rule 8: the brief ------------------------------------------------------


def test_brief_lists_tasks_inventory_variables_and_the_checklist():
    brief = redis_draft().brief
    assert "### Ping `${DEPLOYMENT_NAME}` Redis Workload" in brief
    assert "`runbook.robot:20`" in brief and "`runbook.robot:44`" in brief
    assert "Verifies that a PING can be peformed" in brief
    assert "- Asserts: kubectl exec deployment/NAME -- redis-cli PING does not output PONG" in brief
    assert "- Reads:\n  - kubectl exec deployment/DEPLOYMENT_NAME" in brief
    assert "- Scope: ns:target" in brief
    assert "- False pass: none" in brief
    assert "- Notes: Empty output (exec Forbidden" in brief
    assert brief.index("## Classic tasks") < brief.index("## SLI tasks")
    assert brief.index("## SLI tasks") < brief.index("## Classic variables")
    assert brief.index("## Classic variables") < brief.index("## Credential")
    assert brief.index("## Credential") < brief.index("## Warnings")
    assert brief.index("## Warnings") < brief.index("## Porting checklist")
    checklist = brief.split("## Porting checklist")[1]
    assert checklist.strip() == PORTING_CHECKLIST.strip()


def test_false_pass_and_why_appear_for_inventory_tasks():
    inventory = load_inventory_entry(INVENTORY, "k8s-restart-resource")
    brief = draft_skill(read_bundle(RESTART), "kubernetes", inventory=inventory).brief
    assert "- False pass: confirmed -- runbook.robot:48-59 no rc or stderr check" in brief


def test_checklist_is_the_twelve_item_contributing_list():
    lines = PORTING_CHECKLIST.strip().splitlines()
    assert len(lines) == 12
    assert [line.split(".")[0] for line in lines] == [str(n) for n in range(1, 13)]
    assert "rwtask port <classic-bundle-dir>" in lines[0]
    assert lines[11].startswith("12. `rwtask validate plugins/<name>`")


def test_brief_names_scripts_relative_to_the_bundle_and_lists_sli_tasks(tmp_path):
    path = write_bundle(tmp_path, runbook=RUNBOOK, sli=SLI)
    brief = draft_skill(read_bundle(path), "kubernetes").brief
    assert "- Scripts (reading material only): `check_things.sh`" in brief
    sli_section = brief.split("## SLI tasks")[1].split("## Classic variables")[0]
    assert "Get Things Score (`sli.robot:" in sli_section
    assert "`check_things.sh`" in sli_section


# --- rule 9: no classic script content --------------------------------------


def test_no_script_content_in_any_output(tmp_path):
    path = write_bundle(tmp_path, runbook=RUNBOOK, sli=SLI)
    assert SCRIPT_SECRET_LINE in (path / "check_things.sh").read_text()
    draft = draft_skill(read_bundle(path), "kubernetes")
    everything = "\n".join([*draft.files.values(), draft.brief])
    assert "distinctive-classic-script-line-7f3a" not in everything
    assert "check_things.sh" in draft.brief  # named, not copied


# --- CLI --------------------------------------------------------------------


def run_port(tmp_path, *extra, bundle=REDIS):
    args = ["port", str(bundle), "--plugin", "kubernetes", "--into", str(tmp_path / "skills")]
    return cli.main([*args, *extra])


def test_cli_writes_skill_and_brief_outside_the_skill_dir(tmp_path, capsys):
    rc = run_port(tmp_path, "--inventory", str(INVENTORY))
    assert rc == 0
    skill = tmp_path / "skills" / "k8s-redis-healthcheck"
    assert sorted(p.name for p in skill.iterdir()) == ["SKILL.md", "tasks.yaml"]
    brief = tmp_path / "porting" / "briefs" / "k8s-redis-healthcheck.md"
    assert brief.is_file() and "## Porting checklist" in brief.read_text()
    fm, _ = split_frontmatter((skill / "SKILL.md").read_text())
    assert validate("skill-frontmatter", fm) == []
    assert validate("tasks", load_yaml((skill / "tasks.yaml").read_text())) == []
    err = capsys.readouterr().err
    assert "warning: task verify-redis-read-write-operation-in-kubernetes writes" in err


def test_cli_refuses_to_overwrite_without_force(tmp_path, capsys):
    assert run_port(tmp_path) == 0
    skill = tmp_path / "skills" / "k8s-redis-healthcheck"
    (skill / "SKILL.md").write_text("hand edited\n")
    capsys.readouterr()
    assert run_port(tmp_path) == 1
    assert "refusing to overwrite" in capsys.readouterr().err
    assert (skill / "SKILL.md").read_text() == "hand edited\n"
    assert run_port(tmp_path, "--force") == 0
    assert (skill / "SKILL.md").read_text().startswith("---\n")


def test_cli_refusal_writes_nothing(tmp_path):
    brief = tmp_path / "elsewhere" / "brief.md"
    brief.parent.mkdir()
    brief.write_text("mine\n")
    assert run_port(tmp_path, "--brief", str(brief)) == 1
    assert not (tmp_path / "skills").exists()


def test_cli_skill_and_brief_options(tmp_path):
    brief = tmp_path / "notes" / "redis.md"
    assert run_port(tmp_path, "--skill", "redis-health", "--brief", str(brief)) == 0
    assert (tmp_path / "skills" / "redis-health" / "tasks.yaml").is_file()
    assert brief.is_file()


def test_cli_rejects_a_brief_inside_the_skill_dir(tmp_path, capsys):
    inside = tmp_path / "skills" / "k8s-redis-healthcheck" / "BRIEF.md"
    assert run_port(tmp_path, "--brief", str(inside)) == 1
    assert "outside the skill directory" in capsys.readouterr().err
    assert not (tmp_path / "skills").exists()


def test_cli_warns_when_the_inventory_lacks_the_bundle(tmp_path, capsys):
    assert run_port(tmp_path, "--inventory", str(INVENTORY), bundle=_copy(tmp_path)) == 0
    assert "has no entry for bundle other-bundle" in capsys.readouterr().err


def _copy(tmp_path: Path) -> Path:
    target = tmp_path / "src" / "other-bundle"
    shutil.copytree(REDIS, target)
    return target


def test_cli_reports_a_missing_bundle_and_missing_robot(tmp_path, capsys, monkeypatch):
    assert run_port(tmp_path, bundle=tmp_path / "nope") == 1
    assert "is not a directory" in capsys.readouterr().err

    def boom(path):
        raise robot_model.PortDependencyError("install runwhen-capability[port]")

    monkeypatch.setattr("runwhen_capability.port.robot_model.read_bundle", boom)
    assert run_port(tmp_path) == 1
    assert "runwhen-capability[port]" in capsys.readouterr().err


def test_cli_reports_a_malformed_inventory(tmp_path, capsys):
    for text in ("tasks: [", "42\n"):
        bad = tmp_path / "bad.yaml"
        bad.write_text(text)
        assert run_port(tmp_path, "--inventory", str(bad)) == 1
        err = capsys.readouterr().err
        assert err.startswith("rwtask port:") and "Traceback" not in err
    assert not (tmp_path / "skills").exists()


def test_inventory_top_level_must_be_a_list_or_mapping(tmp_path):
    bad = tmp_path / "scalar.yaml"
    bad.write_text("42\n")
    with pytest.raises(ValueError, match="list of bundle entries"):
        load_inventory_entry(bad, "demo-bundle")


def test_inventory_is_read_as_yaml_1_2(tmp_path):
    inv = tmp_path / "inventory.yaml"
    inv.write_text("- bundle: demo-bundle\n  notes: no\n  tasks: []\n")
    assert load_inventory_entry(inv, "demo-bundle")["notes"] == "no"


def test_brief_and_warnings_say_the_inventory_has_no_entry(tmp_path):
    path = write_bundle(tmp_path, runbook=RUNBOOK)
    draft = draft_skill(read_bundle(path), "kubernetes", inventory_missing=True)
    assert "- Inventory: given, but no entry for demo-bundle" in draft.brief
    warning = "inventory has no entry for bundle demo-bundle"
    assert warning in draft.warnings
    warnings_section = draft.brief.split("## Warnings")[1].split("## Porting checklist")[0]
    assert warning in warnings_section
    # not given at all stays "none"
    plain = draft_skill(read_bundle(path), "kubernetes")
    assert "- Inventory: none" in plain.brief
    assert not any("inventory" in w for w in plain.warnings)


def test_task_variables_are_collected_from_task_bodies(tmp_path):
    runbook = """\
*** Tasks ***
Check Things
    [Tags]    access:read-only
    ${LOOKBACK}=    RW.Core.Import User Variable    LOOKBACK
    ...    description=Window.
    ...    default=1h
    ${NAMESPACE}=    RW.Core.Import User Variable    NAMESPACE
    ${tok}=    RW.Core.Import Secret    task_secret
    ${kc}=    RW.Core.Import Secret    kubeconfig
"""
    path = write_bundle(tmp_path, runbook=runbook)
    bundle = read_bundle(path)
    by_name = {v.name: v for v in bundle.user_vars}
    assert by_name["LOOKBACK"].default == "1h"
    assert [v.name for v in bundle.user_vars].count("NAMESPACE") == 1
    assert bundle.secrets.count("kubeconfig") == 1 and "task_secret" in bundle.secrets


def test_readonly_missing_from_an_existing_entry_is_named_in_the_warning(tmp_path):
    path = write_bundle(tmp_path, runbook=RUNBOOK)
    entry = {
        "bundle": "demo-bundle",
        "tasks": [{"title": "Check Things for Restarts", "asserts": "x"}, {"title": "Wipe Things"}],
    }
    draft = draft_skill(read_bundle(path), "kubernetes", inventory=entry)
    assert any(
        "inventory entry for 'Check Things for `${NAMESPACE}` Restarts' has no readOnly; "
        "using tags" in w
        for w in draft.warnings
    )
    # and a task with no entry at all keeps the old wording
    entry["tasks"] = [{"title": "Wipe Things", "readOnly": False}]
    draft = draft_skill(read_bundle(path), "kubernetes", inventory=entry)
    assert any("no inventory entry for" in w for w in draft.warnings)
