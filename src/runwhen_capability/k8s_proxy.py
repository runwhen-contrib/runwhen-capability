"""`proxy_get`: a GET through the Kubernetes API server's pod/service proxy that never
follows a redirect (security finding S17).

`kubectl get --raw /api/v1/namespaces/<ns>/services/<name>:<port>/proxy/<path>` follows a
backend's 301/302/307. When the `Location` is absolute and names another host, client-go
re-sends `Authorization: Bearer <the runner's token>` there, so anyone who controls a pod a
skill probes (or an SSO realm that redirects an anonymous request to an external IdP) can
capture the runner's cluster credential. kubectl has no flag that turns the following off.

    reply = proxy_get("vault", "services", "vault", 8200, "/v1/sys/health")
    reply.status, reply.body, reply.location

How: `kubectl proxy --unix-socket=<private dir>/p.sock` runs for the duration of the call, in
its own process group, and the request goes to it over that socket (a directory of mode 0700 under
the temporary directory, or /tmp when that path is too long for a socket) with a client that sends
no credential of its own (kubectl's proxy adds the cluster credential on its way to the API
server, and the API server's proxy never hands it to the backend). No TCP port is opened, so
nothing else on the machine can use the proxy; its `--accept-hosts` accepts only the Host
`127.0.0.1`. kubectl 1.3x prints "Starting to serve on <socket>" once it listens, which is the
readiness signal (a proxy that stays silent is used once its socket file exists). The client
treats a 3xx as an answer, never a thing to follow. The proxy is terminated (SIGTERM, then SIGKILL
after 2 s) and its directory removed when the call ends, whatever happened.

`location` of a redirect is reported only when it stays on the same pod/service/port: the API
server rewrites a backend's path-absolute Location into its own path space
(`/api/v1/namespaces/<ns>/<kind>/<[https:]name:port>/proxy/<path>`), and that prefix is stripped,
so `location` is the backend-relative path plus query (`/somewhere/else?x=1`), which can be passed
straight back as `path`. For any other target (an absolute URL for another host, or a path into
another pod, service or port) `location` is None, the `location` header is removed from `headers`
(it is kept, raw, only for a kept redirect) and `x-rw-redirect-dropped: <host or
ns/kind/target>` says where it pointed. The answer is the backend's own, status and body, also
for a 4xx or 5xx, which is data, not an error.

Errors (D64 codes): kubectl missing (`dependency`), a proxy that does not start (the code of its
stderr, else `internal`) or a connection that fails (`unreachable`), a deadline (`timeout`), bad
input (`invalid_input`), and an answer that is the API server's own, told from the backend's by
being a `Status` object (`"kind": "Status"`, `"apiVersion": "v1"`) with an error status code:
401/403 `permission_denied`; 404 with `reason: NotFound` (no such pod or service) `not_found`;
503, `reason: ServiceUnavailable`, or a message with "no endpoints available" or "error trying
to reach service" (the proxy cannot reach the backend) `unreachable`. Any other Status, and every
body that is not a v1 Status (a backend's plain-JSON 503 such as Vault sealed's `{"errors": []}`),
is returned as data. The heuristic is narrow on purpose: a backend that itself speaks Kubernetes
Status objects with these shapes would be mistaken for the API server.

The API for tasks is `ctx.k8s_proxy_get(...)` and bash's `rw_k8s_proxy_get`, both through
`task_get`, which returns a JSON-able dict (the body as text) and records and replays the call
for `rwtask test` (`type: k8s-proxy-get` in a scenario's `calls`; plugin/shims.py describes the
shared state files): with `RW_SHIM_MODE=record` the real call is made and appended to
`$RW_SHIM_LOG`; with `RW_SHIM_MODE=replay` no proxy is started and the next recorded call, which
must be this request, answers.

    python3 -m runwhen_capability.k8s_proxy get <namespace> <pods|services> <name> <port> <path>
        [--scheme https] [--timeout N]

prints that dict as one JSON line; on an error it exits 1 after writing the error to
`$RW_ERRORS_FILE` (the shape `rw_error` writes) and a line to stderr.

A script's unit test declares the call with `testing.stub_commands(tc, k8s_proxy_get=[...])`.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import http.client
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Literal

from .rwerrors import RwCommandError, RwError, classify_command_error

#: How much of a response body is kept (4 MiB).
DEFAULT_MAX_BODY = 4 * 1024 * 1024

#: `type` of a scenario's call item (plugin/fixtures.py).
CALL_TYPE = "k8s-proxy-get"

#: The header that says a redirect's target was dropped, with that target's host.
DROPPED_HEADER = "x-rw-redirect-dropped"

#: How long a terminated proxy gets before it is killed.
_TERM_GRACE = 2.0

_READ_CHUNK = 65536
_STARTUP_LINE = b"Starting to serve on"
#: The proxy's unix socket: `<private dir>/p.sock`, at most this long (sun_path is 104 bytes on
#: macOS, 108 on Linux).
_SOCKET_NAME = "p.sock"
_SOCKET_PATH_MAX = 100
#: How long a proxy that prints no "Starting to serve" line gets before its socket file is
#: taken as readiness.
_SILENT_GRACE = 0.5
#: The Host the client sends; the proxy's --accept-hosts matches only this.
_HOST = "127.0.0.1"
_PROXY_PATH = re.compile(r"^/api/v1/namespaces/([^/]+)/(pods|services)/([^/]+)/proxy(?=/|$)")
_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_PORT_NAME = re.compile(r"^[a-z0-9-]{1,15}$")
_PATH_SAFE = "/:@!$&'()*+,;=-._~"
_QUERY_SAFE = _PATH_SAFE + "?"
_BARE_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_MISMATCH_EXIT = 97

Kind = Literal["pods", "services"]
Scheme = Literal["http", "https"]


@dataclass
class ProxyResponse:
    """What the backend answered. `headers` are lowercased; `body` holds at most `max_body`
    bytes and `truncated` says more followed; `location` is the redirect target when it is
    safe to report (see the module docstring)."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    truncated: bool = False
    location: str | None = None

    def to_task_dict(self) -> dict[str, Any]:
        """The dict a task sees: the body as text (UTF-8, undecodable bytes replaced)."""
        return {
            "status": self.status,
            "headers": dict(self.headers),
            "body": self.body.decode("utf-8", errors="replace"),
            "truncated": self.truncated,
            "location": self.location,
        }


