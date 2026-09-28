"""ctx.findings.filter_changed / .cap -- the changed-files diff filter
(present -> emit only findings whose path is in files with status !=
removed) and the MAX_FINDINGS_PER_RESULT wire cap.
"""

from runwhen_capability import Context
from runwhen_capability.findings import MAX_FINDINGS_PER_RESULT
from runwhen_capability.models import Finding


def make_finding(path: str, rule: str = "F401") -> Finding:
    return Finding(
        capability="rw-checks",
        operation="ruff",
        rule=rule,
        path=path,
        line=1,
        severity="warning",
        message="msg",
        context="import os",
    )


def test_filter_changed_keeps_only_listed_paths():
    ctx = Context(capability="rw-checks", operation="ruff", workdir="/tmp")
    findings = [make_finding("src/x.py"), make_finding("src/y.py"), make_finding("src/z.py")]

    got = ctx.findings.filter_changed(findings, ["src/x.py", "src/z.py"])

    assert [f.path for f in got] == ["src/x.py", "src/z.py"]


def test_filter_changed_empty_changed_list_allows_nothing():
    ctx = Context(capability="rw-checks", operation="ruff", workdir="/tmp")
    findings = [make_finding("src/x.py")]

    got = ctx.findings.filter_changed(findings, [])

    assert got == []


# --- cap (MAX_FINDINGS_PER_RESULT) ------------------------------------------


def test_cap_over_limit_truncates_and_flags_it():
    ctx = Context(capability="rw-checks", operation="ruff", workdir="/tmp")
    findings = [make_finding(f"src/{i}.py") for i in range(MAX_FINDINGS_PER_RESULT + 1)]

    got = ctx.findings.cap(findings)

    assert len(got.findings) == MAX_FINDINGS_PER_RESULT
    assert got.truncated is True


def test_cap_under_limit_keeps_everything_untruncated():
    ctx = Context(capability="rw-checks", operation="ruff", workdir="/tmp")
    findings = [make_finding("src/x.py"), make_finding("src/y.py")]

    got = ctx.findings.cap(findings)

    assert got.findings == findings
    assert got.truncated is False


def test_cap_exactly_at_limit_is_not_truncated():
    ctx = Context(capability="rw-checks", operation="ruff", workdir="/tmp")
    findings = [make_finding(f"src/{i}.py") for i in range(MAX_FINDINGS_PER_RESULT)]

    got = ctx.findings.cap(findings)

    assert len(got.findings) == MAX_FINDINGS_PER_RESULT
    assert got.truncated is False


def test_cap_realistic_monorepo_scale_passes_through_untruncated():
    # Regression guard for the bug this whole change fixes: 19,527 is the
    # measured monorepo-wide ruff run (~6.3 MB, ~330 bytes/
    # finding) that the old 500 cap would have silently gutted to 500 --
    # destroying the only durable record of ~19,000 real findings. It must
    # pass through whole under the new byte-budget-sized ceiling.
    ctx = Context(capability="rw-checks", operation="ruff", workdir="/tmp")
    findings = [make_finding(f"src/{i}.py") for i in range(19_527)]

    got = ctx.findings.cap(findings)

    assert len(got.findings) == 19_527
    assert got.truncated is False
