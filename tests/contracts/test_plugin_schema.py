"""contracts.plugin_schema: plugin-authored JSON Schemas are evaluated with RE2 (S13)."""

from __future__ import annotations

import json
import time

import pytest
from jsonschema.exceptions import _WrappedReferencingError

from runwhen_capability import contracts
from runwhen_capability.contracts import check_plugin_schema, validator_for_plugin_schema
from runwhen_capability.contracts.plugin_schema import iter_schema_problems

EVIL = {"type": "string", "pattern": "^(a+)+$"}
EVIL_VALUE = "a" * 40 + "b"


def errors(schema, instance) -> list[str]:
    return [e.message for e in validator_for_plugin_schema(schema).iter_errors(instance)]


def test_both_functions_are_reexported_from_contracts():
    assert contracts.validator_for_plugin_schema is validator_for_plugin_schema
    assert contracts.check_plugin_schema is check_plugin_schema
    assert {"check_plugin_schema", "validator_for_plugin_schema"} <= set(contracts.__all__)


def test_a_catastrophic_pattern_validates_in_well_under_a_second():
    start = time.perf_counter()
    problems = errors(EVIL, EVIL_VALUE)
    assert time.perf_counter() - start < 1
    assert len(problems) == 1 and "does not match" in problems[0]
    assert errors(EVIL, "a" * 40) == []


def test_a_catastrophic_pattern_inside_every_pattern_consuming_keyword_is_fast():
    cases = [
        ({"patternProperties": {"^(a+)+$": {"type": "integer"}}}, {EVIL_VALUE: 1}),
        (
            {"patternProperties": {"^(a+)+$": {}}, "additionalProperties": False},
            {EVIL_VALUE: 1},
        ),
        (
            {"patternProperties": {"^(a+)+$": {}}, "unevaluatedProperties": False},
            {EVIL_VALUE: 1},
        ),
        ({"propertyNames": {"pattern": "^(a+)+$"}}, {EVIL_VALUE: 1}),
        ({"type": "string", "format": "regex"}, "^(a+)+$"),
    ]
    for schema, instance in cases:
        start = time.perf_counter()
        errors(schema, instance)
        assert time.perf_counter() - start < 1, schema


def test_pattern_is_an_unanchored_search():
    assert errors({"type": "string", "pattern": "b+"}, "abbc") == []
    assert errors({"type": "string", "pattern": "^b+"}, "abbc") != []
    assert errors({"type": "integer", "pattern": "^x$"}, 5) == []  # not a string: ignored


def test_re2_only_syntax_works():
    assert errors({"type": "string", "pattern": r"^\p{L}+$"}, "héllo") == []
    assert errors({"type": "string", "pattern": r"^(?i)abc$"}, "ABC") == []


def test_patternproperties_apply_to_matching_keys_only():
    schema = {"type": "object", "patternProperties": {"^x-": {"type": "integer"}}}
    assert errors(schema, {"x-a": 1, "y": "free"}) == []
    assert errors(schema, {"x-a": "no"}) == ["'no' is not of type 'integer'"]


def test_additional_and_unevaluated_properties_see_patternproperties():
    base = {"type": "object", "patternProperties": {"^x-": {}}, "properties": {"id": {}}}
    closed = {**base, "additionalProperties": False}
    assert errors(closed, {"id": 1, "x-a": 1}) == []
    assert errors(closed, {"id": 1, "x-a": 1, "other": 1}) == [
        "'other' does not match any of the regexes: '^x-'"
    ]
    plain = {"type": "object", "properties": {"id": {}}, "additionalProperties": False}
    assert errors(plain, {"id": 1, "z": 1}) == [
        "Additional properties are not allowed ('z' was unexpected)"
    ]
    typed = {**base, "additionalProperties": {"type": "integer"}}
    assert errors(typed, {"other": "s"}) == ["'s' is not of type 'integer'"]
    open_ = {**base, "unevaluatedProperties": False}
    assert errors(open_, {"id": 1, "x-a": 1}) == []
    assert errors(open_, {"id": 1, "x-a": 1, "other": 1}) == [
        "Unevaluated properties are not allowed ('other' was unexpected)"
    ]
    via_allof = {
        "allOf": [{"patternProperties": {"^x-": {}}}],
        "unevaluatedProperties": False,
    }
    assert errors(via_allof, {"x-a": 1}) == []
    assert errors(via_allof, {"y": 1}) != []


def test_the_regex_format_is_checked_with_re2():
    schema = {"type": "string", "format": "regex"}
    assert errors(schema, r"^\p{L}+$") == []
    assert errors(schema, "(?=a)") == ["'(?=a)' is not a 'regex'"]
    assert errors(schema, "(a)\\1") != []


def test_other_formats_are_still_checked():
    assert errors({"type": "string", "format": "date-time"}, "not a time") != []
    assert errors({"type": "string", "format": "date-time"}, "2026-11-03T09:14:02Z") == []


