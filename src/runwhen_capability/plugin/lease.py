"""Run a `runwhen.com/lease/v1` request (what `rwtask serve` gets from the runner for a
V13 task) and return its result envelope.

    envelope = run_lease(doc, credentials={...}, scope_dir=scope)

The lease (CONTRACTS.md "Lease") is validated against the `lease` contract; its code is
checked against the sha256 it names and read from memory (`code.inline`) or from the
archive cache (`code.archive`); and `runspec_from_lease` turns it into the `RunSpec`
that `plugin.run.execute` runs. Nothing here starts a process but `execute`, which is
bundle.py's hardened child runner.

Every problem with the lease itself is an envelope, never an exception: `failed` with
`invalid_input` (the lease or a credential entry breaks its contract) or `internal`
(the code does not match its hash, cannot be read, or lacks the task's script) or
`dependency` (an archive that is not in the cache: fetching one waits for DLV-9).

Credentials. Today's `credentials` map of the poll response (`{name: value}`) keeps its
meaning: the value of a Kubernetes kubeconfig entry is written to a private file and
`KUBECONFIG` points at it (as for a bundle's `k8s.kubeconfig` input). Every value of the
map, and everything materialized, is redacted from what comes back.
"""

from __future__ import annotations

import base64
import binascii
import copy
import logging
import os
import shutil
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..contracts import ArchiveError, TreeEntry, archive_sha256, check, validate
from ..contracts import archive as archive_mod
from ..envelope import derive_envelope
from .run import RunSpec, _duration_seconds, check_extra_env, execute

_log = logging.getLogger("runwhen_capability.plugin.lease")

ARCHIVE_CACHE_ENV = "RW_ARCHIVE_CACHE"
DEFAULT_ARCHIVE_CACHE = "/work/.archives"
CREDENTIALS_DIR_ENV = "RW_CREDENTIALS_DIR"
DEFAULT_CREDENTIALS_DIR = "/run/rw/credentials"

#: Keys of the poll response's `credentials` map that carry a kubeconfig.
_KUBECONFIG_KEYS = ("kubernetes/kubeconfig", "kubeconfig", "k8s.kubeconfig")


