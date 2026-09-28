"""SARIF -> Finding, including URI normalisation: relative URIs, absolute file://
URIs resolved against the worktree root, and a location that escapes the
worktree being dropped rather than passed through unnormalized.
"""

import json
from pathlib import Path

import pytest

from runwhen_capability import Context
from runwhen_capability.sarif import SARIF_BYTE_BUDGET, SarifTooLargeError, normalize_uri, severity

FIXTURES = Path(__file__).parent / "fixtures"
REPO = FIXTURES / "repo"


def make_ctx(operation: str) -> Context:
    return Context(capability="rw-checks", operation=operation, workdir=REPO)


def test_parse_ruff_relative_uris():
    ctx = make_ctx("ruff")
    text = (FIXTURES / "ruff.sarif").read_text()

    findings = ctx.sarif.parse(text, root=REPO)

    assert len(findings) == 2

    first = findings[0]
    assert first.rule == "F401"
    assert first.severity == "warning"
    assert first.path == "src/x.py"
    assert first.line == 1
    assert first.context == "import os"
    assert first.capability == "rw-checks"
    assert first.operation == "ruff"
    assert first.message

    second = findings[1]
    assert second.rule == "E501"
    assert second.severity == "error"
    assert second.line == 4


def test_parse_gitleaks_no_snippet_reads_worktree_line():
    ctx = make_ctx("gitleaks")
    text = (FIXTURES / "gitleaks.sarif").read_text()

    findings = ctx.sarif.parse(text, root=REPO)

    assert len(findings) == 1
    got = findings[0]
    assert got.rule == "generic-api-key"
    assert got.path == "src/config.py"
    assert got.line == 3
    # No region.snippet in the fixture -- context is read from the worktree
    # at the anchored line.
    assert got.context == 'SECRET_TOKEN = "ghp_00000000000000000000000000000000"'


def test_parse_worktree_reads_are_correct_across_interleaved_paths(tmp_path):
    """_resolve_context's worktree-line fallback (no SARIF snippet -- the
    gitleaks case) is backed by a one-entry memo (_LineReader) keyed on
    path, added because it was re-reading and re-splitting the whole file
    on every single finding. The memo must never return a stale line after
    the requested path switches away and back -- pinned here with paths
    interleaved (a, b, a, b, ...), not just a sequential run of the same
    path, which a broken memo could get right by accident."""
    tree = tmp_path / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "a.py").write_text("a-line-1\na-line-2\na-line-3\n")
    (tree / "src" / "b.py").write_text("b-line-1\nb-line-2\nb-line-3\n")

    def result(path: str, line: int) -> dict:
        return {
            "ruleId": "generic-api-key",
            "level": "error",
            "message": {"text": "secret"},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": path},
                        "region": {"startLine": line},
                    }
                }
            ],
        }

    report = {
        "version": "2.1.0",
        "runs": [
            {
                "results": [
                    result("src/a.py", 1),
                    result("src/b.py", 1),
                    result("src/a.py", 2),
                    result("src/b.py", 2),
                    result("src/a.py", 3),
                    result("src/b.py", 3),
                ]
            }
        ],
    }

    ctx = Context(capability="rw-checks", operation="gitleaks", workdir=tree)
    findings = ctx.sarif.parse(json.dumps(report), root=tree)

    assert [f.context for f in findings] == [
        "a-line-1",
        "b-line-1",
        "a-line-2",
        "b-line-2",
        "a-line-3",
        "b-line-3",
    ]


def test_parse_absolute_file_uris_normalize_to_repo_relative_and_drop_outside_worktree(caplog):
    tmpl = (FIXTURES / "ruff_abs.sarif.tmpl").read_text()
    text = tmpl.format(ROOT=str(REPO.resolve()))

    ctx = make_ctx("ruff")
    findings = ctx.sarif.parse(text, root=REPO)

    # The /etc/passwd location resolves outside the worktree and must be
    # dropped, not passed through with an unnormalized path.
    assert len(findings) == 1
    got = findings[0]
    assert got.rule == "F401"
    assert got.path == "src/x.py"
    assert got.line == 1


def test_normalize_uri_relative():
    got, ok = normalize_uri("src/x.py", "/irrelevant")
    assert ok is True
    assert got == "src/x.py"


def test_normalize_uri_dot_slash_prefixed():
    got, ok = normalize_uri("./src/x.py", "/irrelevant")
    assert ok is True
    assert got == "src/x.py"


def test_normalize_uri_percent_encoded_space(tmp_path):
    uri = f"file://{tmp_path}/my%20dir/file.py"
    got, ok = normalize_uri(uri, str(tmp_path))
    assert ok is True
    assert got == "my dir/file.py"


