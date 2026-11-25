"""`--with` required integrations (INT-2): plugin/deps.py, the dependency checks of
validate_tree and `rwtask validate|compile --with`."""

from __future__ import annotations

import json
import shutil

import pytest
from _plugin_fixtures import (
    KUBE_MINI,
    data_of,
    edit,
    kube_entries,
    platform_ops_entries,
    put,
)

from runwhen_capability.cli import main
from runwhen_capability.plugin import (
    DependencyError,
    PluginInvalid,
    compile_entries,
    compile_tree,
    load_dependency,
    load_dir,
    load_entries,
    validate_entries,
    validate_tree,
)
from runwhen_capability.plugin.deps import CompiledDependency

PLUGIN = "plugin.yaml"
RECOVERY_TASKS = "skills/papi-recovery/tasks.yaml"


def kube_dep() -> CompiledDependency:
    return load_dependency(str(KUBE_MINI))


def ops(requires: str = "^0.1"):
    """platform-ops requiring kubernetes at `requires` (the example says ^0.2; the
    kube-mini fixture is 0.1.0)."""
    return edit(platform_ops_entries(), PLUGIN, "kubernetes: ^0.2", f"kubernetes: {requires}")


def found(entries, code, **kw):
    return [d for d in validate_entries(entries, **kw) if d.code == code]


def compiled_file(tmp_path, name="kubernetes", version="0.1.0"):
    """kube-mini compiled to <tmp>/<name>/<version>.compiled.json (cache layout)."""
    doc = compile_entries(kube_entries())
    doc["plugin"]["name"], doc["plugin"]["version"] = name, version
    path = tmp_path / name / f"{version}.compiled.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    return path


# -- load_dependency ----------------------------------------------------------------------


def test_a_directory_loads_as_a_dependency():
    dep = kube_dep()
    assert (dep.name, dep.version) == ("kubernetes", "0.1.0")
    assert dep.platform == "kubernetes"
    assert dep.types() == {"cluster", "namespace", "deployment", "pod"}
    assert dep.credential_types() == {"kubernetes/kubeconfig"}
    assert dep.permission_schema["title"] == "One Kubernetes RBAC rule"


def test_a_plugin_without_an_integration_has_no_types(tmp_path):
    dep = CompiledDependency("x", "1.0.0", None, None)
    assert dep.types() == set() and dep.credential_types() == set() and dep.platform == "x"


def test_an_invalid_directory_lists_its_errors(tmp_path):
    root = tmp_path / "kube"
    shutil.copytree(KUBE_MINI, root)
    tasks = root / "skills" / "k8s-base" / "tasks.yaml"
    tasks.write_text(tasks.read_text().replace("scripts/kubectl.sh", "scripts/gone.sh"))
    with pytest.raises(DependencyError, match="E_SCRIPT_MISSING"):
        load_dependency(str(root))


def test_a_compiled_json_file_loads(tmp_path):
    dep = load_dependency(str(compiled_file(tmp_path)))
    assert dep.types() == kube_dep().types()


def test_a_compiled_json_file_must_be_a_compiled_plugin(tmp_path):
    bad = tmp_path / "x.compiled.json"
    bad.write_text('{"apiVersion": "nope"}')
    with pytest.raises(DependencyError, match="not a valid compiled plugin"):
        load_dependency(str(bad))
    bad.write_text("{")
    with pytest.raises(DependencyError, match="not valid JSON"):
        load_dependency(str(bad))
    with pytest.raises(DependencyError, match="cannot read"):
        load_dependency(str(tmp_path / "missing.compiled.json"))


def test_name_at_version_resolves_from_the_cache(tmp_path):
    compiled_file(tmp_path)
    dep = load_dependency("kubernetes@0.1.0", cache_dir=tmp_path)
    assert (dep.name, dep.version) == ("kubernetes", "0.1.0")
    with pytest.raises(DependencyError, match="not in the plugin cache"):
        load_dependency("kubernetes@0.9.0", cache_dir=tmp_path)
    with pytest.raises(DependencyError, match="not a semantic version"):
        load_dependency("kubernetes@latest", cache_dir=tmp_path)


def test_a_bare_name_resolves_to_the_highest_version_in_the_cache(tmp_path):
    for version in ("0.2.0", "0.10.0", "0.9.0"):
        compiled_file(tmp_path, version=version)
    assert load_dependency("kubernetes", cache_dir=tmp_path).version == "0.10.0"
    with pytest.raises(DependencyError, match="no version of 'other'"):
        load_dependency("other", cache_dir=tmp_path)


def test_the_cache_dir_defaults_to_the_environment(tmp_path, monkeypatch):
    compiled_file(tmp_path)
    monkeypatch.setenv("RUNWHEN_PLUGIN_CACHE", str(tmp_path))
    assert load_dependency("kubernetes@0.1.0").version == "0.1.0"


