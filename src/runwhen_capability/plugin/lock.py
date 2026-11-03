"""`runwhen.lock`: the exact versions `rwtask pull` resolved for a plugin's `requires` and
`extends` (AUT-1). It sits next to plugin.yaml and is committed.

    lockfileVersion: 1
    requires:
      kubernetes:
        archiveSha256: sha256:...
        range: ^0.2
        source: git+https://github.com/runwhen-contrib/runwhen-plugins
        version: 0.2.1

`range` is plugin.yaml's range when the lock was written (a changed range makes the lock
stale); `archiveSha256` is the identity of the version, checked against the plugin cache
every time the lock is used. `load_locked_dependencies` is what `rwtask validate|compile`
read when no `--with` says otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..contracts import archive, load_yaml
from .deps import CompiledDependency, DependencyError, cache_root, load_dependency
from .semver import parse_version

LOCK_FILE = "runwhen.lock"
LOCKFILE_VERSION = 1
_SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEADER = "# Written by `rwtask pull`; commit it. `rwtask pull --frozen` installs exactly this.\n"


class LockError(ValueError):
    """A runwhen.lock that is malformed, stale or does not match the plugin cache."""


@dataclass(frozen=True)
class LockedPlugin:
    range: str
    version: str
    archive_sha256: str
    source: str


def declared_dependencies(plugin: dict) -> dict[str, str]:
    """{name: range} of the plugins a plugin.yaml document `requires` or `extends`
    (`requires.platform` is the platform's version, not a plugin)."""
    found: dict[str, str] = {}
    for key in ("extends", "requires"):
        section = plugin.get(key)
        if isinstance(section, dict):
            found.update(
                {n: r for n, r in section.items() if n != "platform" and isinstance(r, str)}
            )
    return found


def lock_problems(locked: dict[str, LockedPlugin] | None, declared: dict[str, str]) -> str | None:
    """Why `locked` cannot stand for plugin.yaml's `declared` dependencies (None if it can):
    no lock, an entry missing or left over, or a range that changed since the lock."""
    if locked is None:
        return f"there is no {LOCK_FILE}"
    missing = sorted(set(declared) - set(locked))
    if missing:
        return f"{LOCK_FILE} lacks {', '.join(missing)}"
    extra = sorted(set(locked) - set(declared))
    if extra:
        return f"{LOCK_FILE} still pins {', '.join(extra)}, which plugin.yaml no longer requires"
    changed = sorted(n for n, r in declared.items() if locked[n].range != r)
    if changed:
        return f"{LOCK_FILE} is out of date: the range of {', '.join(changed)} changed"
    return None


def read_lock(plugin_dir: Path) -> dict[str, LockedPlugin] | None:
    """The locked plugins of `plugin_dir`, or None if it has no runwhen.lock. LockError
    when the file cannot be understood."""
    path = Path(plugin_dir) / LOCK_FILE
    if not path.is_file():
        return None
    try:
        doc = load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LockError(f"{path}: cannot read: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("lockfileVersion") != LOCKFILE_VERSION:
        raise LockError(
            f"{path}: not a runwhen.lock this rwtask understands "
            f"(lockfileVersion must be {LOCKFILE_VERSION})"
        )
    requires = doc.get("requires") or {}
    if not isinstance(requires, dict):
        raise LockError(f"{path}: requires must be a mapping")
    locked: dict[str, LockedPlugin] = {}
    for name, entry in requires.items():
        where = f"{path}: requires.{name}"
        if not isinstance(entry, dict) or set(entry) != {
            "range",
            "version",
            "archiveSha256",
            "source",
        }:
            raise LockError(f"{where} needs exactly range, version, archiveSha256 and source")
        if not all(isinstance(v, str) for v in entry.values()):
            raise LockError(f"{where}: every field must be a string")
        try:
            parse_version(entry["version"])
        except ValueError as exc:
            raise LockError(f"{where}: {exc}") from exc
        if not _SHA.match(entry["archiveSha256"]):
            raise LockError(f"{where}: archiveSha256 must be sha256:<64 hex digits>")
        locked[str(name)] = LockedPlugin(
            entry["range"], entry["version"], entry["archiveSha256"], entry["source"]
        )
    return locked


def render_lock(locked: dict[str, LockedPlugin]) -> str:
    """The deterministic text of a runwhen.lock (sorted keys, block style)."""
    doc = {
        "lockfileVersion": LOCKFILE_VERSION,
        "requires": {
            name: {
                "range": entry.range,
                "version": entry.version,
                "archiveSha256": entry.archive_sha256,
                "source": entry.source,
            }
            for name, entry in locked.items()
        },
    }
    body = yaml.safe_dump(doc, sort_keys=True, default_flow_style=False, allow_unicode=True)
    return _HEADER + body


def write_lock(plugin_dir: Path, locked: dict[str, LockedPlugin]) -> bool:
    """Write runwhen.lock; False (and no write) when it already has exactly this text."""
    path = Path(plugin_dir) / LOCK_FILE
    text = render_lock(locked)
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def cached_archive_matches(name: str, entry: LockedPlugin, cache_dir: Path | None = None) -> bool:
    """Whether the plugin cache holds `entry`'s archive, unmodified."""
    tar = cache_root(cache_dir) / name / f"{entry.version}.tar"
    return tar.is_file() and archive.archive_sha256(tar.read_bytes()) == entry.archive_sha256


def load_locked_dependencies(
    plugin_dir: Path, *, skip: set[str] = frozenset(), cache_dir: Path | None = None
) -> dict[str, CompiledDependency] | None:
    """The dependencies `plugin_dir`'s runwhen.lock pins, loaded from the plugin cache, or
    None when there is no lock. `skip` names plugins the caller supplies itself. LockError
    for an entry whose cached archive is missing or differs from the lock's hash."""
    locked = read_lock(plugin_dir)
    if locked is None:
        return None
    dependencies: dict[str, CompiledDependency] = {}
    for name, entry in locked.items():
        if name in skip:
            continue
        tar = cache_root(cache_dir) / name / f"{entry.version}.tar"
        if not tar.is_file():
            raise LockError(
                f"{LOCK_FILE} pins {name}@{entry.version} but it is not in the plugin cache "
                f"(expected {tar}); run `rwtask pull --frozen`"
            )
        if not cached_archive_matches(name, entry, cache_dir):
            raise LockError(
                f"the cached {name}@{entry.version} ({tar}) does not match {LOCK_FILE} "
                f"({entry.archive_sha256}); delete it and run `rwtask pull --frozen`"
            )
        try:
            dependencies[name] = load_dependency(f"{name}@{entry.version}", cache_dir=cache_dir)
        except DependencyError as exc:
            raise LockError(f"{LOCK_FILE}: {name}@{entry.version}: {exc}") from exc
    return dependencies
