"""Canonical JSON and the two hashes the V13 contracts pin down.

These are contracts, not conveniences: the registry, the executor and the SDK
(and any Go implementation) must agree byte for byte, so each algorithm is
spelled out here and replayed from contracts/vectors/*.json by the tests.

- `canonical_json`: the RFC 8785 subset V13 documents use -- sorted keys, no
  whitespace, UTF-8 with non-ASCII kept as-is, floats in ECMAScript form, no
  NaN/Infinity.
- `permission_hash`: identity of one permission rule, insensitive to list order,
  duplicates and keys that are absent, None or an empty list.
- `task_hash`: identity of one compiled task plus the files it depends on.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any


def canonical_json(obj: Any) -> bytes:
    """`obj` as canonical UTF-8 JSON: sorted keys, no whitespace, non-ASCII
    kept, floats written per RFC 8785 (see `_float`). Dict keys must be
    strings (TypeError otherwise); NaN/Infinity and floats outside
    1e-6 <= abs < 1e21 (zero aside) raise ValueError; other non-JSON types
    raise TypeError."""
    return _render(obj).encode("utf-8")


def _render(value: Any) -> str:
    # Pre-walk: floats are rendered here, everything else by json.dumps, so
    # ensure_ascii=False string handling is exactly the standard library's.
    if isinstance(value, float):
        return _float(value)
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        members = (f"{_string(key)}:{_render(value[key])}" for key in sorted(value))
        return "{" + ",".join(members) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_render(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _float(value: float) -> str:
    """RFC 8785 (ECMAScript Number::toString) for the range V13 accepts: a
    finite float with an integral value and abs < 1e21 is an integer (`1.0` ->
    `1`, `-0.0` -> `0`); any other finite float with 1e-6 <= abs < 1e21 is its
    shortest round-trip digits, always positional (Python's repr switches to
    exponent form below 1e-4, ECMAScript does not until 1e-6). Anything else
    is ValueError."""
    if not math.isfinite(value):
        raise ValueError(f"not canonicalizable: {value!r} is not finite")
    if value == 0:
        return "0"
    if not 1e-6 <= abs(value) < 1e21:
        raise ValueError(f"not canonicalizable: float {value!r} is outside 1e-6 <= abs < 1e21")
    digits = Decimal(repr(value))
    if value == int(value):
        digits = digits.to_integral_value()
    return format(digits, "f")


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


def task_hash(compiled_task: Mapping[str, Any], files: Mapping[str, bytes]) -> str:
    """ "sha256:" + hex of sha256(canonical_json({"task": <compiled_task without
    its "hash" key>, "files": {path: sha256(data) hex}})). Paths are validated
    like an archive's (relative POSIX, no empty, '.' or '..' segment)."""
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
