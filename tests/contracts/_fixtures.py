"""Discovery and loading of the fixtures under contracts/examples.

Shared by test_examples.py (JSON Schema) and test_models.py (generated models),
so both read the same files the same way. Fixtures are read from the repo's
`contracts/examples`, never from the packaged `_data` copy.
"""

from __future__ import annotations

from pathlib import Path

from runwhen_capability.contracts import load_yaml, split_frontmatter

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"


def load_fixture(path: Path):
    """A `.yaml` fixture is the document; a `.md` fixture is its frontmatter."""
    text = path.read_text(encoding="utf-8")
    return split_frontmatter(text)[0] if path.suffix == ".md" else load_yaml(text)


def fixtures(*, invalid: bool) -> list[Path]:
    return sorted(
        p
        for p in EXAMPLES.rglob("*")
        if p.is_file()
        and p.suffix in {".yaml", ".md"}
        and ("invalid" in p.relative_to(EXAMPLES).parts) == invalid
    )


def fixture_id(path: Path) -> str:
    return path.relative_to(EXAMPLES).as_posix()


def schema_of(path: Path) -> str:
    return path.relative_to(EXAMPLES).parts[0]
