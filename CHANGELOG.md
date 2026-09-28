# Changelog

This project follows [Semantic Versioning](https://semver.org/). Releases are git tags `v<version>`
with the wheel and sdist attached to the GitHub Release.

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
