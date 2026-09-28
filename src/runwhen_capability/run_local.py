"""`rwtask run <capability-dir> --request request.json [--credentials creds.json]
[--allow-anonymous]`

The reference implementation: the same code path as `rwtask serve`
(host.run_request), against the local filesystem, with credentials from a
local file. A capability author needs no cluster to develop against this SDK.

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

from .host import run_request
from .loader import load_capability
from .models import RequestEnvelope, ResultEnvelope


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
