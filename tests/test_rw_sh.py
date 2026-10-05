"""rw.sh's `rw_input`, run in a real bash: it must derive an input's env var
name exactly as the host does (manifest.input_env_name), and fall back to the
legacy per-letter name an older host sets (H51)."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from runwhen_capability._rw_sh import RW_SH
from runwhen_capability.custom.manifest import input_env_name, legacy_input_env_name

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

_NAMES = [
    "since",
    "maxWait",
    "maxTimeouts",
    "dry_run",
    "tail_lines",
    "fillThresholdPct",
    "THRESHOLD",
    "DRY_RUN",
    "MAX_AUTH_FAILURES",
    "HTTPTimeout",
    "apiURL",
    "k8sNamespace",
    "getHTTPResponseCode",
    "a1B",
    "X",
    "x",
    "ENV",
    "ABc",
    "aBCDe",
]


def _bash(tmp_path, script: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    rw_sh = tmp_path / "rw.sh"
    rw_sh.write_text(RW_SH)
    return subprocess.run(
        ["bash", "-c", f'source "{rw_sh}"\n{script}'],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("name", "env_name"),
    [
        ("THRESHOLD", "THRESHOLD"),
        ("maxWait", "MAX_WAIT"),
        ("HTTPTimeout", "HTTP_TIMEOUT"),
        ("dry_run", "DRY_RUN"),
    ],
)
def test_rw_input_reads_the_new_env_name(tmp_path, name, env_name):
    proc = _bash(tmp_path, f"rw_input {name}", {env_name: "v-new"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "v-new"


@pytest.mark.parametrize("name", _NAMES)
def test_rw_input_name_derivation_matches_input_env_name(tmp_path, name):
    # Only the env var the Python host would set holds the value; every
    # other plausible spelling holds a decoy, so a wrong derivation shows.
    env_name = input_env_name(name)
    decoys = {legacy_input_env_name(name), name.upper(), name} - {env_name}
    env = {decoy: "decoy" for decoy in decoys} | {env_name: "host"}
    proc = _bash(tmp_path, f"rw_input {name}", env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "host"


def test_rw_input_prefers_the_new_name_over_the_legacy_one(tmp_path):
    proc = _bash(tmp_path, "rw_input THRESHOLD", {"THRESHOLD": "new", "T_H_R_E_S_H_O_L_D": "old"})
    assert proc.stdout == "new"


@pytest.mark.parametrize("name", ["THRESHOLD", "HTTPTimeout", "ENV"])
def test_rw_input_falls_back_to_the_legacy_name_from_an_old_host(tmp_path, name):
    legacy = legacy_input_env_name(name)
    assert legacy != input_env_name(name)
    proc = _bash(tmp_path, f"rw_input {name}", {legacy: "old-host"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "old-host"


def test_rw_input_prints_nothing_for_an_unset_input(tmp_path):
    proc = _bash(tmp_path, 'rw_input THRESHOLD; echo "rc=$?"', {})
    assert proc.stdout == "rc=0\n"
