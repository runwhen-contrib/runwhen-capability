"""Discovery and loading of the fixtures under contracts/examples.

Shared by test_examples.py (JSON Schema) and test_models.py (generated models),
so both read the same files the same way. Fixtures are read from the repo's
`contracts/examples`, never from the packaged `_data` copy.
"""

from __future__ import annotations

from pathlib import Path

from jsonschema import Draft202012Validator

from runwhen_capability.contracts import _validate, load_yaml, schema, split_frontmatter, validate

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"

#: Fixtures under these directories (relative to EXAMPLES) are one `$defs` entry of the schema
#: named by their first path part, not a whole document of it: directory -> `$defs` name.
DEF_FIXTURES = {"auth-plan/materialized": "materializedCredential"}


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


def def_of(path: Path) -> str | None:
    """The `$defs` entry a fixture is an instance of; None for a whole document."""
    rel = path.relative_to(EXAMPLES).as_posix()
    return next(
        (name for prefix, name in DEF_FIXTURES.items() if rel.startswith(prefix + "/")), None
    )


def validate_fixture(path: Path, doc):
    """`contracts.validate` for a whole document; the `$defs` entry's violations for a def
    fixture (same fields as `ContractViolation`)."""
    name, def_name = schema_of(path), def_of(path)
    if def_name is None:
        return validate(name, doc)
    ref = {"$ref": f"{schema(name)['$id']}#/$defs/{def_name}"}
    validator = Draft202012Validator(ref, registry=_validate._loaded().registry)
    return sorted(
        (
            _validate.ContractViolation(
                _validate._pointer(e.absolute_path),
                e.message,
                _validate._pointer(e.absolute_schema_path),
            )
            for e in validator.iter_errors(doc)
        ),
        key=lambda v: (v.path, v.message),
    )
