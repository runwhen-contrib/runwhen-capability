#!/usr/bin/env python3
"""Mirror contracts/ into the package as src/runwhen_capability/contracts/_data/.

The wheel ships only the package, so the schemas it validates against are this
copy. Run from the repo root:

    python3 scripts/sync_contracts.py            # make _data match contracts/
    python3 scripts/sync_contracts.py --check    # change nothing; exit 1 on drift

Files only in contracts/ are added, files that differ are overwritten, files
only in _data/ are deleted (and directories left empty). Standard library only,
so CI and the drift test can run it without installing anything.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "contracts"
TARGET = REPO / "src" / "runwhen_capability" / "contracts" / "_data"
IGNORED = {".DS_Store"}


def _files(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        return {}
    return {
        p.relative_to(root).as_posix(): p
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name not in IGNORED and "__pycache__" not in p.parts
    }


def diff() -> tuple[list[str], list[str], list[str]]:
    """(added, removed, changed) relative paths, going from TARGET to SOURCE."""
    source, target = _files(SOURCE), _files(TARGET)
    added = sorted(source.keys() - target.keys())
    removed = sorted(target.keys() - source.keys())
    changed = sorted(
        rel
        for rel in source.keys() & target.keys()
        if source[rel].read_bytes() != target[rel].read_bytes()
    )
    return added, removed, changed


def sync(added: list[str], removed: list[str], changed: list[str]) -> None:
    for rel in added + changed:
        dest = TARGET / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((SOURCE / rel).read_bytes())
    for rel in removed:
        (TARGET / rel).unlink()
    # Drop directories the deletions emptied, deepest first.
    for directory in sorted((d for d in TARGET.rglob("*") if d.is_dir()), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="change nothing; exit 1 if _data/ has drifted"
    )
    args = parser.parse_args(argv)

    added, removed, changed = diff()
    if args.check:
        if not (added or removed or changed):
            return 0
        print("packaged contracts are out of date; run `make contracts-sync`:")
        for label, paths in (("added", added), ("removed", removed), ("changed", changed)):
            for rel in paths:
                print(f"  {label}: {rel}")
        return 1

    sync(added, removed, changed)
    for label, paths in (("added", added), ("removed", removed), ("changed", changed)):
        for rel in paths:
            print(f"{label}: {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
