"""The deterministic ustar plugin archive: build, read, verify, and the golden
vectors (contracts/vectors/archive/) a Go extractor is tested against."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile

import pytest

from runwhen_capability.contracts import archive as archive_mod
from runwhen_capability.contracts import data_dir
from runwhen_capability.contracts.archive import (
    ArchiveError,
    TreeEntry,
    archive_sha256,
    build,
    read,
    verify,
)


def _sorted(entries):
    return sorted(entries, key=lambda e: e.path.encode("utf-8"))


def _entries():
    return [
        TreeEntry("skills/k8s/run.sh", b"#!/bin/sh\necho hi\n", executable=True),
        TreeEntry("plugin.yaml", b"name: demo\n"),
        TreeEntry("skills/k8s/SKILL.md", b"# k8s\n"),
        TreeEntry("knowledge/empty.md", b""),
    ]


# --- build -----------------------------------------------------------------


def test_build_is_deterministic_and_order_independent():
    assert build(_entries()) == build(reversed(_entries()))
    assert build(_entries()) == build(_entries())


def test_build_pads_to_a_multiple_of_10240():
    assert len(build([])) == 10240
    assert build([]) == bytes(10240)
    assert len(build(_entries())) % 10240 == 0
    big = build([TreeEntry("big", b"x" * 20000)])
    assert len(big) % 10240 == 0 and len(big) > 10240


def test_build_matches_python_tarfile_ustar_bytes():
    entries = _entries() + [
        # Only one split is valid (a/ddd/fff is 20 + 60 + 50), so every ustar writer
        # agrees on it; stdlib tarfile and GNU tar pick different splits when several fit.
        TreeEntry("a" * 20 + "/" + "d" * 60 + "/" + "f" * 50, b"long\n")
    ]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for entry in _sorted(entries):
            info = tarfile.TarInfo(entry.path)
            info.size = len(entry.data)
            info.mode = 0o755 if entry.executable else 0o644
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            tf.addfile(info, io.BytesIO(entry.data))
    assert build(entries) == buf.getvalue()


def test_build_is_readable_by_stdlib_tarfile():
    data = build(_entries())
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        members = tf.getmembers()
        assert [m.name for m in members] == [e.path for e in _sorted(_entries())]
        assert all(m.isreg() for m in members)  # no directory entries
        assert [bool(m.mode & 0o100) for m in members] == [
            e.executable for e in _sorted(_entries())
        ]
        assert all(m.uid == m.gid == m.mtime == 0 and not m.uname and not m.gname for m in members)
        for member, entry in zip(members, _sorted(_entries()), strict=True):
            assert tf.extractfile(member).read() == entry.data


def test_header_fields():
    data = build([TreeEntry("run.sh", b"abc", executable=True)])
    header = data[:512]
    assert header[100:108] == b"0000755\x00"
    assert header[108:124] == b"0000000\x00" * 2
    assert header[124:136] == b"00000000003\x00"
    assert header[136:148] == b"00000000000\x00"
    assert header[156:157] == b"0"
    assert header[257:265] == b"ustar\x0000"
    assert header[265:329] == bytes(64)  # uname, gname
    assert header[329:345] == bytes(16)  # devmajor, devminor
    assert header[157:257] == bytes(100)  # linkname
    assert data[512:515] == b"abc" and data[515:1024] == bytes(509)


def test_long_path_is_split_at_the_longest_prefix_that_fits():
    path = "a/" + "b" * 90 + "/" + "c" * 60 + "/" + "d" * 50
    header = build([TreeEntry(path, b"")])[:512]
    assert header[:100].rstrip(b"\x00") == b"d" * 50
    assert header[345:500].rstrip(b"\x00") == ("a/" + "b" * 90 + "/" + "c" * 60).encode()
    assert read(build([TreeEntry(path, b"x")])) == [TreeEntry(path, b"x")]


def test_unicode_nfc_path_round_trips():
    entry = TreeEntry("knowledge/café/naïve.md", b"x")
    assert read(build([entry])) == [entry]


# --- round trip, hash, verify ---------------------------------------------


def test_read_build_round_trip_is_sorted():
    entries = _entries()
    assert read(build(entries)) == _sorted(entries)


def test_archive_sha256_and_verify():
    data = build(_entries())
    digest = archive_sha256(data)
    assert digest == "sha256:" + hashlib.sha256(data).hexdigest()
    verify(data, digest)
    verify(data, digest.removeprefix("sha256:"))


def test_verify_rejects_a_mismatch():
    data = build(_entries())
    with pytest.raises(ArchiveError) as info:
        verify(data, "sha256:" + "0" * 64)
    assert info.value.code == "E_HASH_MISMATCH"
    with pytest.raises(ArchiveError) as info:
        verify(data + b"\x00", archive_sha256(data))
    assert info.value.code == "E_HASH_MISMATCH"


def test_archive_error_is_a_value_error_with_a_code():
    error = ArchiveError("E_INVALID_PLUGIN", "bad")
    assert isinstance(error, ValueError)
    assert error.code == "E_INVALID_PLUGIN" and "bad" in str(error)


# --- build rejections -------------------------------------------------------


def _code(callable_, *args):
    with pytest.raises(ArchiveError) as info:
        callable_(*args)
    return info.value.code


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/abs",
        "../up",
        "a/../b",
        "./a",
        "a/./b",
        "a//b",
        "a/",
        "a\\b",
        "a\x00b",
        "a\nb",
        "a\x7fb",
        "a\x85b",
        "café.md",  # NFD, not NFC
        "bad\ud800.md",  # not encodable as UTF-8
        "x" * 256,  # > 255 bytes
        "é" * 128,  # 256 bytes
        "d" * 200 + "/f",  # no split fits: prefix > 155
        "a/" + "f" * 101,  # name > 100 with a tiny prefix
    ],
)
def test_build_rejects_invalid_paths(path):
    assert _code(build, [TreeEntry(path, b"")]) == "E_INVALID_PLUGIN"


def test_build_rejects_duplicate_and_case_colliding_paths():
    assert _code(build, [TreeEntry("a", b"1"), TreeEntry("a", b"2")]) == "E_INVALID_PLUGIN"
    assert (
        _code(build, [TreeEntry("README.md", b""), TreeEntry("readme.md", b"")])
        == "E_INVALID_PLUGIN"
    )


def test_build_accepts_255_byte_paths_that_split():
    path = "d" * 155 + "/" + "f" * 99  # 255 bytes
    assert read(build([TreeEntry(path, b"")])) == [TreeEntry(path, b"")]


def test_build_limits(monkeypatch):
    monkeypatch.setattr(archive_mod, "MAX_FILE_BYTES", 10)
    assert _code(build, [TreeEntry("f", b"x" * 11)]) == "E_ARCHIVE_TOO_LARGE"
    build([TreeEntry("f", b"x" * 10)])
    monkeypatch.setattr(archive_mod, "MAX_FILES", 2)
    assert _code(build, [TreeEntry(f"f{i}", b"") for i in range(3)]) == "E_ARCHIVE_TOO_LARGE"
    monkeypatch.setattr(archive_mod, "MAX_FILE_BYTES", 100_000)
    monkeypatch.setattr(archive_mod, "MAX_ARCHIVE_BYTES", 10240)
    assert build([TreeEntry("f", b"")])
    assert (
        _code(build, [TreeEntry("f", b"x" * 5000), TreeEntry("g", b"x" * 5000)])
        == "E_ARCHIVE_TOO_LARGE"
    )


def test_documented_limits():
    assert archive_mod.MAX_ARCHIVE_BYTES == 64 * 1024 * 1024
    assert archive_mod.MAX_FILES == 10_000
    assert archive_mod.MAX_FILE_BYTES == 16 * 1024 * 1024


# --- read rejections --------------------------------------------------------


def _tar(*members, fmt=tarfile.USTAR_FORMAT):
    """Hand-made tar: members are (name, type, data, extras)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=fmt) as tf:
        for name, kind, data, extra in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(data) if kind == tarfile.REGTYPE else 0
            info.mode = 0o644
            for key, value in extra.items():
                setattr(info, key, value)
            tf.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    return buf.getvalue()


