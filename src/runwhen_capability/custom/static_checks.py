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
import bisect
import re

# -- python: main()'s signature and what it returns --------------------------


def python_main_params(source: str) -> tuple[list[tuple[str, int]], bool] | None:
    """((name, line) for every named parameter after `ctx`, has **kwargs)
    for the module-level `main` function in a Python task/setup file -- the
    line is the parameter's own `ast.arg` node, e.g. `def main(ctx,
    mystery):`'s `mystery` reports its own line, not `main`'s, so an editor
    can point straight at the offending parameter. None if `source` doesn't
    parse or declares no top-level `main` -- both are runtime errors the
    host itself raises, not something validate() flags."""
    tree = _parse_python(source)
    if tree is None:
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "main":
            args = node.args
            positional = [*args.posonlyargs, *args.args]
            named = positional[1:] if positional else []  # drop `ctx`
            named += list(args.kwonlyargs)
            return [(a.arg, a.lineno) for a in named], args.kwarg is not None
    return None


def _parse_python(source: str) -> ast.Module | None:
    """ast.parse(source), or None when it cannot be parsed for any reason --
    a syntax error, a NUL byte, or nesting deep enough to exhaust the
    parser's recursion limit. Never raises: validate() runs on untrusted
    source and must return diagnostics, not an exception."""
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def _body_without_nested_defs(node: ast.AST):
    """Yields every descendant of `node`, except it does not recurse into a
    nested function/class/lambda -- a `return` inside a closure `main()`
    defines is not one of `main`'s own outputs. Iterative, so a deeply
    nested expression cannot exhaust the interpreter's recursion limit."""
    stack = [node]
    while stack:
        current = stack.pop()
        for child in ast.iter_child_nodes(current):
            if isinstance(
                child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef
            ):
                continue
            yield child
            stack.append(child)


def python_returned_output_names(source: str) -> list[tuple[int, str]] | None:
    """(line, output name) for every string-literal key in a dict literal
    `main()` returns (`return {"foo": ...}`). A dict built any other way
    (`return dict(...)`, `return outputs`) cannot be inspected statically and
    is silently skipped -- this is a best-effort check, not a full one. None
    if there is no top-level `main`."""
    tree = _parse_python(source)
    if tree is None:
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


# -- stripping comments and heredoc bodies before any regex scan ------------
# Both the bash-input/output scan below and mutating_kubectl_calls() run
# against this, not the raw source: a `#` comment mentioning "rw_append" or
# "kubectl delete" as prose, or a heredoc body quoting either as example
# text, is not a call -- scanning it as one is a false positive, not a
# missed one, which this module's own docstring says must not happen.

_HEREDOC_START_RE = re.compile(r"<<-?\s*([\"']?)(\w+)\1")


def _split_comment(line: str) -> tuple[str, int]:
    """(code prefix, length of the trailing `#...` comment, 0 if none) --
    quote-tracked (a `#` inside a '...' or "..." string is not a comment)
    and, like bash itself, only a `#` starting a word (preceded by
    whitespace or at column 0) begins one -- `foo#bar` is not a comment."""
    in_single = in_double = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and not in_single:
            i += 2  # an escaped character never toggles quote state or starts a comment
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double and (i == 0 or line[i - 1] in " \t"):
            return line[:i], len(line) - i
        i += 1
    return line, 0


def _strip_comments_and_heredocs(text: str) -> str:
    """Blanks out (replaces with spaces, same length, same line breaks) every
    `#` comment and every heredoc body (`<<EOF ... EOF`, `<<-EOF ... EOF`) in
    `text`, so a downstream regex scan never sees either as code. Line-based
    and quote-tracked per line, not a shell parser -- see this module's
    docstring for why that trade is the right one here. A heredoc's own
    delimiter line is matched literally (no shell word-expansion), which
    covers every task script this SDK has seen; a delimiter built from a
    variable is out of scope."""
    out_lines: list[str] = []
    heredoc_terminator: str | None = None
    for line in text.split("\n"):
        if heredoc_terminator is not None:
            out_lines.append(" " * len(line))
            if line.strip() == heredoc_terminator:
                heredoc_terminator = None
            continue

        code, comment_len = _split_comment(line)
        out_lines.append(code + " " * comment_len)

        heredoc_match = _HEREDOC_START_RE.search(code)
        if heredoc_match:
            heredoc_terminator = heredoc_match.group(2)
    return "\n".join(out_lines)


# -- bash: env-var/rw_input reads and rw_append/rw_set writes ----------------

# $VAR / ${VAR} / ${VAR:-default} / ${VAR[0]} -- only the name right after
# `$` or `${` is captured; whatever follows it (a default, an error message,
# a subscript) is left for the next match, so `${A:-$B}` reports both A and
# B. Never scanning ahead for the closing `}` also keeps this linear: a
# pattern that did so re-scanned to the end of the text for every unclosed
# `${`, quadratic on crafted input.
_VAR_REF_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)|([A-Za-z_][A-Za-z0-9_]*))")
_RW_INPUT_RE = re.compile(r"\brw_input\s+[\"']?([A-Za-z_][A-Za-z0-9_-]*)[\"']?")
_RW_APPEND_RE = re.compile(r"\brw_append\s+[\"']?([A-Za-z_][A-Za-z0-9_-]*)[\"']?")
_RW_SET_RE = re.compile(r"\brw_set\s+[\"']?([A-Za-z_][A-Za-z0-9_-]*)[\"']?")

