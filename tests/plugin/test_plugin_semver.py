"""runwhen_capability.plugin.semver: npm-style ranges."""

from __future__ import annotations

import pytest

from runwhen_capability.plugin.semver import (
    compare,
    highest,
    parse_version,
    satisfies,
)

# (version, range, expected)
CASES = [
    # caret, including 0.x semantics
    ("1.2.3", "^1.2.3", True),
    ("1.9.0", "^1.2.3", True),
    ("2.0.0", "^1.2.3", False),
    ("1.2.2", "^1.2.3", False),
    ("0.3.7", "^0.3", True),
    ("0.4.0", "^0.3", False),
    ("0.2.9", "^0.3", False),
    ("0.0.3", "^0.0.3", True),
    ("0.0.4", "^0.0.3", False),
    ("0.2.5", "^0.2", True),  # partial: ^0.2 is ^0.2.0
    ("0.3.0", "^0.2", False),
    ("0.9.0", "^0", True),
    ("1.0.0", "^0", False),
    # tilde
    ("1.2.9", "~1.2.3", True),
    ("1.3.0", "~1.2.3", False),
    ("1.2.2", "~1.2.3", False),
    ("1.2.0", "~1.2", True),
    ("1.3.0", "~1.2", False),
    ("1.9.9", "~1", True),
    ("2.0.0", "~1", False),
    # comparators, AND and OR
    ("1.2.3", ">=1.2.3", True),
    ("1.2.3", ">1.2.3", False),
    ("1.2.3", ">= 1.2.3", True),  # a space after the operator
    ("1.3.0", ">1.2", True),  # >1.2 is >=1.3.0
    ("1.2.9", ">1.2", False),
    ("1.2.9", "<=1.2", True),
    ("1.3.0", "<=1.2", False),
    ("1.2.0", "<1.2", False),
    ("1.1.9", "<1.2", True),
    ("1.5.0", ">=1.2 <2", True),
    ("2.0.0", ">=1.2 <2", False),
    ("1.5.0", ">=1.2 <2 || ^3", True),
    ("2.5.0", ">=1.2 <2 || ^3", False),
    ("3.1.0", ">=1.2 <2 || ^3", True),
    ("1.2.3", "=1.2.3", True),
    ("1.2.3", "1.2.3", True),
    ("1.2.4", "1.2.3", False),
    # wildcards and partials
    ("9.9.9", "*", True),
    ("9.9.9", "", True),
    ("1.2.3", "1.x", True),
    ("2.0.0", "1.x", False),
    ("1.2.9", "1.2.x", True),
    ("1.3.0", "1.2", False),
    ("1.2.3", ">=1.x", True),
    # prereleases
    ("1.0.0-beta.1", "^1.0.0", False),
    ("1.0.0-beta.2", "^1.0.0-beta.1", True),
    ("1.0.0-beta.1", "^1.0.0-beta.2", False),
    ("1.0.1-beta.2", "^1.0.0-beta.1", False),  # a different x.y.z does not match
    ("2.0.0-0", "^1.0.0", False),
    ("1.0.0-alpha", ">=1.0.0-0 <1.0.0", True),
    ("1.0.0", "^1.0.0-beta.1", True),  # the release outranks its prereleases
    # build metadata is ignored
    ("1.2.3+build.5", "1.2.3", True),
]


@pytest.mark.parametrize(("version", "range_", "expected"), CASES)
def test_satisfies(version, range_, expected):
    assert satisfies(version, range_) is expected


def test_the_table_is_large_enough():
    assert len(CASES) >= 25


@pytest.mark.parametrize("bad", ["1.2", "v1.2.3", "01.2.3", "1.2.3-", "x", ""])
def test_a_malformed_version_raises(bad):
    with pytest.raises(ValueError, match="not a semantic version"):
        satisfies(bad, "*")


@pytest.mark.parametrize("bad", ["^a.b", ">=", "1.2.3.4", "1.x-beta", "^1 || ^foo"])
def test_a_malformed_range_raises(bad):
    with pytest.raises(ValueError):
        satisfies("1.2.3", bad)


@pytest.mark.parametrize(
    ("low", "high"),
    [
        ("1.0.0-alpha", "1.0.0-alpha.1"),
        ("1.0.0-alpha.1", "1.0.0-alpha.beta"),
        ("1.0.0-alpha.beta", "1.0.0-beta"),
        ("1.0.0-beta.2", "1.0.0-beta.11"),
        ("1.0.0-rc.1", "1.0.0"),
        ("1.9.0", "1.10.0"),
    ],
)
def test_precedence_follows_the_semver_spec(low, high):
    assert compare(parse_version(low), parse_version(high)) == -1
    assert compare(parse_version(high), parse_version(low)) == 1


def test_highest_orders_numerically_not_lexically():
    assert highest(["0.9.0", "0.10.0", "0.2.0"]) == "0.10.0"
    assert highest(["1.0.0-rc.1", "1.0.0"]) == "1.0.0"
    assert highest([]) is None
