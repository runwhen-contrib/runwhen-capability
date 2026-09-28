"""Finding path/context normalization and the changed-files filter.

Findings surface only as GitHub Check Runs, which are keyed by commit SHA:
every push regenerates the whole set and GitHub discards the previous one,
so there is no cross-run identity to reconcile against. This module used to
also compute a `fingerprint` for that purpose; it was removed once its only
consumer (an LLM-prompt citation token) stopped needing it.
"""

from __future__ import annotations

import os
import posixpath
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from .models import Finding, FindingsResult
from .pathsafe import safe_path

#: The only severities a Finding may carry (models.Severity).
_SEVERITIES = frozenset({"error", "warning", "note"})

_WHITESPACE_RUN = re.compile(r"\s+")

# findings: a safety valve against a pathological blowup, NOT a presentation
# limit -- unlike repo_fs.py's read/grep/ls caps (MAX_READ_RESPONSE_BYTES,
# HARD_GREP_MAX_MATCHES, MAX_LS_ENTRIES), which bound what a human/agent reads
# in one call, this result is the *only* durable record a static-check task
# ever produces: the platform stores it verbatim as the run's result, there
# is no separate findings store, and whatever this cap drops is gone forever
# -- `truncated: true` records that data was lost, not what. Human-scale
# truncation belongs in whatever renders the findings (a Check Run, an agent
# tool); do not lower this to make either of those nicer.
#
# Sized off the transport that actually carries it, not a round number: the
# platform ingress this result passes through accepts 64 MiB
# (64 * 1,048,576 = 67,108,864 bytes). Budgeting half of
# that to the findings payload -- 33,554,432 bytes -- and leaving the other
# half as headroom for JSON envelope overhead and per-finding size variance,
# against the ~330 bytes/finding measured on the field failure this exists to
# prevent (a run against a large monorepo: 19,527 raw ruff findings, ~6.3 MB,
# a result rejected with HTTP 413 until the run gave up -- back when the
# ingress was nginx's 1 MB default) gives
# 33,554,432 // 330 = 101,679, rounded down to a clean 100,000. That leaves
# generous headroom above any realistic PR-scoped result, and even lets a
# monorepo-wide run like the 19,527-finding case above pass through whole --
# this cap only bites on a genuine runaway well past that.
#
# This bounds the PARSED result, not the raw report `json.loads` builds it
# from -- that copy is bigger, made before this cap (or `_flatten`'s islice
# in sarif.py) ever runs, and is what a pathological report actually OOMs the
# pod with. See sarif.py's SARIF_BYTE_BUDGET for the guard that stops
# `json.loads` itself from being handed more than it can safely build.
MAX_FINDINGS_PER_RESULT = 100_000


def normalize_path(path: str) -> str:
    """The finding path rule: repo-relative, forward slashes, no leading './'."""
    p = path.replace("\\", "/")
    return p[2:] if p.startswith("./") else p


def normalize_context(context: str) -> str:
    """The finding context rule: leading/trailing whitespace
    stripped, internal whitespace runs (tabs, CRLF, repeated spaces)
    collapsed to a single space. The line number is never part of this
    string -- callers must not fold it in."""
    trimmed = context.strip()
    return _WHITESPACE_RUN.sub(" ", trimmed)


# --- shared path / context resolution ---------------------------------------
# Lives here rather than in sarif.py because EVERY adapter -- SARIF or not --
# must normalise paths and resolve context identically: `context` is what
# a reader of the finding sees (the offending source line), so any
# divergence between the SARIF path and a JSON/text adapter would produce
# inconsistent context for the same defect. sarif.py re-exports
# `normalize_uri` for backwards compatibility with its existing importers.


def normalize_uri(uri: str, worktree_root: str) -> tuple[str, bool]:
    """A tool-reported location -> the finding path shape: repo-relative,
    forward slashes, no leading './'. Returns ("", False) when `uri` is
    empty, names a scheme this does not understand (anything but "file" or
    no scheme at all), or resolves outside `worktree_root` -- callers must
    DROP such a finding rather than diff-filter it against a path that can
    never match."""
    if not uri:
        return "", False

    p = uri
    try:
        parsed = urlparse(uri)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.scheme in ("", "file"):
            if parsed.path:
                p = unquote(parsed.path)
        else:
            return "", False
    p = p.replace("\\", "/")

    if not posixpath.isabs(p):
        stripped = p[2:] if p.startswith("./") else p
        clean = posixpath.normpath(stripped) if stripped else "."
        if clean in (".", "..") or clean.startswith("../"):
            return "", False
        return clean, True

    abs_root = os.path.abspath(worktree_root).replace("\\", "/")
    try:
        rel = posixpath.relpath(p, abs_root)
    except ValueError:
        return "", False
    if rel == ".." or rel.startswith("../"):
        return "", False
    return rel[2:] if rel.startswith("./") else rel, True


def resolve_base_uri(loc: Mapping, bases: Mapping) -> str:
    """Resolves an artifactLocation's uriBaseId against the run's
    originalUriBaseIds (SARIF spec). Returns the raw uri unresolved when
    there is no uriBaseId, the name is unknown, or a URI fails to parse."""
    uri = loc.get("uri", "") or ""
    base_id = loc.get("uriBaseId")
    if not base_id:
        return uri
    base = bases.get(base_id)
    if not base or not base.get("uri"):
        return uri
    try:
        return urljoin(base["uri"], uri)
    except ValueError:
        return uri


def read_line(path: Path, n: int) -> str:
    data = path.read_text(errors="replace")
    lines = data.split("\n")
    if n < 1 or n > len(lines):
        raise ValueError("line out of range")
    return lines[n - 1].rstrip("\r")


