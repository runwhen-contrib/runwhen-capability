"""`rwtask plan/apply/export/test` -- the GitOps client for custom capability
bundles, and the local test runner. See cli.py for argument handling; this
module holds no argparse.

A **capability folder** is any directory, found recursively under a given
root, that directly holds a `capability.yaml` -- the same bundle shape
`runwhen_capability.custom` and `rwtask run --local` already work with.

`plan`/`apply` speak to the RunWhen platform API:

    POST {api_url}/api/v4/workspaces/{workspace}/custom-capabilities:plan
        {"capabilities": [{"name", "files": [{"path", "content"}]}], "prune"}
        -> {"capabilities": [{"name", "action", "content_hash", "diagnostics", "files"}]}

    POST {api_url}/api/v4/workspaces/{workspace}/custom-capabilities:apply
        {"capabilities": [...], "prune", "message", "adopt"}
        -> {"capabilities": [...]}                # same per-capability shape as :plan

    GET {api_url}/api/v4/workspaces/{workspace}/custom-capabilities:export?names=a,b
        -> {"capabilities": [{"name", "managed_by", "version", "files"}]}

Every capability sent to `plan`/`apply` is validated locally first
(`runwhen_capability.custom.validate`) -- an invalid bundle never reaches the
network. Every error response uses the platform's `{"errors": [...]}` shape,
each entry the same `{code, file, line, path, message, hint}` a local
Diagnostic carries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import requests

from .custom import Diagnostic, validate
from .custom.manifest import Manifest
from .custom.yaml_lines import safe_load
from .run_local import _read_bundle_dir, run_local_bundle

DEFAULT_TIMEOUT = 30  # seconds, per plan/apply/export request


class GitOpsError(RuntimeError):
    """A plan/apply/export call could not be completed: a transport
    failure, a non-JSON or non-2xx response from the platform API.
    `diagnostics` carries the response's `errors` list, parsed as
    Diagnostic, when the platform API sent one; empty for a transport
    failure or a response with no such shape."""

    def __init__(self, message: str, diagnostics: list[Diagnostic] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or []


class LocalValidationError(RuntimeError):
    """plan()/apply() found at least one capability that fails
    runwhen_capability.custom.validate() before calling the platform API at
    all. `invalid` is (capability_dir, diagnostics) for every capability
    with at least one error-severity diagnostic."""

    def __init__(self, invalid: list[tuple[Path, list[Diagnostic]]]) -> None:
        self.invalid = invalid
        super().__init__(f"{len(invalid)} capability(ies) failed local validation")


@dataclass
class GitOpsConfig:
    api_url: str
    token: str
    workspace: str


def find_capability_dirs(root: Path) -> list[Path]:
    """Every directory at or under `root` that directly holds a
    capability.yaml, sorted by path -- plan/apply/export/test's shared
    notion of "a capability folder"."""
    root = Path(root)
    return sorted({path.parent for path in root.rglob("capability.yaml") if path.is_file()})


def _load_capabilities(dirs: list[Path]) -> list[tuple[str, Path, dict[str, str]]]:
    """Reads and locally validates every capability in `dirs`. Returns
    (name, dir, files), one entry per capability, in the order `dirs` came
    in. Raises LocalValidationError -- naming every offending capability
    and its diagnostics -- if any of them has an error-severity validate()
    diagnostic; the platform API is never called in that case."""
    capabilities = []
    invalid = []
    for capability_dir in dirs:
        files = _read_bundle_dir(capability_dir)
        diagnostics = validate(files)
        if any(d.severity == "error" for d in diagnostics):
            invalid.append((capability_dir, diagnostics))
            continue
        # validate() already proved capability.yaml parses and matches
        # Manifest -- re-parsing here for its `name` is cheap and keeps this
        # function self-contained, the same tradeoff compiler.py and
        # hashing.py make.
        manifest = Manifest.model_validate(safe_load(files["capability.yaml"]))
        capabilities.append((manifest.name, capability_dir, files))
    if invalid:
        raise LocalValidationError(invalid)
    return capabilities


def _capabilities_payload(capabilities: list[tuple[str, Path, dict[str, str]]]) -> list[dict]:
    return [
        {
            "name": name,
            "files": [
                {"path": path, "content": content} for path, content in sorted(files.items())
            ],
        }
        for name, _capability_dir, files in capabilities
    ]


def plan(cfg: GitOpsConfig, root: Path, prune: bool = False) -> list[dict]:
    """POSTs `:plan`; returns the response's `capabilities` list. Raises
    LocalValidationError or GitOpsError."""
    capabilities = _load_capabilities(find_capability_dirs(root))
    body = {"capabilities": _capabilities_payload(capabilities), "prune": prune}
    response = _post(cfg, "custom-capabilities:plan", body)
    return response.get("capabilities", [])


def apply(
    cfg: GitOpsConfig, root: Path, message: str, prune: bool = False, adopt: bool = False
) -> list[dict]:
    """POSTs `:apply`; returns the response's `capabilities` list, the same
    shape `plan()` returns. Raises LocalValidationError or GitOpsError.
    Re-reads and re-validates the capability tree independently of any
    earlier `plan()` call -- whether/when to plan first is the caller's
    call, not this function's."""
    capabilities = _load_capabilities(find_capability_dirs(root))
    body = {
        "capabilities": _capabilities_payload(capabilities),
        "prune": prune,
        "message": message,
        "adopt": adopt,
    }
    response = _post(cfg, "custom-capabilities:apply", body)
    return response.get("capabilities", [])