class LeaseError(Exception):
    """A lease that cannot be run; `code` is the envelope error code to report."""

    def __init__(self, code: str, message: str, scope: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.scope = scope


def failed_envelope(code: str, message: str, scope: str | None = None) -> dict:
    """A `failed` envelope with one error, for a lease that never ran."""
    now = datetime.now(UTC)
    error: dict[str, Any] = {"code": code, "message": message[:4096]}
    if scope:
        error["scope"] = scope[:1024]
    return derive_envelope(exit_code=1, outputs={}, errors=[error], started_at=now, finished_at=now)


# -- the code ---------------------------------------------------------------------------


def archive_cache_dir() -> Path:
    return Path(os.environ.get(ARCHIVE_CACHE_ENV) or DEFAULT_ARCHIVE_CACHE)


def _decode_inline(item: dict) -> bytes:
    content = item["content"]
    if item.get("encoding", "utf-8") == "base64":
        try:
            return base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            raise LeaseError(
                "internal", f"code file {item['path']!r} is not valid base64", item["path"]
            ) from None
    return content.encode("utf-8")


def load_code(code: dict, cache_dir: Path | None = None) -> tuple[dict[str, bytes], frozenset[str]]:
    """(files by plugin-root path, executable paths) of a lease's `code`, after checking
    them against the sha256 the lease names. Raises LeaseError."""
    if "inline" in code:
        inline = code["inline"]
        entries = []
        for item in inline["files"]:
            entries.append(
                TreeEntry(item["path"], _decode_inline(item), bool(item.get("executable")))
            )
        try:
            computed = archive_sha256(archive_mod.build(entries))
        except ArchiveError as exc:
            raise LeaseError("internal", f"code rejected: {exc}") from None
        if computed != inline["sha256"]:
            raise LeaseError(
                "internal",
                f"code hash mismatch: the lease says {inline['sha256']}, the files hash to "
                f"{computed}",
            )
    else:
        sha = code["archive"]["sha256"]
        hex_digest = sha.removeprefix("sha256:")
        path = (cache_dir or archive_cache_dir()) / f"{hex_digest}.tar"
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise LeaseError(
                "dependency",
                f"archive {sha} is not in the cache: archive fetch not available until DLV-9",
            ) from None
        except OSError as exc:
            raise LeaseError("internal", f"archive {sha} could not be read: {exc}") from None
        try:
            archive_mod.verify(data, sha)
            entries = archive_mod.read(data)
        except ArchiveError as exc:
            raise LeaseError("internal", f"archive {sha} rejected: {exc}") from None
    return (
        {e.path: e.data for e in entries},
        frozenset(e.path for e in entries if e.executable),
    )


# -- the lease as a RunSpec ----------------------------------------------------------------


def runspec_from_lease(lease: dict, *, archive_cache: Path | None = None) -> RunSpec:
    """The one place a lease becomes a RunSpec. `lease` must already satisfy the `lease`
    contract. The compiled-task-shaped `task` carries what `execute` reads (ref, skill,
    name, script, readOnly, dryRun, outputs) and no `inputs`/`inputSchema`/`settings`:
    a lease's inputs and settings are resolved by papi and used as given. The target and
    its identity are the lease's own. `timeout_s` is `limits.timeout`. A run is never a
    dry run: the lease has no field to ask for one. Raises LeaseError."""
    files, executable = load_code(lease["code"], archive_cache)
    leased = lease["task"]
    if leased["script"] not in files:
        raise LeaseError("internal", f"the code has no script {leased['script']!r}")
    task = {
        **{key: leased[key] for key in ("ref", "plugin", "version", "skill", "name", "hash")},
        "script": leased["script"],
        "readOnly": leased["readOnly"],
        "dryRun": leased.get("dryRun", False),
        "outputs": copy.deepcopy(lease["outputs"]),
    }
    return RunSpec(
        task=task,
        files=files,
        executable=executable,
        target=copy.deepcopy(lease.get("target")),
        inputs=dict(lease["inputs"]),
        settings=dict(lease["settings"]),
        dry_run=False,
        timeout_s=_duration_seconds(lease["limits"]["timeout"]),
    )


# -- credentials ---------------------------------------------------------------------------


def credentials_dir() -> Path:
    return Path(os.environ.get(CREDENTIALS_DIR_ENV) or DEFAULT_CREDENTIALS_DIR)


def _kubeconfig_value(lease: dict, credentials: Mapping[str, str]) -> str | None:
    for key in _KUBECONFIG_KEYS:
        if credentials.get(key):
            return credentials[key]
    wants = any(
        isinstance(c, dict) and c.get("type") == "kubernetes/kubeconfig"
        for c in lease.get("credentials", [])
    )
    if wants and len(credentials) == 1:
        return next(iter(credentials.values())) or None
    return None


def _write_secret_file(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)  # the umask must not decide who can read it
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


# -- run ---------------------------------------------------------------------------------------


def run_lease(
    doc: Any,
    *,
    scope_dir: Path,
    credentials: Mapping[str, str] | None = None,
    deadline_s: float | None = None,
    archive_cache: Path | None = None,
    secrets_root: Path | None = None,
    log: logging.Logger | None = None,
) -> dict:
    """Runs one lease and returns its result envelope (it passes `result-envelope`).
    Never raises for a bad lease, bad code or a failing task. `deadline_s` (the
    request's deadline) caps `limits.timeout`."""
    log = log or _log
    credentials = dict(credentials or {})
    violations = validate("lease", doc)
    if violations:
        first = violations[0]
        return failed_envelope(
            "invalid_input", f"the lease is invalid: {first.path or '/'}: {first.message}"
        )
    try:
        spec = runspec_from_lease(doc, archive_cache=archive_cache)
    except LeaseError as exc:
        return failed_envelope(exc.code, exc.message, exc.scope)
    if deadline_s is not None:
        spec.timeout_s = min(spec.timeout_s or deadline_s, deadline_s)

    root = Path(secrets_root) if secrets_root else credentials_dir()
    task_dir: Path | None = None
    try:
        extra_env: dict[str, str] = {}
        redact = [v for v in credentials.values() if v]
        kubeconfig = _kubeconfig_value(doc, credentials)
        if kubeconfig:
            task_dir = _task_secrets_dir(root)
            path = task_dir / "kubeconfig"
            _write_secret_file(path, kubeconfig.encode("utf-8"))
            extra_env["KUBECONFIG"] = str(path)
        check_extra_env(extra_env)
        outcome = execute(spec, scope_dir=scope_dir, extra_env=extra_env, redact=redact, log=log)
        envelope = outcome.envelope
        check("result-envelope", envelope)
        return envelope
    except LeaseError as exc:
        return failed_envelope(exc.code, exc.message, exc.scope)
    except OSError as exc:
        return failed_envelope("internal", f"could not set up the task's credentials: {exc}")
    finally:
        if task_dir is not None:
            shutil.rmtree(task_dir, ignore_errors=True)


def _task_secrets_dir(root: Path) -> Path:
    """A fresh, randomly named 0700 directory for one task's secret files."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="task-", dir=root))


__all__ = [
    "LeaseError",
    "archive_cache_dir",
    "credentials_dir",
    "failed_envelope",
    "load_code",
    "run_lease",
    "runspec_from_lease",
]