@pytest.mark.parametrize(
    "kind",
    [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE],
)
def test_read_rejects_links_devices_and_fifos(kind):
    data = _tar(("ok", tarfile.REGTYPE, b"x", {}), ("evil", kind, b"", {"linkname": "ok"}))
    assert _code(read, data) == "E_INVALID_PLUGIN"


@pytest.mark.parametrize("name", ["/etc/passwd", "../escape", "a/../../escape", "a\\b"])
def test_read_rejects_unsafe_paths(name):
    assert _code(read, _tar((name, tarfile.REGTYPE, b"x", {}))) == "E_INVALID_PLUGIN"


def test_read_ignores_directory_entries_and_metadata():
    data = _tar(
        ("dir/", tarfile.DIRTYPE, b"", {}),
        (
            "dir/f",
            tarfile.REGTYPE,
            b"x",
            {"uid": 1000, "gid": 1000, "mtime": 123456, "mode": 0o755},
        ),
    )
    assert read(data) == [TreeEntry("dir/f", b"x", executable=True)]


def test_read_rejects_duplicates_and_case_collisions():
    dup = _tar(("a", tarfile.REGTYPE, b"1", {}), ("a", tarfile.REGTYPE, b"2", {}))
    assert _code(read, dup) == "E_INVALID_PLUGIN"
    case = _tar(("A", tarfile.REGTYPE, b"1", {}), ("a", tarfile.REGTYPE, b"2", {}))
    assert _code(read, case) == "E_INVALID_PLUGIN"


