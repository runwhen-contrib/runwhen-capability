"""ctx.git -- checkout and the changed-files diff. Shallow-clones a single
commit via the git CLI and, when a base sha is available, computes the
changed-files diff against it.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT = 300  # seconds; generous for a shallow single-commit fetch


@dataclass
class _FileEntry:
    path: str
    status: str  # added|modified|removed|renamed
    previous_path: str = ""


@dataclass
class _Checkout:
    repo_url: str
    sha: str
    token: str | None


class GitClient:
    """`ctx.git` -- checkout() materialises a tree; changed_files() is the
    diff filter. Never takes a raw token: checkout()'s `credential` is a
    declared credential *name*, resolved through Context.credential()."""

    def __init__(self, ctx) -> None:
        self._ctx = ctx
        self._checkouts: dict[str, _Checkout] = {}

    def checkout(
        self, repo_url: str, sha: str, credential: str | None = None, optional: bool = False
    ) -> Path:
        """`credential` is a declared credential *name*, resolved through
        Context.credential() -- never a raw token. A name that cannot be
        resolved is a HARD failure by default: CredentialNotFoundError
        propagates, naming the credential. Degrading to an anonymous fetch
        is a broken-credential-pipeline bug hiding itself -- it succeeds
        silently against a public repo in testing and fails only later,
        against a real private customer repo, exactly where it matters.

        Pass `optional=True` (mirroring the manifest's
        `needs.credentials[].optional: true`) to degrade to anonymous when
        this specific credential is legitimately absent -- an explicit,
        deliberate opt-in at the call site, never automatic. Pass
        `credential=None` outright for a checkout that was never going to
        need one. This method adds no fallback beyond that: local dev's
        `--allow-anonymous` escape hatch lives in Context.credential()
        itself, not here."""
        token = None
        if credential:
            token = self._ctx.credential(credential, optional=optional)

        dest = self._ctx.workdir / "tree"
        dest.mkdir(parents=True, exist_ok=True)

        self._run_git(dest, ["init"])
        self._run_git(dest, ["fetch", "--depth", "1", repo_url, sha], token=token)
        self._run_git(dest, ["checkout", "FETCH_HEAD"])

        self._checkouts[str(dest)] = _Checkout(repo_url=repo_url, sha=sha, token=token)
        return dest

    def changed_files(self, tree: Path, base_sha: str) -> list[str] | None:
        """The diff filter: returns repo-relative paths changed between
        base_sha and the sha checked out at `tree`, excluding removed files.
        Returns None ("absent => no diff filter") when
        either commit is not reachable or the diff cannot be determined --
        the caller decides what "no filter" means, this just reports it."""
        info = self._checkouts.get(str(Path(tree)))
        if info is None:
            raise RuntimeError(
                f"changed_files: {tree} was not produced by this Context's git.checkout()"
            )

        # Best-effort: bring the base commit's objects into the local repo.
        self._run_git(
            tree, ["fetch", "--depth", "1", info.repo_url, base_sha], token=info.token, check=False
        )

        if not self._commit_present(tree, info.sha) or not self._commit_present(tree, base_sha):
            return None

        try:
            entries = self._diff_name_status(tree, base_sha, info.sha)
        except RuntimeError:
            return None

        return [e.path for e in entries if e.status != "removed"]

    # -- internals ------------------------------------------------------

    def _run_git(self, cwd: Path, args: list[str], token: str | None = None, check: bool = True):
        git_args: list[str] = []
        if token:
            header = (
                "AUTHORIZATION: basic "
                + base64.b64encode(f"x-access-token:{token}".encode()).decode()
            )
            git_args += ["-c", f"http.extraheader={header}"]
        git_args += args

        proc = self._ctx.run(["git", *git_args], cwd=cwd, timeout=GIT_TIMEOUT)
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc

    def _commit_present(self, cwd: Path, sha: str) -> bool:
        proc = self._run_git(cwd, ["cat-file", "-e", f"{sha}^{{commit}}"], check=False)
        return proc.returncode == 0

    def _diff_name_status(self, cwd: Path, base: str, head: str) -> list[_FileEntry]:
        proc = self._run_git(cwd, ["diff", "--name-status", "-M", base, head], check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"git diff failed: {proc.stderr.strip()}")

        entries: list[_FileEntry] = []
        for line in proc.stdout.rstrip("\n").split("\n"):
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) < 2:
                continue
            code = fields[0]
            if code.startswith("A"):
                entries.append(_FileEntry(path=fields[1], status="added"))
            elif code.startswith("D"):
                entries.append(_FileEntry(path=fields[1], status="removed"))
            elif code.startswith("R"):
                if len(fields) < 3:
                    continue
                entries.append(
                    _FileEntry(path=fields[2], status="renamed", previous_path=fields[1])
                )
            else:  # M, C, T, ...
                entries.append(_FileEntry(path=fields[1], status="modified"))
        return entries
