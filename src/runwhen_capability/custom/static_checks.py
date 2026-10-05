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
import shlex

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


def _strip_comments_and_heredocs(text: str, keep_heredocs: bool = False) -> str:
    """Blanks out (replaces with spaces, same length, same line breaks) every
    `#` comment and every heredoc body (`<<EOF ... EOF`, `<<-EOF ... EOF`) in
    `text`, so a downstream regex scan never sees either as code. Line-based
    and quote-tracked per line, not a shell parser -- see this module's
    docstring for why that trade is the right one here. A heredoc's own
    delimiter line is matched literally (no shell word-expansion), which
    covers every task script this SDK has seen; a delimiter built from a
    variable is out of scope.

    `keep_heredocs=True` blanks comments only: an unquoted heredoc body
    expands `$VAR`, so "is this name read" must still see it."""
    out_lines: list[str] = []
    heredoc_terminator: str | None = None
    for line in text.split("\n"):
        if heredoc_terminator is not None:
            out_lines.append(line if keep_heredocs else " " * len(line))
            if line.strip() == heredoc_terminator:
                heredoc_terminator = None
            continue

        code, comment_len = _split_comment(line)
        out_lines.append(code + " " * comment_len)

        heredoc_match = _HEREDOC_START_RE.search(code)
        if heredoc_match:
            heredoc_terminator = heredoc_match.group(2)
    return "\n".join(out_lines)


