"""ctx.repo_fs -- read-only file/grep/ls access into an already-checked-out
tree. Globs match basenames at any depth; empty grep/ls results serialise
as []. Backs a repository capability's `read`/`grep`/`ls` tasks.

Every path input is confined to the tree via pathsafe.safe_path before it
touches disk -- ordinary input validation, not a sandbox (kernel-level
isolation is the executor pod's job). A path that escapes (absolute, "..", or an escaping symlink)
raises PathEscapesTreeError with a clear message. Entries *encountered*
while walking a tree for grep/ls (as opposed to a caller-supplied path) are
handled conservatively: a symlink is skipped/not recursed into rather
than erroring -- it is tree content the walk found, not an input someone
is attempting to escape with.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Callable
from pathlib import Path

from .findings import normalize_path
from .models import (
    GrepMatch,
    GrepResult,
    LsEntry,
    LsResult,
    ReadRange,
    ReadRangesResult,
    ReadResult,
)
from .pathsafe import safe_path

# read: binary detection = NUL byte in the first
# 8 KiB; response content is capped well under the 256 KiB response cap,
# leaving headroom for the rest of the envelope (path, startLine, endLine,
# totalLines, truncated) around content.
BINARY_SNIFF_LEN = 8 * 1024
MAX_READ_RESPONSE_BYTES = 256 * 1024
READ_BUDGET = MAX_READ_RESPONSE_BYTES - 1024

# grep: at most 200 matches (maxMatches defaults to and is capped at 200),
# each match's text truncated at 400 chars.
DEFAULT_GREP_MAX_MATCHES = 200
HARD_GREP_MAX_MATCHES = 200
MAX_GREP_MATCH_TEXT_LEN = 400

# ls: at most 500 entries; depth 1 is the default (immediate children only).
MAX_LS_ENTRIES = 500
DEFAULT_LS_DEPTH = 1

# unreadable-path disclosure (grep and ls, both of which walk a subtree):
# capped so a pathologically large unreadable subtree can't blow the
# envelope the way an unbounded `matches`/`entries` list could -- the cap
# itself is disclosed via `unreadableTruncated`, the same shape `truncated`
# already gives the capped match/entry list.
MAX_UNREADABLE_PATHS = 100


class PathEscapesTreeError(ValueError):
    """A caller-supplied path resolved outside the checked-out tree."""


class BinaryFileError(ValueError):
    """`read` was asked to read a file that sniffs as binary."""


class InvalidPatternError(ValueError):
    """A caller-supplied regular expression (grep's `pattern`, read's
    `around`) does not compile."""


class TreeNotMaterializedError(RuntimeError):
    """`tree` is not a usable checkout -- it does not exist, is not a
    directory, or has no checked-out content (empty, or containing only
    `.git`). Raised by read/grep/ls before any of their real work, so an
    infrastructure failure (a reaped scope, a fresh pod, a clone that
    fetched objects but was never checked out) can never be silently
    reported as a genuine zero-match/zero-entry result -- see the module
    docstring for why that distinction matters."""


def error_code(exc: BaseException) -> str | None:
    """The stable, machine-readable code for a caller-input failure raised
    by read/grep/ls/find_around -- what the `query` task reports per op
    instead of failing the whole batch -- or None for anything else.

    TreeNotMaterializedError deliberately has no code here: it is not a
    per-op condition but an infrastructure miss, and must fail the whole
    task so host.py flags the envelope not_materialized and the platform
    re-materialises. An unmapped exception is a bug, not caller input, and
    propagates the same way."""
    # Subclasses before their bases: PathEscapesTreeError, BinaryFileError
    # and InvalidPatternError are all ValueErrors; FileNotFoundError and
    # NotADirectoryError are OSErrors.
    if isinstance(exc, PathEscapesTreeError):
        return "PATH_ESCAPES_TREE"
    if isinstance(exc, BinaryFileError):
        return "BINARY_FILE"
    if isinstance(exc, InvalidPatternError):
        return "INVALID_PATTERN"
    if isinstance(exc, FileNotFoundError):
        return "NOT_FOUND"
    if isinstance(exc, NotADirectoryError):
        return "NOT_A_DIRECTORY"
    if isinstance(exc, OSError):
        # e.g. permission denied on the path the caller named (ls_tree's
        # strict listing, or the file itself)
        return "UNREADABLE"
    return None


def check_tree_materialized(tree: Path) -> None:
    if not tree.exists():
        raise TreeNotMaterializedError(f"tree not materialized: {tree} does not exist")
    if not tree.is_dir():
        raise TreeNotMaterializedError(f"tree not materialized: {tree} is not a directory")
    # A directory containing ONLY ".git" -- a clone that fetched objects
    # but never checked out a working tree -- is not materialized either:
    # this is exactly how the original silent-absence bug reached the
    # agent (grep/ls/read against it looked like a real, empty result
    # instead of a broken checkout). ".git" alone doesn't count as content.
    if not any(entry.name != ".git" for entry in tree.iterdir()):
        raise TreeNotMaterializedError(f"tree not materialized: {tree} has no checked-out content")


def _confined(tree: Path, path: str) -> Path:
    rel = normalize_path(path or "")
    resolved = safe_path(tree, rel)
    if resolved is None:
        raise PathEscapesTreeError(f"path escapes tree: {path!r}")
    return resolved


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:BINARY_SNIFF_LEN]


def _clip_grep_line(line: str) -> str:
    """The same MAX_GREP_MATCH_TEXT_LEN cut GrepMatch.text already gets --
    applied identically to every `before`/`after` context line, so one long
    line pulled in only as context (never itself matching `pattern`) can't
    blow the same budget `text` is already capped against. Byte-sliced then
    decoded with errors="ignore", exactly like `text`'s own cut."""
    line_bytes = line.encode("utf-8")
    if len(line_bytes) <= MAX_GREP_MATCH_TEXT_LEN:
        return line
    return line_bytes[:MAX_GREP_MATCH_TEXT_LEN].decode("utf-8", errors="ignore")