class FixtureMismatch(RwError):
    """A replayed call that is not the next recorded one (`E_FIXTURE_MISMATCH`)."""

    def __init__(self, message: str) -> None:
        super().__init__("internal", f"E_FIXTURE_MISMATCH: {message}")


def _invalid(message: str, scope: str | None = None) -> RwError:
    return RwError("invalid_input", message, scope=scope)


# -- input ----------------------------------------------------------------------------------


def _check_dns_name(value: Any, what: str, *, subdomain: bool) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"{what} must be a non-empty string")
    if subdomain:
        ok = len(value) <= 253 and all(_LABEL.match(label) for label in value.split("."))
    else:
        ok = bool(_LABEL.match(value))
    if not ok:
        raise _invalid(f"{what} {value!r} is not a valid Kubernetes name")
    return value


def _check_port(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise _invalid(f"port must be 1-65535 or a port name, not {value!r}")
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    if isinstance(value, int):
        if not 1 <= value <= 65535:
            raise _invalid(f"port {value} is outside 1-65535")
        return str(value)
    if not _PORT_NAME.match(value):
        raise _invalid(f"port {value!r} is neither a number nor a port name ([a-z0-9-]{{1,15}})")
    return value


def _has_control(text: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in text)


def _check_path(path: Any) -> str:
    """The path (and query) as it goes into the URL: relative to the backend's root, percent
    encoded, without a `..` segment however it is spelled."""
    if not isinstance(path, str):
        raise _invalid("path must be a string")
    if _has_control(path) or "\\" in path:
        raise _invalid("path may not contain control characters or backslashes")
    route, question, query = path.partition("?")
    if route.startswith("/"):
        route = route[1:]
    decoded = route
    for _ in range(3):  # %2e%2e, %252e%252e: a backend may decode more than once
        decoded = urllib.parse.unquote(decoded)
        if _has_control(decoded) or "\\" in decoded:
            raise _invalid("path may not contain encoded control characters or backslashes")
        if ".." in decoded.split("/"):
            raise _invalid("path may not contain '..' segments")
    route = urllib.parse.quote(_BARE_PERCENT.sub("%25", route), safe=_PATH_SAFE + "%")
    if not question:
        return route
    query = urllib.parse.quote(_BARE_PERCENT.sub("%25", query), safe=_QUERY_SAFE + "%")
    return f"{route}?{query}"


# -- kubectl proxy ----------------------------------------------------------------------------


def _resolve_kubectl(kubectl: str) -> str:
    """The kubectl to run. Directories that hold `rwtask test`'s shims (they carry
    `.rw-shim.py`) are skipped: the proxy needs the real tool, and it is not a recorded call."""
    missing = RwCommandError(
        "dependency",
        f"{kubectl} is not installed (not found on PATH)",
        argv=[kubectl, "proxy"],
        returncode=127,
        stderr="",
    )
    if os.sep in kubectl:
        if os.path.isfile(kubectl) and os.access(kubectl, os.X_OK):
            return kubectl
        raise missing
    for directory in os.environ.get("PATH", os.defpath).split(os.pathsep):
        if not directory or os.path.exists(os.path.join(directory, ".rw-shim.py")):
            continue
        candidate = os.path.join(directory, kubectl)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise missing


def _socket_dir() -> str:
    """A fresh 0700 directory whose `p.sock` fits a unix socket path: under the temporary
    directory, else under /tmp."""
    for base in dict.fromkeys([tempfile.gettempdir(), "/tmp"]):
        try:
            directory = tempfile.mkdtemp(prefix="rwk-", dir=base)  # mode 0700
        except OSError:
            continue
        if len(os.path.join(directory, _SOCKET_NAME)) <= _SOCKET_PATH_MAX:
            return directory
        os.rmdir(directory)
    raise RwError(
        "internal",
        f"no writable directory with a short enough path for the proxy's unix socket "
        f"(limit {_SOCKET_PATH_MAX} characters)",
    )


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _wait_ready(proc: subprocess.Popen, sock: str, deadline: float) -> tuple[bool, bytes]:
    """`(ready, what kubectl printed)`: ready once kubectl says "Starting to serve on <socket>"
    (1.3x prints that line with --unix-socket too), or, if it stays silent, once the socket
    exists after `_SILENT_GRACE`. Not ready when the process ended first. RwError(timeout) when
    neither happens by `deadline`."""
    fd = proc.stdout.fileno()
    started = time.monotonic()
    printed = b""
    while True:
        now = time.monotonic()
        if now >= deadline:
            raise RwError("timeout", "kubectl proxy did not start serving in time")
        ready, _, _ = select.select([fd], [], [], min(deadline - now, 0.1))
        if ready:
            chunk = os.read(fd, 4096)
            if not chunk:
                return False, printed
            printed += chunk
            if _STARTUP_LINE in printed and b"\n" in printed:
                return True, printed
        elif proc.poll() is not None:
            return False, printed
        elif now - started >= _SILENT_GRACE and _is_socket(sock):
            return True, printed


def _start_proxy(kubectl: str, deadline: float):
    """Starts `kubectl proxy` on a unix socket in a private directory. Returns `(process,
    socket path, directory, stderr file)`; the caller stops the process and removes the
    directory."""
    binary = _resolve_kubectl(kubectl)
    directory = _socket_dir()
    sock = os.path.join(directory, _SOCKET_NAME)
    # No --port/--address: kubectl refuses --unix-socket together with a port.
    argv = [binary, "proxy", f"--unix-socket={sock}", r"--accept-hosts=^127\.0\.0\.1$"]
    errors = tempfile.TemporaryFile()  # noqa: SIM115 -- closed by the caller's finally
    try:
        try:
            proc = subprocess.Popen(  # noqa: S603 -- argv is built here
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=errors,
                start_new_session=True,  # its own process group: the whole group is stopped
            )
        except OSError as exc:
            raise RwCommandError(
                "dependency"
                if isinstance(exc, FileNotFoundError | PermissionError)
                else "internal",
                f"cannot start kubectl proxy: {exc}",
                argv=argv,
                returncode=127,
                stderr="",
            ) from None
        try:
            ready, printed = _wait_ready(proc, sock, deadline)
            if not ready:
                try:
                    returncode = proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    returncode = -1
                errors.seek(0)
                stderr = errors.read(65536).decode("utf-8", errors="replace")
                last = next((ln.strip() for ln in reversed(stderr.splitlines()) if ln.strip()), "")
                detail = last or printed.decode("utf-8", errors="replace").strip()
                raise RwCommandError(
                    classify_command_error(argv, returncode, stderr),
                    "kubectl proxy did not start" + (f": {detail[:500]}" if detail else ""),
                    argv=argv,
                    returncode=returncode,
                    stderr=stderr,
                )
        except BaseException:
            _stop_proxy(proc)
            raise
        return proc, sock, directory, errors
    except BaseException:
        errors.close()
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _stop_proxy(proc: subprocess.Popen) -> None:
    """SIGTERM to the proxy's process group, SIGKILL after `_TERM_GRACE`; reaps it."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=_TERM_GRACE)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    finally:
        if proc.stdout is not None:
            proc.stdout.close()


# -- the request ---------------------------------------------------------------------------


class _NeverFollow(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer: no new request is built, so the opener raises the 3xx as an
    HTTPError (a readable response) instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _UnixConnection(http.client.HTTPConnection):
    """An HTTP connection over an AF_UNIX socket (the URL's host is only the Host header)."""

    def __init__(self, socket_path: str, host: str, **kwargs) -> None:
        super().__init__(host, **kwargs)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self._socket_path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


class _UnixHandler(urllib.request.HTTPHandler):
    def __init__(self, socket_path: str) -> None:
        super().__init__()
        self._socket_path = socket_path

    def http_open(self, req):
        return self.do_open(lambda host, **kw: _UnixConnection(self._socket_path, host, **kw), req)


def _opener(socket_path: str) -> urllib.request.OpenerDirector:
    # ProxyHandler({}): never an http_proxy from the environment.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _UnixHandler(socket_path), _NeverFollow
    )
    opener.addheaders = [("User-Agent", "runwhen-capability-k8s-proxy-get"), ("Accept", "*/*")]
    return opener


def _read_body(response, max_body: int, deadline: float) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    while size <= max_body:
        if time.monotonic() >= deadline:
            raise RwError("timeout", "the backend did not finish answering in time")
        chunk = response.read(min(_READ_CHUNK, max_body + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    data = b"".join(chunks)
    return data[:max_body], len(data) > max_body


def _headers(raw) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in raw.items():
        key = key.lower()
        out[key] = f"{out[key]}, {value}" if key in out else value
    return out


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.URLError):
        exc = exc.reason  # a TimeoutError while connecting, or a text
    return isinstance(exc, TimeoutError)


def _api_status(body: bytes) -> dict | None:
    """The Kubernetes `Status` object (`kind: Status`, `apiVersion: v1`) in an error body, None
    when the body is anything else."""
    try:
        doc = json.loads(body)
    except ValueError:
        return None
    if isinstance(doc, dict) and doc.get("kind") == "Status" and doc.get("apiVersion") == "v1":
        return doc
    return None


def _api_status_error(
    status: int, doc: dict, scope: str, kind: str, namespace: str
) -> RwError | None:
    """The RwError for an API-server `Status` body, None for a Status that is not one of the
    narrow cases below (it comes back as data). A body is taken for the API server's own only
    when it is a v1 `Status` with an error status code; the cases:

    - 401/403: `permission_denied`;
    - 404 with `reason: NotFound` (the pod or service does not exist): `not_found`;
    - 503, `reason: ServiceUnavailable`, or a message with "no endpoints available" or
      "error trying to reach service" (the proxy could not reach the backend): `unreachable`.

    A backend that answers with its own plain 503 or 404 is not a Status and is data."""
    message = str(doc.get("message") or doc.get("reason") or "refused")[:400]
    reason = doc.get("reason")
    lowered = message.lower()
    if status in (401, 403):
        return RwError(
            "permission_denied",
            f"the API server refused the proxied request ({status}): {message}",
            scope=scope,
            hint=f"the identity needs 'get' on {kind}/proxy in {namespace}",
        )
    if status == 404 and reason == "NotFound":
        return RwError("not_found", f"the API server: {message}", scope=scope)
    if (
        status == 503
        or reason == "ServiceUnavailable"
        or "no endpoints available" in lowered
        or "error trying to reach service" in lowered
    ):
        return RwError(
            "unreachable", f"the API server could not reach the backend: {message}", scope=scope
        )
    return None


def _printable(text: str) -> str:
    return "".join(c for c in text if 0x20 <= ord(c) < 0x7F)[:253] or "?"


def _check_location(value: str | None, prefix: str) -> tuple[str | None, str | None]:
    """`(location to report, dropped target)` for a Location header, given the proxy path of
    this request (`prefix`). The API server rewrites a backend's path-absolute Location into
    its own path space, so a kept redirect has the prefix stripped: the result is the
    backend-relative path plus query, usable as the `path` of another call. Dropped (never
    reported, never followed): an absolute URL for any host but the proxy itself, and a path
    into another pod, service or port."""
    if value is None:
        return None, None
    value = value.strip()
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return None, "?"
    absolute = bool(parts.scheme or parts.netloc)
    if absolute and not (parts.scheme == "http" and parts.netloc.lower() == _HOST):
        return None, _printable(parts.netloc.rpartition("@")[2] or f"{parts.scheme}:")
    path = parts.path
    if path == prefix or path.startswith(prefix + "/"):
        return (path[len(prefix) :] or "/") + (f"?{parts.query}" if parts.query else ""), None
    other = _PROXY_PATH.match(path)
    if other:
        return None, _printable("/".join(other.groups()))
    if absolute:
        return None, _HOST
    return value, None


def proxy_get(
    namespace: str,
    kind: Kind,
    name: str,
    port: int | str,
    path: str,
    *,
    scheme: Scheme = "http",
    timeout: float = 30,
    max_body: int = DEFAULT_MAX_BODY,
    kubectl: str = "kubectl",
) -> ProxyResponse:
    """GET `path` from `<name>:<port>` of the pod or service `namespace/name` through the API
    server's proxy, without ever following a redirect. See the module docstring.

    `scheme` is the protocol the API server speaks to the backend; `timeout` bounds the whole
    call (proxy start included); a body is cut at `max_body` bytes. KUBECONFIG is the
    environment's. Raises RwError (RwCommandError when kubectl failed) with a D64 code."""
    _check_dns_name(namespace, "namespace", subdomain=False)
    if kind not in ("pods", "services"):
        raise _invalid(f"kind must be 'pods' or 'services', not {kind!r}")
    _check_dns_name(name, "name", subdomain=True)
    port_text = _check_port(port)
    route = _check_path(path)
    if scheme not in ("http", "https"):
        raise _invalid(f"scheme must be 'http' or 'https', not {scheme!r}")
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise _invalid(f"timeout must be a positive number of seconds, not {timeout!r}")
    if isinstance(max_body, bool) or not isinstance(max_body, int) or max_body <= 0:
        raise _invalid(f"max_body must be a positive number of bytes, not {max_body!r}")

    target = f"{'https:' if scheme == 'https' else ''}{name}:{port_text}"
    prefix = f"/api/v1/namespaces/{namespace}/{kind}/{target}/proxy"
    scope = f"{namespace}/{kind}/{name}:{port_text}"
    deadline = time.monotonic() + timeout
    proc, sock, directory, errors = _start_proxy(kubectl, deadline)
    try:
        url = f"http://{_HOST}{prefix}/{route}"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RwError("timeout", "no time left to ask the backend", scope=scope)
        response = None
        try:
            try:
                response = _opener(sock).open(url, timeout=remaining)
            except urllib.error.HTTPError as exc:  # any non-2xx, redirects included
                response = exc
            status = response.status if hasattr(response, "status") else response.code
            headers = _headers(response.headers)
            body, truncated = _read_body(response, max_body, deadline)
        except RwError:
            raise
        except urllib.error.URLError as exc:
            if _is_timeout(exc):
                raise RwError("timeout", f"timed out asking {scope}", scope=scope) from None
            raise RwError(
                "unreachable", f"cannot reach the kubectl proxy: {exc.reason}", scope=scope
            ) from None
        except TimeoutError:
            raise RwError("timeout", f"timed out asking {scope}", scope=scope) from None
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise RwError(
                "unreachable", f"the proxied request failed: {exc}", scope=scope
            ) from None
        finally:
            if response is not None:
                with contextlib.suppress(Exception):
                    response.close()
        doc = _api_status(body) if status >= 400 and not truncated else None
        if doc is not None:
            error = _api_status_error(status, doc, scope, kind, namespace)
            if error is not None:
                raise error
        location, dropped = _check_location(headers.get("location"), prefix)
        if "location" in headers and location is None:
            del headers["location"]  # a dropped redirect leaves nothing to follow by accident
        if dropped is not None:
            headers[DROPPED_HEADER] = dropped
        return ProxyResponse(status, headers, body, truncated, location)
    finally:
        _stop_proxy(proc)
        errors.close()
        shutil.rmtree(directory, ignore_errors=True)


# -- the task API, with fixtures --------------------------------------------------------------


def _append_line(variable: str, entry: dict) -> None:
    path = os.environ.get(variable)
    if not path:
        return
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(entry) + "\n").encode())
    finally:
        os.close(fd)


