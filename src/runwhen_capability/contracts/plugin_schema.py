"""Evaluating JSON Schemas that a plugin author wrote (S13).

A plugin's task inputs, settings, output schemas, facet schemas and permission schema are
the author's JSON, and a JSON Schema `pattern` is a regular expression the host runs.
Python's `re` backtracks: `^(a+)+$` against `"a" * 40 + "b"` runs for hours, in the
process that validates (the runner, papi, registry ingest), outside any deadline.

`validator_for_plugin_schema(schema)` is the one place such a schema is evaluated. It is
Draft 2020-12 with `format` checking, but `pattern`, `patternProperties` (and the two
keywords that read `patternProperties` to decide which properties are "extra" or
"unevaluated") and the `regex` format use RE2 (`google-re2`), which matches in time linear
in the input and has no lookaround or backreferences. Its registry refuses every `$ref`
that does not point inside the schema itself: nothing is fetched and no file is read (the
JSON Schema meta-schemas that jsonschema ships stay resolvable from memory).

`check_plugin_schema(schema)` is the authoring-time counterpart: the patterns RE2 cannot
compile and the `$ref`s that leave the schema, as messages. `plugin/validate.py` reports
them as E_SCHEMA_PATTERN; ingest and papi can call it on a compiled plugin's inlined schemas.

Our own contract schemas (`contracts.validate`) are trusted and keep the stock validator.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from typing import Any

import re2
from jsonschema import Draft202012Validator, FormatChecker, validators
from jsonschema.exceptions import ValidationError
from jsonschema.protocols import Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource

__all__ = ["check_plugin_schema", "validator_for_plugin_schema"]

#: Keywords that name another schema document; only same-document ("#...") ones are allowed.
_REF_KEYWORDS = ("$ref", "$dynamicRef")

# Keywords whose value maps names to subschemas (the names are data, not keywords).
_SCHEMA_MAPS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)
# Keywords whose value is one subschema, or a list of them.
_SUBSCHEMAS = frozenset(
    {
        "items",
        "prefixItems",
        "additionalItems",
        "additionalProperties",
        "unevaluatedItems",
        "unevaluatedProperties",
        "contains",
        "propertyNames",
        "not",
        "if",
        "then",
        "else",
        "allOf",
        "anyOf",
        "oneOf",
        "contentSchema",
    }
)


# -- RE2 ----------------------------------------------------------------------------


@functools.lru_cache(maxsize=1024)
def _compile(pattern: str) -> Any:
    return re2.compile(pattern)


def _search(pattern: str, text: str) -> bool:
    regex = _compile(pattern)
    try:
        return regex.search(text) is not None
    except UnicodeEncodeError:  # a lone surrogate cannot be UTF-8 encoded
        return regex.search(text.encode("utf-8", "replace").decode("utf-8")) is not None


def _pattern(validator, pattern, instance, schema):
    if validator.is_type(instance, "string") and not _search(pattern, instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


def _pattern_properties(validator, pattern_properties, instance, schema):
    if not validator.is_type(instance, "object"):
        return
    for pattern, subschema in pattern_properties.items():
        for key, value in instance.items():
            if _search(pattern, key):
                yield from validator.descend(value, subschema, path=key, schema_path=pattern)


def _is_valid(errors: Iterator) -> bool:
    return next(errors, None) is None


def _extras(keys: list) -> str:
    verb = "was" if len(keys) == 1 else "were"
    return ", ".join(repr(key) for key in keys) + f" {verb}"


def _additional_properties(validator, additional, instance, schema):
    if not validator.is_type(instance, "object"):
        return
    properties = schema.get("properties", {})
    patterns = list(schema.get("patternProperties", {}))
    extras = {
        key
        for key in instance
        if key not in properties and not any(_search(p, key) for p in patterns)
    }
    if validator.is_type(additional, "object"):
        for extra in extras:
            yield from validator.descend(instance[extra], additional, path=extra)
    elif not additional and extras:
        if "patternProperties" in schema:
            verb = "does" if len(extras) == 1 else "do"
            joined = ", ".join(repr(each) for each in sorted(extras))
            regexes = ", ".join(repr(each) for each in sorted(patterns))
            yield ValidationError(f"{joined} {verb} not match any of the regexes: {regexes}")
        else:
            yield ValidationError(
                "Additional properties are not allowed "
                f"({_extras(sorted(extras, key=str))} unexpected)"
            )


def _evaluated_property_keys(validator, instance, schema) -> list:
    """The stock `find_evaluated_property_keys_by_schema`, with RE2 for `patternProperties`."""
    if validator.is_type(schema, "boolean"):
        return []
    keys: list = []
    for ref_keyword in _REF_KEYWORDS:
        ref = schema.get(ref_keyword)
        if ref is not None:
            resolved = validator._resolver.lookup(ref)
            keys.extend(
                _evaluated_property_keys(
                    validator.evolve(schema=resolved.contents, _resolver=resolved.resolver),
                    instance,
                    resolved.contents,
                )
            )
    properties = schema.get("properties")
    if validator.is_type(properties, "object"):
        keys += properties.keys() & instance.keys()
    for keyword in ("additionalProperties", "unevaluatedProperties"):
        if (subschema := schema.get(keyword)) is None:
            continue
        keys += (
            key for key, value in instance.items() if _is_valid(validator.descend(value, subschema))
        )
    if "patternProperties" in schema:
        for key in instance:
            for pattern in schema["patternProperties"]:
                if _search(pattern, key):
                    keys.append(key)
    if "dependentSchemas" in schema:
        for key, subschema in schema["dependentSchemas"].items():
            if key in instance:
                keys += _evaluated_property_keys(validator, instance, subschema)
    for keyword in ("allOf", "oneOf", "anyOf"):
        for subschema in schema.get(keyword, []):
            if _is_valid(validator.descend(instance, subschema)):
                keys += _evaluated_property_keys(validator, instance, subschema)
    if "if" in schema:
        if validator.evolve(schema=schema["if"]).is_valid(instance):
            keys += _evaluated_property_keys(validator, instance, schema["if"])
            if "then" in schema:
                keys += _evaluated_property_keys(validator, instance, schema["then"])
        elif "else" in schema:
            keys += _evaluated_property_keys(validator, instance, schema["else"])
    return keys


def _unevaluated_properties(validator, unevaluated, instance, schema):
    if not validator.is_type(instance, "object"):
        return
    evaluated = _evaluated_property_keys(validator, instance, schema)
    unevaluated_keys = []
    for key in instance:
        if key not in evaluated:
            for _ in validator.descend(instance[key], unevaluated, path=key, schema_path=key):
                unevaluated_keys.append(key)  # noqa: PERF401
    if unevaluated_keys:
        if unevaluated is False:
            extras = sorted(unevaluated_keys, key=str)
            yield ValidationError(
                f"Unevaluated properties are not allowed ({_extras(extras)} unexpected)"
            )
        else:
            yield ValidationError(
                "Unevaluated properties are not valid under the given schema "
                f"({_extras(unevaluated_keys)} unevaluated and invalid)"
            )


def _is_regex(instance: object) -> bool:
    if not isinstance(instance, str):
        return True
    _compile(instance)
    return True


def _format_checker() -> FormatChecker:
    checker = FormatChecker([])
    checker.checkers = dict(Draft202012Validator.FORMAT_CHECKER.checkers)
    checker.checks("regex", raises=re2.error)(_is_regex)
    return checker


_FORMAT_CHECKER = _format_checker()

_PluginSchemaValidator = validators.extend(
    Draft202012Validator,
    {
        "pattern": _pattern,
        "patternProperties": _pattern_properties,
        "additionalProperties": _additional_properties,
        "unevaluatedProperties": _unevaluated_properties,
    },
    format_checker=_FORMAT_CHECKER,
)


def _refuse(uri: str):
    raise NoSuchResource(ref=uri)


def validator_for_plugin_schema(schema: dict) -> Validator:
    """A Draft 2020-12 validator for a plugin-authored `schema`: `pattern`,
    `patternProperties` and the `regex` format are evaluated with RE2, and a `$ref` that
    leaves the schema fails (it is never fetched). A pattern RE2 cannot compile, or a
    refused `$ref`, raises from `iter_errors` / `is_valid`; run `check_plugin_schema`
    first to find them in advance."""
    return _PluginSchemaValidator(
        schema,
        registry=Registry(retrieve=_refuse),
        format_checker=_FORMAT_CHECKER,
    )


# -- authoring-time checks --------------------------------------------------------------


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def iter_schema_problems(schema: Any) -> Iterator[tuple[str, str, str]]:
    """(JSON pointer into `schema`, kind, message) for each pattern RE2 cannot compile
    (kind `pattern`) and each `$ref` that leaves the schema (kind `ref`). Walks only where
    a subschema can appear, so a property called `pattern` and data under `enum`, `const`
    or `default` are never mistaken for keywords. Iterative: any depth is safe."""
    stack: list[tuple[Any, str]] = [(schema, "")]
    while stack:
        node, pointer = stack.pop()
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            here = f"{pointer}/{_escape(str(key))}"
            if key == "pattern":
                problem = _pattern_problem(value)
                if problem:
                    yield here, "pattern", problem
            elif key in _REF_KEYWORDS:
                if not (isinstance(value, str) and value.startswith("#")):
                    yield (
                        here,
                        "ref",
                        f"{key} {value!r} is not a local reference (it must start with '#')",
                    )
            if key == "patternProperties" and isinstance(value, dict):
                for regex in value:
                    problem = _pattern_problem(regex)
                    if problem:
                        yield f"{here}/{_escape(str(regex))}", "pattern", problem
            if key in _SCHEMA_MAPS and isinstance(value, dict):
                stack.extend((sub, f"{here}/{_escape(str(name))}") for name, sub in value.items())
            elif key in _SUBSCHEMAS:
                if isinstance(value, list):
                    stack.extend((sub, f"{here}/{i}") for i, sub in enumerate(value))
                else:
                    stack.append((value, here))


def _pattern_problem(pattern: Any) -> str | None:
    if not isinstance(pattern, str):
        return f"pattern must be a string, got {type(pattern).__name__}"
    try:
        _compile(pattern)
    except re2.error as exc:
        reason = exc.args[0] if exc.args else exc
        if isinstance(reason, bytes):
            reason = reason.decode("utf-8", "replace")
        return (
            f"pattern {pattern!r} is not valid RE2 ({reason}); "
            "lookaround and backreferences are not supported"
        )
    return None


def check_plugin_schema(schema: Any) -> list[str]:
    """What is wrong with a plugin-authored `schema` before it is evaluated: each pattern
    (`pattern`, `patternProperties` key) RE2 cannot compile and each `$ref` that does not
    point inside the schema, as `"<pointer>: <message>"`. [] when there is none."""
    return [
        f"{pointer or '/'}: {message}" for pointer, _kind, message in iter_schema_problems(schema)
    ]
