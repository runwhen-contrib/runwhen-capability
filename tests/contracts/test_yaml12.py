"""The plugin-file loader follows YAML 1.2 core-schema scalars, not PyYAML's 1.1."""

import math

import pytest

from runwhen_capability.contracts._yaml12 import load_yaml, split_frontmatter


@pytest.mark.parametrize("word", ["no", "No", "NO", "yes", "on", "off", "y", "n"])
def test_yaml_11_booleans_stay_strings(word):
    assert load_yaml(f"v: {word}\n") == {"v": word}


def test_kubectl_short_name_no_is_a_string():
    assert load_yaml("aliases: [no]\n") == {"aliases": ["no"]}


@pytest.mark.parametrize(("text", "value"), [("true", True), ("False", False), ("TRUE", True)])
def test_core_booleans(text, value):
    assert load_yaml(f"v: {text}\n")["v"] is value


@pytest.mark.parametrize(
    ("text", "value"),
    [("017", 17), ("0o17", 15), ("0x1F", 31), ("-5", -5), ("1e3", 1000.0), ("0.5", 0.5)],
)
def test_core_numbers(text, value):
    assert load_yaml(f"v: {text}\n")["v"] == value


@pytest.mark.parametrize("text", ["1:20", "1_000", "0b101", "2026-11-03", "2026-11-03T09:14:02Z"])
def test_yaml_11_only_scalars_stay_strings(text):
    assert load_yaml(f"v: {text}\n") == {"v": text}


def test_special_floats():
    doc = load_yaml("a: .inf\nb: -.Inf\nc: .nan\n")
    assert doc["a"] == math.inf and doc["b"] == -math.inf and math.isnan(doc["c"])


def test_quoted_mode_stays_a_string():
    assert load_yaml('mode: "0600"\n') == {"mode": "0600"}


def test_split_frontmatter():
    meta, body = split_frontmatter("---\nname: x\ndescription: y\n---\n# Body\n")
    assert meta == {"name": "x", "description": "y"} and body == "# Body\n"


@pytest.mark.parametrize("text", ["name: x\n", "---\nname: x\n", "---\n- a\n---\n"])
def test_split_frontmatter_rejects(text):
    with pytest.raises(ValueError):
        split_frontmatter(text)
