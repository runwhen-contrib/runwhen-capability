"""Shared by tests/upconvert/test_upconvert_*.py."""

from __future__ import annotations

from pathlib import Path

from runwhen_capability.contracts import load_yaml
from runwhen_capability.plugin import load_skill_dir, validate_tree
from runwhen_capability.upconvert import UpconvertResult, write_result

TESTS = Path(__file__).resolve().parents[1]
BUNDLES = TESTS / "fixtures" / "bundles"
CAPABILITIES = TESTS / "fixtures" / "capabilities"


def validation_errors(result: UpconvertResult, tmp_path: Path) -> list:
    """Write `result` into tmp_path/<skill_name>/, load it as a workspace skill and
    return the error-severity findings of validate_tree (warnings are allowed)."""
    dest = tmp_path / "validated" / result.skill_name
    write_result(result, dest)
    return [d for d in validate_tree(load_skill_dir(dest)) if d.severity == "error"]


def tasks_doc(result: UpconvertResult) -> dict:
    return load_yaml(result.files["tasks.yaml"].decode())


def codes(result: UpconvertResult) -> list[str]:
    return [d.code for d in result.diagnostics]


def with_code(result: UpconvertResult, code: str) -> list:
    return [d for d in result.diagnostics if d.code == code]


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return root
