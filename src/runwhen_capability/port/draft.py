"""Draft a V13 skill (SKILL.md + tasks.yaml) and a porting brief from a classic bundle.

The draft is a skeleton: names, inputs, permissions and prose placeholders. It
never carries a classic script's content; the scripts are named in the brief as
reading material, and the skill's scripts are re-derived from the source system
(runwhen-plugins CONTRIBUTING.md, "Porting checklist").
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..contracts import load_yaml
from .robot_model import ClassicBundle, ClassicTask, UserVar

KUBERNETES = "kubernetes"
SKILL_DESCRIPTION_MAX = 1024
TITLE_MAX = 80
NAME_MAX = 64

# Verbatim from runwhen-plugins CONTRIBUTING.md, "Porting checklist".
PORTING_CHECKLIST = """\
1. Generate the draft: `rwtask port <classic-bundle-dir> --plugin <name> --into plugins/<name>/skills/` (Task 5), then merge into the target skill per `porting/parity.yaml`.
2. For each classic task, write in one line what it asserts about the source system (from `porting/inventory/*.yaml` `asserts`). Drop tasks that assert nothing, duplicate another, or need another integration's credential (record `dropped: <reason>` in parity).
3. Re-derive from the source system: read the API object, not the classic script. Python + `ctx.run(["kubectl", ..., "-o", "json"])`; no `oc`, no `2>/dev/null`, no `|| true`, no `// 0` on command output.
4. Outputs are data, not verdicts: lists of objects with the fields an engineer would look at (name, reason, counts, timestamps), plus booleans for threshold crossings driven by settings. No severity, title or next-steps strings.
5. Thresholds and lookbacks are `settings` (admin) or `inputs` (caller) with defaults; lookback defaults ≥ 1h (classic 10m defaults missed most incidents).
6. Errors: every source-system call failure maps to a D64 code via the classifier; per-item failures use `ctx.error` and continue (→ `partial`); a failure that leaves nothing to report raises (→ `failed`).
7. `permissions`: the exact RBAC rules the script needs; target-namespace scope only unless the type is cluster-scoped. Note cluster-scoped needs in SKILL.md.
8. Fixtures: `rwtask test --record` on the kind scenarios for `healthy`, `broken` (the condition the task detects), `forbidden`, `not-found`; commit `tests/`.
9. Bounds: list outputs capped by a `maxItems` setting (default 200); raw logs and dumps go to an `artifact` output.
10. `SKILL.md`: when to use, what each task tells you, how to read the result, what to try next (naming sibling skills by `<plugin>:<skill>/<task>`).
11. Update `porting/parity.yaml` (bundle → skill/tasks, `status: ported`) and `porting/rate.csv` (bundle, start, end, minutes, classic defects found, review defects).
12. `rwtask validate plugins/<name>` and `rwtask test plugins/<name>/skills/<skill>` green before review.
"""  # noqa: E501

# Target identity comes from the resource the skill runs against, never from an input.
_K8S_IDENTITY_VARS = {
    "NAMESPACE",
    "CONTEXT",
    "KUBECONFIG",
    "KUBERNETES_DISTRIBUTION_BINARY",
}
# `<KIND>_NAME` names a target when KIND is one of these (matched on the last one or
# two underscore-separated words before `_NAME`).
_K8S_KINDS = {
    "deployment",
    "statefulset",
    "daemonset",
    "replicaset",
    "workload",
    "pod",
    "node",
    "job",
    "cronjob",
    "service",
    "ingress",
    "pvc",
    "pv",
    "persistentvolumeclaim",
    "persistentvolume",
    "configmap",
    "secret",
    "hpa",
    "cluster",
    "namespace",
    "resource",
    "service_account",
    "serviceaccount",
}

_K8S_TYPES = {
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


@dataclass
class Draft:
    files: dict[str, str] = field(default_factory=dict)
    brief: str = ""
    warnings: list[str] = field(default_factory=list)
    skill_name: str = ""


@dataclass
class _Input:
    var: UserVar
    name: str
    spec: dict[str, Any]


# --- naming -----------------------------------------------------------------


_QUOTED_VAR = re.compile(r"`[^`]*[$@%&]\{[^}]*\}[^`]*`")
_BARE_VAR = re.compile(r"[$@%&]\{[^}]*\}")
_DANGLING = re.compile(
    r"[\s,:;\-]+(?:in namespace|in|for|of|on|from|to|with|and|namespace)$", re.IGNORECASE
)


def clean_title(title: str) -> str:
    """The classic title with `${...}` variables removed and the words they
    leave dangling ("for", "in Namespace") trimmed."""
    removed = _BARE_VAR.sub(" ", _QUOTED_VAR.sub(" ", title))
    changed = removed != title
    cleaned = re.sub(r"\s+", " ", re.sub(r"(['\"])\1", " ", removed)).strip()
    if changed:
        while True:
            trimmed = _DANGLING.sub("", cleaned).strip(" ,:;-")
            if trimmed == cleaned:
                break
            cleaned = trimmed
    return cleaned


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:NAME_MAX].strip("-")


def task_names(tasks: list[ClassicTask]) -> list[str]:
    """One unique slug per task, in order; later duplicates get -2, -3, ..."""
    seen: set[str] = set()
    names: list[str] = []
    for task in tasks:
        base = _slug(clean_title(task.title)) or "task"
        name, n = base, 1
        while name in seen:
            n += 1
            suffix = f"-{n}"
            name = base[: NAME_MAX - len(suffix)].rstrip("-") + suffix
        seen.add(name)
        names.append(name)
    return names


def _task_title(task: ClassicTask) -> str:
    title = clean_title(task.title) or task.title.strip() or "Task"
    return title[:TITLE_MAX].rstrip()


def _camel(var_name: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", var_name) if p]
    if not parts:
        return "input"
    camel = parts[0].lower() + "".join(p.lower().capitalize() for p in parts[1:])
    return camel if camel[0].isalpha() else "v" + camel


def _skill_slug(name: str) -> str:
    return _slug(name) or "skill"


# --- inputs -----------------------------------------------------------------


def _is_k8s_identity(var_name: str) -> bool:
    upper = var_name.upper()
    if upper in _K8S_IDENTITY_VARS:
        return True
    if upper.endswith("_NAME"):
        words = upper[: -len("_NAME")].lower().split("_")
        candidates = {words[-1], "_".join(words[-2:])} if words and words[0] else set()
        return bool(candidates & _K8S_KINDS)
    return False


_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+(\.\d*)?[eE][+-]?\d+)$")


def _typed_default(default: str) -> tuple[str, Any]:
    text = default.strip()
    if _INT.match(text):
        return "integer", int(text)
    if _FLOAT.match(text):
        return "number", float(text)
    if text.lower() in ("true", "false"):
        return "boolean", text.lower() == "true"
    return "string", default


def _build_input(var: UserVar, name: str, warnings: list[str]) -> _Input:
    spec: dict[str, Any] = {}
    if var.default is None:
        spec["type"] = "string"
        spec["required"] = False
        warnings.append(
            f"variable {var.name} has no default; the draft input `{name}` is `required: false` "
            "so it validates. Decide whether it is required, and what a good default is."
        )
    else:
        spec["type"], default = _typed_default(var.default)
        spec["default"] = default
    if var.description:
        spec["description"] = var.description[:1024]
    return _Input(var=var, name=name, spec=spec)


# --- inventory --------------------------------------------------------------


def _inventory_task(inventory: dict | None, task: ClassicTask) -> dict | None:
    """The inventory entry for a classic task: the same title once variables are
    removed, else the same `at: <file>:<line>`."""
    if not inventory:
        return None
    entries = [e for e in inventory.get("tasks") or [] if isinstance(e, dict)]
    wanted = clean_title(task.title).casefold()
    for entry in entries:
        if clean_title(str(entry.get("title", ""))).casefold() == wanted:
            return entry
    for entry in entries:
        if str(entry.get("at", "")) == f"{task.file}:{task.line}":
            return entry
    return None


# --- applies to -------------------------------------------------------------


def _applies_to(bundle: ClassicBundle, plugin: str, warnings: list[str]) -> list[str]:
    if not bundle.resource_types:
        warnings.append(
            "no generation-rule resource types: the skill has no `appliesTo`; add the types "
            "it runs against"
        )
        return []
    if plugin != KUBERNETES:
        warnings.append(
            f"no resource type mapping for plugin {plugin!r}; classic types "
            f"{bundle.resource_types} were not carried to `appliesTo`"
        )
        return []
    types: list[str] = []
    for classic in bundle.resource_types:
        canonical = _K8S_TYPES.get(classic.lower())
        if canonical is None:
            warnings.append(f"classic resource type {classic!r} has no V13 mapping; omitted")
        elif f"kubernetes/{canonical}" not in types:
            types.append(f"kubernetes/{canonical}")
    if not types:
        warnings.append("none of the classic resource types mapped: the skill has no `appliesTo`")
    return types


def _skill_tags(bundle: ClassicBundle) -> list[str]:
    counts: Counter[str] = Counter()
    for task in bundle.tasks:
        for tag in task.tags:
            if ":" in tag:
                continue
            slug = _slug(tag)
            if len(slug) >= 2:
                counts[slug] += 1
    return [tag for tag, _ in counts.most_common(5)]


# --- rendering --------------------------------------------------------------


def _dump(doc: Any) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text[-1] in ".!?" else text + "."


def _skill_md(
    bundle: ClassicBundle,
    skill: str,
    types: list[str],
    task_list: list[tuple[ClassicTask, str]],
) -> str:
    parts = [_sentence(bundle.display_name or bundle.name)]
    summary = bundle.doc or bundle.readme_summary
    if summary:
        parts.append(_sentence(re.sub(r"\s+", " ", summary)))
    parts.append(
        "Use when TODO: describe the symptoms, alerts or questions that select this skill."
    )
    description = " ".join(parts)[:SKILL_DESCRIPTION_MAX]

    runwhen: dict[str, Any] = {"role": "health"}
    if types:
        runwhen["appliesTo"] = {"types": types}
    tags = _skill_tags(bundle)
    if tags:
        runwhen["tags"] = tags
    frontmatter = {"name": skill, "description": description, "runwhen": runwhen}

    bullets = "\n".join(f"- **{name}** -- TODO: what this task tells you." for _, name in task_list)
    body = (
        "## When to use\n\n"
        "TODO: the situations, symptoms or alerts where this skill is the right one.\n\n"
        "## What each task tells you\n\n"
        f"{bullets}\n\n"
        "## How to read the result\n\n"
        "TODO: how to interpret the outputs, and which ones to read together.\n\n"
        "## What to try next\n\n"
        "TODO: the sibling skills and tasks (`<plugin>:<skill>/<task>`) to run next.\n"
    )
    return f"---\n{_dump(frontmatter)}---\n{body}"


def _brief_scripts(bundle: ClassicBundle, task: ClassicTask) -> list[str]:
    paths: list[str] = []
    for script in task.scripts:
        found = None
        if bundle.path is not None:
            matches = sorted(bundle.path.rglob(script))
            if matches:
                found = matches[0].relative_to(bundle.path).as_posix()
        paths.append(found or f"{script} (not found in the bundle)")
    return paths


def _fmt(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def _brief(
    bundle: ClassicBundle,
    plugin: str,
    skill: str,
    inventory: dict | None,
    inventory_missing: bool,
    task_list: list[tuple[ClassicTask, str]],
    dispositions: list[tuple[UserVar, str]],
    warnings: list[str],
) -> str:
    if inventory:
        inventory_state = "found an entry for this bundle"
    elif inventory_missing:
        inventory_state = f"given, but no entry for {bundle.name}"
    else:
        inventory_state = "none"
    out: list[str] = [f"# Porting brief: {bundle.name}", ""]
    out += [
        f"- Classic bundle: `{bundle.name}`"
        + (f" ({bundle.display_name})" if bundle.display_name else ""),
        f"- Target: plugin `{plugin}`, skill `{skill}`",
        f"- Classic resource types: {', '.join(f'`{t}`' for t in bundle.resource_types) or 'none'}",
        f"- Inventory: {inventory_state}",
        "",
        "The draft skill is a skeleton. Re-derive each script from the source system; the "
        "classic scripts below are reading material only and are never copied.",
        "",
        "## Classic tasks",
        "",
    ]
    for task, name in task_list:
        entry = _inventory_task(inventory, task)
        out += [
            f"### {task.title}",
            "",
            f"- Draft task: `{name}`",
            f"- Source: `{task.file}:{task.line}`",
            f"- Doc: {_fmt(task.doc) or 'none'}",
        ]
        scripts = _brief_scripts(bundle, task)
        out.append(
            "- Scripts (reading material only): " + ", ".join(f"`{s}`" for s in scripts)
            if scripts
            else "- Scripts: none"
        )
        if entry:
            out.append(f"- Asserts: {_fmt(entry.get('asserts', ''))}")
            reads = entry.get("reads") or []
            if reads:
                out.append("- Reads:")
                out += [f"  - {_fmt(r)}" for r in reads]
            out.append(f"- Scope: {_fmt(entry.get('scope', ''))}")
            false_pass = _fmt(entry.get("falsePass", ""))
            why = _fmt(entry.get("falsePassWhy", ""))
            out.append(f"- False pass: {false_pass}" + (f" -- {why}" if why else ""))
            notes = _fmt(entry.get("notes", ""))
            if notes:
                out.append(f"- Notes: {notes}")
        elif inventory:
            out.append("- Inventory: no entry for this task")
        out.append("")

    out += ["## SLI tasks", ""]
    if bundle.sli_tasks:
        for task in bundle.sli_tasks:
            line = f"- {task.title} (`{task.file}:{task.line}`)"
            scripts = _brief_scripts(bundle, task)
            if scripts:
                line += "; scripts (reading material only): " + ", ".join(f"`{s}`" for s in scripts)
            out.append(line)
        if inventory and inventory.get("sliAsserts"):
            out += ["", f"Inventory, what the SLI asserts: {_fmt(inventory['sliAsserts'])}"]
    else:
        out.append("None.")
    out.append("")

    out += ["## Classic variables", ""]
    if dispositions:
        out += ["| Variable | Default | Draft |", "|---|---|---|"]
        for var, disposition in dispositions:
            default = "(none)" if var.default is None else f"`{var.default}`"
            out.append(f"| `{var.name}` | {default} | {disposition} |")
    else:
        out.append("None.")
    out.append("")

    out += ["## Credential", ""]
    if bundle.secrets:
        out.append(
            "Secrets are never inputs; the connection supplies the credential. The bundle "
            "imports: " + ", ".join(f"`{s}`" for s in bundle.secrets) + "."
        )
    else:
        out.append("The bundle imports no secrets.")
    out.append("")

    out += ["## Warnings", ""]
    out += [f"- {w}" for w in warnings] or ["None."]
    out += ["", "## Porting checklist", "", PORTING_CHECKLIST.rstrip("\n"), ""]
    return "\n".join(out)


# --- entry point ------------------------------------------------------------


def draft_skill(
    bundle: ClassicBundle,
    plugin: str,
    inventory: dict | None = None,
    skill_name: str | None = None,
    inventory_missing: bool = False,
) -> Draft:
    """Draft SKILL.md and tasks.yaml (relative to the skill dir) plus the brief.

    `inventory` is the CNT-1 inventory entry for this bundle (a mapping with
    `tasks[]`), if there is one. `inventory_missing` says an inventory file was
    given but had no entry for this bundle; the brief and warnings record it.
    """
    if not bundle.tasks:
        raise ValueError(
            f"bundle {bundle.name!r} has no runbook tasks to port "
            "(an SLI-only bundle: port its SLI tasks by hand from the brief)"
        )
    warnings: list[str] = []
    if inventory_missing and not inventory:
        warnings.append(f"inventory has no entry for bundle {bundle.name}")
    kubernetes = plugin == KUBERNETES
    skill = _skill_slug(skill_name or bundle.name)

    inputs: list[_Input] = []
    dispositions: list[tuple[UserVar, str]] = []
    used: set[str] = set()
    for var in bundle.user_vars:
        if kubernetes and _is_k8s_identity(var.name):
            dispositions.append((var, "dropped: target identity comes from the resource"))
            continue
        name, n = _camel(var.name), 1
        while name in used:
            n += 1
            name = f"{_camel(var.name)}{n}"
        used.add(name)
        inputs.append(_build_input(var, name, warnings))
        dispositions.append((var, f"input `{name}`"))

    names = task_names(bundle.tasks)
    task_list = list(zip(bundle.tasks, names, strict=True))
    tasks_doc: list[dict[str, Any]] = []
    for task, name in task_list:
        entry = _inventory_task(inventory, task)
        read_only: bool | None = entry.get("readOnly") if entry else None
        if not isinstance(read_only, bool):
            if entry:
                warnings.append(
                    f"task {name}: inventory entry for {task.title!r} has no readOnly; using tags"
                )
            elif inventory:
                warnings.append(
                    f"task {name}: no inventory entry for {task.title!r}; "
                    "readOnly comes from the classic tags"
                )
            read_only = "access:read-write" not in task.tags
        item: dict[str, Any] = {
            "name": name,
            "title": _task_title(task),
            "description": (_fmt(task.doc) or _task_title(task))[:1024],
            "script": f"scripts/{name.replace('-', '_')}.py",
            "readOnly": read_only,
        }
        if not read_only:
            item["effects"] = ["TODO: describe the change"]
            item["destructive"] = False
            warnings.append(f"task {name} writes (readOnly: false): fill in `effects`")
        permissions = entry.get("permissions") if entry else None
        if (
            isinstance(permissions, list)
            and permissions
            and all(isinstance(p, dict) for p in permissions)
        ):
            item["permissions"] = permissions
        if inputs:
            item["inputs"] = {i.name: dict(i.spec) for i in inputs}
        tasks_doc.append(item)

    tasks_file: dict[str, Any] = {
        "apiVersion": "runwhen.com/tasks/v1",
        "runtime": "kubernetes@1" if kubernetes else "generic@1",
    }
    if kubernetes:
        tasks_file["defaults"] = {"credentials": "kubernetes/kubeconfig"}
    else:
        warnings.append(
            f"plugin {plugin!r}: no credential type is set; add `defaults.credentials` "
            "(a type of the plugin's integration)"
        )
    tasks_file["tasks"] = tasks_doc

    types = _applies_to(bundle, plugin, warnings)
    skill_md = _skill_md(bundle, skill, types, task_list)
    brief = _brief(
        bundle, plugin, skill, inventory, inventory_missing, task_list, dispositions, warnings
    )
    return Draft(
        files={"SKILL.md": skill_md, "tasks.yaml": _dump(tasks_file)},
        brief=brief,
        warnings=warnings,
        skill_name=skill,
    )


def load_inventory_entry(path: Path, bundle_name: str) -> dict | None:
    """The entry for `bundle_name` in an inventory file (a YAML list of bundle
    entries, or a mapping with a `bundles` list); None when absent."""
    doc = load_yaml(Path(path).read_text(encoding="utf-8"))  # F19: YAML 1.2, so `no` stays a string
    entries = doc.get("bundles") if isinstance(doc, dict) else doc
    if entries is not None and not isinstance(entries, list):
        raise ValueError(
            f"inventory {path}: expected a list of bundle entries, "
            f"or a mapping with a `bundles` list; got {type(entries).__name__}"
        )
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("bundle") == bundle_name:
            return entry
    return None
