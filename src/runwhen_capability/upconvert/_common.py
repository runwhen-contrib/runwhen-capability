"""Pieces both up-converters share: the result type, naming, SKILL.md and YAML
rendering, the appliesTo / credential / schema-file mappings, and writing a result
to disk. Nothing here knows either source format's layout."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..custom.diagnostics import Diagnostic

W_UPCONVERT_LOSSY = "W_UPCONVERT_LOSSY"
W_UPCONVERT_REVIEW = "W_UPCONVERT_REVIEW"
W_UPCONVERT_CREDENTIAL = "W_UPCONVERT_CREDENTIAL"
W_UPCONVERT_SETUP_DROPPED = "W_UPCONVERT_SETUP_DROPPED"

TASKS_API_VERSION = "runwhen.com/tasks/v1"

#: The kinds up-conversion knows a V13 credential type for. Everything else (a custom
#: `secret`, a packaged `github.installation`, ...) becomes `generic/env`.
CREDENTIAL_TYPE_FOR_KIND = {
    "k8s.kubeconfig": "kubernetes/kubeconfig",
    "runwhen.resourceSync": "runwhen/sync",
}
KUBECONFIG_TYPE = "kubernetes/kubeconfig"
GENERIC_ENV = "generic/env"

#: V13 output names the contract rejects (D63: no findings, no status of its own).
RESERVED_OUTPUT_NAMES = frozenset({"status", "errors"})

_NAME_PART = re.compile(r"[^A-Za-z0-9]+")
_V13_NAME = re.compile(r"^[a-z][A-Za-z0-9]*$")
_SCHEMA_FILE = re.compile(r"schemas/[^/]+\.v[0-9]+\.json")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_LABEL_KEY = "labels."
_VALUE_TYPE = re.compile(r"^(string|integer|number|boolean|duration|datetime)(\[\])?$")
_ENUM_TYPE = re.compile(r"^enum\((.*)\)(\[\])?$")
_MAX_DESCRIPTION = 1024


class UpconvertError(ValueError):
    """The source cannot be read as the format it claims to be (no manifest, not valid
    YAML, a task file that is not there). Content problems that merely lose
    information are `Diagnostic`s on the result instead."""


@dataclass
class UpconvertResult:
    """A V13 skill directory, in memory: `files` maps relative POSIX paths to bytes,
    `executable` is the subset that carries the exec bit. Never written by the
    converter itself (see `write_result`)."""

    skill_name: str
    files: dict[str, bytes]
    executable: frozenset[str]
    diagnostics: list[Diagnostic] = field(default_factory=list)


class Diagnostics:
    """Collects findings, dropping an exact repeat (same code, file, path, message), or a
    repeat of a `once` key."""

    def __init__(self, file: str) -> None:
        self.file = file
        self.items: list[Diagnostic] = []
        self._seen: set[tuple] = set()

    def add(
        self,
        code: str,
        path: str | None,
        message: str,
        *,
        file: str | None = None,
        once: object = None,
    ) -> None:
        """`once` names what the finding is about: a second finding of the same code, file
        and `once` is dropped whatever its path (one per file and name, not per use)."""
        where = file or self.file
        key = (code, where, path, message) if once is None else (code, where, "once", once)
        if key in self._seen:
            return
        self._seen.add(key)
        self.items.append(
            Diagnostic(code=code, severity="warning", file=where, path=path, message=message)
        )


# -- naming ------------------------------------------------------------------------


def slug(name: str) -> str:
    """A V13 slug for a source name: lower case, `_` and runs of other separators to `-`."""
    return _NAME_PART.sub("-", name.strip()).strip("-").lower()


def title_case(name: str) -> str:
    return _NAME_PART.sub(" ", name).strip().title()


def camel_case(name: str) -> str:
    """`inline_count` -> `inlineCount`, `Name` -> `name`, `DRY_RUN` -> `dryRun`. A name that
    already fits the V13 `^[a-z][A-Za-z0-9]*$` is returned unchanged."""
    if _V13_NAME.match(name):
        return name
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", name) if p]
    if not parts:
        return name
    out = []
    for i, part in enumerate(parts):
        word = part.lower() if part.isupper() else part
        out.append(word[:1].lower() + word[1:] if i == 0 else word[:1].upper() + word[1:])
    result = "".join(out)
    return result if result[:1].isalpha() else "x" + result[:1].upper() + result[1:]


def env_name(name: str) -> str:
    """camelCase -> SCREAMING_SNAKE (`statsDsn` -> `STATS_DSN`), as the runtime names inputs."""
    return _CAMEL_BOUNDARY.sub("_", name).upper()


def v13_name(
    name: str,
    taken: set[str],
    diags: Diagnostics,
    path: str,
    what: str,
    *,
    reserved: frozenset[str] = frozenset(),
) -> str:
    """The V13 name for an input or output called `name`: camelCase, not reserved, not
    already `taken`. A renamed one is a W_UPCONVERT_REVIEW (the script still uses the old
    name). Records the result in `taken`."""
    new = camel_case(name)
    if new in reserved:
        new += "Detail"
    candidate, n = new, 2
    while candidate in taken:
        candidate = f"{new}{n}"
        n += 1
    taken.add(candidate)
    if candidate != name:
        diags.add(
            W_UPCONVERT_REVIEW,
            path,
            f"{what} {name!r} is renamed {candidate!r} (V13 names are camelCase and may not be "
            "status or errors); update the script to the new name",
            once=(what, name),
        )
    return candidate


# -- rendering ---------------------------------------------------------------------


def dump_yaml(doc: Any) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)


def render_skill_md(frontmatter: dict, body: str) -> bytes:
    text = f"---\n{dump_yaml(frontmatter)}---\n\n{body.strip(chr(10))}\n"
    return text.encode("utf-8")


def note_generated_titles(diags: Diagnostics, count: int) -> None:
    """One W_UPCONVERT_REVIEW per skill: every task title is the task name title-cased."""
    if count:
        diags.add(
            W_UPCONVERT_REVIEW,
            "tasks",
            f"the title of each of the {count} task(s) is generated from its name "
            "(V13 tasks have a title); write real ones",
        )


def description_or(
    value: str | None,
    fallback: str,
    diags: Diagnostics,
    path: str,
    what: str,
    *,
    missing: list[str] | None = None,
) -> str:
    """`value`, or `fallback` when it is empty. The stand-in is a W_UPCONVERT_REVIEW at
    `path`, unless `missing` is given: then `what` is appended to it for
    `note_missing_descriptions` to report once."""
    text = (value or "").strip()
    if not text:
        if missing is not None:
            missing.append(what)
        else:
            diags.add(
                W_UPCONVERT_REVIEW,
                path,
                f"{what} has no description; {fallback!r} stands in (V13 requires one)",
            )
        return fallback
    return text[:_MAX_DESCRIPTION]


def note_missing_descriptions(diags: Diagnostics, missing: list[str]) -> None:
    """One W_UPCONVERT_REVIEW per skill for every task description that is missing."""
    if missing:
        diags.add(
            W_UPCONVERT_REVIEW,
            "tasks",
            f"{len(missing)} task(s) have no description ({', '.join(missing)}); the title "
            "stands in (V13 requires one)",
        )


# -- appliesTo ---------------------------------------------------------------------


def applies_to_block(
    entries: list[dict], diags: Diagnostics, base: str = "appliesTo"
) -> dict | None:
    """`[{platform, type, where}]` -> `{types, labels?}`, or None for no entries. Labels come
    from `where.labels.<k>`. Entries that differ in labels, or a `where` key that is not a
    label (`name`, `namespace`, `language`, anything else), cannot be expressed: that is
    W_UPCONVERT_LOSSY at the entry and only the types are kept."""
    types: list[str] = []
    label_sets: list[dict[str, str]] = []
    lossy = False
    for i, entry in enumerate(entries):
        ref = f"{str(entry['platform']).lower()}/{str(entry['type']).lower()}"
        if ref not in types:
            types.append(ref)
        labels: dict[str, str] = {}
        for key, value in (entry.get("where") or {}).items():
            if str(key).startswith(_LABEL_KEY) and len(str(key)) > len(_LABEL_KEY):
                labels[str(key)[len(_LABEL_KEY) :]] = str(value)
            else:
                lossy = True
                diags.add(
                    W_UPCONVERT_LOSSY,
                    f"{base}[{i}].where.{key}",
                    f"`where.{key}` has no V13 equivalent (appliesTo carries types and labels "
                    "only); the skill will match every resource of the type",
                )
        label_sets.append(labels)
    if not types:
        return None
    if any(labels != label_sets[0] for labels in label_sets[1:]):
        lossy = True
        for i, labels in enumerate(label_sets[1:], start=1):
            if labels != label_sets[0]:
                diags.add(
                    W_UPCONVERT_LOSSY,
                    f"{base}[{i}].where",
                    "appliesTo entries differ in labels, which V13 cannot express per type; "
                    "keeping the types and dropping the labels",
                )
    block: dict[str, Any] = {"types": types}
    if not lossy and label_sets and label_sets[0]:
        block["labels"] = label_sets[0]
    return block


# -- credentials -------------------------------------------------------------------


def credential_entry(kind: str | None, input_name: str) -> str | dict:
    """The V13 credential for a source credential of `kind` called `input_name`: a type
    name, or `{type: generic/env, vars: [NAME]}`."""
    mapped = CREDENTIAL_TYPE_FOR_KIND.get(kind or "")
    if mapped:
        return mapped
    return {"type": GENERIC_ENV, "vars": [env_name(input_name)]}


def merge_credentials(entries: list[str | dict]) -> list[str | dict]:
    """Deduplicate named types and fold every generic/env entry into one with all the vars."""
    named: list[str] = []
    env_vars: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            if entry not in named:
                named.append(entry)
        else:
            env_vars += [v for v in entry["vars"] if v not in env_vars]
    merged: list[str | dict] = list(named)
    if env_vars:
        merged.append({"type": GENERIC_ENV, "vars": env_vars})
    return merged


def credentials_value(entries: list[str | dict]) -> str | list | None:
    """The `credentials:` value for merged entries: absent, a lone name, or a list."""
    if not entries:
        return None
    if len(entries) == 1 and isinstance(entries[0], str):
        return entries[0]
    return entries


def credential_runtime(entries: list[list[str | dict]]) -> str:
    kube = any(KUBECONFIG_TYPE in group for group in entries)
    return "kubernetes@1" if kube else "generic@1"


# -- input types -------------------------------------------------------------------


def input_type(spec_type: str) -> tuple[dict | None, str | None]:
    """A source input type string as V13 `{type, enum?}` fields, or (None, reason) when it has
    no V13 equivalent."""
    if _VALUE_TYPE.match(spec_type):
        return {"type": spec_type}, None
    match = _ENUM_TYPE.match(spec_type)
    if match:
        values = [v for v in match.group(1).split("|") if v]
        if values:
            return {"type": "enum" + (match.group(2) or ""), "enum": values}, None
    return None, f"type {spec_type!r} has no V13 equivalent"


# -- schema files ------------------------------------------------------------------


class SchemaFiles:
    """Resolves an output's `./<path>.json` schema reference. V13 names an output schema
    file `schemas/<name>.v<N>.json`; a source file that is not named that way is copied as
    `schemas/<stem>.v1.json` and the reference follows it (W_UPCONVERT_REVIEW). The renamed
    source path is not copied a second time."""

    def __init__(self, files: dict[str, bytes], diags: Diagnostics) -> None:
        self.files = files
        self.diags = diags
        self.moved: dict[str, str] = {}
        self._taken = {p for p in files if p.startswith("schemas/")}

    def resolve(self, value: str, path: str) -> str:
        if not value.startswith("./"):
            return value
        source = posixpath.normpath(value[2:])
        if source in self.moved:
            return "./" + self.moved[source]
        if source not in self.files or _SCHEMA_FILE.fullmatch(source):
            return value
        stem = posixpath.basename(source)
        stem = stem[: -len(".json")] if stem.endswith(".json") else stem
        n = 1
        new = f"schemas/{stem}.v{n}.json"
        while new in self._taken:
            n += 1
            new = f"schemas/{stem}.v{n}.json"
        self._taken.add(new)
        self.moved[source] = new
        self.diags.add(
            W_UPCONVERT_REVIEW,
            path,
            f"schema file {source} is copied as {new} (V13 names output schemas "
            "schemas/<name>.v<N>.json)",
        )
        return "./" + new


# -- writing -----------------------------------------------------------------------


def write_result(result: UpconvertResult, dest: Path) -> None:
    """Write `result.files` under directory `dest` (created), setting the exec bit on
    `result.executable`."""
    dest = Path(dest)
    for rel, data in result.files.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if rel in result.executable else 0o644)
