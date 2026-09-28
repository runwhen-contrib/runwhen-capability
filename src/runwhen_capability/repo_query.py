"""The engine behind a repository `query` task: a batch of grep/read/ls/
defs/refs ops against one checked-out tree, so an agent answers several
questions in one round trip instead of one task request per read.

`run_query` is pure -- a tree path and the ops in, a QueryResult out -- and
built entirely on repo_fs's building blocks, so every path still goes
through pathsafe.safe_path and every cap repo_fs already enforces still
applies per op.

Failure is per op wherever the fault is the op's: a malformed op is
INVALID_OP, and a repo_fs input error (escape, not found, binary, bad
regex, ...) carries repo_fs.error_code's code. Neither fails the others.
Two things do fail the whole task: a malformed `ops` input itself, and
TreeNotMaterializedError -- an evicted tree is not an op's fault, and
host.py must see it to flag the envelope not_materialized, which is what
makes the platform re-materialise and retry once.

Response budget: the whole output is held under QUERY_BUDGET, measured as
serialised JSON rather than content bytes, so per-op, per-match and
per-range structure and string escaping all count. Room for a
RESPONSE_BUDGET entry is reserved up front for every op, so the ops past
the cap can always still be answered with one. The op that straddles the
cap keeps the leading matches/entries/whole lines that fit (and says
`truncated`); every op after it is RESPONSE_BUDGET -- unless the very
first op is what didn't fit, in which case nothing earlier consumed any
of the budget, so that one op gets its own honest RESPONSE_BUDGET message
instead and later ops still run.

Time budget: QUERY_DEADLINE_SECONDS bounds the whole op loop the same way
QUERY_BUDGET bounds its bytes. Checked before each op starts; once it has
passed, that op and every op after it become DEADLINE instead of running,
and `truncated` is set -- the same exhausted-flag mechanism the byte
budget already uses, just a different reason and message.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import (
    GrepResult,
    LsResult,
    QueryError,
    QueryOpResult,
    QueryResult,
    ReadRange,
    ReadRangesResult,
)
from .repo_fs import (
    HARD_GREP_MAX_MATCHES,
    READ_BUDGET,
    check_tree_materialized,
    error_code,
    find_around,
    grep_tree,
    ls_tree,
    read_ranges,
)
from .symbols import definition_pattern, is_definition_line, reference_pattern

# The same 256 KiB response cap, less the same 1 KiB of headroom, that
# read already works to (repo_fs.READ_BUDGET) -- here the headroom is for
# the result envelope serve.py wraps this task's output in.
QUERY_BUDGET = READ_BUDGET

# Limits re-checked here so every caller gets the same contract rather than
# whatever an unbounded op would do.
MAX_QUERY_OPS = 60
MAX_GREP_CONTEXT = 20
MAX_AROUND_CONTEXT = 200
DEFAULT_AROUND_CONTEXT = 20
AROUND_MAX_WINDOWS = 5
DEFS_CONTEXT = 2

# An error message echoes caller input (a path, a pattern); keep one
# pathological input from eating the budget of every op after it.
MAX_ERROR_MESSAGE_LEN = 500

INVALID_OP = "INVALID_OP"
RESPONSE_BUDGET = "RESPONSE_BUDGET"
_RESPONSE_BUDGET_MESSAGE = (
    "response budget spent by earlier ops in this query; send this op again in a new query"
)

DEADLINE = "DEADLINE"
_DEADLINE_MESSAGE = "query time budget spent; send the remaining ops in a new query"

# A synchronous call waits on the order of 30-50s, and serve.py runs one
# request per pod at a time, so two concurrent `query` calls (say, head and
# base of one change) may queue behind each other on the same pod. 20s here
# means both, run back to back, still finish inside a 50s per-call deadline
# -- and comfortably under a capability's own requestTimeoutSeconds, which bounds a whole
# request rather than one op loop. The deadline is only checked between
# ops (see the module docstring above), so one full-tree op already
# running can still overrun it.
# A plain module attribute, read fresh on every call (not a bound default
# parameter), so a test can override it directly.
QUERY_DEADLINE_SECONDS = 20.0

OpResult = GrepResult | ReadRangesResult | LsResult


def _now() -> float:
    return time.monotonic()


class _InvalidOp(ValueError):
    """An op that is missing a required field or has a malformed one."""


# --- op validation -----------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _field(op: dict, key: str, check: Callable[[Any], bool], expected: str, required=False):
    """`op[key]`, or None when absent -- a null reads as absent, since a
    caller serialising a model may send every optional field."""
    value = op.get(key)
    if value is None:
        if required:
            raise _InvalidOp(f"missing required field {key!r}")
        return None
    if not check(value):
        raise _InvalidOp(f"{key!r} must be {expected}")
    return value


def _int_field(op: dict, key: str, low: int, high: int | None = None) -> int | None:
    def check(v):
        return _is_int(v) and v >= low and (high is None or v <= high)

    expected = f"an integer >= {low}" if high is None else f"an integer from {low} to {high}"
    return _field(op, key, check, expected)


def _globs(op: dict) -> list[str] | None:
    def check(v):
        return isinstance(v, list) and all(isinstance(g, str) for g in v)

    return _field(op, "globs", check, "an array of strings")


def _path_field(op: dict, required: bool = False) -> str | None:
    """`op['path']`, rejecting an embedded NUL byte before it ever reaches
    _confined/safe_path: os.path/pathlib raise a bare ValueError on one
    ("embedded null character in path"), which is not one of
    repo_fs.error_code's mapped exceptions and would otherwise fail the
    whole task instead of just this op."""
    path = _field(op, "path", lambda v: isinstance(v, str), "a string", required=required)
    if path is not None and "\x00" in path:
        raise _InvalidOp("'path' must not contain a NUL byte")
    return path


def _symbol(op: dict) -> str:
    return _field(
        op, "symbol", lambda v: isinstance(v, str) and v != "", "a non-empty string", True
    )


def _ranges(op: dict) -> list[tuple[int, int]]:
    def check(v):
        return (
            isinstance(v, list)
            and len(v) > 0
            and all(
                isinstance(r, list)
                and len(r) == 2
                and all(_is_int(n) for n in r)
                and 1 <= r[0] <= r[1]
                for r in v
            )
        )

    ranges = _field(op, "ranges", check, "a non-empty array of [start, end] line pairs")
    return [(start, end) for start, end in ranges]


# --- ops ---------------------------------------------------------------------


def _grep(tree: Path, op: dict) -> GrepResult:
    pattern = _field(op, "pattern", lambda v: isinstance(v, str), "a string", required=True)
    globs = _globs(op)
    context = _int_field(op, "context", 0, MAX_GREP_CONTEXT) or 0
    ignore_case = _field(op, "ignoreCase", lambda v: isinstance(v, bool), "a boolean") or False
    max_matches = _int_field(op, "maxMatches", 1, HARD_GREP_MAX_MATCHES)
    return grep_tree(
        tree,
        pattern,
        globs=globs,
        max_matches=max_matches,
        ignore_case=ignore_case,
        context=context,
    )


def _read(tree: Path, op: dict) -> ReadRangesResult:
    path = _path_field(op, required=True)
    if (op.get("ranges") is None) == (op.get("around") is None):
        raise _InvalidOp("read takes exactly one of 'ranges' or 'around'")
    if op.get("ranges") is not None:
        return read_ranges(tree, path, _ranges(op))

    around = _field(op, "around", lambda v: isinstance(v, str), "a string")
    context = _int_field(op, "context", 0, MAX_AROUND_CONTEXT)
    context = DEFAULT_AROUND_CONTEXT if context is None else context
    windows = find_around(tree, path, around, context, max_windows=AROUND_MAX_WINDOWS)
    return read_ranges(tree, path, windows)


def _ls(tree: Path, op: dict) -> LsResult:
    path = _path_field(op)
    depth = _int_field(op, "depth", 1)
    return ls_tree(tree, path=path, depth=depth)


def _defs(tree: Path, op: dict) -> GrepResult:
    symbol = _symbol(op)
    globs = _globs(op)
    return grep_tree(tree, definition_pattern(symbol), globs=globs, context=DEFS_CONTEXT)


def _refs(tree: Path, op: dict) -> GrepResult:
    symbol = _symbol(op)
    globs = _globs(op)
    # `exclude` sees the full line, not the 400-byte match text, so a long
    # definition line can't slip into refs past the snippet cut.
    return grep_tree(
        tree,
        reference_pattern(symbol),
        globs=globs,
        exclude=lambda line: is_definition_line(line, symbol),
    )


_OPS: dict[str, Callable[[Path, dict], OpResult]] = {
    "grep": _grep,
    "read": _read,
    "ls": _ls,
    "defs": _defs,
    "refs": _refs,
}


def _op_name(op: Any) -> str | None:
    name = op.get("op") if isinstance(op, dict) else None
    return name if isinstance(name, str) and name in _OPS else None


def _error(index: int, op: str | None, code: str, message: str) -> QueryOpResult:
    return QueryOpResult(
        index=index,
        op=op,
        error=QueryError(code=code, message=message[:MAX_ERROR_MESSAGE_LEN]),
    )


def _run_op(tree: Path, index: int, op: Any) -> QueryOpResult:
    if not isinstance(op, dict):
        return _error(index, None, INVALID_OP, "op must be a JSON object")
    name = _op_name(op)
    if name is None:
        return _error(
            index, None, INVALID_OP, f"unknown op {op.get('op')!r}; expected one of {list(_OPS)}"
        )
    try:
        return QueryOpResult(index=index, op=name, result=_OPS[name](tree, op))
    except _InvalidOp as exc:
        return _error(index, name, INVALID_OP, str(exc))
    except Exception as exc:
        code = error_code(exc)
        if code is None:
            raise  # TreeNotMaterializedError, or a bug: fails the whole task
        return _error(index, name, code, str(exc))


# --- the response budget --------------------------------------------------------


def _size(value: Any) -> int:
    """Wire bytes of `value`: json.dumps' defaults (", " and ": "
    separators, non-ASCII escaped) are exactly what serve.py's
    requests.post(json=...) sends, and with everything ASCII-escaped the
    string length is the byte count."""
    return len(json.dumps(value))


def _entry_size(entry: QueryOpResult) -> int:
    return _size(entry.model_dump(mode="json"))


def _with_result(entry: QueryOpResult, result: OpResult) -> QueryOpResult:
    return entry.model_copy(update={"result": result})


def _trim_items(entry: QueryOpResult, field: str, available: int) -> OpResult | None:
    """The longest prefix of a grep result's `matches` or an ls result's
    `entries` that fits in `available`, or None if not even one does."""
    result = entry.result
    shell = _with_result(entry, result.model_copy(update={field: [], "truncated": True}))
    used = _entry_size(shell)
    kept = []
    for item in getattr(result, field):
        add = _size(item.model_dump(mode="json")) + (2 if kept else 0)  # 2: ", "
        if used + add > available:
            break
        kept.append(item)
        used += add
    if not kept:
        return None
    return result.model_copy(update={field: kept, "truncated": True})


def _clip_content(content: str, room: int) -> str | None:
    """The longest line-aligned prefix of `content` whose JSON string takes
    at most `room` bytes -- or, when not even the first line fits, that
    line clipped, as read_lines clips a single over-budget line."""
    low, high = 0, len(content)
    while low < high:
        mid = (low + high + 1) // 2
        if _size(content[:mid]) <= room:
            low = mid
        else:
            high = mid - 1
    if low == 0:
        return None
    prefix = content[:low]
    if low < len(content) and "\n" in prefix:
        prefix = prefix[: prefix.rfind("\n")]
    return prefix


def _trim_read(entry: QueryOpResult, available: int) -> ReadRangesResult | None:
    """The whole ranges that fit, plus the line-aligned head of the first
    range that doesn't."""
    result = entry.result
    shell = _with_result(entry, result.model_copy(update={"ranges": [], "truncated": True}))
    used = _entry_size(shell)
    kept: list[ReadRange] = []
    for rng in result.ranges:
        separator = 2 if kept else 0
        add = _size(rng.model_dump(mode="json")) + separator
        if used + add <= available:
            kept.append(rng)
            used += add
            continue
        # The shell keeps rng.end, which has at least as many digits as the
        # clipped end -- an upper bound. +2: its empty content's quotes,
        # which _size(content) counts again.
        empty = ReadRange(start=rng.start, end=rng.end, content="")
        room = available - used - separator - _size(empty.model_dump(mode="json")) + 2
        clipped = _clip_content(rng.content, room)
        if clipped is not None:
            end = rng.start + clipped.count("\n")
            kept.append(ReadRange(start=rng.start, end=end, content=clipped))
        break
    if not kept:
        return None
    return result.model_copy(update={"ranges": kept, "truncated": True})


