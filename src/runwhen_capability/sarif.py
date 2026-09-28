"""SARIF -> Finding. Only understands the SARIF 2.1.0 shape this SDK needs
(runs[].results[]) -- not a general-purpose SARIF library.

Path normalisation mirrors the Go implementation exactly: an absolute
`file://` artifactLocation.uri (ruff among the tools that emit one) is only
trustworthy against the worktree root the operation actually ran in, so a
location that does not resolve under `root` is DROPPED (logged, not silently
passed through with an unnormalized path) -- such a path can never match the
changed-files filter.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .errors import OutputTooLargeError
from .findings import (
    MAX_FINDINGS_PER_RESULT,
    normalize_context,
    normalize_path,
    normalize_uri,  # noqa: F401 -- re-exported: `sarif.normalize_uri` is a public import path
    resolve_base_uri,
)
from .models import Finding
from .pathsafe import safe_path

_READ_LINE_TIMEOUT_MSG = "line out of range"

# SARIF_BYTE_BUDGET: refuse a raw SARIF payload before json.loads ever builds
# a dict from it. MAX_FINDINGS_PER_RESULT already caps the *parsed* result at
# 100,000 findings, but that cap runs too late to help here: `json.loads`
# builds one full copy of the report -- the dict, every nested list/dict, every
# string -- before any of our code (the islice in parse(), ctx.findings.cap())
# gets a chance to act, and that copy is what OOMs the pod on a pathological
# report. Measured: a 500k-finding / 132 MB runaway peaks at ~1.0-1.16 GB RSS
# even with the finding-count cap in place -- the findings ceiling does not
# protect the pod on its own -- over the 1Gi executor limit. This budget stops that
# copy from ever being made.
#
# Sized from PEAK RSS, not finding count -- an earlier version of this
# constant used ~371 bytes/finding (the WIRE size, i.e. the stored result
# envelope) x MAX_FINDINGS_PER_RESULT to get ~37 MB. That is the wrong
# quantity: this guard measures raw SARIF text per `parse()` call, i.e. per
# TOOL, and raw SARIF carries overhead the wire format strips out entirely
# (tool.driver.rules metadata, full locations/regions, snippets,
# fingerprints). Measured directly against the real case instead: `ruff
# check --output-format=sarif .` against a large Python monorepo (the same
# repo a 6.3 MB / ~18,700-finding WIRE result came from) produces 56.7 MB of
# raw SARIF / 48,064 findings -- ~1,180 raw bytes/finding, 3.2x the wire
# estimate. A 40 MB budget derived from the wire number would
# have REJECTED this real, legitimate scan.
#
# The actual constraint is peak RSS on a 1Gi pod, so the budget is chosen
# from measured RSS at each candidate size, using the WORST memory-per-byte
# shape (many tiny results, no location, minimal strings -- maximises
# dict/object count per input byte) alongside the real monorepo ruff
# report and a synthetic "heavy tool" shape (5,000-rule tool.driver.rules
# block, snippets, full locations) for comparison. `parse()`'s own islice
# still caps how many Finding objects get built past MAX_FINDINGS_PER_RESULT,
# so cost beyond that count is `json.loads` alone -- which is exactly what
# this budget bounds:
#
#   size    worst-case (tiny, no location) peak RSS
#   40 MiB  526 MB
#   64 MiB  749 MB   <- picked: right at the ~750 MB target, 324 MB under 1Gi
#   96 MiB  1045 MB  -- over target AND effectively at the 1Gi ceiling itself
#
# 64 MiB (67,108,864 bytes) is the largest of the three that stays at/under
# the ~750 MB target while leaving real headroom below the hard 1Gi OOM
# point: ~18% above the measured 56.7 MB real monorepo ruff case (the
# only real single-tool raw SARIF size on record -- semgrep, trivy, gitleaks
# etc. were not installed locally to measure directly; if any of those
# routinely produce larger raw SARIF than ruff, e.g. via very large
# tool.driver.rules blocks, this budget should be re-measured against them).
# Coincidentally the same byte value as findings.py's 64 MiB ingress
# figure (64 * 1,048,576) -- unrelated numbers that happen to match; this
# constant is derived from measured RSS above, not from the ingress limit.
#
# Secondary sanity check, not the basis for the number above: at the real
# 1,180 bytes/finding raw density measured on that monorepo, a report that
# actually carried a full MAX_FINDINGS_PER_RESULT ceiling's worth of
# findings would be ~118 MB -- well past this 64 MiB budget. That is
# expected, not a conflict: the two caps guard different pathological
# shapes. This budget catches a single oversized report (however few or many
# findings it claims) before `json.loads` ever runs; MAX_FINDINGS_PER_RESULT
# still bounds materialised Finding objects for a report that stays under
# this byte budget but claims more findings than exist ceiling-space for.
#
# A streaming parser (e.g. ijson) was considered instead -- it would let a
# large-but-legitimate report through where a flat byte budget cannot. Not
# worth it here: the real case has real (if narrower, ~18%) headroom under
# this budget, ijson is a new dependency baked into every capability image,
# and the failure mode this budget produces (a disclosed, honest refusal) is
# exactly what a pathological report should get anyway. A
# byte guard is the simpler trade.
SARIF_BYTE_BUDGET = 64 * 1024 * 1024  # 67,108,864 bytes (64 MiB)


class SarifTooLargeError(OutputTooLargeError):
    """Raised by SarifClient.parse() when `text` exceeds SARIF_BYTE_BUDGET,
    checked BEFORE json.loads runs -- see SARIF_BYTE_BUDGET's docstring for
    why that ordering is the whole point. A subclass of the SDK-wide
    OutputTooLargeError (errors.py) rather than its own unrelated type: this
    is the same "oversized payload, caught before it's fully materialised"
    failure as Context.run()'s stdout cap, just at the point where the text is already in hand as a
    Python str and the next step is `json.loads`. Kept as a distinct
    subclass (not a bare alias) so a caller that specifically cares "was
    this SARIF text already parsed-in-hand when it got refused" can catch
    it by name; anything that just wants the general failure catches
    OutputTooLargeError instead -- both work, since `except
    OutputTooLargeError` also matches this subclass. Uncaught (the normal
    case), it propagates out of the capability's task function and is
    caught by host.py's generic handler, recorded as
    `TaskResult(status="failed", error=str(exc))`. That is a disclosed
    failure the platform surfaces as a failed scan -- never a silently
    empty or partial findings list: the failure that hurts is a clean,
    complete-looking result."""


@dataclass
class _RawFinding:
    rule_id: str
    severity: str  # already mapped -- see `severity_fn` in _flatten
    message: str
    path: str  # "" if no location
    line: int  # 0 if no region
    column: int  # 0 if region carries no startColumn
    end_line: int  # 0 if region carries no endLine (or no region)
    end_column: int  # 0 if region carries no endColumn
    snippet: str  # "" if the result carries no region.snippet.text


def severity(level: str) -> str:
    """SARIF `level` -> Finding severity: error -> error,
    warning -> warning, note/none/absent -> note."""
    lvl = (level or "").lower()
    if lvl == "error":
        return "error"
    if lvl == "warning":
        return "warning"
    return "note"


# `SarifClient.parse`'s own `severity` parameter (below) shadows this
# function inside that method -- this alias is how `parse` still reaches
# today's default mapping without renaming the public `sarif.severity`
# helper tests/test_sarif.py already imports directly.
_default_severity_from_level = severity


def _rule_properties(run: dict) -> dict[str, dict]:
    """ruleId -> that rule's `properties` dict, from
    runs[].tool.driver.rules[] -- the only place a SARIF report carries
    structured per-rule metadata (e.g. trivy's `security-severity`) beyond
    the bare `level` string on each result. {} for a rule with no
    `properties`, or one `results[].ruleId` never lists in `rules` at all
    (gitleaks: 222 rules, none of them carry properties)."""
    driver = ((run.get("tool") or {}).get("driver")) or {}
    out: dict[str, dict] = {}
    for rule in driver.get("rules") or []:
        rid = rule.get("id")
        if rid:
            out[rid] = rule.get("properties") or {}
    return out


def _flatten(
    report: dict,
    worktree_root: str,
    log,
    severity_fn: Callable[[str, str, dict], str],
) -> Iterator[_RawFinding]:
    """A generator, not a list-builder: `SarifClient.parse` wraps this in
    `itertools.islice(..., MAX_FINDINGS_PER_RESULT + 1)`, which stops
    calling `next()` -- and therefore stops this function ever touching
    `report["runs"][*]["results"]` past that point -- once the ceiling is
    hit. That is what keeps a genuine runaway report from materialising a
    full _RawFinding (or downstream Finding) list before the safety valve
    ever gets a chance to act; see MAX_FINDINGS_PER_RESULT's docstring and
    parse()'s comment below."""
    for run in report.get("runs", []) or []:
        bases = run.get("originalUriBaseIds") or {}
        rule_props = _rule_properties(run)
        for res in run.get("results", []) or []:
            rule_id = res.get("ruleId", "") or ""
            level = res.get("level", "") or ""
            sev = severity_fn(rule_id, level, rule_props.get(rule_id, {}))
            message = (res.get("message") or {}).get("text", "") or ""
            locations = res.get("locations") or []
            if not locations:
                yield _RawFinding(rule_id, sev, message, "", 0, 0, 0, 0, "")
                continue

            phys = (locations[0] or {}).get("physicalLocation") or {}
            artifact = phys.get("artifactLocation") or {}
            region = phys.get("region") or {}
            raw_uri = resolve_base_uri(artifact, bases)
            normalized, ok = normalize_uri(raw_uri, worktree_root)
            if not ok:
                _warn_skipped_location(log, rule_id, artifact.get("uri", ""))
                continue

            snippet = ((region.get("snippet") or {}).get("text", "")) or ""
            yield _RawFinding(
                rule_id=rule_id,
                severity=sev,
                message=message,
                path=normalized,
                line=region.get("startLine", 0) or 0,
                column=region.get("startColumn", 0) or 0,
                end_line=region.get("endLine", 0) or 0,
                end_column=region.get("endColumn", 0) or 0,
                snippet=snippet,
            )


def _warn_skipped_location(log, rule_id: str, raw_uri: str) -> None:
    if log is None:
        return
    log.warning("sarif: %s: skipped finding with unresolvable location: %r", rule_id, raw_uri)


def _warn_rejected_path(log, op_name: str, path: str) -> None:
    if log is None:
        return
    log.warning("sarif: %s: rejected path outside worktree: %r", op_name, path)


class _LineReader:
    """One-entry memo for reading a line out of a worktree file, scoped to a
    single SarifClient.parse() call. Without this, `_resolve_context` (the
    no-snippet fallback -- gitleaks' normal case) re-read and re-split the
    WHOLE file from disk for every finding that anchors to it: measured 2.2s
    for 20k findings over 200 x ~90 KB files, ~29s extrapolated at
    MAX_FINDINGS_PER_RESULT, against a lint phase that is itself ~8s. SARIF
    results group per-file (a tool emits all of one file's findings together), so
    remembering only the most-recently-read file's lines -- not every file
    this parse touches -- captures most of the repeat reads while keeping
    memory bounded to one file at a time. A path miss evicts the memo
    (last-write-wins, not an LRU): correctness for interleaved paths, not
    maximum hit rate, is the requirement -- see
    test_sarif.py's interleaved-paths test."""

    def __init__(self) -> None:
        self._path: Path | None = None
        self._lines: list[str] | None = None

    def read_line(self, path: Path, n: int) -> str:
        if path != self._path:
            # Raises before self._path is updated, so a failed read is
            # never cached as if it had succeeded.
            lines = path.read_text(errors="replace").split("\n")
            self._path = path
            self._lines = lines
        assert self._lines is not None
        if n < 1 or n > len(self._lines):
            raise ValueError(_READ_LINE_TIMEOUT_MSG)
        return self._lines[n - 1].rstrip("\r")


def _resolve_context(
    rf: _RawFinding, worktree: Path, op_name: str, log, line_reader: _LineReader
) -> str:
    """The finding context source rule: prefer the SARIF snippet,
    otherwise read the anchored line from the worktree, otherwise ""."""
    if rf.snippet:
        return rf.snippet
    if rf.line <= 0 or not rf.path:
        return ""
    safe = safe_path(worktree, normalize_path(rf.path))
    if safe is None:
        _warn_rejected_path(log, op_name, rf.path)
        return ""
    try:
        return line_reader.read_line(safe, rf.line)
    except OSError:
        return ""
    except ValueError:
        return ""


class SarifClient:
    """`ctx.sarif` -- SARIF -> findings, tagged with the owning Context's
    capability and operation."""

    def __init__(self, ctx) -> None:
        self._ctx = ctx

    def parse(
        self,
        text: str,
        root: Path | str,
        severity: Callable[[str, str, dict], str] | None = None,
    ) -> list[Finding]:
        """`severity`, when given, is called per result as
        `severity(rule_id, sarif_level, rule_properties)` -- `rule_properties`
        is that result's rule's `properties` dict from
        runs[].tool.driver.rules[] (matched by id), or {} when absent -- and
        must return "error"|"warning"|"note". This is a per-CAPABILITY
        policy, not an SDK default: SARIF `level` means a different thing
        for every tool. `None`
        keeps today's behaviour exactly: `level` mapped by the module-level
        `severity()` function above, ignoring `rule_id`/`rule_properties`.

        Bounded at MAX_FINDINGS_PER_RESULT + 1 findings -- one past
        ctx.findings.cap()'s ceiling, so that call still sees `len(findings)
        > MAX_FINDINGS_PER_RESULT` and reports `truncated` correctly,
        without this function ever having built more than a ceiling's worth
        of _RawFinding/Finding objects along the way. `_flatten` is a
        generator specifically so `itertools.islice` bounds it lazily: on a
        genuine runaway (hundreds of thousands of SARIF results), this never
        touches the results past the +1st, rather than flattening all of
        them and discarding the excess only at the very end (cap() running
        last, after every copy in the pipeline had already paid for the
        full size -- see MAX_FINDINGS_PER_RESULT's docstring). Real results
        (measured: 6.3 MB / ~18,700 findings) stay far under the ceiling and
        pass through whole, byte for byte -- islice only ever bites on the
        pathological case this ceiling exists for.

        That ceiling still runs too late to bound `json.loads` itself, which
        is why the very first thing this method does is check `text`'s size
        against SARIF_BYTE_BUDGET and raise SarifTooLargeError before
        `json.loads` ever touches it -- see that constant's docstring for the
        arithmetic and why a byte budget beats a streaming parser here. The
        check itself avoids `text.encode("utf-8")` on the common path: a
        Python str's UTF-8 encoding is always between `len(text)` (all-ASCII)
        and `4 * len(text)` (every character maximal-width) bytes, so
        `len(text)` alone already proves a report over budget (bytes can only
        be >= chars) without paying for an encode, and `4 * len(text)` at or
        under budget already proves a report under it (bytes can only be <=
        4x chars) -- encoding only has to run to get an exact byte count in
        the narrow band between those two bounds. Real SARIF is close enough
        to all-ASCII that this band is rarely entered at all."""
        char_len = len(text)
        if char_len > SARIF_BYTE_BUDGET:
            raise SarifTooLargeError(f"check output too large to process: {char_len} bytes")
        if char_len * 4 > SARIF_BYTE_BUDGET:
            size = len(text.encode("utf-8"))
            if size > SARIF_BYTE_BUDGET:
                raise SarifTooLargeError(f"check output too large to process: {size} bytes")
        root = Path(root)
        report = json.loads(text)
        severity_fn = (
            severity
            if severity is not None
            else (lambda rule_id, level, props: _default_severity_from_level(level))
        )
        raw_findings = itertools.islice(
            _flatten(report, str(root), self._ctx.log, severity_fn), MAX_FINDINGS_PER_RESULT + 1
        )

        line_reader = _LineReader()
        findings: list[Finding] = []
        for rf in raw_findings:
            context = _resolve_context(rf, root, self._ctx.operation, self._ctx.log, line_reader)
            findings.append(
                Finding(
                    capability=self._ctx.capability,
                    operation=self._ctx.operation,
                    rule=rf.rule_id,
                    path=rf.path,
                    line=rf.line,
                    column=rf.column,
                    end_line=rf.end_line,
                    end_column=rf.end_column,
                    severity=rf.severity,
                    message=rf.message,
                    context=normalize_context(context),
                )
            )
        return findings
