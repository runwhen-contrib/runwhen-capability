"""ctx.repo_fs -- read/grep/ls against a real tree on disk. Exercises glob
depth and the containment rules read/grep/ls all share.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from runwhen_capability.repo_fs import (
    MAX_LS_ENTRIES,
    MAX_READ_RESPONSE_BYTES,
    MAX_UNREADABLE_PATHS,
    BinaryFileError,
    PathEscapesTreeError,
    TreeNotMaterializedError,
    find_around,
    grep_tree,
    ls_tree,
    read_lines,
    read_ranges,
)

# An unreadable directory is unreadable only for a non-root user; root
# ignores the mode bits and the test would assert the opposite of reality.
skip_if_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions"
)


def make_tree(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    tree = tmp_path / "tree"
    tree.mkdir()
    for rel, content in files.items():
        full = tree / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            full.write_bytes(content)
        else:
            full.write_text(content)
    return tree


# --- read ----------------------------------------------------------------


def test_read_whole_file_when_no_range_given(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "one\ntwo\nthree\n"})

    got = read_lines(tree, "a.py")

    assert got.path == "a.py"
    assert got.content == "one\ntwo\nthree\n"
    assert got.startLine == 1
    assert got.totalLines == 4  # split("\n") on a trailing-newline file yields a final ""
    assert got.truncated is False


def test_read_line_range(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "one\ntwo\nthree\nfour\n"})

    got = read_lines(tree, "a.py", start_line=2, end_line=3)

    assert got.content == "two\nthree"
    assert got.startLine == 2
    assert got.endLine == 3


def test_read_missing_file_raises(tmp_path):
    # A real (non-empty) tree missing the requested file -- distinct from
    # the tree itself being empty/not materialized, covered below.
    tree = make_tree(tmp_path, {"a.py": "x\n"})

    with pytest.raises(FileNotFoundError):
        read_lines(tree, "nope.py")


def test_read_binary_file_raises(tmp_path):
    tree = make_tree(tmp_path, {"bin": b"\x00\x01\x02binary"})

    with pytest.raises(BinaryFileError):
        read_lines(tree, "bin")


def test_read_rejects_path_escaping_the_tree_with_dotdot(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "x\n"})

    with pytest.raises(PathEscapesTreeError):
        read_lines(tree, "../outside.py")


def test_read_rejects_absolute_path(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "x\n"})

    with pytest.raises(PathEscapesTreeError):
        read_lines(tree, "/etc/passwd")


def test_read_rejects_symlink_escaping_the_tree(tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("secret\n")
    tree = make_tree(tmp_path, {})
    (tree / "link.py").symlink_to(outside)

    with pytest.raises(PathEscapesTreeError):
        read_lines(tree, "link.py")


# --- tree not materialized (a missing/empty tree must never be
# indistinguishable from a genuine zero-match/zero-entry result) -----------


def test_read_missing_tree_raises_typed_error(tmp_path):
    tree = tmp_path / "never-checked-out"

    with pytest.raises(TreeNotMaterializedError) as exc_info:
        read_lines(tree, "a.py")
    assert str(tree) in str(exc_info.value)


def test_read_empty_tree_raises_typed_error(tmp_path):
    tree = make_tree(tmp_path, {})

    with pytest.raises(TreeNotMaterializedError) as exc_info:
        read_lines(tree, "a.py")
    assert str(tree) in str(exc_info.value)


def test_read_git_only_tree_raises_typed_error(tmp_path):
    """A clone that fetched objects into .git/ but was never checked out
    (e.g. `git clone --no-checkout`) is not a materialized tree either --
    exactly how the original silent-absence bug reached the agent:
    read/grep/ls against it looked like a real, empty result rather than a
    broken checkout. `.git` alone must not count as content."""
    tree = make_tree(tmp_path, {".git/config": "[core]\n"})

    with pytest.raises(TreeNotMaterializedError) as exc_info:
        read_lines(tree, "a.py")
    assert str(tree) in str(exc_info.value)


def test_read_over_the_byte_budget_stops_early_and_flags_truncated(tmp_path):
    # 4,000 lines of 1 KiB each -- far past MAX_READ_RESPONSE_BYTES.
    tree = make_tree(tmp_path, {"big.py": "\n".join("x" * 1024 for _ in range(4000))})

    got = read_lines(tree, "big.py")

    assert got.truncated is True
    assert len(got.content.encode("utf-8")) <= MAX_READ_RESPONSE_BYTES
    assert got.totalLines == 4000
    assert got.endLine < 4000  # the honest end of what we actually returned


def test_read_single_line_larger_than_the_budget_is_clipped_and_flagged(tmp_path):
    # One line, no trailing newline: nothing follows it to trip the "next
    # line would overflow" check, so this is the path that used to return a
    # multi-megabyte response with truncated: false -- a minified bundle or
    # a one-line lock file in a real repo.
    tree = make_tree(tmp_path, {"bundle.min.js": "z" * (3 * 1024 * 1024)})

    got = read_lines(tree, "bundle.min.js")

    assert got.truncated is True
    assert len(got.content.encode("utf-8")) <= MAX_READ_RESPONSE_BYTES


# --- grep ------------------------------------------------------------------


def test_grep_matches_across_files_and_skips_binary(tmp_path):
    tree = make_tree(
        tmp_path,
        {
            "src/a.py": "import os\nprint('hello')\n",
            "src/b.py": "import sys\nIMPORT OS\n",
            "docs/c.md": "import os is mentioned here too\n",
            "binary": b"\x00\x01\x02import-not-really-text",
        },
    )

    got = grep_tree(tree, r"^import os$")

    assert len(got.matches) == 1
    assert got.matches[0].path == "src/a.py"
    assert got.matches[0].line == 1
    assert got.truncated is False


def test_grep_ignore_case_widens_the_match(tmp_path):
    tree = make_tree(tmp_path, {"src/a.py": "import os\n", "src/b.py": "IMPORT OS\n"})

    got = grep_tree(tree, r"^import os$", ignore_case=True)

    assert {m.path for m in got.matches} == {"src/a.py", "src/b.py"}


def test_grep_glob_narrows_the_file_set(tmp_path):
    tree = make_tree(tmp_path, {"src/a.py": "needle\n", "docs/c.md": "needle\n"})

    got = grep_tree(tree, "needle", glob="docs/*")

    assert len(got.matches) == 1
    assert got.matches[0].path == "docs/c.md"


def test_grep_zero_matches_is_an_empty_list_not_none(tmp_path):
    """PIN: this is the distinction the whole tree-not-materialized fix is
    about -- a real, non-empty tree searched for a pattern that isn't
    there returns matches: [], no error. Only a missing/empty *tree*
    (below) is an error."""
    tree = make_tree(tmp_path, {"a.py": "nothing here\n"})

    got = grep_tree(tree, "no-such-pattern-anywhere")

    assert got.matches == []
    assert isinstance(got.matches, list)


def test_grep_missing_tree_raises_typed_error(tmp_path):
    tree = tmp_path / "never-checked-out"

    with pytest.raises(TreeNotMaterializedError) as exc_info:
        grep_tree(tree, "anything")
    assert str(tree) in str(exc_info.value)


def test_grep_empty_tree_raises_typed_error(tmp_path):
    tree = make_tree(tmp_path, {})

    with pytest.raises(TreeNotMaterializedError) as exc_info:
        grep_tree(tree, "anything")
    assert str(tree) in str(exc_info.value)


def test_grep_git_only_tree_raises_typed_error(tmp_path):
    tree = make_tree(tmp_path, {".git/config": "[core]\n"})

    with pytest.raises(TreeNotMaterializedError) as exc_info:
        grep_tree(tree, "anything")
    assert str(tree) in str(exc_info.value)


@skip_if_root
def test_grep_unreadable_tree_root_raises_instead_of_reporting_matches_empty(tmp_path):
    """grep has no sub-path scoping input the way ls_tree has `path` -- the
    tree itself is the one thing a caller has no way to not be asking
    about, so an unreadable root must raise, not render as `matches: []`
    (indistinguishable from "nothing matched"). In practice
    check_tree_materialized's own tree.iterdir() call already raises for
    this exact case before _walk_files ever runs; _walk_files' own
    _on_walk_error guard exists for defense in depth (e.g. a permissions
    change between that check and the walk itself) rather than being the
    only thing standing between this and a silent empty result."""
    tree = make_tree(tmp_path, {"ok.py": "import os\n"})
    os.chmod(tree, 0o000)
    try:
        with pytest.raises(OSError):
            grep_tree(tree, "import")
    finally:
        os.chmod(tree, 0o755)


@skip_if_root
def test_grep_unreadable_subdirectory_encountered_while_walking_is_disclosed(tmp_path):
    """Unlike the tree root above, a subdirectory merely *encountered*
    while walking is NOT something the caller explicitly asked about --
    grep has no way to scope to it directly -- so it can't raise. It must
    still be disclosed via `unreadable`, not silently dropped: `matches:
    []`/no error for the unreadable half of the tree is the exact
    silent-absence shape this whole module exists to close."""
    tree = make_tree(tmp_path, {"ok.py": "import os\n", "locked/secret.py": "import os\n"})
    locked = tree / "locked"
    os.chmod(locked, 0o000)
    try:
        got = grep_tree(tree, "import")
        assert [m.path for m in got.matches] == ["ok.py"]
        assert got.truncated is False
        assert got.unreadable == ["locked"]
        assert got.unreadableTruncated is False
    finally:
        os.chmod(locked, 0o755)


@skip_if_root
def test_grep_unreadable_file_encountered_while_walking_is_disclosed(tmp_path):
    """The per-file counterpart: a file that fails to open (not a
    directory) must also surface in `unreadable`, not just render as "the
    pattern wasn't found here"."""
    tree = make_tree(tmp_path, {"ok.py": "import os\n", "secret.py": "import os\n"})
    secret = tree / "secret.py"
    os.chmod(secret, 0o000)
    try:
        got = grep_tree(tree, "import")
        assert [m.path for m in got.matches] == ["ok.py"]
        assert got.unreadable == ["secret.py"]
        assert got.unreadableTruncated is False
    finally:
        os.chmod(secret, 0o644)


