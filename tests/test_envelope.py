"""errors_from_jsonl: reading the error side channel (`$RW_ERRORS_FILE`) a task
wrote, and derive_envelope: the D64 result envelope."""

from __future__ import annotations

import json

import pytest

from runwhen_capability.contracts import check
from runwhen_capability.envelope import MAX_ERRORS, errors_from_jsonl


def _write(tmp_path, *lines: str):
    path = tmp_path / "errors.jsonl"
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def _validate_all(errors):
    for error in errors:
        check("error", error)


def test_errors_from_jsonl_reads_valid_entries_in_order(tmp_path):
    full = {
        "code": "not_found",
        "message": "no pod",
        "scope": "ns/a",
        "hint": "h",
        "details": {"k": 1},
    }
    path = _write(
        tmp_path,
        json.dumps({"code": "timeout", "message": "slow"}),
        json.dumps(full),
    )
    assert errors_from_jsonl(path) == [{"code": "timeout", "message": "slow"}, full]


def test_errors_from_jsonl_of_a_missing_or_empty_file_is_empty(tmp_path):
    assert errors_from_jsonl(tmp_path / "nope.jsonl") == []
    assert errors_from_jsonl(_write(tmp_path)) == []


def test_errors_from_jsonl_skips_blank_lines(tmp_path):
    path = _write(tmp_path, "", json.dumps({"code": "internal", "message": "m"}), "   ")
    assert errors_from_jsonl(path) == [{"code": "internal", "message": "m"}]


@pytest.mark.parametrize(
    ("line", "needle"),
    [
        ("not json at all", "not valid JSON"),
        ('{"code": "timeout", "message": ', "not valid JSON"),
        ("[1, 2]", "not a JSON object"),
        ('"just a string"', "not a JSON object"),
        (json.dumps({"code": "E_BOOM", "message": "m"}), "invalid error code 'E_BOOM'"),
        (json.dumps({"code": 7, "message": "m"}), "invalid error code 7"),
        (json.dumps({"message": "m"}), "invalid error code None"),
        (json.dumps({"code": "timeout"}), "message"),
        (json.dumps({"code": "timeout", "message": ""}), "message"),
        (json.dumps({"code": "timeout", "message": "m", "extra": 1}), "extra"),
        (json.dumps({"code": "timeout", "message": "m", "details": [1]}), "details"),
    ],
)
def test_a_bad_line_becomes_one_internal_error_naming_the_problem(tmp_path, line, needle):
    path = _write(tmp_path, json.dumps({"code": "timeout", "message": "kept"}), line)
    errors = errors_from_jsonl(path)
    assert errors[0] == {"code": "timeout", "message": "kept"}
    assert len(errors) == 2
    assert errors[1]["code"] == "internal"
    assert "line 2" in errors[1]["message"]
    assert needle in errors[1]["message"]
    _validate_all(errors)


def test_an_over_long_string_is_clipped_not_dropped(tmp_path):
    path = _write(
        tmp_path, json.dumps({"code": "timeout", "message": "x" * 5000, "hint": "h" * 9000})
    )
    [error] = errors_from_jsonl(path)
    assert error["code"] == "timeout"
    assert len(error["message"]) == 4096
    assert len(error["hint"]) == 4096
    _validate_all([error])


def test_a_flood_of_errors_is_capped_with_one_internal_note(tmp_path):
    lines = [json.dumps({"code": "timeout", "message": f"e{i}"}) for i in range(MAX_ERRORS + 5)]
    errors = errors_from_jsonl(_write(tmp_path, *lines))
    assert len(errors) == MAX_ERRORS + 1
    assert errors[MAX_ERRORS - 1]["message"] == f"e{MAX_ERRORS - 1}"
    assert errors[-1]["code"] == "internal"
    assert "5" in errors[-1]["message"]
    _validate_all(errors)


def test_invalid_utf8_is_one_problem_not_a_crash(tmp_path):
    path = tmp_path / "errors.jsonl"
    path.write_bytes(
        b'{"code": "timeout", "message": "ok"}\n{"code": "timeout", "message": "\xff\xfe"}\n'
    )
    errors = errors_from_jsonl(path)
    assert errors[0] == {"code": "timeout", "message": "ok"}
    _validate_all(errors)


def test_a_symlinked_errors_file_is_not_followed(tmp_path):
    target = tmp_path / "secret"
    target.write_text(json.dumps({"code": "timeout", "message": "leaked"}) + "\n")
    link = tmp_path / "errors.jsonl"
    link.symlink_to(target)
    errors = errors_from_jsonl(link)
    assert all(e["message"] != "leaked" for e in errors)
    _validate_all(errors)
