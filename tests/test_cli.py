"""`rwtask` CLI argument handling.

Covers the shape the runner actually launches executors with, which no other
test exercised: the image's CMD carries only --capability-dir and everything
else arrives as environment variables.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

BUNDLES = Path(__file__).parent / "fixtures" / "bundles"


def test_serve_takes_relay_and_pool_from_env_when_no_flags_are_passed(monkeypatch):
    """The runner launches the image with NO args -- it injects RELAY_URL and
    POOL_ID as env vars, because a runner that passed capability CLI flags would
    be encoding knowledge of the capability. The image's CMD supplies only
    --capability-dir, so `rwtask serve --capability-dir X` must work on its own.

    Regression: it did not, and every executor pod CrashLooped with
    `error: the following arguments are required: --relay, --pool`.
    """
    seen = {}

    def _fake_serve(**kwargs):
        seen.update(kwargs)

    monkeypatch.setenv("RELAY_URL", "http://runner-relay:8000")
    monkeypatch.setenv("POOL_ID", "ghcr.io/example/cap@sha256:abc")
    monkeypatch.setattr("runwhen_capability.serve.serve", _fake_serve)

    from runwhen_capability.cli import main

    assert main(["serve", "--capability-dir", "capabilities/example"]) == 0
    assert seen["relay"] == "http://runner-relay:8000"
    assert seen["pool_id"] == "ghcr.io/example/cap@sha256:abc"


def test_serve_errors_clearly_when_neither_flag_nor_env_is_present(monkeypatch, capsys):
    monkeypatch.delenv("RELAY_URL", raising=False)
    monkeypatch.delenv("POOL_ID", raising=False)

    from runwhen_capability.cli import main

    with pytest.raises(SystemExit):
        main(["serve", "--capability-dir", "capabilities/example"])
    assert "RELAY_URL" in capsys.readouterr().err


def test_version_prints_the_package_version(capsys):
    from runwhen_capability import __version__
    from runwhen_capability.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"rwtask {__version__}"


def test_run_local_runs_a_bundle_task_and_prints_its_result(capsys):
    from runwhen_capability.cli import main

    exit_code = main(
        [
            "run",
            "--local",
            str(BUNDLES / "pgbouncer-health"),
            "--task",
            "pool-errors",
            "--inputs",
            json.dumps({"since": "45m", "maxWait": 5}),
            "--target",
            json.dumps({"urn": "urn:resource:pgbouncer"}),
            "--credentials",
            _credentials_file({"kubeconfig": "kubeconfig-value"}),
        ]
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    [task] = result["tasks"]
    assert task["task"] == "pool-errors"
    assert task["status"] == "ok"
    assert task["outputs"]["summary"] == {"windowMinutes": 30, "pods": 2, "total": 3}


def test_run_local_needs_at_least_one_task(capsys):
    from runwhen_capability.cli import main

    with pytest.raises(SystemExit):
        main(["run", "--local", str(BUNDLES / "pgbouncer-health")])
    assert "--task" in capsys.readouterr().err


def test_run_without_local_still_needs_capability_dir_and_request(capsys):
    from runwhen_capability.cli import main

    with pytest.raises(SystemExit):
        main(["run"])
    assert "capability_dir" in capsys.readouterr().err


def _credentials_file(credentials: dict) -> str:
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".json")
    with open(fd, "w") as f:
        json.dump(credentials, f)
    return path
