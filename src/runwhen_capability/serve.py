"""`rwtask serve` -- long-polls the runner over plain HTTP/JSON (not Connect).

Relay HTTP contract (this is the wire the runner must implement to match):

    POST {relay}/v1/tasks/next
        body: {"poolId": "<poolId>"}
        -> 200 {"requestId", "request", "credentials": {name: value}, "scopeId", "deadlineMs",
                "maxResultBytes"?, "outputs"?, "inputArtifacts"?}
           (the "request" field is a RequestEnvelope; see models.py)
        -> 204 when idle (the server may hold the connection up to ~30s)

    POST {relay}/v1/tasks/{requestId}/result
        body: {"status": "ok"|"failed"|"timeout", "result": {...}, "error": "..."}
           ("result" is a ResultEnvelope, present only on status "ok")

Every relay call carries `Authorization: Bearer <token>`. The token comes from the
executor token file -- --token-file, else the RW_EXECUTOR_TOKEN_FILE env var, else the
legacy EXECUTOR_TOKEN_FILE, else DEFAULT_TOKEN_FILE -- which is read and then unlinked
(S1 / SEC-2): task processes run as the same uid in the same pod, so a token that stays on
disk is a token any task can `cat`. What happens next depends on whether the unlink worked,
and is logged once:

- It worked (the runner's own tmpfs copy, which the runner never rotates in place; a
  restart is a new pod): the token lives in memory only and is never read again.
- It was refused (a read-only Secret volume, EACCES -- today's runner): the file cannot be
  hidden anyway, and the kubelet refreshes a mounted Secret in place when it rotates, so
  the file is re-read right before every relay call. If that re-read fails or finds the
  file empty, the last token read is used, and logged, rather than dropping a result
  already computed (see relay.TokenSource).

The file need not be there at start-up (an image smoke test runs `rwtask serve` with no
token; a pod may start before its mount does). A missing, unreadable or empty file is
warned about once and serve loads its capability and carries on: each poll tries the file
again and, while there is no token, is skipped (sleeping RETRY_DELAY) -- never sent
unauthenticated. The first read that works applies the rule above, once. Before any of that
the process makes itself non-dumpable (`_make_non_dumpable`), in every mode, so a task cannot
read the in-memory copy out of /proc.

A request may carry `maxResultBytes`. The result body is serialised (compact
JSON) before it is posted, and an `ok` result whose body is larger than that
is replaced by `{"status": "failed", "error": "E_RESULT_TOO_LARGE: ..."}`
instead -- the runner refuses a bigger body anyway, and a failed result it
accepts says why. `outputs`/`inputArtifacts` on the request are the lease's
artifact declarations; they and a `RelayClient` for the request (the
artifact routes -- see relay.py) travel with it into the Context each task
gets. A result with no `maxResultBytes`, or one of 0 or less (the lease's
"unset"), is never capped here.

A `request` that carries a `bundle` (a custom capability's code, inline --
see bundle.py) is run only when this executor was started with
`--allow-bundles` (or RW_ALLOW_BUNDLES=1); otherwise it is refused with a
failed result. Packaged-capability images never set it.

A `request` whose JSON has `apiVersion: runwhen.com/lease/v1` is a V13 lease (see
plugin/lease.py). It needs the same opt-in as a bundle: hash verification proves the code
is what the lease names, not that it is trustworthy, and papi sends workspace skills inline
as arbitrary code. Without `--allow-bundles` (or RW_ALLOW_BUNDLES=1) it completes as a
failed envelope with `invalid_input` (PLUGIN_CODE_NOT_ALLOWED) and nothing is written or
run (the envelope carries the lease's provenance when the lease is valid). Otherwise it is
validated against the `lease` contract and run through the same child runner as a bundle,
in its own scope, which is always wiped. The poll response's
`materialized[]` (secret files and variables the runner produced) is written under
`$RW_CREDENTIALS_DIR`, exported to the task and deleted at its end. The result POSTed is
`{"status": "ok", "result": <the bare D64 result envelope>}` -- not the classic
`{version, setup, tasks}` -- including when the envelope's own status is `failed`;
`status: failed` is only for a request this executor could not process at all.

`scopeId` must be a single path segment (see _is_safe_scope_id): the loop
creates and later deletes <workdir>/<scopeId>/, so anything else -- empty,
absolute, containing "/" or ".." -- is refused as a failed request before
the filesystem is touched. So is ".rw-artifact-cache", the one directory of
<workdir> that is not a scope: the cache of input artifacts that
`ctx.artifacts.input_path` fills (see _artifact_cache.py). It sits beside the
scopes, not in one, so that a stateless scope's wipe does not empty it, and
every request is told where it is. And ".archives", the cache of plugin code
archives a V13 lease fetches from the relay (plugin/lease.py).

Per request: create <workdir>/<scopeId>/, chdir there, run setup then each
task in request.tasks order (host.run_request), aggregate into the result
envelope, POST it. What happens to the scope dir next depends on the loaded
capability's `execution.mode`:

- `stateless` -- delete the scope dir.
- `stateful` -- keep it. A later request carrying the same scopeId finds its
  setup cached (host.py's setup-output cache) and its materialised tree
  still there, instead of re-cloning. Growth is bounded: this pod keeps at
  most `max_stateful_scopes` scopes warm, evicting the least-recently-used
  one (and wiping its directory) when a new scopeId would exceed the cap.
  A scope is never read by a request carrying a different scopeId -- each
  lives in its own <workdir>/<scopeId>/ directory.

Never raises out of the loop -- a poll or post failure is logged and
retried; a request that fails to execute becomes a "failed" PutResult, not
a crash.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import sys
import time
from collections import OrderedDict
from pathlib import Path

import requests

from .artifacts import ARTIFACT_CACHE_DIR
from .bundle import run_bundle_request
from .host import run_request
from .loader import discover_capability_dir, load_capability
from .models import BundleRequestEnvelope, TaskHostRequest, V13LeaseRequest
from .plugin.lease import (
    ARCHIVE_CACHE_DIR,
    attach_provenance,
    failed_envelope,
    run_lease,
    wipe_stale_credentials,
)
from .relay import RelayClient, TokenNotPresent, TokenSource

POLL_TIMEOUT = 35  # seconds; a little over the relay's ~30s long-poll hold
RESULT_TIMEOUT = 30  # seconds
RETRY_DELAY = 5  # seconds, on a poll/post transport failure

DEFAULT_TOKEN_FILE = "/var/run/executor/token"
TOKEN_FILE_ENV = "RW_EXECUTOR_TOKEN_FILE"
LEGACY_TOKEN_FILE_ENV = "EXECUTOR_TOKEN_FILE"

_PR_SET_DUMPABLE = 4  # <linux/prctl.h>

PLUGIN_CODE_NOT_ALLOWED = (
    "this executor does not run plugin code (start with --allow-bundles, or RW_ALLOW_BUNDLES=1)"
)

BUNDLES_NOT_ALLOWED = (
    "this executor does not run bundle requests: it was started without --allow-bundles "
    "(or RW_ALLOW_BUNDLES=1). Route custom capabilities to an image built to run them."
)

# Bounds how many distinct scopeIds a stateful pod keeps warm at once. Each
# one holds a full checkout on disk, and one pod is handed several scopeIds
# over its life (a stateful pool is sticky-routed but not one-scope-per-pod),
# so this must stay small rather than growing until disk fills. 4 gives a
# working set for the handful of scopes a single warm pod is realistically
# juggling concurrently or in close succession, while still bounding worst-case disk to a few
# checkouts, not dozens.
DEFAULT_MAX_STATEFUL_SCOPES = 4


def serve(
    relay: str,
    pool_id: str,
    workdir: Path,
    capability_dir: Path | None = None,
    token_file: Path | None = None,
    max_iterations: int | None = None,
    session: requests.Session | None = None,
    log: logging.Logger | None = None,
    max_stateful_scopes: int = DEFAULT_MAX_STATEFUL_SCOPES,
    allow_bundles: bool = False,
) -> None:
    """Runs the long-poll loop. `max_iterations` (None = forever) and
    `session` exist so tests can drive this deterministically without a real
    relay or an infinite loop. `max_stateful_scopes` bounds how many warm
    scopes a `stateful` capability keeps on disk at once (n/a for
    `stateless` capabilities, which never keep one); tests lower it to
    exercise eviction without dozens of iterations.

    `allow_bundles` (CLI: `--allow-bundles`, or RW_ALLOW_BUNDLES=1) opts
    this executor into bundle mode. Off by default: a bundle request
    carries arbitrary code, and only an image built to run custom
    capabilities should ever execute one. With it off, a bundle request
    is refused with a failed result before anything touches the
    filesystem. A V13 lease (plugin code carried inline or by archive) needs the
    same opt-in and is refused the same way, as a failed envelope."""
    log = log or logging.getLogger("runwhen_capability.serve")
    # First, before the token can be in memory: see _make_non_dumpable.
    _make_non_dumpable(log)
    tokens = _load_executor_token(_resolve_token_file(token_file), log)

    session = session or requests.Session()
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    _wipe_stale_bundle_scopes(workdir, log)
    wipe_stale_credentials()

    cap_dir = Path(capability_dir) if capability_dir else discover_capability_dir()
    capability = load_capability(cap_dir)
    log.info(
        "serving capability %r (execution.mode=%s) from %s; bundle requests %s",
        capability.capability_id,
        capability.execution_mode,
        cap_dir,
        "accepted" if allow_bundles else "refused",
    )

    # LRU of scopeIds this pod is currently keeping warm, for `stateful`
    # capabilities only. Lives for the process's lifetime -- a fresh pod
    # (or a runner restart, which wipes every executor) starts with none.
    stateful_scopes: OrderedDict[str, None] = OrderedDict()

    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        _poll_once(
            session,
            relay,
            pool_id,
            workdir,
            capability,
            tokens,
            log,
            stateful_scopes,
            max_stateful_scopes,
            allow_bundles,
        )


def _make_non_dumpable(log: logging.Logger | None = None) -> bool:
    """Marks this process non-dumpable (`prctl(PR_SET_DUMPABLE, 0)`) and says whether it
    worked. Linux only; elsewhere it does nothing.

    Why: the executor token lives in this process's memory, and task children run as the
    same uid. A dumpable process's /proc/<pid>/mem and /proc/<pid>/environ are readable by
    every process of its uid (and it can be ptrace-attached by one), so a task could lift
    the token out of the serve process. Non-dumpable makes those files root-owned and the
    attach refuse, unless the reader has CAP_SYS_PTRACE -- which a task does not. Children
    are not affected: the flag is reset to dumpable on every execve, which is fine, since
    they hold no token (not in env, argv or an open fd). Called first in `serve`, before the
    token is read, so there is no window in which it is in memory and the process is
    dumpable. A failure is logged with its errno and serve carries on: it hardens, it is not
    what the token's confidentiality rests on alone (the file is unlinked, the env stripped)."""
    log = log or logging.getLogger("runwhen_capability.serve")
    if not sys.platform.startswith("linux"):
        log.debug("not making the process non-dumpable: not Linux (%s)", sys.platform)
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
    except (OSError, AttributeError) as exc:
        log.warning("could not make the process non-dumpable: %s", exc)
        return False
    if result != 0:
        err = ctypes.get_errno()
        log.warning(
            "could not make the process non-dumpable: prctl(PR_SET_DUMPABLE, 0) failed, "
            "errno %d (%s)",
            err,
            os.strerror(err),
        )
        return False
    return True


