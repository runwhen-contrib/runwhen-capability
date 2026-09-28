"""The task host's core engine: run one RequestEnvelope's setup then tasks
against a capability's registered functions, in manifest/request order,
resolving `${setup.<name>}` references, and aggregate into a ResultEnvelope.

Shared by both `rwtask serve` (serve.py) and `rwtask run` (the local
reference implementation) -- the same code path.

Never raises out of run_request(): a setup/task exception becomes a
failed setup/task entry in the result, and the rest of the request still
runs to completion.

Setup-output caching: for a stateful capability the pod is warm and sticky
precisely so a materialised tree persists across requests. A request's
setup outputs are cached inside `scope_dir` -- keyed by the scope the
caller already gave us, never a new identifier -- so a later request
against the same scope (a synchronous call: setup present, no credentials)
reuses them instead of re-running setup. The cache lives on disk, not in
this process's memory, so it survives across separate `rwtask run`/`rwtask
serve` invocations the same way the scope itself does, and it dies when
the scope does (eviction, a fresh pod).
See _load_setup_cache/_save_setup_cache below.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .context import Context
from .errors import CredentialNotFoundError, UnknownTaskError
from .loader import LoadedCapability
from .models import RequestEnvelope, ResultEnvelope, SetupResult, TaskResult
from .repo_fs import TreeNotMaterializedError

_PLACEHOLDER_RE = re.compile(r"^\$\{setup\.(\w+)\}$")
_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")

# The setup-output cache file's name within scope_dir. Dotfile, so it
# never collides with anything a setup task materialises there (e.g.
# rw-worktree's `open` writes scope_dir/tree).
_SETUP_CACHE_FILENAME = ".rwtask-setup-cache.json"


def _camel_to_snake(name: str) -> str:
    return _CAMEL_RE.sub("_", name).lower()


def _kwargs_from_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    return {_camel_to_snake(k): v for k, v in inputs.items()}


def _resolve_placeholders(inputs: dict[str, Any], setup_outputs: dict[str, Any]) -> dict[str, Any]:
    resolved = {}
    for key, value in inputs.items():
        if isinstance(value, str):
            m = _PLACEHOLDER_RE.match(value)
            if m:
                name = m.group(1)
                if name not in setup_outputs:
                    raise UnknownTaskError(
                        f"input {key!r} references unknown setup output '${{setup.{name}}}'"
                    )
                resolved[key] = setup_outputs[name]
                continue
        resolved[key] = value
    return resolved


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):  # pydantic model, e.g. Finding
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def _setup_cache_path(scope_dir: Path) -> Path:
    return scope_dir / _SETUP_CACHE_FILENAME


def _load_setup_cache(scope_dir: Path) -> dict[str, Any] | None:
    """A missing or unreadable/malformed cache file is an ordinary cache
    miss, never an error -- the fresh-pod and evicted-pod cases must look
    identical to a first request."""
    try:
        return json.loads(_setup_cache_path(scope_dir).read_text())
    except (OSError, ValueError):
        return None


def _save_setup_cache(
    scope_dir: Path,
    task: str,
    inputs: dict[str, Any],
    outputs: dict[str, Any],
    log: logging.Logger,
) -> None:
    """Best-effort: a failure here just means the next request re-runs setup
    instead of hitting the cache, never a request failure. TypeError/ValueError
    are caught alongside OSError because json.dumps raises them for an output
    _to_jsonable does not know how to convert (a set, a datetime): letting one
    escape would abort a setup that had already SUCCEEDED, replacing its `ok`
    result and its real outputs with `failed` -- reporting a materialised tree
    as a broken checkout, which is precisely the class of lie this module
    exists to prevent."""
    path_keys = [key for key, value in outputs.items() if isinstance(value, Path)]
    payload = {
        "task": task,
        "inputs": _to_jsonable(inputs),
        "outputs": _to_jsonable(outputs),
        "pathKeys": path_keys,  # so a Path-valued output round-trips as Path, not str
    }
    try:
        _setup_cache_path(scope_dir).write_text(json.dumps(payload))
    except (OSError, TypeError, ValueError) as exc:
        # Swallowed, but never silent: the next request paying for a full
        # re-run of setup should be explainable from the logs.
        log.warning("could not cache setup %r outputs in %s: %s", task, scope_dir, exc)


def _cached_setup_outputs(
    cache: dict[str, Any] | None, task: str, inputs: dict[str, Any]
) -> dict[str, Any] | None:
    """None unless `cache` was saved for this exact (task, inputs) AND
    every path-typed output still exists on disk -- validated, not just
    trusted, so a cache for a different (repoUrl, sha) is a miss rather
    than plausible content from the wrong commit, and so is a cache whose
    materialised tree is gone (pod replaced, scope reaped, fresh volume).
    Every key in `pathKeys` is checked, not just one named "tree" -- a
    `cached` result pointing at a vanished path is exactly the state that
    let repo_fs report a false "no matches" in production."""
    if cache is None:
        return None
    if cache.get("task") != task or cache.get("inputs") != _to_jsonable(inputs):
        return None
    outputs = dict(cache.get("outputs", {}))
    path_keys = cache.get("pathKeys", [])
    for key in path_keys:
        if key in outputs:
            outputs[key] = Path(outputs[key])
    for key in path_keys:
        value = outputs.get(key)
        if isinstance(value, Path) and not value.exists():
            return None
    return outputs


def run_request(
    capability: LoadedCapability,
    request: RequestEnvelope,
    credentials: dict[str, str],
    scope_dir: Path,
    log: logging.Logger | None = None,
    allow_anonymous_credentials: bool = False,
) -> ResultEnvelope:
    log = log or logging.getLogger("runwhen_capability.host")
    result = ResultEnvelope()
    setup_outputs: dict[str, Any] = {}

    if request.setup is not None:
        setup_def = capability.registry.setups.get(request.setup.task)
        if setup_def is None:
            result.setup = SetupResult(
                status="failed", error=f"unknown setup task {request.setup.task!r}"
            )
        else:
            cached_outputs = _cached_setup_outputs(
                _load_setup_cache(scope_dir), request.setup.task, request.setup.inputs
            )
            if cached_outputs is not None:
                # A later request against this same scope (a synchronous
                # call) -- setup already materialised here; reuse it and
                # do not touch credentials at all.
                setup_outputs = cached_outputs
                result.setup = SetupResult(status="cached", outputs=_to_jsonable(cached_outputs))
            else:
                ctx = Context(
                    capability=capability.capability_id,
                    operation=request.setup.task,
                    workdir=scope_dir,
                    credentials=credentials,
                    log=log.getChild(request.setup.task),
                    allow_anonymous_credentials=allow_anonymous_credentials,
                )
                try:
                    kwargs = _kwargs_from_inputs(request.setup.inputs)
                    outputs = setup_def.func(ctx, **kwargs) or {}
                    setup_outputs = outputs
                    result.setup = SetupResult(status="ok", outputs=_to_jsonable(outputs))
                    _save_setup_cache(
                        scope_dir, request.setup.task, request.setup.inputs, outputs, log
                    )
                except CredentialNotFoundError as exc:
                    # Not cached, and can't materialise here without a
                    # credential this request doesn't carry -- a synchronous
                    # call's shape by design. Distinct, machine-readable
                    # status: the platform recovers from this by queueing
                    # a credentialed run that materialises the scope and
                    # retrying once, so it must not read as a generic
                    # failure -- and never leak the exception's own
                    # class name/repr.
                    message = exc.args[0] if exc.args else str(exc)
                    log.warning(
                        "setup %r not materialised on this scope: %s",
                        request.setup.task,
                        message,
                    )
                    result.setup = SetupResult(
                        status="not_materialized",
                        error=f"setup {request.setup.task!r} not materialised: {message}",
                    )
                except Exception as exc:  # noqa: BLE001 -- a task's own bug must not crash the host
                    log.exception("setup %r failed", request.setup.task)
                    result.setup = SetupResult(status="failed", error=str(exc))

    for task_spec in request.tasks:
        task_def = capability.registry.tasks.get(task_spec.task)
        if task_def is None:
            result.tasks.append(
                TaskResult(
                    task=task_spec.task, status="failed", error=f"unknown task {task_spec.task!r}"
                )
            )
            continue

        ctx = Context(
            capability=capability.capability_id,
            operation=task_spec.task,
            workdir=scope_dir,
            credentials=credentials,
            log=log.getChild(task_spec.task),
            allow_anonymous_credentials=allow_anonymous_credentials,
        )
        try:
            resolved_inputs = _resolve_placeholders(task_spec.inputs, setup_outputs)
            kwargs = _kwargs_from_inputs(resolved_inputs)
            outputs = task_def.func(ctx, **kwargs) or {}
            result.tasks.append(
                TaskResult(task=task_spec.task, status="ok", outputs=_to_jsonable(outputs))
            )
        except TreeNotMaterializedError as exc:
            # Scope eviction is normal, invisible recovery, not a failure -- a reaped scope
            # must come back on the next request, not surface as a hard
            # error. The task itself is still recorded failed below, but
            # the envelope as a whole must carry the signal the platform
            # already understands (setup.status == "not_materialized") so
            # it re-materialises the scope through a credentialed run,
            # exactly as it does for a setup-time CredentialNotFoundError
            # above. Only this exception gets the conversion -- an ordinary
            # task bug must not look like a miss, or the platform would
            # re-materialise (and re-run) forever on a plain failure.
            message = exc.args[0] if exc.args else str(exc)
            log.warning("task %r hit an unmaterialised tree: %s", task_spec.task, message)
            result.tasks.append(TaskResult(task=task_spec.task, status="failed", error=message))
            if result.setup is None or result.setup.status != "not_materialized":
                # Don't overwrite an existing not_materialized (already the
                # right signal), and don't let a second/third task's less
                # informative message clobber the first one that explains it.
                result.setup = SetupResult(
                    status="not_materialized",
                    error=f"tree not materialised: {message}",
                )
        except Exception as exc:  # noqa: BLE001 -- one task failing must not lose the others
            log.exception("task %r failed", task_spec.task)
            result.tasks.append(TaskResult(task=task_spec.task, status="failed", error=str(exc)))

    return result
