"""Diagnostic -- one static-check finding against a custom capability bundle,
and the error codes `validate()`/`compile_manifest()` (validate.py, compiler.py)
raise them under.

Static codes (validate() only, no execution):

- E_MANIFEST_SCHEMA -- the manifest doesn't match its schema.
- E_SCHEMA_NOTATION -- a compact schema can't be parsed.
- E_PATH_NOT_ALLOWED -- a file is outside the allowed paths.
- E_LIMIT -- a size or file-count limit is exceeded.
- E_TASK_FILE_MISSING -- a task's (or setup's) `file` doesn't exist.
- E_UNDECLARED_INPUT -- a Python main() parameter, or a bash $VAR read (directly
  or via rw_input), with no declared input.
- E_OUTPUT_UNDECLARED -- a returned or rw_append/rw_set output name that isn't
  declared.
- E_READONLY_WRITE -- kubectl delete/apply/patch/edit/scale/rollout restart/exec
  and similar in a readOnly task.
- E_DUPLICATE_NAME -- a name is used twice.
- E_EFFECTS_REQUIRED -- a task that isn't readOnly declares no effects.
- E_UNKNOWN_SDK_HELPER -- a bash task calls an `rw_*` command rw.sh doesn't define
  (only rw_input, rw_append, rw_set and rw_skip exist) and the script doesn't
  define itself. An authoring gate: the bundle host ignores it at run time.
- E_SCHEMA_FEATURE -- a schema uses a JSON Schema feature custom tasks may not
  use (regex keywords: `pattern`, `patternProperties`).

Warning codes (validate() only; severity "warning", never block a write or a run):

- W_DATA_AS_CODE -- a task builds a command from text (bash `eval`/`sh -c` with an
  expansion, awk `system(`/`getline`, Python `shell=True`/`os.system`).
- W_UNUSED_INPUT -- a declared secret/credential input the task never reads.

Runtime codes (the bundle host, bundle.py) reuse the same string constants:
E_INPUT_TYPE, E_OUTPUT_SCHEMA, E_OUTPUT_TOO_LARGE, E_OUTPUT_MALFORMED, E_TIMEOUT.
E_OUTPUT_MALFORMED fails a run when a line on the private output channel is not
a well-formed rw_set/rw_append/rw_skip event (the line is dropped, so an output
was lost); well-formed lines still populate outputs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Severity = Literal["error", "warning"]

# -- static codes: validate() -------------------------------------------------
E_MANIFEST_SCHEMA = "E_MANIFEST_SCHEMA"
E_SCHEMA_NOTATION = "E_SCHEMA_NOTATION"
E_PATH_NOT_ALLOWED = "E_PATH_NOT_ALLOWED"
E_LIMIT = "E_LIMIT"
E_TASK_FILE_MISSING = "E_TASK_FILE_MISSING"
E_UNDECLARED_INPUT = "E_UNDECLARED_INPUT"
E_OUTPUT_UNDECLARED = "E_OUTPUT_UNDECLARED"
E_READONLY_WRITE = "E_READONLY_WRITE"
E_DUPLICATE_NAME = "E_DUPLICATE_NAME"
E_SCHEMA_FEATURE = "E_SCHEMA_FEATURE"
E_EFFECTS_REQUIRED = "E_EFFECTS_REQUIRED"
E_UNKNOWN_SDK_HELPER = "E_UNKNOWN_SDK_HELPER"

# -- warning codes: validate() ---------------------------------------------------
W_DATA_AS_CODE = "W_DATA_AS_CODE"
W_UNUSED_INPUT = "W_UNUSED_INPUT"

# -- runtime codes: the bundle host (bundle.py) --------------------------------
E_INPUT_TYPE = "E_INPUT_TYPE"
E_OUTPUT_SCHEMA = "E_OUTPUT_SCHEMA"
E_OUTPUT_TOO_LARGE = "E_OUTPUT_TOO_LARGE"
E_OUTPUT_MALFORMED = "E_OUTPUT_MALFORMED"
E_TIMEOUT = "E_TIMEOUT"


class Diagnostic(BaseModel):
    """One validate() finding. `file` is the bundle-relative path the finding
    is about (e.g. "capability.yaml", "tasks/pool_errors.sh"); `line` is
    1-indexed and `None` when no location could be attributed. `path` is a
    dotted/bracketed pointer into the manifest's data (e.g.
    "tasks[0].outputs.errors.schema"), independent of `file`/`line` -- it is
    set for manifest-shaped findings and `None` for source-file findings
    (undeclared input/output, readOnly writes), which carry their location in
    `file`/`line` instead."""

    code: str
    severity: Severity = "error"
    file: str | None = None
    line: int | None = None
    path: str | None = None
    message: str
    hint: str | None = None