def same_request(a: dict, b: dict) -> bool:
    """Two requests are the same call: equal namespace, kind, name, path and scheme (default
    http); the port as text, so 8200 equals "8200"."""

    def norm(r: dict) -> dict:
        return {**r, "port": str(r.get("port")), "scheme": r.get("scheme") or "http"}

    return norm(a) == norm(b)


def describe_request(request: dict) -> str:
    """The call as the mismatch messages (and plugin/shims.py's `describe`) spell it."""
    return "{} {} {}/{}/{}:{} {}".format(
        CALL_TYPE,
        request.get("scheme") or "http",
        request.get("namespace"),
        request.get("kind"),
        request.get("name"),
        request.get("port"),
        request.get("path"),
    )


def normalize_call(call: dict) -> dict:
    """A recorded `k8s-proxy-get` call with its defaults filled: the request's scheme and the
    response's headers, body, truncated and location."""
    out: dict[str, Any] = {
        "type": CALL_TYPE,
        "request": {"scheme": "http", **call["request"]},
    }
    if "error" in call:
        out["error"] = dict(call["error"])
    else:
        out["response"] = {
            "headers": {},
            "body": "",
            "truncated": False,
            "location": None,
            **call["response"],
        }
    return out


def answer_from_call(call: dict) -> dict[str, Any]:
    """What a recorded call answers: the task dict of its response, or its error raised."""
    if "error" in call:
        error = call["error"]
        raise RwError(
            error["code"],
            error.get("message", ""),
            scope=error.get("scope"),
            hint=error.get("hint"),
        )
    return normalize_call(call)["response"]


