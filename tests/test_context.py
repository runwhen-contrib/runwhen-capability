"""ctx.run's environment merge -- the default (inherit the parent process's
environment) and `inherit_env=False` (an exact allow-list, no `os.environ`
in the child at all).
"""

from __future__ import annotations

import sys

from runwhen_capability import Context

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
