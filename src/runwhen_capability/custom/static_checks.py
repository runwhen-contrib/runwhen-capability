"""Static analysis over one task's source text, with no execution: what
inputs a Python `main()`/bash task file reads, what outputs it produces, and
whether it runs a mutating `kubectl` verb. validate.py turns these findings
into E_UNDECLARED_INPUT/E_OUTPUT_UNDECLARED/E_READONLY_WRITE diagnostics.

Bash detection is "careful regex", not a shell parser -- by design (a real
shell grammar is far more machinery than a linting aid over small, largely
templated task scripts needs). It favours false negatives over false
positives: missing a read is a silent gap in the check, but flagging a
read that was never there would block a legitimate bundle from validating.
"""

from __future__ import annotations

import ast
import re

# -- python: main()'s signature and what it returns --------------------------


def python_main_params(source: str) -> tuple[list[str], bool] | None:
    """(named parameters after `ctx`, has **kwargs) for the module-level
    `main` function in a Python task/setup file. None if `source` doesn't
    parse or declares no top-level `main` -- both are runtime errors the
    host itself raises, not something validate() flags."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "main":
            args = node.args
            positional = [a.arg for a in [*args.posonlyargs, *args.args]]
            named = positional[1:] if positional else []  # drop `ctx`
            named += [a.arg for a in args.kwonlyargs]
            return named, args.kwarg is not None
    return None


def _body_without_nested_defs(node: ast.AST):
    """Yields every descendant of `node`, except it does not recurse into a
    nested function/class/lambda -- a `return` inside a closure `main()`
    defines is not one of `main`'s own outputs."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            continue
        yield child
        yield from _body_without_nested_defs(child)


def python_returned_output_names(source: str) -> list[tuple[int, str]] | None:
    """(line, output name) for every string-literal key in a dict literal
    `main()` returns (`return {"foo": ...}`). A dict built any other way
    (`return dict(...)`, `return outputs`) cannot be inspected statically and
    is silently skipped -- this is a best-effort check, not a full one. None
    if there is no top-level `main`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    main_def = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "main"
        ),
        None,
    )
    if main_def is None:
        return None
    hits: list[tuple[int, str]] = []
    for node in _body_without_nested_defs(main_def):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        for key in node.value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                hits.append((key.lineno, key.value))
    return hits


# -- bash: env-var/rw_input reads and rw_append/rw_set writes ----------------

# $VAR / ${VAR} / ${VAR:-default} / ${VAR:?msg} -- the last two carry a
# default/error-message suffix that is not itself a reference to anything.
_VAR_REF_RE = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?:[:]?[-=?+][^}]*)?\}|([A-Za-z_][A-Za-z0-9_]*))"
)
_RW_INPUT_RE = re.compile(r"\brw_input\s+[\"']?([A-Za-z_][A-Za-z0-9_-]*)[\"']?")
_RW_APPEND_RE = re.compile(r"\brw_append\s+[\"']?([A-Za-z_][A-Za-z0-9_-]*)[\"']?")
_RW_SET_RE = re.compile(r"\brw_set\s+[\"']?([A-Za-z_][A-Za-z0-9_-]*)[\"']?")

# Special/positional shell variables ($1, $@, $#, $?, $$, $!, $-, $0) are
# never a declared input; skip them rather than flag every task that reads
# its own exit status or argv.
_SPECIAL_BASH_VARS = frozenset({"0", "1", "2", "3", "4", "5", "6", "7", "8", "9"})


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def bash_env_reads(text: str) -> list[tuple[int, str]]:
    """(line, VAR) for every `$VAR`/`${VAR}` reference -- the raw name as
    written, uppercase or not (the caller compares against each declared
    input's computed env-var name)."""
    hits = []
    for m in _VAR_REF_RE.finditer(text):
        name = m.group(1) or m.group(2)
        if name in _SPECIAL_BASH_VARS:
            continue
        hits.append((_line_of(text, m.start()), name))
    return hits


def bash_rw_input_reads(text: str) -> list[tuple[int, str]]:
    """(line, name) for every `rw_input <name>` call -- `name` as declared
    (not uppercased): rw_input takes the input's own manifest name."""
    return [(_line_of(text, m.start()), m.group(1)) for m in _RW_INPUT_RE.finditer(text)]


def bash_output_writes(text: str) -> list[tuple[int, str]]:
    """(line, output name) for every `rw_append`/`rw_set` call."""
    hits = [(_line_of(text, m.start()), m.group(1)) for m in _RW_APPEND_RE.finditer(text)]
    hits += [(_line_of(text, m.start()), m.group(1)) for m in _RW_SET_RE.finditer(text)]
    return hits


# -- readOnly: a mutating kubectl verb in a readOnly task ---------------------

# Exactly the verbs section 2 names, plus the small set of obvious mutating
# complements ("and similar"). Read-only verbs (get/describe/logs/top/explain/
# version/api-resources/...) are deliberately not in this list.
MUTATING_KUBECTL_VERBS = (
    "delete",
    "apply",
    "patch",
    "edit",
    "scale",
    "exec",
    "replace",
    "create",
    "annotate",
    "label",
    "cp",
    "drain",
    "cordon",
    "uncordon",
    "taint",
    "autoscale",
    "expose",
    "set",
    "evict",
)

# One `kubectl` invocation's argument list runs to the next shell statement
# separator (newline, pipe, `;`, `&`) -- a rough but adequate boundary for a
# regex-based scan over normally-formatted shell.
_KUBECTL_STATEMENT_RE = re.compile(r"\bkubectl\b[^\n;|&]*")
_ROLLOUT_RESTART_RE = re.compile(r"\brollout\s+restart\b")
_VERB_RE = re.compile(r"\b(" + "|".join(MUTATING_KUBECTL_VERBS) + r")\b")


def mutating_kubectl_calls(text: str) -> list[tuple[int, str]]:
    """(line, "kubectl <verb>") for every kubectl invocation in `text` whose
    verb mutates cluster state. Works over both bash and Python source: a
    Python task's `ctx.run(["kubectl", "delete", ...])` call still reads as
    plain text containing "kubectl" and "delete" on the same statement."""
    hits = []
    for m in _KUBECTL_STATEMENT_RE.finditer(text):
        statement = m.group(0)
        if _ROLLOUT_RESTART_RE.search(statement):
            hits.append((_line_of(text, m.start()), "kubectl rollout restart"))
            continue
        verb_match = _VERB_RE.search(statement)
        if verb_match:
            hits.append((_line_of(text, m.start()), f"kubectl {verb_match.group(1)}"))
    return hits
