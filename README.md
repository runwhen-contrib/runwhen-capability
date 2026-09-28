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
rwtask serve [--capability-dir DIR]        # RELAY_URL, POOL_ID, EXECUTOR_TOKEN_FILE from the env
```

`rwtask run` is the same code path as `rwtask serve`, against the local filesystem: a capability
author needs no cluster.

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
