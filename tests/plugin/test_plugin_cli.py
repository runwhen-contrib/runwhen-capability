"""`rwtask validate <dir>`."""

from __future__ import annotations

import shutil

from _plugin_fixtures import KUBE_MINI

from runwhen_capability.cli import main


def test_a_valid_plugin_exits_0_and_prints_nothing_on_stdout(capsys):
    assert main(["validate", str(KUBE_MINI)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "0 error(s), 0 warning(s)" in captured.err


def test_an_error_prints_file_pointer_code_message_and_exits_1(tmp_path, capsys):
    root = tmp_path / "plug"
    shutil.copytree(KUBE_MINI, root)
    tasks = root / "skills" / "k8s-base" / "tasks.yaml"
    tasks.write_text(tasks.read_text().replace("scripts/kubectl.sh", "scripts/gone.sh"))
    assert main(["validate", str(root)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1
    assert out[0].startswith("skills/k8s-base/tasks.yaml:/tasks/1/script: E_SCRIPT_MISSING ")
    assert "gone.sh" in out[0]


def test_warnings_are_prefixed_and_do_not_fail(tmp_path, capsys):
    skill = tmp_path / "k8s-workload-health"
    shutil.copytree(KUBE_MINI / "skills" / "k8s-workload-health", skill)
    assert main(["validate", str(skill)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out
    assert all(line.startswith("warning: skills/k8s-workload-health/") for line in out)
    assert all("W_TYPE_UNCHECKED" in line for line in out)


def test_a_skill_directory_with_an_error_exits_1(tmp_path, capsys):
    skill = tmp_path / "other-name"
    shutil.copytree(KUBE_MINI / "skills" / "k8s-workload-health", skill)
    assert main(["validate", str(skill)]) == 1
    assert "E_SKILL_NAME_MISMATCH" in capsys.readouterr().out


def test_a_loader_diagnostic_without_a_pointer_prints_just_the_file(tmp_path, capsys):
    root = tmp_path / "plug"
    shutil.copytree(KUBE_MINI, root)
    (root / "stray.txt").write_text("x")
    assert main(["validate", str(root)]) == 1
    assert capsys.readouterr().out.startswith("stray.txt: E_PATH_NOT_ALLOWED ")


def test_a_directory_that_is_neither_exits_1(tmp_path, capsys):
    assert main(["validate", str(tmp_path)]) == 1
    assert "neither a plugin.yaml nor a SKILL.md" in capsys.readouterr().err
    assert main(["validate", str(tmp_path / "missing")]) == 1
