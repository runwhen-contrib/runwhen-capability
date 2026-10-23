# runwhen-capability

The Python SDK that RunWhen capability images are written against, and `rwtask`, the task host
that runs them.

A **capability** is a directory holding a `manifest.yaml` (what the capability is, what it needs,
which tasks it exposes) and a `tasks.py` (those tasks, as plain Python functions). A capability
image ships exactly one capability plus this SDK; the runner starts it as a warm executor pod and
hands it requests.

## Install

Capability repositories pin a release tag:

```toml
# pyproject.toml
dependencies = [
    "runwhen-capability @ git+https://github.com/runwhen-contrib/runwhen-capability@v0.4.0",
]
```

For development against a local checkout, install it first and the capability repo without
dependency resolution:

```
pip install -e ../runwhen-capability[dev]
pip install --no-deps -e .
```

## Writing tasks

```python
from runwhen_capability import Context, SkipTask, setup, task


@setup(outputs=["tree", "changed"])
def checkout(ctx: Context, repo_url: str, sha: str, base_sha: str | None = None):
    tree = ctx.git.checkout(repo_url, sha, credential="repo", optional=True)
    return {"tree": tree, "changed": ctx.git.changed_files(tree, base_sha) if base_sha else None}


@task(outputs={"findings": "rw.findings.v1"})
def lint(ctx: Context, tree: str, changed: list[str] | None = None):
    if changed == []:
        raise SkipTask("no changed files to check")
    proc = ctx.run(["ruff", "check", "--output-format=sarif", "."], cwd=tree)
    return {"findings": {"findings": ctx.sarif.parse(proc.stdout, root=tree)}}
```

- `@setup(outputs=[...])` runs once per request, before any task. Tasks reference its outputs in
  the manifest as `${setup.<name>}`.
- `@task(outputs={name: kind})` registers a task. The request's camelCase input names arrive as
  snake_case keyword arguments.
- `Context` is the task's only boundary: `ctx.credential(name)`, `ctx.run(argv)` (bounded output,
  timeouts, process-group kill), `ctx.artifacts`, `ctx.git`, `ctx.sarif`, `ctx.findings`,
  `ctx.repo_fs`, `ctx.workdir`, `ctx.log`.
- Each task ends `ok`, `failed` (it raised; `error` says why) or `skipped` (it raised
  `SkipTask(reason)`; `reason` says why). Either way the rest of the request still runs. Only
  tasks can skip: a setup that raises `SkipTask` has failed.

### Artifacts

A result travels inline and has a size cap. Large or binary output goes out as an **artifact**
instead: its own upload to the runner, streamed, so memory stays bounded however big it is. The
task declares the output as an artifact (`outputs={"dump": "artifact"}`) and returns the
`ArtifactRef`, which serialises as `{"$artifact": "<ref>"}` -- the marker the platform replaces with
the stored artifact.

```python
@task(outputs={"report": "artifact", "dump": "artifact"})
def export(ctx: Context):
    with ctx.artifacts.open("report", "text/plain") as out:
        out.write(b"first line\n")
        out.write(b"second line\n")
    run = ctx.artifacts.from_process(["pg_dump", "--schema-only", "app"], "dump")
    if run.returncode:
        raise RuntimeError(run.stderr)
    return {"report": out.ref, "dump": run.ref}
```

- `open(name, content_type=None)` returns a writer. Leaving the `with` block normally stores the
  artifact (`out.ref`); leaving it on an exception stores nothing. `content_type` defaults to the
  one the output declares.
- `add_file(path_or_open_file, name, content_type=None)` uploads a file. It re-reads the file, so a
  busy runner (503) is retried three times, five seconds apart.
- `from_process(argv, name, *, content_type="text/plain", timeout=None, env=None)` runs `argv` in
  `ctx.workdir` and streams its stdout as the artifact, returning `ProcessArtifact(ref, returncode,
  stderr)`. A non-zero exit still completes the artifact, so check `returncode`. With no `timeout`
  it runs until the request's deadline (and without limit under `rwtask run`); on a timeout the
  process group is killed, nothing is stored, and `subprocess.TimeoutExpired` is raised.
- `name` is a relative path (no empty, `.` or `..` parts) and must be an artifact output the task
  declares.

Once bytes start flowing, the runner abandons an upload that receives no byte for two minutes, so
a writer must keep writing. `open` and `from_process` stream, and a stream cannot be replayed:
they raise `ArtifactBusyError` on a busy runner instead of retrying. For sparse output (a little
now, more many minutes later), write a file under `ctx.workdir` and `add_file` it.

**Reading.** An artifact the platform bound to a task input is read by that input's name:

