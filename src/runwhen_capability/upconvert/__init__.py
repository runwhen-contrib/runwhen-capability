"""runwhen_capability.upconvert -- turn today's two classic capability formats into a
V13 skill directory.

    result = upconvert_custom(Path("bundles/pgbouncer-health"))   # custom-capability/v1
    result = upconvert_packaged(Path("capabilities/rw-checks"))   # capability/v1
    write_result(result, Path("skills") / result.skill_name)

A converter reads the source and returns an `UpconvertResult` (skill name, files,
executable paths, findings); it never writes. The findings are all warnings, codes
`W_UPCONVERT_*` (see from_custom.py / from_packaged.py), each with the source `file` and a
dotted `path` into it. Whether the result is a valid skill is `plugin.validate_tree`'s
call, not the converter's.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..custom.manifest import API_VERSION as CUSTOM_API_VERSION
from ._common import (
    W_UPCONVERT_CREDENTIAL,
    W_UPCONVERT_LOSSY,
    W_UPCONVERT_REVIEW,
    W_UPCONVERT_SETUP_DROPPED,
    UpconvertError,
    UpconvertResult,
    write_result,
)
from .from_custom import upconvert_custom
from .from_packaged import API_VERSION as PACKAGED_API_VERSION
from .from_packaged import upconvert_packaged

__all__ = [
    "W_UPCONVERT_CREDENTIAL",
    "W_UPCONVERT_LOSSY",
    "W_UPCONVERT_REVIEW",
    "W_UPCONVERT_SETUP_DROPPED",
    "UpconvertError",
    "UpconvertResult",
    "detect_format",
    "upconvert_custom",
    "upconvert_dir",
    "upconvert_packaged",
    "write_result",
]


def _api_version(path: Path) -> object:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise UpconvertError(f"{path.name} cannot be read: {exc}") from exc
    return doc.get("apiVersion") if isinstance(doc, dict) else None


def detect_format(src: Path) -> str:
    """`"custom"` for a directory whose capability.yaml is `runwhen.com/custom-capability/v1`,
    `"packaged"` for one whose manifest.yaml is `capability/v1` (a manifest with no
    apiVersion is taken as packaged, as the host's loader does). Raises `UpconvertError`."""
    src = Path(src)
    if not src.is_dir():
        raise UpconvertError(f"{src} is not a directory")
    custom, packaged = src / "capability.yaml", src / "manifest.yaml"
    if custom.is_file():
        version = _api_version(custom)
        if version != CUSTOM_API_VERSION:
            raise UpconvertError(f"capability.yaml: apiVersion must be {CUSTOM_API_VERSION}")
        return "custom"
    if packaged.is_file():
        version = _api_version(packaged)
        if version not in (None, PACKAGED_API_VERSION):
            raise UpconvertError(f"manifest.yaml: apiVersion must be {PACKAGED_API_VERSION}")
        return "packaged"
    raise UpconvertError(f"{src} has neither a capability.yaml nor a manifest.yaml")


def upconvert_dir(src: Path, *, name: str | None = None) -> UpconvertResult:
    """Up-convert whichever format `src` is (see `detect_format`)."""
    if detect_format(src) == "custom":
        return upconvert_custom(src, name=name)
    return upconvert_packaged(src, name=name)
