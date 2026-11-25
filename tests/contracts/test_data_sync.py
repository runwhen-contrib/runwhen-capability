"""The packaged copy of contracts/ (src/runwhen_capability/contracts/_data)
must mirror the repo-level contracts/ directory exactly -- the wheel ships only
the former, so drift means published code validates against stale schemas."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "sync_contracts.py"


def _run(*args: str, cwd: Path = REPO) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_packaged_copy_matches_repo():
    result = _run("--check")
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_reports_drift_and_sync_repairs_it(tmp_path):
    """Exercise the script on a scratch copy of the layout, not the real tree."""
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "sync_contracts.py"
    script.write_text(SCRIPT.read_text())
    src = tmp_path / "contracts" / "schemas"
    src.mkdir(parents=True)
    (src / "kept.json").write_text("{}\n")
    (src / "changed.json").write_text('{"v": 2}\n')
    (src / "added.json").write_text("{}\n")
    dst = tmp_path / "src" / "runwhen_capability" / "contracts" / "_data" / "schemas"
    dst.mkdir(parents=True)
    (dst / "kept.json").write_text("{}\n")
    (dst / "changed.json").write_text('{"v": 1}\n')
    (dst / "stale.json").write_text("{}\n")

    def run(*args):
        return subprocess.run(
            [sys.executable, str(script), *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

    drift = run("--check")
    assert drift.returncode == 1
    assert "added" in drift.stdout and "schemas/added.json" in drift.stdout
    assert "removed" in drift.stdout and "schemas/stale.json" in drift.stdout
    assert "changed" in drift.stdout and "schemas/changed.json" in drift.stdout
    assert "kept.json" not in drift.stdout
    assert not (dst / "added.json").exists()  # --check never writes

    assert run().returncode == 0
    assert (dst / "added.json").read_text() == "{}\n"
    assert (dst / "changed.json").read_text() == '{"v": 2}\n'
    assert not (dst / "stale.json").exists()
    assert run("--check").returncode == 0
