---
name: triage-rwtask
description: "Use when `rwtask plan/apply/export/test` fails or behaves unexpectedly against the RunWhen platform API: a capability rejected with an E_* diagnostic, a `conflict` action blocking `apply`, `export` refusing to overwrite files, `rwtask test` reporting `FAIL`/`no test`, `rwtask schemas --check` disagreeing with its pydantic models, or `rwtask label` refusing to produce an image label. Also use before changing a GitOps command, to see exactly which platform-API route and payload shape it uses."
---

# Triage: rwtask (the capability GitOps CLI)

`rwtask` is the CLI this SDK ships (`src/runwhen_capability/cli.py`); its GitOps
commands (`plan`, `apply`, `export`, `test`) and the image-label/schema tooling
(`label`, `schemas`) live in `gitops.py`, `label.py` and `schemas.py`. `run`/`run
--local`/`serve` are the request-execution side and are out of scope here except
where `test`/`run --local` share code with them.

## 1. Two capability directory shapes — don't mix them up

- **Packaged capability** (what `run`, `run --request`, `serve`, `label` operate
  on): a directory with `manifest.yaml` and `tasks.py`, shipped baked into an
  image. `rwtask label <dir>` / `label --schemas <dir>` read this shape.
- **Custom capability bundle** (what `plan`/`apply`/`export`/`test` and `run
  --local` operate on): a directory with `capability.yaml`
  (`apiVersion: runwhen.com/custom-capability/v1`, `name`, `description`,
  `appliesTo`, `inputs`, optional `setup`, `tasks: [...]`) plus `tasks/`, `lib/`,
  `schemas/`, `tests/` and, at the root, only `capability.yaml` or `README.md`
  (`ALLOWED_DIR_PREFIXES` / `ALLOWED_ROOT_FILES` in `custom/manifest.py`). This is
  the bundle shape sent over the wire in a request, never baked into an image.

`find_capability_dirs(root)` (`gitops.py`) is `plan`/`apply`/`export`/`test`'s
shared definition of "a capability": every directory at or under `root` that
directly holds a `capability.yaml`, found recursively.

## 2. Commands and what they call

| Command | Talks to | Notes |
|---|---|---|
| `rwtask plan <dir> [--prune]` | `POST {api_url}/api/v4/workspaces/{workspace}/custom-capabilities:plan` | Validates every bundle under `dir` locally first (`runwhen_capability.custom.validate`); the platform is never called if any bundle has an error-severity diagnostic. |
| `rwtask apply <dir> -m <msg> [--prune] [--adopt] [--yes]` | Calls `plan()` again internally, then `POST .../custom-capabilities:apply` | Always re-plans and re-validates independently of any earlier `plan` call. |
| `rwtask export <dir> [--name NAME ...] [--force]` | `GET {api_url}/api/v4/workspaces/{workspace}/custom-capabilities:export?names=a,b` | No `--name` exports every capability in the workspace. |
| `rwtask test <dir> [--task NAME]` | Nothing — purely local | Runs `tests/<task>.json` fixtures through the same code path as `run --local`. |
| `rwtask label <dir> [--schemas]` | Nothing — purely local | Reads a **packaged** capability's `manifest.yaml` / `schemas/`. |
| `rwtask schemas [--check] [--project-dir DIR]` | Nothing — purely local | Regenerates JSON Schema files from pydantic models listed in `pyproject.toml`. |

`plan`/`apply`/`export` all take `--api-url`/`--token`/`--workspace`, falling back
to `RW_API_URL`/`RW_API_TOKEN`/`RW_WORKSPACE` (`_add_gitops_args`,
`_gitops_config` in `cli.py`). Missing any of the three is a usage error naming
exactly which flag/env var is missing — this is a local `parser.error()`, not a
platform response. The token is sent as `Authorization: Bearer` (`_headers` in
`gitops.py`) and is never printed or logged.

## 3. plan/apply semantics: actions, exit codes, drift, idempotency

