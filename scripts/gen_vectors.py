#!/usr/bin/env python3
"""Write the cross-language vectors: contracts/vectors/*.json (hashes) and
contracts/vectors/archive/ (golden plugin archives).

Each file is {"description": ..., "cases": [{"name", "input", "expected"}]} so
a Go (or any other) implementation can replay it. Bytes are carried as
base64 (`dataBase64`). Each archive case is a <case>.json (entries, sha256, size)
plus the exact bytes <case>.tar. The case lists below are fixed; `expected` is computed
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
    archive_sha256,
    canonical_json,
    permission_hash,
    task_hash,
)
from runwhen_capability.contracts.archive import build as build_archive

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
    ("integral floats are integers", [1.0, -0.0, 0.0, -3.0, 100.0, 1e20]),
    ("fractional floats", {"a": 0.5, "b": 1.25, "c": -2.5, "d": 0.1, "e": 123.456}),
    ("small floats are positional", [1e-6, 1.5e-5, 0.0001, 0.00025]),
    ("large integral float keeps shortest digits", [1.2345678901234568e20, 9007199254740993.0]),
    ("floats in a document", {"params": {"threshold": 0.75, "retries": 3.0, "name": "x"}}),
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
        "float defaults are canonicalized",
        {
            "task": {"name": "t", "inputs": {"ratio": {"type": "number", "default": 0.5}}},
            "files": [],
        },
    ),
    (
        "integral float equals the integer",
        {
            "task": {"name": "t", "inputs": {"ratio": {"type": "number", "default": 2.0}}},
            "files": [],
        },
    ),
    (
        "integer default",
        {
            "task": {"name": "t", "inputs": {"ratio": {"type": "number", "default": 2}}},
            "files": [],
        },
    ),
    (
        "small float default",
        {
            "task": {"name": "t", "inputs": {"ratio": {"type": "number", "default": 0.00002}}},
            "files": [],
        },
    ),
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


_LONG_DIR = "/".join(["skills", "d" * 40, "e" * 40])  # 88 bytes
_LONG_PATH = f"{_LONG_DIR}/{'f' * 30}.md"  # 122 bytes: needs the ustar prefix field

# case -> (description, entries in the order they are given to build())
ARCHIVE_CASES: dict[str, tuple[str, list[TreeEntry]]] = {
    "single-file": (
        "One regular file.",
        [TreeEntry("plugin.yaml", b"apiVersion: runwhen.com/plugin/v1\nname: demo\n")],
    ),
    "nested-executable": (
        "Nested paths and an executable script; no directory entries are written.",
        [
            TreeEntry("skills/k8s/run.sh", b"#!/bin/sh\necho hi\n", executable=True),
            TreeEntry("plugin.yaml", b"name: demo\n"),
            TreeEntry("skills/k8s/SKILL.md", b"# k8s\n"),
        ],
    ),
    "long-path-prefix-split": (
        "A 122-byte path: written with the longest ustar prefix that fits "
        "(prefix = the first 88 bytes, name = the last 33).",
        [TreeEntry("plugin.yaml", b"name: demo\n"), TreeEntry(_LONG_PATH, b"long path\n")],
    ),
    "unicode-nfc-path": (
        "A non-ASCII (NFC, precomposed) path, written as UTF-8.",
        [TreeEntry("knowledge/caf\u00e9/na\u00efve.md", "na\u00efve caf\u00e9\n".encode())],
    ),
    "empty-file": (
        "A zero-length file: a header and no content blocks.",
        [TreeEntry("lib/__init__.py", b""), TreeEntry("plugin.yaml", b"name: demo\n")],
    ),
    "ten-files-out-of-order": (
        "Ten files given out of order: entries are written sorted by the UTF-8 bytes of the "
        "path (uppercase before lowercase, 'a.txt' before 'a/b.txt').",
        [
            TreeEntry(path, data, executable)
            for path, data, executable in (
                ("skills/b/SKILL.md", b"b\n", False),
                ("a.txt", b"a\n", False),
                ("plugin.yaml", b"name: demo\n", False),
                ("Z.md", b"Z\n", False),
                ("skills/a/run.sh", b"#!/bin/sh\n", True),
                ("a/b.txt", b"a/b\n", False),
                ("rules/no-delete.md", b"never delete\n", False),
                ("knowledge/runbook.md", b"restart it\n", False),
                ("integration/integration.yaml", b"name: demo\n", False),
                ("skills/a/SKILL.md", b"a\n", False),
            )
        ],
    ),
}


def build_archives() -> dict[str, bytes]:
    """relative path under the vectors dir -> bytes, for each archive case."""
    out: dict[str, bytes] = {}
    for case, (description, entries) in ARCHIVE_CASES.items():
        archive = build_archive(entries)
        doc = {
            "description": description,
            "entries": [
                {
                    "path": e.path,
                    "dataBase64": base64.b64encode(e.data).decode("ascii"),
                    "executable": e.executable,
                }
                for e in entries
            ],
            "sha256": archive_sha256(archive),
            "size": len(archive),
        }
        out[f"archive/{case}.json"] = (
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        ).encode()
        out[f"archive/{case}.tar"] = archive
    return out


def _decode(file: dict[str, Any]) -> bytes:
    return base64.b64decode(file["dataBase64"])


def build() -> dict[str, dict[str, Any]]:
    """filename -> document, for every vector file."""
    return {
        "canonical-json.json": {
            "description": (
                "canonical_json(input) as a UTF-8 string: sorted keys, no whitespace, "
                "non-ASCII kept as UTF-8, floats per RFC 8785 (an integral float with abs < 1e21 "
                "is written as an integer, e.g. 1.0 -> 1 and -0.0 -> 0; other floats with "
                "1e-6 <= abs < 1e21 use the shortest round-trip digits in positional form, "
                "never exponent form; floats outside that range are an error and have no "
                "vector)."
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
        "task-hash.json": {
            "description": (
                "task_hash(input.task, files): sha256 of the canonical JSON of "
                "{task: input.task minus 'hash', files: {path: sha256(data)}}, with the task "
                "serialized by the canonical-json rules (including floats). "
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

    files = {
        name: (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        for name, doc in build().items()
    }
    files.update(build_archives())
    for name, data in files.items():
        target = args.out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