class _UnreadablePaths:
    """Collects paths *encountered* but not readable during a grep/ls walk
    (permission denied, a TOCTOU race, etc.), capped at MAX_UNREADABLE_PATHS
    -- shared by grep_tree/_walk_files/_grep_file and ls_tree/_ls_walk so
    both surfaces disclose the same shape. `.paths` feeds `unreadable`;
    `.truncated` feeds `unreadableTruncated` and is set the instant the cap
    bites, exactly like `truncated` already is for `matches`/`entries`."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.truncated = False

    def add(self, rel: str) -> None:
        if len(self.paths) < MAX_UNREADABLE_PATHS:
            self.paths.append(rel)
        else:
            self.truncated = True


# --- read --------------------------------------------------------------


def read_lines(
    tree: Path, path: str, start_line: int | None = None, end_line: int | None = None
) -> ReadResult:
    """Reads lines [start_line, end_line] (1-indexed, inclusive; an omitted or
    out-of-range value defaults to the whole file) from `path`, stopping
    early -- and reporting `truncated` -- the instant the joined content
    would push the response past READ_BUDGET."""
    check_tree_materialized(tree)
    full = _confined(tree, path)
    if not full.is_file():
        raise FileNotFoundError(f"file not found: {path!r}")

    data = full.read_bytes()
    if _is_binary(data):
        raise BinaryFileError(f"binary file: {path!r}")

    lines = data.decode("utf-8", errors="replace").split("\n")
    total_lines = len(lines)

    start = start_line if start_line and start_line >= 1 else 1
    end = end_line if end_line and 0 < end_line <= total_lines else total_lines

    content, truncated, actual_end, _ = _select_lines(lines, start, end)
    return ReadResult(
        path=normalize_path(path),
        content=content,
        startLine=start,
        endLine=actual_end,
        totalLines=total_lines,
        truncated=truncated,
    )


def _select_lines(
    lines: list[str], start: int, end: int, budget: int = READ_BUDGET
) -> tuple[str, bool, int, int]:
    """Returns (content, truncated, actual_end, bytes_used) -- `budget`
    defaults to the whole READ_BUDGET for a single-range `read_lines` call,
    but `read_ranges` passes the REMAINING budget so several ranges share
    one MAX_READ_RESPONSE_BYTES-wide envelope rather than each getting its
    own full budget."""
    if start > end or start > len(lines):
        actual_end = min(start - 1, len(lines))
        return "", False, actual_end, 0

    parts: list[str] = []
    size = 0
    truncated = False
    actual_end = start - 1
    for i in range(start, min(end, len(lines)) + 1):
        line = lines[i - 1]
        add = len(line.encode("utf-8")) + (1 if parts else 0)  # +1: the joining newline
        if size + add > budget:
            if parts:
                truncated = True
                break
            # A single line longer than the whole budget (a minified bundle,
            # a one-line lock file): clip it rather than return a response
            # many times MAX_READ_RESPONSE_BYTES while reporting
            # truncated: false. Byte-sliced then decoded with
            # errors="ignore" -- a split multi-byte rune is dropped, not
            # mojibake'd -- exactly like grep's MAX_GREP_MATCH_TEXT_LEN.
            clipped = line.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
            return clipped, True, i, len(clipped.encode("utf-8"))
        parts.append(line)
        size += add
        actual_end = i
    return "\n".join(parts), truncated, actual_end, size


def _merge_ranges(ranges: list[tuple[int, int]], total_lines: int) -> list[tuple[int, int]]:
    """Clamps each (start, end) into [1, total_lines] -- same defaulting as
    read_lines' start/end (an out-of-range end falls back to the last
    line) -- then merges overlapping or ADJACENT ranges (end + 1 == next
    start) after sorting by start, so [1,5] and [6,10] collapse into one
    [1,10] range rather than two that back onto each other.

    A range starting past EOF (`s > total_lines`, so `s > e` after
    clamping) is NOT dropped here: read_ranges still emits a ReadRange for
    it, with empty content -- the same honesty read_lines already gives a
    single out-of-range request, rather than silently vanishing it from
    the result."""
    clamped: list[tuple[int, int]] = []
    for start, end in ranges:
        s = start if start and start >= 1 else 1
        e = end if end and end <= total_lines else total_lines
        clamped.append((s, e))

    clamped.sort()
    merged: list[tuple[int, int]] = []
    for s, e in clamped:
        if merged and s <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def read_ranges(tree: Path, path: str, ranges: list[tuple[int, int]]) -> ReadRangesResult:
    """The multi-range counterpart to read_lines: merges `ranges` (see
    _merge_ranges) and fills each merged range's content against a single
    byte budget shared across the WHOLE call, so N ranges together still
    land under MAX_READ_RESPONSE_BYTES instead of each getting its own
    full READ_BUDGET. Once the shared budget is spent, remaining ranges are
    dropped entirely and `truncated` is set -- the same honesty a
    within-range clip already gives read_lines."""
    check_tree_materialized(tree)
    full = _confined(tree, path)
    if not full.is_file():
        raise FileNotFoundError(f"file not found: {path!r}")

    data = full.read_bytes()
    if _is_binary(data):
        raise BinaryFileError(f"binary file: {path!r}")

    lines = data.decode("utf-8", errors="replace").split("\n")
    total_lines = len(lines)

    merged = _merge_ranges(ranges, total_lines)

    out: list[ReadRange] = []
    truncated = False
    remaining_budget = READ_BUDGET
    for start, end in merged:
        if remaining_budget <= 0:
            truncated = True
            break
        content, range_truncated, actual_end, used = _select_lines(
            lines, start, end, budget=remaining_budget
        )
        out.append(ReadRange(start=start, end=actual_end, content=content))
        remaining_budget -= used
        if range_truncated:
            truncated = True

    return ReadRangesResult(
        path=normalize_path(path),
        ranges=out,
        totalLines=total_lines,
        truncated=truncated,
    )


# --- grep --------------------------------------------------------------


def _path_match(pattern: str, name: str) -> bool:
    """Go's path.Match semantics: '*'/'?' never cross '/', and the pattern
    and the name must have the same number of '/'-separated segments."""
    pattern_parts = pattern.split("/")
    name_parts = name.split("/")
    if len(pattern_parts) != len(name_parts):
        return False
    return all(fnmatch.fnmatchcase(np, pp) for pp, np in zip(pattern_parts, name_parts))


def match_glob(glob: str, rel: str) -> bool:
    """A glob with no "/" (e.g. "*.py") matches at ANY depth --
    against rel's basename -- rather than only at the repo root, since a
    bare path.Match-equivalent never crosses "/". A leading "**/" is
    stripped first, so "**/*.py" behaves exactly like "*.py". Any other
    glob (one that still contains "/" after stripping "**/") is matched
    against the full repo-relative path, e.g. "tools/*.py" matches only
    files directly inside tools/, not tools/utils/*.py."""
    g = glob[3:] if glob.startswith("**/") else glob
    if "/" not in g:
        return _path_match(g, os.path.basename(rel))
    return _path_match(g, rel)