def _replay(request: dict) -> dict:
    """The next recorded call, when it is this request. Mirrors the shims' bookkeeping:
    the index is taken under a lock from `$RW_SHIM_STATE`, advanced only on a match, and a
    mismatch is appended to `$RW_SHIM_MISMATCH` for the host."""
    with open(os.environ["RW_SHIM_CALLS"], encoding="utf-8") as handle:
        calls = json.load(handle)
    with open(os.environ["RW_SHIM_STATE"], "a+", encoding="utf-8") as state:
        fcntl.flock(state, fcntl.LOCK_EX)
        state.seek(0)
        index = int(state.read() or 0)
        expected = calls[index] if index < len(calls) else None
        problem = None
        if expected is None:
            problem = f"expected no further call got {describe_request(request)}"
            expected_text = None
        elif expected.get("type") != CALL_TYPE:
            expected_text = shlex.join(expected["argv"])
            problem = f"expected {expected_text} got {describe_request(request)}"
        elif not same_request(expected.get("request") or {}, request):
            expected_text = describe_request(expected.get("request") or {})
            problem = f"expected {expected_text} got {describe_request(request)}"
        else:
            expected_text = describe_request(expected["request"])
            state.seek(0)
            state.truncate()
            state.write(str(index + 1))
    if problem is not None:
        _append_line(
            "RW_SHIM_MISMATCH",
            {
                "index": index,
                "expected": expected_text,
                "got": describe_request(request),
                "message": problem,
            },
        )
        raise FixtureMismatch(problem)
    return answer_from_call(expected)


