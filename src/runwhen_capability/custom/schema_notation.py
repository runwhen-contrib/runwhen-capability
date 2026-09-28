"""The compact schema notation compiler: turns an output's compact type
string into a JSON Schema dict.

Grammar (informal):

    schema  := object ('[]')*  |  named ('[]')*
    object  := '{' (field (',' field)*)? '}'
    field   := NAME ':' schema '?'?          -- trailing '?' makes the field optional
    named   := scalar | enum
    scalar  := 'string' | 'integer' | 'number' | 'boolean' | 'datetime'
    enum    := 'enum' '(' VALUE ('|' VALUE)* ')'

Examples:

    "string"                                       -> {"type": "string"}
    "integer[]"                                     -> array of integer
    "enum(a|b)"                                      -> string, one of "a"/"b"
    "{ pod: string, restarts: integer, seen: datetime? }[]"
                                                      -> array of object

The escape hatch (a `schema:` value starting with "./", pointing at a JSON
Schema file in the bundle) is resolved by the caller (compiler.py), not here --
this module only ever sees a compact-notation string.
"""

from __future__ import annotations

import re

_SCALARS: dict[str, dict] = {
    "string": {"type": "string"},
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "datetime": {"type": "string", "format": "date-time"},
}

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Nesting (objects and `[]` lists together) deeper than this is refused: the
# parser is recursive, a schema string is untrusted input to validate(), and
# the compiled schema is later serialized and evaluated recursively too.
MAX_DEPTH = 32


class SchemaNotationError(ValueError):
    """A compact schema string could not be parsed (E_SCHEMA_NOTATION)."""


class _Parser:
    """A small hand-rolled recursive-descent parser -- the grammar above is
    tiny enough that a tokenizer would be more code, not less."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.end = len(text)
        self.depth = 0

    def parse(self) -> dict:
        self._skip_ws()
        node = self._parse_schema()
        self._skip_ws()
        if self.pos != self.end:
            raise SchemaNotationError(
                f"unexpected text at position {self.pos} in {self.text!r}: "
                f"{self.text[self.pos :]!r}"
            )
        return node

    # -- low-level helpers ------------------------------------------------

    def _skip_ws(self) -> None:
        while self.pos < self.end and self.text[self.pos].isspace():
            self.pos += 1

    def _peek(self) -> str:
        return self.text[self.pos] if self.pos < self.end else ""

    def _expect(self, ch: str) -> None:
        self._skip_ws()
        if self._peek() != ch:
            raise SchemaNotationError(f"expected {ch!r} at position {self.pos} in {self.text!r}")
        self.pos += 1

    def _match_ident(self) -> str:
        self._skip_ws()
        m = _IDENT_RE.match(self.text, self.pos)
        if not m:
            raise SchemaNotationError(f"expected a name at position {self.pos} in {self.text!r}")
        self.pos = m.end()
        return m.group(0)

    # -- grammar ------------------------------------------------------------

    def _parse_schema(self) -> dict:
        self._skip_ws()
        node = self._parse_object() if self._peek() == "{" else self._parse_named()
        self._skip_ws()
        levels = 0
        while self.text[self.pos : self.pos + 2] == "[]":
            levels += 1
            if self.depth + levels > MAX_DEPTH:
                raise SchemaNotationError(f"schema nested deeper than {MAX_DEPTH} levels")
            node = {"type": "array", "items": node}
            self.pos += 2
            self._skip_ws()
        return node

    def _parse_object(self) -> dict:
        self._expect("{")
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise SchemaNotationError(f"objects nested deeper than {MAX_DEPTH} levels")
        try:
            return self._parse_object_body()
        finally:
            self.depth -= 1

    def _parse_object_body(self) -> dict:
        properties: dict[str, dict] = {}
        required: list[str] = []
        self._skip_ws()
        if self._peek() == "}":
            self.pos += 1
            return {"type": "object", "properties": properties}
        while True:
            field_name = self._match_ident()
            if field_name in properties:
                raise SchemaNotationError(f"duplicate field {field_name!r} in {self.text!r}")
            self._expect(":")
            self._skip_ws()
            field_schema = self._parse_schema()
            self._skip_ws()
            optional = self._peek() == "?"
            if optional:
                self.pos += 1
            properties[field_name] = field_schema
            if not optional:
                required.append(field_name)
            self._skip_ws()
            if self._peek() == ",":
                self.pos += 1
                continue
            break
        self._expect("}")
        schema: dict = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema

    def _parse_named(self) -> dict:
        name = self._match_ident()
        if name == "enum":
            return self._parse_enum()
        try:
            return dict(_SCALARS[name])
        except KeyError:
            raise SchemaNotationError(
                f"unknown type {name!r} in {self.text!r} -- expected one of "
                f"{', '.join(sorted(_SCALARS))}, 'enum(...)' or an object"
            ) from None

    def _parse_enum(self) -> dict:
        self._expect("(")
        values: list[str] = []
        while True:
            self._skip_ws()
            start = self.pos
            while self.pos < self.end and self.text[self.pos] not in "|)":
                self.pos += 1
            value = self.text[start : self.pos].strip()
            if not value:
                raise SchemaNotationError(f"empty enum value in {self.text!r}")
            values.append(value)
            if self._peek() == "|":
                self.pos += 1
                continue
            break
        self._expect(")")
        return {"type": "string", "enum": values}


def compile_schema_notation(text: str) -> dict:
    """Compiles one compact schema string into a JSON Schema dict. Raises
    SchemaNotationError (E_SCHEMA_NOTATION) if `text` does not match the
    grammar above."""
    if not text or not text.strip():
        raise SchemaNotationError("empty schema")
    return _Parser(text).parse()