@skip_if_root
def test_grep_unreadable_paths_are_capped_and_flagged(tmp_path):
    """A pathologically large unreadable subtree must not blow the
    envelope: `unreadable` stays bounded at MAX_UNREADABLE_PATHS, and
    `unreadableTruncated` discloses that more were dropped -- the same
    shape `truncated` already gives a capped `matches` list."""
    files = {f"f{i:04d}.py": "import os\n" for i in range(MAX_UNREADABLE_PATHS + 5)}
    tree = make_tree(tmp_path, {"ok.py": "import os\n", **files})
    locked = [tree / name for name in files]
    for f in locked:
        os.chmod(f, 0o000)
    try:
        got = grep_tree(tree, "import")
        assert [m.path for m in got.matches] == ["ok.py"]
        assert len(got.unreadable) == MAX_UNREADABLE_PATHS
        assert got.unreadableTruncated is True
    finally:
        for f in locked:
            os.chmod(f, 0o644)


def test_grep_max_matches_caps_and_reports_truncated(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "import a\n", "b.py": "import b\n", "c.py": "import c\n"})

    got = grep_tree(tree, "import", max_matches=1)

    assert len(got.matches) == 1
    assert got.truncated is True


def test_grep_skips_dot_git_entirely(tmp_path):
    tree = make_tree(tmp_path, {".git/config": "import os\n", "a.py": "import os\n"})

    got = grep_tree(tree, "import")

    assert all(not m.path.startswith(".git") for m in got.matches)
    assert len(got.matches) == 1


