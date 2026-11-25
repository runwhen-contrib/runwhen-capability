"""npm-style semver ranges for plugin dependencies (`requires` / `extends`).

    satisfies("0.3.2", "^0.3")                  # True: >=0.3.0 <0.4.0
    satisfies("1.4.0", ">=1.2 <2 || ^3")        # True
    satisfies("1.0.0-beta.1", "^1.0.0")         # False: prereleases match only a range
                                                # that names a prerelease of the same
                                                # major.minor.patch (the npm rule)

Supported: `^`, `~`, the comparators `>= > <= < =` joined by spaces (AND) and `||`
(OR), `x` / `X` / `*` wildcards, and partial versions (`^0.2` is `^0.2.0`, `1.2` is
`1.2.x`). Hyphen ranges are not supported. Build metadata (`+...`) is ignored.
`satisfies` raises `ValueError` for a version or a range it cannot parse.
"""

from __future__ import annotations

import functools
import re
from typing import NamedTuple

_NUM = r"0|[1-9][0-9]*"
_VERSION = re.compile(
    rf"^({_NUM})\.({_NUM})\.({_NUM})(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_PARTIAL = re.compile(
    r"^v?([0-9]+|[xX*])(?:\.([0-9]+|[xX*]))?(?:\.([0-9]+|[xX*]))?"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_OPERATOR = re.compile(r"^(>=|<=|>|<|=|\^|~)?(.*)$")
_GAP = re.compile(r"(>=|<=|>|<|=|\^|~)\s+")


class Version(NamedTuple):
    major: int
    minor: int
    patch: int
    pre: tuple[str, ...] = ()


def parse_version(text: str) -> Version:
    """A strict `major.minor.patch[-prerelease][+build]`; ValueError otherwise."""
    match = _VERSION.match(text.strip()) if isinstance(text, str) else None
    if match is None:
        raise ValueError(f"not a semantic version: {text!r}")
    major, minor, patch, pre = match.groups()
    return Version(int(major), int(minor), int(patch), tuple(pre.split(".")) if pre else ())


def _compare_pre(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    if not a or not b:  # a release outranks any of its prereleases
        return (not a) - (not b)
    for left, right in zip(a, b, strict=False):
        if left == right:
            continue
        left_num, right_num = left.isdigit(), right.isdigit()
        if left_num and right_num:
            return -1 if int(left) < int(right) else 1
        if left_num != right_num:
            return -1 if left_num else 1  # numeric identifiers sort below alphanumeric
        return -1 if left < right else 1
    return (len(a) > len(b)) - (len(a) < len(b))


def compare(a: Version, b: Version) -> int:
    """-1, 0 or 1 by semver precedence."""
    if a[:3] != b[:3]:
        return -1 if a[:3] < b[:3] else 1
    return _compare_pre(a.pre, b.pre)


def version_key(text: str):
    """A sort key for `sorted(..., key=version_key)` over version strings."""
    return functools.cmp_to_key(compare)(parse_version(text))


def highest(versions: list[str]) -> str | None:
    """The greatest of `versions` by semver precedence, or None."""
    return max(versions, key=version_key) if versions else None


# -- ranges ---------------------------------------------------------------------------

_Comparator = tuple[str, Version]  # (">=" | ">" | "<=" | "<" | "=", version)
_ANY: list[_Comparator] = [(">=", Version(0, 0, 0))]
_NONE: list[_Comparator] = [("<", Version(0, 0, 0, ("0",)))]


def _upper(major: int, minor: int, patch: int) -> _Comparator:
    """Exclusive bound that also excludes the prereleases of `major.minor.patch`."""
    return ("<", Version(major, minor, patch, ("0",)))


def _partial(text: str) -> tuple[int | None, int | None, int | None, tuple[str, ...]]:
    match = _PARTIAL.match(text)
    if match is None:
        raise ValueError(f"cannot parse {text!r} in a version range")
    parts: list[int | None] = []
    wild = False
    for raw in match.groups()[:3]:
        if raw is None or raw in "xX*" or wild:
            wild = True
            parts.append(None)
        else:
            parts.append(int(raw))
    pre = tuple(match.group(4).split(".")) if match.group(4) else ()
    if pre and None in parts:
        raise ValueError(f"cannot parse {text!r} in a version range")
    major, minor, patch = parts
    return major, minor, patch, pre


def _comparators(token: str) -> list[_Comparator]:
    op, rest = _OPERATOR.match(token).groups()  # type: ignore[union-attr]
    op = op or "="
    major, minor, patch, pre = _partial(rest)
    if major is None:
        return _NONE if op in (">", "<") else _ANY
    low = Version(major, minor or 0, patch or 0, pre)
    if op == "=":
        if minor is None:
            return [(">=", low), _upper(major + 1, 0, 0)]
        if patch is None:
            return [(">=", low), _upper(major, minor + 1, 0)]
        return [("=", low)]
    if op == "^":
        if major > 0 or minor is None:
            return [(">=", low), _upper(major + 1, 0, 0)]
        if minor > 0 or patch is None:
            return [(">=", low), _upper(0, minor + 1, 0)]
        return [(">=", low), _upper(0, 0, (patch or 0) + 1)]
    if op == "~":
        if minor is None:
            return [(">=", low), _upper(major + 1, 0, 0)]
        return [(">=", low), _upper(major, minor + 1, 0)]
    if op == ">=":
        return [(">=", low)]
    if op == ">":
        if minor is None:
            return [(">=", Version(major + 1, 0, 0))]
        if patch is None:
            return [(">=", Version(major, minor + 1, 0))]
        return [(">", low)]
    if op == "<=":
        if minor is None:
            return [_upper(major + 1, 0, 0)]
        if patch is None:
            return [_upper(major, minor + 1, 0)]
        return [("<=", low)]
    # "<"
    if minor is None or patch is None:
        return [("<", Version(major, minor or 0, patch or 0, ("0",)))]
    return [("<", low)]


def _holds(version: Version, comparator: _Comparator) -> bool:
    op, bound = comparator
    order = compare(version, bound)
    return {">=": order >= 0, ">": order > 0, "<=": order <= 0, "<": order < 0, "=": order == 0}[op]


def _set_matches(version: Version, comparators: list[_Comparator]) -> bool:
    if not all(_holds(version, c) for c in comparators):
        return False
    if version.pre:  # only a range that names a prerelease of this very x.y.z admits one
        return any(bound.pre and bound[:3] == version[:3] for _op, bound in comparators)
    return True


def satisfies(version: str, range_: str) -> bool:
    """Whether `version` is within the npm-style `range_` (ValueError if either does
    not parse)."""
    parsed = parse_version(version)
    if not isinstance(range_, str):
        raise ValueError(f"not a version range: {range_!r}")
    alternatives: list[list[_Comparator]] = []  # parse them all: a bad range is always an error
    for alternative in range_.split("||"):
        comparators: list[_Comparator] = []
        for token in _GAP.sub(r"\1", alternative.strip()).split():
            comparators.extend(_comparators(token))
        alternatives.append(comparators or list(_ANY))
    return any(_set_matches(parsed, comparators) for comparators in alternatives)
