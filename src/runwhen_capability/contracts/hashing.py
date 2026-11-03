"""Canonical JSON and the three hashes the V13 contracts pin down.

These are contracts, not conveniences: the registry, the executor and the SDK
(and any Go implementation) must agree byte for byte, so each algorithm is
spelled out here and replayed from contracts/vectors/*.json by the tests.

- `canonical_json`: the RFC 8785 subset V13 documents use -- sorted keys, no
  whitespace, UTF-8 with non-ASCII kept as-is, no NaN/Infinity. Documents hashed
  this way must not contain floats; use integers or strings.
- `permission_hash`: identity of one permission rule, insensitive to list order,
  duplicates and keys that are absent, None or an empty list.
- `tree_hash`: identity of a file tree (path, content and exec bit).
- `task_hash`: identity of one compiled task plus the files it depends on.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


def canonical_json(obj: Any) -> bytes:
    """`obj` as canonical UTF-8 JSON: sorted keys, no whitespace, non-ASCII
    kept, NaN/Infinity rejected (ValueError); other non-JSON types TypeError."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def normalize_permission(rule: Mapping[str, Any]) -> dict[str, Any]:
    """The rule with keys whose value is None or an empty list dropped, every
    list de-duplicated and sorted (by the canonical JSON of each item), and
    all of that applied recursively. A list holding one empty string is NOT
    empty: `apiGroups: [""]` (the core API group) differs from `apiGroups: []`."""
    return _normalize(rule)


def _normalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _normalize(item)
            for key, item in value.items()
            if item is not None and not (isinstance(item, (list, tuple)) and len(item) == 0)
        }
    if isinstance(value, (list, tuple)):
        unique = {canonical_json(item): item for item in map(_normalize, value)}
        return [unique[key] for key in sorted(unique)]
    return value


def permission_hash(rule: Mapping[str, Any]) -> str:
    """ "sha256:" + hex of sha256(canonical_json(normalize_permission(rule)))."""
    return _sha256_tag(canonical_json(normalize_permission(rule)))


@dataclass(frozen=True)
class TreeEntry:
    """One file of a tree: POSIX path relative to the tree root, its bytes, and
    whether the exec bit is set."""

    path: str
    data: bytes
    executable: bool = False


def tree_hash(entries: Iterable[TreeEntry]) -> str:
    """ "sha256:" + hex of sha256 over one line per entry, sorted by the path's
    UTF-8 bytes: `<sha256(data) hex> <755|644> <path>\\n`. Raises ValueError on
    an invalid path (see `_check_path`) or a duplicate path."""
    lines: dict[bytes, bytes] = {}
    for entry in entries:
        _check_path(entry.path)
        key = entry.path.encode("utf-8")
        if key in lines:
            raise ValueError(f"duplicate path in tree: {entry.path!r}")
        mode = "755" if entry.executable else "644"
        digest = hashlib.sha256(entry.data).hexdigest()
        lines[key] = f"{digest} {mode} {entry.path}\n".encode()
    return _sha256_tag(b"".join(lines[key] for key in sorted(lines)))


def task_hash(compiled_task: Mapping[str, Any], files: Mapping[str, bytes]) -> str:
    """ "sha256:" + hex of sha256(canonical_json({"task": <compiled_task without
    its "hash" key>, "files": {path: sha256(data) hex}})). Paths are validated
    like `tree_hash`'s."""
    task = {key: value for key, value in compiled_task.items() if key != "hash"}
    digests: dict[str, str] = {}
    for path, data in files.items():
        _check_path(path)
        digests[path] = hashlib.sha256(data).hexdigest()
    return _sha256_tag(canonical_json({"task": task, "files": digests}))


def _sha256_tag(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _check_path(path: str) -> None:
    """A POSIX path relative to the root: no leading or trailing "/", no empty,
    "." or ".." segment, no backslash."""
    if not path:
        raise ValueError("empty path")
    if "\\" in path:
        raise ValueError(f"path must use '/' separators: {path!r}")
    if path.startswith("/"):
        raise ValueError(f"path must be relative: {path!r}")
    if path.endswith("/"):
        raise ValueError(f"path must not end with '/': {path!r}")
    for segment in path.split("/"):
        if segment in ("", ".", ".."):
            raise ValueError(f"path has an empty, '.' or '..' segment: {path!r}")
