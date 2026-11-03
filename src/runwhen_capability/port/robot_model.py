"""Read a classic rw-cli Robot bundle into plain dataclasses.

Only the facts `rwtask port` needs: what the runbook is called, which tasks it
has, which user variables and secrets it imports, which scripts each task
calls, and which resource types its generation rules match. Nothing here reads
a script's content.

`robot` (robotframework) is imported lazily, so the base install works without
the `port` extra; `read_bundle` raises `PortDependencyError` when it is absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# The keyword arguments RW.Core.Import User Variable documents.
_VAR_KEYWORDS = ("type", "description", "pattern", "example", "default", "enum")
_VAR_KWARG = re.compile(rf"^({'|'.join(_VAR_KEYWORDS)})=(.*)$", re.DOTALL)
_SCRIPT = re.compile(r"[\w\-./]+\.(?:sh|py)\b")


class PortDependencyError(RuntimeError):
    """robotframework is not installed."""


@dataclass
class UserVar:
    name: str
    default: str | None = None
    description: str = ""
    pattern: str | None = None


@dataclass
class ClassicTask:
    title: str
    doc: str = ""
    tags: list[str] = field(default_factory=list)
    line: int = 0
    scripts: list[str] = field(default_factory=list)
    file: str = "runbook.robot"


@dataclass
class ClassicBundle:
    name: str
    display_name: str = ""
    doc: str = ""
    readme_summary: str = ""
    user_vars: list[UserVar] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    tasks: list[ClassicTask] = field(default_factory=list)
    sli_tasks: list[ClassicTask] = field(default_factory=list)
    resource_types: list[str] = field(default_factory=list)
    path: Path | None = None


def _import_robot():
    try:
        from robot.api import get_model
        from robot.api.parsing import ModelVisitor
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise PortDependencyError(
            "rwtask port needs robotframework to read classic bundles; "
            "install it with: pip install 'runwhen-capability[port]'"
        ) from exc
    return get_model, ModelVisitor


def _norm_keyword(name: str) -> str:
    return re.sub(r"[\s_]+", " ", name).strip().lower()


def _parse_user_var(args: tuple[str, ...]) -> UserVar | None:
    name: str | None = None
    kwargs: dict[str, str] = {}
    for arg in args:
        match = _VAR_KWARG.match(arg)
        if match:
            kwargs[match.group(1)] = match.group(2)
        elif name is None:
            name = arg
    if not name:
        return None
    return UserVar(
        name=name,
        default=kwargs.get("default"),
        description=kwargs.get("description", "").strip(),
        pattern=kwargs.get("pattern"),
    )


def _scripts_in(lines: list[str]) -> list[str]:
    found: list[str] = []
    for line in lines:
        if line.lstrip().startswith("#"):
            continue
        for match in _SCRIPT.finditer(line):
            base = match.group(0).rsplit("/", 1)[-1]
            if base not in found:
                found.append(base)
    return found


@dataclass
class _Parsed:
    display_name: str = ""
    doc: str = ""
    tasks: list[ClassicTask] = field(default_factory=list)
    user_vars: list[UserVar] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)


def _parse_robot(path: Path) -> _Parsed:
    get_model, ModelVisitor = _import_robot()
    source_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    parsed = _Parsed()

    class Visitor(ModelVisitor):
        def visit_SettingSection(self, node: Any) -> None:
            for item in node.body:
                kind = type(item).__name__
                if kind == "Documentation":
                    parsed.doc = (item.value or "").strip()
                elif kind == "Metadata" and _norm_keyword(item.name) == "display name":
                    parsed.display_name = (item.value or "").strip()

        def visit_TestCase(self, node: Any) -> None:
            self.generic_visit(node)  # variables and secrets imported inside the task body
            doc, tags = "", []
            for item in node.body:
                kind = type(item).__name__
                if kind == "Documentation":
                    doc = (item.value or "").strip()
                elif kind == "Tags":
                    tags = list(item.values)
            end = getattr(node, "end_lineno", None) or node.lineno
            parsed.tasks.append(
                ClassicTask(
                    title=node.name,
                    doc=doc,
                    tags=tags,
                    line=node.lineno,
                    scripts=_scripts_in(source_lines[node.lineno - 1 : end]),
                    file=path.name,
                )
            )

        def visit_KeywordCall(self, node: Any) -> None:
            keyword = _norm_keyword(node.keyword or "")
            if keyword.endswith("import user variable"):
                var = _parse_user_var(node.args)
                if var and all(v.name != var.name for v in parsed.user_vars):
                    parsed.user_vars.append(var)
            elif keyword.endswith("import secret"):
                if node.args and node.args[0] not in parsed.secrets:
                    parsed.secrets.append(node.args[0])

    Visitor().visit(get_model(str(path)))
    return parsed


def _readme_summary(path: Path) -> str:
    readme = path / "README.md"
    if not readme.is_file():
        return ""
    paragraph: list[str] = []
    for line in readme.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if paragraph:
                break
            continue
        if not stripped:
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    return " ".join(paragraph)


def _resource_types(path: Path) -> list[str]:
    types: list[str] = []
    rules_dir = path / ".runwhen" / "generation-rules"
    if not rules_dir.is_dir():
        return types
    for rule_file in sorted([*rules_dir.glob("*.yaml"), *rules_dir.glob("*.yml")]):
        try:
            doc = yaml.safe_load(rule_file.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        rules = ((doc or {}).get("spec") or {}).get("generationRules") or []
        for rule in rules:
            for resource_type in (rule or {}).get("resourceTypes") or []:
                if isinstance(resource_type, str) and resource_type not in types:
                    types.append(resource_type)
    return types


def read_bundle(path: Path) -> ClassicBundle:
    """Read the classic bundle directory at `path`.

    A bundle may have only a runbook, only an SLI, and no generation rules.
    """
    path = Path(path)
    bundle = ClassicBundle(name=path.name, path=path)
    parsed_files: list[_Parsed] = []
    for filename in ("runbook.robot", "sli.robot"):
        robot_file = path / filename
        if robot_file.is_file():
            parsed_files.append(_parse_robot(robot_file))
        else:
            parsed_files.append(_Parsed())
    runbook, sli = parsed_files

    bundle.display_name = runbook.display_name or sli.display_name
    bundle.doc = runbook.doc or sli.doc
    bundle.tasks = runbook.tasks
    bundle.sli_tasks = sli.tasks
    for parsed in (runbook, sli):
        for var in parsed.user_vars:
            if all(v.name != var.name for v in bundle.user_vars):
                bundle.user_vars.append(var)
        for secret in parsed.secrets:
            if secret not in bundle.secrets:
                bundle.secrets.append(secret)
    bundle.readme_summary = _readme_summary(path)
    bundle.resource_types = _resource_types(path)
    return bundle
