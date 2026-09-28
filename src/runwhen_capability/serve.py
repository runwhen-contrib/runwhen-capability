"""`rwtask serve` -- long-polls the runner over plain HTTP/JSON (not Connect).

Relay HTTP contract (this is the wire the runner must implement to match):

    POST {relay}/v1/tasks/next
        body: {"poolId": "<poolId>"}
        -> 200 {"requestId", "request", "credentials": {name: value}, "scopeId", "deadlineMs"}
           (the "request" field is a RequestEnvelope; see models.py)
        -> 204 when idle (the server may hold the connection up to ~30s)

    POST {relay}/v1/tasks/{requestId}/result
        body: {"status": "ok"|"failed"|"timeout", "result": {...}, "error": "..."}
           ("result" is a ResultEnvelope, present only on status "ok")

Both relay calls carry `Authorization: Bearer <token>`, where <token> is read
fresh from the executor token file rather than cached at startup -- reading
is cheap and it lets a rotated or late-mounted token recover on its own
instead of wedging the pod. The two calls read it at different points,
though, not once per poll: GetTask reads it right before the poll, but
PutResult reads it AGAIN right before posting the result, because a
request can run for up to the manifest's requestTimeoutSeconds (often
minutes) between those two points -- long enough for a token to rotate mid-request,
which would otherwise mean the POST silently used the stale one and lost
an already-computed result to a 401. If that second read is briefly
unreadable, PutResult falls back to the token GetTask used rather than
dropping the result (see _post_result). The token file path comes from
--token-file, else the EXECUTOR_TOKEN_FILE env var, else DEFAULT_TOKEN_FILE.
A missing/unreadable token file at GetTask time is logged and the loop
keeps polling -- it never falls back to an unauthenticated request.

`scopeId` must be a single path segment (see _is_safe_scope_id): the loop
creates and later deletes <workdir>/<scopeId>/, so anything else -- empty,
absolute, containing "/" or ".." -- is refused as a failed request before
the filesystem is touched.

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

import logging
import os
import shutil
import time
from collections import OrderedDict
from pathlib import Path

import requests

from .host import run_request
from .loader import discover_capability_dir, load_capability
from .models import TaskHostRequest

POLL_TIMEOUT = 35  # seconds; a little over the relay's ~30s long-poll hold
RESULT_TIMEOUT = 30  # seconds
RETRY_DELAY = 5  # seconds, on a poll/post transport failure

DEFAULT_TOKEN_FILE = "/var/run/executor/token"

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
) -> None:
    """Runs the long-poll loop. `max_iterations` (None = forever) and
    `session` exist so tests can drive this deterministically without a real
    relay or an infinite loop. `max_stateful_scopes` bounds how many warm
    scopes a `stateful` capability keeps on disk at once (n/a for
    `stateless` capabilities, which never keep one); tests lower it to
    exercise eviction without dozens of iterations."""
    log = log or logging.getLogger("runwhen_capability.serve")
    session = session or requests.Session()
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    token_file = (
        Path(token_file)
        if token_file
        else Path(os.environ.get("EXECUTOR_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    )

    cap_dir = Path(capability_dir) if capability_dir else discover_capability_dir()
    capability = load_capability(cap_dir)
    log.info(
        "serving capability %r (execution.mode=%s) from %s",
        capability.capability_id,
        capability.execution_mode,
        cap_dir,
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
            token_file,
            log,
            stateful_scopes,
            max_stateful_scopes,
        )


def _is_safe_scope_id(scope_id: str) -> bool:
    """A scopeId is used as ONE directory name under workdir, and that
    directory is later rmtree'd (a stateless wipe, or an LRU eviction).
    Anything that is not a single ordinary path segment makes
    `workdir / scopeId` land somewhere else entirely -- `Path("/work") / "/x"`
    is `/x`, `Path("/work") / ""` is `/work` itself, and `".."` walks up --
    so a malformed scopeId would delete something that is not a scope. The
    relay is authenticated, so this is not the security boundary; it is the
    guard that keeps a bug on the other side of the wire from costing this
    pod its whole workdir."""
    return scope_id not in ("", ".", "..") and scope_id == Path(scope_id).name


def _post_result(
    session,
    relay: str,
    request_id: str,
    payload: dict,
    token_file: Path,
    fallback_token: str,
    log,
) -> None:
    """POSTs one PutResult. Re-reads the bearer token from `token_file`
    immediately before posting, rather than reusing the one the poll
    started with: a request can run up to the manifest's
    requestTimeoutSeconds before this runs, so a mid-request rotation would otherwise
    mean the POST carries a now-stale token and gets a 401 -- silently
    losing a result that was already computed, which defeats the whole
    reason the token is re-read per poll rather than cached at startup (see
    the module docstring). If the token file is briefly unreadable right at
    this moment, falls back to `fallback_token` -- the token the poll
    already had -- and logs the fallback, rather than dropping a result in
    hand over a transient read.

    A transport failure or a non-2xx is logged and dropped -- the runner's
    lease expiry is what recovers the request -- but it is never treated as
    a delivered result: an unlogged 401/500 here is a result that vanished
    with nothing to explain it."""
    token = _read_token(token_file, log)
    if token is None:
        log.warning(
            "could not re-read executor token before posting result for %s; "
            "falling back to the token this poll started with",
            request_id,
        )
        token = fallback_token
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = session.post(
            f"{relay}/v1/tasks/{request_id}/result",
            json=payload,
            timeout=RESULT_TIMEOUT,
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


def _read_token(token_file: Path, log) -> str | None:
    try:
        return token_file.read_text().strip()
    except OSError as exc:
        log.error("could not read executor token from %s: %s", token_file, exc)
        return None


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
    token_file: Path,
    log,
    stateful_scopes: OrderedDict[str, None],
    max_stateful_scopes: int,
) -> None:
    token = _read_token(token_file, log)
    if token is None:
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
            "request %s: refusing scopeId %r -- not a single path segment",
            task_request.requestId,
            task_request.scopeId,
        )
        _post_result(
            session,
            relay,
            task_request.requestId,
            {"status": "failed", "error": f"invalid scopeId {task_request.scopeId!r}"},
            token_file,
            token,
            log,
        )
        return

    scope_dir = workdir / task_request.scopeId
    scope_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = run_request(
            capability,
            task_request.request,
            task_request.credentials,
            scope_dir,
            log=log.getChild(task_request.requestId),
        )
        payload = {"status": "ok", "result": result.model_dump(mode="json")}
    except Exception as exc:  # noqa: BLE001 -- one request must never take down the loop
        log.exception("request %s failed", task_request.requestId)
        payload = {"status": "failed", "error": str(exc)}
    finally:
        _retain_or_wipe_scope(
            capability, scope_dir, workdir, stateful_scopes, max_stateful_scopes, log
        )

    _post_result(session, relay, task_request.requestId, payload, token_file, token, log)
