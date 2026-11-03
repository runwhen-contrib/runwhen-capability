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
import yaml

from runwhen_capability.contracts import SCHEMA_NAMES, validate

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"

# Schemas that are still being written, or that are shared definitions only.
NO_EXAMPLE_NEEDED = {"common", "rw.discovery.v1", "rw.discovery.summary.v1"}

# Valid fixtures that fail today: path relative to contracts/examples -> the
# exact violation. strict xfail, so fixing the schema or the wiki flips it red.
KNOWN_FAILURES: dict[str, str] = {
    "types/41a-kubernetes.yaml": (
        "YAML 1.1 reads the unquoted alias `no` (kubectl's short name for nodes, "
        "`aliases: [no]` on the `node` line) as boolean false; the schema reports "
        "/types/node/aliases/0: False is not of type 'string'. The wiki block needs "
        '`"no"`, or the loader needs a YAML 1.2 boolean rule.'
    ),
}


class _Loader(yaml.SafeLoader):
    """SafeLoader that leaves `2026-11-03T09:14:02Z` a string (schemas use
    `format: date-time` on strings), instead of resolving it to a datetime."""


_Loader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _load_yaml(text: str):
    return yaml.load(text, Loader=_Loader)  # noqa: S506 - a SafeLoader subclass


def _frontmatter(text: str) -> str:
    lines = text.splitlines()
    start = lines.index("---")
    end = lines.index("---", start + 1)
    return "\n".join(lines[start + 1 : end])


def _load(path: Path):
    text = path.read_text(encoding="utf-8")
    return _load_yaml(_frontmatter(text) if path.suffix == ".md" else text)


def _fixtures(*, invalid: bool) -> list[Path]:
    return sorted(
        p
        for p in EXAMPLES.rglob("*")
        if p.is_file()
        and p.suffix in {".yaml", ".md"}
        and ("invalid" in p.relative_to(EXAMPLES).parts) == invalid
    )


def _rel(path: Path) -> str:
    return path.relative_to(EXAMPLES).as_posix()


def _valid_params():
    for path in _fixtures(invalid=False):
        marks = []
        reason = KNOWN_FAILURES.get(_rel(path))
        if reason:
            marks.append(pytest.mark.xfail(strict=True, reason=reason))
        yield pytest.param(path, id=_rel(path), marks=marks)


def _schema_of(path: Path) -> str:
    return path.relative_to(EXAMPLES).parts[0]


_EXPECT = re.compile(r"^# expect: (\S+) (.+)$")


def _expectation(path: Path) -> tuple[str, str]:
    for line in path.read_text(encoding="utf-8").splitlines()[:3]:
        match = _EXPECT.match(line)
        if match:
            pointer, substring = match.groups()
            return ("" if pointer == '""' else pointer), substring.strip()
    raise AssertionError(f"{_rel(path)}: no `# expect: <json-pointer> <substring>` line")


def test_loader_keeps_timestamps_as_strings():
    doc = _load_yaml("at: 2026-11-03T09:14:02Z\nmode: '0600'\nnext: on\n")
    assert doc["at"] == "2026-11-03T09:14:02Z"
    assert doc["mode"] == "0600"


def test_fixture_directories_are_schemas():
    unknown = {_schema_of(p) for p in _fixtures(invalid=False) + _fixtures(invalid=True)} - set(
        SCHEMA_NAMES
    )
    assert not unknown, f"examples for schemas that do not exist: {sorted(unknown)}"


@pytest.mark.parametrize("path", _valid_params())
def test_every_valid_example_validates(path: Path):
    violations = validate(_schema_of(path), _load(path))
    assert not violations, "\n".join(f"{v.path or '(root)'}: {v.message}" for v in violations)


@pytest.mark.parametrize("path", _fixtures(invalid=False), ids=_rel)
def test_valid_example_names_its_origin(path: Path):
    head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:2])
    assert "# source: " in head, f"{_rel(path)}: no `# source:` line"
    if "# source: synthetic" in head:
        assert path.name.startswith("synthetic-"), (
            f"{_rel(path)}: synthetic fixtures are named synthetic-*"
        )
    else:
        assert re.match(r"\d+[a-z]?-", path.name), (
            f"{_rel(path)}: wiki fixtures are named <page>-<slug>"
        )


@pytest.mark.parametrize("path", _fixtures(invalid=True), ids=_rel)
def test_every_invalid_example_fails_where_expected(path: Path):
    pointer, substring = _expectation(path)
    violations = validate(_schema_of(path), _load(path))
    assert violations, f"{_rel(path)} validated, but it should be rejected"
    assert any(v.path == pointer and substring in v.message for v in violations), (
        f"expected a violation at {pointer or '(root)'} containing {substring!r}; got:\n"
        + "\n".join(f"  {v.path or '(root)'}: {v.message}" for v in violations)
    )


def test_every_schema_has_an_example():
    covered = {_schema_of(p) for p in _fixtures(invalid=False)}
    missing = set(SCHEMA_NAMES) - NO_EXAMPLE_NEEDED - covered
    assert not missing, f"schemas with no valid example fixture: {sorted(missing)}"
