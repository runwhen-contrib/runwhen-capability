"""Puts tests/ on sys.path so `from bundle_fixtures import load_bundle` works
from any test module, regardless of how deep under tests/ it lives."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
