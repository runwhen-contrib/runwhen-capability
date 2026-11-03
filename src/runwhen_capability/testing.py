"""Unit-test a skill's scripts against the real V13 semantics, without a runner.

    from runwhen_capability.testing import make_context, run_main, stub_commands

    def test_pods():
        resource = {"path": "kubernetes/clusters/kind/namespaces/web", "cluster": "kind",
                    "type": "kubernetes/namespace", "name": "web"}
        with make_context(resource=resource, settings={"maxPods": 1}) as tc:
            stub_commands(tc, {
                ("kubectl", "get", "pods", "-n", "web", "-o", "json"): (0, '{"items": []}', ""),
            })
            env = run_main(main, tc, limit=5)    # main(ctx, limit=5), from scripts/pods.py
            assert tc.errors == []               # ctx.error / RwError, as the executor reads them
            assert tc.artifact("report") is None  # a file the script wrote under $RW_OUT
        assert env["status"] == "ok"             # checked against `result-envelope`
        assert env["outputs"] == {"pods": []}

`make_context` builds a real `Context` the way the V13 executor does (plugin/run.py): the
same environment (`RW_TASK_CONTRACT=v13`, `RW_RESOURCE_JSON`, `RW_SETTINGS_JSON`,
`RW_DRY_RUN`, `RW_OUT`, `RW_ERRORS_FILE`), so `ctx.resource` keys are verbatim, `ctx.settings`
names are snake_cased at the top level only, `ctx.run` raises `RwCommandError` on a non-zero
exit, and `ctx.error` writes the side channel. `settings` takes the names as `tasks.yaml`
declares them (camelCase). Pass `v13=False` for the classic contract (`ctx.run` returns on a
non-zero exit unless `check=True`).

The environment is set only while a `Context` is built or a script runs (`run_main`,
`TestContext.environ()`), and restored after, so nothing leaks between tests. A script that
reads `os.environ["RW_OUT"]` directly needs to run inside `run_main` or `tc.environ()`.

`stub_commands` replaces `ctx.run` for that context: an argv is matched exactly, nothing is
spawned, and an argv with no stub is an `AssertionError`. `run_main` calls
`main(ctx, **inputs)` as the executor's entry point does: `ctx.skip` is a skipped envelope,
an `RwError` is recorded and ends the run `failed`, and any other exception propagates (the
executor would turn it into a generic failure; a test wants the traceback). What is not
reproduced: output schema checks against the task's `tasks.yaml`, and artifact upload.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .context import Context
from .contracts import check as check_contract
from .envelope import derive_envelope, errors_from_jsonl
from .errors import SkipTask
from .rwerrors import RwError

__all__ = ["TestContext", "make_context", "run_main", "stub_commands"]


@contextmanager
def _scoped_environ(updates: Mapping[str, str | None]) -> Iterator[None]:
    """`updates` applied to os.environ (None: unset) for the block, then put back."""
    saved = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _json_default(value: Any) -> Any:
    # The entry point's rule: a pydantic model (an ArtifactRef) travels as its JSON form.
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class TestContext:
    """A real `Context` plus what the executor reads back from the run. Make one with
    `make_context`. Use it as a context manager (or call `close()`) to remove its
    temporary directory."""

    __test__ = False  # not a pytest test class, whatever the name says

    def __init__(
        self,
        ctx: Context,
        *,
        root: Path,
        out_dir: Path,
        errors_file: Path,
        env: dict[str, str | None],
        dry_run: bool,
        owns_root: bool,
    ) -> None:
        self.ctx = ctx
        self.out_dir = out_dir
        self._root = root
        self._errors_file = errors_file
        self._env = env
        self._dry_run = dry_run
        self._owns_root = owns_root
        self._stubs: dict[tuple[str, ...], tuple[int, str, str]] | None = None
        self._calls: list[tuple[str, ...]] = []

    def __enter__(self) -> TestContext:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Removes the temporary directory (`out_dir`, the error side channel)."""
        if self._owns_root:
            shutil.rmtree(self._root, ignore_errors=True)

    def environ(self):
        """A context manager that applies this context's `RW_*` environment (what the
        executor sets for the script) and restores os.environ on exit."""
        return _scoped_environ(self._env)

    @property
    def errors(self) -> list[dict[str, Any]]:
        """The structured errors recorded so far, read back from the side channel
        (`$RW_ERRORS_FILE`) exactly as the executor reads them."""
        return errors_from_jsonl(self._errors_file)

    def artifact(self, name: str) -> Path | None:
        """The file a script wrote for the artifact output `name`: `$RW_OUT/<name>`, or the
        only `$RW_OUT/<name>.<ext>` (several candidates, or none: None)."""
        exact = self.out_dir / name
        if exact.is_file() or exact.is_symlink():
            return exact
        try:
            candidates = sorted(
                p
                for p in self.out_dir.iterdir()
                if p.name.split(".", 1)[0] == name and (p.is_file() or p.is_symlink())
            )
        except OSError:
            return None
        return candidates[0] if len(candidates) == 1 else None

    def envelope(
        self,
        outputs: dict[str, Any],
        *,
        exit_code: int = 0,
        skipped_reason: str | None = None,
    ) -> dict[str, Any]:
        """The D64 result envelope for a run that ended with `exit_code` and `outputs`,
        with the errors recorded so far; checked against the `result-envelope` contract
        (ContractError if it does not satisfy it)."""
        now = datetime.now(UTC)
        envelope = derive_envelope(
            exit_code=exit_code,
            outputs=json.loads(json.dumps(outputs, default=_json_default)),
            errors=self.errors,
            started_at=now,
            finished_at=now,
            skipped_reason=skipped_reason,
        )
        if self._dry_run:
            envelope["dryRun"] = True
        check_contract("result-envelope", envelope)
        return envelope