def export(cfg: GitOpsConfig, names: list[str] | None = None) -> list[dict]:
    """GETs `:export`; returns the response's `capabilities` list (`name`,
    `managed_by`, `version`, `files`). `names` narrows the export to those
    capabilities; omitted, every capability in the workspace comes back."""
    params = {"names": ",".join(names)} if names else None
    response = _get(cfg, "custom-capabilities:export", params=params)
    return response.get("capabilities", [])


def has_changes(capabilities: list[dict]) -> bool:
    return any(cap.get("action") != "unchanged" for cap in capabilities)


def conflicted(capabilities: list[dict]) -> list[str]:
    """The names of every capability a plan reported as `conflict` -- it
    exists with `managed_by != git`, so apply refuses it unless `adopt`."""
    return [cap["name"] for cap in capabilities if cap.get("action") == "conflict"]


# -- exporting to disk ---------------------------------------------------------


def export_conflicts(root: Path, capabilities: list[dict]) -> list[Path]:
    """Every local file under `root` that write_export() would overwrite
    with DIFFERENT content -- the check `export --force` exists to bypass.
    A file that does not exist locally yet, or is already byte-identical,
    is never a conflict."""
    conflicts = []
    for cap in capabilities:
        for entry in cap.get("files", []):
            target = root / cap["name"] / entry["path"]
            if not target.is_file():
                continue
            try:
                current = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                conflicts.append(target)
                continue
            if current != entry.get("content", ""):
                conflicts.append(target)
    return conflicts


def write_export(root: Path, capabilities: list[dict]) -> list[Path]:
    """Writes every capability's files under `root/<name>/`. Callers check
    export_conflicts() first unless `--force` was given -- this function
    itself always overwrites."""
    written = []
    for cap in capabilities:
        for entry in cap.get("files", []):
            target = root / cap["name"] / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(entry.get("content", ""), encoding="utf-8")
            written.append(target)
    return written


# -- running tests locally ------------------------------------------------------


@dataclass
class TaskTestResult:
    capability: str
    capability_dir: Path
    task: str
    outcome: str  # "pass" | "fail" | "no test"
    detail: str = ""


def run_tests(root: Path, task: str | None = None) -> list[TaskTestResult]:
    """For each capability under `root`, and each of its declared tasks
    that has a `tests/<task>.json` (only the one named `task`, when
    given): runs it through run_local_bundle() -- the same code path
    `rwtask run --local` uses -- with that file's `inputs`/`target`, and
    compares the resulting status to `expect.status` (default "ok").

    Outputs are checked as a side effect of that run: run_bundle_request()
    already fails a task whose outputs do not validate against their
    declared schema, so a status match here already means the outputs
    validated too.

    Secrets/credentials are never part of a test file's shape, so every
    run here is `allow_anonymous`: a task whose credential input goes
    unresolved simply runs without it, rather than failing before it
    starts (see run_local.run_local_bundle's `allow_anonymous`)."""
    results = []
    for capability_dir in find_capability_dirs(root):
        files = _read_bundle_dir(capability_dir)
        if "capability.yaml" not in files:
            continue
        try:
            manifest = Manifest.model_validate(safe_load(files["capability.yaml"]))
        except Exception:  # noqa: BLE001 -- plan/apply's own validate() reports this; not our job
            continue
        for task_spec in manifest.tasks:
            if task is not None and task_spec.name != task:
                continue
            results.append(_run_one_test(capability_dir, manifest.name, task_spec.name, files))
    return results