def test_a_local_ref_resolves_and_a_remote_ref_is_refused():
    local = {"$defs": {"s": {"type": "string"}}, "$ref": "#/$defs/s"}
    assert errors(local, 5) == ["5 is not of type 'string'"]
    for remote in (
        "https://example.com/schema.json",
        "file:///etc/passwd",
        "other.json",
    ):
        with pytest.raises(_WrappedReferencingError):
            errors({"$ref": remote}, 1)


def test_a_remote_ref_is_never_fetched(monkeypatch):
    import urllib.request

    def refuse(*args, **kwargs):
        raise AssertionError("a network call was made")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(_WrappedReferencingError):
        errors({"properties": {"a": {"$ref": "http://127.0.0.1:9/x.json"}}}, {"a": 1})


def test_a_pattern_re2_cannot_compile_raises_when_evaluated():
    with pytest.raises(Exception, match="invalid perl operator"):
        errors({"type": "string", "pattern": "(?=a)"}, "a")


# -- check_plugin_schema ---------------------------------------------------------------------


def test_a_lookahead_pattern_is_a_problem():
    problems = check_plugin_schema({"type": "string", "pattern": "(?=a)"})
    assert len(problems) == 1
    assert problems[0].startswith("/pattern: ") and "(?=a)" in problems[0]


def test_lookbehind_and_backreferences_are_problems():
    for pattern in ("(?<=a)b", "(?!a)b", r"(a)\1", "(?<n>a)\\k<n>"):
        assert check_plugin_schema({"pattern": pattern}), pattern


def test_every_pattern_and_patternproperties_key_is_found_with_a_pointer():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "pattern": "(?=x)"},
            "pattern": {"type": "string"},  # a property named pattern is not the keyword
        },
        "patternProperties": {"(?!y)": {"type": "string"}, "^ok$": {"pattern": "(?<=z)"}},
        "items": {"anyOf": [{"pattern": "(?=q)"}]},
        "$defs": {"d": {"pattern": "(?=d)"}},
    }
    pointers = sorted(p.split(": ", 1)[0] for p in check_plugin_schema(schema))
    assert pointers == [
        "/$defs/d/pattern",
        "/items/anyOf/0/pattern",
        "/patternProperties/(?!y)",
        "/patternProperties/^ok$/pattern",
        "/properties/a/pattern",
    ]


def test_pointer_tokens_are_escaped():
    problems = check_plugin_schema({"patternProperties": {"(?=a/b~c)": {}}})
    assert problems[0].startswith("/patternProperties/(?=a~1b~0c): ")


def test_data_is_not_mistaken_for_keywords():
    schema = {
        "type": "object",
        "default": {"pattern": "(?=a)"},
        "examples": [{"$ref": "https://example.com/x"}],
        "enum": [{"pattern": "(?=a)"}],
        "const": {"pattern": "(?=a)"},
    }
    assert check_plugin_schema(schema) == []


def test_a_remote_ref_is_a_problem_and_a_local_one_is_not():
    assert check_plugin_schema({"$ref": "#/$defs/a", "$defs": {"a": {}}}) == []
    assert check_plugin_schema({"$ref": "#"}) == []
    remote = check_plugin_schema({"properties": {"a": {"$ref": "https://example.com/a.json"}}})
    assert len(remote) == 1 and remote[0].startswith("/properties/a/$ref: ")
    assert check_plugin_schema({"$dynamicRef": "https://example.com/a"}) != []


def test_a_clean_schema_and_non_schemas_have_no_problems_or_a_clear_one():
    assert check_plugin_schema({"type": "string", "pattern": "^[a-z]+(-[a-z0-9]+)*$"}) == []
    assert check_plugin_schema(True) == []
    assert check_plugin_schema({"pattern": 5}) != []


def test_a_deeply_nested_schema_is_walked_without_recursion_errors():
    schema: dict = {"pattern": "(?=a)"}
    for _ in range(5000):
        schema = {"not": schema}
    assert len(check_plugin_schema(schema)) == 1


def test_the_contract_schemas_patterns_are_all_re2_compilable():
    # Not their `$ref`s: those point at sibling schemas, which is what the registry of
    # contracts.validate is for.
    for name in contracts.SCHEMA_NAMES:
        found = [p for p in iter_schema_problems(contracts.schema(name)) if p[1] == "pattern"]
        assert found == [], name


def test_a_lone_surrogate_pattern_is_reported_not_raised():
    # JSON allows "\ud800"; RE2 compiles UTF-8 and cannot encode it.
    schema = json.loads('{"type": "string", "pattern": "\\ud800"}')
    problems = check_plugin_schema(schema)
    assert problems and "not valid UTF-8" in problems[0]


def test_a_lone_surrogate_regex_format_is_a_format_error():
    validator = validator_for_plugin_schema({"type": "string", "format": "regex"})
    errors = list(validator.iter_errors(json.loads('"\\ud800"')))
    assert errors and errors[0].validator == "format"
