"""ctx.run's environment merge -- the default (inherit the parent process's
environment) and `inherit_env=False` (an exact allow-list, no `os.environ`
in the child at all).
"""

from __future__ import annotations

import sys

import pytest

from runwhen_capability import Context
from runwhen_capability.models import InputArtifactDecl, OutputDecl

# Interpreter startup itself may add a variable or two to its OWN environ
# (e.g. CPython's locale coercion, or a macOS CoreFoundation runtime var) --
# an artifact of the fresh process, not a leak of the parent's env. So the
# assertion below checks the one thing `inherit_env=False` actually
# promises: the passed mapping arrives, and a parent-only variable does not.
_CHECK_SCRIPT = "import os; print(os.environ.get('ONLY_ME', ''), 'RW_CHECKS_SECRET' in os.environ)"


def test_run_inherit_env_false_gives_the_child_exactly_the_passed_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("RW_CHECKS_SECRET", "leaked-if-inherited")
    ctx = Context(capability="rw-checks", operation="test", workdir=tmp_path)

    proc = ctx.run([sys.executable, "-c", _CHECK_SCRIPT], env={"ONLY_ME": "1"}, inherit_env=False)

    assert proc.stdout.strip() == "1 False"


def test_run_default_still_inherits_the_parent_environment(tmp_path, monkeypatch):
    """Most ctx.run callers rely on this -- inherit_env's default must stay
    True."""
    monkeypatch.setenv("RW_CHECKS_MARKER", "present")
    ctx = Context(capability="rw-worktree", operation="test", workdir=tmp_path)

    proc = ctx.run(
        [sys.executable, "-c", "import os; print(os.environ.get('RW_CHECKS_MARKER', ''))"],
        env={"EXTRA": "1"},
    )

    assert proc.stdout.strip() == "present"


def test_storage_put_points_to_artifacts(tmp_path):
    ctx = Context(capability="rw-checks", operation="test", workdir=tmp_path)

    with pytest.raises(NotImplementedError, match=r"use ctx\.artifacts"):
        ctx.storage.put("report.txt", b"data")


def test_context_artifact_wiring_defaults_to_none(tmp_path):
    ctx = Context(capability="rw-checks", operation="test", workdir=tmp_path)

    assert ctx.relay is None
    assert ctx.task is None
    assert ctx.outputs == []
    assert ctx.input_artifacts == []
    assert ctx.artifacts is not None


def test_context_carries_the_relay_task_outputs_and_input_artifacts(tmp_path):
    relay = object()
    outputs = [OutputDecl(task="scan", name="report.txt", artifact=True)]
    inputs = [InputArtifactDecl(input="prior", sizeBytes=3, sha256="ab" * 32)]

    ctx = Context(
        capability="rw-checks",
        operation="scan",
        workdir=tmp_path,
        relay=relay,
        task="scan",
        outputs=outputs,
        input_artifacts=inputs,
    )

    assert ctx.relay is relay
    assert ctx.task == "scan"
    assert ctx.outputs == outputs
    assert ctx.input_artifacts == inputs
