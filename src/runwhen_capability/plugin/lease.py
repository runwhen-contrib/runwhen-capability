"""Run a `runwhen.com/lease/v1` request (what `rwtask serve` gets from the runner for a
V13 task) and return its result envelope.

    envelope = run_lease(doc, credentials={...}, scope_dir=scope)

The lease (CONTRACTS.md "Lease") is validated against the `lease` contract; its code is
checked against the sha256 it names and read from memory (`code.inline`) or from the
archive cache (`code.archive`; on a miss it is fetched from the runner relay and cached); and
`runspec_from_lease` turns it into the `RunSpec` that `plugin.run.execute` runs. Nothing here
starts a process but `execute`, which is bundle.py's hardened child runner.

Every problem with the lease itself is an envelope, never an exception: `failed` with
`invalid_input` (the lease or a credential entry breaks its contract) or `internal`
(the code does not match its hash, cannot be read, or lacks the task's script) or
`dependency` (an archive that is not in the cache and could not be fetched from the relay).

Provenance (VER-6). Every envelope a lease produces carries `provenance` (the lease's task
ref, plugin, version, task hash, the code's archive sha256, the runtime image and digest,
`leaseId`, `taskRun`, and the first credential entry's `connection`): the ok, the failed,
the timed out, and every refusal after the lease validated (a hash mismatch, a missing
archive, a dry run the task does not support, plugin code switched off). A lease that does
not validate gets none: its fields cannot be trusted. It is added in one place
(`run_lease`, and `attach_provenance` for the opt-in refusal in serve.py), never by the
paths that build the envelope.

Credentials, new (lane C's contract): the poll response may also carry `materialized[]`
(`auth-plan#/$defs/materializedCredential`: the runner's native auth plans, already
executed). Each file is written under a fresh 0700 per-task directory of
`$RW_CREDENTIALS_DIR` (default /run/rw/credentials) with the entry's mode (default 0600),
its `env` variable exported pointing at it, and each `env{}` pair exported; the directory
is deleted when the task ends, whatever happened. Their names are held to the same
reserved list as any caller-supplied environment (RW_*, HOME, PATH, BASH*, LD_*, DYLD_*,
PYTHON*, the host-only variables): a runner cannot use this to reach SEC-2's
variables. The one exception is the platform-minted credential: a `files[].env` of
`RW_SYNC_CREDENTIAL` in an entry whose credentialType is `runwhen/sync` (run.py
PLATFORM_MINTED_FILE_ENV); the same name anywhere else, and every other RW_* name, is still
refused. Every value, and every file's content (a minted credential's token too), is
redacted from the envelope.

Credentials, script auth plans (contracts-v4). A `credentials[]` entry whose `authPlan.method`
is a `script` is run here, before the task, by plugin/auth_script.py (its module docstring is
the script contract): the plan's archive (the integration plugin's, which may be another than
the task's) is loaded like the task's code, but asked of the relay by hash (`GET .../code?sha256=`)
when it is not cached; the runner's `materialized[].reference` of the same credentialType is
written to `$RW_AUTH_REF_DIR/<key>`; and what the script produces joins the task's environment
(a file's path in `materialize.file.env`, or the `NAME=value` pairs) under the same reserved-name
check and redaction as a materialized credential. A k8s-secret, vault or file reference without
reference material is `invalid_input`; any failure is an envelope with scope `auth` and the task
script never starts. The auth script's directory (`auth-<n>/` in the task's) is deleted with the
rest when the task ends, and the request's deadline (`deadline_s`) bounds the scripts and the task
together. `RW_AUTH_*` exists only in the auth script's environment.

Credentials, old. Today's `credentials` map of the poll response (`{name: value}`) keeps its
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
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from .._redaction import Redactor
from ..context import Context
from ..contracts import ArchiveError, TreeEntry, archive_sha256, check, schema, validate
from ..contracts import archive as archive_mod
from ..envelope import derive_envelope
from ..errors import ArtifactError
from ..models import OutputDecl
from ..relay import TRANSPORT_ERROR, RelayClient
from .auth_script import MAX_SECONDS as MAX_AUTH_SECONDS
from .auth_script import REFERENCE_KEY, AuthScriptError, run_script_plan
from .run import (
    PLATFORM_MINTED_FILE_ENV,
    ArtifactUploader,
    ArtifactUploadError,
    RunSpec,
    _duration_seconds,
    check_extra_env,
    execute,
    minted_file_secrets,
)

_log = logging.getLogger("runwhen_capability.plugin.lease")

ARCHIVE_CACHE_ENV = "RW_ARCHIVE_CACHE"
#: The archive cache's directory name in the executor's workdir, which is not a scope.
ARCHIVE_CACHE_DIR = ".archives"
DEFAULT_ARCHIVE_CACHE = f"/work/{ARCHIVE_CACHE_DIR}"
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


def provenance_from_lease(lease: dict) -> dict:
    """The `provenance` of an envelope of this lease. `lease` must satisfy the `lease`
    contract. `connection` is the first credential entry that names one (a `runwhen/sync`
    entry does not)."""
    code = lease["code"]
    leased = lease["task"]
    provenance: dict[str, Any] = {
        "task": leased["ref"],
        "plugin": leased["plugin"],
        "version": leased["version"],
        "taskHash": leased["hash"],
        "archiveSha256": (code.get("archive") or code["inline"])["sha256"],
        "runtime": {"image": lease["runtime"]["image"], "digest": lease["runtime"]["digest"]},
        "leaseId": lease["leaseId"],
        "taskRun": lease["taskRun"],
    }
    connection = next(
        (c["connection"] for c in lease["credentials"] if isinstance(c.get("connection"), str)),
        None,
    )
    if connection is not None:
        provenance["connection"] = connection
    return provenance


def attach_provenance(envelope: dict, doc: Any) -> dict:
    """`envelope` with the provenance of the lease `doc`, unless `doc` does not satisfy the
    `lease` contract (then it is returned as it is). For a refusal made before `run_lease`."""
    if not validate("lease", doc):
        envelope["provenance"] = provenance_from_lease(doc)
    return envelope


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


_FETCH_CHUNK = 64 * 1024
_STALE_TMP_SECONDS = 3600
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")


def _remove_stale_temp_files(cache: Path) -> None:
    """Best effort: deletes the `.tmp` files of fetches that died mid-write (a killed
    pod) and are older than an hour; a fetch in flight is never that old."""
    cutoff = time.time() - _STALE_TMP_SECONDS
    try:
        for tmp in cache.glob("*.tar.*.tmp"):
            try:
                if tmp.stat().st_mtime < cutoff:
                    tmp.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _fetch_archive(
    relay: RelayClient, code_archive: dict, path: Path, *, by_hash: bool = False
) -> bytes:
    """The archive `code_archive` (`{sha256, size}`) names, fetched from the runner relay
    (`GET /v1/tasks/{requestId}/code`), checked and stored at `path` (the cache's
    `<hex>.tar`). The body is read at most `size` (and MAX_ARCHIVE_BYTES) bytes plus one,
    so an oversized or endless body is refused without being held; its length must be
    `size`, its `X-RW-Sha256` header (when sent) and its bytes must hash to the lease's
    sha256. Only bytes that pass are stored, written to a unique temporary file and renamed
    into place, so a concurrent fetch of the same archive in this pod, or a crash, never
    leaves a partial file under the final name. A failure is a `dependency` LeaseError
    that names the archive and the relay's code, never the URL or the token. `by_hash` asks
    the relay for the archive by its sha256 (an auth script's, not the request's own code)."""
    sha = code_archive["sha256"]
    size = code_archive["size"]
    if size > archive_mod.MAX_ARCHIVE_BYTES:
        raise LeaseError(
            "dependency",
            f"archive {sha} could not be fetched: its size, {size} bytes, is over the "
            f"{archive_mod.MAX_ARCHIVE_BYTES} allowed",
        )
    _remove_stale_temp_files(path.parent)
    cap = size
    try:
        with relay.open_code(sha if by_hash else None) as stream:
            header = stream.headers.get("X-RW-Sha256")
            if header is not None and header.removeprefix("sha256:").lower() != (
                sha.removeprefix("sha256:").lower()
            ):
                raise LeaseError(
                    "dependency", f"archive {sha} could not be fetched: the relay sent another one"
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = stream.read(min(_FETCH_CHUNK, cap + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > cap:
                    raise LeaseError(
                        "dependency",
                        f"archive {sha} could not be fetched: the relay sent more than the "
                        f"{cap} bytes allowed",
                    )
    except ArtifactError as exc:
        what = (
            "the relay has no code archive for this request"
            if exc.code == "E_ARTIFACT_UNDECLARED"
            else f"relay error {exc.code}"
        )
        raise LeaseError("dependency", f"archive {sha} could not be fetched: {what}") from None
    data = b"".join(chunks)
    if len(data) != size:
        raise LeaseError(
            "dependency",
            f"archive {sha} could not be fetched: the relay sent {len(data)} bytes, "
            f"the lease says {size}",
        )
    try:
        archive_mod.verify(data, sha)
    except ArchiveError:
        raise LeaseError(
            "dependency", f"archive {sha} could not be fetched: the bytes do not match its hash"
        ) from None
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(tmp, path)
        tmp = None
    except OSError as exc:
        # The archive is verified and in memory: run it, and fetch again next time.
        _log.warning("archive %s could not be cached at %s: %s", sha, path, exc)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return data


def load_code(
    code: dict,
    cache_dir: Path | None = None,
    relay: RelayClient | None = None,
    *,
    by_hash: bool = False,
) -> tuple[dict[str, bytes], frozenset[str]]:
    """(files by plugin-root path, executable paths) of a lease's `code`, after checking
    them against the sha256 the lease names. An archive that is not in the cache is
    fetched through `relay` and cached (`_fetch_archive`); with no relay it is a
    `dependency` error. A cached archive that fails its hash is fetched again once when
    there is a relay (`internal` error when there is none). `by_hash`: a fetch asks the
    relay for this archive by sha256 rather than for the request's own (auth scripts).
    Raises LeaseError."""
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
        if not _HEX_DIGEST.fullmatch(hex_digest):
            # the hex becomes a file name: nothing but a sha256 may reach the path
            raise LeaseError("invalid_input", f"archive sha256 {sha!r} is not sha256:<64 hex>")
        path = (cache_dir or archive_cache_dir()) / f"{hex_digest}.tar"
        try:
            data = path.read_bytes()
            try:
                archive_mod.verify(data, sha)
            except ArchiveError as exc:
                if relay is None:
                    raise LeaseError("internal", f"archive {sha} rejected: {exc}") from None
                # a corrupt cache entry heals itself: drop it and fetch it once more
                _log.warning("cached archive %s is corrupt (%s); fetching it again", sha, exc)
                try:
                    path.unlink()
                except OSError:
                    pass  # the fetch replaces it anyway
                data = _fetch_archive(relay, code["archive"], path, by_hash=by_hash)
        except FileNotFoundError:
            if relay is None:
                raise LeaseError(
                    "dependency",
                    f"archive {sha} is not in the cache and there is no runner relay to fetch "
                    "it from",
                ) from None
            data = _fetch_archive(relay, code["archive"], path, by_hash=by_hash)
        except OSError as exc:
            raise LeaseError("internal", f"archive {sha} could not be read: {exc}") from None
        try:
            entries = archive_mod.read(data)
        except ArchiveError as exc:
            raise LeaseError("internal", f"archive {sha} rejected: {exc}") from None
    return (
        {e.path: e.data for e in entries},
        frozenset(e.path for e in entries if e.executable),
    )


# -- the lease as a RunSpec ----------------------------------------------------------------


def runspec_from_lease(
    lease: dict, *, archive_cache: Path | None = None, relay: RelayClient | None = None
) -> RunSpec:
    """The one place a lease becomes a RunSpec. `lease` must already satisfy the `lease`
    contract. The compiled-task-shaped `task` carries what `execute` reads (ref, skill,
    name, script, readOnly, dryRun, outputs) and no `inputs`/`inputSchema`/`settings`:
    a lease's inputs and settings are resolved by papi and used as given. The target and
    its identity are the lease's own. `timeout_s` is `limits.timeout` and `memory` is
    `limits.resources.memory` (`cpu` is advisory and not read). The run is a dry run
    (`RW_DRY_RUN=1`, `dryRun: true` on the envelope) when the lease says `dryRun: true`; a
    task that does not declare `dryRun` refuses it (LeaseError invalid_input) before any
    code is read. An archive missing from `archive_cache` is fetched through `relay`.
    Raises LeaseError."""
    dry_run = bool(lease.get("dryRun"))
    if dry_run and not lease["task"].get("dryRun"):
        raise LeaseError("invalid_input", "task does not support dry run")
    files, executable = load_code(lease["code"], archive_cache, relay)
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
        dry_run=dry_run,
        timeout_s=_duration_seconds(lease["limits"]["timeout"]),
        memory=lease["limits"].get("resources", {}).get("memory"),
    )


# -- artifacts ----------------------------------------------------------------------------------

_ARTIFACT_REF_RE = re.compile(
    schema("common")["$defs"]["artifactRef"]["properties"]["artifact"]["pattern"]
)


def relay_uploader(
    relay: RelayClient,
    *,
    plugin: str,
    task_name: str,
    scope_dir: Path,
    output_decls: Sequence[OutputDecl],
    deadline_at: float | None = None,
) -> ArtifactUploader:
    """An `execute` artifact uploader that stores each artifact output through the
    runner's relay -- the same `ctx.artifacts.add_file` call (busy retries included) that
    the bundle path's `_upload_artifacts` makes -- and returns the envelope value
    `{artifact: <relay ref>, contentType, size}` (`truncated: true` when the relay says it
    stored less). The relay's ref must satisfy the contract's artifactRef
    (`^art_[A-Za-z0-9_-]+$`); one that does not is an `internal` error and the output is
    dropped, rather than a value papi would reject. The task name the relay sees is the one
    the request's own `outputs` declare the artifact under, else the lease's task name."""
    declared = {d.name: d.task for d in output_decls if d.artifact}

    def upload(name: str, handle: BinaryIO, content_type: str) -> dict:
        if deadline_at is not None and time.monotonic() >= deadline_at:
            raise ArtifactUploadError(
                "timeout", f"the request's deadline passed before artifact {name!r} was uploaded"
            )
        task = declared.get(name, task_name)
        ctx = Context(
            capability=plugin,
            operation=task,
            workdir=scope_dir,
            relay=relay,
            task=task,
            outputs=output_decls,
        )
        try:
            ref = ctx.artifacts.add_file(handle, name, content_type=content_type)
        except ArtifactError as exc:
            code = "unreachable" if exc.code == TRANSPORT_ERROR else "storage"
            raise ArtifactUploadError(code, f"artifact {name!r}: {exc.code}: {exc}") from None
        except OSError as exc:
            raise ArtifactUploadError("storage", f"artifact {name!r}: {exc}") from None
        if not _ARTIFACT_REF_RE.match(ref.ref):
            raise ArtifactUploadError(
                "internal",
                f"artifact {name!r}: the relay returned the ref {ref.ref!r}, which is not an "
                f"artifactRef ({_ARTIFACT_REF_RE.pattern})",
            )
        value: dict[str, Any] = {
            "artifact": ref.ref,
            "contentType": content_type,
            "size": ref.size_bytes,
        }
        if ref.truncated:
            value["truncated"] = True
        return value

    return upload


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


_MODE_RE = re.compile(r"^0[0-7]{3}$")


@dataclass(frozen=True)
class _Materialized:
    files: dict[str, tuple[bytes, int]]  # env var -> (content, mode)
    env: dict[str, str]
    secrets: list[str]  # every string to scrub from what comes back
    # PLATFORM_MINTED_FILE_ENV names set as a file of their own credential type's entry
    allowed_env: frozenset[str] = frozenset()
    # credentialType -> reference material (key, content) for that type's script auth plan
    references: dict[str, list[tuple[str, bytes]]] = field(default_factory=dict)


def _parse_materialized(items: Sequence[Any]) -> _Materialized:
    """The `materialized[]` of the poll response, checked against materializedCredential.
    Raises LeaseError(invalid_input) naming the entry and the problem, never a value."""
    env_pattern = re.compile(schema("common")["$defs"]["envVarName"]["pattern"])
    files: dict[str, tuple[bytes, int]] = {}
    env: dict[str, str] = {}
    secrets: list[str] = []
    allowed: set[str] = set()
    references: dict[str, list[tuple[str, bytes]]] = {}

    def bad(index: int, problem: str) -> LeaseError:
        return LeaseError("invalid_input", f"materialized[{index}]: {problem}")

    def claim(index: int, name: str, minted_name: str | None = None) -> None:
        """`minted_name`: the one RW_* name this entry's credential type may set as a file."""
        if not env_pattern.match(name):
            raise bad(index, f"{name!r} is not a valid environment variable name")
        try:
            check_extra_env({name: ""}, allow=(minted_name,) if name == minted_name else ())
        except ValueError:
            raise bad(index, f"{name!r} is a reserved environment variable") from None
        if name in files or name in env:
            raise bad(index, f"{name!r} is set twice")

    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("credentialType"), str):
            raise bad(index, "not a materializedCredential (credentialType is required)")
        if set(item) - {"credentialType", "files", "env", "reference"}:
            raise bad(index, "unknown field")
        if "reference" in item:
            if item.get("files") or item.get("env"):
                raise bad(index, "reference cannot come with files or env")
            if item["credentialType"] in references:
                raise bad(index, f"a second reference for {item['credentialType']!r}")
            references[item["credentialType"]] = _parse_reference(
                item["reference"], index, secrets, bad
            )
            continue
        for entry in item.get("files") or []:
            if not isinstance(entry, dict) or set(entry) - {"env", "mode", "content", "encoding"}:
                raise bad(index, "a file has an unknown field or is not an object")
            name, content = entry.get("env"), entry.get("content")
            if not isinstance(name, str) or not isinstance(content, str):
                raise bad(index, "a file needs a string env and content")
            mode = entry.get("mode", "0600")
            if not isinstance(mode, str) or not _MODE_RE.match(mode):
                raise bad(index, f"file {name!r}: mode must look like 0600")
            encoding = entry.get("encoding", "utf-8")
            if encoding not in ("utf-8", "base64"):
                raise bad(index, f"file {name!r}: encoding must be utf-8 or base64")
            minted_name = PLATFORM_MINTED_FILE_ENV.get(item["credentialType"])
            claim(index, name, minted_name)
            if name == minted_name:
                allowed.add(name)
            if encoding == "base64":
                try:
                    data = base64.b64decode(content, validate=True)
                except (binascii.Error, ValueError):
                    raise bad(index, f"file {name!r}: content is not valid base64") from None
                secrets.append(content)
            else:
                data = content.encode("utf-8")
            try:
                secrets.append(data.decode("utf-8"))
            except UnicodeDecodeError:
                pass
            if name == minted_name:
                secrets.extend(minted_file_secrets(data))  # the token alone, too
            files[name] = (data, int(mode, 8))
        env_pairs = item.get("env") or {}
        if not isinstance(env_pairs, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env_pairs.items()
        ):
            raise bad(index, "env must map names to strings")
        for name, value in env_pairs.items():
            claim(index, name)
            env[name] = value
            secrets.append(value)
    return _Materialized(files, env, [x for x in secrets if x], frozenset(allowed), references)


