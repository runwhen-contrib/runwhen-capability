"""Structured execution errors (CAP-5, D64): the closed code list, `RwError`
for a script that wants to fail with a code, `RwCommandError` for a command
that exited non-zero, and `classify_command_error`, which maps a failed
CLI's stderr to a code.

An error here is "the task failed to look", never a finding (D63). The codes
are the `error` contract's enum (contracts/schemas/error.schema.json);
retryability belongs to the code, never the instance.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

ERROR_CODES: tuple[str, ...] = (
    "permission_denied",
    "not_found",
    "unreachable",
    "timeout",
    "rate_limited",
    "storage",
    "invalid_input",
    "dependency",
    "internal",
)


def check_error_code(code: object) -> str:
    """`code` if it is one of ERROR_CODES, else ValueError."""
    if not isinstance(code, str) or code not in ERROR_CODES:
        raise ValueError(f"invalid error code {code!r}: expected one of {', '.join(ERROR_CODES)}")
    return code


class RwError(Exception):
    """A task's structured failure: `raise RwError("not_found", "no such
    deployment", scope="ns/web")`. Uncaught, it ends the task with that code
    instead of a generic `internal`. `scope`, `hint` and `details` are
    optional; `code` must be one of ERROR_CODES (ValueError otherwise)."""

    def __init__(
        self,
        code: str,
        message: str,
        scope: str | None = None,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = check_error_code(code)
        self.message = message
        self.scope = scope
        self.hint = hint
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        """The `error` contract's shape: optional keys only when set."""
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.scope is not None:
            out["scope"] = self.scope
        if self.hint is not None:
            out["hint"] = self.hint
        if self.details is not None:
            out["details"] = self.details
        return out


class RwCommandError(RwError):
    """A command that exited non-zero (`ctx.run(..., check=True)`): an
    RwError whose `code` came from `classify_command_error`, carrying what
    the command did."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        argv: Sequence[str],
        returncode: int,
        stderr: str,
        scope: str | None = None,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code, message, scope=scope, hint=hint, details=details)
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr


def _any(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


_RATE_LIMITED = re.compile(r"\b429\b|too many requests|rate limit|throttling")


def classify_command_error(argv: Sequence[str], returncode: int, stderr: str) -> str:
    """The error code for a failed command, from its stderr and exit status.
    First match wins, in this order: permission_denied, not_found,
    unreachable, timeout, rate_limited, storage, dependency, else internal.
    Matching is case-insensitive. `argv` is accepted so a later rule can be
    tool-specific; no rule reads it today.

    `command not found` is a missing binary (dependency), never a missing
    resource, so it is excluded from the `not found` rule; as is any
    `not found` at exit status 127 (the shell's "no such command")."""
    text = stderr.lower()
    if _any(text, "forbidden", "permission denied", "authorizationfailed", "accessdenied"):
        return "permission_denied"
    if returncode != 127 and (
        "notfound" in text or "not found" in text.replace("command not found", "")
    ):
        return "not_found"
    if _any(
        text,
        "unable to connect",
        "i/o timeout",
        "connection refused",
        "no such host",
        "could not translate host name",
        "tls handshake",
        "x509:",
        "could not resolve host",
    ):
        return "unreachable"
    if returncode == 124 or _any(text, "deadline exceeded", "context deadline", "timed out"):
        return "timeout"
    if _RATE_LIMITED.search(text):
        return "rate_limited"
    if _any(text, "no space left on device", "disk quota"):
        return "storage"
    if returncode == 127 or "command not found" in text:
        return "dependency"
    return "internal"