```python
with ctx.artifacts.input("prior-report") as fh:  # a binary stream; close it
    header = fh.readline()
path = ctx.artifacts.input_path("prior-report")  # a local file, for tools that want a path
```

`input_path` downloads once into a cache under the executor's workdir (`.rw-artifact-cache/`)
that later requests share, verifying the sha256 as it streams (`E_ARTIFACT_CORRUPT` on a
mismatch). The cache is bounded by `RW_ARTIFACT_CACHE_MAX_BYTES` (default 1 GiB, least recently
used first). The files are read-only (mode 0444): copy one to edit it. The path stays valid for
the rest of the task. A name the request did not bind raises `ArtifactUndeclaredError`.

**Local mode.** Under `rwtask run` there is no runner, so artifacts are files:
`<workdir>/.rw-artifacts/<name>` for outputs (with a `local_...` ref) and
`<workdir>/.rw-artifacts/inputs/<name>` for inputs (`input_path` returns that file itself),
`<workdir>` being `ctx.workdir`. `rwtask run` deletes the temporary scope it created when it
finishes, so pass `--keep-workdir` (or your own `--workdir`) to look at them.

**Size limits.** When a request carries `maxResultBytes`, a result whose posted body (compact JSON)
is larger is replaced by a small failed result,
`E_RESULT_TOO_LARGE: ... write large data with ctx.artifacts`; with none, nothing is capped. An
output the platform declares spillable is moved to an `application/json` artifact on its own when
its JSON is over 256 KiB, and replaced by its ref. Where artifacts are switched off or unsupported
it stays inline.

**Errors.** Every artifact failure is an `ArtifactError` (from `runwhen_capability.errors`) with a
`.code`; an uncaught one fails the task with its message.

```python
from runwhen_capability.errors import ArtifactError

try:
    ref = ctx.artifacts.add_file(ctx.workdir / "dump.sql", "dump")
except ArtifactError as exc:
    ctx.log.error("could not store the dump: %s", exc.code)  # e.g. E_ARTIFACT_TOO_LARGE
    raise
```

Each code the runner can return has its own subclass, so a task can catch just one case:

| Class | Code |
|---|---|
| `ArtifactTooLargeError` (`.max_bytes`) | `E_ARTIFACT_TOO_LARGE` |
| `ArtifactUndeclaredError` | `E_ARTIFACT_UNDECLARED` |
| `ArtifactExistsError` | `E_ARTIFACT_EXISTS` |
| `ArtifactQuotaError` | `E_ARTIFACT_QUOTA` |
| `ArtifactLeaseLostError` | `E_ARTIFACT_LEASE_LOST` |
| `ArtifactBusyError` | `E_ARTIFACT_BUSY` |
| `ArtifactsUnavailableError` | `E_ARTIFACTS_DISABLED`, `E_ARTIFACTS_UNSUPPORTED` |
| `ArtifactError` | `E_ARTIFACT_TRANSPORT` (the runner could not be reached, or a read was cut short), `E_ARTIFACT_RESPONSE` (an answer the SDK cannot read), `E_ARTIFACT_PATH` (a bad name), `E_ARTIFACT_CORRUPT`, `E_ARTIFACT_ABORTED`, `E_ARTIFACT_ENDED`, or `HTTP_<status>` for an answer with no recognised code |

## rwtask

```
rwtask run <capability-dir> --request request.json [--credentials creds.json]
rwtask run --local <bundle-dir> --task <name> [--inputs '<json>'] [--credentials creds.json]
rwtask serve [--capability-dir DIR] [--allow-bundles]
             # RELAY_URL, POOL_ID, EXECUTOR_TOKEN_FILE from the env
rwtask plan <dir> [--prune]
rwtask apply <dir> -m <message> [--prune] [--adopt] [--yes]
rwtask export <dir> [--name NAME ...] [--force]
rwtask test <dir> [--task NAME]
```

`rwtask run` is the same code path as `rwtask serve`, against the local filesystem: a capability
author needs no cluster.

### GitOps

`rwtask plan`/`apply`/`export` manage custom capability bundles -- a directory holding a
`capability.yaml` plus its `tasks/`, `lib/`, `schemas/`, `tests/` files (see "Custom capability
bundles" below) -- against the RunWhen platform API, so a repository of them can be reviewed and
merged like any other code:

```
rwtask plan capabilities/            # find every capability.yaml under the dir, recursively
rwtask apply capabilities/ -m "add pgbouncer-health"
rwtask export capabilities/ --name pgbouncer-health
```

- `rwtask plan <dir>` validates every capability locally first (a local error prints as
  `file:line code message` and exits 1, with no network call); it then asks the platform API what
  `apply` would change and prints each capability's action (`create`, `update`, `unchanged`,
  `delete`, or `conflict` -- it exists but is not git-managed) and its file diffs. Exit codes are
  Terraform-style, for CI: 0 when nothing would change, 2 when something would, 1 on error.
