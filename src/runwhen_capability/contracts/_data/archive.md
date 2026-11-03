# Plugin archive format (contracts-v1)

DLV-0, frozen from lane D's proposal. Normative for the registry (lane D), papi (lanes B, D), the runner (lanes C, D) and `rwtask compile` (lane A).
Decisions it implements: D20 (one semver per plugin), D34 (code is a content-addressed archive),
D35 (the lease carries the archive hash), D36 (air-gap snapshot checked against the release manifest).

## 1. What an archive is

One plugin version = one archive. The archive is an **uncompressed POSIX ustar tar** whose bytes
are fully determined by the plugin's file tree, so two registries that build the same tag produce
the same bytes and therefore the same hash.

**Identity:** the SHA-256 of the archive bytes, written `sha256:<64 lowercase hex>` in every JSON document (lane A `common.schema.json#/$defs/sha256`) and as the bare 64-hex in URL paths and file names.
This one value is used everywhere: the index entry's `archive.sha256`, the lease's `archive.sha256`,
`GET /v1/blobs/{sha256}`, the runner's cache key, the snapshot's `blobs/sha256/<hex>` path and the
release manifest. (The SDK's `content_hash`, `sha256:`-prefixed and computed over files, is a
different value and is never used to address an archive.)

A `(name, version)` maps to exactly one `sha256`, forever. A source that later offers different
bytes for the same version is rejected with `E_VERSION_CONFLICT`; the first archive stays.

## 2. Tree: which files go in

| Source | Root of the archive |
|---|---|
| `runwhen-plugins` (public, D73) | `plugins/<name>/` at the commit of tag `<name>/vX.Y.Z` |
| Private git source | the source's configured `path` (default: repo root, or `plugins/<name>/` when the repo uses the D73 layout) at the commit of the tag |
| Upload | the uploaded tree as given (an uploaded archive is re-normalised by these rules; its hash is the normalised one) |

- Files are taken from **git's tree object for the tagged commit** (`git ls-tree -r`), never from a
  working copy: untracked and ignored files can never leak in.
- There is **no wrapper directory**: `plugin.yaml` is at the archive root, next to `integration/`,
  `skills/`, `rules/`, `knowledge/`, `runtimes/`-free (runtimes are images, D34).
- **Excluded**: at the archive root, `tests/` (recorded fixtures are CI evidence and can be
  large; no consumer of the archive needs them), `runwhen.lock` and `.runwhen/` (the author's pinned
  dependencies and editor schemas written by `rwtask pull`, AUT-1; contracts-v3 addendum); and in
  every skill, `skills/<skill>/tests/` (recorded fixtures are dev-only; `rwtask test` reads them from
  the plugin directory, never from an archive; contracts-v3 addendum); and nothing else.
  `rwtask compile`, `rwtask pull` and the registry apply the same exclusions, so the archive sha256
  is one identity. A skill's `tests/` stays an allowed authoring path (no `E_PATH_NOT_ALLOWED` for it
  on disk); a submitted archive that contains one is re-normalised by dropping it.