def task_get(
    namespace: str,
    kind: Kind,
    name: str,
    port: int | str,
    path: str,
    *,
    scheme: Scheme = "http",
    timeout: float = 30,
    max_body: int = DEFAULT_MAX_BODY,
    kubectl: str = "kubectl",
) -> dict[str, Any]:
    """`proxy_get` for a task: `{status, headers, body, truncated, location}` with the body as
    text, recorded to `$RW_SHIM_LOG` under `RW_SHIM_MODE=record` and answered from
    `$RW_SHIM_CALLS` under `RW_SHIM_MODE=replay` (no proxy is started then). A failed call is
    recorded as `error: {code, message, scope?, hint?}` and raised again on replay."""
    mode = os.environ.get("RW_SHIM_MODE")
    request = {
        "namespace": namespace,
        "kind": kind,
        "name": name,
        "port": port,
        "path": path,
        "scheme": scheme,
    }
    if mode == "replay":
        return _replay(request)
    try:
        result = proxy_get(
            namespace,
            kind,
            name,
            port,
            path,
            scheme=scheme,
            timeout=timeout,
            max_body=max_body,
            kubectl=kubectl,
        ).to_task_dict()
    except RwError as exc:
        if mode == "record":
            error = {"code": exc.code, "message": exc.message}
            if exc.scope is not None:
                error["scope"] = exc.scope
            if exc.hint is not None:
                error["hint"] = exc.hint
            _append_line("RW_SHIM_LOG", {"type": CALL_TYPE, "request": request, "error": error})
        raise
    if mode == "record":
        _append_line("RW_SHIM_LOG", {"type": CALL_TYPE, "request": request, "response": result})
    return result


