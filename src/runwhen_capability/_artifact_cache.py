"""The on-disk cache behind `ctx.artifacts.input_path`: input artifacts spooled
to files, keyed by their sha256, shared by every request an executor serves.

    <root>/<sha256>                 a complete, verified artifact
    <root>/<sha256>.part-<random>   a download in progress (or abandoned)
    <root>/<sha256>.lock            "<pid> <token>": someone is downloading <sha256>

Correctness never rests on the lock; the lock only saves duplicate downloads.
What makes an entry trustworthy is how it gets its name: bytes are streamed to
a part file while being hashed, and only a body whose digest equals the key is
moved (`os.replace`, atomic) to `<root>/<sha256>`. A file under that name is
therefore always complete and always the right bytes, however many downloaders
raced or died. A body that does not hash to the key is deleted and reported
(`E_ARTIFACT_CORRUPT`, naming both digests).

Where the key comes from: the digest the lease declared for the input; if it
declared none, the `X-RW-Sha256` header of the answer; if there is neither,
the digest of the bytes themselves (nothing to verify against, so the entry is
simply named for what arrived). Only the first allows a hit with no request.

The lock. Concurrent callers for one key queue on `<sha256>.lock`, created
with `O_CREAT | O_EXCL`, and re-check the cache once they hold it, so N
callers cost one download. The cache outlives processes (the volume survives a
container restart), so a lock can outlive its owner; a waiter breaks a lock
that is:

- owned by a pid that is gone;
- unreadable or empty and a few seconds old (an owner that died between
  creating the file and writing its pid);
- untouched for a minute. A downloading owner touches its lock as it reads, so
  this is the owner stopping; it is also what catches a pid that looks alive
  because the restarted container reused it (the first process of a container
  is always pid 1).

Eviction keeps the completed entries under `RW_ARTIFACT_CACHE_MAX_BYTES`
(default 1 GiB), least recently used first. "Used" is the entry's atime, which
we set ourselves with `os.utime` on every hit (the kernel's own atime updates
are off or lazy on most mounts). Never evicted: an entry this cache object has
handed out (the task that got the path may not have opened it yet), and the
entry just added, even if it alone is over budget. So the cache can sit over
budget while a task holds a lot; it is trimmed by the next request.

This module does not import `requests`: the response is duck-typed (`.raw`,
`.headers`, `.close()`), like the relay client in artifacts.py.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import ArtifactError

# The cache's directory name, under the executor's work root (or, with no host
# to say otherwise, under the Context's workdir).
ARTIFACT_CACHE_DIR = ".rw-artifact-cache"

MAX_BYTES_ENV = "RW_ARTIFACT_CACHE_MAX_BYTES"
DEFAULT_MAX_BYTES = 1 << 30

# Size of each read from the network: with the hash update and the file write,
# the bound on how much of an artifact is in memory at once.
_READ_BYTES = 1 << 20

_LOCK_POLL_SECONDS = 0.05
# How often a downloading owner touches its lock.
_LOCK_HEARTBEAT_SECONDS = 5
# An owner that has not touched its lock for this long is gone, whatever its pid says.
_LOCK_STALE_SECONDS = 60
# An unreadable or empty lock is given this long before it counts as abandoned.
_LOCK_EMPTY_GRACE_SECONDS = 5
# A part file nothing has written to for this long was abandoned by a dead host.
_PART_STALE_SECONDS = 3600

_SHA256 = re.compile(r"[0-9a-f]{64}")


def max_bytes_from_env() -> int:
    """The cache budget: `RW_ARTIFACT_CACHE_MAX_BYTES`, else 1 GiB. A value
    that is not a non-negative integer is ignored (the default applies)."""
    raw = os.environ.get(MAX_BYTES_ENV)
    if raw is None:
        return DEFAULT_MAX_BYTES
    try:
        value = int(raw.strip())
    except ValueError:
        return DEFAULT_MAX_BYTES
    return value if value >= 0 else DEFAULT_MAX_BYTES


def normalize_sha256(value: str, what: str) -> str:
    """`value` lower-cased, if it is 64 hex digits; the digest names a file in
    the cache, so anything else is refused (`E_ARTIFACT_RESPONSE`) rather than
    used as a path."""
    digest = value.strip().lower()
    if not _SHA256.fullmatch(digest):
        raise ArtifactError("E_ARTIFACT_RESPONSE", f"{what} is not a sha256 hex digest: {value!r}")
    return digest


class ArtifactCache:
    """The cache in directory `root`, for ONE task: it remembers which entries
    it handed out (they are not evicted while this object lives)."""

    def __init__(self, root: Path, max_bytes: int, log: logging.Logger) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.log = log
        self._handed_out: set[str] = set()

    def fetch(
        self,
        label: str,
        expected: str | None,
        open_response: Callable[[], Any],
    ) -> Path:
        """The path of the artifact in the cache, downloading it first if it
        is not there. `label` names the input in errors. `expected` is the
        declared sha256 (normalized) or None. `open_response` opens the
        download and returns a streaming `requests.Response`; it is not called
        at all when `expected` is known and cached."""
        self.root.mkdir(parents=True, exist_ok=True)
        if expected is not None:
            return self._fetch_keyed(label, expected, open_response, None)
        resp = open_response()
        try:
            advertised = resp.headers.get("X-RW-Sha256") or None
            key = (
                None
                if advertised is None
                else normalize_sha256(advertised, "the X-RW-Sha256 header of the relay's answer")
            )
        except BaseException:
            resp.close()
            raise
        if key is not None:
            return self._fetch_keyed(label, key, open_response, resp)
        return self._finish(self._store_unkeyed(resp))

    # -- keyed ----------------------------------------------------------------

    def _fetch_keyed(
        self, label: str, key: str, open_response: Callable[[], Any], resp: Any | None
    ) -> Path:
        """`resp` is a response already open (its header gave the key), or
        None; either way it is closed here."""
        try:
            hit = self._touch(key)
            if hit is not None:
                return self._hand_out(hit)
            with _Lock(self.root / f"{key}.lock") as lock:
                hit = self._touch(key)
                if hit is not None:
                    return self._hand_out(hit)
                if resp is not None and lock.waited:
                    resp.close()  # it sat idle while we queued; ask again
                    resp = None
                if resp is None:
                    resp = open_response()
                path = self._store(resp, key, label, lock)
                resp = None  # _store closed it
            return self._finish(path)
        finally:
            if resp is not None:
                resp.close()

    def _touch(self, key: str) -> Path | None:
        """The cached entry for `key` after marking it used, or None."""
        path = self.root / key
        try:
            os.utime(path)
        except FileNotFoundError:
            return None
        return path

    def _store(self, resp: Any, key: str, label: str, lock: _Lock) -> Path:
        part, actual = self._download(resp, key, lock)
        try:
            if actual != key:
                raise ArtifactError(
                    "E_ARTIFACT_CORRUPT",
                    f"input artifact {label!r} is corrupt: expected sha256 {key}, "
                    f"downloaded bytes hash to {actual}",
                )
            return self._publish(part, key)
        except BaseException:
            part.unlink(missing_ok=True)
            raise

    # -- unkeyed --------------------------------------------------------------

    def _store_unkeyed(self, resp: Any) -> Path:
        part, actual = self._download(resp, "unkeyed", None)
        try:
            return self._publish(part, actual)  # replacing an identical entry is harmless
        except BaseException:
            part.unlink(missing_ok=True)
            raise

    # -- shared ---------------------------------------------------------------

    def _download(self, resp: Any, prefix: str, lock: _Lock | None) -> tuple[Path, str]:
        """Streams the response body to a new part file in `_READ_BYTES` reads,
        hashing as it goes. Closes `resp`. Returns the part file and the hex
        sha256 of what was read; on any error the part file is removed."""
        part = self.root / f"{prefix}.part-{secrets.token_hex(6)}"
        digest = hashlib.sha256()
        raw = resp.raw
        raw.decode_content = False  # the digest is of the stored bytes
        try:
            with resp, open(part, "wb") as out:
                while True:
                    data = raw.read(_READ_BYTES)
                    if not data:
                        break
                    out.write(data)
                    digest.update(data)
                    if lock is not None:
                        lock.heartbeat()
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return part, digest.hexdigest()

    def _publish(self, part: Path, key: str) -> Path:
        final = self.root / key
        os.replace(part, final)
        os.utime(final)
        return final

    def _hand_out(self, path: Path) -> Path:
        self._handed_out.add(path.name)
        return path

    def _finish(self, added: Path) -> Path:
        """Hands out a freshly added entry, then trims the cache. Handing out
        first is what spares the entry just added from its own trim."""
        self._hand_out(added)
        self._evict()
        return added

    def _evict(self) -> None:
        """Brings the completed entries under the budget, least recently used
        first, sparing every entry this task holds (which includes the one
        just added); also removes part files abandoned by dead hosts."""
        entries: list[tuple[int, str, int]] = []  # (atime_ns, name, size)
        total = 0
        now = time.time()
        with os.scandir(self.root) as scan:
            for child in scan:
                try:
                    st = child.stat()
                except FileNotFoundError:
                    continue
                if _SHA256.fullmatch(child.name):
                    entries.append((st.st_atime_ns, child.name, st.st_size))
                    total += st.st_size
                elif ".part-" in child.name and now - st.st_mtime > _PART_STALE_SECONDS:
                    self.log.info("removing abandoned download %s", child.name)
                    Path(child.path).unlink(missing_ok=True)
        if total <= self.max_bytes:
            return
        for _atime, name, size in sorted(entries):
            if total <= self.max_bytes:
                break
            if name in self._handed_out:
                continue
            self.log.info(
                "input artifact cache over budget (%d > %d bytes): evicting %s",
                total,
                self.max_bytes,
                name,
            )
            (self.root / name).unlink(missing_ok=True)
            total -= size


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, it is just not ours to signal
    return True


def _owner_pid(text: str) -> int | None:
    try:
        pid = int(text.split()[0])
    except (IndexError, ValueError):
        return None
    return pid if pid > 0 else None  # 0 and negatives would signal a process group


def _break_if_stale(path: Path) -> bool:
    """Removes the lock at `path` if its owner is gone (see the module
    docstring). True if the lock is now gone, whoever removed it."""
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return True
    try:
        pid = _owner_pid(path.read_text())
    except (OSError, UnicodeDecodeError):
        pid = None
    if pid is None:
        stale = age > _LOCK_EMPTY_GRACE_SECONDS
    elif not _pid_alive(pid):
        stale = True
    else:
        stale = age > _LOCK_STALE_SECONDS
    if stale:
        path.unlink(missing_ok=True)
    return stale


class _Lock:
    """`<sha256>.lock`, held for the duration of a `with`. `waited` says
    whether acquiring it took a wait (another downloader had it)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.waited = False
        self._token = f"{os.getpid()} {secrets.token_hex(8)}"
        self._beat_at = time.monotonic()

    def __enter__(self) -> _Lock:
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                self.waited = True
                if not _break_if_stale(self.path):
                    time.sleep(_LOCK_POLL_SECONDS)
                continue
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(self._token)
            except BaseException:
                self.path.unlink(missing_ok=True)
                raise
            self._beat_at = time.monotonic()
            return self

    def heartbeat(self) -> None:
        """Tells waiters the owner is alive: touches the lock, at most every
        few seconds."""
        if time.monotonic() - self._beat_at < _LOCK_HEARTBEAT_SECONDS:
            return
        self._beat_at = time.monotonic()
        try:
            os.utime(self.path)
        except FileNotFoundError:
            pass  # broken by a waiter that thought us dead; the download is still sound

    def __exit__(self, *exc: object) -> None:
        try:
            # Not someone else's: a waiter may have broken ours and taken its own.
            if self.path.read_text() == self._token:
                self.path.unlink(missing_ok=True)
        except OSError:
            pass
