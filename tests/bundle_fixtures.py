"""load_bundle(name) -- reads tests/fixtures/bundles/<name>/** off disk into
the bundle-relative-path -> text mapping every runwhen_capability.custom
function and bundle.py take as `files`. Shared by tests/custom/*.py and the
top-level bundle-host tests.
"""

from __future__ import annotations

from pathlib import Path

BUNDLES_DIR = Path(__file__).parent / "fixtures" / "bundles"


def load_bundle(name: str) -> dict[str, str]:
    root = BUNDLES_DIR / name
    return {
        path.relative_to(root).as_posix(): path.read_text()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
