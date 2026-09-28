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
