"""The V13 result envelope (D64) and the error side channel that feeds it.

A task reports structured errors by appending one JSON line per error to the
file named by `$RW_ERRORS_FILE` (`ctx.error`, an uncaught `RwError`, bash
`rw_error`). `errors_from_jsonl` reads that file back, tolerating anything a
task could have written there; `derive_envelope` turns exit status, outputs
and those errors into the envelope the contract `result-envelope` describes
(CONTRACTS.md "Result envelope (D64)").
"""

from __future__ import annotations

import copy
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import validate as validate_contract
from .rwerrors import ERROR_CODES

# A task cannot flood the host: at most this many entries are read, from at
# most this many bytes. What is beyond either is reported as one `internal` note.
MAX_ERRORS = 100
MAX_ERRORS_FILE_BYTES = 1024 * 1024

# The `error` contract's string bounds; a longer string is clipped, never a reason
# to drop the error it belongs to.
_MESSAGE_MAX = 4096
_SCOPE_MAX = 1024
_HINT_MAX = 4096
_CLIP = {"message": _MESSAGE_MAX, "scope": _SCOPE_MAX, "hint": _HINT_MAX}


def _internal(message: str) -> dict[str, Any]:
    return {"code": "internal", "message": message[:_MESSAGE_MAX]}


def _read_capped(path: Path) -> tuple[bytes | None, bool, str | None]:
    """(bytes, truncated, problem): the file's first MAX_ERRORS_FILE_BYTES.
    Never follows a symlink -- the file is in the task's own scope, so the task
    could point it anywhere the host can read."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None, False, None
    except OSError as exc:
        return None, False, f"the errors file could not be read: {exc.strerror or exc}"
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_ERRORS_FILE_BYTES + 1)
    except OSError as exc:
        return None, False, f"the errors file could not be read: {exc.strerror or exc}"
    return data[:MAX_ERRORS_FILE_BYTES], len(data) > MAX_ERRORS_FILE_BYTES, None


def errors_from_jsonl(path: Path | str) -> list[dict[str, Any]]:
    """The structured errors in the side-channel file at `path`, in order; `[]`
    if there is no file. A line that is not JSON, not an object, or not a valid
    `error` (unknown code, empty message, extra key, ...) is dropped and replaced
    by one `internal` error naming the line and the problem, so a script's own
    mistake in reporting an error still fails the run visibly."""
    data, truncated, problem = _read_capped(Path(path))
    if problem is not None:
        return [_internal(problem)]
    if data is None:
        return []
    if truncated:
        # The last line is cut mid-way: not trusted.
        data = data[: data.rfind(b"\n") + 1]

    errors: list[dict[str, Any]] = []
    dropped = 0
    for number, raw in enumerate(data.split(b"\n"), start=1):
        if not raw.strip():
            continue
        if len(errors) >= MAX_ERRORS:
            dropped += 1
            continue
        errors.append(_parse_line(number, raw))
    if dropped:
        errors.append(_internal(f"{dropped} further error(s) were written and dropped"))
    if truncated:
        errors.append(
            _internal(f"the errors file passed {MAX_ERRORS_FILE_BYTES} bytes; the rest was ignored")
        )
    return errors


def _parse_line(number: int, raw: bytes) -> dict[str, Any]:
    try:
        entry = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return _internal(f"errors line {number} is not valid JSON ({exc}); the error was dropped")
    if not isinstance(entry, dict):
        return _internal(f"errors line {number} is not a JSON object; the error was dropped")
    code = entry.get("code")
    if not isinstance(code, str) or code not in ERROR_CODES:
        return _internal(
            f"errors line {number}: invalid error code {code!r} "
            f"(expected one of {', '.join(ERROR_CODES)}); the error was dropped"
        )
    for key, limit in _CLIP.items():
        if isinstance(entry.get(key), str) and len(entry[key]) > limit:
            entry[key] = entry[key][:limit]
    violations = validate_contract("error", entry)
    if violations:
        first = violations[0]
        where = first.path or "/"
        return _internal(
            f"errors line {number} is not a valid error: {where}: {first.message}; "
            "the error was dropped"
        )
    return entry


_STDERR_MESSAGE_MAX = 1000


def _rfc3339(moment: datetime) -> str:
    """UTC, millisecond precision, `Z`: a naive datetime is taken to be UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _last_line(text: str) -> str:
    return next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")


def derive_envelope(
    *,
    exit_code: int,
    outputs: dict[str, Any],
    errors: list[dict[str, Any]],
    started_at: datetime,
    finished_at: datetime,
    skipped_reason: str | None = None,
    timed_out: bool = False,
    stderr_tail: str = "",
) -> dict[str, Any]:
    """The D64 result envelope for one run (CONTRACTS.md "Result envelope"):

    - timed out                 -> `failed`, with a `timeout` error unless `errors`
                                   already has one (a timeout outranks a skip)
    - `skipped_reason` given    -> `skipped` with `reason`, and no errors
    - exit 0, no errors         -> `ok`
    - exit 0, errors            -> `partial`
    - non-zero exit             -> `failed`; with no errors, one `internal` error is
                                   added: the last non-empty line of `stderr_tail`
                                   (at most 1000 characters), or `process exited N`

    `errors` are `error`-contract dicts (see errors_from_jsonl); the platform-filled
    fields (`dryRun`, `provenance`, `logTail`, ...) are not set here."""
    errors = copy.deepcopy(errors)
    reason: str | None = None
    if timed_out:
        if not any(e.get("code") == "timeout" for e in errors):
            errors.append({"code": "timeout", "message": "the task was killed at its deadline"})
        status = "failed"
    elif skipped_reason is not None:
        status = "skipped"
        reason = skipped_reason or "skipped"
        errors = []
    elif exit_code == 0:
        status = "partial" if errors else "ok"
    else:
        status = "failed"
        if not errors:
            message = _last_line(stderr_tail)[:_STDERR_MESSAGE_MAX] or f"process exited {exit_code}"
            errors.append({"code": "internal", "message": message})
    envelope: dict[str, Any] = {
        "status": status,
        "startedAt": _rfc3339(started_at),
        "finishedAt": _rfc3339(finished_at),
        "outputs": copy.deepcopy(outputs),
        "errors": errors,
    }
    if reason is not None:
        envelope["reason"] = reason
    return envelope
