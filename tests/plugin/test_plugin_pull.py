"""Pull a plugin version from git, a registry or a directory into the plugin cache (AUT-1):
plugin/pull.py. No network: git runs against a local bare repository."""

from __future__ import annotations

import json
import shutil

import pytest
import responses
from _git_remote import git, make_remote, write_version
from _plugin_fixtures import KUBE_MINI, kube_entries

from runwhen_capability.contracts import archive
from runwhen_capability.plugin import compile_entries, load_dependency, load_dir
from runwhen_capability.plugin.pull import (
    DirSource,
    GitSource,
    PullError,
    RegistrySource,
    parse_source,
    pull,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture(scope="module")
def remote(tmp_path_factory):
    return make_remote(tmp_path_factory.mktemp("remote"))


@pytest.fixture
def cache(tmp_path):
    return tmp_path / "cache"


# -- git: version resolution ---------------------------------------------------------------


def test_the_git_source_lists_the_tagged_versions(remote):
    assert GitSource(remote).versions("kubernetes") == ["0.1.0", "0.2.0", "0.3.0-rc.1"]
    assert GitSource(remote).versions("nothing") == []


@pytest.mark.parametrize(
    ("range_", "version"),
    [
        ("^0.1", "0.1.0"),
        ("^0.2", "0.2.0"),
        (None, "0.2.0"),  # no range: the highest release, never the rc
        ("*", "0.2.0"),
        (">=0.1.0", "0.2.0"),
        (">=0.2.0-rc.0", "0.2.0"),  # the rc is of 0.3.0, a different tuple: not eligible
        (">=0.3.0-rc.0", "0.3.0-rc.1"),
        ("^0.3.0-rc.1", "0.3.0-rc.1"),
        ("=0.1.0", "0.1.0"),
    ],
)
def test_a_range_resolves_to_the_highest_match(remote, cache, range_, version):
    pulled = pull("kubernetes", range_, source=GitSource(remote), cache_dir=cache)
    assert pulled.version == version
    assert pulled.source_ref.endswith(f"#kubernetes/v{version}")


@pytest.mark.parametrize("range_", ["^0.4", "^1", "<0.1.0"])
def test_no_matching_version_lists_what_there_is(remote, cache, range_):
    with pytest.raises(PullError, match=r"no version of 'kubernetes' satisfies .*0\.1\.0, 0\.2\.0"):
        pull("kubernetes", range_, source=GitSource(remote), cache_dir=cache)
    assert not cache.exists()


def test_a_bad_range_or_name_is_a_pull_error(remote, cache):
    with pytest.raises(PullError, match="kubernetes@banana"):
        pull("kubernetes", "banana", source=GitSource(remote), cache_dir=cache)
    with pytest.raises(PullError, match="not a plugin name"):
        pull("../x", None, source=GitSource(remote), cache_dir=cache)


def test_an_unreachable_repository_is_a_pull_error(tmp_path, cache):
    with pytest.raises(PullError, match="git list tags failed"):
        pull("kubernetes", None, source=GitSource(f"file://{tmp_path}/nope.git"), cache_dir=cache)


# -- git: what lands in the cache ----------------------------------------------------------


def test_the_cache_gets_a_compiled_json_and_an_archive(remote, cache):
    pulled = pull("kubernetes", "^0.2", source=GitSource(remote), cache_dir=cache)
    assert pulled.compiled_path == cache / "kubernetes" / "0.2.0.compiled.json"
    assert pulled.archive_path == cache / "kubernetes" / "0.2.0.tar"
    data = pulled.archive_path.read_bytes()
    assert pulled.archive_sha256 == archive.archive_sha256(data)
    compiled = json.loads(pulled.compiled_path.read_text())
    assert compiled["plugin"]["version"] == "0.2.0"
    text = pulled.compiled_path.read_text()
    assert text == json.dumps(compiled, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert sorted(p.name for p in (cache / "kubernetes").iterdir()) == [
        "0.2.0.compiled.json",
        "0.2.0.tar",
    ]
    dep = load_dependency("kubernetes@0.2.0", cache_dir=cache)
    assert (dep.name, dep.version) == ("kubernetes", "0.2.0")


def test_the_archive_is_what_compile_would_build_from_the_directory(remote, cache, tmp_path):
    pulled = pull("kubernetes", "^0.2", source=GitSource(remote), cache_dir=cache)
    local = tmp_path / "local"
    write_version(local, "0.2.0")
    tree = load_dir(local)
    expected = archive.build(
        archive.TreeEntry(p, d, p in tree.executable) for p, d in tree.files.items()
    )
    assert pulled.archive_path.read_bytes() == expected  # no tests/, no .github/


def test_exec_bits_survive_the_trip(remote, cache):
    pulled = pull("kubernetes", "^0.1", source=GitSource(remote), cache_dir=cache)
    entries = {e.path: e for e in archive.read(pulled.archive_path.read_bytes())}
    assert entries["skills/k8s-base/scripts/kubectl.sh"].executable
    assert not entries["skills/k8s-base/scripts/discover.py"].executable
    assert not any(p.startswith(("tests/", ".github/")) for p in entries)


def test_pulling_twice_is_idempotent(remote, cache):
    first = pull("kubernetes", "^0.1", source=GitSource(remote), cache_dir=cache)
    stamp = first.archive_path.stat().st_mtime_ns
    second = pull("kubernetes", "^0.1", source=GitSource(remote), cache_dir=cache)
    assert second == first
    assert second.archive_path.stat().st_mtime_ns == stamp


def test_a_cache_entry_that_differs_is_an_error(remote, cache):
    first = pull("kubernetes", "^0.1", source=GitSource(remote), cache_dir=cache)
    first.archive_path.write_bytes(archive.build([archive.TreeEntry("plugin.yaml", b"x: 1\n")]))
    with pytest.raises(PullError, match="differs.*delete it to re-pull"):
        pull("kubernetes", "^0.1", source=GitSource(remote), cache_dir=cache)


def test_a_missing_compiled_json_is_rewritten(remote, cache):
    first = pull("kubernetes", "^0.1", source=GitSource(remote), cache_dir=cache)
    first.compiled_path.unlink()
    assert pull("kubernetes", "^0.1", source=GitSource(remote), cache_dir=cache) == first
    assert first.compiled_path.is_file()


def test_a_tag_whose_plugin_has_another_version_is_rejected(tmp_path, cache):
    base = tmp_path / "r"
    url = make_remote(base)
    work = base / "work"
    write_version(work / "plugins" / "kubernetes", "0.9.9")  # content says 0.9.9 ...
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "mislabelled", cwd=work)
    git("tag", "kubernetes/v0.5.0", cwd=work)  # ... under the 0.5.0 tag
    git("push", "-q", "origin", "--tags", cwd=work)
    with pytest.raises(PullError, match=r"holds kubernetes@0\.9\.9, not kubernetes@0\.5\.0"):
        pull("kubernetes", "^0.5", source=GitSource(url), cache_dir=cache)


def test_a_tag_without_the_subdirectory_is_a_pull_error(remote, cache):
    with pytest.raises(PullError, match="no files under"):
        pull("kubernetes", "^0.1", source=GitSource(remote, "elsewhere/{name}"), cache_dir=cache)


def test_a_symlink_in_the_plugin_is_a_pull_error(tmp_path, cache):
    base = tmp_path / "r"
    url = make_remote(base)
    work = base / "work"
    (work / "plugins" / "kubernetes" / "link").symlink_to("plugin.yaml")
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "link", cwd=work)
    git("tag", "kubernetes/v0.6.0", cwd=work)
    git("push", "-q", "origin", "--tags", cwd=work)
    with pytest.raises(PullError, match="symlink"):
        pull("kubernetes", "^0.6", source=GitSource(url), cache_dir=cache)