- `rwtask apply <dir> -m <message> [--prune] [--adopt] [--yes]` plans first, prints the plan, then
  applies it. A `conflict` is refused unless `--adopt` is given. `--prune` also removes every
  git-managed capability missing from `dir`. Without `--yes` on a TTY, it asks for confirmation;
  off a TTY (e.g. CI) it proceeds without asking, since there is no one to ask -- the pipeline that
  invoked it is the actual gate.
- `rwtask export <dir> [--name NAME ...] [--force]` writes each capability's published files under
  `<dir>/<name>/`. Given no `--name`, every capability in the workspace is exported. It refuses to
  overwrite a local file whose content has changed, unless `--force` is given.
- All three take `--api-url`/`--token`/`--workspace`, or the `RW_API_URL`/`RW_API_TOKEN`/
  `RW_WORKSPACE` env vars. The token is sent as `Authorization: Bearer` and never printed.

`rwtask test <dir> [--task NAME]` runs a capability's own tests locally, with no platform API
involved. For each capability under `dir` and each of its tasks that has a `tests/<task>.json`
(`{"inputs": {...}, "target"?: {...}, "expect"?: {"status": "ok"}}`), it runs that task through the
same code path `run --local` uses and checks the resulting status against `expect.status` (default
`"ok"`) -- the task's outputs are already checked against their declared schema as part of that
run, so a status match means the outputs validated too. A task with no test file is reported as
`no test`, which does not fail the run. Exits 1 if any task's test fails.

### Custom capability bundles

A custom capability travels as a bundle -- `capability.yaml` plus its `tasks/`, `lib/`, `schemas/`
files -- inside the request itself, rather than baked into an image
(`runwhen_capability.custom` validates and compiles one; `rwtask run --local` runs one).

`rwtask serve` runs bundle requests **only when started with `--allow-bundles`**, or with
`RW_ALLOW_BUNDLES=1` in its environment. Without it, a request that carries a bundle is refused
with a failed result and none of its code is written or run. A bundle is arbitrary code, so only
an image built to run custom capabilities should turn this on; packaged-capability images leave
it off.

#### Artifact outputs

A task's output is either a value checked against a `schema` or a file declared with `artifact`
(exactly one of the two per output). The host uploads the file as an artifact and the output
becomes `{"$artifact": "<ref>"}`:

```yaml
# capability.yaml
tasks:
  - name: list-files
    file: tasks/list_files.sh
    readOnly: true
    outputs:
      files: { artifact: { contentType: text/plain, maxSize: 2Gi } }
      count: { schema: "integer" }
```

```bash
# tasks/list_files.sh
source "$RW_SDK/rw.sh"
find . -type f | sort | rw_artifact files -     # stdin becomes $RW_ARTIFACTS_DIR/files
rw_set count "$(find . -type f | wc -l | tr -d ' ')"
```

- `contentType` defaults to `application/octet-stream`. `maxSize` is a byte count or a number with
  a `Ki`, `Mi`, `Gi` or `Ti` suffix (`512Mi`, `2Gi`); unset, the platform's own cap applies.
- The name is one segment matching `[A-Za-z0-9_][A-Za-z0-9_.-]*`.
- `rw_artifact <name> -` copies stdin to `$RW_ARTIFACTS_DIR/<name>` and registers it.
  `rw_artifact <name> <path>` registers a file where it is, without copying it. A file written
  straight to `$RW_ARTIFACTS_DIR/<name>` is uploaded too. A Python task uses `ctx.artifacts`, which
  writes into the same directory, and may return the ref.
- `$RW_ARTIFACTS_DIR` is the scope's `artifacts/` directory, emptied before every task.
  `rwtask run --local --workdir DIR` refuses a non-empty `DIR/artifacts/` it did not create.
- The host, not the script, uploads, after the script exits and only for a task that otherwise
  succeeded: a task that failed, was skipped or timed out uploads nothing. Each file must be a
  regular file inside the request's scope, reached without crossing a symlink, or the task fails
  with `E_ARTIFACT_PATH`. A failed upload fails the task with the relay's code (for example
  `E_ARTIFACT_TOO_LARGE`), or `E_ARTIFACT_UPLOAD`. Files are uploaded as written; unlike inline
  outputs, they are not redacted.