def make_context(
    *,
    resource: dict | None = None,
    settings: dict | None = None,
    dry_run: bool = False,
    workdir: Path | None = None,
    v13: bool = True,
) -> TestContext:
    """A `TestContext` whose `.ctx` is a real `Context`, built as the V13 executor builds
    it. `resource` is `ctx.resource` verbatim (identity fields plus `path` and `type`;
    None: `ctx.resource` is None); `settings` are the task's settings by their declared
    names; `workdir` is `ctx.workdir` (default: a directory inside the temporary one).
    See the module docstring for the example."""
    root = Path(tempfile.mkdtemp(prefix="rw-testing-"))
    try:
        out_dir = root / "out"
        out_dir.mkdir()
        errors_file = root / "errors.jsonl"
        if workdir is None:
            workdir = root / "work"
            workdir.mkdir()
        env: dict[str, str | None] = {
            "RW_TASK_CONTRACT": "v13" if v13 else None,
            "RW_RESOURCE_JSON": json.dumps(resource) if resource is not None else None,
            "RW_SETTINGS_JSON": json.dumps(settings or {}),
            "RW_DRY_RUN": "1" if dry_run else None,
            "RW_OUT": str(out_dir),
            "RW_ERRORS_FILE": str(errors_file),
        }
        with _scoped_environ(env):
            ctx = Context(capability="test", operation="test", workdir=Path(workdir))
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return TestContext(
        ctx,
        root=root,
        out_dir=out_dir,
        errors_file=errors_file,
        env=env,
        dry_run=dry_run,
        owns_root=True,
    )


def stub_commands(
    test_ctx: TestContext, stubs: Mapping[tuple[str, ...], tuple[int, str, str]]
) -> list[tuple[str, ...]]:
    """Makes `test_ctx.ctx.run` answer from `stubs` -- `{argv tuple: (returncode, stdout,
    stderr)}` -- without spawning anything. The result is what a real run would do with
    that exit: a CompletedProcess, or RwCommandError (classified from the stderr) when
    `check` is true (the default under v13). An argv with no stub is an AssertionError
    naming it. Calling it again adds to the stubs. Returns the live list of argv tuples
    that `ctx.run` was called with."""
    for argv, result in stubs.items():
        if not isinstance(argv, tuple) or not all(isinstance(a, str) for a in argv):
            raise TypeError(f"a stub key must be a tuple of strings, got {argv!r}")
        if not isinstance(result, tuple) or len(result) != 3:
            raise TypeError(f"stub for {argv!r} must be (returncode, stdout, stderr)")
    if test_ctx._stubs is not None:
        test_ctx._stubs.update(stubs)
        return test_ctx._calls
    test_ctx._stubs = dict(stubs)
    ctx = test_ctx.ctx

    def run(
        argv: list[str],
        cwd: Path | str | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
        *,
        inherit_env: bool = True,
        check: bool | None = None,
    ):
        key = tuple(argv)
        test_ctx._calls.append(key)
        if key not in test_ctx._stubs:
            known = ", ".join(repr(k) for k in test_ctx._stubs) or "none"
            raise AssertionError(
                f"ctx.run called with an unstubbed argv {key!r} (stubbed: {known})"
            )
        returncode, stdout, stderr = test_ctx._stubs[key]
        return ctx._finish_run(list(argv), returncode, stdout, stderr, check)

    ctx.run = run  # instance attribute: this one context only
    return test_ctx._calls


def run_main(main, test_ctx: TestContext, **inputs: Any) -> dict[str, Any]:
    """Calls `main(ctx, **inputs)` the way the executor's Python entry point does, with
    the context's environment applied, and returns the result envelope. `ctx.skip(...)`
    gives a `skipped` envelope; an `RwError` (`RwCommandError` too) is recorded as an
    error and ends the run `failed` (exit 1), with no outputs; any other exception
    propagates."""
    ctx = test_ctx.ctx
    outputs: dict[str, Any] = {}
    exit_code = 0
    skipped_reason: str | None = None
    with test_ctx.environ():
        try:
            outputs = main(ctx, **inputs) or {}
        except SkipTask as exc:
            skipped_reason = str(exc.args[0]) if exc.args else ""
        except RwError as exc:
            ctx.error(exc.code, exc.message, scope=exc.scope, hint=exc.hint, details=exc.details)
            exit_code = 1
        finally:
            ctx.artifacts._abort_unclosed()
    return test_ctx.envelope(outputs, exit_code=exit_code, skipped_reason=skipped_reason)