def test_a_subdir_template_of_the_repo_root(tmp_path, cache):
    base = tmp_path / "r"
    (base / "work").mkdir(parents=True)
    (base / "bare.git").mkdir()
    work = base / "work"
    git("init", "-q", "--bare", cwd=base / "bare.git")
    git("init", "-q", "-b", "main", cwd=work)
    write_version(base / "tree", "0.1.0")  # write_version replaces its target: keep .git out
    shutil.copytree(base / "tree", work, dirs_exist_ok=True)
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "root", cwd=work)
    git("tag", "kubernetes/v0.1.0", cwd=work)
    git("remote", "add", "origin", str(base / "bare.git"), cwd=work)
    git("push", "-q", "origin", "main", "--tags", cwd=work)
    pulled = pull(
        "kubernetes", None, source=GitSource(f"file://{base}/bare.git", "."), cache_dir=cache
    )
    assert pulled.version == "0.1.0"


def test_the_spec_never_carries_a_credential():
    assert GitSource("https://user:tok@example.com/org/repo.git").spec == (
        "git+https://example.com/org/repo.git"
    )
    assert RegistrySource("https://reg.example.com/", "secret").spec == (
        "registry+https://reg.example.com"
    )
    assert isinstance(parse_source("git+https://x/y"), GitSource)
    assert isinstance(parse_source("registry+https://x"), RegistrySource)
    assert isinstance(parse_source("../plugins"), DirSource)