def test_grep_text_truncated_at_400_chars(tmp_path):
    long_line = "needle" + ("x" * 1000)
    tree = make_tree(tmp_path, {"long.txt": long_line})

    got = grep_tree(tree, "needle")

    assert len(got.matches) == 1
    assert len(got.matches[0].text) == 400


def test_grep_rejects_invalid_pattern(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "x\n"})

    with pytest.raises(ValueError):
        grep_tree(tree, "(unclosed")


def test_grep_exclude_drops_lines_before_they_count_toward_max_matches(tmp_path):
    """`refs` (the query task) excludes definition lines through this: an
    excluded line must not use up a match slot, and must be judged on the
    full line rather than the 400-byte text snippet."""
    long_skip = " " * 450 + "SKIP needle"
    tree = make_tree(tmp_path, {"a.txt": f"{long_skip}\nSKIP needle\nneedle 1\nneedle 2\n"})

    got = grep_tree(tree, "needle", max_matches=2, exclude=lambda line: "SKIP" in line)

    assert [m.line for m in got.matches] == [3, 4]


# --- glob-at-any-depth regression -------------------------------------------


def test_grep_glob_matches_at_any_depth_not_only_root(tmp_path):
    """FIELD FAILURE regression: a no-slash glob like "*.py" previously
    matched only at the repo root (path.Match-equivalent never crosses
    "/"). It must match a NESTED file too -- a root-only implementation
    would find only tools/a.py, missing tools/utils/b.py."""
    tree = make_tree(
        tmp_path,
        {
            "tools/a.py": "needle\n",
            "tools/utils/b.py": "needle\n",  # nested -- this is the regression case
            "docs/c.md": "needle\n",
        },
    )

    bare = grep_tree(tree, "needle", glob="*.py")
    assert {m.path for m in bare.matches} == {"tools/a.py", "tools/utils/b.py"}

    scoped = grep_tree(tree, "needle", glob="tools/*.py")
    assert {m.path for m in scoped.matches} == {"tools/a.py"}

    double_star = grep_tree(tree, "needle", glob="**/*.py")
    assert {m.path for m in double_star.matches} == {"tools/a.py", "tools/utils/b.py"}