- **Rejected** (`E_INVALID_PLUGIN`, the version is not ingested): symlinks, submodules (gitlinks),
  any path that after normalisation is absolute, contains `..`, `\`, a NUL or a control character,
  is not valid UTF-8 NFC, exceeds 255 bytes, or collides case-insensitively with another path.

## 3. Bytes: how the tar is written

1. **Entries:** one regular-file entry (`typeflag '0'`) per file. **No directory entries**
   (consumers create parent directories). Order: by path, comparing the UTF-8 bytes.
2. **Header fields:**
   - `name`/`prefix`: the relative path, split at a `/` per ustar rules (`name` ≤ 100 bytes,
     `prefix` ≤ 155 bytes, the longest `prefix` that fits). A path that cannot be split is rejected.
     Python 3.12's `tarfile` picks the *shortest* valid prefix, so a writer implements the
     longest-prefix rule itself; the golden vectors include a path with several valid splits.
   - `mode`: `0000755` if git records the file as executable (`100755`), else `0000644`.
   - `uid`, `gid`, `mtime`: `0`. `devmajor`, `devminor`: all NUL, as `tarfile` writes them for
     regular files (readers see 0). `uname`, `gname`, `linkname`: empty.
   - `magic` `ustar\0`, `version` `00`; numeric fields octal, zero-padded, NUL-terminated, as
     Python's `tarfile.USTAR_FORMAT` writes them.
3. **Content:** the file's bytes as stored in git (no line-ending conversion), padded with zeros to a
   multiple of 512.
4. **End:** two 512-byte zero blocks, then zero padding to a multiple of 10240 bytes (the ustar
   default record size, which is what both Python `tarfile` and GNU tar emit).
5. No compression. Transport may add `Content-Encoding: gzip`; the hash is always over the
   uncompressed tar.

The SDK ships the reference implementation (`runwhen_capability.contracts.archive`: `build(entries) ->
bytes`, `read(bytes) -> entries`, `verify(bytes, sha256)`) with golden vectors in `contracts/vectors/archive/`
(each case's entries and its exact `.tar` bytes); the registry and `rwtask` both call it, and the runner's Go
extractor is tested against the same vectors.

## 3a. Layout inside the archive

```text
plugin.yaml                     required (plugin.schema.json)
README.md  LICENSE  CHANGELOG.md   optional
connections.yaml                optional (connections-file.schema.json; private plugins, CON-10)
integration/                    at most one integration per plugin (D16)
  integration.yaml              integration.schema.json; may hold every block (one-file integration)
  types.yaml                    types.schema.json
  facets.yaml                   facets.schema.json
  dependencies.yaml             dependencies.schema.json
  schemas/**                    facet value schemas and the permission schema, <key>.v<N>.json, immutable
  auth/**                       auth method scripts (authMethods.<m>.script is relative to integration/)
skills/<skill>/                 folder name = skill name
  SKILL.md                      required
  tasks.yaml                    only if the skill runs tasks
  scripts/**  lib/**  schemas/**  references/**  assets/**
  tests/**                      authoring only, not packaged (section 2)
rules/<name>.md                 rule-frontmatter.schema.json; file name = name
knowledge/<name>.md             knowledge-frontmatter.schema.json; file name = name
```

Any other path is rejected (`E_INVALID_PLUGIN`, SDK code `E_PATH_NOT_ALLOWED`). A top-level key of the
integration may appear in only one of the four integration files; the loader merges them into one document
that validates against `integration.schema.json`. Every YAML file is YAML 1.2 (CONTRACTS.md). The compiled
plugin (`compiled-plugin.schema.json`) is **not** in the archive: `rwtask compile` writes it next to the
archive and the registry stores it in the index entry.

## 4. Limits

| Limit | Value | Error |
|---|---|---|
| Archive size | 64 MiB | `E_ARCHIVE_TOO_LARGE` |
| Files | 10 000 | `E_ARCHIVE_TOO_LARGE` |
| One file | 16 MiB | `E_ARCHIVE_TOO_LARGE` |

Plus the rule budget (D60): at most 10 rules per plugin and 80 words per rule body (`E_INVALID_PLUGIN`).

These replace the inline-bundle caps (40 files, 64 KiB per file, 256 KiB total) for anything that
travels by hash. Workspace skills that still go inline (D35) keep the old caps.

## 5. Consumers: verify, then extract

Every consumer (papi on install, the runner on fetch, the registry on import) must:

1. Hash the bytes **as they stream in** and reject the whole archive on mismatch with the expected
   `sha256` (`E_HASH_MISMATCH`) before writing anything outside a temp location.
2. Extract with the same rules it would have produced: accept only `typeflag '0'` (and `'5'`
   directory entries from older tools, ignored), reject absolute paths, `..`, symlinks, hardlinks
   and devices, and ignore `uid`/`gid`/`mtime`. Never trust the tar to be well-formed because the
   hash matched: the hash proves origin, the rules prove safety.
   **Reject every extension header** (pax `x`/`g`, GNU `L`/`K`/`S`, sparse maps): a generic tar
   library folds a pax global header into the next member, so a member that looks like a regular
   file can carry another path and a declared size far beyond its bytes (a 47 KiB archive expanded
   to 480 MiB in lane D's review). Parse plain ustar headers directly, or with a library check that
   every member's data starts 512 bytes after its own header and right where the previous member's
   padded data ended, with no pax headers and no sparse map. The size limits (§4) apply to the
   declared sizes as well as the bytes. Vectors: `contracts/vectors/archive/invalid/`.
3. Cache by `sha256`; a cached archive is never re-validated against a different hash and never
   mutated (the runner extracts into `<cache>/sha256/<hex>/` and treats it as read-only).

## 6. Snapshot (air-gap, D36)

A snapshot is how public plugins reach an install with no egress: a release ships one, and an
operator can import a newer one between releases.

```
snapshot.json                         the manifest (below)
index/<name>/<version>.json           index entries (registry-api.openapi.yaml#/components/schemas/IndexEntry)
runtimes.json                         the runtimes index, with upstream (not rewritten) image refs
blobs/sha256/<hex>                    each archive, named by its sha256
```

`snapshot.json`:

```yaml
apiVersion: registry.runwhen.com/snapshot/v1
createdAt: "2026-11-20T00:00:00Z"
release: "2026.11.1"            # the platform release that ships it, or null for an ad-hoc export
source: public                  # which source the plugins are attributed to on import
plugins:
  - {name: kubernetes, version: 1.4.0, sha256: "<hex>", size: 81920}
runtimes:
  - {name: kubernetes, major: 1, digests: {linux/amd64: "sha256:<hex>", linux/arm64: "sha256:<hex>"}}
```

- The snapshot file itself is a tar (optionally gzip-compressed, `.tar.gz`) of these paths, in that
  order, written with the same header rules as §3. Its own sha256 is what the platform release
  manifest records.
- **Import** checks every blob against `snapshot.json`, every index entry's `archive.sha256` against
  its blob, and, when the operator supplies the release manifest's snapshot hash, the snapshot's
  own hash. Any mismatch rejects the whole import. Import is idempotent: versions already present
  with the same hash are skipped, a different hash is `E_VERSION_CONFLICT`.
- Runtime images are not in the snapshot; they come through the image mirror and the runtimes index
  rewrites their references on read (registry-api `GET /v1/runtimes`).

## 7. Per-task hash (VER-6)

Each compiled task carries `hash`, which changes when anything the task runs changes and only then
(upgrade previews diff by it, D20, D24; provenance records it, VER-6):

```text
files = the task's script + every file under its skill's lib/ + every schema file its outputs reference
hash  = "sha256:" + sha256(canonical_json({"task": <compiled task without "hash">,
                                           "files": {path: sha256(data).hexdigest()}})).hexdigest()
```

`canonical_json` is RFC 8785 (`runwhen_capability.contracts.canonical_json`); vectors in
`contracts/vectors/task-hash.json`.

## 8. Workspace skills

A workspace skill revision is not published to a registry (D30). papi packages it as a plugin-shaped tree
with only `skills/<skill>/**` (no `plugin.yaml`), plugin name `workspace`, version `0.0.0-rev.<revision>`,
built with the same §3 rules. A tree of at most 256 KiB (40 files, 64 KiB per file) goes inline in the lease
(`code.inline`, with the sha256 of the archive those files would build); a larger one is stored and leased by
hash like a plugin.