Every capability in a `:plan`/`:apply` response carries an `action`:
`create`, `update`, `unchanged`, `delete` (only with `--prune`), or `conflict`
(the capability already exists in the workspace but is **not** git-managed —
i.e. the platform's state has drifted from what this GitOps flow tracks).

- **Idempotency**: re-running `plan`/`apply` against an unchanged directory
  reports every capability `unchanged`, and `apply` prints `"nothing to apply"`
  and returns 0 without ever calling `:apply` (`has_changes()` short-circuits it
  — see `test_apply_skips_the_apply_call_when_nothing_changed` in
  `tests/test_gitops.py`). Applying the same bundle twice does not double-create
  anything.
- **Drift / conflict**: a `conflict` means the workspace has a capability of that
  name whose `managed_by` is not this GitOps flow (created some other way).
  `apply` refuses it — no `:apply` call is made — unless `--adopt` is passed
  (`test_apply_conflict_without_adopt_refuses_and_never_calls_apply`).
- **Exit codes** (`rwtask plan`, Terraform-style): `0` nothing would change,
  `2` something would change, `1` on error (local validation failure or a
  platform-API error). `apply` and `export` return `0` on success, `1` on any
  error (including a refused conflict or an unresolved local-file conflict).
- **Confirmation**: `apply` proceeds without prompting when `--yes` is given, or
  when stdin is not a TTY (CI) — there is no one to ask, so the pipeline that
  invoked it is treated as the actual gate. On an interactive TTY with no
  `--yes`, it asks `apply N capability change(s) to workspace '<ws>'? [y/N]` and
  only proceeds on `y`/`yes` (`_confirm_apply` in `cli.py`).

## 4. `rwtask test`

`rwtask test <dir> [--task NAME]` finds every capability under `dir`, and for
each declared task that has a `tests/<task>.json`
(`{"inputs": {...}, "target"?: {...}, "expect"?: {"status": "ok"}}`), runs it
through `run_local_bundle()` — the same path `run --local` uses — with
`allow_anonymous=True` (a task whose credential input can't be resolved just
runs without it, rather than failing before it starts; test files never carry
secrets). The run's resulting status is compared to `expect.status` (default
`"ok"`); since `run_local_bundle` already fails a task whose outputs don't
validate against their declared schema, a status match means the outputs
validated too. Outcomes: `ok` (pass), `FAIL <detail>` (status mismatch — detail
includes the task's `error`/`reason` when there is one), or `-- no test` (no
fixture file; does not fail the run). Exit code is `1` if any task's test
failed, `0` otherwise (a run with only passes and `no test`s is still `0`).

## 5. Image label / schema tooling

- `rwtask label <dir>` prints base64 of `<dir>/manifest.yaml`, verbatim
  (`com.runwhen.capability.manifest.v1`). Refuses (`ManifestLabelError`, exit 1,
  printed as `rwtask label: <message>`) if the manifest is missing or has a
  top-level `image:` key — an image cannot know its own digest.
- `rwtask label --schemas <dir>` prints base64 of a compact, sorted-key JSON
  object of every file under `<dir>/schemas/`, keyed `schemas/<filename>`
  (`com.runwhen.capability.schemas.v1`) — every file, not only the ones the
  current manifest references, so an old run's output stays resolvable after a
  manifest's `schema:` ref moves on. Every `schema:` ref found in the manifest
  text must resolve to one of these files, or it's refused
  (`ManifestLabelError`, printed as `rwtask label --schemas: <message>`).
- `rwtask schemas [--check] [--project-dir DIR]` exports each task output's
  JSON Schema from the pydantic model that produces it, per a
  `[tool.rwtask.schemas."<capability-dir>"]` table in `pyproject.toml`:
  ```toml
  [tool.rwtask.schemas."capabilities/rw-checks"]
  "findings.v1.json" = "runwhen_capability.models:FindingsResult"
  ```
  Without `--check`, it writes every listed file. With `--check`, it writes
  nothing and fails (exit 1) if a listed file is missing or differs byte-for-byte
  from what the export would produce — the message tells you to run
  `rwtask schemas` and commit the result. Files under `schemas/` not listed in
  the table are left alone (an older published schema version can coexist).

## 6. Running with verbose output

There is no `--verbose`/`--debug` flag. `cli.py`'s `main()` unconditionally calls
`logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s
%(levelname)s %(message)s")`, so module loggers (`runwhen_capability.run`,
`.bundle`, `.host`, `.serve`) already emit at INFO by default. The commands
themselves are the main source of detail: `plan`/`apply` print every
capability's action, its diagnostics, and a per-file unified diff
(`_print_plan` in `cli.py`); `run`/`run --local` print the complete result as
indented JSON. For finer-grained (DEBUG) logging, call
`logging.basicConfig(level=logging.DEBUG)` yourself before invoking
`runwhen_capability.cli.main()` from a Python shell — there is no CLI flag or
env var for it today.

## 7. Common failures and fixes