def _parse_reference(
    reference: Any, index: int, secrets: list[str], bad: Callable[[int, str], LeaseError]
) -> list[tuple[str, bytes]]:
    """The `reference.files` of a materialized entry as (key, content): checked against
    materializedCredential, every content (and its base64 text) added to `secrets`."""
    if not isinstance(reference, dict) or set(reference) - {"files"}:
        raise bad(index, "reference must be {files: [...]}")
    items = reference.get("files")
    if not isinstance(items, list) or not items:
        raise bad(index, "reference.files must be a non-empty list")
    out: list[tuple[str, bytes]] = []
    for entry in items:
        if not isinstance(entry, dict) or set(entry) - {"key", "content", "encoding"}:
            raise bad(index, "a reference file has an unknown field or is not an object")
        key, content = entry.get("key"), entry.get("content")
        if not isinstance(key, str) or not isinstance(content, str):
            raise bad(index, "a reference file needs a string key and content")
        if not REFERENCE_KEY.match(key) or key in (".", ".."):
            raise bad(index, f"reference key {key!r} is not a file name")
        if any(key == other for other, _ in out):
            raise bad(index, f"reference key {key!r} is given twice")
        encoding = entry.get("encoding", "utf-8")
        if encoding not in ("utf-8", "base64"):
            raise bad(index, f"reference file {key!r}: encoding must be utf-8 or base64")
        if encoding == "base64":
            try:
                data = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError):
                raise bad(index, f"reference file {key!r}: content is not valid base64") from None
            secrets.append(content)
        else:
            data = content.encode("utf-8")
        try:
            secrets.append(data.decode("utf-8"))
        except UnicodeDecodeError:
            pass
        out.append((key, data))
    return out


