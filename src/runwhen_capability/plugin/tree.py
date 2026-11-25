"""Load a plugin tree (contracts/archive.md section 3a) into memory.

`load_entries` takes the files of a tree (`contracts.archive.read` output, or any
`TreeEntry`s), `load_dir` walks a directory first, `load_skill_dir` wraps one
skill directory as a workspace skill. Loading never raises for a content
problem: whatever cannot be read becomes a `Diagnostic` on the result, and the
rest of the tree still loads, so `validate_tree` can report everything at once.

Loader diagnostics (all severity "error", `file` is the tree-relative path):

- E_YAML -- a YAML file or a frontmatter block does not parse as YAML 1.2, is
  not valid UTF-8, or is not a mapping.
- E_SKILL_MD_MISSING -- `skills/<skill>/` has no SKILL.md.
- E_PATH_NOT_ALLOWED -- a path outside the section 3a layout, or a symlink
  or other non-regular file in a directory.
- E_INVALID_PATH -- a path the archive rules refuse (absolute, `..`, backslash,
  control character, not NFC, too long, case collision).
- E_DUPLICATE_KEY -- a top-level integration key in more than one of
  integration/{integration,types,facets,dependencies}.yaml.

Authoring state is dropped silently and never reaches `files`: the root `tests/`,
`runwhen.lock` and `.runwhen/`, and each skill's `tests/` (recorded fixtures, dev-only:
`rwtask test` reads them from the plugin directory; archive.md section 2).

Parsing goes only through `contracts.load_yaml` / `split_frontmatter`, so every
file is YAML 1.2.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..contracts import TreeEntry, load_yaml, split_frontmatter
from ..contracts.archive import ArchiveError, _check_path
from ..custom.diagnostics import E_PATH_NOT_ALLOWED, Diagnostic

E_YAML = "E_YAML"
E_SKILL_MD_MISSING = "E_SKILL_MD_MISSING"
E_INVALID_PATH = "E_INVALID_PATH"
E_DUPLICATE_KEY = "E_DUPLICATE_KEY"

INTEGRATION_FILES = (
    "integration/integration.yaml",
    "integration/types.yaml",
    "integration/facets.yaml",
    "integration/dependencies.yaml",
)
# `rwtask pull`'s lockfile: authoring state next to plugin.yaml, never part of the archive.
LOCK_FILE = "runwhen.lock"
_ROOT_FILES = frozenset({"plugin.yaml", "README.md", "LICENSE", "CHANGELOG.md", "connections.yaml"})
_SKILL_FILES = frozenset({"SKILL.md", "tasks.yaml"})
_SKILL_DIRS = frozenset({"scripts", "lib", "schemas", "references", "assets", "tests"})
_INTEGRATION_DIRS = frozenset({"schemas", "auth"})
_SKILL_DIR_REL = re.compile(r"skills/[^/]+/")


@dataclass(frozen=True)
class SkillTree:
    """One `skills/<name>/` directory. `files` is every file of the directory
    (SKILL.md and tasks.yaml included) keyed by its path relative to it;
    `tasks` is the parsed tasks.yaml, None when there is none (or it is broken)."""

    name: str
    frontmatter: dict
    body: str
    tasks: dict | None
    files: dict[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginTree:
    """A loaded plugin tree. `integration` is the merge of the integration files'
    top-level keys, `integration_sources` says which file each key came from.
    `rules` and `knowledge` map the file stem to (frontmatter, body). `files`
    holds every retained file by its tree-relative path."""

    plugin: dict | None = None
    integration: dict | None = None
    integration_sources: dict[str, str] = field(default_factory=dict)
    skills: dict[str, SkillTree] = field(default_factory=dict)
    rules: dict[str, tuple[dict, str]] = field(default_factory=dict)
    knowledge: dict[str, tuple[dict, str]] = field(default_factory=dict)
    files: dict[str, bytes] = field(default_factory=dict)
    executable: frozenset[str] = frozenset()
    diagnostics: list[Diagnostic] = field(default_factory=list)


def _diag(code: str, file: str | None, message: str, *, path: str | None = None, line=None):
    return Diagnostic(code=code, file=file, path=path, line=line, message=message)


def layout_allows(path: str) -> bool:
    """Whether a (valid) relative path is inside the archive.md section 3a layout."""
    parts = path.split("/")
    head = parts[0]
    if len(parts) == 1:
        return head in _ROOT_FILES
    if head == "integration":
        if len(parts) == 2:
            return path in INTEGRATION_FILES
        return parts[1] in _INTEGRATION_DIRS
    if head == "skills":
        if len(parts) == 3:
            return parts[2] in _SKILL_FILES
        return len(parts) > 3 and parts[2] in _SKILL_DIRS
    if head in ("rules", "knowledge"):
        return len(parts) == 2 and parts[1].endswith(".md") and len(parts[1]) > 3
    return False


def _yaml_failure(path: str, exc: Exception, *, line_offset: int) -> Diagnostic:
    line = None
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        line = mark.line + line_offset
    reason = getattr(exc, "problem", None) or str(exc)
    return _diag(E_YAML, path, f"not valid YAML 1.2: {reason}", line=line)


def _text(path: str, data: bytes, diags: list[Diagnostic]) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        diags.append(_diag(E_YAML, path, "not valid UTF-8"))
        return None


def _load_mapping(path: str, data: bytes, diags: list[Diagnostic]) -> dict | None:
    text = _text(path, data, diags)
    if text is None:
        return None
    try:
        doc: Any = load_yaml(text)
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        diags.append(_yaml_failure(path, exc, line_offset=1))
        return None
    if not isinstance(doc, dict):
        diags.append(_diag(E_YAML, path, "must be a YAML mapping"))
        return None
    return doc


def _load_markdown(path: str, data: bytes, diags: list[Diagnostic]) -> tuple[dict, str] | None:
    text = _text(path, data, diags)
    if text is None:
        return None
    try:
        return split_frontmatter(text)
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        # the frontmatter text starts on line 2, after the opening `---`
        diags.append(_yaml_failure(path, exc, line_offset=2))
        return None


def _is_excluded(parts: list[str]) -> bool:
    """A path that is authoring state, not plugin content (archive.md section 2): anything
    under the root `tests/` or `.runwhen/`, or under a skill's `tests/` (recorded fixtures,
    read by `rwtask test` from the plugin directory and never packaged)."""
    if parts[0] in ("tests", ".runwhen") and len(parts) > 1:
        return True
    return len(parts) > 3 and parts[0] == "skills" and parts[2] == "tests"


def load_entries(entries: Iterable[TreeEntry]) -> PluginTree:
    """Load a plugin tree from its files. Never raises for a content problem."""
    diags: list[Diagnostic] = []
    files: dict[str, bytes] = {}
    executable: set[str] = set()
    folded: dict[str, str] = {}

    for entry in entries:
        path = entry.path
        try:
            _check_path(path)
        except ArchiveError as exc:
            diags.append(_diag(E_INVALID_PATH, path, str(exc).partition(": ")[2]))
            continue
        key = path.casefold()
        if key in folded:
            diags.append(_diag(E_INVALID_PATH, path, f"{path!r} collides with {folded[key]!r}"))
            continue
        folded[key] = path
        parts = path.split("/")
        if _is_excluded(parts) or path == LOCK_FILE:
            continue  # excluded from the archive (archive.md section 2)
        if not layout_allows(path):
            diags.append(
                _diag(
                    E_PATH_NOT_ALLOWED,
                    path,
                    "not a path a plugin may contain (archive.md section 3a)",
                )
            )
            continue
        files[path] = entry.data
        if entry.executable:
            executable.add(path)

    plugin = None
    if "plugin.yaml" in files:
        plugin = _load_mapping("plugin.yaml", files["plugin.yaml"], diags)

    integration: dict | None = None
    sources: dict[str, str] = {}
    for path in INTEGRATION_FILES:
        if path not in files:
            continue
        doc = _load_mapping(path, files[path], diags)
        if doc is None:
            continue
        if integration is None:
            integration = {}
        for key, value in doc.items():
            if key in sources:
                diags.append(
                    _diag(
                        E_DUPLICATE_KEY,
                        path,
                        f"top-level key {key!r} is also in {sources[key]}; "
                        "a key may be in only one integration file",
                        path=f"/{key}",
                    )
                )
                continue
            integration[key] = value
            sources[key] = path

    skill_files: dict[str, dict[str, bytes]] = {}
    for path, data in files.items():
        parts = path.split("/", 2)
        if parts[0] == "skills":
            skill_files.setdefault(parts[1], {})[parts[2]] = data
    skills = {name: _load_skill(name, skill_files[name], diags) for name in sorted(skill_files)}

    rules = _load_collection("rules", files, diags)
    knowledge = _load_collection("knowledge", files, diags)

    return PluginTree(
        plugin=plugin,
        integration=integration,
        integration_sources=sources,
        skills=skills,
        rules=rules,
        knowledge=knowledge,
        files=files,
        executable=frozenset(executable),
        diagnostics=diags,
    )


def _load_skill(name: str, files: dict[str, bytes], diags: list[Diagnostic]) -> SkillTree:
    base = f"skills/{name}/"
    frontmatter: dict = {}
    body = ""
    if "SKILL.md" in files:
        loaded = _load_markdown(base + "SKILL.md", files["SKILL.md"], diags)
        if loaded is not None:
            frontmatter, body = loaded
    else:
        diags.append(_diag(E_SKILL_MD_MISSING, base + "SKILL.md", "every skill needs a SKILL.md"))
    tasks = None
    if "tasks.yaml" in files:
        tasks = _load_mapping(base + "tasks.yaml", files["tasks.yaml"], diags)
    return SkillTree(name, frontmatter, body, tasks, dict(files))


def _load_collection(
    directory: str, files: dict[str, bytes], diags: list[Diagnostic]
) -> dict[str, tuple[dict, str]]:
    found: dict[str, tuple[dict, str]] = {}
    prefix = directory + "/"
    for path in sorted(files):
        if not path.startswith(prefix):
            continue
        loaded = _load_markdown(path, files[path], diags)
        found[path[len(prefix) : -len(".md")]] = loaded if loaded is not None else ({}, "")
    return found


def _read_dir(root: Path) -> tuple[list[TreeEntry], list[Diagnostic]]:
    """Every regular file under `root`, in sorted order. Symlinks and other
    non-regular files are E_PATH_NOT_ALLOWED and never followed. Root-level
    dotfiles and dot-directories (.git, .github), the root `runwhen.lock`, the
    root `tests/` directory and each `skills/<skill>/tests/` directory are skipped
    (`root` may be one skill directory, whose own `tests/` is skipped likewise)."""
    entries: list[TreeEntry] = []
    diags: list[Diagnostic] = []

    def walk(directory: Path, rel: str) -> None:
        with os.scandir(directory) as scan:
            children = sorted(scan, key=lambda e: e.name)
        for child in children:
            child_rel = f"{rel}{child.name}"
            if not rel and (child.name.startswith(".") or child.name == LOCK_FILE):
                continue
            if child.name == "tests" and (not rel or _SKILL_DIR_REL.fullmatch(rel)):
                continue
            mode = child.stat(follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                diags.append(
                    _diag(E_PATH_NOT_ALLOWED, child_rel, "a symlink is not allowed in a plugin")
                )
            elif stat.S_ISDIR(mode):
                walk(Path(child.path), child_rel + "/")
            elif stat.S_ISREG(mode):
                entries.append(
                    TreeEntry(
                        child_rel,
                        Path(child.path).read_bytes(),
                        bool(mode & stat.S_IXUSR),
                    )
                )
            else:
                diags.append(
                    _diag(E_PATH_NOT_ALLOWED, child_rel, "not a regular file or directory")
                )

    walk(root, "")
    return entries, diags


def load_dir(root: Path) -> PluginTree:
    """Load the plugin in directory `root`."""
    entries, diags = _read_dir(Path(root))
    tree = load_entries(entries)
    return _with_leading(tree, diags)


def load_skill_dir(path: Path) -> PluginTree:
    """Load one skill directory as a workspace skill: plugin `workspace` has no
    plugin.yaml, so `plugin` is None and the single skill is named after the
    directory."""
    root = Path(path).resolve()
    entries, diags = _read_dir(root)
    prefixed = [TreeEntry(f"skills/{root.name}/{e.path}", e.data, e.executable) for e in entries]
    for diag in diags:
        diag.file = f"skills/{root.name}/{diag.file}"
    return _with_leading(load_entries(prefixed), diags)


def _with_leading(tree: PluginTree, diags: list[Diagnostic]) -> PluginTree:
    if not diags:
        return tree
    return PluginTree(
        plugin=tree.plugin,
        integration=tree.integration,
        integration_sources=tree.integration_sources,
        skills=tree.skills,
        rules=tree.rules,
        knowledge=tree.knowledge,
        files=tree.files,
        executable=tree.executable,
        diagnostics=diags + tree.diagnostics,
    )
