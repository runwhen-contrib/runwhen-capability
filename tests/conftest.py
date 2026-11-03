"""Puts tests/ on sys.path so `from bundle_fixtures import load_bundle` works
from any test module, regardless of how deep under tests/ it lives -- and
defines the `relay_server` fixture (see relay_server.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def relay_server():
    """A real local HTTP server standing in for the runner's relay: records
    every request, answers from a script. See tests/relay_server.py."""
    from relay_server import RelayServer

    with RelayServer() as server:
        yield server


@pytest.fixture(autouse=True)
def _serve_does_not_make_the_test_process_non_dumpable(monkeypatch):
    """`serve()` calls prctl(PR_SET_DUMPABLE, 0) on its own process (see serve.py); in a
    test that would be the pytest process. tests/test_executor_token.py exercises the real
    function (with a mocked libc) and a real `rwtask serve` subprocess."""
    import runwhen_capability.serve as serve_mod

    monkeypatch.setattr(serve_mod, "_make_non_dumpable", lambda *args, **kwargs: False)
