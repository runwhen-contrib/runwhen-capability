"""rw.sh's `rw_input`, run in a real bash: it must derive an input's env var
name exactly as the host does (manifest.input_env_name), and fall back to the
legacy per-letter name an older host sets."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from runwhen_capability._rw_sh import RW_SH
from runwhen_capability.custom.manifest import (
    input_env_name,
    is_valid_artifact_name,
    legacy_input_env_name,
)

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


# -- rw_artifact ------------------------------------------------------------------


def _artifact_bash(tmp_path, script: str, cwd=None) -> tuple[subprocess.CompletedProcess, list]:
    """Runs `script` after sourcing rw.sh with the private output channel on fd 7
    (a file) and RW_ARTIFACTS_DIR set; returns the process and the events it wrote."""
    rw_sh = tmp_path / "rw.sh"
    rw_sh.write_text(RW_SH)
    events = tmp_path / "events"
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'exec 7>"{events}"\nsource "{rw_sh}"\n{script}',
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "RW_OUTPUT_FD": "7",
            "RW_ARTIFACTS_DIR": str(tmp_path / "artifacts"),
        },
        cwd=cwd or tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = events.read_text().splitlines() if events.exists() else []
    return proc, [json.loads(line) for line in lines]


def test_rw_artifact_pipe_writes_file_and_emits_event(tmp_path):
    proc, events = _artifact_bash(tmp_path, "printf 'a\\nb\\n' | rw_artifact dump -")
    assert proc.returncode == 0, proc.stderr
    target = tmp_path / "artifacts" / "dump"
    assert target.read_bytes() == b"a\nb\n"
    assert events == [{"op": "artifact", "name": "dump", "path": str(target)}]


def test_rw_artifact_pipe_accepts_a_name_with_dots_dashes_and_underscores(tmp_path):
    proc, events = _artifact_bash(tmp_path, "printf x | rw_artifact v2_a-b.tar.gz -")
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "artifacts" / "v2_a-b.tar.gz").read_bytes() == b"x"
    assert events[0]["name"] == "v2_a-b.tar.gz"


def test_rw_artifact_pipe_recreates_a_missing_artifacts_dir(tmp_path):
    proc, events = _artifact_bash(
        tmp_path, 'rmdir "$RW_ARTIFACTS_DIR"; printf x | rw_artifact dump -'
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "artifacts" / "dump").read_bytes() == b"x"


def test_rw_artifact_pipe_streams_binary_data_through_cat(tmp_path):
    # NUL bytes and a missing trailing newline survive: the data never passes
    # through a shell variable.
    script = "printf 'a\\000b\\377' | rw_artifact blob -"
    proc, _ = _artifact_bash(tmp_path, script)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "artifacts" / "blob").read_bytes() == b"a\x00b\xff"


def test_rw_artifact_path_registers_the_file_without_copying_it(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "r.txt").write_text("data")
    proc, events = _artifact_bash(tmp_path, "rw_artifact report out/r.txt")
    assert proc.returncode == 0, proc.stderr
    # emitted absolute even though it was given relative, and nothing was copied
    assert events == [
        {"op": "artifact", "name": "report", "path": str(tmp_path.resolve() / "out" / "r.txt")}
    ]
    assert list((tmp_path / "artifacts").iterdir()) == []


def test_rw_artifact_path_keeps_an_absolute_path_as_given(tmp_path):
    target = tmp_path / "r.txt"
    target.write_text("data")
    proc, events = _artifact_bash(tmp_path, f"rw_artifact report '{target}'")
    assert proc.returncode == 0, proc.stderr
    assert events[0]["path"] == str(target)


def test_rw_artifact_escapes_a_path_with_quotes_and_backslashes(tmp_path):
    target = tmp_path / 'we"ird \\ name.txt'
    target.write_text("data")
    proc, events = _artifact_bash(tmp_path, f"rw_artifact report '{target}'")
    assert proc.returncode == 0, proc.stderr
    assert events == [{"op": "artifact", "name": "report", "path": str(target)}]


@pytest.mark.parametrize("script", ["rw_artifact", "rw_artifact dump", "rw_artifact '' -"])
def test_rw_artifact_needs_a_name_and_a_source(tmp_path, script):
    proc, events = _artifact_bash(tmp_path, f'{script}\necho "rc=$?"')
    assert "rc=0" not in proc.stdout
    assert "rw_artifact:" in proc.stderr
    assert events == []


def test_rw_artifact_refuses_a_path_it_cannot_read(tmp_path):
    proc, events = _artifact_bash(tmp_path, 'rw_artifact dump missing.txt; echo "rc=$?"')
    assert "rc=1" in proc.stdout
    assert "missing.txt" in proc.stderr
    assert events == []


def test_rw_artifact_refuses_a_directory(tmp_path):
    (tmp_path / "somedir").mkdir()
    proc, events = _artifact_bash(tmp_path, 'rw_artifact dump somedir; echo "rc=$?"')
    assert "rc=1" in proc.stdout
    assert events == []


@pytest.mark.parametrize(
    "name",
    ["../up", "/abs", "a/../b", "a//b", "a/b", "a/", ".", "..", ".hidden", "-x", "my report.txt"]
    + ["dump:v2", "a'b", 'a"b', "a\nb", "$HOME"],
)
def test_rw_artifact_refuses_a_name_that_is_not_one_plain_segment(tmp_path, name):
    rw_sh = tmp_path / "rw.sh"
    rw_sh.write_text(RW_SH)
    proc = subprocess.run(
        ["bash", "-c", f'source "{rw_sh}"\necho x | rw_artifact "$NAME" -; echo "rc=$?"'],
        env={
            "PATH": "/usr/bin:/bin",
            "NAME": name,
            "RW_OUTPUT_FD": "2",
            "RW_ARTIFACTS_DIR": str(tmp_path / "artifacts"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert "rc=1" in proc.stdout
    assert "invalid artifact name" in proc.stderr
    assert not (tmp_path / "artifacts").exists() or list((tmp_path / "artifacts").iterdir()) == []
    assert not (tmp_path / "up").exists()


_NAME_CASES = ["dump", "report.txt", "a-b_c", "v2.tar.gz", "_x", "0day"] + [
    "my report.txt",
    "dump:v2",
    "-x",
    "a/b",
    ".hidden",
    "..",
    "../up",
    "/abs",
    "a/",
    "a//b",
    "a\nb",
    "a'b",
    'a"b',
    "$HOME",
    "caf\u00e9",
]


@pytest.mark.parametrize("name", _NAME_CASES)
def test_rw_artifact_accepts_exactly_the_names_the_manifest_does(tmp_path, name):
    """One rule: custom.manifest.is_valid_artifact_name, written out again in bash."""
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    rw_sh = tmp_path / "rw.sh"
    rw_sh.write_text(RW_SH)
    proc = subprocess.run(
        ["bash", "-c", f'source "{rw_sh}"\necho x | rw_artifact "$NAME" -'],
        env={
            "PATH": "/usr/bin:/bin",
            "NAME": name,
            "RW_OUTPUT_FD": "2",
            "RW_ARTIFACTS_DIR": str(tmp_path / "artifacts"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (proc.returncode == 0) == is_valid_artifact_name(name), proc.stderr


def test_rw_artifact_without_an_artifacts_dir_fails_clearly(tmp_path):
    rw_sh = tmp_path / "rw.sh"
    rw_sh.write_text(RW_SH)
    proc = subprocess.run(
        ["bash", "-c", f'source "{rw_sh}"\necho x | rw_artifact dump -; echo "rc=$?"'],
        env={"PATH": "/usr/bin:/bin", "RW_OUTPUT_FD": "2"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert "rc=1" in proc.stdout
    assert "RW_ARTIFACTS_DIR" in proc.stderr
