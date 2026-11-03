"""Canonical JSON and the permission/tree/task hashes, plus the cross-language
vectors (contracts/vectors/*.json) that pin them for other implementations."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from runwhen_capability.contracts import (
    TreeEntry,
    canonical_json,
    data_dir,
    normalize_permission,
    permission_hash,
    task_hash,
    tree_hash,
)

REPO = Path(__file__).resolve().parents[2]
GEN_SCRIPT = REPO / "scripts" / "gen_vectors.py"


def _gen_vectors():
    spec = importlib.util.spec_from_file_location("gen_vectors", GEN_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- canonical_json --------------------------------------------------------


def test_canonical_json_sorts_keys_and_is_compact():
    assert canonical_json({"b": 1, "a": [True, None, "x"]}) == b'{"a":[true,null,"x"],"b":1}'


def test_canonical_json_keeps_non_ascii_as_utf8():
    assert canonical_json({"k": "naïve"}) == '{"k":"naïve"}'.encode()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_non_finite_floats(bad):
    with pytest.raises(ValueError):
        canonical_json({"x": bad})


def test_canonical_json_rejects_unserialisable_types():
    with pytest.raises(TypeError):
        canonical_json({"x": object()})


# --- permission hash -------------------------------------------------------


def test_permission_hash_ignores_order_and_empty_keys():
    a = {
        "apiGroups": ["apps"],
        "resources": ["deployments", "replicasets"],
        "verbs": ["list", "get"],
    }
    b = {
        "verbs": ["get", "list"],
        "resources": ["replicasets", "deployments"],
        "apiGroups": ["apps"],
        "nonResourceURLs": [],
    }
    assert permission_hash(a) == permission_hash(b)
    assert permission_hash(a).startswith("sha256:")


def test_permission_hash_dedupes_list_items():
    a = {"verbs": ["get", "get", "list"]}
    assert permission_hash(a) == permission_hash({"verbs": ["list", "get"]})


def test_empty_string_api_group_is_not_an_empty_list():
    core = {"apiGroups": [""], "resources": ["events"], "verbs": ["list"]}
    none = {"apiGroups": [], "resources": ["events"], "verbs": ["list"]}
    assert permission_hash(core) != permission_hash(none)
    assert normalize_permission(core)["apiGroups"] == [""]
    assert "apiGroups" not in normalize_permission(none)


def test_normalize_permission_drops_none_and_recurses():
    rule = {"a": None, "b": [], "c": {"d": [], "e": [2, 1, 2]}, "f": [{"z": [], "y": 1}]}
    assert normalize_permission(rule) == {"c": {"e": [1, 2]}, "f": [{"y": 1}]}


def test_normalize_permission_does_not_mutate_input():
    rule = {"verbs": ["list", "get"], "x": []}
    normalize_permission(rule)
    assert rule == {"verbs": ["list", "get"], "x": []}


# --- tree hash -------------------------------------------------------------


def _entries():
    return [
        TreeEntry("SKILL.md", b"# skill\n"),
        TreeEntry("run.sh", b"#!/bin/sh\n", executable=True),
        TreeEntry("lib/util.py", b""),
    ]


def test_tree_hash_ignores_input_order():
    assert tree_hash(_entries()) == tree_hash(reversed(_entries()))


def test_tree_hash_changes_with_exec_bit():
    flipped = [TreeEntry("run.sh", b"#!/bin/sh\n", executable=False)]
    original = [TreeEntry("run.sh", b"#!/bin/sh\n", executable=True)]
    assert tree_hash(flipped) != tree_hash(original)


def test_tree_hash_line_format():
    data = b"hello"
    digest = hashlib.sha256(data).hexdigest()
    expected = hashlib.sha256(f"{digest} 755 a/b.sh\n".encode()).hexdigest()
    assert tree_hash([TreeEntry("a/b.sh", data, executable=True)]) == f"sha256:{expected}"


def test_tree_hash_sorts_by_utf8_bytes():
    # "Z" (0x5a) sorts before "a" (0x61); "é" (0xc3 0xa9) after both.
    entries = [TreeEntry(p, b"") for p in ("é", "a", "Z")]
    assert tree_hash(entries) == tree_hash([entries[2], entries[1], entries[0]])


@pytest.mark.parametrize(
    "path",
    ["", "/abs", "../up", "a/../b", "./a", "a/./b", "a//b", "a/", "a\\b", "."],
)
def test_tree_hash_rejects_bad_paths(path):
    with pytest.raises(ValueError):
        tree_hash([TreeEntry(path, b"")])


def test_tree_hash_rejects_duplicate_paths():
    with pytest.raises(ValueError):
        tree_hash([TreeEntry("a", b"1"), TreeEntry("a", b"2")])


def test_empty_tree_has_a_hash():
    assert tree_hash([]) == "sha256:" + hashlib.sha256(b"").hexdigest()


# --- task hash -------------------------------------------------------------


def test_task_hash_ignores_hash_key_and_dict_order():
    files = {"tasks/a.sh": b"echo a\n", "lib/x.py": b""}
    one = task_hash({"name": "a", "hash": "sha256:old"}, files)
    two = task_hash({"hash": "sha256:other", "name": "a"}, dict(reversed(list(files.items()))))
    assert one == two
    assert one.startswith("sha256:")


def test_task_hash_changes_with_task_or_file_content():
    base = task_hash({"name": "a"}, {"f": b"1"})
    assert task_hash({"name": "b"}, {"f": b"1"}) != base
    assert task_hash({"name": "a"}, {"f": b"2"}) != base


def test_task_hash_does_not_mutate_input():
    task = {"name": "a", "hash": "x"}
    task_hash(task, {})
    assert task == {"name": "a", "hash": "x"}


@pytest.mark.parametrize("path", ["/abs", "../up", "./a", "a//b", "a/", "a\\b"])
def test_task_hash_rejects_bad_paths(path):
    with pytest.raises(ValueError):
        task_hash({"name": "a"}, {path: b""})


# --- vectors ---------------------------------------------------------------


def _b64(entry: dict) -> bytes:
    return base64.b64decode(entry["dataBase64"])


def _load_vectors() -> dict[str, dict]:
    vectors = data_dir() / "vectors"
    out = {
        f.name: json.loads(f.read_text(encoding="utf-8"))
        for f in vectors.iterdir()
        if f.name.endswith(".json")
    }
    return out


def test_vector_files_present():
    assert set(_load_vectors()) == {
        "canonical-json.json",
        "permission-hash.json",
        "tree-hash.json",
        "task-hash.json",
    }


def test_vectors_replay():
    replayed = 0
    for filename, doc in _load_vectors().items():
        assert len(doc["cases"]) >= 5, filename
        for case in doc["cases"]:
            given, expected = case["input"], case["expected"]
            if filename == "canonical-json.json":
                actual = canonical_json(given).decode("utf-8")
            elif filename == "permission-hash.json":
                actual = permission_hash(given)
            elif filename == "tree-hash.json":
                actual = tree_hash(TreeEntry(e["path"], _b64(e), e["executable"]) for e in given)
            else:
                actual = task_hash(given["task"], {f["path"]: _b64(f) for f in given["files"]})
            assert actual == expected, f"{filename}: {case['name']}"
            replayed += 1
    assert replayed >= 20


def test_vectors_are_current(tmp_path):
    _gen_vectors().main(["--out", str(tmp_path)])
    committed = REPO / "contracts" / "vectors"
    generated = sorted(p.name for p in tmp_path.iterdir())
    assert generated == sorted(p.name for p in committed.iterdir())
    for name in generated:
        assert (tmp_path / name).read_bytes() == (committed / name).read_bytes(), name
