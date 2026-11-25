"""Helpers shared by tests/plugin: the kube-mini fixture as TreeEntries, and the
runwhen-platform-ops plugin (wiki 42b) built from contracts/examples."""

from __future__ import annotations

import os
from pathlib import Path

from runwhen_capability.contracts import TreeEntry, load_yaml

HERE = Path(__file__).resolve().parent
KUBE_MINI = HERE / "fixtures" / "kube-mini"
EXAMPLES = HERE.parents[1] / "contracts" / "examples"


def dir_entries(root: Path) -> list[TreeEntry]:
    entries = []
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            p = Path(dirpath) / name
            entries.append(
                TreeEntry(
                    p.relative_to(root).as_posix(),
                    p.read_bytes(),
                    bool(p.stat().st_mode & 0o100),
                )
            )
    return sorted(entries, key=lambda e: e.path)


def kube_entries() -> list[TreeEntry]:
    return dir_entries(KUBE_MINI)


def put(entries: list[TreeEntry], path: str, text: str | bytes) -> list[TreeEntry]:
    """entries with `path` added or replaced."""
    data = text.encode() if isinstance(text, str) else text
    return [e for e in entries if e.path != path] + [TreeEntry(path, data)]


def drop(entries: list[TreeEntry], *paths: str) -> list[TreeEntry]:
    return [e for e in entries if e.path not in paths]


def data_of(entries: list[TreeEntry], path: str) -> str:
    return next(e.data for e in entries if e.path == path).decode()


def edit(entries: list[TreeEntry], path: str, old: str, new: str) -> list[TreeEntry]:
    text = data_of(entries, path)
    assert old in text, f"{old!r} not in {path}"
    return put(entries, path, text.replace(old, new, 1))


def platform_ops_entries() -> list[TreeEntry]:
    """runwhen-platform-ops (42b): three skills, requires kubernetes, no integration.
    Scripts and schema files are stubs named by the tasks files."""
    entries = [
        TreeEntry(
            "plugin.yaml",
            (EXAMPLES / "plugin" / "42b-runwhen-platform-ops.yaml").read_bytes(),
        )
    ]
    for skill in ("papi-health", "papi-recovery", "pgbouncer-health"):
        tasks_text = (EXAMPLES / "tasks" / f"42b-{skill}.yaml").read_text()
        entries.append(TreeEntry(f"skills/{skill}/tasks.yaml", tasks_text.encode()))
        md = EXAMPLES / "skill-frontmatter" / f"42b-{skill}.md"
        if md.exists():
            skill_md = md.read_bytes()
        else:
            skill_md = (
                f"---\nname: {skill}\n"
                "description: Recover the task worker when it is hung.\n"
                "runwhen:\n  role: health\n  appliesTo:\n    types: [kubernetes/deployment]\n---\n"
                "Restart the worker only when it is idle and the pool is saturated.\n"
            ).encode()
        entries.append(TreeEntry(f"skills/{skill}/SKILL.md", skill_md))
        for task in load_yaml(tasks_text)["tasks"]:
            entries.append(TreeEntry(f"skills/{skill}/{task['script']}", b"#!/usr/bin/env bash\n"))
            for out in task.get("outputs", {}).values():
                schema = out.get("schema", "")
                if schema.startswith("./"):
                    entries.append(TreeEntry(f"skills/{skill}/{schema[2:]}", b"{}"))
    return entries
