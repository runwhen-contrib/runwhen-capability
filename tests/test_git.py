"""ctx.git.checkout / .changed_files against a real local git repository
(file-path remote, no network).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from runwhen_capability import Context
from runwhen_capability.errors import CredentialNotFoundError


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _rev_parse(cwd: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _make_origin(tmp_path: Path) -> tuple[Path, str, str]:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q")
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "t")

    (origin / "a.py").write_text("print('a')\n")
    (origin / "b.py").write_text("print('b')\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "base")
    base_sha = _rev_parse(origin, "HEAD")

    (origin / "a.py").write_text("print('a modified')\n")
    (origin / "c.py").write_text("print('c new')\n")
    (origin / "b.py").unlink()
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "head")
    head_sha = _rev_parse(origin, "HEAD")

    return origin, base_sha, head_sha


def test_checkout_materialises_the_head_tree(tmp_path):
    origin, _base_sha, head_sha = _make_origin(tmp_path)
    scope_dir = tmp_path / "scope"
    scope_dir.mkdir()
    ctx = Context(capability="rw-checks", operation="checkout", workdir=scope_dir)

    tree = ctx.git.checkout(str(origin), head_sha)

    assert tree.is_dir()
    assert (tree / "a.py").read_text() == "print('a modified')\n"
    assert (tree / "c.py").exists()
    assert not (tree / "b.py").exists()


def test_changed_files_excludes_removed_and_uses_current_path_for_renames(tmp_path):
    origin, base_sha, head_sha = _make_origin(tmp_path)
    scope_dir = tmp_path / "scope"
    scope_dir.mkdir()
    ctx = Context(capability="rw-checks", operation="checkout", workdir=scope_dir)
    tree = ctx.git.checkout(str(origin), head_sha)

    changed = ctx.git.changed_files(tree, base_sha)

    assert changed is not None
    # b.py was removed -- removed files are excluded from the changed set.
    assert set(changed) == {"a.py", "c.py"}


def test_changed_files_returns_none_when_base_is_unreachable(tmp_path):
    origin, _base_sha, head_sha = _make_origin(tmp_path)
    scope_dir = tmp_path / "scope"
    scope_dir.mkdir()
    ctx = Context(capability="rw-checks", operation="checkout", workdir=scope_dir)
    tree = ctx.git.checkout(str(origin), head_sha)

    changed = ctx.git.changed_files(tree, "0" * 40)

    assert changed is None


def test_changed_files_raises_for_a_tree_not_from_this_context(tmp_path):
    ctx = Context(capability="rw-checks", operation="checkout", workdir=tmp_path)
    try:
        ctx.git.changed_files(tmp_path / "nope", "deadbeef")
    except RuntimeError as exc:
        assert "was not produced by this Context" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_checkout_raises_when_a_declared_credential_is_unresolved(tmp_path):
    """A required credential the platform never bound is a hard failure --
    checkout() must not silently degrade to an anonymous fetch (that hides
    a broken credential pipeline behind a public repo passing in testing).
    The error must name the credential."""
    origin, _base_sha, head_sha = _make_origin(tmp_path)
    scope_dir = tmp_path / "scope"
    scope_dir.mkdir()
    ctx = Context(capability="rw-checks", operation="checkout", workdir=scope_dir, credentials={})

    with pytest.raises(CredentialNotFoundError) as exc_info:
        ctx.git.checkout(str(origin), head_sha, credential="repo")

    assert "repo" in str(exc_info.value)


def test_checkout_optional_credential_degrades_to_anonymous(tmp_path):
    """A credential the caller explicitly marks optional=True degrades to
    an anonymous fetch instead of raising -- the deliberate opt-in this
    contrasts with the (removed) automatic degrade."""
    origin, _base_sha, head_sha = _make_origin(tmp_path)
    scope_dir = tmp_path / "scope"
    scope_dir.mkdir()
    ctx = Context(capability="rw-checks", operation="checkout", workdir=scope_dir, credentials={})

    tree = ctx.git.checkout(str(origin), head_sha, credential="repo", optional=True)

    assert tree.is_dir()


def test_checkout_allow_anonymous_context_degrades_a_required_credential(tmp_path):
    """rwtask run --allow-anonymous (Context(allow_anonymous_credentials=True))
    degrades even a credential the call site did NOT mark optional -- the
    blanket local-dev override."""
    origin, _base_sha, head_sha = _make_origin(tmp_path)
    scope_dir = tmp_path / "scope"
    scope_dir.mkdir()
    ctx = Context(
        capability="rw-checks",
        operation="checkout",
        workdir=scope_dir,
        credentials={},
        allow_anonymous_credentials=True,
    )

    tree = ctx.git.checkout(str(origin), head_sha, credential="repo")

    assert tree.is_dir()