def resolve_context(
    worktree: Path,
    path: str,
    line: int,
    snippet: str = "",
    op_name: str = "",
    log=None,
) -> str:
    """The finding context source rule: prefer a tool-supplied
    snippet, otherwise read the anchored line out of the worktree, otherwise
    "". Never raises -- an unreadable file yields "" so one bad path cannot
    fail a whole task."""
    if snippet:
        return snippet
    if line <= 0 or not path:
        return ""
    safe = safe_path(worktree, normalize_path(path))
    if safe is None:
        if log is not None:
            log.warning("%s: rejected path outside worktree: %r", op_name, path)
        return ""
    try:
        return read_line(safe, line)
    except (OSError, ValueError):
        return ""


class FindingsClient:
    """`ctx.findings` -- the contract-defined finding operations."""

    def __init__(self, ctx) -> None:
        self._ctx = ctx

    def filter_changed(self, findings: list[Finding], changed: list[str] | None) -> list[Finding]:
        """The changed-files filter: emit only findings whose
        path is in `changed`. `changed` is the list ctx.git.changed_files()
        returned -- already reduced to paths with status != removed. A None
        or empty `changed` allows nothing through by design; callers only
        call this when they have a real diff (see the SDK example: `if
        changed: findings = ctx.findings.filter_changed(...)`)."""
        allowed = set(changed or [])
        return [f for f in findings if f.path in allowed]

    def from_records(
        self,
        records: Iterable[Mapping],
        *,
        root: Path | str,
        severity_map: Mapping[str, str] | None = None,
        default_severity: str = "warning",
    ) -> list[Finding]:
        """The non-SARIF adapter path: tool-shaped records -> Findings.

        Each record is a mapping with the keys an adapter has already
        extracted from its tool's output:

            path      required -- dropped if it does not resolve under `root`
            rule      required -- the tool's own rule/check id
            line      optional, default 0
            column    optional, default 0 -- 1-based
            end_line  optional, default 0 -- 0 means single-line
            end_column optional, default 0 -- 1-based
            severity  optional -- looked up in `severity_map`, else
                      `default_severity`. Mapping to our three-value
                      vocabulary is the CAPABILITY's job: every tool has its
                      own severity words and none of them mean the same
                      thing.
            message   optional, default ""
            snippet   optional -- when the tool supplies the offending text,
                      it is preferred over re-reading the line from disk.

        Behaves exactly like `ctx.sarif.parse` -- both paths share this
        module's path/context normalization so every adapter agrees.
        """
        root = Path(root)
        smap = severity_map or {}
        out: list[Finding] = []
        for rec in records:
            raw_path = rec.get("path", "") or ""
            normalized, ok = normalize_uri(raw_path, str(root))
            if not ok:
                if self._ctx.log is not None:
                    self._ctx.log.warning(
                        "%s: skipped finding with unresolvable location: %r",
                        self._ctx.operation,
                        raw_path,
                    )
                continue
            line = int(rec.get("line", 0) or 0)
            column = int(rec.get("column", 0) or 0)
            end_line = int(rec.get("end_line", 0) or 0)
            end_column = int(rec.get("end_column", 0) or 0)
            context = resolve_context(
                root,
                normalized,
                line,
                str(rec.get("snippet", "") or ""),
                self._ctx.operation,
                self._ctx.log,
            )
            raw_sev = str(rec.get("severity", "") or "").lower()
            out.append(
                Finding(
                    capability=self._ctx.capability,
                    operation=self._ctx.operation,
                    rule=str(rec.get("rule", "") or ""),
                    path=normalized,
                    line=line,
                    column=column,
                    end_line=end_line,
                    end_column=end_column,
                    # An adapter that already speaks our vocabulary passes
                    # through untouched. `severity_map` is for the other shape --
                    # a record carrying the tool's RAW severity word. Without
                    # this passthrough an already-mapped value misses `smap`
                    # entirely and silently becomes `default_severity`, which
                    # flattens every tool to one level.
                    severity=(
                        raw_sev if raw_sev in _SEVERITIES else smap.get(raw_sev, default_severity)
                    ),
                    message=str(rec.get("message", "") or ""),
                    context=normalize_context(context),
                )
            )
        return out

    def cap(
        self,
        findings: list[Finding],
        *,
        skipped: str | None = None,
        files_checked: int = 0,
    ) -> FindingsResult:
        """Caps `findings` at MAX_FINDINGS_PER_RESULT and reports whether it
        did -- the same honesty ctx.repo_fs.grep already gives a capped
        match list. This is a safety valve against a genuine runaway, not
        routine truncation -- any realistic run, even an undiffed one
        against a large repo, should pass through untouched. See
        MAX_FINDINGS_PER_RESULT for the byte-budget arithmetic. `cap` is
        the LAST call on the exact list a task is about to return: rw-checks'
        runner calls it after its own per-invocation safety filter (dropping
        findings outside the invocation's own files, `_runner.run_check`),
        so the cap sees only what the task actually hands back.

        The list handed in here is already bounded at MAX_FINDINGS_PER_RESULT
        + 1 by SarifClient.parse (sarif.py) -- that is what stops a genuine
        runaway from ever materialising a full-size findings list in the
        first place. This is still where `truncated` gets decided and the
        list gets sliced to the exact ceiling: parse() deliberately leaves
        the "+1" in so this comparison (`len(findings) > MAX...`) still
        fires correctly.

        `skipped`/`files_checked` pass straight through to FindingsResult --
        see models.FindingsResult for what each means."""
        truncated = len(findings) > MAX_FINDINGS_PER_RESULT
        return FindingsResult(
            findings=findings[:MAX_FINDINGS_PER_RESULT],
            truncated=truncated,
            skipped=skipped,
            files_checked=files_checked,
        )
