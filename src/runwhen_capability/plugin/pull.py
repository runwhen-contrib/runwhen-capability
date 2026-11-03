"""Pull a plugin version into the local plugin cache (AUT-1): `rwtask pull`'s engine.

    pulled = pull("kubernetes", "^0.2", source=GitSource("https://github.com/org/plugins"))
    pulled.version, pulled.archive_sha256         # 0.2.1, sha256:...
    pulled.compiled_path                          # <cache>/kubernetes/0.2.1.compiled.json

Three sources resolve a name and an npm-style range (semver.py) to one version and its
bytes:

    GitSource(url, subdir_template="plugins/{name}")   tags `<name>/v<semver>`; a shallow,
                                                       blob-filtered fetch of the tag, no
                                                       clone of the history
    RegistrySource(base_url, token=None)               the registry API (registry-api.openapi.yaml)
    DirSource(path)                                    a plugin directory (or a monorepo
                                                       with `plugins/<name>`), for tests

`pull` writes `<cache>/<name>/<version>.compiled.json` and `.tar` -- the layout
`load_dependency("name@version")` reads -- atomically, and refuses to replace a cached
archive that hashes differently (a version is immutable). A prerelease is only chosen
when the range names one (the npm rule), so no range means the highest release.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import requests

from ..contracts import TreeEntry, archive
from . import PluginInvalid
from .compile import compile_entries
from .deps import cache_root
from .semver import highest, parse_version, satisfies, version_key
from .tree import LOCK_FILE, load_dir

GIT_TIMEOUT_SECONDS = 120
HTTP_TIMEOUT_SECONDS = 30
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class PullError(ValueError):
    """A plugin version that cannot be resolved, fetched or cached."""


@dataclass(frozen=True)
class PulledPlugin:
    name: str
    version: str
    archive_sha256: str  # "sha256:<hex>"
    compiled_path: Path
    archive_path: Path
    source_ref: str  # where the bytes came from, e.g. git+<url>#kubernetes/v0.2.1


@dataclass(frozen=True)
class Fetched:
    """One version as a source hands it over: the archive, its compiled plugin and a
    human-readable reference to where it came from."""

    archive: bytes
    compiled: dict
    ref: str


class Source(Protocol):
    @property
    def spec(self) -> str:
        """The string `rwtask pull --from` / runwhen.lock use to name this source
        (never carries a credential)."""

    def versions(self, name: str) -> list[str]:
        """Every version of `name` the source has, in any order ([] if it has none)."""

    def fetch(self, name: str, version: str) -> Fetched:
        """The archive and compiled plugin of one of the versions `versions` listed."""


def pull(
    name: str, range_: str | None, *, source: Source, cache_dir: Path | None = None
) -> PulledPlugin:
    """Resolve `name` at `range_` (None: the highest release) through `source`, fetch it
    and write it to the plugin cache. Raises PullError."""
    if not _NAME.match(name):
        raise PullError(f"{name!r} is not a plugin name")
    available = source.versions(name)
    version = pick_version(name, available, range_)
    fetched = source.fetch(name, version)
    sha = archive.archive_sha256(fetched.archive)
    plugin = fetched.compiled.get("plugin") or {}
    if (plugin.get("name"), plugin.get("version")) != (name, version):
        raise PullError(
            f"{fetched.ref} holds {plugin.get('name')}@{plugin.get('version')}, "
            f"not {name}@{version}"
        )
    compiled_path, archive_path = _store(name, version, fetched, sha, cache_dir)
    return PulledPlugin(name, version, sha, compiled_path, archive_path, fetched.ref)


def pick_version(name: str, available: list[str], range_: str | None) -> str:
    """The highest of `available` satisfying `range_` ("*" when None)."""
    wanted = range_ or "*"
    try:
        matching = [v for v in available if satisfies(v, wanted)]
    except ValueError as exc:
        raise PullError(f"{name}@{wanted}: {exc}") from exc
    version = highest(matching)
    if version is None:
        have = ", ".join(sorted(available, key=version_key)) or "none"
        raise PullError(f"no version of {name!r} satisfies {wanted!r} (available: {have})")
    return version


def _store(
    name: str, version: str, fetched: Fetched, sha: str, cache_dir: Path | None
) -> tuple[Path, Path]:
    root = cache_root(cache_dir) / name
    archive_path = root / f"{version}.tar"
    compiled_path = root / f"{version}.compiled.json"
    if archive_path.is_file():
        have = archive.archive_sha256(archive_path.read_bytes())
        if have != sha:
            raise PullError(
                f"cache entry {archive_path} differs from {fetched.ref} "
                f"({have} cached, {sha} pulled); delete it to re-pull"
            )
        if compiled_path.is_file():
            return compiled_path, archive_path
    root.mkdir(parents=True, exist_ok=True)
    text = json.dumps(fetched.compiled, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    _write_atomic(archive_path, fetched.archive)
    _write_atomic(compiled_path, text.encode("utf-8"))
    return compiled_path, archive_path


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _publishable(path: str) -> bool:
    """What `rwtask compile` leaves out of an archive: root-level dotfiles and
    dot-directories, runwhen.lock and the root `tests/` directory."""
    top = path.split("/", 1)[0]
    return not top.startswith(".") and top not in ("tests", LOCK_FILE)


def _fetched_from_entries(entries: list[TreeEntry], ref: str) -> Fetched:
    try:
        compiled = compile_entries(entries)
        data = archive.build(entries)
    except PluginInvalid as exc:
        raise PullError(f"{ref} is not a valid plugin: {exc}") from exc
    except archive.ArchiveError as exc:
        raise PullError(f"{ref}: {exc}") from exc
    return Fetched(data, compiled, ref)


def _without_credentials(url: str) -> str:
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


# -- git ---------------------------------------------------------------------------------


class GitSource:
    """Plugins in a git repository, one tag `<name>/v<semver>` per version and the plugin
    at `subdir_template` (`{name}` filled in) in the tagged tree. Needs `git` on PATH."""

    def __init__(self, url: str, subdir_template: str = "plugins/{name}") -> None:
        self.url = url
        self.subdir_template = subdir_template
        self._tags: dict[str, dict[str, str]] = {}

    @property
    def spec(self) -> str:
        return f"git+{_without_credentials(self.url)}"

    def _subdir(self, name: str) -> str:
        subdir = self.subdir_template.format(name=name).strip("/")
        return "" if subdir == "." else subdir

    def versions(self, name: str) -> list[str]:
        return sorted(self._tag_map(name))

    def _tag_map(self, name: str) -> dict[str, str]:
        """{version: tag} from `git ls-remote --tags`; tags that are not `<name>/v<semver>`
        are ignored."""
        if name not in self._tags:
            prefix = f"refs/tags/{name}/v"
            out = _git(
                ["ls-remote", "--tags", self.url, f"{prefix}*"], cwd=None, what="list tags"
            ).decode("utf-8", "replace")
            found: dict[str, str] = {}
            for line in out.splitlines():
                ref = line.partition("\t")[2].removesuffix("^{}")
                if not ref.startswith(prefix):
                    continue
                version = ref[len(prefix) :]
                try:
                    parse_version(version)
                except ValueError:
                    continue
                found[version] = ref.removeprefix("refs/tags/")
            self._tags[name] = found
        return self._tags[name]

    def fetch(self, name: str, version: str) -> Fetched:
        tag = self._tag_map(name).get(version)
        if tag is None:
            raise PullError(f"{self.spec} has no tag {name}/v{version}")
        ref = f"{self.spec}#{tag}"
        subdir = self._subdir(name)
        with tempfile.TemporaryDirectory(prefix="rwtask-pull-") as tmp:
            entries = self._read_tag(Path(tmp), tag, subdir, ref)
        return _fetched_from_entries(entries, ref)

    def _read_tag(self, work: Path, tag: str, subdir: str, ref: str) -> list[TreeEntry]:
        """The files of `subdir` at `tag`, modes from git's own record (100755 is
        executable). Shallow, blob-less fetch of the one tag, then a sparse checkout of the
        subdirectory, which makes git download exactly its blobs in one batch."""
        _git(["init", "-q"], cwd=work, what="init")
        _git(["remote", "add", "origin", self.url], cwd=work, what="init")
        if subdir:
            _git(["sparse-checkout", "set", "--cone", subdir], cwd=work, what="init")
        _git(
            ["fetch", "-q", "--no-tags", "--depth", "1", "--filter=blob:none", "origin"]
            + [f"refs/tags/{tag}"],
            cwd=work,
            what=f"fetch {tag}",
        )
        _git(["checkout", "-q", "--detach", "FETCH_HEAD"], cwd=work, what=f"check out {tag}")
        listing = _git(
            ["ls-tree", "-r", "-z", "FETCH_HEAD", "--", subdir or "."], cwd=work, what="list files"
        )
        strip = f"{subdir}/" if subdir else ""
        entries = []
        for record in listing.split(b"\0"):
            if not record:
                continue
            meta, _, raw_path = record.partition(b"\t")
            mode, kind, _oid = meta.decode().split()
            full = raw_path.decode("utf-8", "surrogateescape")
            path = full[len(strip) :]
            if not _publishable(path):
                continue
            if kind != "blob" or mode not in ("100644", "100755"):
                raise PullError(
                    f"{ref}: {path} is a {'symlink' if mode == '120000' else kind}, "
                    "which a plugin cannot contain"
                )
            entries.append(TreeEntry(path, (work / full).read_bytes(), mode == "100755"))
        if not entries:
            raise PullError(f"{ref} has no files under {subdir or '/'!r}")
        return entries


def _git(args: list[str], *, cwd: Path | None, what: str) -> bytes:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],  # noqa: S607
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PullError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise PullError(f"git {what} timed out after {GIT_TIMEOUT_SECONDS}s") from exc
    if proc.returncode != 0:
        detail = _without_credentials(proc.stderr.decode("utf-8", "replace").strip())
        raise PullError(f"git {what} failed: {detail or f'exit {proc.returncode}'}")
    return proc.stdout


# -- registry ----------------------------------------------------------------------------


class RegistrySource:
    """A plugin registry (contracts/registry-api.openapi.yaml). Every archive is verified
    against the sha256 the registry published for it."""

    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    @property
    def spec(self) -> str:
        return f"registry+{_without_credentials(self.base_url)}"

    def _get(self, path: str, what: str) -> requests.Response:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            response = requests.get(
                f"{self.base_url}{path}", headers=headers, timeout=HTTP_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            raise PullError(f"registry {self.base_url}: cannot {what}: {exc}") from exc
        if response.status_code != 200:
            detail = ""
            try:
                body = response.json()
                detail = body.get("detail") or body.get("title") or ""
            except (ValueError, AttributeError):
                pass
            raise PullError(
                f"registry {self.base_url}: {what} answered {response.status_code}"
                + (f": {detail}" if detail else "")
            )
        return response

    def versions(self, name: str) -> list[str]:
        try:
            response = self._get(f"/v1/plugins/{name}/versions", f"list versions of {name}")
        except PullError as exc:
            if "answered 404" in str(exc):
                return []
            raise
        try:
            items = response.json()["items"]
            return [item["version"] for item in items if not item.get("withdrawn")]
        except (ValueError, KeyError, TypeError) as exc:
            raise PullError(f"registry {self.base_url}: malformed version list: {exc}") from exc

    def fetch(self, name: str, version: str) -> Fetched:
        ref = f"{self.spec}#{name}@{version}"
        base = f"/v1/plugins/{name}/versions/{version}"
        try:
            entry = self._get(base, f"read {name}@{version}").json()
            compiled, sha = entry["compiled"], entry["archive"]["sha256"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PullError(f"{ref}: malformed index entry: {exc}") from exc
        data = self._get(f"{base}/archive", f"download {name}@{version}").content
        try:
            archive.verify(data, sha)
        except archive.ArchiveError as exc:
            raise PullError(f"{ref}: {exc}") from exc
        return Fetched(data, compiled, ref)


# -- a directory -------------------------------------------------------------------------


class DirSource:
    """A plugin directory (has plugin.yaml), or a monorepo directory holding it at
    `plugins/<name>` or `<name>`. It offers the one version plugin.yaml declares."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def spec(self) -> str:
        return str(self.path)

    def _plugin_dir(self, name: str) -> Path | None:
        for candidate in (self.path, self.path / "plugins" / name, self.path / name):
            if (candidate / "plugin.yaml").is_file():
                tree = load_dir(candidate)
                if (tree.plugin or {}).get("name") == name:
                    return candidate
        return None

    def versions(self, name: str) -> list[str]:
        directory = self._plugin_dir(name)
        if directory is None:
            return []
        version = (load_dir(directory).plugin or {}).get("version")
        return [version] if isinstance(version, str) else []

    def fetch(self, name: str, version: str) -> Fetched:
        directory = self._plugin_dir(name)
        if directory is None:
            raise PullError(f"{self.path} has no plugin {name!r}")
        tree = load_dir(directory)
        entries = [TreeEntry(p, data, p in tree.executable) for p, data in tree.files.items()]
        return _fetched_from_entries(entries, f"{directory}")


# -- spec -> source ----------------------------------------------------------------------

DEFAULT_SOURCE = "git+https://github.com/runwhen-contrib/runwhen-plugins"


def parse_source(spec: str, *, token: str | None = None) -> Source:
    """`git+<url>` is a GitSource, `registry+<url>` a RegistrySource (with `token`),
    anything else a DirSource at that path."""
    if spec.startswith("git+"):
        return GitSource(spec[4:])
    if spec.startswith("registry+"):
        return RegistrySource(spec[len("registry+") :], token)
    return DirSource(spec)


__all__ = [
    "DEFAULT_SOURCE",
    "DirSource",
    "Fetched",
    "GitSource",
    "PullError",
    "PulledPlugin",
    "RegistrySource",
    "Source",
    "parse_source",
    "pick_version",
    "pull",
]
