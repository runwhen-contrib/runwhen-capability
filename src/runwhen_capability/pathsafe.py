"""Path containment -- confines an untrusted, repo-relative path to a tree
root before it touches disk. Shared by sarif.py (a SARIF-reported path,
before reading its context line) and repo_fs.py (read/grep/ls path
inputs) -- this is ordinary input validation, not a sandbox (kernel-level
isolation is the executor pod's job, out of scope for this SDK).
"""

from __future__ import annotations

import os
from pathlib import Path


def confined_to(base: Path, target: Path) -> bool:
    """Reports whether target is inside (or equal to) base."""
    try:
        rel = os.path.relpath(str(target), str(base))
    except ValueError:
        return False
    return rel != ".." and not rel.startswith(".." + os.sep)


def safe_path(worktree: Path, path: str) -> Path | None:
    """Confines a repo-relative `path` to `worktree` before it is read from
    disk. `path` must already be normalize_path()'d. On success, returns the
    joined, worktree-confined path; on any attempt to escape worktree --
    an absolute path, "..", or a symlink that resolves outside -- returns
    None."""
    clean = os.path.normpath(path)
    if os.path.isabs(clean) or clean == ".." or clean.startswith(".." + os.sep):
        return None

    full = worktree / clean
    if not confined_to(worktree, full):
        return None

    try:
        resolved_full = full.resolve(strict=True)
    except FileNotFoundError:
        # A missing file isn't an escape attempt -- let the caller's own
        # read fail closed instead.
        return full
    except OSError:
        return None

    try:
        resolved_worktree = worktree.resolve(strict=True)
    except OSError:
        return None
    if not confined_to(resolved_worktree, resolved_full):
        return None

    return full