def _walk_files(tree: Path, unreadable: _UnreadablePaths):
    """Depth-first, lexically sorted per directory (mirrors
    filepath.WalkDir's default order), skipping ".git" entirely and any
    symlink (file or directory) without following it."""
    tree = Path(tree)
    root_str = str(tree)

    def _on_walk_error(exc: OSError) -> None:
        # os.walk's default (onerror=None) silently skips any directory it
        # cannot scandir and moves on to its siblings. But grep has no
        # sub-path scoping input the way ls_tree has `path` -- `tree`
        # itself is the one thing a grep caller has no way to NOT be
        # asking about -- so an unreadable tree root is the caller's
        # literal, explicit ask, and `matches: []` for it would be the same
        # lie ls_tree's strict=True already closed for its own
        # caller-supplied path (85b3a8b). Raise for that one path; a
        # subtree merely *encountered* while walking is recorded into
        # `unreadable` instead (see this module's docstring and
        # `_UnreadablePaths`).
        if exc.filename == root_str:
            raise exc
        rel = normalize_path(str(Path(exc.filename).relative_to(tree)))
        unreadable.add(rel)

    for root, dirnames, filenames in os.walk(tree, onerror=_on_walk_error, followlinks=False):
        root_path = Path(root)
        dirnames.sort()
        filenames.sort()

        kept_dirs = []
        for d in dirnames:
            full_d = root_path / d
            rel_d = normalize_path(str(full_d.relative_to(tree)))
            if rel_d == ".git" or full_d.is_symlink():
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for fn in filenames:
            full = root_path / fn
            if full.is_symlink():
                continue
            rel = normalize_path(str(full.relative_to(tree)))
            if rel == ".git":
                continue
            yield rel, full