def test_normalize_uri_outside_worktree(tmp_path):
    got, ok = normalize_uri("file:///etc/passwd", str(tmp_path))
    assert ok is False


def test_normalize_uri_empty():
    got, ok = normalize_uri("", "/irrelevant")
    assert ok is False


def test_normalize_uri_unsupported_scheme():
    got, ok = normalize_uri("https://example.com/x.py", "/irrelevant")
    assert ok is False


@pytest.mark.parametrize(
    "level,want",
    [
        ("error", "error"),
        ("ERROR", "error"),
        ("warning", "warning"),
        ("note", "note"),
        ("none", "note"),
        ("", "note"),
        ("something-unknown", "note"),
    ],
)
def test_severity(level, want):
    assert severity(level) == want


# --- SARIF_BYTE_BUDGET (guard before json.loads) -----------------------------
# See sarif.py's SARIF_BYTE_BUDGET docstring for the arithmetic. These build
# ASCII payloads, so `len(text) == len(text.encode("utf-8"))` and the exact
# byte counts below are exact -- except the UTF-8 test, which exists
# specifically to prove the guard counts encoded bytes, not characters.


def _padded_sarif_text(total_bytes: int) -> str:
    """A valid, parseable SARIF-shaped ASCII JSON string of exactly
    `total_bytes` bytes (== characters, since every filler byte is ASCII):
    `{"runs": [], "pad": "<a * n>"}`. Valid JSON matters for the
    at-or-under-budget cases, which must actually reach json.loads and
    succeed -- an over-budget payload never gets that far, but building one
    that would still parse (rather than e.g. random garbage) keeps every
    case here testing the same shape."""
    template = '{{"runs": [], "pad": "{}"}}'
    empty_len = len(template.format(""))
    assert total_bytes >= empty_len, "total_bytes too small for the template overhead"
    return template.format("a" * (total_bytes - empty_len))


def test_parse_exactly_at_budget_is_accepted():
    text = _padded_sarif_text(SARIF_BYTE_BUDGET)
    assert len(text.encode("utf-8")) == SARIF_BYTE_BUDGET

    ctx = make_ctx("ruff")
    findings = ctx.sarif.parse(text, root=REPO)

    assert findings == []


def test_parse_one_byte_over_budget_is_rejected():
    text = _padded_sarif_text(SARIF_BYTE_BUDGET + 1)

    ctx = make_ctx("ruff")
    with pytest.raises(SarifTooLargeError) as exc_info:
        ctx.sarif.parse(text, root=REPO)

    # The exact message required: a disclosed cause, not a
    # silent empty/partial findings list -- host.py records this verbatim as
    # the failed TaskResult's `error`.
    assert (
        str(exc_info.value) == f"check output too large to process: {SARIF_BYTE_BUDGET + 1} bytes"
    )


def test_parse_over_budget_never_reaches_json_loads(monkeypatch):
    """The whole point of checking size before parsing: json.loads must
    never even be called on an over-budget payload, so the runaway
    allocation it would make never happens."""
    import runwhen_capability.sarif as sarif_module

    def _boom(*_args, **_kwargs):
        raise AssertionError("json.loads must not run on an over-budget payload")

    monkeypatch.setattr(sarif_module.json, "loads", _boom)

    text = _padded_sarif_text(SARIF_BYTE_BUDGET + 1)
    ctx = make_ctx("ruff")
    with pytest.raises(SarifTooLargeError):
        ctx.sarif.parse(text, root=REPO)


def test_parse_budget_is_measured_in_utf8_bytes_not_characters():
    """A multi-byte character pads the byte count faster than the character
    count -- the guard must reject on bytes, or a report full of non-ASCII
    text could sail through under a budget sized for bytes while actually
    costing json.loads far more than SARIF_BYTE_BUDGET bytes to parse."""
    # "é" (e-acute) is 1 char, 2 UTF-8 bytes: well under SARIF_BYTE_BUDGET
    # in character count, comfortably over it in encoded bytes.
    template = '{{"runs": [], "pad": "{}"}}'
    filler_chars = SARIF_BYTE_BUDGET // 2 + 1000
    text = template.format("é" * filler_chars)
    assert len(text) < SARIF_BYTE_BUDGET  # char count alone would look fine
    assert len(text.encode("utf-8")) > SARIF_BYTE_BUDGET  # actual bytes do not

    ctx = make_ctx("ruff")
    with pytest.raises(SarifTooLargeError):
        ctx.sarif.parse(text, root=REPO)