def _resolve_token_file(token_file: Path | None) -> Path:
    """--token-file, else $RW_EXECUTOR_TOKEN_FILE, else the legacy $EXECUTOR_TOKEN_FILE,
    else DEFAULT_TOKEN_FILE. An empty variable counts as unset."""
    if token_file:
        return Path(token_file)
    return Path(
        os.environ.get(TOKEN_FILE_ENV)
        or os.environ.get(LEGACY_TOKEN_FILE_ENV)
        or DEFAULT_TOKEN_FILE
    )


def _load_executor_token(path: Path, log) -> TokenSource:
    """The `TokenSource` every relay call uses (see the module docstring and
    relay.TokenSource): the token file is read and unlinked now if it is there, else the
    source is pending and the poll loop waits for it. Never exits for a missing token."""
    return TokenSource.from_file(path, log)


def _wipe_stale_bundle_scopes(workdir: Path, log) -> None:
    """A bundle request's scope is wiped as soon as the request ends, so
    one still here at startup was left by a host that died mid-request
    (an OOM kill, an evicted container restarting onto the same volume) --
    and may still hold that request's bundle and secret files. A bundle
    scope is recognised by the `.rw-sdk/rw.sh` bundle.py writes into every
    one; nothing else in `workdir` -- a stateful packaged capability's warm
    scopes included -- is touched. Assumes, as the stateful LRU already
    does, that `workdir` belongs to this one serve process."""
    for child in workdir.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        if (child / ".rw-sdk" / "rw.sh").is_file():
            log.info("removing bundle scope %r left behind by an earlier run", child.name)
            shutil.rmtree(child, ignore_errors=True)