def _grep_file(
    full: Path,
    rel: str,
    pattern: re.Pattern,
    limit: int,
    matches: list[GrepMatch],
    unreadable: _UnreadablePaths,
    context: int = 0,
    exclude: Callable[[str], bool] | None = None,
) -> bool:
    """Scans one file line by line, appending every match until `limit` is
    reached, returning True the instant that happens so the caller can stop
    walking rather than scan the rest of the tree for matches nobody will
    see.

    `context` (0 by default -- unchanged shape for existing callers) slices
    each match's `before`/`after` window straight out of the file's own
    line list, so it's clipped at the file's start/end by ordinary list
    slicing rather than any extra bounds check.

    `exclude` (None by default) drops a matching line before it counts
    toward `limit`, and sees the FULL line, never the 400-byte `text`
    snippet -- `refs` uses it to skip definition lines, where a definition
    keyword past the snippet cut must still be seen."""
    try:
        data = full.read_bytes()
    except OSError:
        # An unreadable file must not fail the whole walk -- but it must
        # not render identically to "the pattern didn't match here"
        # either: `rel` was only *encountered* while walking, not a path
        # the caller named directly, so it can't raise the way a
        # caller-supplied path does (see _walk_files' _on_walk_error) --
        # it is disclosed via `unreadable` instead.
        unreadable.add(rel)
        return False
    if _is_binary(data):
        return False  # binary -- skip, not an error

    text = data.decode("utf-8", errors="replace")
    # mirrors bufio.ScanLines' CRLF handling
    lines = [raw_line.rstrip("\r") for raw_line in text.split("\n")]
    for line_no, line in enumerate(lines, start=1):
        if not pattern.search(line):
            continue
        if exclude is not None and exclude(line):
            continue
        snippet = _clip_grep_line(line)
        before = (
            [_clip_grep_line(x) for x in lines[max(0, line_no - 1 - context) : line_no - 1]]
            if context
            else []
        )
        after = [_clip_grep_line(x) for x in lines[line_no : line_no + context]] if context else []
        matches.append(GrepMatch(path=rel, line=line_no, text=snippet, before=before, after=after))
        if len(matches) >= limit:
            return True
    return False


