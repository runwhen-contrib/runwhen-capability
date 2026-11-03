"""Fixture scenarios for `rwtask test` (wiki 42a "Local development", D43).

A scenario is one YAML file, `skills/<skill>/tests/<task>/<scenario>.yaml`:

    resource: kubernetes/clusters/kind/namespaces/web/deployments/api   # optional
    inputs: {limit: 3}                    # optional, typed values
    settings: {label: x}                  # optional
    identity: {cluster: kind}             # optional, overrides the path-derived identity
    calls:                                # the CLI calls the task makes, in order
      - argv: [kubectl, get, pods]        # argv[0] is the tool's basename
        stdin: "..."                      # optional; asserted only when present
        rc: 0                             # default 0
        stdout: "..."                     # default ""
        stderr: "..."                     # default ""
    expect:
      status: ok | partial | failed | skipped
      errorCodes: [permission_denied]     # optional; the envelope's codes as a set
      outputs: {name: value}              # optional; subset match, see `match_expect`

The format is the SDK's own (not a contract); lane F's generators write it. Loading is
strict: an unknown key, a wrong type or a missing `expect` is an error naming the file and
the path, never a silently ignored typo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from ..contracts import load_yaml
from ..rwerrors import ERROR_CODES

STATUSES = ("ok", "partial", "failed", "skipped")

_STRING_MAP = {"type": "object", "additionalProperties": {"type": "string"}}

SCENARIO_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "rwtask test scenario",
    "type": "object",
    "additionalProperties": False,
    "required": ["expect"],
    "properties": {
        "resource": {"type": "string", "minLength": 1},
        "inputs": {"type": "object"},
        "settings": {"type": "object"},
        "identity": _STRING_MAP,
        "calls": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["argv"],
                "properties": {
                    "argv": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                    "stdin": {"type": "string"},
                    "rc": {"type": "integer"},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                },
            },
        },
        "expect": {
            "type": "object",
            "additionalProperties": False,
            "required": ["status"],
            "properties": {
                "status": {"enum": list(STATUSES)},
                "errorCodes": {"type": "array", "items": {"enum": list(ERROR_CODES)}},
                "outputs": {"type": "object"},
            },
        },
    },
}

_VALIDATOR = jsonschema.Draft202012Validator(SCENARIO_SCHEMA)


class FixtureError(ValueError):
    """A scenario file that cannot be used. `errors` are the individual problems."""

    def __init__(self, path: Path | str, errors: list[str]) -> None:
        self.path = str(path)
        self.errors = errors
        super().__init__(f"{path}: " + "; ".join(errors))


@dataclass
class Scenario:
    name: str
    path: Path
    expect: dict
    resource: str | None = None
    inputs: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    identity: dict = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)


def _path(parts) -> str:
    return "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in parts).lstrip(".")


def load_scenario(path: Path) -> Scenario:
    """The scenario in `path`, validated, with every call's `rc`/`stdout`/`stderr`
    defaulted. Raises FixtureError."""
    path = Path(path)
    try:
        doc = load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise FixtureError(path, [f"cannot read it: {exc}"]) from None
    except yaml.YAMLError as exc:
        raise FixtureError(path, [f"not valid YAML: {exc}"]) from None
    if not isinstance(doc, dict):
        raise FixtureError(path, ["a scenario is a mapping with `calls` and `expect`"])
    problems = []
    for error in sorted(_VALIDATOR.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path))):
        where = _path(error.absolute_path)
        if error.validator == "required":
            problems.extend(
                f"{where or '(scenario)'}: {name!r} is a required property"
                for name in error.validator_value
                if name not in error.instance
            )
        elif error.validator == "additionalProperties":
            extra = sorted(set(error.instance) - set(error.schema.get("properties", {})))
            problems.append(f"{where + ': ' if where else ''}unknown key(s) {', '.join(extra)}")
        else:
            problems.append(f"{where or '(scenario)'}: {error.message}"[:300])
    if problems:
        raise FixtureError(path, problems)
    calls = [
        {"argv": list(c["argv"]), "rc": 0, "stdout": "", "stderr": "", **_without_argv(c)}
        for c in doc.get("calls") or []
    ]
    return Scenario(
        name=path.stem,
        path=path,
        expect=doc["expect"],
        resource=doc.get("resource"),
        inputs=doc.get("inputs") or {},
        settings=doc.get("settings") or {},
        identity=doc.get("identity") or {},
        calls=calls,
    )


def _without_argv(call: dict) -> dict:
    return {k: v for k, v in call.items() if k != "argv"}


def scenario_files(tests_dir: Path) -> list[Path]:
    """The `*.yaml` scenarios directly in `tests_dir`, by name."""
    try:
        return sorted(p for p in Path(tests_dir).glob("*.yaml") if p.is_file())
    except OSError:
        return []


# -- writing ----------------------------------------------------------------------------


class _Dumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.SafeDumper, value: str):
    # Multi-line text as a literal block, so a recorded stdout diffs line by line.
    # PyYAML falls back to a quoted scalar for text a block cannot carry.
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str_representer)


def dump_scenario(
    *,
    resource: str | None,
    inputs: dict,
    settings: dict,
    identity: dict,
    calls: list[dict],
    expect: dict,
    header: str = "",
) -> str:
    """The YAML text of a scenario (the `--record` output)."""
    doc: dict[str, Any] = {}
    if resource is not None:
        doc["resource"] = resource
    for key, value in (("inputs", inputs), ("settings", settings), ("identity", identity)):
        if value:
            doc[key] = value
    doc["calls"] = calls
    doc["expect"] = expect
    body = yaml.dump(doc, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)
    return (header.rstrip("\n") + "\n" if header else "") + body


# -- matching -----------------------------------------------------------------------------


def _same(a: Any, b: Any) -> bool:
    """JSON equality: a bool is not a number, an int equals the same float."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict | list) or isinstance(b, dict | list):
        return False
    if isinstance(a, int | float) and isinstance(b, int | float):
        return a == b
    return type(a) is type(b) and a == b


def _subset(expected: Any, got: Any, where: str, problems: list[str]) -> None:
    if isinstance(expected, dict) and isinstance(got, dict):
        for key, value in expected.items():
            sub = f"{where}.{key}"
            if key not in got:
                problems.append(f"{sub}: missing")
            else:
                _subset(value, got[key], sub, problems)
        return
    if not _same(expected, got):
        problems.append(f"{where}: expected {expected!r}, got {got!r}")


def match_expect(envelope: dict, expect: dict) -> list[str]:
    """Why `envelope` does not meet `expect`: one message per mismatch, [] for a pass.

    `status` must be equal. `errorCodes`, when given, is compared with the envelope's
    codes as a sorted set. `outputs` is a subset match: every expected key of a dict must
    be present with an equal value, recursively through dicts; a list or a scalar must be
    equal as a whole (a bool is not a number)."""
    problems: list[str] = []
    if envelope.get("status") != expect.get("status"):
        problems.append(f"status: expected {expect.get('status')}, got {envelope.get('status')}")
    if "errorCodes" in expect:
        got = sorted({e["code"] for e in envelope.get("errors") or []})
        want = sorted(set(expect["errorCodes"]))
        if got != want:
            problems.append(f"errorCodes: expected {want}, got {got}")
    if "outputs" in expect:
        _subset(expect["outputs"], envelope.get("outputs") or {}, "outputs", problems)
    return problems
