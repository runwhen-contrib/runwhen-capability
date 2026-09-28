"""`rwtask run <capability-dir> --request request.json [--credentials creds.json]
[--allow-anonymous]`, and its bundle-mode counterpart
`rwtask run --local <bundle-dir> --task <name> --inputs '<json>'`.

Both are reference implementations: the same code path as `rwtask serve`
(host.run_request / bundle.run_bundle_request), against the local
filesystem, with credentials from a local file. A capability author needs no
cluster, and a custom capability author needs no papi and no runner, to
develop against this SDK.

`allow_anonymous` (CLI: `--allow-anonymous`) is `rwtask run`'s own escape
hatch for local dev against public repos without a credentials.json: it
degrades every unresolved credential to anonymous instead of failing the
request. It exists only here -- `rwtask serve` never sets it -- because
degrading a REQUIRED credential must be a deliberate local-dev act, never
something reachable from a running pod. See Context.credential().
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .bundle import run_bundle_request
from .custom.hashing import content_hash
from .custom.manifest import is_allowed_path
from .host import run_request
from .loader import load_capability
from .models import (
    Bundle,
    BundleFile,
    BundleRequestEnvelope,
    BundleTarget,
    RequestEnvelope,
    ResultEnvelope,
)


def run_local(
    capability_dir: Path,
    request_path: Path,
    credentials_path: Path | None = None,
    workdir: Path | None = None,
    keep_workdir: bool = False,
    log: logging.Logger | None = None,
    allow_anonymous: bool = False,
) -> ResultEnvelope:
    log = log or logging.getLogger("runwhen_capability.run")
    capability = load_capability(Path(capability_dir))

    request = RequestEnvelope.model_validate_json(Path(request_path).read_text())
    credentials: dict[str, str] = {}
    if credentials_path:
        credentials = json.loads(Path(credentials_path).read_text())

    owns_workdir = workdir is None
    scope_dir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="rwtask-run-"))
    scope_dir.mkdir(parents=True, exist_ok=True)

    try:
        return run_request(
            capability,
            request,
            credentials,
            scope_dir,
            log=log,
            allow_anonymous_credentials=allow_anonymous,
        )
    finally:
        if owns_workdir and not keep_workdir:
            shutil.rmtree(scope_dir, ignore_errors=True)


def _read_bundle_dir(bundle_dir: Path) -> dict[str, str]:
    """Every file under `bundle_dir` that sits on an allowed bundle path
    (custom.manifest.is_allowed_path), read as UTF-8 text and keyed by its
    bundle_dir-relative, forward-slash path -- exactly the `files` shape
    content_hash()/compile_manifest() take. Anything else in the directory
    (an editor swap file, a stray `.git/`) is silently not part of the
    bundle, same as it would not be part of a real upload."""
    files = {}
    for path in sorted(bundle_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(bundle_dir).as_posix()
        if is_allowed_path(rel):
            files[rel] = path.read_text(encoding="utf-8")
    return files


def run_local_bundle(
    bundle_dir: Path,
    tasks: list[str],
    inputs: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
    credentials_path: Path | None = None,
    workdir: Path | None = None,
    keep_workdir: bool = False,
    log: logging.Logger | None = None,
    allow_anonymous: bool = False,
) -> ResultEnvelope:
    """`rwtask run --local <bundle-dir> --task <name> --inputs '<json>'`: the
    same code path bundle.py's run_bundle_request() is (materialise, verify
    hash, compile, run) -- except the bundle is read straight off local
    disk, and its hash is computed from those same files, so the
    verification step that guards against a tampered-in-transit bundle is
    always a trivial pass here; nothing else about the path differs."""
    log = log or logging.getLogger("runwhen_capability.run")
    bundle_dir = Path(bundle_dir)
    files = _read_bundle_dir(bundle_dir)

    credentials: dict[str, str] = {}
    if credentials_path:
        credentials = json.loads(Path(credentials_path).read_text())

    request = BundleRequestEnvelope(
        bundle=Bundle(
            hash=content_hash(files),
            files=[BundleFile(path=path, content=text) for path, text in files.items()],
        ),
        tasks=tasks,
        inputs=inputs or {},
        target=BundleTarget.model_validate(target) if target else None,
    )

    owns_workdir = workdir is None
    scope_dir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="rwtask-run-"))
    scope_dir.mkdir(parents=True, exist_ok=True)

    try:
        return run_bundle_request(
            request,
            credentials,
            scope_dir,
            log=log,
            allow_anonymous_credentials=allow_anonymous,
        )
    finally:
        if owns_workdir and not keep_workdir:
            shutil.rmtree(scope_dir, ignore_errors=True)