def grep_tree(
    tree: Path,
    pattern: str,
    glob: str | None = None,
    globs: list[str] | None = None,
    max_matches: int | None = None,
    ignore_case: bool = False,
    context: int = 0,
    exclude: Callable[[str], bool] | None = None,
) -> GrepResult:
    """`pattern` is a regular expression (Python's `re`).

    `glob` (singular, back-compat) and `globs` (a list, for the query
    task) OR together -- a file is kept if it matches ANY of them,
    with match_glob's same any-depth semantics for each -- so an existing
    `glob=` caller sees no change when `globs` is omitted, and a caller
    that only passes `globs` needs no `glob`.

    `exclude` is passed straight to _grep_file (see its docstring)."""
    check_tree_materialized(Path(tree))
    try:
        compiled = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except (re.error, OverflowError, RecursionError) as exc:
        raise InvalidPatternError(f"invalid pattern: {exc}") from exc

    limit = max_matches if max_matches and max_matches > 0 else DEFAULT_GREP_MAX_MATCHES
    limit = min(limit, HARD_GREP_MAX_MATCHES)

    all_globs = list(globs) if globs else []
    if glob:
        all_globs.append(glob)

    matches: list[GrepMatch] = []
    unreadable = _UnreadablePaths()
    for rel, full in _walk_files(tree, unreadable):
        if all_globs and not any(match_glob(g, rel) for g in all_globs):
            continue
        if _grep_file(
            full, rel, compiled, limit, matches, unreadable, context=context, exclude=exclude
        ):
            break
        if len(matches) >= limit:
            break

    return GrepResult(
        matches=matches,
        truncated=len(matches) >= limit,
        unreadable=unreadable.paths,
        unreadableTruncated=unreadable.truncated,
    )


def find_around(
    tree: Path, path: str, pattern: str, context: int, max_windows: int = 5
) -> list[tuple[int, int]]:
    """Finds up to `max_windows` matches of `pattern` (a regular
    expression, same convention as grep_tree) in `path`, in file order, and
    returns a `±context`-line window around each match's line -- clamped
    to [1, totalLines] the same way _merge_ranges clamps read_ranges'
    input. Backs the query task's `read` op's `around` field: the caller then treats
    the returned windows as `ranges` and hands them to read_ranges, which
    is what actually merges any that overlap."""
    check_tree_materialized(tree)
    try:
        compiled = re.compile(pattern)
    except (re.error, OverflowError, RecursionError) as exc:
        raise InvalidPatternError(f"invalid pattern: {exc}") from exc

    full = _confined(tree, path)
    if not full.is_file():
        raise FileNotFoundError(f"file not found: {path!r}")

    data = full.read_bytes()
    if _is_binary(data):
        raise BinaryFileError(f"binary file: {path!r}")

    lines = data.decode("utf-8", errors="replace").split("\n")
    total_lines = len(lines)

    windows: list[tuple[int, int]] = []
    for line_no, raw_line in enumerate(lines, start=1):
        if len(windows) >= max_windows:
            break
        line = raw_line.rstrip("\r")
        if not compiled.search(line):
            continue
        windows.append((max(1, line_no - context), min(total_lines, line_no + context)))
    return windows


