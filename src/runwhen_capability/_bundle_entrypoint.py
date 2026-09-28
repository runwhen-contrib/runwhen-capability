"""`python3 -m runwhen_capability._bundle_entrypoint` -- the child process
bundle.py spawns to run one Python setup/task file's `main(ctx, **inputs)`.

A bundle's Python code runs in its own process, not in-process inside the
long-lived `rwtask serve` loop, for the same reason bash does: bundle.py has
to be able to SIGKILL the whole process group on a deadline without taking
the host down with it (see bundle.py's module docstring). This entrypoint is
the thin, uniform wrapper both bundle.py and rw.sh's shell functions feed the
same private-fd wire format through -- one JSON line per event:

    {"op": "set",    "name": <output>, "value": <json>}
    {"op": "append", "name": <output>, "value": <json>}
    {"op": "skip",   "reason": <string>}

A successful `main()` return value is emitted as one "set" event per key. Any
other exception is left to propagate: this process's non-zero exit code (and
its stderr, captured by bundle.py as part of the task's log) is how bundle.py
learns the task failed -- there is no separate error channel.

Everything this process needs arrives by environment variable -- see
bundle.py's _python_env() for exactly what it sets:

    RW_TASK_FILE        absolute path to the .py file to run
    RW_FUNC             the function to call (default "main")
    RW_INPUTS_JSON       inputs, JSON-encoded
    RW_OUTPUT_FD          the private fd to write wire events to
    RW_WORKDIR            Context.workdir
    RW_CAPABILITY          Context.capability
    RW_OPERATION            Context.operation
    RW_CREDENTIALS_JSON      Context credentials, JSON-encoded
    RW_ALLOW_ANONYMOUS       "1" to degrade unresolved credentials (rwtask
                              run --allow-anonymous only)
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import uuid
from pathlib import Path

from .context import Context
from .errors import SkipTask


def _write_event(fd: int, event: dict) -> None:
    os.write(fd, (json.dumps(event) + "\n").encode("utf-8"))


def main() -> int:
    task_file = Path(os.environ["RW_TASK_FILE"])
    func_name = os.environ.get("RW_FUNC", "main")
    inputs = json.loads(os.environ.get("RW_INPUTS_JSON", "{}"))
    output_fd = int(os.environ["RW_OUTPUT_FD"])
    credentials = json.loads(os.environ.get("RW_CREDENTIALS_JSON", "{}"))

    ctx = Context(
        capability=os.environ.get("RW_CAPABILITY", ""),
        operation=os.environ.get("RW_OPERATION", ""),
        workdir=Path(os.environ.get("RW_WORKDIR", ".")),
        credentials=credentials,
        allow_anonymous_credentials=os.environ.get("RW_ALLOW_ANONYMOUS") == "1",
    )

    module_name = f"runwhen_capability_bundle_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, task_file)
    if spec is None or spec.loader is None:
        print(f"could not load {task_file}", file=sys.stderr)
        return 1
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    func = getattr(module, func_name, None)
    if func is None:
        print(f"{task_file} defines no {func_name!r} function", file=sys.stderr)
        return 1

    # `inputs` is every input resolved for this run (capability-level ones
    # shared with every other task included) -- a task's own main() only
    # has to name the ones it actually wants (validate.py's
    # E_UNDECLARED_INPUT only flags the reverse: a parameter with no
    # declared input). Pass everything only when main() itself accepts
    # **kwargs; otherwise narrow to the parameters it actually declares, or
    # an unrelated shared input becomes a spurious TypeError.
    signature = inspect.signature(func)
    accepts_var_keyword = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
    )
    if not accepts_var_keyword:
        inputs = {name: value for name, value in inputs.items() if name in signature.parameters}

    try:
        outputs = func(ctx, **inputs) or {}
    except SkipTask as exc:
        reason = str(exc.args[0]) if exc.args else ""
        _write_event(output_fd, {"op": "skip", "reason": reason})
        return 0

    for name, value in outputs.items():
        _write_event(output_fd, {"op": "set", "name": name, "value": value})
    return 0


if __name__ == "__main__":
    sys.exit(main())
