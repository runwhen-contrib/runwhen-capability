"""`rwtask` CLI argument handling.

Covers the shape the runner actually launches executors with, which no other
test exercised: the image's CMD carries only --capability-dir and everything
else arrives as environment variables.
"""

from __future__ import annotations

import pytest


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