def wipe_stale_credentials(root: Path | None = None) -> None:
    """Removes per-task directories (`task-*`) a host that died mid-task left in the
    credentials directory: they hold secret files nothing will ever clean up."""
    root = Path(root) if root else credentials_dir()
    if not root.is_dir():
        return
    for child in root.iterdir():
        if child.name.startswith("task-") and child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)


# -- run ---------------------------------------------------------------------------------------


def run_lease(
    doc: Any,
    *,
    scope_dir: Path,
    credentials: Mapping[str, str] | None = None,
    materialized: Sequence[Any] = (),
    deadline_s: float | None = None,
    archive_cache: Path | None = None,
    secrets_root: Path | None = None,
    relay: RelayClient | None = None,
    output_decls: Sequence[OutputDecl] = (),
    log: logging.Logger | None = None,
) -> dict:
    """Runs one lease and returns its result envelope (it passes `result-envelope`).
    Never raises for a bad lease, bad code or a failing task. `deadline_s` (the
    request's deadline) caps `limits.timeout`. `materialized` is the poll response's
    `materialized[]`; `secrets_root` is where its files go (default: credentials_dir()).
    With a `relay` (and the request's `output_decls`), artifact outputs are stored through
    it; without one they stay in the scope."""
    log = log or _log
    credentials = dict(credentials or {})
    violations = validate("lease", doc)
    if violations:
        first = violations[0]
        return failed_envelope(
            "invalid_input", f"the lease is invalid: {first.path or '/'}: {first.message}"
        )
    envelope = _run_valid_lease(
        doc,
        scope_dir=scope_dir,
        credentials=credentials,
        materialized=materialized,
        deadline_s=deadline_s,
        archive_cache=archive_cache,
        secrets_root=secrets_root,
        relay=relay,
        output_decls=output_decls,
        log=log,
    )
    envelope["provenance"] = provenance_from_lease(doc)
    check("result-envelope", envelope)
    return envelope


