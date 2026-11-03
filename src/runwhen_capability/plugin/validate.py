"""Validate a loaded plugin tree: every document against its contracts-v1 schema,
then the rules a schema cannot express (CONTRACTS.md "Plugin format").

`validate_tree(tree)` returns the loader's diagnostics first, then:

Schema
- E_SCHEMA -- a document breaks its schema; `file` is the document's file (for the
  merged integration, the file the offending top-level key came from) and `path`
  the JSON pointer into the document.

Semantic (errors unless noted)
- E_PLUGIN_MISSING -- no plugin.yaml in a tree that is more than workspace skills.
- E_SKILL_NAME_MISMATCH / E_FILE_NAME_MISMATCH -- frontmatter `name` differs from
  the skill folder / the rule or knowledge file stem.
- E_TASK_NAME_DUPLICATE -- two tasks of one skill share a name.
- E_SCRIPT_MISSING -- a task's `script` is not a file under the skill's scripts/.
- E_OUTPUT_SCHEMA_MISSING / E_SCHEMA_FILE_NAME / E_SCHEMA_NOTATION -- an output's
  `./schemas/...` file is absent, not named `schemas/<name>.v<N>.json`, or its
  compact notation does not compile.
- E_ENTRY_POINT_UNKNOWN -- an integration entry point names a task this plugin
  does not have.
- E_UNKNOWN_TYPE -- an `appliesTo` type of this plugin's own platform that its
  `types` do not declare. W_TYPE_UNCHECKED (warning) -- a type or credential type
  of a `requires`/`extends` platform, which cannot be checked without the other
  integration (a workspace skill, with no plugin.yaml, is unchecked throughout).
  E_UNKNOWN_PLATFORM -- a platform that is neither this integration's nor named in
  `requires`/`extends`.
- E_CREDENTIAL_TYPE_UNKNOWN -- a credential type this integration does not
  declare (`generic/*` is always known; `runwhen/sync` only on the integration's
  discovery entry-point task).
- E_PERMISSION_SCHEMA -- a task permission fails the integration's own permission
  schema, or that schema cannot be loaded. Only tasks that use one of this
  integration's credential types are checked; the others rely on a required
  integration (INT-2 adds that lookup).
- E_TYPE_CHAIN -- an integration type whose parent chain does not end at the one
  root (unknown parent, cycle), or the integration does not have exactly one root.
  An integration that `extends` another may have none: its types chain to a
  qualified parent of the extended platform.
- E_FACET_SCHEMA_MISSING / E_AUTH_SCRIPT_MISSING -- a facet `schema` or an auth
  method `script` is not a file under integration/.
- E_RULE_BUDGET -- more than 10 rules, or a rule body over 80 words (whitespace-
  separated tokens).
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Iterable
from typing import Any

import yaml
from jsonschema import exceptions as jsonschema_exceptions
from jsonschema.validators import validator_for

from ..contracts import TreeEntry, load_yaml, validate
from ..custom.diagnostics import Diagnostic
from ..custom.schema_notation import SchemaNotationError, compile_schema_notation
from .tree import E_SKILL_MD_MISSING, E_YAML, INTEGRATION_FILES, PluginTree, load_entries

E_SCHEMA = "E_SCHEMA"
E_PLUGIN_MISSING = "E_PLUGIN_MISSING"
E_SKILL_NAME_MISMATCH = "E_SKILL_NAME_MISMATCH"
E_FILE_NAME_MISMATCH = "E_FILE_NAME_MISMATCH"
E_SCRIPT_MISSING = "E_SCRIPT_MISSING"
E_OUTPUT_SCHEMA_MISSING = "E_OUTPUT_SCHEMA_MISSING"
E_SCHEMA_NOTATION = "E_SCHEMA_NOTATION"
E_SCHEMA_FILE_NAME = "E_SCHEMA_FILE_NAME"
E_ENTRY_POINT_UNKNOWN = "E_ENTRY_POINT_UNKNOWN"
E_UNKNOWN_TYPE = "E_UNKNOWN_TYPE"
W_TYPE_UNCHECKED = "W_TYPE_UNCHECKED"
E_UNKNOWN_PLATFORM = "E_UNKNOWN_PLATFORM"
E_CREDENTIAL_TYPE_UNKNOWN = "E_CREDENTIAL_TYPE_UNKNOWN"
E_PERMISSION_SCHEMA = "E_PERMISSION_SCHEMA"
E_TYPE_CHAIN = "E_TYPE_CHAIN"
E_FACET_SCHEMA_MISSING = "E_FACET_SCHEMA_MISSING"
E_AUTH_SCRIPT_MISSING = "E_AUTH_SCRIPT_MISSING"
E_RULE_BUDGET = "E_RULE_BUDGET"
E_TASK_NAME_DUPLICATE = "E_TASK_NAME_DUPLICATE"

MAX_RULES = 10
MAX_RULE_WORDS = 80

_OUTPUT_SCHEMA_FILE = re.compile(r"schemas/[^/]+\.v[0-9]+\.json")
_DEFAULT_INTEGRATION_FILE = "integration/integration.yaml"


def validate_entries(entries: Iterable[TreeEntry]) -> list[Diagnostic]:
    """`validate_tree(load_entries(entries))`."""
    return validate_tree(load_entries(entries))


def validate_tree(tree: PluginTree) -> list[Diagnostic]:
    """Every finding for `tree`: the loader's diagnostics, then schema violations
    and the semantic checks above. Errors and warnings alike."""
    return _Validator(tree).run()


def _pointer(*parts: object) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _sequence(value: Any) -> list:
    return value if isinstance(value, list) else []


def _resolve(base: str, ref: str) -> str | None:
    """`ref` (a path relative to the directory `base`, "" or ending in "/") as a
    tree-relative path, or None when it leaves that directory."""
    normal = posixpath.normpath(ref)
    if normal.startswith(("/", "..")) or normal == ".":
        return None
    return base + normal


class _Validator:
    def __init__(self, tree: PluginTree) -> None:
        self.tree = tree
        self.out: list[Diagnostic] = list(tree.diagnostics)
        self.plugin = tree.plugin
        self.integration = tree.integration
        self.sources = tree.integration_sources
        # Files whose content did not load: their documents are not checked again.
        self.broken = {
            d.file for d in tree.diagnostics if d.code in (E_YAML, E_SKILL_MD_MISSING) and d.file
        }
        integration = _mapping(self.integration)
        platform = integration.get("platform")
        self.platform = platform if isinstance(platform, str) else None
        self.own_types = set(_mapping(integration.get("types")))
        self.own_credentials = set(_mapping(integration.get("credentialTypes")))
        plugin = _mapping(self.plugin)
        self.external = set(_mapping(plugin.get("requires"))) | set(_mapping(plugin.get("extends")))
        discovery = _mapping(_mapping(integration.get("entryPoints")).get("discovery"))
        task = discovery.get("task")
        self.discovery_task = task if isinstance(task, str) else None
        # A plugin.yaml or integration file that did not load leaves the platform
        # and its types unknown: type and credential checks would only cascade.
        self.types_unknowable = "plugin.yaml" in self.broken or any(
            f in self.broken for f in INTEGRATION_FILES
        )
        self.permission_validator: Any = None
        self.seen: set[tuple] = set()

    # -- output ---------------------------------------------------------------

    def add(self, code: str, file: str | None, path: str | None, message: str) -> None:
        severity = "warning" if code.startswith("W_") else "error"
        key = (code, file, path, message)
        if key in self.seen:
            return
        self.seen.add(key)
        self.out.append(
            Diagnostic(code=code, severity=severity, file=file, path=path, message=message)
        )

    def schema(self, name: str, doc: Any, file: str, *, file_of=None) -> None:
        for violation in validate(name, doc):
            where = file_of(violation.path) if file_of else file
            self.add(E_SCHEMA, where, violation.path, violation.message)

    # -- driver -----------------------------------------------------------------

    def run(self) -> list[Diagnostic]:
        tree = self.tree
        self.check_plugin()
        self.check_integration()
        for name in sorted(tree.skills):
            self.check_skill(name)
        self.check_collection("rules", tree.rules, "rule-frontmatter")
        self.check_collection("knowledge", tree.knowledge, "knowledge-frontmatter")
        self.check_rule_budget()
        self.check_connections()
        return self.out

    # -- plugin.yaml ----------------------------------------------------------

    def check_plugin(self) -> None:
        if self.plugin is not None:
            self.schema("plugin", self.plugin, "plugin.yaml")
            return
        if "plugin.yaml" in self.broken:
            return
        only_skills = all(path.startswith("skills/") for path in self.tree.files)
        if not only_skills:
            self.add(
                E_PLUGIN_MISSING,
                "plugin.yaml",
                None,
                "a plugin needs a plugin.yaml (only a workspace skill tree has none)",
            )

    # -- integration ------------------------------------------------------------

    def integration_file(self, pointer: str) -> str:
        first = pointer.lstrip("/").split("/", 1)[0].replace("~1", "/").replace("~0", "~")
        if first in self.sources:
            return self.sources[first]
        return _DEFAULT_INTEGRATION_FILE

    def key_file(self, key: str) -> str:
        return self.sources.get(key, _DEFAULT_INTEGRATION_FILE)

    def check_integration(self) -> None:
        if self.integration is None:
            return
        self.schema(
            "integration",
            self.integration,
            _DEFAULT_INTEGRATION_FILE,
            file_of=self.integration_file,
        )
        self.check_type_chains()
        self.check_facets()
        self.check_auth_scripts()
        self.load_permission_validator()
        self.check_entry_points()

    def check_type_chains(self) -> None:
        types = self.integration.get("types")
        if not isinstance(types, dict) or not types:
            return
        file = self.key_file("types")
        roots = [n for n, d in types.items() if isinstance(d, dict) and d.get("root") is True]
        external_parent = False
        for name, definition in types.items():
            if not isinstance(definition, dict) or definition.get("root") is True:
                continue
            parent = definition.get("parent")
            if not isinstance(parent, str):
                continue
            where = _pointer("types", name, "parent")
            seen = {name}
            current = parent
            while True:
                platform, slash, bare = current.partition("/")
                if slash:
                    if platform == self.platform:
                        current = bare
                        continue
                    if platform in self.external:
                        external_parent = True
                    else:
                        self.add(
                            E_UNKNOWN_PLATFORM,
                            file,
                            where,
                            f"type {name!r}: parent {parent!r} is of platform {platform!r}, "
                            "which is neither this integration nor in requires/extends",
                        )
                    break
                if current in seen:
                    self.add(E_TYPE_CHAIN, file, where, f"type {name!r}: parent chain is a cycle")
                    break
                target = types.get(current)
                if not isinstance(target, dict):
                    self.add(
                        E_TYPE_CHAIN,
                        file,
                        where,
                        f"type {name!r}: parent {current!r} is not a type of this integration",
                    )
                    break
                if target.get("root") is True:
                    break
                seen.add(current)
                following = target.get("parent")
                if not isinstance(following, str):
                    break
                current = following
        if len(roots) > 1:
            self.add(
                E_TYPE_CHAIN,
                file,
                _pointer("types"),
                f"exactly one root type is required, found {len(roots)}: {', '.join(roots)}",
            )
        elif not roots and not external_parent:
            self.add(
                E_TYPE_CHAIN,
                file,
                _pointer("types"),
                "exactly one root type is required, found none",
            )

    def check_facets(self) -> None:
        file = self.key_file("facets")
        for i, facet in enumerate(_sequence(self.integration.get("facets"))):
            ref = _mapping(facet).get("schema")
            if isinstance(ref, str) and self.integration_path(ref) not in self.tree.files:
                self.add(
                    E_FACET_SCHEMA_MISSING,
                    file,
                    _pointer("facets", i, "schema"),
                    f"facet schema {ref!r} is not a file under integration/",
                )

    def check_auth_scripts(self) -> None:
        file = self.key_file("authMethods")
        for name, method in _mapping(self.integration.get("authMethods")).items():
            ref = _mapping(method).get("script")
            if isinstance(ref, str) and self.integration_path(ref) not in self.tree.files:
                self.add(
                    E_AUTH_SCRIPT_MISSING,
                    file,
                    _pointer("authMethods", name, "script"),
                    f"auth script {ref!r} is not a file under integration/",
                )

    @staticmethod
    def integration_path(ref: str) -> str | None:
        return _resolve("integration/", ref)

    def load_permission_validator(self) -> None:
        ref = _mapping(self.integration.get("permissions")).get("schema")
        if not isinstance(ref, str):
            return
        file, where = self.key_file("permissions"), _pointer("permissions", "schema")
        path = self.integration_path(ref)
        if path is None or path not in self.tree.files:
            self.add(
                E_PERMISSION_SCHEMA,
                file,
                where,
                f"permission schema {ref!r} is not a file under integration/",
            )
            return
        try:
            schema = json.loads(self.tree.files[path].decode("utf-8"))
            validator_class = validator_for(schema)
            validator_class.check_schema(schema)
        except (ValueError, jsonschema_exceptions.SchemaError) as exc:
            self.add(
                E_PERMISSION_SCHEMA,
                file,
                where,
                f"{path} is not a valid JSON Schema: {getattr(exc, 'message', None) or exc}",
            )
            return
        self.permission_validator = validator_class(schema)

    def check_entry_points(self) -> None:
        file = self.key_file("entryPoints")
        for kind, entry in _mapping(self.integration.get("entryPoints")).items():
            ref = _mapping(entry).get("task")
            if not isinstance(ref, str):
                continue
            skill_name, _, task_name = ref.partition("/")
            skill = self.tree.skills.get(skill_name)
            if skill is not None and f"skills/{skill_name}/tasks.yaml" in self.broken:
                continue
            names = {
                t.get("name")
                for t in _sequence(_mapping(skill.tasks if skill else None).get("tasks"))
                if isinstance(t, dict)
            }
            if task_name not in names:
                self.add(
                    E_ENTRY_POINT_UNKNOWN,
                    file,
                    _pointer("entryPoints", kind, "task"),
                    f"entry point {kind!r} names task {ref!r}, which this plugin does not have",
                )

    # -- skills -----------------------------------------------------------------

    def check_skill(self, name: str) -> None:
        skill = self.tree.skills[name]
        md = f"skills/{name}/SKILL.md"
        if md not in self.broken:
            self.schema("skill-frontmatter", skill.frontmatter, md)
            declared = skill.frontmatter.get("name")
            if isinstance(declared, str) and declared != name:
                self.add(
                    E_SKILL_NAME_MISMATCH,
                    md,
                    "/name",
                    f"name {declared!r} must equal the skill folder {name!r}",
                )
            applies = _mapping(_mapping(skill.frontmatter.get("runwhen")).get("appliesTo"))
            self.check_types(md, "/runwhen/appliesTo/types", applies.get("types"))
        if skill.tasks is not None:
            self.check_tasks(name, skill)

    def check_types(self, file: str, pointer: str, types: Any) -> None:
        for i, ref in enumerate(_sequence(types)):
            if not isinstance(ref, str) or "/" not in ref:
                continue
            platform, _, type_name = ref.partition("/")
            where = f"{pointer}/{i}"
            if self.types_unknowable:
                continue
            if self.plugin is None:
                self.add(
                    W_TYPE_UNCHECKED,
                    file,
                    where,
                    f"type {ref!r} is not checked: there is no plugin.yaml, "
                    "so no integration to check it against",
                )
            elif platform == self.platform:
                if type_name not in self.own_types:
                    self.add(
                        E_UNKNOWN_TYPE,
                        file,
                        where,
                        f"type {ref!r} is not declared by this plugin's integration",
                    )
            elif platform in self.external:
                self.add(
                    W_TYPE_UNCHECKED,
                    file,
                    where,
                    f"type {ref!r} belongs to {platform!r}, a required or extended "
                    "integration; not checked",
                )
            else:
                self.add(
                    E_UNKNOWN_PLATFORM,
                    file,
                    where,
                    f"type {ref!r}: platform {platform!r} is neither this plugin's integration "
                    "nor in requires/extends",
                )

    def check_tasks(self, skill_name: str, skill) -> None:
        file = f"skills/{skill_name}/tasks.yaml"
        tasks_doc = skill.tasks
        self.schema("tasks", tasks_doc, file)
        defaults = _mapping(tasks_doc.get("defaults"))
        seen_names: set[str] = set()
        for i, task in enumerate(_sequence(tasks_doc.get("tasks"))):
            if not isinstance(task, dict):
                continue
            name = task.get("name")
            if isinstance(name, str):
                if name in seen_names:
                    self.add(
                        E_TASK_NAME_DUPLICATE,
                        file,
                        _pointer("tasks", i, "name"),
                        f"task name {name!r} is used twice in {file}",
                    )
                seen_names.add(name)
            self.check_script(skill_name, skill, file, i, task)
            self.check_outputs(skill_name, skill, file, i, task)
            ref = f"{skill_name}/{name}" if isinstance(name, str) else None
            credentials = self.check_credentials(file, i, task, defaults, ref)
            self.check_permissions(file, i, task, credentials)

    def check_script(self, skill_name, skill, file, i, task) -> None:
        script = task.get("script")
        if not isinstance(script, str):
            return
        resolved = _resolve("", script)
        if resolved is None or not resolved.startswith("scripts/") or resolved not in skill.files:
            self.add(
                E_SCRIPT_MISSING,
                file,
                _pointer("tasks", i, "script"),
                f"script {script!r} is not a file under skills/{skill_name}/scripts/",
            )

    def check_outputs(self, skill_name, skill, file, i, task) -> None:
        for out_name, spec in _mapping(task.get("outputs")).items():
            value = _mapping(spec).get("schema")
            if not isinstance(value, str):
                continue
            where = _pointer("tasks", i, "outputs", out_name, "schema")
            if value.startswith("./"):
                resolved = _resolve("", value)
                if resolved is None or not _OUTPUT_SCHEMA_FILE.fullmatch(resolved):
                    self.add(
                        E_SCHEMA_FILE_NAME,
                        file,
                        where,
                        f"output schema file {value!r} must be named schemas/<name>.v<N>.json",
                    )
                if resolved is None or resolved not in skill.files:
                    self.add(
                        E_OUTPUT_SCHEMA_MISSING,
                        file,
                        where,
                        f"output schema {value!r} is not a file of skill {skill_name!r}",
                    )
            else:
                try:
                    compile_schema_notation(value)
                except SchemaNotationError as exc:
                    self.add(
                        E_SCHEMA_NOTATION, file, where, f"compact schema does not compile: {exc}"
                    )

    # -- credentials and permissions -----------------------------------------------

    @staticmethod
    def credential_types(value: Any) -> list[tuple[str, str]]:
        """(type, pointer suffix) for a credentials value: a name, or a list of
        names and {type, vars}."""
        if isinstance(value, str):
            return [(value, "")]
        found = []
        for j, item in enumerate(_sequence(value)):
            ctype = item if isinstance(item, str) else _mapping(item).get("type")
            if isinstance(ctype, str):
                found.append((ctype, f"/{j}"))
        return found

    def check_credentials(self, file, i, task, defaults, ref) -> list[str]:
        if "credentials" in task:
            declared = self.credential_types(task["credentials"])
            base = _pointer("tasks", i, "credentials")
        else:
            declared = self.credential_types(defaults.get("credentials"))
            base = _pointer("defaults", "credentials")
        for ctype, suffix in declared:
            self.check_credential(ctype, file, base + suffix, ref)
        return [ctype for ctype, _ in declared]

    def check_credential(self, ctype: str, file: str, where: str, task_ref: str | None) -> None:
        platform = ctype.partition("/")[0]
        if self.types_unknowable and ctype != "runwhen/sync":
            return
        if ctype == "runwhen/sync":
            if task_ref is None or task_ref != self.discovery_task:
                self.add(
                    E_CREDENTIAL_TYPE_UNKNOWN,
                    file,
                    where,
                    "runwhen/sync is platform-minted and allowed only on the integration's "
                    "discovery entry-point task",
                )
        elif platform == "generic" or ctype in self.own_credentials:
            return
        elif platform == self.platform:
            self.add(
                E_CREDENTIAL_TYPE_UNKNOWN,
                file,
                where,
                f"credential type {ctype!r} is not declared by this plugin's integration",
            )
        elif self.plugin is None or platform in self.external:
            self.add(
                W_TYPE_UNCHECKED,
                file,
                where,
                f"credential type {ctype!r} belongs to another integration; not checked",
            )
        else:
            self.add(
                E_CREDENTIAL_TYPE_UNKNOWN,
                file,
                where,
                f"credential type {ctype!r}: platform {platform!r} is neither this plugin's "
                "integration, generic, nor in requires/extends",
            )

    def check_permissions(self, file, i, task, credentials: list[str]) -> None:
        if self.permission_validator is None:
            return
        own = any(
            c in self.own_credentials or c.partition("/")[0] == self.platform for c in credentials
        )
        if not own:
            return  # relies on a required integration (INT-2 resolves it)
        for j, rule in enumerate(_sequence(task.get("permissions"))):
            try:
                errors = sorted(
                    self.permission_validator.iter_errors(rule), key=lambda e: list(e.absolute_path)
                )
            except Exception as exc:  # noqa: BLE001 - an unresolvable $ref in author JSON
                self.add(
                    E_PERMISSION_SCHEMA,
                    file,
                    _pointer("tasks", i, "permissions", j),
                    f"cannot evaluate the permission schema: {exc}",
                )
                continue
            for err in errors:
                self.add(
                    E_PERMISSION_SCHEMA,
                    file,
                    _pointer("tasks", i, "permissions", j, *err.absolute_path),
                    f"fails the integration's permission schema: {err.message}",
                )

    # -- rules, knowledge, connections ---------------------------------------------

    def check_collection(self, directory: str, items: dict, schema_name: str) -> None:
        for stem, (frontmatter, _body) in sorted(items.items()):
            file = f"{directory}/{stem}.md"
            if file in self.broken:
                continue
            self.schema(schema_name, frontmatter, file)
            declared = frontmatter.get("name")
            if isinstance(declared, str) and declared != stem:
                self.add(
                    E_FILE_NAME_MISMATCH,
                    file,
                    "/name",
                    f"name {declared!r} must equal the file name {stem!r}",
                )
            if directory == "knowledge":
                applies = _mapping(frontmatter.get("appliesTo"))
                self.check_types(file, "/appliesTo/types", applies.get("types"))

    def check_rule_budget(self) -> None:
        rules = self.tree.rules
        if len(rules) > MAX_RULES:
            self.add(
                E_RULE_BUDGET,
                "rules/",
                None,
                f"{len(rules)} rules; a plugin has at most {MAX_RULES}",
            )
        for stem, (_fm, body) in sorted(rules.items()):
            words = len(body.split())
            if words > MAX_RULE_WORDS:
                self.add(
                    E_RULE_BUDGET,
                    f"rules/{stem}.md",
                    None,
                    f"rule body is {words} words; the limit is {MAX_RULE_WORDS}",
                )

    def check_connections(self) -> None:
        data = self.tree.files.get("connections.yaml")
        if data is None:
            return
        try:
            doc = load_yaml(data.decode("utf-8"))
        except (yaml.YAMLError, ValueError, RecursionError) as exc:
            self.add(E_YAML, "connections.yaml", None, f"not valid YAML 1.2: {exc}")
            return
        self.schema("connections-file", doc, "connections.yaml")