def test_read_rejects_garbage_and_truncation():
    assert _code(read, b"not a tar" * 100) == "E_INVALID_PLUGIN"
    data = build([TreeEntry("f", b"x" * 5000)])
    assert _code(read, data[:1024]) == "E_INVALID_PLUGIN"


def test_read_limits(monkeypatch):
    data = build([TreeEntry("a", b"x" * 20), TreeEntry("b", b"")])
    monkeypatch.setattr(archive_mod, "MAX_FILE_BYTES", 10)
    assert _code(read, data) == "E_ARCHIVE_TOO_LARGE"
    monkeypatch.undo()
    monkeypatch.setattr(archive_mod, "MAX_FILES", 1)
    assert _code(read, data) == "E_ARCHIVE_TOO_LARGE"
    monkeypatch.undo()
    monkeypatch.setattr(archive_mod, "MAX_ARCHIVE_BYTES", 10239)
    assert _code(read, data) == "E_ARCHIVE_TOO_LARGE"


# --- golden vectors ---------------------------------------------------------


def _vector_cases():
    folder = data_dir() / "vectors" / "archive"
    return sorted(f.name[: -len(".json")] for f in folder.iterdir() if f.name.endswith(".json"))


def test_golden_vector_cases_present():
    assert _vector_cases() == [
        "empty-file",
        "long-path-prefix-split",
        "nested-executable",
        "single-file",
        "ten-files-out-of-order",
        "unicode-nfc-path",
    ]


@pytest.mark.parametrize("case", _vector_cases())
def test_golden_vector_replays(case):
    folder = data_dir() / "vectors" / "archive"
    doc = json.loads((folder / f"{case}.json").read_text(encoding="utf-8"))
    golden = (folder / f"{case}.tar").read_bytes()
    entries = [
        TreeEntry(e["path"], base64.b64decode(e["dataBase64"]), e["executable"])
        for e in doc["entries"]
    ]
    assert set(doc) == {"description", "entries", "sha256", "size"}
    assert build(entries) == golden
    assert doc["size"] == len(golden)
    assert doc["sha256"] == archive_sha256(golden)
    verify(golden, doc["sha256"])
    assert read(golden) == _sorted(entries)