def _run_valid_lease(
    doc: dict,
    *,
    scope_dir: Path,
    credentials: Mapping[str, str],
    materialized: Sequence[Any],
    deadline_s: float | None,
    archive_cache: Path | None,
    secrets_root: Path | None,
    relay: RelayClient | None,
    output_decls: Sequence[OutputDecl],
    log: logging.Logger,
) -> dict:
    """The envelope of a lease that satisfies the `lease` contract, without provenance."""
    started = time.monotonic()

    def elapsed() -> float:
        return time.monotonic() - started

    try:
        spec = runspec_from_lease(doc, archive_cache=archive_cache, relay=relay)
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
        parsed = _parse_materialized(materialized)
        redact.extend(parsed.secrets)
        plans = _script_plans(doc)
        _check_reference_material(plans, parsed)
        if kubeconfig or parsed.files or plans:
            task_dir = _task_secrets_dir(root)
        if kubeconfig:
            path = task_dir / "kubeconfig"
            _write_secret_file(path, kubeconfig.encode("utf-8"))
            extra_env["KUBECONFIG"] = str(path)
        for name, (data, mode) in parsed.files.items():
            _write_secret_file(task_dir / name, data, mode)
            extra_env[name] = str(task_dir / name)
        extra_env.update(parsed.env)
        if plans:
            assert task_dir is not None
            _run_script_plans(
                plans,
                parsed=parsed,
                task_dir=task_dir,
                extra_env=extra_env,
                redact=redact,
                relay=relay,
                archive_cache=archive_cache,
                remaining=lambda: None if deadline_s is None else deadline_s - elapsed(),
                log=log,
            )
            if deadline_s is not None:
                # the request's deadline covers the auth scripts too
                left = deadline_s - elapsed()
                if left <= 0:
                    raise LeaseError(
                        "timeout", "the request's deadline passed while the auth ran", "auth"
                    )
                spec.timeout_s = min(spec.timeout_s or left, left)
        check_extra_env(extra_env, allow=parsed.allowed_env)
        uploader = None
        if relay is not None:
            uploader = relay_uploader(
                relay,
                plugin=doc["task"]["plugin"],
                task_name=doc["task"]["name"],
                scope_dir=scope_dir,
                output_decls=output_decls,
                deadline_at=None if deadline_s is None else started + deadline_s,
            )
        outcome = execute(
            spec,
            scope_dir=scope_dir,
            extra_env=extra_env,
            allow_env=parsed.allowed_env,
            redact=redact,
            artifact_uploader=uploader,
            log=log,
        )
        return outcome.envelope
    except LeaseError as exc:
        return failed_envelope(exc.code, exc.message, exc.scope)
    except OSError as exc:
        return failed_envelope("internal", f"could not set up the task's credentials: {exc}")
    finally:
        if task_dir is not None:
            shutil.rmtree(task_dir, ignore_errors=True)


