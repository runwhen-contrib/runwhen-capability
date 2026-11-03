"""A local bare git repository holding the kubernetes fixture plugin at three tagged
versions, for the `rwtask pull` tests (no network)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from _plugin_fixtures import KUBE_MINI

VERSIONS = ("0.1.0", "0.2.0", "0.3.0-rc.1")
_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(*args: str, cwd: Path) -> str:
    env = {**os.environ, **_ENV}
    out = subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def write_version(dest: Path, version: str) -> None:
    """The kube-mini fixture at `dest`, as version `version`, plus a root `tests/` dir and a
    dotfile that an archive leaves out."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(KUBE_MINI, dest)
    plugin = dest / "plugin.yaml"
    plugin.write_text(plugin.read_text().replace("version: 0.1.0", f"version: {version}"))
    (dest / "tests").mkdir()
    (dest / "tests" / "note.txt").write_text("not published\n")
    (dest / ".github").mkdir()
    (dest / ".github" / "ci.yml").write_text("x: 1\n")


def make_remote(base: Path, *, name: str = "kubernetes") -> str:
    """Create `base/remote.git` (and a work clone) with `plugins/<name>/` tagged
    `<name>/v<version>` for each of VERSIONS (0.2.0 as an annotated tag); the `file://` URL."""
    bare = base / "remote.git"
    work = base / "work"
    bare.mkdir(parents=True)
    work.mkdir()
    git("init", "-q", "--bare", cwd=bare)
    git("config", "uploadpack.allowFilter", "true", cwd=bare)
    git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=bare)
    git("init", "-q", "-b", "main", cwd=work)
    git("remote", "add", "origin", str(bare), cwd=work)
    for version in VERSIONS:
        write_version(work / "plugins" / name, version)
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", f"{name} {version}", cwd=work)
        tag = f"{name}/v{version}"
        if version == "0.2.0":
            git("tag", "-a", "-m", f"release {version}", tag, cwd=work)
        else:
            git("tag", tag, cwd=work)
    git("push", "-q", "origin", "main", "--tags", cwd=work)
    return f"file://{bare}"
