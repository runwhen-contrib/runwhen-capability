"""Fixture scenarios for `rwtask test` (wiki 42a "Local development", D43).

A scenario is one YAML file, `skills/<skill>/tests/<task>/<scenario>.yaml`:

    resource: kubernetes/clusters/kind/namespaces/web/deployments/api   # optional
    inputs: {limit: 3}                    # optional, typed values
    settings: {label: x}                  # optional
    identity: {cluster: kind}             # optional, overrides the path-derived identity
    dryRun: true                          # optional: RW_DRY_RUN=1; the task must declare dryRun
    now: "2026-11-04T09:14:02.123Z"       # optional: the run's reference time (RW_NOW, ctx.now());
                                          # --record writes it; absent: a fresh time
    calls:                                # the calls the task makes, in order
      - argv: [kubectl, get, pods]        # a CLI call; argv[0] is the tool's basename
        stdin: "..."                      # optional; asserted only when present
        rc: 0                             # default 0
        stdout: "..."                     # default ""
        stderr: "..."                     # default ""
      - type: k8s-proxy-get               # ctx.k8s_proxy_get / rw_k8s_proxy_get (k8s_proxy.py)
        request: {namespace: vault, kind: services, name: vault, port: 8200,
                  path: /v1/sys/health, scheme: http}      # scheme defaults to http
        response: {status: 200, headers: {}, body: "...", truncated: false, location: null}
        # or, for a call that failed:  error: {code: unreachable, message: "...", scope: ...}
    expect:
      status: ok | partial | failed | skipped
      errorCodes: [permission_denied]     # optional; the envelope's codes as a set
      outputs: {name: value}              # optional; subset match, see `match_expect`
      artifacts:                          # optional; per artifact output of the task
        <name>: {contains: [str], notContains: [str], lines: {min: 1, max: 9}}

The format is the SDK's own (not a contract); lane F's generators write it. Loading is
strict: an unknown key, a wrong type or a missing `expect` is an error naming the file and
the path, never a silently ignored typo.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from ..contracts import load_yaml
from ..k8s_proxy import CALL_TYPE as PROXY_CALL_TYPE
from ..reftime import format_now, parse_now
from ..rwerrors import ERROR_CODES

STATUSES = ("ok", "partial", "failed", "skipped")

_NOW_PATTERN = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$"

_STRING_MAP = {"type": "object", "additionalProperties": {"type": "string"}}

_CLI_CALL: dict[str, Any] = {
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
}

_PROXY_CALL: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["type", "request"],
    "properties": {
        "type": {"const": PROXY_CALL_TYPE},
        "request": {
            "type": "object",
            "additionalProperties": False,
            "required": ["namespace", "kind", "name", "port", "path"],
            "properties": {
                "namespace": {"type": "string"},
                "kind": {"enum": ["pods", "services"]},
                "name": {"type": "string"},
                "port": {"type": ["integer", "string"]},
                "path": {"type": "string"},
                "scheme": {"enum": ["http", "https"]},
            },
        },
        "response": {
            "type": "object",
            "additionalProperties": False,
            "required": ["status"],
            "properties": {
                "status": {"type": "integer"},
                "headers": _STRING_MAP,
                "body": {"type": "string"},
                "truncated": {"type": "boolean"},
                "location": {"type": ["string", "null"]},
            },
        },
        "error": {
            "type": "object",
            "additionalProperties": False,
            "required": ["code", "message"],
            "properties": {
                "code": {"enum": list(ERROR_CODES)},
                "message": {"type": "string"},
                "scope": {"type": "string"},
                "hint": {"type": "string"},
            },
        },
    },
}

#: A call is a proxy call when it names a `type`; otherwise it is a CLI call (the original
#: shape), so a mistake in either reports against its own schema.
_CALL: dict[str, Any] = {
    "type": "object",
    "if": {"required": ["type"], "properties": {"type": {"const": PROXY_CALL_TYPE}}},
    "then": _PROXY_CALL,
    "else": _CLI_CALL,
}

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
        "dryRun": {"type": "boolean"},
        "now": {"type": "string", "pattern": _NOW_PATTERN},
        "calls": {"type": "array", "items": _CALL},
        "expect": {
            "type": "object",
            "additionalProperties": False,
            "required": ["status"],
            "properties": {
                "status": {"enum": list(STATUSES)},
                "errorCodes": {"type": "array", "items": {"enum": list(ERROR_CODES)}},
                "outputs": {"type": "object"},
                "artifacts": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "contains": {"type": "array", "items": {"type": "string"}},
                            "notContains": {"type": "array", "items": {"type": "string"}},
                            "lines": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "min": {"type": "integer", "minimum": 0},
                                    "max": {"type": "integer", "minimum": 0},
                                },
                            },
                        },
                    },
                },
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
    dry_run: bool = False
    #: the run's reference time (`now:`), or None: replay with a fresh one
    now: datetime | None = None


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
    if not problems:
        problems = _call_problems(doc.get("calls") or []) + _expectation_problems(doc["expect"])
    if problems:
        raise FixtureError(path, problems)
    try:
        now = parse_now(doc["now"]) if "now" in doc else None
    except ValueError as exc:  # matches the pattern but is no date: 2026-13-45T...
        raise FixtureError(path, [f"now: {exc}"]) from None
    calls = [_normal_call(c) for c in doc.get("calls") or []]
    return Scenario(
        name=path.stem,
        path=path,
        expect=doc["expect"],
        resource=doc.get("resource"),
        inputs=doc.get("inputs") or {},
        settings=doc.get("settings") or {},
        identity=doc.get("identity") or {},
        calls=calls,
        dry_run=bool(doc.get("dryRun", False)),
        now=now,
    )


MATCHERS = ("$contains", "$containsWhere")


def _is_matcher(value: Any) -> bool:
    """A non-empty dict whose keys are all `$`-prefixed."""
    return (
        isinstance(value, dict)
        and bool(value)
        and all(isinstance(k, str) and k.startswith("$") for k in value)
    )


def _has_dollar_key(value: dict) -> bool:
    return any(isinstance(k, str) and k.startswith("$") for k in value)


def _expectation_problems(expect: dict) -> list[str]:
    """Matcher mistakes in `expect.outputs` (the schema cannot say these), with paths."""
    problems: list[str] = []

    def walk(value: Any, where: str) -> None:
        if isinstance(value, dict):
            if _is_matcher(value):
                for name, argument in value.items():
                    if name not in MATCHERS:
                        problems.append(f"{where}: unknown matcher {name}")
                    elif name == "$contains":
                        if not isinstance(argument, list):
                            problems.append(f"{where}: $contains wants a list of items")
                        else:
                            for i, item in enumerate(argument):
                                walk(item, f"{where}[{i}]")
                    elif not isinstance(argument, dict) or not argument:
                        problems.append(f"{where}: $containsWhere wants an object")
                    else:
                        walk(argument, where)
            elif _has_dollar_key(value):
                problems.append(f"{where}: mixes $-matchers with plain keys")
            else:
                for key, sub in value.items():
                    walk(sub, f"{where}.{key}")
        elif isinstance(value, list):
            whole(value, where)

    def whole(value: Any, where: str) -> None:
        # a list is compared whole: a matcher in it would be taken for literal data
        if isinstance(value, dict):
            if _has_dollar_key(value):
                problems.append(f"{where}: a matcher inside a list is not evaluated; use $contains")
            else:
                for key, sub in value.items():
                    whole(sub, f"{where}.{key}")
        elif isinstance(value, list):
            for i, sub in enumerate(value):
                whole(sub, f"{where}[{i}]")

    if "outputs" in expect:
        walk(expect["outputs"], "expect.outputs")
    for name, spec in (expect.get("artifacts") or {}).items():
        lines = spec.get("lines") or {}
        if "min" in lines and "max" in lines and lines["min"] > lines["max"]:
            problems.append(
                f"expect.artifacts.{name}.lines: min {lines['min']} is above max {lines['max']}"
            )
    return problems


def _call_problems(calls: list[dict]) -> list[str]:
    """What the schema cannot say about proxy calls: exactly one of response and error."""
    problems = []
    for i, call in enumerate(calls):
        if call.get("type") != PROXY_CALL_TYPE:
            continue
        if "response" in call and "error" in call:
            problems.append(
                f"calls[{i}]: a {PROXY_CALL_TYPE} call has a response or an error (not both)"
            )
        elif "response" not in call and "error" not in call:
            problems.append(f"calls[{i}]: a {PROXY_CALL_TYPE} call needs a response or an error")
    return problems


def _normal_call(call: dict) -> dict:
    """A call with its defaults filled: a CLI call's rc/stdout/stderr, a proxy call's scheme
    and response fields."""
    if call.get("type") == PROXY_CALL_TYPE:
        out: dict[str, Any] = {
            "type": PROXY_CALL_TYPE,
            "request": {"scheme": "http", **call["request"]},
        }
        if "error" in call:
            out["error"] = dict(call["error"])
        else:
            out["response"] = {
                "headers": {},
                "body": "",
                "truncated": False,
                "location": None,
                **call["response"],
            }
        return out
    return {
        "argv": list(call["argv"]),
        "rc": 0,
        "stdout": "",
        "stderr": "",
        **{k: v for k, v in call.items() if k != "argv"},
    }


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
    dry_run: bool = False,
    now: datetime | None = None,
) -> str:
    """The YAML text of a scenario (the `--record` output). `now` is the reference time
    the run used, written as a quoted RFC 3339 string."""
    doc: dict[str, Any] = {}
    if resource is not None:
        doc["resource"] = resource
    for key, value in (("inputs", inputs), ("settings", settings), ("identity", identity)):
        if value:
            doc[key] = value
    if dry_run:
        doc["dryRun"] = True
    if now is not None:
        doc["now"] = format_now(now)
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
    if _is_matcher(expected):
        for name, argument in expected.items():
            _matcher(name, argument, got, where, problems)
        return
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


def _matches(expected: Any, got: Any) -> bool:
    problems: list[str] = []
    _subset(expected, got, "", problems)
    return not problems


def _matcher(name: str, argument: Any, got: Any, where: str, problems: list[str]) -> None:
    if not isinstance(got, list):
        problems.append(f"{where}: {name}: expected a list, got {got!r}")
    elif name == "$contains":
        for item in argument:
            if not any(_matches(item, element) for element in got):
                problems.append(f"{where}: $contains: no element matches {item!r}")
                return  # the first missing item is enough
    elif not any(isinstance(e, dict) and _matches(argument, e) for e in got):
        problems.append(f"{where}: $containsWhere: no element matches {argument!r}")


def _artifact_problems(
    expected: dict, texts: Mapping[str, str], declared: Collection[str]
) -> list[str]:
    problems: list[str] = []
    for name, spec in expected.items():
        where = f"artifacts.{name}"
        if name not in declared:
            known = ", ".join(sorted(declared)) or "none"
            problems.append(
                f"{where}: not an artifact output of this task (artifact outputs: {known})"
            )
            continue
        if name not in texts:
            problems.append(f"{where}: the task did not produce this artifact")
            continue
        text = texts[name]
        problems.extend(
            f"{where}: missing text {s!r}" for s in spec.get("contains", []) if s not in text
        )
        problems.extend(
            f"{where}: contains forbidden text {s!r}"
            for s in spec.get("notContains", [])
            if s in text
        )
        count = len(text.splitlines())
        lines = spec.get("lines") or {}
        if "min" in lines and count < lines["min"]:
            problems.append(f"{where}: {count} lines, expected at least {lines['min']}")
        if "max" in lines and count > lines["max"]:
            problems.append(f"{where}: {count} lines, expected at most {lines['max']}")
    return problems


def match_expect(
    envelope: dict,
    expect: dict,
    *,
    artifacts: Mapping[str, str] | None = None,
    artifact_outputs: Collection[str] | None = None,
) -> list[str]:
    """Why `envelope` does not meet `expect`: one message per mismatch, [] for a pass.

    `status` must be equal. `errorCodes`, when given, is compared with the envelope's
    codes as a sorted set. `outputs` is a subset match: every expected key of a dict must
    be present with an equal value, recursively through dicts; a list or a scalar must be
    equal as a whole (a bool is not a number). A dict whose keys are all `$`-prefixed is a
    matcher on a list instead: `{"$contains": [items]}` (each item subset-matches some
    element, any order, one element may serve two items) and `{"$containsWhere": {k: v}}`
    (some element subset-matches); several on one dict must all hold. Matchers work at any
    depth, including inside `$contains` items. The first missing item is the one named.

    `artifacts` (what the run produced: name -> text) and `artifact_outputs` (the task's
    artifact output names; default: those produced) are for `expect.artifacts`."""
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
    if expect.get("artifacts"):
        texts = artifacts or {}
        declared = artifact_outputs if artifact_outputs is not None else list(texts)
        problems.extend(_artifact_problems(expect["artifacts"], texts, declared))
    return problems
