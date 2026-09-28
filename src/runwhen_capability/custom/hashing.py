"""content_hash/task_hash -- the two hashes a custom capability version is
identified and diffed by.

`content_hash` is over the WHOLE bundle: GitOps apply and draft/publish
idempotency key off it. `task_hash` is over the smaller set of files one task
actually depends on: test evidence is matched by it, so editing an unrelated
task's file must not invalidate every other task's evidence.
"""

from __future__ import annotations

import hashlib

import yaml

from .manifest import Manifest


def _file_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def content_hash(files: dict[str, str]) -> str:
    """The bundle's content hash: "sha256:" + hex of sha256 over the sorted
    (path, sha256(content)) pairs -- sorted so file iteration/dict order never changes the hash, and
    pairing each path with its own digest (rather than hashing the
    concatenated content) so a renamed-but-identical file changes the hash
    while a whitespace-only edit to one file cannot cancel out against
    another."""
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_file_sha256(files[path]).encode("ascii"))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def task_files(files: dict[str, str], task: str) -> list[str]:
    """The paths task_hash() covers for `task`: capability.yaml, every file
    under lib/, the setup file (if any), and the named task's own file.
    Raises KeyError if `task` does not exist in capability.yaml's `tasks`."""
    manifest = Manifest.model_validate(yaml.safe_load(files["capability.yaml"]))
    task_spec = next((t for t in manifest.tasks if t.name == task), None)
    if task_spec is None:
        raise KeyError(f"no task {task!r} in capability.yaml")

    paths = {"capability.yaml", task_spec.file}
    if manifest.setup is not None:
        paths.add(manifest.setup.file)
    paths.update(path for path in files if path.startswith("lib/"))
    return sorted(paths)


def task_hash(files: dict[str, str], task: str) -> str:
    """content_hash(), restricted to task_files(files, task) -- the exact
    subset of the bundle `task` depends on."""
    relevant = {path: files[path] for path in task_files(files, task) if path in files}
    return content_hash(relevant)
