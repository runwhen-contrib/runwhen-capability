"""The reference loader for plugin files: YAML 1.2 core schema on top of PyYAML.

PyYAML implements YAML 1.1, where `no`, `on` and `yes` are booleans, `017` is
octal, `1:20` is a base-60 integer and an unquoted RFC 3339 string becomes a
datetime. Plugin files are YAML 1.2 (CONTRACTS.md), so `aliases: [no]` is the
string "no" and timestamps stay strings for `format: date-time` to check.
Every lane that parses plugin files uses this loader so they agree.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

__all__ = ["load_yaml", "split_frontmatter"]

_BOOL = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
_INT = re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$")
_FLOAT = re.compile(
    r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
    r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
)
_REPLACED = {
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:timestamp",
}


class _Loader(yaml.SafeLoader):
    pass


_Loader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag not in _REPLACED]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver("tag:yaml.org,2002:bool", _BOOL, list("tTfF"))
_Loader.add_implicit_resolver("tag:yaml.org,2002:int", _INT, list("-+0123456789"))
_Loader.add_implicit_resolver("tag:yaml.org,2002:float", _FLOAT, list("-+0123456789."))


def _construct_bool(loader: yaml.SafeLoader, node: yaml.Node) -> bool:
    return loader.construct_scalar(node).lower() == "true"


def _construct_int(loader: yaml.SafeLoader, node: yaml.Node) -> int:
    value = loader.construct_scalar(node)
    if value.startswith(("0o", "0x")):
        return int(value, 0)
    return int(value, 10)


def _construct_float(loader: yaml.SafeLoader, node: yaml.Node) -> float:
    value = loader.construct_scalar(node).lower()
    if value.endswith(".inf"):
        return float("-inf") if value.startswith("-") else float("inf")
    if value == ".nan":
        return float("nan")
    return float(value)


_Loader.add_constructor("tag:yaml.org,2002:bool", _construct_bool)
_Loader.add_constructor("tag:yaml.org,2002:int", _construct_int)
_Loader.add_constructor("tag:yaml.org,2002:float", _construct_float)


def load_yaml(text: str) -> Any:
    """Parse one YAML document with YAML 1.2 core-schema scalars."""
    return yaml.load(text, Loader=_Loader)  # noqa: S506 - a SafeLoader subclass


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a SKILL.md / rule / knowledge file into (frontmatter, body).

    The file must start with a `---` line; the frontmatter ends at the next
    `---` line. Raises ValueError when there is no frontmatter mapping.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise ValueError("no frontmatter: the file must start with a '---' line")
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == "---":
            meta = load_yaml("".join(lines[1:i]))
            if not isinstance(meta, dict):
                raise ValueError("frontmatter is not a mapping")
            return meta, "".join(lines[i + 1 :])
    raise ValueError("frontmatter is not closed by a '---' line")