# -- command line ---------------------------------------------------------------------------


def _record_error(error: RwError) -> None:
    """Appends the error to `$RW_ERRORS_FILE`, as rw.sh's `rw_error` does."""
    path = os.environ.get("RW_ERRORS_FILE")
    if not path:
        return
    entry = error.to_dict()
    entry.pop("details", None)
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (json.dumps(entry) + "\n").encode())
        finally:
            os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m runwhen_capability.k8s_proxy",
        description="GET through the Kubernetes API server's pod/service proxy, never "
        "following a redirect; prints {status, headers, body, truncated, location} as JSON.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    get = commands.add_parser("get", help="one proxied GET")
    get.add_argument("namespace")
    get.add_argument("kind", help="pods or services")
    get.add_argument("name")
    get.add_argument("port", help="a port number or a port name")
    get.add_argument("path", help="relative to the backend's root; may carry ?query")
    get.add_argument("--scheme", default="http", help="http (default) or https")
    get.add_argument("--timeout", type=float, default=30, help="seconds for the whole call")
    args = parser.parse_args(argv)
    try:
        result = task_get(
            args.namespace,
            args.kind,
            args.name,
            args.port,
            args.path,
            scheme=args.scheme,
            timeout=args.timeout,
        )
    except FixtureMismatch as exc:
        print(exc.message, file=sys.stderr)
        return _MISMATCH_EXIT
    except RwError as exc:
        _record_error(exc)
        print(f"rw_k8s_proxy_get: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