def _is_safe_scope_id(scope_id: str) -> bool:
    """A scopeId is used as ONE directory name under workdir, and that
    directory is later rmtree'd (a stateless wipe, or an LRU eviction).
    Anything that is not a single ordinary path segment makes
    `workdir / scopeId` land somewhere else entirely -- `Path("/work") / "/x"`
    is `/x`, `Path("/work") / ""` is `/work` itself, and `".."` walks up --
    so a malformed scopeId would delete something that is not a scope. The
    relay is authenticated, so this is not the security boundary; it is the
    guard that keeps a bug on the other side of the wire from costing this
    pod its whole workdir.

    The name of the input-artifact cache, a directory of `workdir` too, is
    refused as well: a scope called that would be wiped after its request and
    take every cached artifact with it. So is the plugin archive cache
    (`/work/.archives` by default, see plugin/lease.py)."""
    return (
        scope_id not in ("", ".", "..", ARTIFACT_CACHE_DIR, ARCHIVE_CACHE_DIR)
        and scope_id == Path(scope_id).name
    )


def _encode_result(payload: dict, log, max_result_bytes: int | None = None) -> bytes:
    """Serialises one PutResult body (compact JSON), and is where the inline
    result cap lives. Only an `ok` payload is ever replaced, and only when
    the request carried a positive `max_result_bytes`: if the encoded body is
    bigger, it becomes a `failed` payload saying so -- the runner would
    refuse the oversized body, and a small failed result is one it accepts
    and can show. None or a value <= 0 means "unset" (the lease's 0 may be
    forwarded raw), and no cap is applied here; the runner enforces its own
    ceiling then. Failed payloads are never capped; they are an error
    message, not data a task produced.

    A payload that cannot be encoded as JSON (a NaN or a non-JSON type in a
    task's output) likewise becomes a failed payload, so the caller always
    has bytes to post and the loop never raises."""
    try:
        body = _dumps(payload)
    except (TypeError, ValueError) as exc:
        log.error("result is not valid JSON: %s", exc)
        return _dumps({"status": "failed", "error": f"result is not valid JSON: {exc}"})
    if (
        max_result_bytes is None
        or max_result_bytes <= 0
        or payload.get("status") != "ok"
        or len(body) <= max_result_bytes
    ):
        return body
    log.error(
        "result is %d bytes, over the request's maxResultBytes (%d); posting a failure instead",
        len(body),
        max_result_bytes,
    )
    return _dumps(
        {
            "status": "failed",
            "error": (
                f"E_RESULT_TOO_LARGE: result is {len(body)} bytes; "
                f"maxResultBytes is {max_result_bytes}; write large data with ctx.artifacts"
            ),
        }
    )