# Special/positional shell variables ($1, $@, $#, $?, $$, $!, $-, $0) are
# never a declared input; skip them rather than flag every task that reads
# its own exit status or argv.
_SPECIAL_BASH_VARS = frozenset({"0", "1", "2", "3", "4", "5", "6", "7", "8", "9"})

# A name assigned anywhere in the script (`name=...`, `local name=...`,
# `export name=...`, `readonly name=...`) or bound by a `for name in ...`
# loop is an ordinary local variable, not an input read -- $VAR references to
# it must not be flagged as E_UNDECLARED_INPUT just because no input happens
# to share its name.
#
# The keyword needs its own trailing blank(s) so the two blank runs around
# it can never both match the same spaces: an optional keyword between two
# `[ \t]*` let a long run of blanks (e.g. a blanked-out comment line) be
# split between them every possible way -- quadratic backtracking.
_ASSIGNMENT_RE = re.compile(
    r"(?m)^[ \t]*(?:(?:local|export|readonly)[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)="
)
_FOR_LOOP_RE = re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")


def _line_starts(text: str) -> list[int]:
    return [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]


def _line_of(starts: list[int], pos: int) -> int:
    """1-indexed line of `pos`, by binary search over _line_starts(text) --
    not a count of every newline before `pos`, which is O(n) per hit and
    O(n * hits) over a file full of hits."""
    return bisect.bisect_right(starts, pos)


def bash_locally_assigned_names(text: str) -> set[str]:
    """Every name the script itself assigns -- ordinary variables and
    `for`-loop bindings -- as opposed to a name read from the environment."""
    text = _strip_comments_and_heredocs(text)
    names = {m.group(1) for m in _ASSIGNMENT_RE.finditer(text)}
    names |= {m.group(1) for m in _FOR_LOOP_RE.finditer(text)}
    return names


def bash_env_reads(text: str) -> list[tuple[int, str]]:
    """(line, VAR) for every `$VAR`/`${VAR}` reference that is NOT one of
    this script's own local variables -- the raw name as written, uppercase
    or not (the caller compares against each declared input's computed
    env-var name). Comments and heredoc bodies are never scanned."""
    text = _strip_comments_and_heredocs(text)
    starts = _line_starts(text)
    local_names = bash_locally_assigned_names(text)
    hits = []
    for m in _VAR_REF_RE.finditer(text):
        name = m.group(1) or m.group(2)
        if name in _SPECIAL_BASH_VARS or name in local_names:
            continue
        hits.append((_line_of(starts, m.start()), name))
    return hits


def bash_rw_input_reads(text: str) -> list[tuple[int, str]]:
    """(line, name) for every `rw_input <name>` call -- `name` as declared
    (not uppercased): rw_input takes the input's own manifest name. Comments
    and heredoc bodies are never scanned."""
    text = _strip_comments_and_heredocs(text)
    starts = _line_starts(text)
    return [(_line_of(starts, m.start()), m.group(1)) for m in _RW_INPUT_RE.finditer(text)]


def bash_output_writes(text: str) -> list[tuple[int, str]]:
    """(line, output name) for every `rw_append`/`rw_set` call. Comments and
    heredoc bodies are never scanned -- a comment mentioning either by name
    (documentation, an example) is not a call."""
    text = _strip_comments_and_heredocs(text)
    starts = _line_starts(text)
    hits = [(_line_of(starts, m.start()), m.group(1)) for m in _RW_APPEND_RE.finditer(text)]
    hits += [(_line_of(starts, m.start()), m.group(1)) for m in _RW_SET_RE.finditer(text)]
    return hits


# -- readOnly: a mutating kubectl verb in a readOnly task ---------------------

# kubectl delete/apply/patch/edit/scale/rollout restart/exec and the small
# set of obvious mutating complements. Read-only verbs
# (get/describe/logs/top/explain/version/api-resources/...) are deliberately
# not in this list.
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
    plain text containing "kubectl" and "delete" on the same statement.
    Comments and heredoc bodies are never scanned -- "kubectl delete" in a
    docstring or a help message is not an invocation."""
    text = _strip_comments_and_heredocs(text)
    starts = _line_starts(text)
    hits = []
    for m in _KUBECTL_STATEMENT_RE.finditer(text):
        statement = m.group(0)
        if _ROLLOUT_RESTART_RE.search(statement):
            hits.append((_line_of(starts, m.start()), "kubectl rollout restart"))
            continue
        verb_match = _VERB_RE.search(statement)
        if verb_match:
            hits.append((_line_of(starts, m.start()), f"kubectl {verb_match.group(1)}"))
    return hits
