"""The plugin archive: an uncompressed POSIX ustar tar whose bytes are fully
determined by the plugin's file tree, so its sha256 is the one identity a
plugin version has everywhere (index, lease, blob URL, runner cache, snapshot).

`build` writes it, `read` extracts it to memory under the safety rules every
consumer must apply, `verify` checks bytes against an expected hash. The byte
layout is normative (contracts/archive.md section 3) and pinned by the golden
vectors in contracts/vectors/archive/, which a Go extractor is tested against.

- Regular-file entries only, no directory entries, sorted by the UTF-8 bytes of
  the path; `name`/`prefix` split at the rightmost "/" that leaves `prefix` at
  most 155 bytes (the longest prefix that fits, `name` at most 100).
- mode 0644, or 0755 for an executable file; uid, gid and mtime 0; uname, gname,
  linkname, devmajor and devminor empty (all NUL, which is what `tarfile`
  writes for a regular file; a reader sees 0); magic `ustar\\0`, version `00`;
  numerics octal, zero-padded, NUL-terminated, exactly as `tarfile.USTAR_FORMAT`
  writes them.
- Content padded with zeros to 512, then two zero blocks, then zero padding to
  a multiple of 10240.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_FILES = 10_000
MAX_FILE_BYTES = 16 * 1024 * 1024

_BLOCK = 512
_RECORD = 10240
_MAX_PATH_BYTES = 255
_MAX_NAME_BYTES = 100
_MAX_PREFIX_BYTES = 155


class ArchiveError(ValueError):
    """An archive (or a tree to be archived) breaks a rule. `code` is one of
    E_INVALID_PLUGIN, E_ARCHIVE_TOO_LARGE, E_HASH_MISMATCH."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class TreeEntry:
    """One file of a plugin tree: POSIX path relative to the root, its bytes,
    and whether the owner exec bit is set."""

    path: str
    data: bytes
    executable: bool = False


def _invalid(message: str) -> ArchiveError:
    return ArchiveError("E_INVALID_PLUGIN", message)


def _too_large(message: str) -> ArchiveError:
    return ArchiveError("E_ARCHIVE_TOO_LARGE", message)