# --- ls ----------------------------------------------------------------


def test_ls_default_depth_is_immediate_children_only(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "x", "sub/b.py": "y"})

    got = ls_tree(tree)

    paths = {e.path for e in got.entries}
    assert paths == {"a.py", "sub"}
    dir_entry = next(e for e in got.entries if e.path == "sub")
    assert dir_entry.type == "dir"


def test_ls_depth_recurses(tmp_path):
    tree = make_tree(tmp_path, {"sub/b.py": "y", "sub/deeper/c.py": "z"})

    got = ls_tree(tree, depth=3)

    paths = {e.path for e in got.entries}
    assert "sub/b.py" in paths
    assert "sub/deeper" in paths
    assert "sub/deeper/c.py" in paths


def test_ls_empty_subdirectory_within_a_real_tree_is_an_empty_list_not_none(tmp_path):
    """A legitimately empty subdirectory *inside* a real, materialised
    tree is a valid zero-entry result -- distinct from the tree itself
    being empty (below), which means the checkout never happened."""
    tree = make_tree(tmp_path, {"a.py": "x"})
    (tree / "empty").mkdir()

    got = ls_tree(tree, path="empty")

    assert got.entries == []
    assert isinstance(got.entries, list)
    assert got.truncated is False


def test_ls_missing_tree_raises_typed_error(tmp_path):
    tree = tmp_path / "never-checked-out"

    with pytest.raises(TreeNotMaterializedError) as exc_info:
        ls_tree(tree)
    assert str(tree) in str(exc_info.value)


