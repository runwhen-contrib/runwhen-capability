"""`rwtask upconvert <src-dir> --into <skills-dir> [--name NAME] [--force]`."""

from __future__ import annotations

import os

from upconvert_helpers import BUNDLES, CAPABILITIES, write_tree

from runwhen_capability.cli import main
from runwhen_capability.plugin import load_skill_dir, validate_tree


def test_a_custom_bundle_is_written_validated_and_reported(tmp_path, capsys):
    skills = tmp_path / "skills"
    assert main(["upconvert", str(BUNDLES / "pgbouncer-health"), "--into", str(skills)]) == 0
    skill = skills / "pgbouncer-health"
    assert (skill / "SKILL.md").is_file() and (skill / "tasks.yaml").is_file()
    assert (skill / "scripts" / "pool_errors.sh").is_file()
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert lines and all(line.startswith("warning: ") for line in lines)
    assert any(
        "capability.yaml:appliesTo[0].where.namespace: W_UPCONVERT_LOSSY" in line for line in lines
    )
    assert any("W_TYPE_UNCHECKED" in line for line in lines)  # the validation of the result
    assert "0 error(s)" in err
    assert not [d for d in validate_tree(load_skill_dir(skill)) if d.severity == "error"]


def test_a_packaged_capability_is_detected_by_its_manifest(tmp_path, capsys):
    skills = tmp_path / "skills"
    assert main(["upconvert", str(CAPABILITIES / "echo"), "--into", str(skills)]) == 0
    assert (skills / "rw-echo-fixture" / "lib" / "tasks.py").is_file()
    assert (skills / "rw-echo-fixture" / "scripts" / "echo.py").is_file()
    assert "W_UPCONVERT_SETUP_DROPPED" in capsys.readouterr().out


def test_name_overrides_the_directory(tmp_path):
    skills = tmp_path / "skills"
    assert (
        main(["upconvert", str(BUNDLES / "testable"), "--into", str(skills), "--name", "t2"]) == 0
    )
    assert (skills / "t2" / "SKILL.md").is_file()
    assert "name: t2" in (skills / "t2" / "SKILL.md").read_text()
    assert not (skills / "testable").exists()


def test_an_existing_skill_is_not_overwritten_without_force(tmp_path, capsys):
    skills = tmp_path / "skills"
    args = ["upconvert", str(BUNDLES / "testable"), "--into", str(skills)]
    assert main(args) == 0
    marker = skills / "testable" / "stale.txt"
    marker.write_text("keep")
    capsys.readouterr()
    assert main(args) == 1
    assert "--force" in capsys.readouterr().err
    assert marker.read_text() == "keep"
    assert main([*args, "--force"]) == 0
    assert not marker.exists()  # the directory is replaced, not merged


def test_the_exec_bit_is_set_on_the_written_script(tmp_path):
    src = write_tree(
        tmp_path / "src",
        {
            "capability.yaml": "apiVersion: runwhen.com/custom-capability/v1\nname: runme\n"
            "description: d\ntasks:\n  - name: go\n    file: tasks/go.sh\n    description: Go\n"
            "    readOnly: true\n",
            "tasks/go.sh": "#!/bin/sh\n",
        },
    )
    os.chmod(src / "tasks/go.sh", 0o755)
    skills = tmp_path / "skills"
    assert main(["upconvert", str(src), "--into", str(skills)]) == 0
    assert os.access(skills / "runme" / "scripts" / "go.sh", os.X_OK)


def test_a_result_that_fails_validation_exits_1(tmp_path, capsys):
    src = write_tree(
        tmp_path / "src",
        {
            "capability.yaml": "apiVersion: runwhen.com/custom-capability/v1\nname: longdoc\n"
            "description: d\ntasks:\n  - name: go\n    file: tasks/go.sh\n    description: Go\n"
            "    readOnly: true\n",
            "tasks/go.sh": "x\n",
            "README.md": "line\n" * 600,  # a SKILL.md body is at most 500 lines
        },
    )
    assert main(["upconvert", str(src), "--into", str(tmp_path / "skills")]) == 1
    out, err = capsys.readouterr()
    assert any("E_SKILL_BODY_TOO_LONG" in line for line in out.splitlines())
    assert "1 error(s)" in err


def test_a_directory_that_is_neither_format_exits_1(tmp_path, capsys):
    assert main(["upconvert", str(tmp_path), "--into", str(tmp_path / "skills")]) == 1
    assert "capability.yaml" in capsys.readouterr().err
    assert main(["upconvert", str(tmp_path / "missing"), "--into", str(tmp_path / "s")]) == 1


def test_a_manifest_of_another_apiVersion_exits_1(tmp_path, capsys):
    src = write_tree(tmp_path / "src", {"capability.yaml": "apiVersion: x/y\nname: a\n"})
    assert main(["upconvert", str(src), "--into", str(tmp_path / "skills")]) == 1
    assert "apiVersion" in capsys.readouterr().err


def test_a_name_that_is_a_path_is_refused_before_anything_is_removed(tmp_path, capsys):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "keep.txt").write_text("x")
    for bad in ("..", ".", "a/b"):
        args = ["upconvert", str(BUNDLES / "testable"), "--into", str(skills), "--force"]
        assert main([*args, "--name", bad]) == 1
    assert "not a usable skill name" in capsys.readouterr().err
    assert (skills / "keep.txt").read_text() == "x"