def test_a_cache_file_holding_another_plugin_is_refused(tmp_path):
    path = compiled_file(tmp_path)
    other = tmp_path / "other" / "0.1.0.compiled.json"
    other.parent.mkdir()
    shutil.copy(path, other)
    with pytest.raises(DependencyError, match="cache is corrupt"):
        load_dependency("other@0.1.0", cache_dir=tmp_path)


def test_something_that_is_no_spec_is_refused(tmp_path):
    with pytest.raises(DependencyError, match="not a directory or a file"):
        load_dependency("./nope/../x")
    (tmp_path / "empty").mkdir()
    with pytest.raises(DependencyError, match="no plugin.yaml"):
        load_dependency(str(tmp_path / "empty"))


# -- validate_tree against a dependency ---------------------------------------------------


def test_platform_ops_with_kube_mini_validates_clean():
    assert validate_entries(ops(), dependencies={"kubernetes": kube_dep()}) == []


def test_without_the_dependency_the_types_stay_unchecked():
    assert {d.code for d in validate_entries(ops())} == {"W_TYPE_UNCHECKED"}


def test_a_permission_with_a_typo_d_key_fails_the_dependencys_schema():
    entries = edit(ops(), RECOVERY_TASKS, "verbs: [patch]", "verb: [patch]")
    hits = found(entries, "E_PERMISSION_SCHEMA", dependencies={"kubernetes": kube_dep()})
    assert hits
    assert all(d.file == RECOVERY_TASKS for d in hits)
    assert all(d.path.startswith("/tasks/0/permissions/0") for d in hits)
    assert any("kubernetes integration's permission schema" in d.message for d in hits)
    assert found(entries, "E_PERMISSION_SCHEMA") == []  # unchecked without the dependency


def test_an_unknown_applies_to_type_fails_with_the_dependency():
    skill = next(
        f"skills/{name}/SKILL.md"
        for name in ("papi-health", "papi-recovery", "pgbouncer-health")
        if "kubernetes/deployment" in data_of(ops(), f"skills/{name}/SKILL.md")
    )
    entries = edit(ops(), skill, "kubernetes/deployment", "kubernetes/deploymnt")
    (hit,) = found(entries, "E_UNKNOWN_TYPE", dependencies={"kubernetes": kube_dep()})
    assert hit.file == skill and hit.path == "/runwhen/appliesTo/types/0"
    assert "kubernetes/deploymnt" in hit.message and "kubernetes@0.1.0" in hit.message


def test_an_unknown_credential_type_fails_with_the_dependency():
    entries = edit(
        ops(), RECOVERY_TASKS, "credentials: kubernetes/kubeconfig", "credentials: kubernetes/oops"
    )
    (hit,) = found(entries, "E_CREDENTIAL_TYPE_UNKNOWN", dependencies={"kubernetes": kube_dep()})
    assert "kubernetes/oops" in hit.message
    assert hit.file == RECOVERY_TASKS


def test_knowledge_applies_to_types_are_checked_too():
    entries = put(
        ops(),
        "knowledge/pods.md",
        "---\nname: pods\ndescription: About pods.\n"
        "appliesTo:\n  types: [kubernetes/podd]\n---\nBody.\n",
    )
    (hit,) = found(entries, "E_UNKNOWN_TYPE", dependencies={"kubernetes": kube_dep()})
    assert hit.file == "knowledge/pods.md"


def test_a_version_outside_the_range_is_e_requires_range():
    (hit,) = found(ops("^0.3"), "E_REQUIRES_RANGE", dependencies={"kubernetes": kube_dep()})
    assert (hit.file, hit.path, hit.severity) == (PLUGIN, "/requires/kubernetes", "error")
    assert "kubernetes@0.1.0" in hit.message and "^0.3" in hit.message
    assert found(ops("^0.3"), "E_REQUIRES_RANGE") == []  # nothing to check without it


def test_an_extends_range_is_checked_at_its_own_pointer():
    entries = edit(
        kube_entries(),
        PLUGIN,
        "license: Apache-2.0",
        "license: Apache-2.0\nextends:\n  kubernetes: ^1",
    )
    (hit,) = found(entries, "E_REQUIRES_RANGE", dependencies={"kubernetes": kube_dep()})
    assert hit.path == "/extends/kubernetes"


def test_requires_platform_is_not_a_dependency():
    entries = edit(ops(), PLUGIN, "requires:\n", "requires:\n  platform: '>=2026.11'\n")
    assert validate_entries(entries, dependencies={"kubernetes": kube_dep()}) == []


def test_an_unused_dependency_is_a_warning():
    (hit,) = found(kube_entries(), "W_DEPENDENCY_UNUSED", dependencies={"kubernetes": kube_dep()})
    assert (hit.severity, hit.file) == ("warning", PLUGIN)
    assert "'kubernetes'" in hit.message
    assert not [
        d
        for d in validate_entries(kube_entries(), dependencies={"kubernetes": kube_dep()})
        if d.severity == "error"
    ]


