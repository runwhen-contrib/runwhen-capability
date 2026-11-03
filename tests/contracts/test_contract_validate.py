"""The offline contract validator (runwhen_capability.contracts): schemas are
read from the packaged `_data/` copy, cross-file `$ref`s resolve through an
in-memory registry keyed on `$id`, and nothing here may touch the network.

Every test points `data_dir` at a tmp directory of tiny schemas -- never at the
real contracts/ files, which other work is free to change.
"""

from __future__ import annotations

import json
import socket

import pytest

from runwhen_capability.contracts import (
    ContractError,
    ContractViolation,
    _validate,
    check,
    schema,
    validate,
)

A_ID = "https://schemas.example.test/a/v1.json"
B_ID = "https://schemas.example.test/b/v1.json"

SCHEMA_B = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": B_ID,
    "type": "object",
    "properties": {"n": {"type": "integer", "minimum": 1}},
    "required": ["n"],
    "additionalProperties": False,
}

SCHEMA_A = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": A_ID,
    "type": "object",
    "properties": {
        "x": {"$ref": B_ID},
        "when": {"type": "string", "format": "date"},
    },
    "required": ["x"],
    "additionalProperties": False,
}


def _write(root, name: str, doc: dict) -> None:
    schemas = root / "schemas"
    schemas.mkdir(parents=True, exist_ok=True)
    (schemas / f"{name}.schema.json").write_text(json.dumps(doc))


@pytest.fixture
def data(tmp_path, monkeypatch):
    _write(tmp_path, "b", SCHEMA_B)
    _write(tmp_path, "a", SCHEMA_A)
    (tmp_path / "schemas" / ".gitkeep").write_text("")
    (tmp_path / "schemas" / "notes.txt").write_text("not a schema")
    monkeypatch.setattr(_validate, "data_dir", lambda: tmp_path)
    _validate._reset_cache()
    yield tmp_path
    _validate._reset_cache()


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_schema_names_are_sorted_stems(data):
    from runwhen_capability import contracts

    assert contracts.SCHEMA_NAMES == ("a", "b")


def test_schema_returns_the_document_and_rejects_unknown_names(data):
    assert schema("b")["$id"] == B_ID
    with pytest.raises(KeyError):
        schema("nope")


def test_schema_returns_a_copy(data):
    schema("b")["type"] = "string"
    assert schema("b")["type"] == "object"


def test_valid_document_has_no_violations(data, no_network):
    assert validate("a", {"x": {"n": 3}}) == []


def test_violation_carries_json_pointer_and_message(data, no_network):
    violations = validate("a", {"x": {"n": 0}})

    assert len(violations) == 1
    v = violations[0]
    assert isinstance(v, ContractViolation)
    assert v.path == "/x/n"
    assert "minimum" in v.schema_path
    assert "0" in v.message


def test_root_violation_has_empty_path(data, no_network):
    (v,) = validate("a", [])
    assert v.path == ""


def test_pointer_escapes_tilde_and_slash(data, no_network, tmp_path):
    _write(
        tmp_path,
        "m",
        {
            "$id": "https://schemas.example.test/m/v1.json",
            "type": "object",
            "properties": {"a/b~c": {"type": "integer"}},
        },
    )
    _validate._reset_cache()

    (v,) = validate("m", {"a/b~c": "x"})
    assert v.path == "/a~1b~0c"


def test_array_index_in_path(data, no_network, tmp_path):
    _write(tmp_path, "l", {"type": "array", "items": {"type": "integer"}})
    _validate._reset_cache()

    (v,) = validate("l", [1, "x"])
    assert v.path == "/1"


def test_all_errors_are_reported_sorted_by_path(data, no_network):
    violations = validate("a", {"zzz": 1, "x": {"n": 0}, "when": 5})

    assert [v.path for v in violations] == sorted(v.path for v in violations)
    assert {v.path for v in violations} == {"", "/x/n", "/when"}


def test_format_is_checked(data, no_network):
    (v,) = validate("a", {"x": {"n": 1}, "when": "not a date"})
    assert v.path == "/when"


def test_check_raises_contract_error_with_violations(data, no_network):
    with pytest.raises(ContractError) as exc:
        check("a", {"x": {"n": 0}})

    assert isinstance(exc.value, ValueError)
    assert [v.path for v in exc.value.violations] == ["/x/n"]
    assert "/x/n" in str(exc.value)


def test_check_returns_none_when_valid(data, no_network):
    assert check("a", {"x": {"n": 1}}) is None


def test_unknown_schema_name_raises_key_error(data):
    with pytest.raises(KeyError):
        validate("nope", {})


def test_unknown_ref_raises_instead_of_fetching(data, no_network, tmp_path):
    _write(
        tmp_path,
        "c",
        {
            "$id": "https://schemas.example.test/c/v1.json",
            "type": "object",
            "properties": {"y": {"$ref": "https://schemas.example.test/missing/v1.json"}},
        },
    )
    _validate._reset_cache()

    with pytest.raises(Exception) as exc:
        validate("c", {"y": 1})

    assert "missing/v1.json" in str(exc.value)
    assert not isinstance(exc.value, AssertionError)


def test_data_dir_is_the_packaged_copy():
    root = _validate.data_dir()
    assert root.name == "_data"
