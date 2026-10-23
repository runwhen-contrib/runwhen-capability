"""Exceptions the SDK raises. All of them are ordinary Python exceptions --
the task host (see host.py) catches anything a setup/task function raises and
turns it into a failed setup/task result entry; it never needs to special-case
these types.
"""


class CredentialNotFoundError(KeyError):
    """Raised by Context.credential() when the named credential was not
    resolved for this request."""


class CapabilityLoadError(RuntimeError):
    """Raised when a capability directory's manifest.yaml or tasks.py cannot
    be loaded."""


class UnknownTaskError(RuntimeError):
    """Raised when a request names a setup/task that is not registered by the
    capability's tasks.py."""


class OutputTooLargeError(ValueError):
    """Raised when captured tool output exceeds the SDK's byte budget
    (sarif.py's SARIF_BYTE_BUDGET), before that output is fully read into
    memory or handed to a parser. Context.run() (context.py) bounds a
    subprocess's captured stdout while it is still streaming in, and
    sarif.py's SarifClient.parse() (via its subclass SarifTooLargeError, see
    below) checks SARIF text already in hand before `json.loads`; a
    capability that reads a tool's report FILE can raise it too, before
    reading the file. All of them exist for the same reason: whichever of these
    runs first is the one that would otherwise hold a pathological amount
    of text in memory before any downstream cap (MAX_FINDINGS_PER_RESULT,
    ctx.findings.cap()) gets a chance to act.

    Context.run() is generic across every tool this SDK runs, SARIF-emitting
    or not -- so bounding it there also protects a capability's plain
    `json.loads` of tool output, which has no SARIF-shaped guard of its
    own: an oversized payload never makes it out of ctx.run in the first
    place.

    An ordinary exception, per this module's docstring: uncaught, it
    propagates to host.py's generic handler and becomes a failed
    TaskResult -- a disclosed failure, never a silent empty/partial
    findings list."""


class SkipTask(Exception):  # noqa: N818 -- a control-flow signal, not an error
    """Raised by a task that decides not to run -- nothing in its scope to
    check, a precondition that makes the work meaningless. The task host
    records it as `TaskResult(status="skipped", reason=<message>)`: distinct
    from `ok` (the task did its work) and from `failed` (the task tried and
    could not). The rest of the request still runs.

    Only tasks can skip. A setup that raises this is an ordinary setup
    failure: it has not materialised anything the tasks could use.

        raise SkipTask("no Python files in the diff")
    """


class ArtifactError(Exception):
    """Base of every artifact failure. `code` is the relay's machine-readable
    code (`E_ARTIFACT_TOO_LARGE`, ...), or `HTTP_<status>` when the relay
    answered without one; `message` is the human-readable text.

    An ordinary exception, per this module's docstring: uncaught, it fails
    the task with its message, never a silently missing artifact. Each code
    the relay can return has its own subclass below, so a task that wants to
    handle one case (say, an artifact that already exists) catches that
    class instead of comparing strings."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code, message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.message else self.code


class ArtifactTooLargeError(ArtifactError):
    """The artifact is bigger than the maximum its output declares. The
    relay stops reading at that size, so the upload fails fast rather than
    after the whole body is sent. `max_bytes` is that maximum, when the
    relay reported it."""

    def __init__(self, code: str, message: str = "", max_bytes: int | None = None) -> None:
        super().__init__(code, message)
        self.max_bytes = max_bytes


class ArtifactUndeclaredError(ArtifactError):
    """The name is not one the task declared as an artifact output (or, for
    an input, not one of the request's input artifacts), or the request is
    not running on this pod or has no artifact lease (a synchronous
    request)."""


class ArtifactExistsError(ArtifactError):
    """An artifact with this name was already written for this request."""


class ArtifactQuotaError(ArtifactError):
    """The workspace is over its artifact quota."""


class ArtifactLeaseLostError(ArtifactError):
    """This request's lease is no longer held, so its artifacts can no
    longer be accepted (the runner re-queued the request)."""


class ArtifactBusyError(ArtifactError):
    """The runner had no upload slot free within its stall timeout. A
    stream cannot be replayed, so only a caller that can re-read its source
    (a file) is able to retry."""


class ArtifactsUnavailableError(ArtifactError):
    """Artifacts are switched off for this deployment (`E_ARTIFACTS_DISABLED`)
    or not supported by this runner (`E_ARTIFACTS_UNSUPPORTED`)."""


class ResultTooLargeError(Exception):
    """A result is larger than the request's `maxResultBytes`. The serve
    loop does not raise it: it posts a failed result whose error starts with
    `E_RESULT_TOO_LARGE` (serve.py), rather than a result the runner would
    reject. Large data belongs in an artifact (`ctx.artifacts`)."""
