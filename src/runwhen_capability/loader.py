"""Loads a capability directory: its manifest.yaml (for the capability id)
and its tasks.py (for the registered setup/task functions).
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import yaml

from .decorators import Registry, _current_registry
from .errors import CapabilityLoadError


class LoadedCapability:
    def __init__(
        self,
        capability_dir: Path,
        capability_id: str,
        registry: Registry,
        execution_mode: str = "stateless",
    ) -> None:
        self.dir = capability_dir
        self.capability_id = capability_id
        self.registry = registry
        # From the manifest's `execution.mode`: "stateless" | "stateful".
        # Drives serve.py's scope lifecycle -- wipe every request vs. keep
        # warm, LRU-bounded, across requests sharing a scopeId. Defaults to "stateless" for
        # manifests/fixtures that omit `execution` entirely.
        self.execution_mode = execution_mode


def load_manifest(capability_dir: Path) -> dict:
    manifest_path = capability_dir / "manifest.yaml"
    if not manifest_path.is_file():
        raise CapabilityLoadError(f"no manifest.yaml in {capability_dir}")
    with manifest_path.open() as f:
        manifest = yaml.safe_load(f)
    if not isinstance(manifest, dict) or "capability" not in manifest:
        raise CapabilityLoadError(f"{manifest_path}: missing required 'capability' key")
    return manifest


def load_capability(capability_dir: Path) -> LoadedCapability:
    capability_dir = Path(capability_dir)
    manifest = load_manifest(capability_dir)

    tasks_path = capability_dir / "tasks.py"
    if not tasks_path.is_file():
        raise CapabilityLoadError(f"no tasks.py in {capability_dir}")

    # A capability's tasks.py is loaded by path, so it never belongs to a
    # package -- `import adapters` or `import tools.ruff` inside it has
    # nothing to resolve against. Put the capability directory on sys.path
    # HERE, once, rather than making every capability open with its own
    # sys.path juggling. Only the capability dir: a subpackage like `tools/`
    # must be reachable by ONE name, or the same file gets imported twice as
    # two distinct module objects.
    resolved = str(capability_dir.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)

    registry = Registry()
    token = _current_registry.set(registry)
    try:
        module_name = f"runwhen_capability_tasks_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, tasks_path)
        if spec is None or spec.loader is None:
            raise CapabilityLoadError(f"could not load {tasks_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        _current_registry.reset(token)

    execution_mode = (manifest.get("execution") or {}).get("mode", "stateless")
    return LoadedCapability(capability_dir, manifest["capability"], registry, execution_mode)


def discover_capability_dir(base: Path = Path("capabilities")) -> Path:
    """Auto-discovers the single capability a capability image ships (image
    == capability, 1:1). Used by `rwtask serve`, which is not told a
    capability directory on the command line -- the image already is one."""
    base = Path(base)
    candidates = sorted(base.glob("*/manifest.yaml"))
    if len(candidates) != 1:
        raise CapabilityLoadError(
            f"expected exactly one capability under {base}/*/manifest.yaml, found {len(candidates)}"
        )
    return candidates[0].parent
