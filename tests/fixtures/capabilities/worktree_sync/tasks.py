"""Fixture capability: mimics rw-worktree's open(setup)+read(task) shape,
for exercising host.run_request's setup-output caching -- a synchronous
call is a request carrying `setup` but no credentials, against a scope
whose setup already materialised in an earlier (credentialed) request.

`open` requires a declared credential named "repo", exactly like
rw-worktree's real `open` -- a request
with no credentials can only succeed here via a cache hit. It writes a
real file under the scope so `read` has real content to return, without
needing an actual git checkout.

`grep` routes through the real ctx.repo_fs.grep (repo_fs.py), like
rw-worktree's own `grep` task, so tests can exercise the real
TreeNotMaterializedError path and a real zero-match result -- not a
fixture-only stand-in. `wreck` is test-only: it deletes the materialised
tree from under a request, standing in for an eviction/reaped scope that
happens mid-request rather than waiting on the pod itself to be reaped."""

from __future__ import annotations

import shutil
from pathlib import Path

from runwhen_capability import Context, setup, task


@setup(outputs=["tree"])
def open(ctx: Context, repo_url: str, sha: str):
    ctx.credential("repo")  # hard failure if unresolved -- see Context.credential()
    tree = ctx.workdir / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "hello.txt").write_text(f"hello from {repo_url}@{sha}")
    return {"tree": tree}


@task(outputs={"content": "text"})
def read(ctx: Context, tree: Path):
    return {"content": (tree / "hello.txt").read_text()}


@task(outputs={"result": "rw.repo_grep.v1"})
def grep(ctx: Context, tree: Path, pattern: str):
    result = ctx.repo_fs.grep(tree, pattern)
    return {"result": result}


@task(outputs={})
def wreck(ctx: Context, tree: Path):
    """Test-only: deletes the materialised tree so a later task in the
    same request hits TreeNotMaterializedError, simulating eviction
    happening between setup and a read without needing a second process."""
    shutil.rmtree(tree)
    return {}