def archive_sha256(data: bytes) -> str:
    """ "sha256:" + hex of the archive bytes -- the plugin version's identity."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def verify(data: bytes, sha256: str) -> None:
    """Raise ArchiveError(E_HASH_MISMATCH) unless `data` hashes to `sha256`
    (`sha256:<hex>` or bare hex)."""
    expected = sha256.removeprefix("sha256:").lower()
    actual = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise ArchiveError(
            "E_HASH_MISMATCH", f"archive hashes to sha256:{actual}, expected {sha256}"
        )


def build(entries: Iterable[TreeEntry]) -> bytes:
    """The archive for a file tree. Input order is irrelevant. Raises
    ArchiveError(E_INVALID_PLUGIN) for a bad or colliding path and
    ArchiveError(E_ARCHIVE_TOO_LARGE) past a limit."""
    files = _validated(entries)
    if len(files) > MAX_FILES:
        raise _too_large(f"{len(files)} files (limit {MAX_FILES})")
    total = 2 * _BLOCK
    for entry in files:
        if len(entry.data) > MAX_FILE_BYTES:
            raise _too_large(f"{entry.path!r} is {len(entry.data)} bytes (limit {MAX_FILE_BYTES})")
        total += _BLOCK + _padded(len(entry.data), _BLOCK)
    total = _padded(total, _RECORD)
    if total > MAX_ARCHIVE_BYTES:
        raise _too_large(f"archive would be {total} bytes (limit {MAX_ARCHIVE_BYTES})")

    out = bytearray()
    for entry in files:
        out += _header(entry)
        out += entry.data
        out += bytes(_padded(len(entry.data), _BLOCK) - len(entry.data))
    out += bytes(2 * _BLOCK)
    out += bytes(_padded(len(out), _RECORD) - len(out))
    return bytes(out)


def read(data: bytes) -> list[TreeEntry]:
    """Extract an archive to memory, sorted by the UTF-8 bytes of the path.

    Parses plain ustar headers directly -- never through a tar library, which
    folds pax and GNU extension headers into the next member -- so every
    entry's data is exactly the `size` bytes after its own 512-byte header and
    the declared sizes are what the limits are checked against. Each header
    needs a valid checksum, magic `ustar\\0` and version `00`. Typeflag '0' is a
    file, '5' a directory entry (ignored, size 0); every other typeflag
    (symlinks, hardlinks, devices, fifos, pax x/g, GNU L/K/S, NUL) is
    E_INVALID_PLUGIN, as are an unsafe or colliding path, a non-octal size
    (base-256), truncation, a missing second end block, or non-zero bytes after
    the end. uid/gid/mtime are ignored; `executable` is the owner exec bit.
    Exceeding a limit is E_ARCHIVE_TOO_LARGE. The hash matching is not proof of
    safety: callers verify and read."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise _too_large(f"archive is {len(data)} bytes (limit {MAX_ARCHIVE_BYTES})")
    entries: list[TreeEntry] = []
    pos = 0
    while True:
        block = data[pos : pos + _BLOCK]
        if len(block) < _BLOCK:
            raise _invalid("truncated: no end-of-archive marker")
        if block == _ZERO_BLOCK:
            break
        size, typeflag, path, mode = _parse_header(block)
        pos += _BLOCK
        if typeflag == b"5":
            if size != 0:
                raise _invalid("directory entry with data")
            continue
        if len(entries) >= MAX_FILES:
            raise _too_large(f"more than {MAX_FILES} files")
        if size > MAX_FILE_BYTES:
            raise _too_large(f"{path!r} declares {size} bytes (limit {MAX_FILE_BYTES})")
        end = pos + _padded(size, _BLOCK)
        if end > len(data):
            raise _invalid(f"{path!r}: truncated (declares {size} bytes)")
        entries.append(TreeEntry(path, data[pos : pos + size], bool(mode & 0o100)))
        pos = end
    if data[pos + _BLOCK : pos + 2 * _BLOCK] != _ZERO_BLOCK:
        raise _invalid("missing second end-of-archive block")
    if data.count(0, pos + 2 * _BLOCK) != len(data) - pos - 2 * _BLOCK:
        raise _invalid("non-zero bytes after the end-of-archive marker")
    return _validated(entries)


_ZERO_BLOCK = bytes(_BLOCK)
_OCTAL = re.compile(rb"[0-7]+")


def _octal_field(field: bytes, what: str) -> int:
    """A numeric header field: octal digits then NULs or spaces. Anything else
    (empty, base-256, junk) is invalid."""
    text = field.rstrip(b"\x00 ")
    if not _OCTAL.fullmatch(text):
        raise _invalid(f"header {what} field is not octal: {field!r}")
    return int(text, 8)


def _parse_header(block: bytes) -> tuple[int, bytes, str, int]:
    """(size, typeflag, path, mode) of one ustar header, or E_INVALID_PLUGIN."""
    stored = _octal_field(block[148:156].lstrip(b" "), "checksum")
    if stored != sum(block[:148]) + 8 * 0x20 + sum(block[156:]):
        raise _invalid("header checksum mismatch")
    if block[257:263] != b"ustar\x00" or block[263:265] != b"00":
        raise _invalid("not a ustar header (magic/version)")
    typeflag = block[156:157]
    if typeflag not in (b"0", b"5"):
        raise _invalid(f"unsupported entry type {typeflag!r}")
    size = _octal_field(block[124:136], "size")
    mode = _octal_field(block[100:108], "mode")
    try:
        name = block[0:100].split(b"\x00", 1)[0].decode("utf-8")
        prefix = block[345:500].split(b"\x00", 1)[0].decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid("path is not valid UTF-8") from None
    return size, typeflag, f"{prefix}/{name}" if prefix else name, mode


