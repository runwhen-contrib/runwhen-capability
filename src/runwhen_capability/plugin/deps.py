"""Required integrations for `rwtask validate|compile --with SPEC` (INT-2).

A plugin that `requires` or `extends` another cannot check that plugin's types,
credential types and permission schema on its own. `load_dependency` turns a spec into
the pieces `validate_tree(tree, dependencies={name: dep})` needs:

    load_dependency("../kubernetes")                 # a plugin directory (compiled here)
    load_dependency("dist/kubernetes-0.2.0.compiled.json")
    load_dependency("kubernetes@0.2.0")              # the local plugin cache
    load_dependency("kubernetes")                    # the highest version in the cache

The cache is `$RUNWHEN_PLUGIN_CACHE` (default `~/.cache/runwhen/plugins`), laid out as
`<name>/<version>.compiled.json`; `rwtask pull` (AUT-1) fills it later.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..contracts import ContractError, check
from . import PluginInvalid
from .compile import compile_tree
from .semver import highest, parse_version
from .tree import load_dir
from .validate import validate_tree

CACHE_ENV = "RUNWHEN_PLUGIN_CACHE"
DEFAULT_CACHE = "~/.cache/runwhen/plugins"

_NAME_VERSION = re.compile(r"^([a-z0-9]+(?:-[a-z0-9]+)*)(?:@(.+))?$")
_SUFFIX = ".compiled.json"


class DependencyError(ValueError):
    """A `--with` spec that does not resolve to a valid compiled plugin."""


@dataclass(frozen=True)
class CompiledDependency:
    """What validation needs of a required plugin: its identity, its merged
    integration definition and its permission schema (None when it has none)."""

    name: str
    version: str
    integration: dict | None
    permission_schema: dict | None

    def types(self) -> set[str]:
        """The type names (without the platform prefix) the integration declares."""
        return set((self.integration or {}).get("types") or {})

    def credential_types(self) -> set[str]:
        """The full credential type names (`kubernetes/kubeconfig`) it declares."""
        return set((self.integration or {}).get("credentialTypes") or {})

    @property
    def platform(self) -> str:
        """The integration's platform, falling back to the plugin name."""
        platform = (self.integration or {}).get("platform")
        return platform if isinstance(platform, str) else self.name


def from_compiled(doc: dict) -> CompiledDependency:
    """The dependency view of a compiled-plugin document (checked against its schema)."""
    try:
        check("compiled-plugin", doc)
    except ContractError as exc:
        raise DependencyError(f"not a valid compiled plugin: {exc}") from exc
    integration = doc.get("integration") or None
    return CompiledDependency(
        name=doc["plugin"]["name"],
        version=doc["plugin"]["version"],
        integration=integration["definition"] if integration else None,
        permission_schema=integration.get("permissionSchema") if integration else None,
    )


def load_dependency(spec: str, *, cache_dir: Path | None = None) -> CompiledDependency:
    """Resolve a `--with` spec (see the module docstring); DependencyError if it does
    not name a valid compiled plugin."""
    path = Path(spec).expanduser()
    if (path / "plugin.yaml").is_file():
        return _from_directory(path)
    if spec.endswith(_SUFFIX):
        return _from_file(path)
    match = _NAME_VERSION.match(spec)
    if match is None:
        why = "has no plugin.yaml" if path.is_dir() else "is not a directory or a file"
        raise DependencyError(
            f"{spec!r} {why}; expected a plugin directory, a *{_SUFFIX} file or name[@version]"
        )
    return _from_cache(match.group(1), match.group(2), cache_dir)


def _from_directory(path: Path) -> CompiledDependency:
    tree = load_dir(path)
    errors = [d for d in validate_tree(tree) if d.severity == "error"]
    if errors:
        lines = "\n".join(f"  {d.file}: {d.code} {d.message}" for d in errors)
        raise DependencyError(f"{path} is not a valid plugin ({len(errors)} error(s)):\n{lines}")
    try:
        return from_compiled(compile_tree(tree))
    except PluginInvalid as exc:
        raise DependencyError(f"{path} does not compile: {exc}") from exc


def _from_file(path: Path) -> CompiledDependency:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DependencyError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except ValueError as exc:
        raise DependencyError(f"{path} is not valid JSON: {exc}") from exc
    try:
        return from_compiled(doc)
    except DependencyError as exc:
        raise DependencyError(f"{path}: {exc}") from exc


def _from_cache(name: str, version: str | None, cache_dir: Path | None) -> CompiledDependency:
    root = (cache_dir or Path(os.environ.get(CACHE_ENV, DEFAULT_CACHE))).expanduser() / name
    if version is None:
        present = (
            [p.name[: -len(_SUFFIX)] for p in root.glob(f"*{_SUFFIX}")] if root.is_dir() else []
        )
        usable = []
        for candidate in present:
            try:
                parse_version(candidate)
            except ValueError:
                continue
            usable.append(candidate)
        version = highest(usable)
        if version is None:
            raise DependencyError(f"no version of {name!r} in the plugin cache {root}")
    else:
        try:
            parse_version(version)
        except ValueError as exc:
            raise DependencyError(f"{name}@{version}: {exc}") from exc
    path = root / f"{version}{_SUFFIX}"
    if not path.is_file():
        raise DependencyError(f"{name}@{version} is not in the plugin cache (expected {path})")
    dep = _from_file(path)
    if (dep.name, dep.version) != (name, version):
        raise DependencyError(
            f"{path} holds {dep.name}@{dep.version}, not {name}@{version}: cache is corrupt"
        )
    return dep
