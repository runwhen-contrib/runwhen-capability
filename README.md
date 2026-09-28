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

## Development

```
make install   # pip install -e ".[dev]"
make test      # pytest
make lint      # ruff check
make fmt-check # ruff format --check
```

## License

Apache-2.0.