def _run_one_test(
    capability_dir: Path, capability: str, task: str, files: dict[str, str]
) -> TaskTestResult:
    test_path = f"tests/{task}.json"
    if test_path not in files:
        return TaskTestResult(capability, capability_dir, task, "no test")

    try:
        spec = json.loads(files[test_path])
    except ValueError as exc:
        return TaskTestResult(capability, capability_dir, task, "fail", f"{test_path}: {exc}")

    expected_status = (spec.get("expect") or {}).get("status") or "ok"
    result = run_local_bundle(
        bundle_dir=capability_dir,
        tasks=[task],
        inputs=spec.get("inputs"),
        target=spec.get("target"),
        allow_anonymous=True,
    )
    task_result = result.tasks[0]
    if task_result.status == expected_status:
        return TaskTestResult(capability, capability_dir, task, "pass")

    detail = f"expected status {expected_status!r}, got {task_result.status!r}"
    reason = task_result.error or task_result.reason
    if reason:
        detail += f": {reason}"
    return TaskTestResult(capability, capability_dir, task, "fail", detail)


# -- the platform API client -----------------------------------------------------


def format_diagnostic(
    code: str, message: str, file: str | None = None, line: int | None = None
) -> str:
    """`file:line code message` -- the format both plan/apply's local
    pre-check and the platform API's own error responses print in."""
    if file and line:
        location = f"{file}:{line} "
    elif file:
        location = f"{file} "
    else:
        location = ""
    return f"{location}{code} {message}"


def _headers(cfg: GitOpsConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {cfg.token}"}


def _url(cfg: GitOpsConfig, path: str) -> str:
    workspace = quote(cfg.workspace, safe="")
    return f"{cfg.api_url.rstrip('/')}/api/v4/workspaces/{workspace}/{path}"


def _post(cfg: GitOpsConfig, path: str, body: dict) -> dict:
    try:
        resp = requests.post(
            _url(cfg, path), json=body, headers=_headers(cfg), timeout=DEFAULT_TIMEOUT
        )
    except requests.RequestException as exc:
        raise GitOpsError(f"{path}: {exc}") from exc
    return _parse_response(path, resp)


def _get(cfg: GitOpsConfig, path: str, params: dict | None = None) -> dict:
    try:
        resp = requests.get(
            _url(cfg, path), params=params, headers=_headers(cfg), timeout=DEFAULT_TIMEOUT
        )
    except requests.RequestException as exc:
        raise GitOpsError(f"{path}: {exc}") from exc
    return _parse_response(path, resp)


def _parse_response(path: str, resp: requests.Response) -> dict:
    if resp.status_code >= 400:
        diagnostics = _error_diagnostics(resp)
        if diagnostics:
            message = "; ".join(
                format_diagnostic(d.code, d.message, d.file, d.line) for d in diagnostics
            )
        else:
            message = f"HTTP {resp.status_code}: {resp.text[:500]}"
        raise GitOpsError(f"{path}: {message}", diagnostics)
    try:
        return resp.json()
    except ValueError as exc:
        raise GitOpsError(f"{path}: response was not JSON: {exc}") from exc


def _error_diagnostics(resp: requests.Response) -> list[Diagnostic]:
    try:
        body = resp.json()
    except ValueError:
        return []
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return []
    diagnostics = []
    for entry in errors:
        if not isinstance(entry, dict):
            continue
        try:
            diagnostics.append(Diagnostic.model_validate(entry))
        except Exception:  # noqa: BLE001 -- one malformed error entry must not hide the others
            continue
    return diagnostics
