"""A YAML loader that tags every mapping/sequence it builds with the source
line it started on, so validate.py can attribute a Diagnostic to a `line`
without a second parse pass.

`_LineDict`/`_LineList` are ordinary dict/list subclasses -- `isinstance(x, dict)`
still holds, pydantic's `model_validate` accepts them like any mapping, and
`compile_manifest` can iterate/index them exactly like plain YAML output. The
line number rides as an attribute (`.line`), never as a dict key, so it can
never leak into a compiled schema or an appliesTo `where` clause.
"""

from __future__ import annotations

from typing import Any

import yaml


class _LineDict(dict):
    __slots__ = ("line",)

    def __init__(self, data: dict, line: int) -> None:
        super().__init__(data)
        self.line = line


class _LineList(list):
    __slots__ = ("line",)

    def __init__(self, data: list, line: int) -> None:
        super().__init__(data)
        self.line = line


class _LineLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> _LineDict:
    return _LineDict(loader.construct_mapping(node, deep=True), node.start_mark.line + 1)


def _construct_sequence(loader: yaml.SafeLoader, node: yaml.SequenceNode) -> _LineList:
    return _LineList(loader.construct_sequence(node, deep=True), node.start_mark.line + 1)


_LineLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
_LineLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_SEQUENCE_TAG, _construct_sequence)


def load_with_lines(text: str) -> Any:
    """yaml.safe_load, except every mapping/sequence node also carries
    `.line` (1-indexed, the line its opening brace/dash is on)."""
    return yaml.load(text, Loader=_LineLoader)


def line_of(node: Any) -> int | None:
    """`node.line` if `node` came from load_with_lines() and is a mapping or
    sequence; None for a scalar or a plain (untagged) value."""
    return getattr(node, "line", None)
