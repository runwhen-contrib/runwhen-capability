"""Read a source directory into memory."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def read_tree(root: Path) -> tuple[dict[str, bytes], frozenset[str]]:
    """Every regular file under `root` as {relative POSIX path: bytes}, and the paths that
    carry the owner exec bit. Symlinks, other special files, dot-files and `__pycache__`
    are skipped."""
    files: dict[str, bytes] = {}
    executable: set[str] = set()

    def walk(directory: Path, rel: str) -> None:
        with os.scandir(directory) as scan:
            children = sorted(scan, key=lambda e: e.name)
        for child in children:
            if child.name.startswith(".") or child.name == "__pycache__":
                continue
            mode = child.stat(follow_symlinks=False).st_mode
            child_rel = f"{rel}{child.name}"
            if stat.S_ISDIR(mode):
                walk(Path(child.path), child_rel + "/")
            elif stat.S_ISREG(mode):
                files[child_rel] = Path(child.path).read_bytes()
                if mode & stat.S_IXUSR:
                    executable.add(child_rel)

    walk(Path(root), "")
    return files, frozenset(executable)
