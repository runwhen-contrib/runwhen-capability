"""`rwtask pull`: runwhen.lock, --frozen and the offline editor schemas (AUT-1)."""

from __future__ import annotations

import json
import shutil

import pytest
import responses
from _git_remote import make_remote
from _plugin_fixtures import edit, kube_entries, platform_ops_entries
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from runwhen_capability.cli import main
from runwhen_capability.contracts import archive, load_yaml
from runwhen_capability.plugin import compile_entries
from runwhen_capability.plugin.lock import (
    LOCK_FILE,
    LockedPlugin,
    LockError,
    declared_dependencies,
    read_lock,
    render_lock,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture(scope="module")
def remote(tmp_path_factory):
    return make_remote(tmp_path_factory.mktemp("remote"))


@pytest.fixture
def cache(tmp_path, monkeypatch):
    path = tmp_path / "cache"
    monkeypatch.setenv("RUNWHEN_PLUGIN_CACHE", str(path))
    return path


@pytest.fixture
def ops_dir(tmp_path):
    """platform-ops requiring kubernetes ^0.1 (and the platform, which is not pulled)."""
    entries = edit(
        platform_ops_entries(),
        "plugin.yaml",
        "kubernetes: ^0.2",
        'platform: ">=2026.11"\n  kubernetes: ^0.1',
    )
    root = tmp_path / "ops"
    for entry in entries:
        target = root / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(entry.data)
    return root


def run_pull(ops_dir, *args):
    return main(["pull", "--plugin-dir", str(ops_dir), *args])


def set_range(ops_dir, range_):
    plugin = ops_dir / "plugin.yaml"
    plugin.write_text(plugin.read_text().replace("kubernetes: ^0.1", f'kubernetes: "{range_}"'))


# -- the lockfile --------------------------------------------------------------------------


def test_pull_resolves_requires_caches_and_writes_the_lock(ops_dir, cache, remote, capsys):
    assert run_pull(ops_dir, "--from", f"git+{remote}") == 0
    out = capsys.readouterr().out
    assert out.startswith("kubernetes 0.1.0 sha256:") and "(git+file://" in out
    tar = cache / "kubernetes" / "0.1.0.tar"
    assert tar.is_file() and (cache / "kubernetes" / "0.1.0.compiled.json").is_file()
    lock = read_lock(ops_dir)
    assert lock == {
        "kubernetes": LockedPlugin(
            "^0.1", "0.1.0", archive.archive_sha256(tar.read_bytes()), f"git+{remote}"
        )
    }
    text = (ops_dir / LOCK_FILE).read_text()
    assert text.splitlines()[1:] == [
        "lockfileVersion: 1",
        "requires:",
        "  kubernetes:",
        f"    archiveSha256: {lock['kubernetes'].archive_sha256}",
        "    range: ^0.1",
        f"    source: git+{remote}",
        "    version: 0.1.0",
    ]
    assert load_yaml(text)["requires"]["kubernetes"]["range"] == "^0.1"


def test_pulling_again_leaves_the_lock_alone(ops_dir, cache, remote, capsys):
    assert run_pull(ops_dir, "--from", f"git+{remote}") == 0
    before = (ops_dir / LOCK_FILE).read_bytes()
    capsys.readouterr()
    assert run_pull(ops_dir) == 0  # the lock remembers the source
    assert (ops_dir / LOCK_FILE).read_bytes() == before
    assert "wrote" not in capsys.readouterr().out


def test_a_changed_range_re_resolves(ops_dir, cache, remote):
    assert run_pull(ops_dir, "--from", f"git+{remote}") == 0
    set_range(ops_dir, "^0.2")
    assert run_pull(ops_dir) == 0
    assert read_lock(ops_dir)["kubernetes"].version == "0.2.0"
    assert read_lock(ops_dir)["kubernetes"].range == "^0.2"


def test_a_named_pull_may_narrow_the_range(ops_dir, cache, remote, capsys):
    set_range(ops_dir, ">=0.1.0")
    assert run_pull(ops_dir, "--from", f"git+{remote}", "kubernetes@^0.1") == 0
    entry = read_lock(ops_dir)["kubernetes"]
    assert (entry.range, entry.version) == (">=0.1.0", "0.1.0")  # plugin.yaml's range is kept
    assert run_pull(ops_dir, "kubernetes") == 0
    assert read_lock(ops_dir)["kubernetes"].version == "0.2.0"
    capsys.readouterr()


def test_a_named_range_outside_plugin_yaml_is_refused(ops_dir, cache, remote, capsys):
    assert run_pull(ops_dir, "--from", f"git+{remote}", "kubernetes@^0.2") == 2
    assert "does not satisfy plugin.yaml's '^0.1'" in capsys.readouterr().err
    assert not (ops_dir / LOCK_FILE).exists()


def test_an_undeclared_name_is_refused(ops_dir, cache, remote, capsys):
    assert run_pull(ops_dir, "--from", f"git+{remote}", "other") == 2
    assert "'other' is not in plugin.yaml's requires or extends" in capsys.readouterr().err


def test_an_unresolvable_range_exits_1_and_writes_no_lock(ops_dir, cache, remote, capsys):
    set_range(ops_dir, "^0.4")
    assert run_pull(ops_dir, "--from", f"git+{remote}") == 1
    assert "no version of 'kubernetes' satisfies '^0.4'" in capsys.readouterr().err
    assert not (ops_dir / LOCK_FILE).exists()


def test_an_unparseable_plugin_yaml_exits_1(ops_dir, capsys):
    (ops_dir / "plugin.yaml").write_text("name: [\n")
    assert run_pull(ops_dir) == 1
    assert "cannot read" in capsys.readouterr().err


def test_a_directory_without_a_plugin_yaml_exits_1(tmp_path, capsys):
    assert main(["pull", "--plugin-dir", str(tmp_path)]) == 1
    assert "has no plugin.yaml" in capsys.readouterr().err


def test_from_and_registry_are_exclusive(ops_dir):
    with pytest.raises(SystemExit) as info:
        run_pull(ops_dir, "--from", "x", "--registry", "https://r")
    assert info.value.code == 2


def test_a_plugin_that_requires_nothing_gets_no_lock(tmp_path, cache):
    for entry in kube_entries():
        target = tmp_path / "k" / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(entry.data)
    assert main(["pull", "--plugin-dir", str(tmp_path / "k")]) == 0
    assert not (tmp_path / "k" / LOCK_FILE).exists()
    assert (tmp_path / "k" / ".runwhen" / "schemas" / "plugin.schema.json").is_file()


def test_the_lock_reader_rejects_what_it_cannot_use(tmp_path):
    def read(text):
        (tmp_path / LOCK_FILE).write_text(text)
        return read_lock(tmp_path)

    assert read_lock(tmp_path) is None
    with pytest.raises(LockError, match="lockfileVersion must be 1"):
        read("lockfileVersion: 2\n")
    with pytest.raises(LockError, match="needs exactly"):
        read("lockfileVersion: 1\nrequires:\n  a: {version: 1.0.0}\n")
    good = {"range": "^1", "version": "1.0.0", "source": "x", "archiveSha256": "sha256:" + "a" * 64}
    assert read(render_lock({"a": LockedPlugin("^1", "1.0.0", good["archiveSha256"], "x")}))
    with pytest.raises(LockError, match="archiveSha256"):
        read(render_lock({"a": LockedPlugin("^1", "1.0.0", "abc", "x")}))
    with pytest.raises(LockError, match="not a semantic version"):
        read(render_lock({"a": LockedPlugin("^1", "one", good["archiveSha256"], "x")}))


def test_declared_dependencies_skip_the_platform():
    plugin = {
        "requires": {"platform": ">=1", "kubernetes": "^0.1"},
        "extends": {"base": "^2"},
    }
    assert declared_dependencies(plugin) == {"kubernetes": "^0.1", "base": "^2"}


# -- --frozen ------------------------------------------------------------------------------


def test_frozen_needs_a_lock(ops_dir, cache, remote, capsys):
    assert run_pull(ops_dir, "--frozen", "--from", f"git+{remote}") == 2
    assert "there is no runwhen.lock" in capsys.readouterr().err


def test_frozen_installs_the_locked_version_not_the_newest(ops_dir, cache, remote, capsys):
    set_range(ops_dir, ">=0.1.0")
    assert run_pull(ops_dir, "--from", f"git+{remote}", "kubernetes@^0.1") == 0
    shutil.rmtree(cache)
    capsys.readouterr()
    assert run_pull(ops_dir, "--frozen") == 0  # from the lock's recorded source
    assert (cache / "kubernetes" / "0.1.0.tar").is_file()
    assert not (cache / "kubernetes" / "0.2.0.tar").exists()
    assert "kubernetes 0.1.0" in capsys.readouterr().out


def test_frozen_with_a_warm_cache_does_not_touch_the_network(ops_dir, cache, remote, capsys):
    assert run_pull(ops_dir, "--from", f"git+{remote}") == 0
    capsys.readouterr()
    assert run_pull(ops_dir, "--frozen", "--from", "git+file:///does/not/exist.git") == 0
    assert "(cached)" in capsys.readouterr().out


def test_frozen_does_not_write_the_lock(ops_dir, cache, remote):
    assert run_pull(ops_dir, "--from", f"git+{remote}") == 0
    lock = ops_dir / LOCK_FILE
    lock.write_text(lock.read_text() + "\n\n")  # a hand edit a rewrite would undo
    edited = lock.read_bytes()
    assert run_pull(ops_dir, "--frozen") == 0
    assert lock.read_bytes() == edited


def test_frozen_fails_when_the_lock_is_stale(ops_dir, cache, remote, capsys):
    assert run_pull(ops_dir, "--from", f"git+{remote}") == 0
    set_range(ops_dir, "^0.2")
    assert run_pull(ops_dir, "--frozen") == 2
    assert "range of kubernetes changed" in capsys.readouterr().err
    plugin = ops_dir / "plugin.yaml"
    plugin.write_text(plugin.read_text().replace('  kubernetes: "^0.2"\n', ""))
    assert run_pull(ops_dir, "--frozen") == 2
    assert "still pins kubernetes" in capsys.readouterr().err


def test_frozen_fails_when_the_lock_lacks_an_entry(ops_dir, cache, remote, capsys):
    (ops_dir / LOCK_FILE).write_text("lockfileVersion: 1\nrequires: {}\n")
    assert run_pull(ops_dir, "--frozen") == 2
    assert "lacks kubernetes" in capsys.readouterr().err


def test_frozen_rejects_an_archive_that_hashes_differently(ops_dir, cache, remote, capsys):
    assert run_pull(ops_dir, "--from", f"git+{remote}") == 0
    lock = ops_dir / LOCK_FILE
    entry = read_lock(ops_dir)["kubernetes"]
    lock.write_text(lock.read_text().replace(entry.archive_sha256, "sha256:" + "0" * 64))
    shutil.rmtree(cache)
    capsys.readouterr()
    assert run_pull(ops_dir, "--frozen") == 2
    assert "hashes to" in capsys.readouterr().err


# -- a registry ----------------------------------------------------------------------------


@responses.activate
def test_pull_from_a_registry_records_it_in_the_lock(ops_dir, cache):
    registry = "https://registry.example.test"
    entries = kube_entries()
    data = archive.build(entries)
    base = f"{registry}/v1/plugins/kubernetes"
    responses.get(f"{base}/versions", json={"items": [{"version": "0.1.0", "withdrawn": None}]})
    responses.get(
        f"{base}/versions/0.1.0",
        json={
            "compiled": compile_entries(entries),
            "archive": {"sha256": archive.archive_sha256(data), "size": len(data)},
        },
    )
    responses.get(f"{base}/versions/0.1.0/archive", body=data)
    assert run_pull(ops_dir, "--registry", registry, "--token", "tok") == 0
    assert read_lock(ops_dir)["kubernetes"].source == f"registry+{registry}"
    assert "tok" not in (ops_dir / LOCK_FILE).read_text()


# -- the offline editor schemas ------------------------------------------------------------


def test_pull_writes_self_contained_editor_schemas(ops_dir, cache, remote, capsys):
    assert run_pull(ops_dir, "--from", f"git+{remote}") == 0
    err = capsys.readouterr().err
    assert ".gitignore" in err
    schemas = ops_dir / ".runwhen" / "schemas"
    names = {p.name for p in schemas.iterdir()}
    assert names >= {
        f"{n}.schema.json"
        for n in (
            "tasks",
            "skill-frontmatter",
            "plugin",
            "integration",
            "types",
            "facets",
            "dependencies",
            "rule-frontmatter",
            "knowledge-frontmatter",
            "common",
        )
    }
    readme = (ops_dir / ".runwhen" / "README").read_text()
    assert "yaml-language-server" in readme and ".runwhen/schemas/tasks.schema.json" in readme
    for path in schemas.iterdir():
        text = path.read_text()
        doc = json.loads(text)
        assert "$id" not in doc and "https://schemas.runwhen.com" not in text, path.name
    # the relative $refs resolve between the copies: validate with them alone
    registry = Registry().with_resources(
        (
            f"file:///schemas/{p.name}",
            Resource.from_contents(
                {**json.loads(p.read_text()), "$id": f"file:///schemas/{p.name}"},
                default_specification=DRAFT202012,
            ),
        )
        for p in schemas.iterdir()
    )
    validator = Draft202012Validator(
        {"$ref": "file:///schemas/plugin.schema.json"}, registry=registry
    )
    plugin = load_yaml((ops_dir / "plugin.yaml").read_text())
    assert list(validator.iter_errors(plugin)) == []
    assert list(validator.iter_errors({**plugin, "version": "banana"}))
    tasks = Draft202012Validator({"$ref": "file:///schemas/tasks.schema.json"}, registry=registry)
    skill = load_yaml((ops_dir / "skills" / "papi-health" / "tasks.yaml").read_text())
    assert list(tasks.iter_errors(skill)) == []


def test_the_editor_schemas_are_refreshed_by_a_frozen_pull(ops_dir, cache, remote):
    assert run_pull(ops_dir, "--from", f"git+{remote}") == 0
    stale = ops_dir / ".runwhen" / "schemas" / "plugin.schema.json"
    stale.write_text("{}")
    assert run_pull(ops_dir, "--frozen") == 0
    assert "properties" in json.loads(stale.read_text())


# -- validate and compile read the lock ----------------------------------------------------


def test_validate_loads_the_locked_dependency_without_with(ops_dir, cache, remote, capsys):
    assert main(["validate", str(ops_dir)]) == 0  # no lock yet: the dependency is unchecked
    assert "W_TYPE_UNCHECKED" in capsys.readouterr().out
    assert run_pull(ops_dir, "--from", f"git+{remote}") == 0
    capsys.readouterr()
    assert main(["validate", str(ops_dir)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and "0 error(s), 0 warning(s)" in captured.err


def test_validate_finds_a_dependency_error_through_the_lock(ops_dir, cache, remote, capsys):
    assert run_pull(ops_dir, "--from", f"git+{remote}") == 0
    tasks = ops_dir / "skills/papi-recovery/tasks.yaml"
    tasks.write_text(tasks.read_text().replace("verbs: [patch]", "verb: [patch]"))
    capsys.readouterr()
    assert main(["validate", str(ops_dir)]) == 1
    assert "E_PERMISSION_SCHEMA" in capsys.readouterr().out


def test_a_cached_archive_that_differs_from_the_lock_exits_2(ops_dir, cache, remote, capsys):
    assert run_pull(ops_dir, "--from", f"git+{remote}") == 0
    (cache / "kubernetes" / "0.1.0.tar").write_bytes(
        archive.build([archive.TreeEntry("plugin.yaml", b"x: 1\n")])
    )
    capsys.readouterr()
    assert main(["validate", str(ops_dir)]) == 2
    err = capsys.readouterr().err
    assert "rwtask validate: the cached kubernetes@0.1.0" in err and "runwhen.lock" in err
    assert main(["compile", str(ops_dir), "--out", str(ops_dir / "dist")]) == 2
    assert "rwtask compile: the cached kubernetes@0.1.0" in capsys.readouterr().err


def test_a_locked_version_missing_from_the_cache_exits_2(ops_dir, cache, remote, capsys):
    assert run_pull(ops_dir, "--from", f"git+{remote}") == 0
    shutil.rmtree(cache)
    capsys.readouterr()
    assert main(["validate", str(ops_dir)]) == 2
    assert "not in the plugin cache" in capsys.readouterr().err


def test_an_explicit_with_wins_over_the_lock(ops_dir, cache, remote, tmp_path, capsys):
    assert run_pull(ops_dir, "--from", f"git+{remote}") == 0
    (cache / "kubernetes" / "0.1.0.tar").write_bytes(b"corrupt")  # the lock entry is unusable
    capsys.readouterr()
    kube = tmp_path / "kube"
    for entry in kube_entries():
        target = kube / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(entry.data)
    assert main(["validate", str(ops_dir), "--with", str(kube)]) == 0
    assert main(["validate", str(ops_dir)]) == 2


def test_compile_uses_the_lock_too(ops_dir, cache, remote, capsys):
    assert run_pull(ops_dir, "--from", f"git+{remote}") == 0
    out = ops_dir / "dist"
    capsys.readouterr()
    assert main(["compile", str(ops_dir), "--out", str(out)]) == 0
    assert "W_TYPE_UNCHECKED" not in capsys.readouterr().out
    assert (out / "runwhen-platform-ops-0.1.0.tar").is_file()


def test_a_malformed_lock_exits_2(ops_dir, capsys):
    (ops_dir / LOCK_FILE).write_text("lockfileVersion: 9\n")
    assert main(["validate", str(ops_dir)]) == 2
    assert "lockfileVersion must be 1" in capsys.readouterr().err


def test_the_lock_is_not_part_of_the_plugin(ops_dir, cache, remote):
    from runwhen_capability.plugin import load_dir

    assert run_pull(ops_dir, "--from", f"git+{remote}") == 0
    tree = load_dir(ops_dir)
    assert LOCK_FILE not in tree.files
    assert not [d for d in tree.diagnostics if d.code == "E_PATH_NOT_ALLOWED"]
