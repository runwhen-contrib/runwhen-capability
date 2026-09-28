"""JSON Schema features a custom capability may not use.

Regex keywords (`pattern`, `patternProperties`) are refused in custom task
schemas: the host evaluates output schemas in its own process, where a
regex that backtracks catastrophically on a crafted value cannot be
interrupted by any deadline and would wedge the executor for every later
request. validate() reports them as E_SCHEMA_FEATURE, and the bundle host
refuses to evaluate a schema that still contains one.
"""

from __future__ import annotations

from typing import Any

REGEX_KEYWORDS = ("pattern", "patternProperties")

REGEX_HINT = (
    "regex patterns aren't supported in custom task schemas; validate the format in the task code"
)

# Keywords whose value maps names to subschemas: the names are data, so a
# property that happens to be called "pattern" is not the keyword.
_SCHEMA_MAPS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas", "dependencies"}
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


def regex_keyword_paths(schema: Any) -> list[str]:
    """JSON Pointers to every `pattern`/`patternProperties` keyword in
    `schema` that a validator would evaluate -- found wherever a subschema
    can appear, and never mistaking a property *named* "pattern" (or data
    in `enum`/`const`/`default`) for the keyword. Iterative, so any depth
    is safe to walk."""
    found: list[str] = []
    stack: list[tuple[Any, str]] = [(schema, "")]
    while stack:
        node, pointer = stack.pop()
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            here = f"{pointer}/{_escape(str(key))}"
            if key in REGEX_KEYWORDS:
                found.append(here)
            if key in _SCHEMA_MAPS and isinstance(value, dict):
                stack.extend((sub, f"{here}/{_escape(str(name))}") for name, sub in value.items())
            elif key in _SUBSCHEMAS:
                if isinstance(value, list):
                    stack.extend((sub, f"{here}/{i}") for i, sub in enumerate(value))
                else:
                    stack.append((value, here))
    return sorted(found)


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")