def test_ls_empty_tree_raises_typed_error(tmp_path):
    tree = make_tree(tmp_path, {})

    with pytest.raises(TreeNotMaterializedError) as exc_info:
        ls_tree(tree)
    assert str(tree) in str(exc_info.value)


def test_ls_git_only_tree_raises_typed_error(tmp_path):
    """A directory containing only `.git` -- a clone whose objects were
    fetched but never checked out -- is a false-positive materialisation:
    `any(tree.iterdir())` alone would see one entry and call it done. This
    is how the original silent-absence bug reached the agent."""
    tree = make_tree(tmp_path, {".git/config": "[core]\n"})

    with pytest.raises(TreeNotMaterializedError) as exc_info:
        ls_tree(tree)
    assert str(tree) in str(exc_info.value)


def test_ls_skips_dot_git(tmp_path):
    tree = make_tree(tmp_path, {".git/config": "x", "a.py": "y"})

    got = ls_tree(tree)

    assert {e.path for e in got.entries} == {"a.py"}


def test_ls_reports_file_size(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "12345"})

    got = ls_tree(tree)

    entry = next(e for e in got.entries if e.path == "a.py")
    assert entry.type == "file"
    assert entry.size == 5


def test_ls_rejects_path_escaping_the_tree(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "x"})

    with pytest.raises(PathEscapesTreeError):
        ls_tree(tree, path="../")


def test_ls_not_a_directory_raises(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "x"})

    with pytest.raises(NotADirectoryError):
        ls_tree(tree, path="a.py")


def test_ls_over_cap_truncates_and_flags_it(tmp_path):
    files = {f"f{i:04d}.py": "x" for i in range(MAX_LS_ENTRIES + 5)}
    tree = make_tree(tmp_path, files)

    got = ls_tree(tree)

    assert len(got.entries) == MAX_LS_ENTRIES
    assert got.truncated is True


def test_ls_under_cap_is_not_truncated(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "x", "b.py": "y"})

    got = ls_tree(tree)

    assert len(got.entries) == 2
    assert got.truncated is False


def test_ls_symlinked_entry_is_type_other_and_not_recursed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("s")
    tree = make_tree(tmp_path, {})
    (tree / "link").symlink_to(outside)

    # depth=5 -- proves the symlink is skipped BECAUSE it is a symlink, not
    # merely because the default depth=1 wouldn't have recursed anyway.
    got = ls_tree(tree, depth=5)

    entry = next(e for e in got.entries if e.path == "link")
    assert entry.type == "other"
    assert not any(e.path.startswith("link/") for e in got.entries)


@skip_if_root
def test_ls_unreadable_directory_raises_instead_of_reporting_it_empty(tmp_path):
    """The bug this branch exists to kill, on the ls path: a directory we
    cannot read must never render as `entries: []`, which a caller reads as
    "the directory is empty"."""
    tree = make_tree(tmp_path, {"ok.py": "x", "locked/secret.py": "s"})
    locked = tree / "locked"
    os.chmod(locked, 0o000)
    try:
        with pytest.raises(OSError):
            ls_tree(tree, path="locked")
    finally:
        os.chmod(locked, 0o755)


@skip_if_root
def test_ls_unreadable_subdirectory_encountered_while_recursing_is_disclosed(tmp_path):
    """Unlike the directory the caller explicitly asked for above, a
    subdirectory merely *encountered* while recursing (depth > 1) can't
    raise -- it wasn't the caller's own ask. It must still be disclosed
    via `unreadable`, not silently dropped from the listing."""
    tree = make_tree(tmp_path, {"ok.py": "x", "locked/secret.py": "s"})
    locked = tree / "locked"
    os.chmod(locked, 0o000)
    try:
        got = ls_tree(tree, depth=3)
        assert {e.path for e in got.entries} == {"ok.py", "locked"}
        assert got.unreadable == ["locked"]
        assert got.unreadableTruncated is False
    finally:
        os.chmod(locked, 0o755)


