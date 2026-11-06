"""The YAML 1.2 loader is part of the public contracts API."""

import runwhen_capability.contracts as contracts
from runwhen_capability.contracts import load_yaml, split_frontmatter


def test_public_load_yaml_is_yaml_12():
    assert load_yaml("v: no") == {"v": "no"}


def test_public_split_frontmatter():
    meta, body = split_frontmatter("---\nname: x\n---\nbody\n")
    assert meta == {"name": "x"}
    assert body.strip() == "body"


def test_yaml_helpers_are_in_all():
    assert {"load_yaml", "split_frontmatter"} <= set(contracts.__all__)