# --- ls ------------------------------------------------------------------


def _ls_walk(
    directory: Path,
    rel_prefix: str,
    depth: int,
    max_depth: int,
    entries: list[LsEntry],
    unreadable: _UnreadablePaths,
    strict: bool = False,
) -> None:
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        # `strict` is set only for the directory the caller actually asked
        # for: failing to read THAT one must surface as an error, never as
        # `entries: []` -- an unreadable directory and an empty one must not
        # render identically (this module's docstring; the same rule
        # check_tree_materialized enforces one level up). A directory
        # merely *encountered* while recursing can't raise the same way (it
        # wasn't the caller's explicit ask) -- it is disclosed via
        # `unreadable` instead.
        if strict:
            raise
        unreadable.add(rel_prefix)
        return

    for name in names:
        if len(entries) >= MAX_LS_ENTRIES:
            return

        full = directory / name
        rel_path = f"{rel_prefix}/{name}" if rel_prefix else name
        if rel_path == ".git":
            continue

        if full.is_symlink():
            typ, size = "other", 0
        elif full.is_dir():
            typ, size = "dir", 0
        else:
            typ = "file"
            try:
                size = full.stat().st_size
            except OSError:
                size = 0
        entries.append(LsEntry(path=rel_path, type=typ, size=size))

        if typ == "dir" and depth < max_depth and len(entries) < MAX_LS_ENTRIES:
            _ls_walk(full, rel_path, depth + 1, max_depth, entries, unreadable)


def ls_tree(tree: Path, path: str | None = None, depth: int | None = None) -> LsResult:
    """Lists `path` (repo root when omitted) up to `depth` levels deep (default 1:
    immediate children only), sorted lexically per directory, skipping
    ".git" entirely and capping the total at MAX_LS_ENTRIES -- `truncated`
    is set the instant the cap bites, the same honesty grep's `truncated`
    already gives a capped match list (previously ls silently reported a
    capped listing as if it were complete)."""
    check_tree_materialized(Path(tree))
    root = _confined(tree, path or "")
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {path!r}")

    max_depth = depth if depth and depth > 0 else DEFAULT_LS_DEPTH
    entries: list[LsEntry] = []
    unreadable = _UnreadablePaths()
    _ls_walk(root, "", 1, max_depth, entries, unreadable, strict=True)
    return LsResult(
        entries=entries,
        truncated=len(entries) >= MAX_LS_ENTRIES,
        unreadable=unreadable.paths,
        unreadableTruncated=unreadable.truncated,
    )


class RepoFsClient:
    """`ctx.repo_fs` -- read/grep/ls operations against an
    already-checked-out tree (see git.py's GitClient.checkout for how a
    setup task materialises the tree itself)."""

    def __init__(self, ctx) -> None:
        self._ctx = ctx

    def read(
        self, tree: Path, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> ReadResult:
        return read_lines(Path(tree), path, start_line=start_line, end_line=end_line)

    def grep(
        self,
        tree: Path,
        pattern: str,
        glob: str | None = None,
        max_matches: int | None = None,
        ignore_case: bool = False,
    ) -> GrepResult:
        return grep_tree(
            Path(tree), pattern, glob=glob, max_matches=max_matches, ignore_case=ignore_case
        )

    def ls(self, tree: Path, path: str | None = None, depth: int | None = None) -> LsResult:
        return ls_tree(Path(tree), path=path, depth=depth)