# --- grep: multi-glob OR and context lines ---------------------------------


def test_grep_globs_list_ors_multiple_patterns(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "needle\n", "b.md": "needle\n", "c.txt": "needle\n"})

    got = grep_tree(tree, "needle", globs=["*.py", "*.md"])

    assert {m.path for m in got.matches} == {"a.py", "b.md"}


def test_grep_glob_and_globs_combine_with_or(tmp_path):
    """`glob` (singular, back-compat) and `globs` (new, plural) OR together
    rather than one overriding the other."""
    tree = make_tree(tmp_path, {"a.py": "needle\n", "b.md": "needle\n", "c.txt": "needle\n"})

    got = grep_tree(tree, "needle", glob="*.py", globs=["*.md"])

    assert {m.path for m in got.matches} == {"a.py", "b.md"}


def test_grep_context_lines_before_and_after(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "one\ntwo\nneedle\nfour\nfive"})

    got = grep_tree(tree, "needle", context=1)

    assert got.matches[0].before == ["two"]
    assert got.matches[0].after == ["four"]


def test_grep_context_clipped_at_file_start_and_end(tmp_path):
    """A context wider than the file must clip at both edges rather than
    error or wrap -- the same honesty read_lines' byte-budget clipping
    already gives a single range."""
    tree = make_tree(tmp_path, {"a.py": "one\nneedle"})

    got = grep_tree(tree, "needle", context=5)

    assert got.matches[0].before == ["one"]  # clipped: only 1 line precedes it
    assert got.matches[0].after == []  # clipped: nothing follows it


def test_grep_context_defaults_to_no_context(tmp_path):
    """Existing callers (context=0, the default) must see no before/after
    fields materialize -- unchanged shape."""
    tree = make_tree(tmp_path, {"a.py": "one\nneedle\nthree"})

    got = grep_tree(tree, "needle")

    assert got.matches[0].before == []
    assert got.matches[0].after == []


def test_grep_context_lines_are_capped_like_text(tmp_path):
    """GrepMatch.text is cut at MAX_GREP_MATCH_TEXT_LEN, but a `before`/
    `after` context line was never put through the same cut -- one long
    line pulled in only as context (never itself matched by `pattern`)
    could blow the same per-match budget `text` is already capped
    against. Every context line must get the identical cut."""
    long_line = "y" * 1000
    tree = make_tree(tmp_path, {"a.py": f"{long_line}\nneedle\n{long_line}\n"})

    got = grep_tree(tree, "needle", context=1)

    assert len(got.matches[0].before[0]) == 400
    assert len(got.matches[0].after[0]) == 400
    assert got.matches[0].before[0] == long_line[:400]
    assert got.matches[0].after[0] == long_line[:400]


# --- bad patterns must not crash the whole call: re.compile can raise more
# than re.error on adversarial input --
# OverflowError on a huge {n} repetition, RecursionError on deep nesting --
# and both must become the same InvalidPatternError grep_tree/find_around
# already raise for an ordinary re.error. -----------------------------------


def test_grep_rejects_pattern_with_overflow_repetition(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "x\n"})

    with pytest.raises(ValueError):
        grep_tree(tree, "a{4294967296}")


def test_grep_rejects_deeply_nested_pattern(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "x\n"})

    with pytest.raises(ValueError):
        grep_tree(tree, "(" * 1000)


# --- read_ranges: merged, multi-range reads ---------------------------------


def test_read_ranges_merges_overlapping_and_adjacent(tmp_path):
    body = "\n".join(f"line{i}" for i in range(1, 21))  # line1..line20
    tree = make_tree(tmp_path, {"a.py": body})

    got = read_ranges(tree, "a.py", [(1, 5), (4, 8), (9, 10), (15, 16)])

    assert got.path == "a.py"
    assert got.totalLines == 20
    assert [(r.start, r.end) for r in got.ranges] == [(1, 10), (15, 16)]
    assert got.ranges[0].content == "\n".join(f"line{i}" for i in range(1, 11))
    assert got.ranges[1].content == "line15\nline16"
    assert got.truncated is False