def _dumps(payload: dict) -> bytes:
    # Compact separators keep the size we measure small and equal to what is
    # sent. Like `requests`' json= (which this replaced), it escapes non-ASCII
    # (so encoding can never fail on a stray surrogate) and refuses NaN.
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()


def _post_result(
    session,
    relay: str,
    request_id: str,
    body: bytes,
    tokens: TokenSource,
    log,
) -> None:
    """POSTs one PutResult; `body` is the JSON that `_encode_result` made. The token comes
    from `tokens.get()` immediately before posting: in the re-reading mode a request can
    run up to the manifest's requestTimeoutSeconds before this runs, long enough for the
    mounted Secret to rotate, and a stale token would lose a result already computed to a
    401. If that re-read fails, `get()` falls back to the last token read, and says so. (A
    request was polled, so the source is never pending here.)

    The timeout grows by a second per MiB of body: a result near the cap
    takes real time to upload, and the flat RESULT_TIMEOUT is sized for a
    small one.

    A transport failure or a non-2xx is logged and dropped -- the runner's
    lease expiry is what recovers the request -- but it is never treated as
    a delivered result: an unlogged 401/500 here is a result that vanished
    with nothing to explain it."""
    headers = {"Authorization": f"Bearer {tokens.get()}", "Content-Type": "application/json"}

    try:
        resp = session.post(
            f"{relay}/v1/tasks/{request_id}/result",
            data=body,
            timeout=RESULT_TIMEOUT + len(body) // (1 << 20),
            headers=headers,
        )
    except requests.RequestException as exc:
        log.error("post %s/v1/tasks/%s/result failed: %s", relay, request_id, exc)
        return
    if not 200 <= resp.status_code < 300:
        log.error(
            "post %s/v1/tasks/%s/result: unexpected status %s -- result not delivered",
            relay,
            request_id,
            resp.status_code,
        )