# -- script auth plans -------------------------------------------------------------------------

#: Reference kinds whose credential is ambient or mounted by the pod: no material is sent.
_NO_MATERIAL_KINDS = frozenset({"in-cluster-sa", "cloud-identity"})


def _script_plans(lease: dict) -> list[dict]:
    """The `authPlan` of each lease credential whose method is a script, in lease order."""
    return [
        entry["authPlan"]
        for entry in lease["credentials"]
        if "authPlan" in entry and "script" in entry["authPlan"]["method"]
    ]


def _check_reference_material(plans: Sequence[dict], parsed: _Materialized) -> None:
    """A script plan whose reference is a secret, vault path or file needs the runner's
    `materialized[].reference` for its credential type (invalid_input, before any code is
    fetched); in-cluster-sa and cloud-identity need none."""
    for plan in plans:
        credential_type = plan["credentialType"]
        if (
            credential_type not in parsed.references
            and plan["reference"]["kind"] not in _NO_MATERIAL_KINDS
        ):
            raise LeaseError(
                "invalid_input", f"no reference material for {credential_type}", "auth"
            )


def _run_script_plans(
    plans: Sequence[dict],
    *,
    parsed: _Materialized,
    task_dir: Path,
    extra_env: dict[str, str],
    redact: list[str],
    relay: RelayClient | None,
    archive_cache: Path | None,
    remaining: Callable[[], float | None],
    log: logging.Logger,
) -> None:
    """Runs each script plan (plugin/auth_script.py) before the task script and adds what it
    produced to `extra_env` and its values to `redact`. Raises LeaseError (scope `auth`)
    for the first plan that fails: the task script then never runs. Each plan gets
    `<task_dir>/auth-<n>/`, which the caller deletes with the task's directory."""
    for number, plan in enumerate(plans):
        left = remaining()
        if left is not None and left <= 0:
            raise LeaseError(
                "timeout", "the request's deadline passed before the auth script ran", "auth"
            )
        timeout_s = MAX_AUTH_SECONDS if left is None else min(MAX_AUTH_SECONDS, left)
        try:
            files, executable = load_code(
                {"archive": plan["method"]["script"]["archive"]}, archive_cache, relay, by_hash=True
            )
        except LeaseError as exc:
            raise LeaseError(exc.code, f"auth script archive: {exc.message}", "auth") from None
        try:
            result = run_script_plan(
                plan,
                files=files,
                executable=executable,
                reference=parsed.references.get(plan["credentialType"], []),
                work_dir=task_dir / f"auth-{number}",
                timeout_s=timeout_s,
                redactor=Redactor([*redact]),
                taken=extra_env,
                log=log,
            )
        except AuthScriptError as exc:
            raise LeaseError(exc.code, exc.message, exc.scope) from None
        extra_env.update(result.env)
        redact.extend(result.secrets)


def _task_secrets_dir(root: Path) -> Path:
    """A fresh, randomly named 0700 directory for one task's secret files."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="task-", dir=root))


__all__ = [
    "ARCHIVE_CACHE_DIR",
    "LeaseError",
    "archive_cache_dir",
    "attach_provenance",
    "credentials_dir",
    "failed_envelope",
    "load_code",
    "provenance_from_lease",
    "relay_uploader",
    "run_lease",
    "runspec_from_lease",
    "wipe_stale_credentials",
]