def test_read_ranges_keeps_an_empty_entry_for_a_range_starting_past_eof(tmp_path):
    """A range entirely past EOF must not be silently dropped -- it gets a
    ReadRange with empty content, the same honesty read_lines already gives
    a single out-of-range request (empty content, not an error, not a
    vanished result)."""
    body = "\n".join(f"line{i}" for i in range(1, 11))  # line1..line10, no trailing newline
    tree = make_tree(tmp_path, {"a.py": body})

    got = read_ranges(tree, "a.py", [(1, 3), (50, 60)])

    assert got.totalLines == 10
    assert [(r.start, r.end, r.content) for r in got.ranges] == [
        (1, 3, "line1\nline2\nline3"),
        (50, 10, ""),
    ]


def test_read_ranges_missing_tree_raises_typed_error(tmp_path):
    tree = tmp_path / "never-checked-out"

    with pytest.raises(TreeNotMaterializedError):
        read_ranges(tree, "a.py", [(1, 1)])


def test_read_ranges_rejects_path_escaping_the_tree(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "x\n"})

    with pytest.raises(PathEscapesTreeError):
        read_ranges(tree, "../outside.py", [(1, 1)])


def test_read_ranges_binary_file_raises(tmp_path):
    tree = make_tree(tmp_path, {"bin": b"\x00\x01\x02binary"})

    with pytest.raises(BinaryFileError):
        read_ranges(tree, "bin", [(1, 1)])


# --- find_around: a context window around each match -----------------------


def test_find_around_returns_window_around_each_match(tmp_path):
    body = "\n".join(f"line{i}" for i in range(1, 21))
    tree = make_tree(tmp_path, {"a.py": body})

    windows = find_around(tree, "a.py", "line10", context=2)

    assert windows == [(8, 12)]


def test_find_around_clips_at_file_start_and_end(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "one\nneedle\nthree"})

    windows = find_around(tree, "a.py", "needle", context=5)

    assert windows == [(1, 3)]


def test_find_around_caps_at_max_windows(tmp_path):
    body = "\n".join("needle" for _ in range(10))
    tree = make_tree(tmp_path, {"a.py": body})

    windows = find_around(tree, "a.py", "needle", context=0, max_windows=3)

    assert windows == [(1, 1), (2, 2), (3, 3)]


def test_find_around_rejects_path_escaping_the_tree(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "x\n"})

    with pytest.raises(PathEscapesTreeError):
        find_around(tree, "../outside.py", "x", context=1)


def test_find_around_missing_tree_raises_typed_error(tmp_path):
    tree = tmp_path / "never-checked-out"

    with pytest.raises(TreeNotMaterializedError):
        find_around(tree, "a.py", "x", context=1)


def test_find_around_rejects_invalid_pattern_even_for_a_missing_file(tmp_path):
    """The pattern is validated before the file itself is looked at -- same
    order as grep_tree -- so an invalid pattern always surfaces as its own
    ValueError, never masked by an unrelated FileNotFoundError."""
    tree = make_tree(tmp_path, {"a.py": "x\n"})

    with pytest.raises(ValueError, match="invalid pattern"):
        find_around(tree, "nope.py", "(unclosed", context=1)


def test_find_around_rejects_pattern_with_overflow_repetition(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "x\n"})

    with pytest.raises(ValueError):
        find_around(tree, "a.py", "a{4294967296}", context=1)


def test_find_around_rejects_deeply_nested_pattern(tmp_path):
    tree = make_tree(tmp_path, {"a.py": "x\n"})

    with pytest.raises(ValueError):
        find_around(tree, "a.py", "(" * 1000, context=1)