def _retain_or_wipe_scope(
    capability,
    scope_dir: Path,
    workdir: Path,
    stateful_scopes: OrderedDict[str, None],
    max_stateful_scopes: int,
    log,
) -> None:
    """`stateless` (default): unchanged -- wipe the scope unconditionally.

    `stateful`: keep it. Recorded as most-recently-used in `stateful_scopes`
    (keyed by scopeId, i.e. `scope_dir.name`); once that set would exceed
    `max_stateful_scopes`, the least-recently-used scope is evicted and its
    directory wiped -- the only cross-scope interaction that ever happens,
    and it only ever deletes, never reads, another scope's files."""
    if capability.execution_mode != "stateful":
        shutil.rmtree(scope_dir, ignore_errors=True)
        return

    scope_id = scope_dir.name
    stateful_scopes.pop(scope_id, None)  # re-insert at the MRU end
    stateful_scopes[scope_id] = None
    while len(stateful_scopes) > max_stateful_scopes:
        evicted_id, _ = stateful_scopes.popitem(last=False)
        log.info("evicting least-recently-used scope %r to bound stateful growth", evicted_id)
        shutil.rmtree(workdir / evicted_id, ignore_errors=True)


def _poll_once(
    session,
    relay: str,
    pool_id: str,
    workdir: Path,
    capability,
    tokens: TokenSource,
    log,
    stateful_scopes: OrderedDict[str, None],
    max_stateful_scopes: int,
    allow_bundles: bool = False,
) -> None:
    try:
        token = tokens.get()
    except TokenNotPresent:
        # No token yet: skip the poll (never poll unauthenticated) and try again shortly.
        time.sleep(RETRY_DELAY)
        return
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = session.post(
            f"{relay}/v1/tasks/next",
            json={"poolId": pool_id},
            timeout=POLL_TIMEOUT,
            headers=headers,
        )
    except requests.RequestException as exc:
        log.error("poll %s/v1/tasks/next failed: %s", relay, exc)
        time.sleep(RETRY_DELAY)
        return

    if resp.status_code == 204:
        return
    if resp.status_code != 200:
        log.error("poll %s/v1/tasks/next: unexpected status %s", relay, resp.status_code)
        time.sleep(RETRY_DELAY)
        return

    try:
        task_request = TaskHostRequest.model_validate(resp.json())
    except Exception as exc:  # noqa: BLE001 -- a malformed relay response must not crash the loop
        log.error("poll %s/v1/tasks/next: malformed response: %s", relay, exc)
        time.sleep(RETRY_DELAY)  # a relay stuck on malformed 200s must not be hot-looped
        return

    if not _is_safe_scope_id(task_request.scopeId):
        # Reported as a failed request rather than swallowed: the runner must
        # learn why nothing ran. Nothing has touched the filesystem yet.
        log.error(
            "request %s: refusing scopeId %r -- not a single path segment, or a reserved name",
            task_request.requestId,
            task_request.scopeId,
        )
        _post_result(
            session,
            relay,
            task_request.requestId,
            _encode_result(
                {"status": "failed", "error": f"invalid scopeId {task_request.scopeId!r}"}, log
            ),
            tokens,
            log,
        )
        return

    is_lease = isinstance(task_request.request, V13LeaseRequest)
    is_bundle = isinstance(task_request.request, BundleRequestEnvelope)
    if is_bundle and not allow_bundles:
        # Refused, not ignored: the runner must learn why nothing ran.
        # Nothing has touched the filesystem, and none of the bundle's code
        # has been looked at.
        log.error(
            "request %s: refusing a bundle request -- this executor was not started "
            "with --allow-bundles",
            task_request.requestId,
        )
        _post_result(
            session,
            relay,
            task_request.requestId,
            _encode_result({"status": "failed", "error": BUNDLES_NOT_ALLOWED}, log),
            tokens,
            log,
        )
        return

    if is_lease and not allow_bundles:
        # Refused as an envelope, like every other lease that cannot run: the runner and
        # papi learn why nothing ran. Nothing has been written, and no code looked at.
        log.error(
            "request %s: refusing a V13 lease -- this executor was not started with "
            "--allow-bundles",
            task_request.requestId,
        )
        envelope = attach_provenance(
            failed_envelope("invalid_input", PLUGIN_CODE_NOT_ALLOWED), task_request.request.doc
        )
        body = _encode_result({"status": "ok", "result": envelope}, log)
        _post_result(session, relay, task_request.requestId, body, tokens, log)
        return

    scope_dir = workdir / task_request.scopeId
    scope_dir.mkdir(parents=True, exist_ok=True)
    # One client per request: it carries the request id the artifact routes
    # are scoped to, and the executor's token source.
    relay_client = RelayClient(relay, None, task_request.requestId, session, token_source=tokens)
    try:
        if is_lease:
            # A V13 lease: plugin code pinned by hash, the same hardened child runner
            # as a bundle, and the bare result envelope as the result.
            os.chmod(scope_dir, 0o700)
            envelope = run_lease(
                task_request.request.doc,
                scope_dir=scope_dir,
                credentials=task_request.credentials,
                materialized=task_request.materialized,
                relay=relay_client,
                output_decls=task_request.outputs,
                deadline_s=task_request.deadlineMs / 1000,
                log=log.getChild(task_request.requestId),
            )
            payload = {"status": "ok", "result": envelope}
        elif is_bundle:
            # A bundle's code travels with the request, not baked into this
            # pod's image -- see bundle.py's module docstring. Bundle mode
            # enforces deadlineMs as one budget shared by the request's setup
            # and all of its tasks, killing a child that runs past it.
            os.chmod(scope_dir, 0o700)
            result = run_bundle_request(
                task_request.request,
                task_request.credentials,
                scope_dir,
                log=log.getChild(task_request.requestId),
                deadline_seconds=task_request.deadlineMs / 1000,
                relay=relay_client,
                outputs=task_request.outputs,
                input_artifacts=task_request.inputArtifacts,
            )
        else:
            # A packaged task is an in-process call that cannot be killed, so
            # the deadline only bounds what it starts through ctx.artifacts.
            result = run_request(
                capability,
                task_request.request,
                task_request.credentials,
                scope_dir,
                log=log.getChild(task_request.requestId),
                relay=relay_client,
                outputs=task_request.outputs,
                input_artifacts=task_request.inputArtifacts,
                artifact_cache_dir=workdir / ARTIFACT_CACHE_DIR,
                deadline_seconds=task_request.deadlineMs / 1000,
            )
        if not is_lease:
            payload = {"status": "ok", "result": result.model_dump(mode="json")}
    except Exception as exc:  # noqa: BLE001 -- one request must never take down the loop
        log.exception("request %s failed", task_request.requestId)
        payload = {"status": "failed", "error": str(exc)}
    finally:
        if is_bundle or is_lease:
            # No execution.mode concept applies to a bundle or a lease -- it
            # never shares a scope with a later request, so it is always
            # wiped, exactly like a stateless packaged capability.
            shutil.rmtree(scope_dir, ignore_errors=True)
        else:
            _retain_or_wipe_scope(
                capability, scope_dir, workdir, stateful_scopes, max_stateful_scopes, log
            )

    body = _encode_result(payload, log, task_request.maxResultBytes)
    _post_result(session, relay, task_request.requestId, body, tokens, log)
