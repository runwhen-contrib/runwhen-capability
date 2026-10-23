"""The executor's artifact client for the runner's relay (in-cluster HTTP,
per-pod bearer token). Two routes, both scoped to one request:

    PUT {relay}/v1/tasks/{requestId}/artifacts/{name}
        headers: Authorization: Bearer <token>, Content-Type, X-RW-Task: <task>
        body: the artifact bytes, chunked
        -> 201 {"artifactRef", "sizeBytes", "sha256", "lineCount", "isText"}
        -> 404 {"code": "E_ARTIFACT_UNDECLARED"}
        -> 409 {"code": "E_ARTIFACT_EXISTS" | "E_ARTIFACT_LEASE_LOST"}
        -> 413 {"code": "E_ARTIFACT_TOO_LARGE", "maxBytes": N}
        -> 422 {"code": "E_ARTIFACT_QUOTA" | "E_ARTIFACTS_DISABLED" | "E_ARTIFACTS_UNSUPPORTED",
                "message": "..."}
        -> 503 {"code": "E_ARTIFACT_BUSY"}

    GET {relay}/v1/tasks/{requestId}/artifacts/in/{input}
        -> 200 the bytes, with Content-Length and X-RW-Sha256
        -> 404 when `input` is not one of the request's input artifacts

The runner answers a PUT early on purpose: 413 after reading only
`maxBytes + 1` bytes, 503 before reading any. The client is usually still
sending when that happens; requests/urllib3 tolerate the broken pipe a close
can cause while a body is being sent and still read the response, so the
caller sees the mapped exception below rather than a ConnectionError.

The bearer token is read from the token file on every call (the same file
`rwtask serve` polls with -- see serve.py), never cached: a request can run
for minutes, long enough for the token to rotate under it.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote

import requests

from .errors import (
    ArtifactBusyError,
    ArtifactError,
    ArtifactExistsError,
    ArtifactLeaseLostError,
    ArtifactQuotaError,
    ArtifactsUnavailableError,
    ArtifactTooLargeError,
    ArtifactUndeclaredError,
)

# requests/urllib3 timeout tuples are (connect, read). An upload's read timeout
# is None on purpose: its response only comes after the runner has taken the
# whole body, however slow the producer is. The task's own deadline is what
# ends a stalled upload.
#
# The connect slot is not only about connecting. urllib3 leaves the connect
# timeout on the socket for the whole time it SENDS the request body (the read
# timeout applies only once it starts reading the response), and Python's
# sendall treats that timeout as a limit on the whole call. So for an upload
# the first number is the longest the runner may stop reading mid-body before
# the chunk in flight fails with a raw requests.ConnectionError. The runner
# legitimately stops reading for up to its stall timeout (2 minutes) while it
# waits for an upload slot or flushes a part to object storage, so the upload
# gets 150 s, more than that. A refused or reset connection still fails at
# once; only a blackholed one waits this long. A download sends no body, so
# its connect slot stays short.
_SEND_TIMEOUT = 150
_CONNECT_TIMEOUT = 10
# A download does have a read timeout: a relay connection that goes half-open
# would otherwise hang the task for ever -- and `rwtask serve`, which runs one
# request at a time, with it. requests applies it to each socket read of a
# streamed body, not to the transfer as a whole, so a slow transfer that keeps
# moving is never cut; only a connection that stays silent this long is. More
# than the runner's 2-minute stall timeout, for the same reason as
# _SEND_TIMEOUT: a runner that merely pauses must not be cut off.
_READ_TIMEOUT = 150

_BY_CODE: dict[str, type[ArtifactError]] = {
    "E_ARTIFACT_TOO_LARGE": ArtifactTooLargeError,
    "E_ARTIFACT_UNDECLARED": ArtifactUndeclaredError,
    "E_ARTIFACT_EXISTS": ArtifactExistsError,
    "E_ARTIFACT_QUOTA": ArtifactQuotaError,
    "E_ARTIFACT_LEASE_LOST": ArtifactLeaseLostError,
    "E_ARTIFACT_BUSY": ArtifactBusyError,
    "E_ARTIFACTS_DISABLED": ArtifactsUnavailableError,
    "E_ARTIFACTS_UNSUPPORTED": ArtifactsUnavailableError,
}

# Text for a code the relay sent without a "message" (413 and 503 carry only
# a code). Keyed like _BY_CODE; anything missing falls back to the code itself.
_DEFAULT_MESSAGE = {
    "E_ARTIFACT_TOO_LARGE": "the artifact is larger than its declared maximum size",
    "E_ARTIFACT_UNDECLARED": "no such artifact output or input is declared for this request",
    "E_ARTIFACT_EXISTS": "an artifact with this name was already written",
    "E_ARTIFACT_QUOTA": "the artifact quota is exhausted",
    "E_ARTIFACT_LEASE_LOST": "this request's lease is no longer held",
    "E_ARTIFACT_BUSY": "the runner has no upload slot free; try again later",
    "E_ARTIFACTS_DISABLED": "artifacts are disabled",
    "E_ARTIFACTS_UNSUPPORTED": "artifacts are not supported by this runner",
}

# How much of a non-JSON error body is quoted in the exception message.
_BODY_SNIPPET = 200


class RelayClient:
    """One request's view of the relay: `base_url` is the relay root,
    `token_file` the executor token file, `request_id` the request being
    served, `session` the HTTP session the serve loop already uses."""

    def __init__(
        self,
        base_url: str,
        token_file: Path,
        request_id: str,
        session: requests.Session,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_file = Path(token_file)
        self.request_id = request_id
        self.session = session

    def put_artifact(
        self, *, task: str, name: str, content_type: str, body: Iterable[bytes]
    ) -> dict:
        """Uploads `body` as artifact `name` for `task`, as a chunked PUT, and
        returns the relay's 201 JSON (`artifactRef`, `sizeBytes`, `sha256`,
        `lineCount`, `isText`). `body` is consumed lazily, once; it is
        streamed, never buffered. Any other answer raises the matching
        ArtifactError subclass -- see `_raise_for`."""
        # An iterator (not the list the caller may have passed): requests
        # form-encodes a bare list, and only an iterator of unknown length
        # goes out chunked. Empty pieces are dropped -- a zero-length chunk is
        # the body's terminator on the wire.
        chunks = (piece for piece in body if piece)
        resp = self.session.put(
            self._url(f"artifacts/{quote(name, safe='/')}"),
            data=chunks,
            headers={
                **self._auth(),
                "Content-Type": content_type,
                "X-RW-Task": task,
            },
            timeout=(_SEND_TIMEOUT, None),
        )
        with resp:
            if resp.status_code != 201:
                self._raise_for(resp)
            return resp.json()

    def open_input(self, input: str) -> requests.Response:  # noqa: A002 -- the relay's name
        """Opens input artifact `input` for streaming: a `stream=True`
        response whose body the caller reads (`iter_content`, `raw`) and must
        close. A non-200 raises the matching ArtifactError instead. Each read
        of the body waits at most `_READ_TIMEOUT` seconds for data."""
        resp = self.session.get(
            self._url(f"artifacts/in/{quote(input, safe='')}"),
            headers=self._auth(),
            stream=True,
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        if resp.status_code != 200:
            with resp:
                self._raise_for(resp)
        return resp

    @staticmethod
    def _raise_for(resp: requests.Response) -> NoReturn:
        """Maps a relay error response to the ArtifactError subclass for its
        `code` (see the module docstring). A 404 with no code -- the input
        GET's answer for an undeclared input -- is ArtifactUndeclaredError.
        Anything else (a 401, a non-JSON body, a code this SDK predates)
        is the base ArtifactError, carrying the body's code if there is one,
        else `HTTP_<status>`, and a message that names the status."""
        status = resp.status_code
        try:
            parsed = resp.json()
        except ValueError:
            parsed = None
        body = parsed if isinstance(parsed, dict) else {}
        code = body.get("code") if isinstance(body.get("code"), str) else None
        message = body.get("message") if isinstance(body.get("message"), str) else None

        if code is None and status == 404:
            code = "E_ARTIFACT_UNDECLARED"
        exc_type = _BY_CODE.get(code) if code else None
        if exc_type is None:
            text = f"{message or 'unexpected relay response'} (HTTP {status})"
            if parsed is None and resp.text.strip():
                text += f": {resp.text.strip()[:_BODY_SNIPPET]}"
            raise ArtifactError(code or f"HTTP_{status}", text)

        if exc_type is ArtifactTooLargeError:
            max_bytes = body.get("maxBytes")
            if not isinstance(max_bytes, int) or isinstance(max_bytes, bool):
                max_bytes = None
            if message is None:
                message = _DEFAULT_MESSAGE[code]
                if max_bytes is not None:
                    message += f" ({max_bytes} bytes)"
            raise ArtifactTooLargeError(code, message, max_bytes=max_bytes)
        raise exc_type(code, message or _DEFAULT_MESSAGE[code])

    def _url(self, suffix: str) -> str:
        return f"{self.base_url}/v1/tasks/{self.request_id}/{suffix}"

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token_file.read_text().strip()}"}
