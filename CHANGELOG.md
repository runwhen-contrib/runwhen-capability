# Changelog

This project follows [Semantic Versioning](https://semver.org/). Releases are git tags `v<version>`
with the wheel and sdist attached to the GitHub Release.

## 0.2.0

### Added

- `runwhen_capability.custom`: parses, validates and compiles a **custom capability bundle**
  (`capability.yaml`, `apiVersion: runwhen.com/custom-capability/v1`, plus `tasks/`, `lib/`,
  `schemas/`, `tests/`).
  - `validate(files) -> list[Diagnostic]`: every static check, with no execution -- manifest shape,
    path/size limits, task file existence, the compact schema notation, undeclared inputs/outputs,
    `readOnly` writes, and duplicate names. See the module docstring for the full error code list.
  - `compile_manifest(files) -> dict`: the packaged-capability manifest shape (`capability`,
    `appliesTo`, `needs.credentials`, `tasks[]` with every output's schema compiled to JSON Schema).
    Raises `CompileError` if `validate()` found an error.
  - `content_hash(files)` / `task_hash(files, task)`.
- **Bundle mode**: a request whose envelope carries a `bundle` (its files inline, content-hash
  verified) is materialised, compiled, and run -- `rwtask serve` dispatches to it automatically;
  `rwtask run --local <bundle-dir> --task <name> --inputs '<json>'` runs the same path locally, with
  no relay.
  - Python: `main(ctx, **inputs)`, with the new `ctx.skip(reason)` (equivalent to `raise
    SkipTask(reason)`). Bash: inputs as upper-cased env vars, plus `rw.sh` (`rw_input`, `rw_append`,
    `rw_set`, `rw_skip`) sourced via `$RW_SDK`, writing to a private file descriptor.
  - Each setup/task runs as its own child process, in its own process group, killed with its
    whole group when it exits or when the request's deadline passes. `deadlineMs` is one budget for
    the whole request: setup and every task share it.
  - Outputs are checked against their compiled schema (`E_OUTPUT_SCHEMA`, with the JSON path) and
    capped (`E_OUTPUT_TOO_LARGE`; a list output is truncated instead of failing the task).
  - Every credential/secret value used by the run is redacted from outputs and the log tail.
  - `TaskResult`/`SetupResult` gain `errors` (the full runtime diagnostic list) and `logTail`
    (bundle mode only); `TaskResult.status`/`SetupResult.status` gain `"timeout"`.
- `Context.skip(reason="")`.

## 0.1.0

First release as its own repository. The SDK and the `rwtask` task host previously shipped inside
rw-checks-codecollection; behaviour carried over unchanged, and the import name is still
`runwhen_capability`.

### Added

- Task-level `skipped` status: a task raises `SkipTask(reason)` and is recorded as
  `TaskResult(status="skipped", reason=...)`. The remaining tasks still run. Setup cannot skip.
- `rwtask label [--schemas] <capability-dir>` prints the `com.runwhen.capability.manifest.v1` or
  `com.runwhen.capability.schemas.v1` label value, and refuses a manifest with a top-level `image:`
  key.
- `rwtask schemas [--check]` exports output schemas from pydantic models listed under
  `[tool.rwtask.schemas]` in the capability repo's `pyproject.toml`, or checks the committed files
  match them.
- `rwtask --version`.
- `.github/workflows/capability-image.yml`, a reusable workflow that builds, smoke-tests and pushes
  one capability image.

### Changed

- `TaskResult` carries a `reason` field (`null` unless the task was skipped).
