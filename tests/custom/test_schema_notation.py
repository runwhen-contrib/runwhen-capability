"""Golden cases for the compact schema notation compiler."""

from __future__ import annotations

import pytest

from runwhen_capability.custom.schema_notation import SchemaNotationError, compile_schema_notation


@pytest.mark.parametrize(
    "text, expected",
    [
        ("string", {"type": "string"}),
        ("integer", {"type": "integer"}),
        ("number", {"type": "number"}),
        ("boolean", {"type": "boolean"}),
        ("datetime", {"type": "string", "format": "date-time"}),
        ("string[]", {"type": "array", "items": {"type": "string"}}),
        (
            "integer[][]",
            {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
        ),
        ("enum(a|b)", {"type": "string", "enum": ["a", "b"]}),
        ("enum(a|b|c)", {"type": "string", "enum": ["a", "b", "c"]}),
        ("{}", {"type": "object", "properties": {}}),
        (
            "{ checked: integer, crashLooping: integer }",
            {
                "type": "object",
                "properties": {"checked": {"type": "integer"}, "crashLooping": {"type": "integer"}},
                "required": ["checked", "crashLooping"],
            },
        ),
        (
            "{ pod: string, restarts: integer, lastRestart: datetime? }[]",
            {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "pod": {"type": "string"},
                        "restarts": {"type": "integer"},
                        "lastRestart": {"type": "string", "format": "date-time"},
                    },
                    "required": ["pod", "restarts"],
                },
            },
        ),
        (
            "{ tags: string[], status: enum(open|closed)? }",
            {
                "type": "object",
                "properties": {
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "status": {"type": "string", "enum": ["open", "closed"]},
                },
                "required": ["tags"],
            },
        ),
        (
            "{ inner: { a: integer, b: string? } }",
            {
                "type": "object",
                "properties": {
                    "inner": {
                        "type": "object",
                        "properties": {"a": {"type": "integer"}, "b": {"type": "string"}},
                        "required": ["a"],
                    }
                },
                "required": ["inner"],
            },
        ),
    ],
)
def test_golden_cases(text, expected):
    assert compile_schema_notation(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "foo",
        "{ a: string",
        "{ a string }",
        "{ a: nope }",
        "enum()",
        "enum(a|)",
        "string extra",
        "{ a: string, a: integer }",
    ],
)
def test_malformed_notation_raises(text):
    with pytest.raises(SchemaNotationError):
        compile_schema_notation(text)