- A declared artifact the task never wrote is simply absent from the outputs.
- `validate` knows `rw_artifact` and `RW_ARTIFACTS_DIR`, and reports a literal `rw_artifact` name
  that is not a declared artifact output (`E_OUTPUT_UNDECLARED`).

A `schema` output the platform declares spillable, and whose compact JSON outgrows 256 KiB, is
uploaded as an `application/json` artifact (redacted first) instead of being truncated or failing
with `E_OUTPUT_TOO_LARGE`. Where artifacts are switched off or unsupported, the usual caps apply.

### Image labels

A capability image carries its own manifest and schemas as OCI labels, so a catalog reads them
straight off the pushed image:

```
rwtask label capabilities/<name>             # com.runwhen.capability.manifest.v1
rwtask label --schemas capabilities/<name>   # com.runwhen.capability.schemas.v1
```

Both print base64 on stdout and nothing else, for use as build args. The manifest label is
`manifest.yaml`, verbatim; it is refused while the manifest has a top-level `image:` key (an image
cannot know its own digest). The schemas label is a compact, sorted-key JSON object of every file
under `schemas/`, keyed `schemas/<file>`; every `schema:` ref in the manifest must be one of them.

To read a label back off a published image:

```
crane config --platform linux/amd64 <ref> \
  | jq -r '.config.Labels["com.runwhen.capability.manifest.v1"]' | base64 -d
```

### Output schemas

A task output's JSON Schema is exported from the pydantic model that produces it, never written by
hand. List the files to export in the capability repo's `pyproject.toml`:

```toml
[tool.rwtask.schemas."capabilities/rw-checks"]
"findings.v1.json" = "runwhen_capability.models:FindingsResult"
```

```
rwtask schemas           # write capabilities/rw-checks/schemas/findings.v1.json
rwtask schemas --check   # CI: fail if any listed file is missing or differs from its model
```

Files under `schemas/` that are not listed are left alone, so an older published version can stay
on disk after the listing moves on to a new one.

## Building a capability image

`.github/workflows/capability-image.yml` is a reusable workflow that builds one capability image
for linux/amd64 and linux/arm64 on native runners, smoke-tests it and pushes a multi-arch manifest
to GHCR. Call it once per image, at the same tag as the SDK the repo pins:

```yaml
jobs:
  image:
    needs: test
    permissions:
      contents: read
      packages: write
    uses: runwhen-contrib/runwhen-capability/.github/workflows/capability-image.yml@v0.4.0
    with:
      capability: my-capability            # capabilities/my-capability/
      dockerfile: Dockerfile.my-capability
      image-name: my-capability-codecollection
      push: ${{ github.event_name != 'workflow_dispatch' || inputs.push }}
```

| Input | Default | Meaning |
|---|---|---|
| `capability` | required | Directory under `capabilities/`; also the manifest's `capability` id. |
| `dockerfile` | required | Dockerfile for this image. |
| `image-name` | required | Published as `ghcr.io/<owner>/<image-name>`. |
| `push` | `true` | Push the image. A pull request from a fork never pushes. |
| `execution-mode` | `''` | Expected `execution.mode`; empty skips the check. |
| `sbom` | `''` | Repo path of an SBOM the image ships; the smoke test checks the file named by the image's `io.runwhen.sbom` label is identical. |
| `smoke-command` | `''` | Extra bash run against the built image, with `$IMG` set. |
| `python-version` | `3.12` | Python used to run `rwtask` on the build host. |

Tags: `<branch>-<sha7>` plus `<branch>` (and `latest` on `main`) for a branch push, `pr-<n>-<sha7>`
plus `pr-<n>` for a pull request, and the bare tag (e.g. `v1.2.0`) with no aliases for a semver
tag. The Dockerfile must turn the `CAPABILITY_MANIFEST_B64` and `CAPABILITY_SCHEMAS_B64` build args
into the `com.runwhen.capability.manifest.v1` and `com.runwhen.capability.schemas.v1` labels; the
smoke test fails if it does not.

## Development

```
make install   # pip install -e ".[dev]"
make test      # pytest
make lint      # ruff check
make fmt-check # ruff format --check
```

## Releasing

Versions follow semver. The version is written in exactly one place, `__version__` in
`src/runwhen_capability/__init__.py`; `rwtask --version` prints it.

1. Bump `__version__` and add a `CHANGELOG.md` entry; merge to `main`.
2. Tag that commit `v<version>` and push the tag.

The release workflow tests the tagged commit, refuses a tag that differs from `__version__`,
builds the wheel and sdist, and attaches them to a GitHub Release. Nothing is published to PyPI.

## License

Apache-2.0.
