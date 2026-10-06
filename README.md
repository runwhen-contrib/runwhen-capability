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
    "runwhen-capability @ git+https://github.com/runwhen-contrib/runwhen-capability@v0.1.0",
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
  timeouts, process-group kill), `ctx.git`, `ctx.sarif`, `ctx.findings`, `ctx.repo_fs`,
  `ctx.workdir`, `ctx.log`.
- Each task ends `ok`, `failed` (it raised; `error` says why) or `skipped` (it raised
  `SkipTask(reason)`; `reason` says why). Either way the rest of the request still runs. Only
  tasks can skip: a setup that raises `SkipTask` has failed.

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
    uses: runwhen-contrib/runwhen-capability/.github/workflows/capability-image.yml@v0.1.0
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