def _single_quoted_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of the body of every '...' string in `text` (the
    characters between the quotes; an unterminated quote runs to the end).
    Tracked across lines, since a quoted jq or awk program often spans
    several; a `'` inside "..." or escaped as `\\'` opens nothing.

    Each `(` -- a `$(...)` substitution or a subshell -- starts a fresh
    quoting context, closed by its `)`: in `"$(jq "$n" '{n: $n}')"` the
    inner quotes belong to the substitution, not to the outer string."""
    spans: list[tuple[int, int]] = []
    in_double = [False]  # one entry per open `(`; the last is the current context
    start: int | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if start is not None:
            if ch == "'":
                spans.append((start, i))
                start = None
        elif ch == "\\":
            i += 2  # an escaped character never toggles quote state
            continue
        elif ch == "'" and not in_double[-1]:
            start = i + 1
        elif ch == '"':
            in_double[-1] = not in_double[-1]
        elif ch == "(" and (not in_double[-1] or text[i - 1 : i] == "$"):
            in_double.append(False)
        elif ch == ")" and not in_double[-1] and len(in_double) > 1:
            in_double.pop()
        i += 1
    if start is not None:
        spans.append((start, len(text)))
    return spans


def _blank_single_quoted(text: str) -> str:
    """Blanks out the body of every '...' string (same length, line breaks
    kept), so a `$` inside one -- a jq or awk variable, say `jq '{t: $t}'` --
    is never read as a bash expansion; bash never expands inside single
    quotes. See _single_quoted_spans for the quoting rules."""
    out = list(text)
    for start, end in _single_quoted_spans(text):
        for i in range(start, end):
            if out[i] != "\n":
                out[i] = " "
    return "".join(out)


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
#
# An assignment starts a simple command: at the start of a line, or after a
# command separator (`;`, `&&`, `||`, `|`, `(`, `{`, `!`) or a compound
# keyword (`then`, `do`, `else`, `while`, `until`, `if`, `elif`) -- `a=1; b=2`, `cmd || n=0` and
# `for ((i=0; ...))` all bind their names. `x+=` appends and counts too.
_COMMAND_START = r"(?:^|[;&|({!]|\b(?:then|do|else|while|until|if|elif)\b)[ \t]*"
_ASSIGNMENT_RE = re.compile(
    r"(?m)"
    + _COMMAND_START
    + r"(?:(?:local|export|readonly|declare|typeset)[ \t]+(?:-[A-Za-z]+[ \t]+)*)?"
    r"([A-Za-z_][A-Za-z0-9_]*)\+?="
)
_FOR_LOOP_RE = re.compile(r"\b(?:for|select)\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")
# Builtins that bind the names they are given without an `=`: the rest of
# the simple command (up to a separator or the end of the line) is parsed
# by _names_bound_by().
_BINDING_BUILTIN_RE = re.compile(
    r"(?m)"
    + _COMMAND_START
    + r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*[ \t]+)*"  # an env prefix: `IFS='|' read ...`
    r"(local|export|readonly|declare|typeset|read|mapfile|readarray|printf|getopts)\b([^;&|\n]*)"
)
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Option letters that consume a value (glued, `-d:`, or the next word) --
# per builtin, from bash(1). `read -a NAME` and `printf -v NAME` bind NAME.
_VALUE_OPTIONS = {"read": "dinNptu", "mapfile": "dnOsuCc", "readarray": "dnOsuCc"}
_NAME_OPTIONS = {"read": "a", "printf": "v"}


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
    for m in _BINDING_BUILTIN_RE.finditer(text):
        names |= _names_bound_by(m.group(1), m.group(2))
    return names


def _words(rest: str) -> list[str]:
    """The words of a simple command's arguments, quotes removed; up to the
    first redirection, since `read x < file` binds x and not `file`."""
    try:
        words = shlex.split(rest)
    except ValueError:  # an unbalanced quote: fall back to plain splitting
        words = rest.split()
    for i, word in enumerate(words):
        if word.startswith(("<", ">", "0<", "1>", "2>")):
            return words[:i]
    return words


def _names_bound_by(builtin: str, rest: str) -> set[str]:
    """The variable names `builtin rest...` binds.

    Declaration builtins and `read` bind every plain word (`local a b=1`,
    `read -r x y`); `mapfile`/`readarray` bind their last word; `getopts`
    binds its second; `printf` binds only its `-v` name. Option letters that
    take a value skip that value, so `read -p prompt x` binds x alone."""
    value_options = _VALUE_OPTIONS.get(builtin, "")
    name_options = _NAME_OPTIONS.get(builtin, "")
    bound: set[str] = set()
    operands: list[str] = []
    words = _words(rest)
    i = 0
    while i < len(words):
        word = words[i]
        i += 1
        if word.startswith("-") and len(word) > 1 and not operands:
            for pos, letter in enumerate(word[1:], start=1):
                if letter in value_options or letter in name_options:
                    value = word[pos + 1 :]
                    if not value and i < len(words):
                        value, i = words[i], i + 1
                    if letter in name_options and _NAME_RE.fullmatch(value):
                        bound.add(value)
                    break
            continue
        operands.append(word)
    if builtin in ("mapfile", "readarray"):
        operands = operands[-1:]
    elif builtin == "getopts":
        operands = operands[1:2]
    elif builtin == "printf":
        operands = []
    for operand in operands:
        name = operand.split("=", 1)[0].removesuffix("+")
        if _NAME_RE.fullmatch(name):
            bound.add(name)
    return bound


def bash_env_reads(text: str) -> list[tuple[int, str]]:
    """(line, VAR) for every `$VAR`/`${VAR}` reference that is NOT one of
    this script's own local variables -- the raw name as written, uppercase
    or not (the caller compares against each declared input's computed
    env-var name). Comments, heredoc bodies and single-quoted strings are
    never scanned."""
    text = _strip_comments_and_heredocs(text)
    starts = _line_starts(text)
    local_names = bash_locally_assigned_names(text)
    hits = []
    for m in _VAR_REF_RE.finditer(_blank_single_quoted(text)):
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
# `\W+`, not `\s+`: in a Python argv list the two words are separated by
# quotes and a comma (`"rollout", "restart"`), not only by blanks.
_ROLLOUT_RESTART_RE = re.compile(r"\brollout\W+restart\b")
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


# -- data as code: text built from a variable, then run as a command -----------

_EVAL_RE = re.compile(r"(?m)" + _COMMAND_START + r"eval\b([^\n]*)")
# sh/bash/dash/zsh/ksh -c, with any options before the -c (`bash -euo pipefail
# -c`, `sh -lc`). Not preceded by a word character or `.`, so `run.sh -c x` is
# a script called run.sh, not a shell. Group 1 is the command string alone
# (the first word after -c); later words are positional arguments, which
# are data passed as data.
_SHELL_C_RE = re.compile(
    r"(?<![\w.\-])(?:ba|da|z|k)?sh(?:[ \t]+(?:-[A-Za-z]*o[ \t]+\w+|-[A-Za-z-]+))*?"
    r"[ \t]+-[A-Za-z]*c\b[ \t]*(\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S*)"
)
_SHELL_NAME_RE = re.compile(r"(?:ba|da|z|k)?sh")
_XARGS_RE = re.compile(r"(?<![\w.\-])xargs\b([^\n]*)")
_XARGS_REPLACE_RE = re.compile(r"(?<![\w-])(?:-I|-i|--replace)\b|\{\}")
_AWK_RE = re.compile(r"(?<![\w.\-])[gmn]?awk\b")
# `system(` and the pipe form `<expr> | getline` run a command; a plain
# `getline` and `getline var < file` only read input and are safe.
_AWK_CMD_RE = re.compile(r"\bsystem[ \t]*\(|\|&?[ \t]*getline\b")
_STATEMENT_SEP_RE = re.compile(r"[;|&\n(]")


def bash_data_as_code(text: str) -> list[tuple[int, str]]:
    """(line, construct) -- at most one per line -- for every bash construct
    that runs text as a command where that text can be built from data:
    `eval` with a `$` expansion; `sh -c`/`bash -c` whose command string has
    a `$` expansion; `xargs ... sh -c` with a replace string (`-I`, `{}`) or
    an expansion; and an awk program (single-quoted, so its text is
    blanked for the `$` checks) containing `system(` or a `| getline` pipe (a plain `getline` or
    `getline < file` reads input and is safe).
    A single-quoted `$` (`bash -c 'echo "$1"' _ "$x"`) is the safe pattern
    and never flagged. Comments and heredoc bodies are never scanned."""
    stripped = _strip_comments_and_heredocs(text)
    starts = _line_starts(stripped)
    blanked = _blank_single_quoted(stripped)
    found: dict[int, str] = {}

    for m in _EVAL_RE.finditer(blanked):
        if "$" in m.group(1):
            found.setdefault(_line_of(starts, m.start(1)), "eval")
    for m in _XARGS_RE.finditer(blanked):
        shell = _SHELL_C_RE.search(m.group(1))
        if shell and ("$" in shell.group(1) or _XARGS_REPLACE_RE.search(m.group(1))):
            found.setdefault(_line_of(starts, m.start(1)), "xargs sh -c")
    for m in _SHELL_C_RE.finditer(blanked):
        if "$" in m.group(1):
            shell_name = _SHELL_NAME_RE.search(m.group(0)).group(0)
            found.setdefault(_line_of(starts, m.start()), f"{shell_name} -c")

    # awk: a single-quoted span whose statement (back to the previous command
    # separator, looking at the blanked text so `-F'|'` is no separator)
    # names awk is an awk program.
    joined = re.sub(r"\\\n", "  ", blanked)  # a `\`-continued line is one statement
    for start, end in _single_quoted_spans(stripped):
        seps = list(_STATEMENT_SEP_RE.finditer(joined, 0, start))
        statement_start = seps[-1].end() if seps else 0
        if not _AWK_RE.search(joined, statement_start, start):
            continue
        for hit in _AWK_CMD_RE.finditer(stripped, start, end):
            construct = "awk system()" if hit.group(0).startswith("system") else "awk | getline"
            found.setdefault(_line_of(starts, hit.start()), construct)
    return sorted(found.items())


_SUBPROCESS_CALLS = frozenset({"run", "call", "check_call", "check_output", "Popen"})
_SUBPROCESS_ALWAYS_SHELL = frozenset({"getoutput", "getstatusoutput"})


def python_data_as_code(source: str) -> list[tuple[int, str]]:
    """(line, construct) for every `subprocess.run/call/check_call/
    check_output/Popen(..., shell=True)`, `subprocess.getoutput`/
    `getstatusoutput` (always a shell) and `os.system(...)` in a Python
    task, following `import subprocess as sp` / `from subprocess import run`
    aliases. [] when the source does not parse."""
    tree = _parse_python(source)
    if tree is None:
        return []
    modules: dict[str, str] = {}  # local name -> "subprocess" | "os"
    functions: dict[str, str] = {}  # local name -> "subprocess.run" | "os.system" ...
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("subprocess", "os"):
                    modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in ("subprocess", "os"):
            for alias in node.names:
                functions[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        qualified = None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            module = modules.get(func.value.id)
            if module:
                qualified = f"{module}.{func.attr}"
        elif isinstance(func, ast.Name):
            qualified = functions.get(func.id)
        if qualified is None:
            continue
        module, _, name = qualified.partition(".")
        if module == "os" and name == "system":
            hits.append((node.lineno, "os.system()"))
        elif module == "subprocess" and name in _SUBPROCESS_ALWAYS_SHELL:
            hits.append((node.lineno, f"subprocess.{name}()"))
        elif module == "subprocess" and name in _SUBPROCESS_CALLS:
            shell_true = any(
                kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                for kw in node.keywords
            )
            if shell_true:
                hits.append((node.lineno, f"subprocess.{name}(shell=True)"))
    return sorted(hits)


# -- unknown SDK helpers: an rw_* command rw.sh doesn't define ------------------

#: The public helpers rw.sh defines; any other `rw_*` command is a typo or an
#: invented helper that bash reports as "command not found" and carries on.
RW_SH_HELPERS = ("rw_input", "rw_append", "rw_set", "rw_skip")

# An rw_* word in command position: _COMMAND_START, plus a backtick
# substitution. Not followed by `=`/`+=` (an assignment to a variable that
# happens to start with rw_) or by a word character.
_RW_COMMAND_RE = re.compile(
    r"(?m)(?:" + _COMMAND_START + r"|`[ \t]*)\b(rw_[a-z_]+)(?![A-Za-z0-9_]|\+?=)"
)
_RW_FUNCTION_DEF_RE = re.compile(
    r"(?m)^[ \t]*(?:function[ \t]+(rw_[a-z_]+)\b|(rw_[a-z_]+)[ \t]*\(\s*\))"
)


def _blank_double_quoted(text: str) -> str:
    """Blanks the literal text of every "..." string (same length, line breaks
    kept), keeping each `$(...)` inside one: its body is code. Expects
    single-quoted bodies to be blanked already (_blank_single_quoted)."""
    out = list(text)
    in_double = [False]  # one entry per open `$(`/`(`, as in _single_quoted_spans
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            if in_double[-1]:
                out[i] = " "
                if i + 1 < len(text) and text[i + 1] != "\n":
                    out[i + 1] = " "
            i += 2
            continue
        if ch == '"':
            in_double[-1] = not in_double[-1]
        elif ch == "(" and (not in_double[-1] or text[i - 1 : i] == "$"):
            in_double.append(False)
        elif ch == ")" and not in_double[-1] and len(in_double) > 1:
            in_double.pop()
        elif in_double[-1] and ch != "\n":
            out[i] = " "
        i += 1
    return "".join(out)


def bash_defined_rw_functions(text: str) -> set[str]:
    """Every `rw_*` function the script defines (`rw_foo() {`, `function rw_foo`)."""
    text = _strip_comments_and_heredocs(text)
    return {m.group(1) or m.group(2) for m in _RW_FUNCTION_DEF_RE.finditer(text)}


def bash_unknown_sdk_helpers(
    text: str, defined: frozenset[str] = frozenset()
) -> list[tuple[int, str]]:
    """(line, name) for every `rw_*` word used as a command that is neither one
    of rw.sh's helpers (RW_SH_HELPERS), a function the script itself defines,
    nor one in `defined` (functions from lib/ or setup). Comments, heredoc
    bodies and quoted strings are never scanned."""
    known = set(RW_SH_HELPERS) | bash_defined_rw_functions(text) | set(defined)
    stripped = _strip_comments_and_heredocs(text)
    starts = _line_starts(stripped)
    blanked = _blank_double_quoted(_blank_single_quoted(stripped))
    return [
        (_line_of(starts, m.start(1)), m.group(1))
        for m in _RW_COMMAND_RE.finditer(blanked)
        if m.group(1) not in known
    ]


# -- unused inputs: a secret/credential the task never reads ---------------------


def bash_input_used(text: str, name: str) -> bool:
    """True if the bash task mentions the declared input `name` at all: its
    env var under the new or the legacy name (`$NAME`, `${NAME}`, `printenv
    NAME`, any whole-word `NAME` -- even one assigned locally,
    `NAME="${NAME:-x}"`, is a read) or `rw_input name`. Comments are
    ignored; heredoc bodies and single quotes are not, because an unquoted
    heredoc expands `$NAME`. Biased to "used": a spurious mention only hides
    a warning."""
    text = _strip_comments_and_heredocs(text, keep_heredocs=True)
    # The legacy (pre-H48) spelling counts too: the host still sets it, and
    # the reserved-name warning tells authors to read it (`ENV` -> $E_N_V).
    for env in {_input_env_name(name), _legacy_input_env_name(name)}:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(env)}(?![A-Za-z0-9_])", text):
            return True
    return any(m.group(1) == name for m in _RW_INPUT_RE.finditer(text))


def _input_env_name(name: str) -> str:
    from .manifest import input_env_name

    return input_env_name(name)


def _legacy_input_env_name(name: str) -> str:
    from .manifest import legacy_input_env_name

    return legacy_input_env_name(name)


def python_input_used(source: str, name: str) -> bool | None:
    """Whether `main` uses the declared input `name`: the matching parameter
    (camelCase -> snake_case) is read somewhere in the body, or
    `ctx.credential("name")` names it. `**kwargs`, `locals()`, a dynamic
    `ctx.credential(x)` and any mention of RW_INPUTS_JSON count as using
    everything. None when there is no parseable top-level `main`."""
    from .manifest import python_kwarg_name

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
    if main_def.args.kwarg is not None:
        return True
    param = python_kwarg_name(name)
    for stmt in main_def.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id == param:
                    return True
                if node.id == "locals":
                    return True
            elif isinstance(node, ast.Constant) and node.value == "RW_INPUTS_JSON":
                return True
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "credential" and node.args:
                    arg = node.args[0]
                    if not isinstance(arg, ast.Constant):
                        return True
                    if arg.value == name:
                        return True
    return False


_KUBECTL_RE = re.compile(r"\bkubectl\b")


def runs_kubectl(text: str) -> bool:
    """True if `kubectl` appears anywhere in the task's code (bash or
    Python), outside `#` comments -- KUBECONFIG is set for it automatically."""
    return _KUBECTL_RE.search(_strip_comments_and_heredocs(text, keep_heredocs=True)) is not None


# -- unknown commands: one the rw-task image doesn't ship (W_UNKNOWN_COMMAND) -----

# Command position, as _COMMAND_START: a line start or a command separator
# (`;`, `&`, `|`, `(`, `{`, `!`; a backtick substitution is rewritten to
# `(...)` first), then any compound keywords (`if`, `then`, `do`, `else`, ...),
# then any env-prefix assignments (`FOO=1 BAR="a b" cmd`). Stricter than
# _COMMAND_START in one way: a keyword counts only at command position
# itself, so `echo please do this` never makes `this` a command. The command
# word must end where a word ends (blank, separator, redirection), which
# rules out an assignment (`x=1`), a function definition (`f() {`), a path
# (`lib/run.sh`) and a brace expansion (`{a,b}`).
_KEYWORDS_BEFORE_COMMAND = r"(?:(?:then|do|else|while|until|if|elif|time|!)[ \t]+)*"
_ENV_PREFIX = r"""(?:[A-Za-z_][A-Za-z0-9_]*\+?=(?:"[^"\n]*"|'[^'\n]*'|[^\s;&|()"'])*[ \t]+)*"""
_COMMAND_WORD_RE = re.compile(
    r"(?m)(?:^|[;&|({!])[ \t]*"
    + _KEYWORDS_BEFORE_COMMAND
    + _ENV_PREFIX
    + r"([A-Za-z_][A-Za-z0-9_.+-]*)(?=[ \t;&|)<>]|$)"
)
_FUNCTION_DEF_RE = re.compile(
    r"(?m)^[ \t]*(?:function[ \t]+([A-Za-z_][\w.:-]*)|([A-Za-z_][\w.:-]*)[ \t]*\(\s*\))"
)
_ALIAS_RE = re.compile(r"(?m)(?:^|[;&|])[ \t]*alias[ \t]+([A-Za-z_][\w.:-]*)=")

# Length-preserving rewrites (same positions, same line breaks) that take
# text out of command position before _COMMAND_WORD_RE runs.
_BACKTICK_RE = re.compile(r"`([^`]*)`")
_CONTINUATION_RE = re.compile(r"\\\n")
_PARAM_EXPANSION_RE = re.compile(r"\$\{[^{}\n]*\}")  # ${x:-y}, ${!ref}: no command
_ARITHMETIC_RE = re.compile(r"\(\((?:[^()\n]|\([^()\n]*\))*\)\)")  # (( )), $(( )), for (( ))
_ARRAY_RE = re.compile(r"(?<=[A-Za-z0-9_\]]=)\([^()]*\)|(?<=[A-Za-z0-9_\]]\+=)\([^()]*\)")
# A case pattern: after `case ... in` or a `;;`/`;&`/`;;&`, up to its `)`.
_CASE_PATTERN_RE = re.compile(r"(?:\bin|;;&?|;&)\s*(\(?[^()\n;]*\))")
_REGEX_MATCH_RE = re.compile(r"=~[ \t]+(?:\\.|[^ \t\n])*")  # [[ $x =~ ^(a|b)$ ]]


def _blank(match: re.Match, group: int = 0) -> str:
    whole = match.group(0)
    start, end = match.start(group) - match.start(0), match.end(group) - match.start(0)
    inner = "".join("\n" if ch == "\n" else " " for ch in whole[start:end])
    return whole[:start] + inner + whole[end:]


def _command_scan_text(text: str) -> str:
    """`text` (comments, heredocs and quoted literals already blanked) with
    everything that only looks like a command position blanked too."""
    text = _CONTINUATION_RE.sub("  ", text)
    for _ in range(3):  # ${a:-${b}} -- the inner one first
        text = _PARAM_EXPANSION_RE.sub(_blank, text)
    text = _ARITHMETIC_RE.sub(_blank, text)
    text = _ARRAY_RE.sub(_blank, text)
    # after arrays: `x=`cmd`` becomes `x=(cmd)`, which is not an array
    text = _BACKTICK_RE.sub(lambda m: "(" + m.group(1) + ")", text)
    text = _REGEX_MATCH_RE.sub(_blank, text)
    return _CASE_PATTERN_RE.sub(lambda m: _blank(m, 1), text)


def bash_defined_functions(text: str) -> set[str]:
    """Every function (`f() {`, `function f`) and alias (`alias f=...`) the
    script defines, whatever its name."""
    text = _strip_comments_and_heredocs(text)
    names = {m.group(1) or m.group(2) for m in _FUNCTION_DEF_RE.finditer(text)}
    return names | {m.group(1) for m in _ALIAS_RE.finditer(text)}


def bash_unknown_commands(
    text: str, defined: frozenset[str] = frozenset()
) -> list[tuple[int, str]]:
    """(line of first use, name) for every distinct command word that is not a
    bash builtin or keyword, an `rw_*` helper (E_UNKNOWN_SDK_HELPER's job), a
    function or alias in `defined` or in the script itself, or a command on
    the rw-task image (runtime_commands.RW_TASK_COMMANDS). Paths (`./x`,
    `/usr/bin/x`) and expansions (`$TOOL`) are never a command word here.
    Comments, heredoc bodies and quoted strings are never scanned; a command
    run through another (`xargs cmd`, `timeout 5 cmd`) is not seen."""
    from .runtime_commands import BASH_BUILTINS_AND_KEYWORDS, RW_TASK_COMMANDS

    known = BASH_BUILTINS_AND_KEYWORDS | RW_TASK_COMMANDS | defined | bash_defined_functions(text)
    stripped = _strip_comments_and_heredocs(text)
    starts = _line_starts(stripped)
    scanned = _command_scan_text(_blank_double_quoted(_blank_single_quoted(stripped)))
    found: dict[str, int] = {}
    for m in _COMMAND_WORD_RE.finditer(scanned):
        name = m.group(1)
        if name in known or name.startswith("rw_") or name in found:
            continue
        found[name] = _line_of(starts, m.start(1))
    return [(line, name) for name, line in found.items()]
