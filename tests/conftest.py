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
