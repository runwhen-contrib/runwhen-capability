#!/usr/bin/env python3
"""Write the cross-language vectors: contracts/vectors/*.json (hashes) and
contracts/vectors/archive/ (golden plugin archives).

Each file is {"description": ..., "cases": [{"name", "input", "expected"}]} so
a Go (or any other) implementation can replay it. Bytes are carried as
base64 (`dataBase64`). Each archive case is a <case>.json (entries, sha256, size)
plus the exact bytes <case>.tar; archive/invalid/ holds hand-built malicious archives
(<case>.json {description, expectedCode} + <case>.tar) that every reader must reject.
The case lists below are fixed; `expected` is computed by the Python reference
implementation, and a test fails if the committed files differ from a fresh run.
Run from the repo root:

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


def _raw_header(
    name: bytes,
    size: int | bytes,
    typeflag: bytes = b"0",
    *,
    linkname: bytes = b"",
    magic: bytes = b"ustar\x0000",
    realsize: int | None = None,
    checksum_delta: int = 0,
) -> bytes:
    """A 512-byte ustar header written by hand (no tar library), so malicious
    archives can be made that a library's own writer would refuse to produce.
    `size` is an octal number or the raw 12-byte field; `checksum_delta` breaks
    the checksum."""
    header = bytearray(512)
    header[0 : len(name)] = name
    header[100:108] = b"0000644\x00"
    header[108:124] = b"0000000\x00" * 2
    header[124:136] = size if isinstance(size, bytes) else f"{size:011o}".encode() + b"\x00"
    header[136:148] = b"00000000000\x00"
    header[156:157] = typeflag
    header[157 : 157 + len(linkname)] = linkname
    header[257:265] = magic
    if realsize is not None:  # GNU sparse 'realsize' field
        header[483:495] = f"{realsize:011o}".encode() + b"\x00"
    header[148:156] = b" " * 8
    header[148:156] = f"{sum(header) + checksum_delta:06o}".encode() + b"\x00 "
    return bytes(header)


def _padded_block(data: bytes) -> bytes:
    return data + bytes(-len(data) % 512)


_END = bytes(1024)


def _pax(records: dict[str, str]) -> bytes:
    """pax extended-header data: '<len> key=value\\n' records."""
    out = b""
    for key, value in records.items():
        body = f" {key}={value}\n".encode()
        length = len(body) + 1
        while len(str(length)) + len(body) != length:
            length = len(str(length)) + len(body)
        out += f"{length}".encode() + body
    return out


def _invalid_archives() -> dict[str, tuple[str, str, bytes]]:
    """case -> (description, expectedCode, bytes). Hand-built; each one is an
    archive a safe reader must reject."""
    ok_member = _raw_header(b"plugin.yaml", 5) + _padded_block(b"hello")
    pax_global = _pax({"path": "../../etc/evil", "size": "480000000"})
    pax_extended = _pax({"path": "../../etc/evil"})
    longname = b"a/" * 200 + b"evil\x00"
    return {
        "pax-global-path-override": (
            "A pax global header ('g') that overrides the path and declared size of the "
            "next member, which looks like a plain regular file; a generic tar library "
            "folds it in silently.",
            "E_INVALID_PLUGIN",
            _raw_header(b"pax_global_header", len(pax_global), b"g")
            + _padded_block(pax_global)
            + ok_member
            + _END,
        ),
        "pax-extended-header": (
            "A pax extended header ('x') that rewrites the next member's path.",
            "E_INVALID_PLUGIN",
            _raw_header(b"PaxHeader/plugin.yaml", len(pax_extended), b"x")
            + _padded_block(pax_extended)
            + ok_member
            + _END,
        ),
        "gnu-longname": (
            "A GNU long-name entry ('L') carrying a path longer than ustar allows "
            "(ustar magic, so only the typeflag gives it away).",
            "E_INVALID_PLUGIN",
            _raw_header(b"././@LongLink", len(longname), b"L")
            + _padded_block(longname)
            + ok_member
            + _END,
        ),
        "gnu-sparse": (
            "A GNU sparse entry ('S') claiming a 480 MiB real size in a few bytes of archive.",
            "E_INVALID_PLUGIN",
            _raw_header(b"sparse.bin", 5, b"S", realsize=480 * 1024 * 1024)
            + _padded_block(b"hello")
            + _END,
        ),
        "bad-checksum": (
            "A well-formed regular file whose header checksum is off by one.",
            "E_INVALID_PLUGIN",
            _raw_header(b"plugin.yaml", 5, checksum_delta=1) + _padded_block(b"hello") + _END,
        ),
        "symlink": (
            "A symlink ('2') pointing at an absolute path.",
            "E_INVALID_PLUGIN",
            _raw_header(b"link", 0, b"2", linkname=b"/etc/passwd") + ok_member + _END,
        ),
        "truncated-data": (
            "A regular file declaring 5000 bytes but ending after 100, with no end marker.",
            "E_INVALID_PLUGIN",
            _raw_header(b"plugin.yaml", 5000) + b"x" * 100,
        ),
        "trailing-garbage-after-end": (
            "A valid archive followed, after the two zero blocks, by non-zero bytes.",
            "E_INVALID_PLUGIN",
            build_archive([TreeEntry("plugin.yaml", b"hello")]) + b"garbage" + bytes(505),
        ),
        "base256-size": (
            "A size field in GNU base-256 form (high bit set) instead of octal.",
            "E_INVALID_PLUGIN",
            _raw_header(b"plugin.yaml", b"\x80" + bytes(10) + b"\x05")
            + _padded_block(b"hello")
            + _END,
        ),
        "declared-size-over-limit": (
            "A regular file declaring 17 MiB (limit 16 MiB) with no bytes behind it: the "
            "declared size is checked before any slicing.",
            "E_ARCHIVE_TOO_LARGE",
            _raw_header(b"big.bin", 17 * 1024 * 1024) + _END,
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
    for case, (description, code, archive) in _invalid_archives().items():
        doc = {"description": description, "expectedCode": code}
        out[f"archive/invalid/{case}.json"] = (
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        ).encode()
        out[f"archive/invalid/{case}.tar"] = archive
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
