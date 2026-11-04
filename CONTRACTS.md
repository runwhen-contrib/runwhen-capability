# V13 plugins contracts (contracts-v4)

Every seam between the lanes of the V13 plugins program, frozen at M0 (`contracts-v1`)
and extended additively in `contracts-v2`, `contracts-v3` and `contracts-v4` (see [Changes in contracts-v2](#changes-in-contracts-v2),
[Changes in contracts-v3](#changes-in-contracts-v3), [Changes in contracts-v4](#changes-in-contracts-v4)). The
schemas in [`contracts/`](contracts/) are normative; this page says what they
cannot: who produces and consumes each document, the rules that are not
expressible in JSON Schema, and the calls made at freeze.

Design source: the V13 wiki ([skill spec](https://wiki.sdlc.shared.runwhen.com/#42a-skill-spec),
[integration spec](https://wiki.sdlc.shared.runwhen.com/#41a-integration-spec),
[connections](https://wiki.sdlc.shared.runwhen.com/#50-connections-credentials),
[registry](https://wiki.sdlc.shared.runwhen.com/#49-plugin-registry),
[rules and knowledge](https://wiki.sdlc.shared.runwhen.com/#52-rules-and-knowledge),
[decisions](https://wiki.sdlc.shared.runwhen.com/#98-decisions)). Decision numbers (D20, D64…) refer to it.

## Using the contracts

```bash
pip install "runwhen-capability @ git+https://github.com/runwhen-contrib/runwhen-capability@contracts-v4"
```

The `contracts-v4` tag is pending: until it exists, pin the commit
(`@<sha>`, the `v13/plugins` merge commit that introduced contracts-v4) in place of `@contracts-v4`.

```python
from runwhen_capability.contracts import archive, check, load_yaml, validate
from runwhen_capability.contracts.models.lease import Lease

# [] when valid; each violation has .path (JSON pointer) and .message
violations = validate("tasks", load_yaml(text))
check("result-envelope", envelope)  # raises ContractError
lease = Lease.model_validate(doc)  # pydantic v2, generated from the schema
tar = archive.build(entries)  # build / read / verify the plugin archive
```

Other languages read the same files at the tag: `contracts/schemas/*.schema.json`
(JSON Schema 2020-12, `$id` `https://schemas.runwhen.com/<name>/v1.json`, all
`$ref`s resolve inside the directory), `contracts/registry-api.openapi.yaml`,
`contracts/archive.md`, and `contracts/vectors/*.json` (inputs and expected
outputs for every hash, so a Go implementation can prove it agrees).
`contracts/examples/` holds a valid example per schema, mostly copied from the
wiki; use them as fixtures instead of writing your own.

**YAML 1.2.** Every plugin file is YAML 1.2 (core schema): `no`, `yes`, `on`
and `off` are strings, `017` is 17, timestamps are strings. PyYAML is YAML 1.1,
so Python consumers parse with `load_yaml` (and `split_frontmatter` for
`SKILL.md`, rules and knowledge); Go's `yaml.v3` is already 1.2. The kubernetes
`node` type's alias `no` is the case that bites.

**Strict.** Every object rejects unknown keys (D65/SK7), except where a map is
open by design: `native`, dependency `params`, error `details`, permission rules,
Agent Skills `metadata`, inventory `hints`.

**Changes.** A seam changes only by a PR to this repo with the affected lanes as
reviewers. Changes after `contracts-v1` are batched into `contracts-v2` at M1,
not trickled. A new tag is additive where it can be; a document's `apiVersion`
or `$id` changes only on an incompatible change.

## The seams

| Seam | Schema | Producer → consumer | Lanes |
|---|---|---|---|
| Plugin manifest | `plugin` | author → SDK, registry, papi | F → A, D, B |
| Integration | `integration` (merge of `integration.yaml`, `types`, `facets`, `dependencies`) | author → SDK, papi | F → A, B, C |
| Skill | `skill-frontmatter`, `tasks` | author → SDK → papi, agent | F → A → B, E |
| Rules, knowledge | `rule-frontmatter`, `knowledge-frontmatter` | author → SDK → papi, agent | F → A → E |
| Compiled plugin | `compiled-plugin` | `rwtask compile`, registry ingest → registry index, papi | A, D → B, D, E |
| Result envelope | `result-envelope`, `error` | script → runner → papi → agent | A, C → B → E |
| Lease | `lease` (embeds `auth-plan`) | papi → runner | B, C → C, D |
| Connection | `connection`, `connections-file`, `credential-inventory`, `auth-plan` | runner ↔ papi ↔ UI | C, G |
| Discovery sync | `rw.discovery.v1` (+ `rw.discovery.summary.v1` section) | discovery task → papi | F → C, B |
| Permission verdicts | `rw.access.v1` | permissionCheck task → papi | F → C |
| Registry | `registry-api.openapi.yaml`, `archive.md` | registry ↔ papi; papi → runner | D → B, C |

## Plugin format

**`plugin.yaml`.** `name` (slug; `workspace` and `runwhen` are reserved),
`version` (semver; the integration and every skill inherit it, D20),
`description`, optional `requires` and `extends` (name → npm-style range;
`requires.platform` is a CalVer range for papi features), `license`. A git tag
`<name>/v<version>` is a release (CAP-13, D73).

**Integration.** At most one per plugin. The loader reads every file under
`integration/` and merges the top-level keys; a key in two files is an error.
`authMethods.<m>.script`, `permissions.schema` and facet `schema` paths are
relative to `integration/`. Types are qualified `<platform>/<type>` everywhere
outside the integration (`appliesTo`, connections, leases); inside it, a bare
name means this platform's type and `parent` may name an extended integration's
type (`kubernetes/namespace`).

| Block | Rules the schema does not carry |
|---|---|
| `types` | Exactly one type is `root` (with `rootKey`); every other type has a `parent` whose chain ends at a root. Path = `platform/<plural>/<name>/…` down the chain. Names and parents never change (D21). The root may also name a `canonicalKey` (camelCase, e.g. Kubernetes `kubeSystemUid`), the identity-bearing value the integration's discovery or permissionCheck task reports for the root; see "Canonical root key" under Discovery sync. A `canonicalKey` on a non-root type is a schema error. |
| `credentialTypes` | Name prefix is the platform (`kubernetes/kubeconfig`), except `generic/*` and the platform-minted `runwhen/sync`. `materialize.env: {fromTaskVars: true}` means the task names the variables (`generic/env`). |
| `credentialTypes` `content` | Optional token naming what the type's material is (`kubeconfig` for `kubernetes/kubeconfig`); used to match references (call F1). |
| `authMethods` | Exactly one of `native` (`passthrough`, `in-cluster-token`) or `script`. `accepts` lists reference kinds and/or content tokens (call F1). `in-cluster-token` only produces a file-materialized type. |
| `permissions` | `schema` is a JSON Schema for one permission in the source system's terms. Every task permission validates against it (INT-2). |
| `entryPoints` | Each `task` is `<skill>/<task>` of a skill in this plugin; `discovery` may carry a cron `schedule`. |
| `recognizers` | `when` is OR across matchers; a matcher ANDs `labels` equality and `doc` equalities (key = JMESPath over the resource's doc). `propose` values may use `{{name}}`, `{{namespace}}`, `{{labels.<k>}}`, `{{doc.<jmespath>}}`. |
| `rules` (dependency) | Today's shape (`shared/resources/packs.py`); `from`/`to.platform` defaults to this platform; `edgeType` is a core type or one in `edgeTypes`. |

The generic integration is built into the platform, not a plugin:
[`contracts/builtin/generic/integration.yaml`](contracts/builtin/generic/integration.yaml),
pinned as `generic@1.0.0` on its connections.

**`SKILL.md`.** Agent Skills keys (`name` = folder name, `description` ≤1024,
`license`, `compatibility`, `metadata`, `allowed-tools` ignored),
`disable-model-invocation`, and one `runwhen:` key with `role`, `appliesTo`
(`types` + optional `labels`, D70) and `tags`. Body under 500 lines. No version,
no owner scope (D71).

**`tasks.yaml`.** `apiVersion: runwhen.com/tasks/v1` (required), `runtime`
(`<name>@<major>`, default `generic@1`), `defaults`, file-level `settings`,
`tasks[]`. Rules beyond the schema:

- `readOnly` is required; `readOnly: false` requires `effects` and `destructive` (D62).
- An input is **required unless it has a `default` or says `required: false`** (call F3). Settings always have a `default` (call F4).
- Credentials: a type name, or a list of names and `{type, vars}`; all of a task's credentials must come from connections on one runner (D46). `runwhen/sync` is platform-minted (D44).
- Output names may not be `status` or `errors`. An output is data (`schema`: a `./schemas/<n>.v<N>.json` file or compact notation), an artifact, or a platform `kind` (`rw.discovery.summary.v1`, `rw.access.v1`).
- `script` is under `scripts/`; Python `main(ctx, **inputs)`, bash via `rw.sh`.

What a script sees (the runner and `rwtask run` set the same names):

| | Python | bash |
|---|---|---|
| Target identity and path | `ctx.resource.<field>`, `ctx.resource.path` | `RW_RESOURCE_<FIELD>`, `RW_RESOURCE_PATH`, `RW_RESOURCE_TYPE` |
| Inputs | keyword arguments (snake_case) | `RW_INPUT_<NAME>` |
| Settings | `ctx.settings.<snake_name>` | `RW_SETTING_<NAME>` |
| Credential | the tool reads its standard form (`$KUBECONFIG`, `PG*`, `REDIS_URL`) | same |
| Dry run | `ctx.dry_run` | `RW_DRY_RUN=1` |
| Contract | `RW_TASK_CONTRACT=v13` | `RW_TASK_CONTRACT=v13` |
| Reference time | `ctx.now()` (aware UTC `datetime`) | `$RW_NOW` (`2026-11-04T09:14:02.123Z`), `$RW_NOW_EPOCH` (integer seconds) |
| Outputs | the returned dict; `ctx.error(code, msg, scope=, hint=, details=)`; `raise RwError(code, msg)` | `rw_output_append <name> <json>`; `rw_output_set <name> <json>`; `rw_error <code> <msg> [scope]`; files in `$RW_OUT` |
| Files | `$RW_OUT` (scratch and artifact files), `$RW_LIB_DIR` | `$RW_OUT`, `$RW_LIB_DIR` |

**`RW_TASK_CONTRACT=v13`** is how a script tells the V13 contract from a classic
bundle's, and both the runner and `rwtask run` set it. Under it `ctx.run` raises
`RwCommandError` (an `RwError` whose code comes from the command's stderr) on a
non-zero exit by default; classic bundles keep returning the exit status unless
`check=True`. An explicit `check=` always wins.

**The run's reference time.** The executor takes one instant when a run starts and
gives it to the script as `RW_NOW` (RFC 3339, UTC, millisecond precision, `Z`) and
`RW_NOW_EPOCH` (the same instant in integer seconds); `ctx.now()` returns `RW_NOW`
parsed (without it, in a classic bundle or a plain unit test, it is the wall clock).
Use it for anything that ends up in a command or an output
(`kubectl annotate ... restarted-at="$RW_NOW"`, "events in the last hour"), so a
replay is reproducible: `rwtask test --record` stores it as the scenario's `now:`
and replay runs the task at that time, so the recorded argv matches exactly (a
scenario without `now` replays with a fresh time). Use `time.monotonic()` (bash:
`$SECONDS`) for durations. A lease carries no reference time: the runner uses the
start of the run. `RW_*` stays reserved: a credential or `extra_env` cannot set it.

**bash.** A task runs under `set -euo pipefail` with `rw.sh` already loaded: call
the helpers without sourcing anything.

| Helper | What it does |
|---|---|
| `rw_output_append <name> <json>` | append one JSON value to a list-shaped output |
| `rw_output_set <name> <json>` | set (replace) an output's value |
| `rw_error <code> <msg> [scope]` | record a structured error (`code` is one of the closed list under Result envelope) and carry on; a clean exit with errors is `partial`, the exit status alone decides `failed` |
| `rw_skip [reason]` | end the task `skipped`, exit 0 |
| `rw_artifact <name> <path>` | register a file as an artifact output (or write it to `$RW_OUT/<name>`) |

`$RW_OUT` is a directory fresh for this task, free to write to; the file named
after an artifact output is what the platform stores. `$RW_LIB_DIR` is the
skill's `lib/` directory, for sourcing shared code (`source "$RW_LIB_DIR/x.sh"`).
Stdout and stderr are logs and are never parsed for outputs.

**Testing a script.** `runwhen_capability.testing` (`make_context`, `run_main`,
`stub_commands`) builds the same context and environment the runner does, so a
Python script's unit test sees `ctx.resource`, `ctx.settings`, `ctx.run` and
`ctx.error` with the V13 semantics and no cluster. `rwtask test` replays
recorded CLI calls through PATH shims, for bash and Python scripts alike.
A skill's `tests/` (the recorded fixtures) is authoring only: `rwtask compile`, `rwtask pull` and the
registry never package it (archive.md section 2), so a script must not read it at run time
(`rwtask validate` warns `W_SCRIPT_READS_TESTS`).

`<NAME>` is the camelCase name in SCREAMING_SNAKE (`latencyThresholdMs` →
`RW_SETTING_LATENCY_THRESHOLD_MS`). Identity fields are the integration's
(`cluster`, `namespace`, `name` for a Kubernetes namespaced type).

Only the top-level setting names are snake_cased (`ctx.settings.latency_threshold_ms`). An
object-typed setting or input is user data: its nested keys arrive verbatim
(`ctx.settings.patterns["connRefused"]`, also `ctx.settings.patterns.connRefused`).

**Rules and knowledge.** `rules/<name>.md` (`name`, `description`; no
`appliesTo`, D59) and `knowledge/<name>.md` (`name`, `description`, optional
`appliesTo`, `resources`, `tags`, D61). File name equals `name`. Budget (D60,
call F9): **a rule body is at most 80 words; a plugin has at most 10 rules.**
`rwtask validate` fails over either; the compiled plugin carries each rule's
word count for the install preview (RK-4).

## Compiled plugin

What `rwtask compile` writes next to the archive and the registry index serves
(`IndexEntry.compiled`); it is not inside the archive, whose sha256 is the
plugin version's one identity. It lets papi validate, match and preview without
unpacking (D29, D23):

- `plugin` as authored; `integration.definition` (merged), with the permission
  schema and facet schemas **inlined**;
- `skills[]`: frontmatter, `SKILL.md` hash, file list, task names (the body stays
  in the archive);
- `tasks[]`: everything resolved: `ref` (`<plugin>:<skill>/<task>`), inherited
  `appliesTo` and `role`, defaults applied, credentials normalized to
  `[{type, vars?}]`, each permission with its canonical `hash`, merged settings,
  inputs with `required` resolved, an `inputSchema` (JSON Schema), outputs with
  their JSON Schema inlined, and the task `hash` (VER-6, archive.md);
- `rules[]` with body and word count; `knowledge[]` with body.

**Every path in the compiled doc is relative to the plugin root**, the same
paths the lease and the archive use: `skills[].files`, `tasks[].script` and any
output `schemaRef` read `skills/papi-health/scripts/api_probe.sh`, never a path
relative to the skill directory (`scripts/api_probe.sh`, which is how
`tasks.yaml` writes it). `tasks.yaml` itself stays skill-relative.

## Result envelope (D64)

```yaml
status: ok | partial | failed | skipped      # derived by the runner
startedAt / finishedAt: RFC 3339             # platform
outputs: { <name>: <value> | {artifact: art_…, truncated: true} }
errors: [{code, message, scope?, hint?, details?}]
```

| Exit | Errors written | `status` |
|---|---|---|
| 0 | none | `ok` |
| 0 | some | `partial` |
| non-zero | any | `failed`; `internal` (from the last stderr lines) is added if none |
| `ctx.skip(reason)` / `rw_skip` | none | `skipped` with `reason` (call F5) |

The schema enforces the table: `ok` and `skipped` have no errors, `partial` and
`failed` have at least one. A timeout is `failed` with `code: timeout`. Error
codes are closed: `permission_denied`, `not_found`, `unreachable`, `timeout`,
`rate_limited`, `storage`, `invalid_input`, `dependency`, `internal`.
Retryability belongs to the code, never the instance. Platform-filled, never
authored: `startedAt`, `finishedAt`, `truncated`, `dryRun`, `provenance` (plugin,
version, task hash, archive, runtime digest, connection, runner; VER-6) and
`logTail` (call F6). The runner scrubs every materialized credential value from
outputs, errors, `logTail` and artifacts before they leave the pod.

## Lease

papi → the selected connection's runner (D46). Carries: the task (`ref`,
plugin, version, skill, name, `hash`, script path from the archive root,
`readOnly`, `dryRun`), the target (`path`, qualified `type`, `identity`,
`labels`; absent for a workspace-wide skill), resolved `inputs` and `settings`,
the runtime (`name`, `major`, mirror-rewritten `image`, `digest`), the code
(`archive {sha256, size, url?}`, fetched by hash through runner-control → papi
→ registry and cached by hash, or `inline` files with the sha256 of the archive
they would build, for a small workspace skill; archive.md §8), one credential entry per credential type
(`{type, connection, authPlan}`, or `{type: runwhen/sync, minted}`), `limits`
(`timeout`, `resources`, `maxResultBytes`) and the compiled `outputs` (so the
runner validates and spills without the index). The runner materializes a
`runwhen/sync` entry to the executor as an ordinary `materialized[]` entry,
`{credentialType: runwhen/sync, files: [{env: RW_SYNC_CREDENTIAL, mode: "0600", content: <the sync credential JSON>}]}`;
`RW_SYNC_CREDENTIAL` is the one RW_* name a credential may set, and only as a file of a
`runwhen/sync` entry (locally, `rwtask run` / `rwtask test --record` pass the caller's own
`$RW_SYNC_CREDENTIAL` to a task that declares `runwhen/sync`).

**Dry run.** Optional top-level `dryRun: true` asks for a dry-run execution: the
runner runs the task with `RW_DRY_RUN=1` (`ctx.dry_run`) and the envelope carries
`dryRun: true`. It is honored only when `task.dryRun` is true; otherwise the runner
runs nothing and completes `failed` with `invalid_input` ("task does not support
dry run"). Absent or `false`, the task runs for real, whatever `task.dryRun` says.

## Connections

**Credential reference** (five kinds, platform code, D67): `k8s-secret`,
`vault`, `file` (all with `ref`, optional `key`), `in-cluster-sa`,
`cloud-identity` (ambient; `ref` optional). `ref` uses the runner's grammar
(`k8s:file@secret/runwhen-local-kubeconfig:kubeconfig`, `k8s:incluster`).
`content` says what the material is (`kubeconfig`, `gcp-sa-key`,
`aws-access-key`, `azure-sp`, `opaque`); `keys` maps variables to keys for
`generic/env`.

**Matching a reference to an auth method** (call F1): `in-cluster-sa` and
`cloud-identity` match by `kind`. A storage kind (`k8s-secret`, `vault`, `file`)
matches by `kind`, and for a `passthrough` method its `content` must also equal
the produced type's `content` (a Secret holding a GCP key never matches the
kubeconfig method). A content token in `accepts` (`gcp-sa-key`) matches a
reference with that `content` on any storage kind.

**Connection record.** `name`, `integration` pinned `<plugin>@<version>`,
`authMethod`, `credentialRef` (never material), `inputs` (configuration, never
secrets), `runner`, **either** `root` (a resource path, D50) **or** `selector`
(generic connections only, D44), `priority`, `state` (`proposed`, `active`,
`paused`, `disabled`), `origin` (`inventory`, `manual`, `declared`), and
`verified {status, at, integration, rules: {<permission hash>: verdict}}`. A
verdict is `granted`, `denied`, `unverified`, or `{verdict: partial, scope: [...]}`
(D68); only `denied` excludes the connection (D29, D46).

**Credential inventory** (CON-2): `{runnerUuid, reportedAt, entries[]}`; each
entry has `id`, `kind`, `ref`, detected `content`, key names (never values),
`version` (the Secret's `resourceVersion`: rotation is detected by version,
never by a content hash), `state` (`ok`, `unreadable`, `invalid`, `missing`),
`errorCode` and non-secret `hints`: kubeconfig contexts `{name, cluster,
server, namespace, authKind}`, `inClusterSa {serviceAccount, namespace,
apiServer}`, `gcpSaKey {clientEmail, projectId}`, `cloudIdentity {provider,
principal}`. Never user names, tokens, keys, certificates or CA data. The
runner's own credentials are never reported. Proposals match entries against
installed auth methods (above); `derived` inputs are filled from `hints`.

**`connections.yaml`** (CON-10): the same fields as the record minus the
lifecycle ones, with `integration` as a plugin name (the installed version is
pinned when papi reconciles).

**Auth plan** (D67). papi compiles one per credential per run from the pinned
method: `credentialType`, `reference`, the `method` (`native`, or `script {path,
archive}` from the integration's plugin archive, which may differ from the
task's plugin), resolved `inputs` (derived ones included) and the credential
type's `materialize` target (for `generic/env`, the task's variables). Executors
are warm pool pods with no per-task init container, so the plan runs before the
task script where credentials already resolve (call F7): a **native** method in
the runner (`Dispatcher.NextTask`), which hands the executor the result as
`materialized[]` on `/v1/tasks/next` (`auth-plan.schema.json#/$defs/materializedCredential`);
a **script** method in the executor as an SDK pre-step. The SDK writes files
under `$RW_CREDENTIALS_DIR` (a memory-backed emptyDir), mode 0600, and deletes
them when the task ends; nothing is cached across task runs. For a script
method the runner also sends the credential reference's material (it holds the
secret; the executor never reads a secret store) as `materialized[].reference`
(`{files: [{key, content, encoding?}]}`, `key` = the secret's key, or `value`
for one unnamed value; contracts-v4), an entry of its own that carries no
`files` or `env`. The SDK writes each file to `$RW_AUTH_REF_DIR/<key>`
(directory 0700, files 0600, per task) and keeps them until the task ends, then
deletes them: the produced credential may point at them (a GKE kubeconfig's
`exec` entry with `GOOGLE_APPLICATION_CREDENTIALS=$RW_AUTH_REF_DIR/key.json`).
The script contract:

| | |
|---|---|
| Runs in | the executor, in the task's runtime image (which ships `gke-gcloud-auth-plugin`, `aws`, `kubelogin`), before the task script; working dir = the integration's plugin root |
| Script | `method.script.path` is relative to the integration's `integration/` directory, exactly as the definition writes it: `auth/gke.py` runs `<archive root>/integration/auth/gke.py`. papi and the runner pass it verbatim; an absolute path, `..` or an empty segment fails the task `invalid_input`. The working dir (above) is the archive root, one level up |
| Interpreter | by extension: `.py` → `python3`, `.sh` → `bash` with `set -euo pipefail` |
| Reference | `RW_AUTH_REF_DIR/<key>` per key of a secret/vault/file reference (`value` when it has one unnamed value), written from `materialized[].reference` and kept until the task ends; nothing for `in-cluster-sa` (standard SA mount) or `cloud-identity` (ambient). A k8s-secret, vault or file reference without `materialized[].reference` fails the task `invalid_input` |
| Inputs | `RW_AUTH_INPUT_<NAME>` |
| Output | file types: write `RW_AUTH_OUT_FILE`; env types: `NAME=value` lines to `RW_AUTH_OUT_ENV` |
| Failure | non-zero exit fails the task run before its script starts (`status: failed`); the auth script may write one `{code, message}` JSON line to `RW_AUTH_OUT_ERROR` (default `internal`, scope `auth`) |

The runtime image has no `gcloud`. A kubeconfig an auth script writes for GKE must call
`gke-gcloud-auth-plugin` with `--use_application_default_credentials` in its `exec.args`
(credentials from `GOOGLE_APPLICATION_CREDENTIALS`); without it the plugin shells out to
`gcloud` and every kubectl call fails.

`passthrough` writes the single reference value to the file target, or maps keys
to variables for env targets. `in-cluster-token` writes a kubeconfig built from
the pod's ServiceAccount token, CA and the `apiServer` input. Locally
(`rwtask run`) no auth method runs: your environment provides the credential
type (D43).

## Discovery sync and permission verdicts

**`rw.discovery.v1`** is today's papi sync API (`/api/v4/workspaces/{ws}/resource-syncs`)
with the V13 changes, so lane C's and B's work is an edit, not a rewrite:

| Call | Body (`$defs`) | Response | V13 change |
|---|---|---|---|
| `POST /resource-syncs` | `open {source, scopePath, mode: full, taskRun?, configHash?, discoveredTypes[]}` | `openResult {syncId, leaseExpiresAt}` | no pack `PUT` first: static types register at install (INT-1); types discovery adds (CRDs) come on `open`; `capabilityRunUuid` → `taskRun` |
| `POST …/{id}/items` | `itemsBatch {seq, items[≤500]}`; an item is `{identity {platform, chain[{type, name}]}, providerUid?, displayName?, labels, document, status?, rollups{facetKey: value}}` | `itemsResult` (`rejected[]` codes `unknown_type`, `bad_name`, `parent_missing`, `out_of_scope`, `bad_rollup`) | none |
| `POST …/{id}/commit` | `commit {partitions[{type, parentPath?, status: complete\|failed\|forbidden\|excluded, count?, reason?}], summary?}` | `commitResult {status: committed\|held, counts, heldReason?}` | `summary` (`rw.discovery.summary.v1`) replaces the ignored `stats` and is stored |
| `POST …/{id}/abort` | — | — | none |

papi mints paths and parents from identity chains (it is the only minter),
derives edges from dependency rules after commit (there is no edges payload),
sweeps unseen resources only under `complete` partitions, and keeps today's
mass-delete guard, 45-minute lease and one-open-sync-per-scope rule. The
schema's root document (`apiVersion: rw.discovery.v1`, `open`, `batches[]`,
`commit`) is a whole recorded sync, used for fixtures and `rwtask test`
replays.

The discovery task authenticates with the **`runwhen/sync`** credential: the
platform mints it per run from the lease (the one D44 exception), and the task
reads the JSON `{apiBaseUrl, token, workspace, expiresAt, scopes}`
(`rw.discovery.v1.schema.json#/$defs/syncCredential`) from the file at
`$RW_SYNC_CREDENTIAL`. The token is bound to the task run and to scope paths at
or under the connection's root.

**`rw.discovery.summary.v1`** is both the discovery task's `summary` output and
the commit's `summary`: `counts` per type, partition totals, discovered type
names, `durationMs`, and per-step `errors[]` (D64 errors, e.g.
`permission_denied` scoped to a namespace), which replace the pack's
`featureGroups`.

**`rw.access.v1`** is the output of an integration's `permissionCheck` task:
one entry per rule it was asked about, `{hash, rule, verdict, scope?,
evidence?}`; `partial` requires `scope` in the integration's own terms
(namespaces for Kubernetes). papi asks with the union of rules declared by the
installed skills (CON-3) and stores the verdicts on the connection.

**Entry-point inputs.** What the platform passes when it runs an integration's
entry-point tasks (agreed with lanes C and F, 2026-11-04):

| Entry point | Lease `inputs` the platform sends | The task declares | Output |
|---|---|---|---|
| `permissionCheck` | `{"rules": [<permission>, …]}`: the deduplicated union of the rules the workspace's installed skills declare for this integration, at most 500 per run (papi batches beyond that), any order | a required input `rules` (`type: object[]`); integration-specific extras must be optional with defaults (Kubernetes: `namespaces`, empty = every namespace the identity can list), and the platform never sends them | `access` of `kind: rw.access.v1`: exactly one entry per input rule, in input order, `hash = permission_hash(rule)`, `rule` echoed; a rule the task cannot evaluate is `unverified`, never dropped. papi recomputes each hash and matches by it |
| `discovery` | `{}`: discovery is driven by settings and the connection's root | settings only (e.g. Kubernetes `namespaces`) | `summary` of `kind: rw.discovery.summary.v1`; resources go through the sync API |
| `cli` | `{"command": "<arguments>"}` | a required input `command` (`type: string`); allowlists live in settings | the integration's own output (text or artifact) |

**Canonical root key** (contracts-v3, call F21). `rootKey` is the provisional
key papi dedupes a root on when a connection is confirmed, before the
integration has run anything. In-cluster and private values collide across
clusters (in-cluster `apiServer` is the service-range IP: 10.96.0.1 on
kind/kubeadm, 10.0.0.1 on AKS), so until a canonical key is reported papi scopes
in-cluster and private keys by runner (`runner:<uuid>|<server>`). When the root
type declares `canonicalKey: <name>`, the integration's first discovery run (the
summary) or permission check (the access report) may report
`root: {canonicalKey: {name, value}}`, with `name` equal to the declared name
(`kubeSystemUid`: the `kube-system` namespace UID, which is the same whichever
path reaches the cluster). papi then:

1. re-keys the connection's provisional root to `<name>=<value>`;
2. if another root already holds that key, re-points the reporting connection to
   it and marks its own old root `superseded`, for an admin to retire. There is
   no automatic subtree merge: both roots may carry children, issues and
   memories.

`rootKey` stays the key until one is reported. A report whose `name` differs
from the root type's declared `canonicalKey`, or for a root type that declares
none, is ignored and logged: papi never re-keys a root on an undeclared name.

## Hashing

All in `runwhen_capability.contracts.hashing`, with vectors in `contracts/vectors/`:

| Function | Rule |
|---|---|
| `canonical_json` | RFC 8785 for these values: sorted keys, no whitespace, UTF-8, no NaN/infinity |
| `permission_hash(rule)` | `sha256:` + sha256 of `canonical_json` of the rule normalized: keys with `null` or `[]` dropped, every list deduplicated and sorted (call F8). `{apiGroups: [""]}` ≠ `{apiGroups: []}`. |
| `archive.build` / `read` / `verify` | the deterministic ustar archive and its sha256 (archive.md §3, §5); golden `.tar` vectors in `contracts/vectors/archive/` |
| `task_hash` | per-task `hash` (archive.md) |

## Registry

[`contracts/registry-api.openapi.yaml`](contracts/registry-api.openapi.yaml)
(OpenAPI 3.1) and [`contracts/archive.md`](contracts/archive.md), frozen from
lane D's proposal. The archive is an **uncompressed ustar whose bytes are fully
determined by the file tree** (taken from git's tree object for the tag), so its
sha256 is the one identity: index entry, lease, `GET /v1/blobs/{sha256}`, runner
cache, snapshot and release manifest. papi syncs `GET /v1/index` (ETag), installs
by hash and verifies it; the runner fetches through runner-control → papi →
registry and caches by hash (D35); runtimes resolve `kubernetes@1` to a
mirror-rewritten image and digest (D34, D36); `POST /v1/uploads` takes one plugin
tree (the no-git path), `GET /v1/snapshot` / `POST /v1/import` move air-gap
snapshots, `…:withdraw` is the kill switch (archives stay fetchable by hash for
in-flight runs, D24). Ingest validates and compiles with this SDK and rejects
with the same findings `rwtask validate` prints (`Problem.details[]`).

## Calls made at freeze (for review)

The wiki left these open or ambiguous; each is the smallest choice that keeps
every wiki example valid.

| # | Call | Why |
|---|---|---|
| F1 | `authMethods.accepts` lists reference **kinds** or **content** tokens; matching per the rule under Connections (lane C's proposal). Credential types may name their `content`. | The wiki's examples mix both (`[k8s-secret, vault, file]` vs `[gcp-sa-key, cloud-identity]`) while D67 says five kinds; without the content check a GCP key Secret would match the kubeconfig method. |
| F2 | `runwhen.role` is one role or a list. | The base skill is discovery, cli and permission-check at once. |
| F3 | An input without `default` is required unless `required: false`. | Matches the examples (`reason`, `command` must be given); "make an input required" is a major (D22). |
| F4 | Every setting has a `default`. | D41 says "defaulted"; a task must run on a fresh install. |
| F5 | `skipped` is a fourth status, with `reason` and no errors. | 42a: a task that can't decide returns `failed` or `skipped`; today's SDK has `ctx.skip`. |
| F6 | Envelope gains optional platform-filled `provenance`, `dryRun`, `logTail`. | VER-6 and CAP-14 need them; adding later would be a v2 change. |
| F7 | Auth plans run before the task script where credentials resolve: native methods in the runner (`materialized[]` to the executor), scripts as an SDK pre-step in the executor, files under `$RW_CREDENTIALS_DIR` deleted when the task ends. Auth scripts live in the plugin archive under `integration/auth/`; the plan carries that archive. | The runner has no per-task pod or init container (lane C); "gone with the task" replaces "gone with the pod" while O10 is open. Keeps auth code hash-verified with its integration; a papi skill (plugin A) uses kubernetes' GKE script (plugin B). |
| F8 | Permission canonicalization treats every list as a set and drops empty keys. | One rule hashed the same by papi, runner and CI, for every integration's permission shape. |
| F9 | Rule budget: 80 words per rule body, 10 rules per plugin. | D60 asked for numbers at freeze; the wiki's example rules are 10–25 words. |
| F10 | The archive is lane D's deterministic uncompressed ustar (no `contentHash`; the compiled plugin lives beside it, not in it). Limits 64 MiB, 10 000 files, 16 MiB per file. | One hash everywhere, independent of zlib; D35 puts limits on the archive. |
| F11 | A connection has `root` xor `selector`; `selector` is for generic connections. | D50 (one root) and D44 (generic scoped by selector). |
| F12 | Recognizer = `from` + `when` (label and JMESPath `doc` equalities, OR across matchers) + `propose`. | No predicate grammar anywhere (D70); page 51's `when:` string sketch predates D68/D69 and is superseded. |
| F13 | `workspace` and `runwhen` are reserved plugin names; the generic integration is built in as `generic@1.0.0`. | `workspace:<skill>` references (SK5); one shared generic definition for lanes C and F. |
| F14 | Workspace skill revisions lease as `skills/<skill>/**` trees, version `0.0.0-rev.<n>`, inline up to 256 KiB with the sha256 of the archive they would build. | One runner path for plugins and workspace skills (D35 allows inline). |
| F15 | No author `retries` field: the platform retries infrastructure failures, and only for `idempotent: true` tasks (CAP-7). | 42a: "idempotent is a hint; the platform retries infrastructure failures only". |
| F16 | Facet populator kinds are `projection`, `rollup` (a discovery-task output) and `agent`; `schema` is required. | D66/IN6; today's `task` kind becomes `rollup`. |
| F17 | `apiVersion` is required only in `tasks.yaml`; optional elsewhere. | The wiki's `plugin.yaml` and one-file integration examples omit it. |
| F18 | Claude Code's extra frontmatter keys (`argument-hint`, `model`, `user-invocable`) are rejected. | SK7 strict; add them in v2 if a skill needs one. |
| F19 | Plugin files are YAML 1.2; `load_yaml` is the reference loader for Python. | YAML 1.1 turns the kubernetes alias `no` into `false`; every lane must parse the same way. |
| F20 | `rw.discovery.v1` keeps today's open/items/commit API: no edges payload (rule-derived), discovered types move from the pack `PUT` to `open`, `summary` on commit replaces `stats`, `taskRun` replaces `capabilityRunUuid`. | Smallest change to working code; static types now register at install. |
| F21 | Kubernetes `rootKey: server` is provisional: an in-cluster and a GKE connection to one cluster report different servers, so connection confirm takes `attachToRoot` (lane C). Resolved by `contracts-v3`: the root type may declare `canonicalKey` (Kubernetes: `kubeSystemUid`, the `kube-system` namespace UID) and discovery or the permission check reports it (see "Canonical root key"). | e2e #3 needs two connections on one root. |

## Not in v1

Findings and issues (D63); relationship predicates in `appliesTo` (D70);
cross-source links and `entryPoints.ingest` (O15); archive signing; settings
resolution below the workspace (O16 changes no schema); plugin automations and
agent tools (D16).

## Changes in contracts-v2

Additive: no field was removed and every document that validated under
`contracts-v1` still does (`$id`s stay `.../v1.json`).

| # | Change | Affects |
|---|---|---|
| V2-1 | `compiled-plugin`'s `plugin` also accepts the synthetic workspace plugin `{name: workspace, version: 0.0.0-rev.<n>, description}`; `compile_tree` / `compile_entries` take `workspace_revision=` and compile a skill-only tree (refs `workspace:<skill>/<task>`, `integration: null`). `plugin.schema` is unchanged: authors still cannot name a plugin `workspace`. | B (ingest and lease of workspace skills), C |
| V2-2 | Every path in a compiled-plugin document is plugin-root-relative (`skills/<skill>/scripts/x.sh`); the `synthetic-runwhen-platform-ops` example said skill-relative. Stated under "Compiled plugin"; a test checks every compiled-plugin example. | D (used the example as a template), B |
| V2-5 | "What a script sees" names `RW_TASK_CONTRACT=v13`, the bash helpers (`rw_error`, `rw_output_append`, `rw_output_set`, ...), `$RW_OUT`, `$RW_LIB_DIR`, strict mode with `rw.sh` preloaded, and `runwhen_capability.testing`. Documentation only. | F (skill authors) |
| V2-6 | S13: plugin-authored JSON Schemas (task input `pattern`, settings, output, facet and permission schemas, compact notation) are evaluated with RE2, not Python `re`: new dependency `google-re2`, `contracts.validator_for_plugin_schema(schema)` and `contracts.check_plugin_schema(schema)`, and `rwtask validate` / ingest report `E_SCHEMA_PATTERN` (a pattern RE2 cannot compile: lookaround, backreferences) and `E_SCHEMA_REF` (a `$ref` that leaves the schema). Plugins that used lookaround in a pattern must rewrite it. | B (papi re-validation, CAP-5), D (registry ingest), F (authors) |
| V2-7 | `lease.dryRun` (optional boolean): papi asks for a dry-run execution. The runner runs with `RW_DRY_RUN=1` and marks the envelope `dryRun: true`, only if `task.dryRun` is true; else it completes `failed` with `invalid_input` "task does not support dry run". | B, E (CAP-14 approval card), C |

## Changes in contracts-v3

Additive: no field was removed and every document that validated under
`contracts-v2` still does (`$id`s stay `.../v1.json`).

| # | Change | Affects |
|---|---|---|
| V2-3 | Canonical root key. `types.yaml`: a root type may declare `canonicalKey: <camelName>` (Kubernetes: `kubeSystemUid`); on a non-root type it is a schema error. `common.schema.json#/$defs/rootIdentity` is `{canonicalKey: {name, value}}`; `rw.discovery.summary.v1` and `rw.access.v1` each gain an optional top-level `root` of that shape. papi re-keys the root to `<name>=<value>`; if another root holds that key, it re-points the reporting connection and marks its own old root `superseded` (no automatic subtree merge). Compile carries `canonicalKey` through in `integration.definition`; no semantic check beyond the schema. Settles F21. | C (papi root dedupe), F (kubernetes discover and check-permissions report it) |
| V3-1 | A skill's `skills/<skill>/tests/**` is dev-only and never packaged, like the root `tests/`, `runwhen.lock` and `.runwhen/`: `rwtask compile` and `rwtask pull` drop it (silently; not in the archive nor `skills[].files`; it stays an allowed authoring path), `rwtask test` reads fixtures from the plugin directory, and `rwtask validate` warns `W_SCRIPT_READS_TESTS` when a script or `lib/` file mentions a `tests/` path. archive.md section 2 and 3a say so. The registry must apply the same exclusion when it builds or re-normalises an archive, or the sha256 differs between builders. | D (registry ingest, upload re-normalisation), F (runwhen-plugins skill authors) |

## Changes in contracts-v4

Additive: no field was removed and every document that validated under
`contracts-v3` still does (`$id`s stay `.../v1.json`).

| # | Change | Affects |
|---|---|---|
| V4-1 | Script auth plans get their reference material. `auth-plan.schema.json#/$defs/materializedCredential` gains an optional `reference: {files: [{key, content, encoding?}]}` (`key` = a secret's key, or `value`; `minItems` 1; strict), which excludes `files` and `env` in the same entry. The runner sends it on `/v1/tasks/next` for a plan whose method is a `script` and whose reference is a `k8s-secret`, `vault` or `file`; the SDK writes it to `$RW_AUTH_REF_DIR/<key>` for the auth script and keeps it until the task ends (see "Auth plan"). `rwtask serve` now runs script auth plans before the task script (`RW_AUTH_REF_DIR`, `RW_AUTH_INPUT_<NAME>`, `RW_AUTH_OUT_FILE` / `RW_AUTH_OUT_ENV`, `RW_AUTH_OUT_ERROR`), which is behaviour, not schema. | C (runner: send `reference`; CON-5 GKE auth), D (relay serves the auth archive), F (kubernetes `gke` auth script) |