def _fit(entry: QueryOpResult, available: int) -> tuple[QueryOpResult | None, int, bool]:
    """(entry or its trimmed form, its size, whether it was trimmed) --
    (None, 0, False) when nothing useful fits in `available`."""
    size = _entry_size(entry)
    if size <= available:
        return entry, size, False

    if isinstance(entry.result, ReadRangesResult):
        trimmed = _trim_read(entry, available)
    elif isinstance(entry.result, GrepResult):
        trimmed = _trim_items(entry, "matches", available)
    elif isinstance(entry.result, LsResult):
        trimmed = _trim_items(entry, "entries", available)
    else:
        trimmed = None  # an error entry too large to fit
    if trimmed is None:
        return None, 0, False

    fitted = _with_result(entry, trimmed)
    size = _entry_size(fitted)
    if size > available:  # never exceed the cap on an arithmetic slip
        return None, 0, False
    return fitted, size, True


# --- entry point -------------------------------------------------------------


def run_query(worktree: Path, ops: list) -> QueryResult:
    """Runs `ops` against `worktree` in order (see the module docstring).
    An op's `rev` is ignored: the caller splits ops by rev and sends each
    rev's ops to the tree checked out at that sha.

    Three ways an op's own result can end up replaced and the top-level
    `truncated` set, rather than raising and failing the whole task:

    - the byte budget (QUERY_BUDGET) crosses inside one op's result: that
      op keeps the leading matches/entries/whole lines that fit (_fit's
      trim), and every op after it is RESPONSE_BUDGET without running;
    - that op's result alone exceeds QUERY_BUDGET before anything earlier
      has spent any of it (only possible for the very first op run): it
      gets its own honest RESPONSE_BUDGET error instead of a trim, and the
      batch is not otherwise exhausted -- later ops still run;
    - the time budget (QUERY_DEADLINE_SECONDS) has passed: a batch of up
      to MAX_QUERY_OPS full-tree ops can, in the worst case, run long
      enough to blow the capability's requestTimeoutSeconds; this deadline
      ends the query cleanly first, telling the caller to send the rest as
      a new query, instead of the runner timing out the whole request."""
    if not isinstance(ops, list):
        raise ValueError(f"ops must be a JSON array, got {type(ops).__name__}")
    if len(ops) > MAX_QUERY_OPS:
        raise ValueError(f"too many ops: {len(ops)} (at most {MAX_QUERY_OPS})")

    tree = Path(worktree)
    # Up front, not left to the first op that touches disk: a batch of
    # malformed ops against an evicted tree must still read as a miss.
    check_tree_materialized(tree)

    budget_errors = [
        _error(index, _op_name(op), RESPONSE_BUDGET, _RESPONSE_BUDGET_MESSAGE)
        for index, op in enumerate(ops)
    ]
    # DEADLINE's message is shorter than RESPONSE_BUDGET's, so each entry
    # here is never bigger than its budget_errors counterpart -- `held`
    # (sized off budget_errors) is a safe reservation for either fallback.
    deadline_errors = [
        _error(index, _op_name(op), DEADLINE, _DEADLINE_MESSAGE) for index, op in enumerate(ops)
    ]
    # Each op's held-back room: its RESPONSE_BUDGET entry plus the ", "
    # before it. Released as that op is answered.
    held = [_entry_size(e) + (2 if index else 0) for index, e in enumerate(budget_errors)]
    reserved = sum(held)
    used = _size(QueryResult().model_dump(mode="json"))  # {"results": [], "truncated": false}
    baseline_used = used

    deadline = _now() + QUERY_DEADLINE_SECONDS
    results: list[QueryOpResult] = []
    exhausted = False
    truncated = False
    timed_out = False
    for index, op in enumerate(ops):
        reserved -= held[index]
        separator = 2 if index else 0
        entry: QueryOpResult | None = None
        size = 0
        if not exhausted and _now() > deadline:
            exhausted = True
            truncated = True
            timed_out = True
        if not exhausted:
            available = QUERY_BUDGET - used - reserved - separator
            fitted, fit_size, trimmed = _fit(_run_op(tree, index, op), available)
            if fitted is None and used == baseline_used:
                # Nothing earlier has used any of the budget beyond its
                # reservation (this is the very first op run) -- so the op
                # itself, not starvation by an earlier one, is what doesn't
                # fit. Its own honest error, and later ops still get their
                # turn rather than being pre-emptively marked exhausted.
                #
                # held[index] (sized off _RESPONSE_BUDGET_MESSAGE) is this
                # op's per-op reserved room; this message must not run
                # longer than that one -- and if it somehow still doesn't
                # fit, fall back to the reserved, guaranteed-to-fit generic
                # one rather than risk pushing the whole result over
                # QUERY_BUDGET.
                own_too_big = _error(
                    index,
                    _op_name(op),
                    RESPONSE_BUDGET,
                    "this op's result alone exceeds the response budget; narrow it and retry",
                )
                own_too_big_size = _entry_size(own_too_big)
                if own_too_big_size <= held[index] - separator:
                    entry, size = own_too_big, own_too_big_size
                else:
                    entry, size = budget_errors[index], held[index] - separator
                truncated = True
            elif fitted is None:
                exhausted = True
                truncated = True
            else:
                entry, size = fitted, fit_size
                if trimmed:
                    exhausted = True
                    truncated = True
        if entry is None:
            entry = deadline_errors[index] if timed_out else budget_errors[index]
            size = held[index] - separator
        results.append(entry)
        used += separator + size

    return QueryResult(results=results, truncated=truncated)