| Symptom | Check | Fix |
|---|---|---|
| `plan`/`apply` exits 1 with `<file>:<line> E_MANIFEST_SCHEMA ...` before any network call | `capability.yaml` doesn't match its schema | Fix the manifest; nothing was sent to the platform (`test_plan_local_validation_error_exits_1_and_never_calls_the_api`) |
| `... E_PATH_NOT_ALLOWED ...` | A bundle file sits outside `capability.yaml`/`README.md` at the root, or outside `tasks/`, `lib/`, `schemas/`, `tests/` | Move the file under one of those prefixes (`ALLOWED_DIR_PREFIXES`/`ALLOWED_ROOT_FILES` in `custom/manifest.py`) |
| `... E_LIMIT ...` | A single file, or the whole bundle, is too large, or there are too many files | Limits are 64 KiB/file, 40 files, 256 KiB total bundle (`MAX_FILE_BYTES`/`MAX_FILES`/`MAX_BUNDLE_BYTES`) |
| `... E_TASK_FILE_MISSING ...` | A task's (or `setup`'s) `file:` doesn't exist in the bundle | Add the file or fix the path in `capability.yaml` |
| `... E_UNDECLARED_INPUT ...` | A Python `main()` parameter, or a bash `$VAR`, isn't declared under `inputs:` | Add it to `capability.yaml`'s `inputs` |
| `... E_OUTPUT_UNDECLARED ...` | A returned/`rw_set`/`rw_append` output name isn't declared under the task's `outputs:` | Add it to the task's `outputs` |
| `... E_READONLY_WRITE ...` | The task has `readOnly: true` but its script runs `kubectl delete/apply/patch/edit/scale/rollout restart/exec` (or similar) | Either drop `readOnly: true` or remove the mutating call |
| `... E_SCHEMA_FEATURE ...` | An output schema uses `pattern`/`patternProperties` (regex keywords) | Custom-task schemas can't use regex keywords — rewrite without them |
| `... E_SCHEMA_NOTATION ...` | A compact inline schema (e.g. `"{ count: integer }[]"`) can't be parsed | Fix the notation, or use a `schema: ./schemas/<file>.json` ref instead |
| `apply` refuses with `"... already exist and are not git-managed; rerun with --adopt ..."` | Action was `conflict` | Confirm you actually want to take over that capability, then rerun with `--adopt` |
| `apply` prints `"nothing to apply"` and exits 0 | Every capability's action was `unchanged` | Expected — no drift, nothing to do |
| `export` refuses with `"... has local changes; rerun with --force ..."` | A local file differs from the exported content and isn't identical | Review the diff yourself, then rerun with `--force` if you want the export to win |
| `plan`/`apply`/`export` fails with `"needs --api-url/RW_API_URL and ..."` | One or more of `--api-url`/`--token`/`--workspace` (or their env vars) is unset | Set the missing flag(s) or env var(s); this is a local check, no request was made |
| Platform-side error, e.g. `"... 422: capability.yaml:3 E_LIMIT bundle is over the file-count limit"` | The platform's own `{"errors": [...]}` body, mapped through the same `file:line code message` formatting as a local error | Read the message like a local diagnostic — same codes, same shape |
| `rwtask test` reports `FAIL <task>: expected status 'ok', got 'failed': <reason>` | The task raised, or its outputs failed schema validation, when the fixture expected `ok` | Fix the task or update the fixture's `expect.status` if `failed`/`skipped` is actually correct |
| `rwtask test` reports `-- <task>: no test` | No `tests/<task>.json` for that declared task | Not a failure by itself, but means that task has zero test coverage — add a fixture |
| `rwtask label <dir>` fails with `"has a top-level 'image:' key"` | `manifest.yaml` declares its own `image:` | Remove it — the image doesn't know its own digest until after it's built |
| `rwtask label --schemas <dir>` fails with a missing-file message | A manifest `schema:` ref doesn't match any file actually under `schemas/` | Add the missing schema file, or fix the ref |
| `rwtask schemas --check` fails | A listed schema file is missing, or differs from what its pydantic model would export | Run `rwtask schemas` (no `--check`) and commit the regenerated file(s) |

## 8. Tests to run

```bash
make install     # pip install -e ".[dev]"
make test        # pytest -q, the whole suite
make lint        # ruff check .
make fmt-check   # ruff format --check .
```

Narrower, while iterating on GitOps behavior:

```bash
pytest tests/test_gitops.py -v         # plan/apply/export/test: actions, conflicts, exit codes
pytest tests/test_cli.py -v            # argument parsing and dispatch for every subcommand
pytest tests/test_label.py -v          # rwtask label / label --schemas
pytest tests/test_schemas_cmd.py -v    # rwtask schemas / schemas --check
pytest tests/custom/test_validate.py -v  # the E_* static checks bundles are validated against
```

`tests/test_gitops.py` mocks the platform API with the `responses` library (no
network, no live platform needed) — that's the fastest way to reproduce a
plan/apply/export symptom locally without a real workspace.

## References

- `src/runwhen_capability/gitops.py` — `plan`/`apply`/`export`/`test`, the platform-API client, `GitOpsError`/`LocalValidationError`
- `src/runwhen_capability/cli.py` — argument parsing and dispatch for every `rwtask` subcommand
- `src/runwhen_capability/label.py` — `rwtask label` / `label --schemas`
- `src/runwhen_capability/schemas.py` — `rwtask schemas` / `schemas --check`
- `src/runwhen_capability/custom/manifest.py` — bundle path/size limits, `Manifest` model
- `src/runwhen_capability/custom/diagnostics.py` — every `E_*` code, static and runtime
- `src/runwhen_capability/custom/validate.py` — the static checks that raise those codes
- `tests/test_gitops.py`, `tests/test_cli.py`, `tests/test_label.py`, `tests/test_schemas_cmd.py`
- `README.md` — "GitOps", "Custom capability bundles", "Image labels", "Output schemas"
