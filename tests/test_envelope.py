"""errors_from_jsonl: reading the error side channel (`$RW_ERRORS_FILE`) a task
wrote, and derive_envelope: the D64 result envelope."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from runwhen_capability.contracts import check
from runwhen_capability.envelope import MAX_ERRORS, derive_envelope, errors_from_jsonl


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


# -- derive_envelope ------------------------------------------------------------------

T0 = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(seconds=2, milliseconds=500)
ERR = {"code": "not_found", "message": "no such pod", "scope": "ns/a"}


def _derive(**kwargs):
    base = {"exit_code": 0, "outputs": {}, "errors": [], "started_at": T0, "finished_at": T1}
    return derive_envelope(**{**base, **kwargs})


# (case id, kwargs, expected status, expected error codes)
_BRANCHES = [
    ("ok", {}, "ok", []),
    ("ok-with-outputs", {"outputs": {"rows": [1, 2], "total": 2}}, "ok", []),
    ("partial", {"errors": [ERR]}, "partial", ["not_found"]),
    (
        "partial-many",
        {"errors": [ERR, {"code": "timeout", "message": "n2"}]},
        "partial",
        ["not_found", "timeout"],
    ),
    ("failed-with-errors", {"exit_code": 1, "errors": [ERR]}, "failed", ["not_found"]),
    (
        "failed-no-errors-uses-stderr",
        {"exit_code": 2, "stderr_tail": "Traceback\nRuntimeError: boom\n"},
        "failed",
        ["internal"],
    ),
    ("failed-no-errors-no-stderr", {"exit_code": 3}, "failed", ["internal"]),
    ("failed-signal", {"exit_code": -9}, "failed", ["internal"]),
    ("skipped", {"skipped_reason": "nothing to check"}, "skipped", []),
    (
        "skipped-drops-errors",
        {"skipped_reason": "nothing to check", "errors": [ERR]},
        "skipped",
        [],
    ),
    ("skipped-empty-reason", {"skipped_reason": ""}, "skipped", []),
    ("timed-out", {"exit_code": -9, "timed_out": True}, "failed", ["timeout"]),
    ("timed-out-exit-0", {"exit_code": 0, "timed_out": True}, "failed", ["timeout"]),
    (
        "timed-out-keeps-its-timeout-error",
        {
            "exit_code": -9,
            "timed_out": True,
            "errors": [{"code": "timeout", "message": "killed at the deadline"}],
        },
        "failed",
        ["timeout"],
    ),
    (
        "timed-out-with-other-errors",
        {"exit_code": -9, "timed_out": True, "errors": [ERR]},
        "failed",
        ["not_found", "timeout"],
    ),
    (
        "timed-out-beats-skip",
        {"timed_out": True, "skipped_reason": "x", "exit_code": -9},
        "failed",
        ["timeout"],
    ),
]


@pytest.mark.parametrize(
    ("kwargs", "status", "codes"),
    [pytest.param(k, s, c, id=i) for i, k, s, c in _BRANCHES],
)
def test_derive_envelope_follows_the_d64_table_and_validates(kwargs, status, codes):
    envelope = _derive(**kwargs)
    check("result-envelope", envelope)
    assert envelope["status"] == status
    assert [e["code"] for e in envelope["errors"]] == codes


def test_derive_envelope_fills_the_platform_fields_as_rfc_3339_with_z():
    envelope = _derive()
    assert envelope["startedAt"] == "2026-11-03T12:00:00.000Z"
    assert envelope["finishedAt"] == "2026-11-03T12:00:02.500Z"
    assert set(envelope) == {"status", "startedAt", "finishedAt", "outputs", "errors"}


def test_derive_envelope_converts_any_timezone_to_utc_and_reads_naive_as_utc():
    plus2 = timezone(timedelta(hours=2))
    envelope = _derive(
        started_at=datetime(2026, 11, 3, 14, 0, 0, tzinfo=plus2),
        finished_at=datetime(2026, 11, 3, 12, 0, 1),
    )
    assert envelope["startedAt"] == "2026-11-03T12:00:00.000Z"
    assert envelope["finishedAt"] == "2026-11-03T12:00:01.000Z"
    check("result-envelope", envelope)


def test_derive_envelope_carries_outputs_and_the_skip_reason():
    envelope = _derive(outputs={"total": 2}, skipped_reason="no pods")
    assert envelope["reason"] == "no pods"
    assert envelope["outputs"] == {"total": 2}
    assert "reason" not in _derive()
    assert "reason" not in _derive(errors=[ERR])


def test_an_empty_skip_reason_still_validates():
    envelope = _derive(skipped_reason="")
    assert envelope["status"] == "skipped"
    assert envelope["reason"]
    check("result-envelope", envelope)


def test_internal_error_message_is_the_last_non_empty_stderr_line():
    envelope = _derive(exit_code=1, stderr_tail="first\nlast line here  \n\n  \n")
    assert envelope["errors"] == [{"code": "internal", "message": "last line here"}]


def test_internal_error_message_is_capped_at_1000_chars():
    envelope = _derive(exit_code=1, stderr_tail="x" * 5000)
    assert len(envelope["errors"][0]["message"]) == 1000
    check("result-envelope", envelope)


@pytest.mark.parametrize("stderr_tail", ["", "\n\n", "   \n  "])
def test_internal_error_message_without_stderr_names_the_exit_code(stderr_tail):
    envelope = _derive(exit_code=7, stderr_tail=stderr_tail)
    assert envelope["errors"] == [{"code": "internal", "message": "process exited 7"}]


def test_derive_envelope_does_not_alias_its_inputs():
    errors = [dict(ERR)]
    outputs = {"rows": [1]}
    envelope = _derive(errors=errors, outputs=outputs)
    errors[0]["message"] = "changed"
    outputs["rows"].append(2)
    assert envelope["errors"][0]["message"] == "no such pod"
    assert envelope["outputs"] == {"rows": [1]}


def test_errors_from_jsonl_feeds_derive_envelope(tmp_path):
    path = _write(
        tmp_path,
        json.dumps({"code": "unreachable", "message": "db down"}),
        "garbage",
    )
    envelope = _derive(errors=errors_from_jsonl(path))
    check("result-envelope", envelope)
    assert envelope["status"] == "partial"
    assert [e["code"] for e in envelope["errors"]] == ["unreachable", "internal"]
