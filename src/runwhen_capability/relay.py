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

    GET {relay}/v1/tasks/{requestId}/code
        -> 200 the request's plugin code archive (a tar), with Content-Length and
           X-RW-Sha256: sha256:<hex>
        -> 404 when the request's lease carries no `code.archive`

The runner answers a PUT early on purpose: 413 after reading only
`maxBytes + 1` bytes, 503 before reading any. The client is usually still
sending when that happens; requests/urllib3 tolerate the broken pipe a close
can cause while a body is being sent and still read the response, so the
caller sees the mapped exception below rather than a ConnectionError.

Every failure is an ArtifactError. An answer with a code maps to its subclass
(`_raise_for`); a failure to talk to the relay at all -- a refused or broken
connection, a timeout, a body cut short, a 201
whose body is not JSON -- is the base ArtifactError with the code
E_ARTIFACT_TRANSPORT, raised from the original error. `_transport_errors` is
the one place that conversion happens, for the calls and for every read of an
input's body (`InputStream`), so a task that catches ArtifactError never
sees a requests or urllib3 exception.

The bearer token comes from a `TokenSource`. `rwtask serve` reads the executor token
file (when it is there: a pod may start before its token does) and unlinks it; the source
then holds the string in memory only, with no file for a task in the same pod to copy. If
the unlink is refused (a read-only Secret volume, today's runner), the source re-reads the
file on every call instead, so a Secret the kubelet refreshes in place on rotation is
picked up (see serve.py). A client built from a plain `token` string or a `token_file`
(tests, `rwtask run`) holds a fixed token: the file is read once, at construction.
"""

from __future__ import annotations

import io
import logging
import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote

import requests
import urllib3

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

# requests/urllib3 timeout tuples are (connect, read), and an upload uses two
# different limits for them:
#
# - The connect slot, _SEND_TIMEOUT, bounds each send of the body. It is not only
#   about connecting: urllib3 leaves the connect timeout on the socket for the
#   whole time it SENDS the request body (the read timeout applies only once it
#   starts reading the response), and Python's sendall treats that timeout as a
#   limit on the whole call. So it is the longest the runner may stop reading
#   mid-body before the chunk in flight fails with a raw requests.ConnectionError.
#   The runner legitimately stops reading for up to its stall timeout (2 minutes)
#   while it waits for an upload slot or flushes a part to object storage, so
#   the upload gets 150 s, more than that. A refused or reset connection still
#   fails at once; only a blackholed one waits this long. A download sends no
#   body, so its connect slot stays short.
#
# - The read slot, _RESPONSE_TIMEOUT, only starts after the last body byte, so a
#   slow producer never eats into it. By then the runner still has to finish its
#   final parts and finalise the upload: up to three attempts of two minutes each
#   with a second or two of backoff between them, and its stall watchdog (which
#   counts progress while the body flows, not elapsed time) is already stopped.
#   The 150 s above does not cover that, so the slot is ten minutes: the whole
#   finalisation, with margin. It is finite all the same: a runner that takes the
#   body and then never answers must not hang the caller -- and `rwtask serve`,
#   which runs one request at a time, with it.
_SEND_TIMEOUT = 150
_RESPONSE_TIMEOUT = 600
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

# The code of every failure to talk to the relay (see the module docstring).
TRANSPORT_ERROR = "E_ARTIFACT_TRANSPORT"
_TRANSPORT_EXCEPTIONS = (requests.RequestException, urllib3.exceptions.HTTPError, OSError)


class TokenNotPresent(OSError):
    """`TokenSource.get()` on a source still waiting for its token file."""


class TokenSource:
    """The executor's bearer token: `get()` returns the one to send. Three states:

    Pending (`from_file` found no usable file): the pod started before its token did (an
    image smoke test, a late mount). Each `get()` tries the file again and raises
    `TokenNotPresent` while it is missing, unreadable or empty -- the serve loop skips the
    poll and sleeps. The first successful read applies the normal rule, once: try to unlink
    the file; if that works the source becomes fixed, if it is refused it becomes
    re-reading.

    Fixed (`path=None`): `token` for the life of the process -- the file it came from is
    gone. Re-reading: `token` is what the file held and `get()` re-reads `path` every time,
    so a rotation in place is picked up. A re-read that fails or finds the file empty (a
    refresh caught mid-way) logs a warning and returns the last token that was read, rather
    than sending nothing or dropping a result in hand."""

    def __init__(
        self, token: str | None, path: Path | None = None, log: logging.Logger | None = None
    ):
        self._token = token
        self._path = Path(path) if path is not None else None
        self._log = log or logging.getLogger("runwhen_capability.relay")

    @classmethod
    def from_file(cls, path: Path, log: logging.Logger | None = None) -> TokenSource:
        """The source for an executor token file, never raising: a file that is missing,
        unreadable or empty gives a pending source (warned about once). Otherwise the file
        is read and the unlink tried (see the class docstring), and the resulting mode is
        logged once."""
        source = cls(None, path, log)
        if not source._adopt():
            source._log.warning("executor token not present yet at %s; waiting for it", path)
        return source

    @property
    def pending(self) -> bool:
        return self._token is None

    @property
    def rereads_file(self) -> bool:
        return self._path is not None and self._token is not None

    def _adopt(self) -> bool:
        """Pending only: reads the file and, if it holds a token, takes it and applies the
        unlink rule. False when there is no token yet."""
        try:
            token = self._path.read_text().strip()
        except OSError:
            return False
        if not token:
            return False
        self._token = token
        try:
            os.unlink(self._path)
        except OSError:
            self._log.info("executor token file left in place (read-only mount): %s", self._path)
            self._log.info(
                "executor token mode: re-reading %s on every relay call (rotation in place)",
                self._path,
            )
        else:
            self._log.info("executor token mode: held in memory only; the token file was removed")
            self._path = None
        return True

    def get(self) -> str:
        if self._token is None:
            if not self._adopt():
                raise TokenNotPresent(f"executor token not present at {self._path}")
            return self._token
        if self._path is None:
            return self._token
        try:
            fresh = self._path.read_text().strip()
        except OSError as exc:
            self._log.warning(
                "could not re-read executor token from %s: %s; using the last token read",
                self._path,
                exc,
            )
            return self._token
        if not fresh:
            self._log.warning(
                "executor token file %s is empty; using the last token read", self._path
            )
            return self._token
        self._token = fresh
        return fresh


@contextmanager
def _transport_errors(what: str) -> Iterator[None]:
    """Re-raises a transport failure inside the block -- a requests or urllib3
    error, or an OSError (the socket) -- as
    ArtifactError(TRANSPORT_ERROR, "<what> failed: ..."), from the original.
    ArtifactErrors (the relay's coded answers) pass through untouched."""
    try:
        yield
    except _TRANSPORT_EXCEPTIONS as exc:
        if isinstance(exc, requests.exceptions.InvalidJSONError):
            detail = f"the relay's answer is not JSON ({exc})"
        else:
            detail = f"{type(exc).__name__}: {exc}"
        raise ArtifactError(TRANSPORT_ERROR, f"{what} failed: {detail}") from exc


class InputStream(io.RawIOBase):
    """An input artifact as the relay is sending it, from `open_input`: a
    binary stream of the stored bytes, never decoded (any Content-Encoding
    is left alone) and never held whole -- each read takes at most what it
    asks for off the response. `headers` are those of the relay's answer.
    A read that fails -- the relay silent past the read timeout, a body that
    ends short of its Content-Length -- raises ArtifactError(TRANSPORT_ERROR).
    Closing it closes the response; it is a context manager."""

    def __init__(self, resp: requests.Response, name: str) -> None:
        super().__init__()
        self.headers = resp.headers
        self._resp = resp
        self._raw = resp.raw
        self._raw.decode_content = False
        self._name = name

    def readable(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> bytes:
        if self.closed:
            raise ValueError("read from a closed input artifact stream")
        amount = None if size is None or size < 0 else size
        with _transport_errors(f"reading input artifact {self._name!r}"):
            return self._raw.read(amount, decode_content=False)

    def readall(self) -> bytes:
        return self.read()

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        memoryview(buffer).cast("B")[: len(data)] = data
        return len(data)

    def close(self) -> None:
        if not self.closed:
            try:
                self._resp.close()
            finally:
                super().close()


class RelayClient:
    """One request's view of the relay: `base_url` is the relay root,
    `request_id` the request being served, `session` the HTTP session the serve
    loop already uses. The bearer token is given as a `token_source` (what the
    serve loop does), a `token` string, or a `token_file` (read once, here; an
    unreadable file raises OSError) -- exactly one of them; pass `None` for a
    `token_file` that is not used."""

    def __init__(
        self,
        base_url: str,
        token_file: Path | None,
        request_id: str,
        session: requests.Session,
        *,
        token: str | None = None,
        token_source: TokenSource | None = None,
    ) -> None:
        given = [x for x in (token_file, token, token_source) if x is not None]
        if not given:
            raise ValueError("a RelayClient needs a token, a token_file or a token_source")
        if len(given) > 1:
            raise ValueError("a RelayClient takes one of token, token_file, token_source")
        self.base_url = base_url.rstrip("/")
        if token_source is not None:
            self._tokens = token_source
        elif token is not None:
            self._tokens = TokenSource(token.strip())
        else:
            self._tokens = TokenSource(Path(token_file).read_text().strip())
        self.request_id = request_id
        self.session = session

    def put_artifact(
        self, *, task: str, name: str, content_type: str, body: Iterable[bytes]
    ) -> dict:
        """Uploads `body` as artifact `name` for `task`, as a chunked PUT, and
        returns the relay's 201 JSON (`artifactRef`, `sizeBytes`, `sha256`,
        `lineCount`, `isText`). `body` is consumed lazily, once; it is
        streamed, never buffered. Any other answer raises the matching
        ArtifactError subclass -- see `_raise_for` -- and a transport failure
        is ArtifactError(TRANSPORT_ERROR)."""
        # An iterator (not the list the caller may have passed): requests
        # form-encodes a bare list, and only an iterator of unknown length
        # goes out chunked. Empty pieces are dropped -- a zero-length chunk is
        # the body's terminator on the wire.
        chunks = (piece for piece in body if piece)
        with _transport_errors(f"uploading artifact {name!r}"):
            resp = self.session.put(
                self._url(f"artifacts/{quote(name, safe='/')}"),
                data=chunks,
                headers={
                    **self._auth(),
                    "Content-Type": content_type,
                    "X-RW-Task": task,
                },
                timeout=(_SEND_TIMEOUT, _RESPONSE_TIMEOUT),
            )
            with resp:
                if resp.status_code != 201:
                    self._raise_for(resp, upload=True)
                return resp.json()

    def open_input(self, input: str) -> InputStream:  # noqa: A002 -- the relay's name
        """Opens input artifact `input` for streaming, as an InputStream the
        caller reads and must close. A non-200 raises the matching
        ArtifactError instead, a transport failure ArtifactError(
        TRANSPORT_ERROR). Each read of the body waits at most `_READ_TIMEOUT`
        seconds for data."""
        with _transport_errors(f"downloading input artifact {input!r}"):
            resp = self.session.get(
                self._url(f"artifacts/in/{quote(input, safe='')}"),
                headers=self._auth(),
                stream=True,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
            if resp.status_code != 200:
                with resp:
                    self._raise_for(resp)
        return InputStream(resp, input)

    def open_code(self) -> InputStream:
        """Opens the request's plugin code archive (`GET .../code`) for streaming,
        as an InputStream the caller reads and must close. Failures are as in
        `open_input`; a 404 (the lease has no `code.archive`) is
        ArtifactUndeclaredError."""
        with _transport_errors("downloading the code archive"):
            resp = self.session.get(
                self._url("code"),
                headers=self._auth(),
                stream=True,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
            if resp.status_code != 200:
                with resp:
                    self._raise_for(resp)
        return InputStream(resp, "code")

    @staticmethod
    def _raise_for(resp: requests.Response, *, upload: bool = False) -> NoReturn:
        """Maps a relay error response to the ArtifactError subclass for its
        `code` (see the module docstring). A 404 with no code -- the input
        GET's answer for an undeclared input -- is ArtifactUndeclaredError;
        on an `upload` it may also be a runner that predates artifact support
        (it has no such route), and the message says so. Anything else (a
        401, a non-JSON body, a code this SDK predates) is the base
        ArtifactError, carrying the body's code if there is one, else
        `HTTP_<status>`, and a message that names the status."""
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
            if upload and message is None:
                message = (
                    "the relay answered 404 with no code: the artifact output is not declared "
                    "for this request, or the runner predates artifact support"
                )
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
        return {"Authorization": f"Bearer {self._tokens.get()}"}
