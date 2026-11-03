"""Canonical JSON and the permission/task hashes, plus the cross-language
vectors (contracts/vectors/*.json) that pin them for other implementations."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from runwhen_capability.contracts import (
    canonical_json,
    data_dir,
    normalize_permission,
    permission_hash,
    task_hash,
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


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (1.0, "1"),
        (-0.0, "0"),
        (0.0, "0"),
        (-3.0, "-3"),
        (0.5, "0.5"),
        (1.25, "1.25"),
        (0.1, "0.1"),
        (-2.5, "-2.5"),
        (1e-6, "0.000001"),
        (1.5e-5, "0.000015"),
        (1e20, "100000000000000000000"),
        (1.2345678901234568e20, "123456789012345680000"),
        (9007199254740993.0, "9007199254740992"),
    ],
)
def test_canonical_json_formats_floats_per_rfc_8785(value, text):
    assert canonical_json(value) == text.encode()
    assert canonical_json({"x": [value]}) == f'{{"x":[{text}]}}'.encode()


@pytest.mark.parametrize("bad", [1e-7, -1e-7, 1e21, -1e21, 1e300, 5e-324])
def test_canonical_json_rejects_floats_outside_the_accepted_range(bad):
    with pytest.raises(ValueError, match="not canonicalizable"):
        canonical_json({"x": bad})


def test_canonical_json_rejects_non_string_keys():
    with pytest.raises(TypeError):
        canonical_json({1: "a"})


def test_canonical_json_bool_is_not_an_int():
    assert canonical_json([True, 1, False, 0]) == b"[true,1,false,0]"


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
            else:
                actual = task_hash(given["task"], {f["path"]: _b64(f) for f in given["files"]})
            assert actual == expected, f"{filename}: {case['name']}"
            replayed += 1
    assert replayed >= 20


def test_vectors_are_current(tmp_path):
    _gen_vectors().main(["--out", str(tmp_path)])
    committed = REPO / "contracts" / "vectors"
    generated = sorted(
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()
    )
    assert generated == sorted(
        p.relative_to(committed).as_posix() for p in committed.rglob("*") if p.is_file()
    )
    for name in generated:
        assert (tmp_path / name).read_bytes() == (committed / name).read_bytes(), name