# -- a directory ---------------------------------------------------------------------------


def test_a_dir_source_offers_its_one_version(cache):
    pulled = pull("kubernetes", "^0.1", source=DirSource(KUBE_MINI), cache_dir=cache)
    assert pulled.version == "0.1.0"
    entries = {e.path: e for e in archive.read(pulled.archive_path.read_bytes())}
    assert entries["skills/k8s-base/scripts/kubectl.sh"].executable
    with pytest.raises(PullError, match="no version of 'kubernetes' satisfies '\\^0.2'"):
        pull("kubernetes", "^0.2", source=DirSource(KUBE_MINI), cache_dir=cache)
    with pytest.raises(PullError, match="no version of 'other'"):
        pull("other", None, source=DirSource(KUBE_MINI), cache_dir=cache)


def test_a_dir_source_finds_a_plugin_in_a_monorepo(tmp_path, cache):
    shutil.copytree(KUBE_MINI, tmp_path / "plugins" / "kubernetes")
    assert pull("kubernetes", None, source=DirSource(tmp_path), cache_dir=cache).version == "0.1.0"


# -- a registry ----------------------------------------------------------------------------

REGISTRY = "https://registry.example.test"


def registry_mocks(rsps, *, sha=None, data=None):
    entries = kube_entries()
    data = data if data is not None else archive.build(entries)
    sha = sha or archive.archive_sha256(data)
    compiled = compile_entries(entries)
    base = f"{REGISTRY}/v1/plugins/kubernetes"
    rsps.get(
        f"{base}/versions",
        json={
            "items": [
                {"name": "kubernetes", "version": "0.1.0", "withdrawn": None},
                {"name": "kubernetes", "version": "0.1.1", "withdrawn": {"at": "x", "reason": "y"}},
            ]
        },
    )
    rsps.get(
        f"{base}/versions/0.1.0",
        json={"compiled": compiled, "archive": {"sha256": sha, "size": len(data)}},
    )
    rsps.get(f"{base}/versions/0.1.0/archive", body=data)
    return data


@responses.activate
def test_a_registry_pull_verifies_the_hash_and_skips_withdrawn_versions(cache):
    data = registry_mocks(responses)
    source = RegistrySource(REGISTRY + "/", token="tok")
    assert source.versions("kubernetes") == ["0.1.0"]
    pulled = pull("kubernetes", "*", source=source, cache_dir=cache)
    assert pulled.version == "0.1.0"
    assert pulled.archive_path.read_bytes() == data
    assert pulled.archive_sha256 == archive.archive_sha256(data)
    assert pulled.source_ref == f"registry+{REGISTRY}#kubernetes@0.1.0"
    assert responses.calls[0].request.headers["Authorization"] == "Bearer tok"
    assert load_dependency("kubernetes@0.1.0", cache_dir=cache).version == "0.1.0"


@responses.activate
def test_a_registry_archive_that_does_not_match_its_hash_is_rejected(cache):
    registry_mocks(responses, sha="sha256:" + "0" * 64)
    with pytest.raises(PullError, match="E_HASH_MISMATCH"):
        pull("kubernetes", None, source=RegistrySource(REGISTRY), cache_dir=cache)
    assert not cache.exists()


@responses.activate
def test_a_registry_without_the_plugin_has_no_versions(cache):
    responses.get(f"{REGISTRY}/v1/plugins/kubernetes/versions", status=404, json={"title": "no"})
    with pytest.raises(PullError, match="no version of 'kubernetes' satisfies '\\*' .*none"):
        pull("kubernetes", None, source=RegistrySource(REGISTRY), cache_dir=cache)


@responses.activate
def test_a_registry_error_names_the_status(cache):
    responses.get(
        f"{REGISTRY}/v1/plugins/kubernetes/versions",
        status=503,
        json={"detail": "try later"},
    )
    with pytest.raises(PullError, match="answered 503: try later"):
        RegistrySource(REGISTRY).versions("kubernetes")
