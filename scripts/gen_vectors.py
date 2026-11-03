#!/usr/bin/env python3
"""Write the cross-language hash vectors, contracts/vectors/*.json.

Each file is {"description": ..., "cases": [{"name", "input", "expected"}]} so
a Go (or any other) implementation can replay it. Bytes are carried as
base64 (`dataBase64`). The case lists below are fixed; `expected` is computed
by the Python reference implementation, and a test fails if the committed
files differ from a fresh run. Run from the repo root:

    PYTHONPATH=src python3 scripts/gen_vectors.py             # rewrite contracts/vectors/
    PYTHONPATH=src python3 scripts/gen_vectors.py --out DIR   # write somewhere else

After rewriting, run `make contracts-sync` to refresh the packaged copy.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any

from runwhen_capability.contracts import (
    TreeEntry,
    canonical_json,
    permission_hash,
    task_hash,
    tree_hash,
)

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "contracts" / "vectors"

CANONICAL_JSON_INPUTS: list[tuple[str, Any]] = [
    ("key order", {"b": 1, "a": 2, "c": {"z": True, "y": False}}),
    ("unicode kept as utf-8", {"name": "naïve", "emoji": "✓ ok", "cjk": "日本"}),
    ("nested dicts and lists", {"outer": {"inner": [{"b": 1, "a": None}, [], {}], "k": "v"}}),
    ("scalars", [True, False, None, 0, -7, 9007199254740993, "", "a b"]),
    ("string escapes", {"s": 'quote " backslash \\ newline \n tab \t control \u0001'}),
    ("empty containers", {"a": [], "b": {}}),
    ("list order is preserved", {"xs": [3, 1, 2, "b", "a"]}),
    ("top-level string", "just a string"),
]

PERMISSION_INPUTS: list[tuple[str, dict[str, Any]]] = [
    (
        "apps deployments and replicasets",
        {
            "apiGroups": ["apps"],
            "resources": ["deployments", "replicasets"],
            "verbs": ["get", "list"],
        },
    ),
    (
        "same rule, shuffled lists and an empty key",
        {
            "verbs": ["list", "get"],
            "resources": ["replicasets", "deployments"],
            "apiGroups": ["apps"],
            "nonResourceURLs": [],
        },
    ),
    (
        "core group is the empty string, not an empty list",
        {"apiGroups": [""], "resources": ["events"], "verbs": ["list"]},
    ),
    (
        "empty apiGroups list is dropped",
        {"apiGroups": [], "resources": ["events"], "verbs": ["list"]},
    ),
    ("non-resource URL", {"nonResourceURLs": ["/version"], "verbs": ["get"]}),
    (
        "wildcards",
        {"apiGroups": ["*"], "resources": ["*"], "verbs": ["get", "list", "watch"]},
    ),
    ("duplicates collapse", {"apiGroups": ["apps"], "verbs": ["get", "get", "list", "get"]}),
    ("null value is dropped", {"apiGroups": ["apps"], "resourceNames": None, "verbs": ["get"]}),
    (
        "nested dict with an empty list inside",
        {"resources": ["pods"], "verbs": ["get"], "selector": {"labels": [], "ns": ["b", "a"]}},
    ),
    ("unicode resource name", {"resourceNames": ["naïve", "café"], "verbs": ["get"]}),
]


def _file(path: str, data: bytes, executable: bool | None = False) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": path, "dataBase64": base64.b64encode(data).decode("ascii")}
    if executable is not None:
        entry["executable"] = executable
    return entry


TREE_INPUTS: list[tuple[str, list[dict[str, Any]]]] = [
    ("empty tree", []),
    ("single file", [_file("SKILL.md", b"# skill\n")]),
    (
        "input order does not matter",
        [_file("b.txt", b"b"), _file("a.txt", b"a"), _file("lib/c.py", b"c = 1\n")],
    ),
    ("executable bit", [_file("run.sh", b"#!/bin/sh\necho hi\n", True)]),
    ("same file without the executable bit", [_file("run.sh", b"#!/bin/sh\necho hi\n", False)]),
    ("empty file", [_file("lib/__init__.py", b"")]),
    (
        "paths sort by utf-8 bytes, not locale",
        [_file("a", b"1"), _file("Z", b"2"), _file("é", b"3"), _file("a/b", b"4")],
    ),
    ("binary content", [_file("data.bin", bytes(range(256)))]),
    ("unicode path", [_file("docs/naïve.md", "naïve\n".encode())]),
]

TASK = {
    "name": "restart-count",
    "description": "List pods restarting in the last hour",
    "script": "tasks/restart-count.sh",
    "params": {"namespace": "default", "threshold": 5},
}

TASK_INPUTS: list[tuple[str, dict[str, Any]]] = [
    ("task with no files", {"task": TASK, "files": []}),
    (
        "task with files",
        {
            "task": TASK,
            "files": [
                _file("tasks/restart-count.sh", b"kubectl get pods\n", None),
                _file("lib/util.sh", b"# helpers\n", None),
            ],
        },
    ),
    (
        "file order does not matter",
        {
            "task": TASK,
            "files": [
                _file("lib/util.sh", b"# helpers\n", None),
                _file("tasks/restart-count.sh", b"kubectl get pods\n", None),
            ],
        },
    ),
    (
        "existing hash key is ignored",
        {
            "task": {**TASK, "hash": "sha256:" + "0" * 64},
            "files": [_file("tasks/restart-count.sh", b"kubectl get pods\n", None)],
        },
    ),
    (
        "task key order does not matter",
        {
            "task": dict(reversed(list(TASK.items()))),
            "files": [_file("tasks/restart-count.sh", b"kubectl get pods\n", None)],
        },
    ),
    ("empty file", {"task": {"name": "t"}, "files": [_file("lib/__init__.py", b"", None)]}),
    (
        "unicode in task and file",
        {
            "task": {"name": "naïve", "description": "café"},
            "files": [_file("notes.md", "naïve\n".encode(), None)],
        },
    ),
    (
        "changed file content changes the hash",
        {
            "task": TASK,
            "files": [_file("tasks/restart-count.sh", b"kubectl get pods -A\n", None)],
        },
    ),
]


def _decode(file: dict[str, Any]) -> bytes:
    return base64.b64decode(file["dataBase64"])


def build() -> dict[str, dict[str, Any]]:
    """filename -> document, for every vector file."""
    return {
        "canonical-json.json": {
            "description": (
                "canonical_json(input) as a UTF-8 string: sorted keys, no whitespace, "
                "non-ASCII kept as UTF-8. Vectors contain no floats."
            ),
            "cases": [
                {"name": name, "input": value, "expected": canonical_json(value).decode("utf-8")}
                for name, value in CANONICAL_JSON_INPUTS
            ],
        },
        "permission-hash.json": {
            "description": (
                "permission_hash(input): sha256 of the canonical JSON of the rule with "
                "None/empty-list keys dropped and every list de-duplicated and sorted "
                "by the canonical JSON of each item."
            ),
            "cases": [
                {"name": name, "input": rule, "expected": permission_hash(rule)}
                for name, rule in PERMISSION_INPUTS
            ],
        },
        "tree-hash.json": {
            "description": (
                "tree_hash(input): sha256 over lines '<sha256(data)> <755|644> <path>\\n' "
                "sorted by the path's UTF-8 bytes. Input is a list of "
                "{path, dataBase64, executable}."
            ),
            "cases": [
                {
                    "name": name,
                    "input": entries,
                    "expected": tree_hash(
                        TreeEntry(e["path"], _decode(e), e["executable"]) for e in entries
                    ),
                }
                for name, entries in TREE_INPUTS
            ],
        },
        "task-hash.json": {
            "description": (
                "task_hash(input.task, files): sha256 of the canonical JSON of "
                "{task: input.task minus 'hash', files: {path: sha256(data)}}. "
                "input.files is a list of {path, dataBase64}."
            ),
            "cases": [
                {
                    "name": name,
                    "input": given,
                    "expected": task_hash(
                        given["task"], {f["path"]: _decode(f) for f in given["files"]}
                    ),
                }
                for name, given in TASK_INPUTS
            ],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="directory to write (default: contracts/vectors)",
    )
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    for filename, doc in build().items():
        text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        (args.out / filename).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
