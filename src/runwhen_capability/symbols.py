"""Language-aware symbol regex for the query task's `defs`/`refs` ops. No
tree-sitter -- per-language regex patterns are the whole mechanism.

`definition_pattern`/`reference_pattern` return plain regex-syntax strings,
in the same dialect grep_tree already compiles patterns with (Python's
`re`, not ripgrep/PCRE -- see repo_fs.py's grep_tree docstring): the
`defs` op is `grep_tree(..., pattern=definition_pattern(symbol), context=2)`
and `refs` is `grep_tree(..., pattern=reference_pattern(symbol))` with
matches on a definition line dropped via `is_definition_line`.

A caller-supplied `symbol` is untrusted regex metacharacters, not a pattern
fragment, so every pattern here `re.escape`s it before embedding it.

Identifier boundary: plain `\\b` treats `$` as a non-word character, so it
never fires directly against one -- in `"const $scope = x"`, both the space
and the `$` are non-word, so there is no word/non-word transition for `\\b`
to anchor on immediately before `$`, even though `$scope` is a normal JS/TS
identifier (JS identifiers may start with `$`/`_`). Every boundary placed
directly against the symbol itself below uses a `[\\w$]`-aware lookaround
instead of `\\b`; boundaries against a fixed keyword (`def`, `class`,
`func`, ...) keep plain `\\b`, since keywords never contain `$`.
"""

from __future__ import annotations

import re

# Stands in for \b wherever the boundary sits directly against the symbol
# (see module docstring) -- \w alone under-covers a JS/TS `$`-prefixed
# identifier's edges.
_IDENT_CHARS = r"\w$"


def _left_boundary(symbol: str) -> str:
    return rf"(?<![{_IDENT_CHARS}]){re.escape(symbol)}"


def _right_boundary() -> str:
    return rf"(?![{_IDENT_CHARS}])"


def definition_pattern(symbol: str) -> str:
    """A single alternation matching any of the supported language-specific
    definition forms for `symbol`: Python `def`/`class`/`async def`, JS
    `function`/`const`/`let`/`var`, TS `interface`/`type`/`enum`, Go `func`
    (with an optional method receiver) and `type X struct`/`interface`.

    Two extensions beyond those literal forms: a TS-typed variable
    (`export const foo: Foo = ...` -- `:` as well as `=` after the name)
    and a Go generic func (`func Map[T any](...)` -- `[` as well as `(`
    after the name)."""
    x = re.escape(symbol)
    right = _right_boundary()
    alternatives = [
        rf"\bdef\s+{x}{right}",  # Python def
        rf"\bclass\s+{x}{right}",  # Python/JS class
        rf"\basync\s+def\s+{x}{right}",  # Python async def
        rf"\bfunction\s+{x}{right}",  # JS function
        rf"\b(?:const|let|var)\s+{x}\s*[:=]",  # JS/TS variable, optionally typed (`: T =`)
        rf"\b(?:interface|type|enum)\s+{x}{right}",  # TS
        rf"\bfunc\s+(?:\(.*\)\s+)?{x}[\[(]",  # Go func / method, optionally generic (`X[T any](`)
        rf"\btype\s+{x}\s+(?:struct|interface){right}",  # Go type declaration
    ]
    return "|".join(f"(?:{alt})" for alt in alternatives)


def reference_pattern(symbol: str) -> str:
    """`symbol` as a whole identifier anywhere on a line -- `\\bX\\b`,
    `$`-aware (see module docstring)."""
    return f"{_left_boundary(symbol)}{_right_boundary()}"


def is_definition_line(line: str, symbol: str) -> bool:
    """Whether `line` matches `symbol`'s definition_pattern -- backs the
    `refs` op's exclusion of definition lines from its matches."""
    return re.search(definition_pattern(symbol), line) is not None
