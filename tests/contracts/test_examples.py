"""Round-trip the wiki's examples through the contracts' JSON Schemas.

`contracts/examples/<schema-name>/` holds one fixture per example block on the
spec pages (42a, 41a, 42b, 50, 52, 46), copied verbatim except for marked edits
(`# fixture:` comments), plus `synthetic-*` fixtures for the schemas the wiki
has no example for. `invalid/` holds documents each schema must reject.

Conventions:

* A `.yaml` fixture is the document. A `.md` fixture (SKILL.md, rule,
  knowledge note) is validated on its frontmatter, the YAML between the first
  two `---` lines; its `# source:` / `# expect:` comment is the first line
  *inside* the frontmatter so the file stays a real markdown-with-frontmatter
  file.
* An invalid fixture carries `# expect: <json-pointer> <substring>`. Some
  violation must have exactly that `path` and the substring in its `message`;
  the token `""` stands for the document root (the empty pointer).
* Fixtures are read from the repo's `contracts/examples`, never from `_data`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from _fixtures import fixture_id, fixtures, load_fixture, schema_of

from runwhen_capability.contracts import SCHEMA_NAMES, load_yaml, validate

# Shared definitions only, no document of their own.
NO_EXAMPLE_NEEDED = {"common"}

# Valid fixtures that fail today: path relative to contracts/examples -> the
# exact violation. strict xfail, so fixing the schema or the wiki flips it red.
KNOWN_FAILURES: dict[str, str] = {}


def _valid_params():
    for path in fixtures(invalid=False):
        marks = []
        reason = KNOWN_FAILURES.get(fixture_id(path))
        if reason:
            marks.append(pytest.mark.xfail(strict=True, reason=reason))
        yield pytest.param(path, id=fixture_id(path), marks=marks)


_EXPECT = re.compile(r"^# expect: (\S+) (.+)$")


def _expectation(path: Path) -> tuple[str, str]:
    for line in path.read_text(encoding="utf-8").splitlines()[:3]:
        match = _EXPECT.match(line)
        if match:
            pointer, substring = match.groups()
            return ("" if pointer == '""' else pointer), substring.strip()
    raise AssertionError(f"{fixture_id(path)}: no `# expect: <json-pointer> <substring>` line")


def test_loader_keeps_timestamps_as_strings():
    doc = load_yaml("at: 2026-11-03T09:14:02Z\nmode: '0600'\nnext: on\n")
    assert doc["at"] == "2026-11-03T09:14:02Z"
    assert doc["mode"] == "0600"


def test_fixture_directories_are_schemas():
    unknown = {schema_of(p) for p in fixtures(invalid=False) + fixtures(invalid=True)} - set(
        SCHEMA_NAMES
    )
    assert not unknown, f"examples for schemas that do not exist: {sorted(unknown)}"


@pytest.mark.parametrize("path", _valid_params())
def test_every_valid_example_validates(path: Path):
    violations = validate(schema_of(path), load_fixture(path))
    assert not violations, "\n".join(f"{v.path or '(root)'}: {v.message}" for v in violations)


@pytest.mark.parametrize("path", fixtures(invalid=False), ids=fixture_id)
def test_valid_example_names_its_origin(path: Path):
    head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:2])
    assert "# source: " in head, f"{fixture_id(path)}: no `# source:` line"
    if "# source: synthetic" in head:
        assert path.name.startswith("synthetic-"), (
            f"{fixture_id(path)}: synthetic fixtures are named synthetic-*"
        )
    else:
        assert re.match(r"\d+[a-z]?-", path.name), (
            f"{fixture_id(path)}: wiki fixtures are named <page>-<slug>"
        )


@pytest.mark.parametrize("path", fixtures(invalid=True), ids=fixture_id)
def test_every_invalid_example_fails_where_expected(path: Path):
    pointer, substring = _expectation(path)
    violations = validate(schema_of(path), load_fixture(path))
    assert violations, f"{fixture_id(path)} validated, but it should be rejected"
    assert any(v.path == pointer and substring in v.message for v in violations), (
        f"expected a violation at {pointer or '(root)'} containing {substring!r}; got:\n"
        + "\n".join(f"  {v.path or '(root)'}: {v.message}" for v in violations)
    )


def test_every_schema_has_an_example():
    covered = {schema_of(p) for p in fixtures(invalid=False)}
    missing = set(SCHEMA_NAMES) - NO_EXAMPLE_NEEDED - covered
    assert not missing, f"schemas with no valid example fixture: {sorted(missing)}"
