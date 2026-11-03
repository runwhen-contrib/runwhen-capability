"""runwhen_capability.plugin.tree: loading a plugin tree from entries or a directory."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
from _plugin_fixtures import (
    KUBE_MINI,
    data_of,
    drop,
    edit,
    kube_entries,
    platform_ops_entries,
    put,
)

from runwhen_capability.contracts import TreeEntry, archive
from runwhen_capability.plugin import load_dir, load_entries, load_skill_dir


def codes(tree) -> list[str]:
    return [d.code for d in tree.diagnostics]


def one(tree, code):
    found = [d for d in tree.diagnostics if d.code == code]
    assert len(found) == 1, tree.diagnostics
    return found[0]


def test_loads_the_kube_mini_fixture_from_entries():
    tree = load_entries(kube_entries())
    assert tree.diagnostics == []
    assert tree.plugin["name"] == "kubernetes"
    assert tree.integration["platform"] == "kubernetes"
    assert set(tree.integration["types"]) == {"cluster", "namespace", "deployment", "pod"}
    assert tree.integration_sources["platform"] == "integration/integration.yaml"
    assert tree.integration_sources["types"] == "integration/types.yaml"
    assert set(tree.skills) == {"k8s-base", "k8s-workload-health"}
    base = tree.skills["k8s-base"]
    assert base.name == "k8s-base"
    assert base.frontmatter["runwhen"]["role"] == ["discovery", "cli"]
    assert base.body.startswith("Run `discover`")
    assert [t["name"] for t in base.tasks["tasks"]] == ["discover", "kubectl"]
    assert "scripts/kubectl.sh" in base.files and "tasks.yaml" in base.files
    assert "skills/k8s-base/tasks.yaml" in tree.files
    fm, body = tree.rules["one"]
    assert fm["name"] == "one" and body.startswith("Use read-only")
    assert tree.knowledge["one"][0]["appliesTo"] == {"types": ["kubernetes/deployment"]}


def test_executable_bit_is_kept_from_the_entries():
    tree = load_entries(kube_entries())
    assert tree.executable == {
        "skills/k8s-base/scripts/kubectl.sh",
        "skills/k8s-workload-health/scripts/restart.sh",
    }


def test_loads_the_platform_ops_plugin_with_no_integration():
    tree = load_entries(platform_ops_entries())
    assert tree.diagnostics == []
    assert tree.integration is None and tree.integration_sources == {}
    assert tree.plugin["requires"] == {"kubernetes": "^0.2"}
    assert set(tree.skills) == {"papi-health", "papi-recovery", "pgbouncer-health"}


def test_round_trips_through_the_archive():
    entries = kube_entries()
    via_archive = load_entries(archive.read(archive.build(entries)))
    direct = load_entries(entries)
    assert via_archive.diagnostics == []
    assert via_archive.files == direct.files
    assert via_archive.executable == direct.executable
    assert via_archive.plugin == direct.plugin
    assert via_archive.integration == direct.integration
    assert set(via_archive.skills) == set(direct.skills)


def test_entries_may_be_a_generator():
    tree = load_entries(e for e in kube_entries())
    assert tree.plugin["name"] == "kubernetes"


def test_unparseable_plugin_yaml_is_e_yaml_with_the_file_and_line():
    tree = load_entries(put(kube_entries(), "plugin.yaml", "name: [unclosed\n"))
    d = one(tree, "E_YAML")
    assert d.file == "plugin.yaml" and d.severity == "error"
    assert d.line is not None
    assert tree.plugin is None


def test_a_yaml_file_that_is_not_a_mapping_is_e_yaml():
    tree = load_entries(put(kube_entries(), "plugin.yaml", "- a\n- b\n"))
    assert one(tree, "E_YAML").file == "plugin.yaml"


def test_yaml_is_parsed_as_1_2():
    entries = put(
        kube_entries(), "integration/types.yaml", "types:\n  node: { parent: x, aliases: [no] }\n"
    )
    tree = load_entries(entries)
    assert tree.integration["types"]["node"]["aliases"] == ["no"]


def test_unparseable_frontmatter_is_e_yaml():
    entries = put(kube_entries(), "skills/k8s-base/SKILL.md", "no frontmatter here\n")
    tree = load_entries(entries)
    assert one(tree, "E_YAML").file == "skills/k8s-base/SKILL.md"
    assert "k8s-base" in tree.skills  # the skill is still there; its tasks are still loaded
    assert tree.skills["k8s-base"].tasks is not None


def test_broken_rule_and_tasks_files_are_e_yaml():
    entries = put(kube_entries(), "rules/one.md", "---\nname: [x\n---\nbody\n")
    entries = put(entries, "skills/k8s-base/tasks.yaml", "tasks: [\n")
    tree = load_entries(entries)
    assert sorted(d.file for d in tree.diagnostics if d.code == "E_YAML") == [
        "rules/one.md",
        "skills/k8s-base/tasks.yaml",
    ]
    assert tree.skills["k8s-base"].tasks is None


def test_non_utf8_yaml_is_e_yaml():
    tree = load_entries(put(kube_entries(), "plugin.yaml", b"name: \xff\xfe\n"))
    assert one(tree, "E_YAML").file == "plugin.yaml"


def test_skill_directory_without_skill_md():
    tree = load_entries(drop(kube_entries(), "skills/k8s-base/SKILL.md"))
    assert one(tree, "E_SKILL_MD_MISSING").file == "skills/k8s-base/SKILL.md"
    assert tree.skills["k8s-base"].tasks is not None


@pytest.mark.parametrize(
    "path",
    [
        "stray.txt",
        "integration/notes.md",
        "integration/other/x.yaml",
        "skills/k8s-base/README.md",
        "skills/loose.md",
        "rules/sub/x.md",
        "rules/x.txt",
        "knowledge/x.yaml",
        ".github/workflows/ci.yaml",
        "scripts/run.sh",
    ],
)
def test_a_path_outside_the_layout_is_e_path_not_allowed(path):
    tree = load_entries(put(kube_entries(), path, "x"))
    d = one(tree, "E_PATH_NOT_ALLOWED")
    assert d.file == path
    assert path not in tree.files


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "LICENSE",
        "CHANGELOG.md",
        "connections.yaml",
        "integration/facets.yaml",
        "integration/dependencies.yaml",
        "integration/schemas/deep/x.v1.json",
        "integration/auth/gke.py",
        "skills/k8s-base/lib/helpers.py",
        "skills/k8s-base/references/guide.md",
        "skills/k8s-base/assets/logo.png",
        "skills/k8s-base/tests/kubectl/a.yaml",
        "skills/k8s-base/schemas/x.v1.json",
    ],
)
def test_every_layout_path_is_allowed(path):
    entries = put(kube_entries(), path, "{}" if path.endswith(("yaml", "json")) else "x")
    if path == "integration/facets.yaml":
        entries = put(entries, path, "facets: []\n")
    if path == "integration/dependencies.yaml":
        entries = put(entries, path, "rules: []\n")
    if path == "connections.yaml":
        entries = put(entries, path, "connections: []\n")
    tree = load_entries(entries)
    assert "E_PATH_NOT_ALLOWED" not in codes(tree)
    assert path in tree.files


def test_root_tests_directory_is_excluded_like_the_archive_does():
    tree = load_entries(put(kube_entries(), "tests/fixtures/x.yaml", "a: 1"))
    assert tree.diagnostics == []
    assert "tests/fixtures/x.yaml" not in tree.files


@pytest.mark.parametrize(
    "path",
    [
        "/abs.yaml",
        "skills/../plugin.yaml",
        "a\\b.yaml",
        "skills/k8s-base/é.md",  # not NFC
        "x" * 300,
        "skills//x",
        "skills/k8s-base/a\nb",
    ],
)
def test_an_unsafe_path_is_e_invalid_path(path):
    tree = load_entries(put(kube_entries(), path, "x"))
    d = one(tree, "E_INVALID_PATH")
    assert d.file == path
    assert path not in tree.files


def test_case_colliding_paths_are_e_invalid_path():
    tree = load_entries(put(kube_entries(), "PLUGIN.yaml", "name: x\n"))
    assert one(tree, "E_INVALID_PATH").file == "PLUGIN.yaml"


def test_the_same_integration_key_in_two_files_is_e_duplicate_key():
    entries = put(
        kube_entries(),
        "integration/facets.yaml",
        "types:\n  x: {plural: xs, root: true, rootKey: k}\n",
    )
    tree = load_entries(entries)
    d = one(tree, "E_DUPLICATE_KEY")
    assert d.file == "integration/facets.yaml" and d.path == "/types"
    assert "integration/types.yaml" in d.message
    assert tree.integration_sources["types"] == "integration/types.yaml"
    assert set(tree.integration["types"]) == {"cluster", "namespace", "deployment", "pod"}


def test_integration_files_merge_each_key_from_the_file_that_has_it():
    entries = put(kube_entries(), "integration/dependencies.yaml", "rules: []\nedgeTypes: {}\n")
    tree = load_entries(entries)
    assert tree.diagnostics == []
    assert tree.integration["rules"] == []
    assert tree.integration_sources["rules"] == "integration/dependencies.yaml"


def test_load_dir_matches_load_entries():
    from_dir = load_dir(KUBE_MINI)
    assert from_dir.diagnostics == []
    assert from_dir.files == load_entries(kube_entries()).files
    assert from_dir.executable == load_entries(kube_entries()).executable


def test_load_dir_skips_git_root_dotfiles_and_root_tests(tmp_path):
    root = tmp_path / "plug"
    shutil.copytree(KUBE_MINI, root)
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: x")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yaml").write_text("on: push")
    (root / ".gitignore").write_text("x")
    (root / "tests").mkdir()
    (root / "tests" / "big.yaml").write_text("x: 1")
    tree = load_dir(root)
    assert tree.diagnostics == []
    assert not [p for p in tree.files if p.startswith((".", "tests/"))]


def test_load_dir_flags_a_path_outside_the_layout(tmp_path):
    root = tmp_path / "plug"
    shutil.copytree(KUBE_MINI, root)
    (root / "notes.txt").write_text("x")
    assert one(load_dir(root), "E_PATH_NOT_ALLOWED").file == "notes.txt"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_load_dir_rejects_symlinks_and_never_follows_them(tmp_path):
    root = tmp_path / "plug"
    shutil.copytree(KUBE_MINI, root)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-MARKER")
    os.symlink(secret, root / "skills" / "k8s-base" / "scripts" / "leak.sh")
    os.symlink(tmp_path, root / "skills" / "k8s-base" / "assets")
    tree = load_dir(root)
    flagged = sorted(d.file for d in tree.diagnostics if d.code == "E_PATH_NOT_ALLOWED")
    assert flagged == ["skills/k8s-base/assets", "skills/k8s-base/scripts/leak.sh"]
    assert b"TOP-SECRET-MARKER" not in b"".join(tree.files.values())
    assert "skills/k8s-base/scripts/leak.sh" not in tree.files


def test_load_dir_reads_the_executable_bit(tmp_path):
    root = tmp_path / "plug"
    shutil.copytree(KUBE_MINI, root)
    os.chmod(root / "skills/k8s-base/scripts/discover.py", 0o755)
    os.chmod(root / "skills/k8s-base/scripts/kubectl.sh", 0o644)
    assert "skills/k8s-base/scripts/discover.py" in load_dir(root).executable
    assert "skills/k8s-base/scripts/kubectl.sh" not in load_dir(root).executable


def test_load_skill_dir_wraps_a_lone_skill_as_a_workspace_plugin(tmp_path):
    skill = tmp_path / "k8s-base"
    shutil.copytree(KUBE_MINI / "skills" / "k8s-base", skill)
    tree = load_skill_dir(skill)
    assert tree.diagnostics == []
    assert tree.plugin is None and tree.integration is None
    assert list(tree.skills) == ["k8s-base"]
    assert tree.skills["k8s-base"].tasks["runtime"] == "kubernetes@1"
    assert "skills/k8s-base/scripts/kubectl.sh" in tree.files
    assert "skills/k8s-base/scripts/kubectl.sh" in tree.executable


def test_load_skill_dir_takes_the_name_from_the_directory(tmp_path):
    skill = tmp_path / "renamed"
    shutil.copytree(KUBE_MINI / "skills" / "k8s-base", skill)
    tree = load_skill_dir(skill)
    assert list(tree.skills) == ["renamed"]
    assert tree.skills["renamed"].frontmatter["name"] == "k8s-base"


def test_load_skill_dir_with_a_relative_dot(tmp_path, monkeypatch):
    skill = tmp_path / "k8s-base"
    shutil.copytree(KUBE_MINI / "skills" / "k8s-base", skill)
    monkeypatch.chdir(skill)
    assert list(load_skill_dir(Path(".")).skills) == ["k8s-base"]


def test_data_of_helper_sanity():
    assert "kubectl" in data_of(kube_entries(), "skills/k8s-base/tasks.yaml")
    edited = edit(kube_entries(), "plugin.yaml", "0.1.0", "0.2.0")
    assert load_entries(edited).plugin["version"] == "0.2.0"
    assert TreeEntry("a", b"").path == "a"


def test_load_entries_drops_the_lock_and_editor_schemas_like_root_tests():
    from runwhen_capability.contracts import TreeEntry
    from runwhen_capability.plugin import load_entries

    entries = [
        TreeEntry("plugin.yaml", b"name: p\nversion: 0.1.0\ndescription: d\n"),
        TreeEntry("runwhen.lock", b"lockfileVersion: 1\n"),
        TreeEntry(".runwhen/schemas/tasks.schema.json", b"{}"),
        TreeEntry(".runwhen/README", b"x"),
    ]
    tree = load_entries(entries)
    assert not [d for d in tree.diagnostics if d.code == "E_PATH_NOT_ALLOWED"]
    assert set(tree.files) == {"plugin.yaml"}