def test_a_dependency_with_a_broken_permission_schema_is_reported_once():
    dep = kube_dep()
    broken = CompiledDependency(dep.name, dep.version, dep.integration, {"type": 12})
    hits = found(ops(), "E_PERMISSION_SCHEMA", dependencies={"kubernetes": broken})
    assert len(hits) == 1
    assert (hits[0].file, hits[0].path) == (PLUGIN, "/requires/kubernetes")
    pattern = CompiledDependency(
        dep.name, dep.version, dep.integration, {"type": "object", "pattern": "(?=a)"}
    )
    assert found(ops(), "E_SCHEMA_PATTERN", dependencies={"kubernetes": pattern})


def test_validate_tree_takes_a_tree():
    tree = load_entries(ops())
    assert validate_tree(tree, dependencies={"kubernetes": kube_dep()}) == []


def test_compile_validates_against_the_dependencies():
    entries = edit(ops(), RECOVERY_TASKS, "verbs: [patch]", "verb: [patch]")
    assert compile_entries(entries)  # no dependency: nothing to refuse
    with pytest.raises(PluginInvalid, match="E_PERMISSION_SCHEMA"):
        compile_entries(entries, dependencies={"kubernetes": kube_dep()})
    with pytest.raises(PluginInvalid, match="E_REQUIRES_RANGE"):
        compile_tree(load_entries(ops("^0.3")), dependencies={"kubernetes": kube_dep()})
    good = compile_entries(ops(), dependencies={"kubernetes": kube_dep()})
    assert good == compile_entries(ops())  # the output does not depend on them


# -- the CLI -------------------------------------------------------------------------------


@pytest.fixture
def ops_dir(tmp_path):
    root = tmp_path / "ops"
    for entry in ops():
        target = root / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(entry.data)
    return root


def test_validate_with_a_directory_is_clean(ops_dir, capsys):
    assert main(["validate", str(ops_dir), "--with", str(KUBE_MINI)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "0 error(s), 0 warning(s)" in captured.err
    assert main(["validate", str(ops_dir)]) == 0
    assert "W_TYPE_UNCHECKED" in capsys.readouterr().out


def test_validate_with_reports_a_dependency_error(ops_dir, capsys):
    tasks = ops_dir / RECOVERY_TASKS
    tasks.write_text(tasks.read_text().replace("verbs: [patch]", "verb: [patch]"))
    assert main(["validate", str(ops_dir), "--with", str(KUBE_MINI)]) == 1
    assert "E_PERMISSION_SCHEMA" in capsys.readouterr().out


def test_validate_with_a_compiled_json_and_a_cache_name(ops_dir, tmp_path, capsys, monkeypatch):
    path = compiled_file(tmp_path / "cache")
    assert main(["validate", str(ops_dir), "--with", str(path)]) == 0
    monkeypatch.setenv("RUNWHEN_PLUGIN_CACHE", str(tmp_path / "cache"))
    assert main(["validate", str(ops_dir), "--with", "kubernetes@0.1.0"]) == 0
    assert main(["validate", str(ops_dir), "--with", "kubernetes"]) == 0
    capsys.readouterr()


def test_validate_with_a_range_mismatch_exits_1(ops_dir, capsys):
    plugin = ops_dir / PLUGIN
    plugin.write_text(plugin.read_text().replace("^0.1", "^0.3"))
    assert main(["validate", str(ops_dir), "--with", str(KUBE_MINI)]) == 1
    assert "plugin.yaml:/requires/kubernetes: E_REQUIRES_RANGE" in capsys.readouterr().out


def test_a_bad_spec_exits_2_with_a_message(ops_dir, tmp_path, capsys):
    assert main(["validate", str(ops_dir), "--with", str(tmp_path / "nope.compiled.json")]) == 2
    err = capsys.readouterr().err
    assert "rwtask validate: --with" in err and "cannot read" in err
    assert main(["compile", str(ops_dir), "--with", "../not a spec"]) == 2
    assert "rwtask compile: --with ../not a spec" in capsys.readouterr().err


def test_the_same_plugin_twice_exits_2(ops_dir, capsys):
    assert main(["validate", str(ops_dir), "--with", str(KUBE_MINI), "--with", str(KUBE_MINI)]) == 2
    assert "twice" in capsys.readouterr().err


def test_compile_with_validates_then_writes(ops_dir, tmp_path, capsys):
    out = tmp_path / "dist"
    assert main(["compile", str(ops_dir), "--with", str(KUBE_MINI), "--out", str(out)]) == 0
    assert (out / "runwhen-platform-ops-0.1.0.compiled.json").is_file()
    tasks = ops_dir / RECOVERY_TASKS
    tasks.write_text(tasks.read_text().replace("verbs: [patch]", "verb: [patch]"))
    assert main(["compile", str(ops_dir), "--with", str(KUBE_MINI), "--out", str(out / "2")]) == 1
    assert "E_PERMISSION_SCHEMA" in capsys.readouterr().out
    assert not (out / "2").exists()
    assert load_dir(KUBE_MINI)  # the fixture is untouched