def _validated(entries: Iterable[TreeEntry]) -> list[TreeEntry]:
    """Entries sorted by path bytes, every path checked and unique (also
    case-insensitively)."""
    result: list[TreeEntry] = []
    seen: dict[str, str] = {}
    for entry in entries:
        _check_path(entry.path)
        folded = entry.path.casefold()
        if folded in seen:
            raise _invalid(f"{entry.path!r} collides with {seen[folded]!r}")
        seen[folded] = entry.path
        result.append(TreeEntry(entry.path, entry.data, bool(entry.executable)))
    result.sort(key=lambda e: e.path.encode("utf-8"))
    return result


def _check_path(path: str) -> None:
    """A relative POSIX path: UTF-8 NFC, at most 255 bytes, no leading or
    trailing "/", no empty, "." or ".." segment, no backslash, no control
    character, and one that ustar can split into prefix and name."""
    if not path:
        raise _invalid("empty path")
    try:
        raw = path.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid(f"path is not valid UTF-8: {path!r}") from None
    if unicodedata.normalize("NFC", path) != path:
        raise _invalid(f"path is not NFC-normalised: {path!r}")
    if any(unicodedata.category(ch) == "Cc" for ch in path):
        raise _invalid(f"path contains a control character: {path!r}")
    if "\\" in path:
        raise _invalid(f"path must use '/' separators: {path!r}")
    if path.startswith("/"):
        raise _invalid(f"path must be relative: {path!r}")
    if path.endswith("/"):
        raise _invalid(f"path must not end with '/': {path!r}")
    for segment in path.split("/"):
        if segment in ("", ".", ".."):
            raise _invalid(f"path has an empty, '.' or '..' segment: {path!r}")
    if len(raw) > _MAX_PATH_BYTES:
        raise _invalid(f"path is {len(raw)} bytes (limit {_MAX_PATH_BYTES}): {path!r}")
    _split(raw)


def _split(raw: bytes) -> tuple[bytes, bytes]:
    """(prefix, name): `raw` whole when it fits in `name`, else split at the
    rightmost "/" leaving prefix <= 155 bytes; name must then be <= 100."""
    if len(raw) <= _MAX_NAME_BYTES:
        return b"", raw
    cut = raw.rfind(b"/", 0, _MAX_PREFIX_BYTES + 1)
    if cut <= 0 or len(raw) - cut - 1 > _MAX_NAME_BYTES:
        raise _invalid(f"path cannot be split into ustar name and prefix: {raw.decode()!r}")
    return raw[:cut], raw[cut + 1 :]


def _padded(size: int, multiple: int) -> int:
    return -(-size // multiple) * multiple


def _octal(value: int, width: int) -> bytes:
    return f"{value:0{width - 1}o}".encode("ascii") + b"\x00"


def _header(entry: TreeEntry) -> bytes:
    prefix, name = _split(entry.path.encode("utf-8"))
    header = bytearray(_BLOCK)
    header[0 : len(name)] = name
    header[100:108] = _octal(0o755 if entry.executable else 0o644, 8)
    header[108:116] = _octal(0, 8)  # uid
    header[116:124] = _octal(0, 8)  # gid
    header[124:136] = _octal(len(entry.data), 12)
    header[136:148] = _octal(0, 12)  # mtime
    header[148:156] = b" " * 8  # checksum placeholder
    header[156:157] = b"0"
    header[257:263] = b"ustar\x00"
    header[263:265] = b"00"
    # devmajor/devminor stay all NUL, as tarfile writes them for a regular
    # file; a reader sees 0 either way.
    header[345 : 345 + len(prefix)] = prefix
    header[148:156] = f"{sum(header):06o}".encode("ascii") + b"\x00 "
    return bytes(header)
