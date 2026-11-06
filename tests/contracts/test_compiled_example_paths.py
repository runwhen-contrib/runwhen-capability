"""Every path in a compiled-plugin document is relative to the plugin root
(CONTRACTS.md "Compiled plugin"): the same paths the lease and the archive use,
so a consumer can join them to an unpacked archive without knowing the skill."""

from __future__ import annotations

from pathlib import Path

import pytest
from _fixtures import EXAMPLES, fixture_id, load_fixture

VALID = sorted((EXAMPLES / "compiled-plugin").glob("*.yaml"))


def test_there_are_compiled_plugin_examples():
    assert VALID


@pytest.mark.parametrize("path", VALID, ids=fixture_id)
def test_compiled_example_paths_are_plugin_root_relative(path: Path):
    doc = load_fixture(path)
    for skill in doc["skills"]:
        prefix = f"skills/{skill['name']}/"
        assert skill["files"], f"{skill['name']}: no files"
        for file in skill["files"]:
            assert file.startswith(prefix), f"{skill['name']}: file {file!r} is not under {prefix}"
        assert prefix + "SKILL.md" in skill["files"]
    for task in doc["tasks"]:
        prefix = f"skills/{task['skill']}/"
        assert task["script"].startswith(prefix), (
            f"{task['ref']}: script {task['script']!r} is not under {prefix}"
        )
        skill = next(s for s in doc["skills"] if s["name"] == task["skill"])
        assert task["script"] in skill["files"], f"{task['ref']}: script is not in skills[].files"
        for name, output in task["outputs"].items():
            ref = output.get("schemaRef")
            if ref is not None:
                assert ref.startswith(prefix), f"{task['ref']}: {name} schemaRef {ref!r}"
                assert ref in skill["files"], f"{task['ref']}: {name} schemaRef is not in files"
