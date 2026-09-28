"""validate() -- one test per static error code, plus the clean-bundle case."""

from __future__ import annotations

import pytest
from bundle_fixtures import load_bundle

from runwhen_capability.custom import validate
from runwhen_capability.custom.diagnostics import (
    E_DUPLICATE_NAME,
    E_LIMIT,
    E_MANIFEST_SCHEMA,
    E_OUTPUT_UNDECLARED,
    E_PATH_NOT_ALLOWED,
    E_READONLY_WRITE,
    E_SCHEMA_NOTATION,
    E_TASK_FILE_MISSING,
    E_UNDECLARED_INPUT,
)

BASE_MANIFEST = """\
apiVersion: runwhen.com/custom-capability/v1
name: pgbouncer-health
tasks:
  - name: pool-errors
    file: tasks/pool_errors.sh
    outputs:
      summary: {{ schema: "{schema}" }}
"""


def _codes(files: dict[str, str]) -> set[str]:
    return {d.code for d in validate(files)}


def test_a_clean_bundle_has_no_diagnostics():
    assert validate(load_bundle("pgbouncer-health")) == []


def test_e_manifest_schema_on_missing_capability_yaml():
    assert E_MANIFEST_SCHEMA in _codes({})


def test_e_manifest_schema_on_bad_yaml():
    assert E_MANIFEST_SCHEMA in _codes({"capability.yaml": "not: valid: yaml: at: all: ["})


def test_e_manifest_schema_on_missing_required_field():
    files = {"capability.yaml": "apiVersion: runwhen.com/custom-capability/v1\n"}
    diagnostics = validate(files)
    assert any(d.code == E_MANIFEST_SCHEMA and d.path == "name" for d in diagnostics)


def test_e_manifest_schema_on_wrong_api_version():
    files = {"capability.yaml": ("apiVersion: runwhen.com/custom-capability/v2\nname: x\n")}
    diagnostics = validate(files)
    assert any(d.code == E_MANIFEST_SCHEMA and d.path == "apiVersion" for d in diagnostics)


def test_e_schema_notation_on_unparsable_compact_schema():
    files = {
        "capability.yaml": BASE_MANIFEST.format(schema="not a schema {"),
        "tasks/pool_errors.sh": "echo hi\n",
    }
    diagnostics = validate(files)
    assert any(d.code == E_SCHEMA_NOTATION for d in diagnostics)


def test_e_schema_notation_on_missing_escape_hatch_file():
    files = {
        "capability.yaml": BASE_MANIFEST.format(schema="./schemas/missing.json"),
        "tasks/pool_errors.sh": "echo hi\n",
    }
    diagnostics = validate(files)
    assert any(d.code == E_SCHEMA_NOTATION for d in diagnostics)


def test_e_path_not_allowed_on_a_file_outside_the_allowed_roots():
    files = dict(load_bundle("pgbouncer-health"))
    files["Dockerfile"] = "FROM scratch\n"
    assert E_PATH_NOT_ALLOWED in _codes(files)


def test_e_path_not_allowed_on_a_task_file_with_an_unsupported_extension():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.rb
""",
        "tasks/t.rb": "puts 'hi'\n",
    }
    assert E_PATH_NOT_ALLOWED in _codes(files)


def test_e_limit_on_a_file_over_the_per_file_cap():
    files = dict(load_bundle("pgbouncer-health"))
    files["tasks/pool_errors.sh"] += "x" * (64 * 1024)
    assert E_LIMIT in _codes(files)


def test_e_limit_on_too_many_files():
    files = dict(load_bundle("pgbouncer-health"))
    files.update({f"tests/f{i}.txt": "x" for i in range(45)})
    assert E_LIMIT in _codes(files)


def test_e_limit_on_total_bundle_size():
    files = dict(load_bundle("pgbouncer-health"))
    # Five ~60 KiB files, each under the per-file cap on its own, together
    # well over the 256 KiB bundle cap.
    files.update({f"tests/f{i}.txt": "x" * (60 * 1024) for i in range(5)})
    assert E_LIMIT in _codes(files)


def test_e_task_file_missing():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/missing.sh
"""
    }
    diagnostics = validate(files)
    assert any(d.code == E_TASK_FILE_MISSING and d.path == "tasks[0].file" for d in diagnostics)


def test_e_task_file_missing_on_setup():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
setup: { file: lib/missing.py }
tasks: []
"""
    }
    diagnostics = validate(files)
    assert any(d.code == E_TASK_FILE_MISSING and d.path == "setup.file" for d in diagnostics)


def test_e_undeclared_input_python_parameter():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.py
""",
        "tasks/t.py": "def main(ctx, mystery):\n    return {}\n",
    }
    diagnostics = validate(files)
    assert any(d.code == E_UNDECLARED_INPUT and "mystery" in d.message for d in diagnostics)


def test_e_undeclared_input_python_parameter_carries_its_own_line():
    """The line reported is the offending parameter's own -- so an editor
    (or an agent's edit tool) can point straight at it -- not main()'s
    def line or the file's first line."""
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.py
""",
        "tasks/t.py": "def main(\n    ctx,\n    mystery,\n):\n    return {}\n",
    }
    [diag] = [d for d in validate(files) if d.code == E_UNDECLARED_INPUT and "mystery" in d.message]
    assert diag.file == "tasks/t.py"
    assert diag.line == 3


def test_e_undeclared_input_bash_carries_its_line():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.sh
""",
        "tasks/t.sh": 'echo start\necho "$MYSTERY"\n',
    }
    [diag] = [d for d in validate(files) if d.code == E_UNDECLARED_INPUT]
    assert diag.file == "tasks/t.sh"
    assert diag.line == 2


def test_e_undeclared_input_python_kwargs_is_not_flagged():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.py
""",
        "tasks/t.py": "def main(ctx, **inputs):\n    return {}\n",
    }
    assert E_UNDECLARED_INPUT not in _codes(files)


def test_e_undeclared_input_bash_env_var():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.sh
""",
        "tasks/t.sh": 'echo "$MYSTERY"\n',
    }
    diagnostics = validate(files)
    assert any(d.code == E_UNDECLARED_INPUT and "MYSTERY" in d.message for d in diagnostics)


def test_e_undeclared_input_bash_rw_input_call():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.sh
""",
        "tasks/t.sh": "rw_input mystery\n",
    }
    diagnostics = validate(files)
    assert any(d.code == E_UNDECLARED_INPUT and "mystery" in d.message for d in diagnostics)


def test_e_undeclared_input_bash_declared_input_is_not_flagged():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.sh
    inputs:
      since: { type: duration, runtime: true }
""",
        "tasks/t.sh": 'echo "$SINCE"\nrw_input since\n',
    }
    assert E_UNDECLARED_INPUT not in _codes(files)


def test_e_undeclared_input_bash_local_variable_is_not_flagged():
    """A plain shell variable the script assigns itself (`content=...`, a
    `for` loop binding) is not an input read, even if nothing declares an
    input of the same name."""
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.sh
""",
        "tasks/t.sh": (
            'content=$(cat somefile)\necho "$content"\nfor item in a b c; do echo "$item"; done\n'
        ),
    }
    assert E_UNDECLARED_INPUT not in _codes(files)


def test_e_output_undeclared_python_return():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.py
    outputs:
      known: { schema: "string" }
""",
        "tasks/t.py": 'def main(ctx):\n    return {"known": "x", "mystery": 1}\n',
    }
    diagnostics = validate(files)
    assert any(d.code == E_OUTPUT_UNDECLARED and "mystery" in d.message for d in diagnostics)
    assert not any("known" in d.message for d in diagnostics if d.code == E_OUTPUT_UNDECLARED)


def test_e_output_undeclared_bash_rw_append():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.sh
    outputs:
      known: { schema: "string[]" }
""",
        "tasks/t.sh": 'source "$RW_SDK/rw.sh"\nrw_append mystery "1"\n',
    }
    diagnostics = validate(files)
    assert any(d.code == E_OUTPUT_UNDECLARED and "mystery" in d.message for d in diagnostics)


def test_e_readonly_write_bash():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.sh
    readOnly: true
""",
        "tasks/t.sh": "kubectl delete pod x\n",
    }
    diagnostics = validate(files)
    assert any(d.code == E_READONLY_WRITE for d in diagnostics)


def test_e_readonly_write_rollout_restart():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.sh
    readOnly: true
""",
        "tasks/t.sh": "kubectl rollout restart deployment/x\n",
    }
    assert E_READONLY_WRITE in _codes(files)


def test_e_readonly_write_python_ctx_run():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.py
    readOnly: true
""",
        "tasks/t.py": (
            'def main(ctx):\n    ctx.run(["kubectl", "delete", "pod", "x"])\n    return {}\n'
        ),
    }
    assert E_READONLY_WRITE in _codes(files)


def test_read_only_task_may_still_read():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.sh
    readOnly: true
""",
        "tasks/t.sh": "kubectl get pods\nkubectl describe pod x\n",
    }
    assert E_READONLY_WRITE not in _codes(files)


def test_e_duplicate_name_task():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/a.sh
  - name: t
    file: tasks/b.sh
""",
        "tasks/a.sh": "echo a\n",
        "tasks/b.sh": "echo b\n",
    }
    diagnostics = validate(files)
    assert any(d.code == E_DUPLICATE_NAME and d.path == "tasks[1].name" for d in diagnostics)


def test_a_comment_mentioning_rw_append_is_not_an_undeclared_output():
    """A `#` comment that happens to say "rw_append errors ..." as prose
    (documenting the real call below it) must not itself be scanned as a
    call -- regression: the naive regex matched across the comment's
    trailing newline into the next line's first word too."""
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.sh
    outputs:
      message: { schema: "string" }
""",
        "tasks/t.sh": (
            "# rw_append errors is how you would append, but this task uses rw_set\n"
            'source "$RW_SDK/rw.sh"\n'
            'rw_set message "\\"hi\\""\n'
        ),
    }
    assert validate(files) == []


def test_kubectl_delete_in_a_comment_is_not_a_readonly_violation():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.sh
    readOnly: true
""",
        "tasks/t.sh": "# do not run kubectl delete here, only reads below\nkubectl get pods\n",
    }
    assert E_READONLY_WRITE not in _codes(files)


def test_a_heredoc_body_is_not_scanned_for_calls_or_readonly_writes():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/t.sh
    readOnly: true
""",
        "tasks/t.sh": (
            "cat <<EOF\n"
            "Example: rw_append errors 1, or kubectl delete pod x\n"
            "EOF\n"
            "kubectl get pods\n"
        ),
    }
    assert validate(files) == []


def test_diagnostics_carry_file_and_line_when_available():
    files = {
        "capability.yaml": BASE_MANIFEST.format(schema="not a schema {"),
        "tasks/pool_errors.sh": "echo hi\n",
    }
    diagnostics = [d for d in validate(files) if d.code == E_SCHEMA_NOTATION]
    assert diagnostics
    assert diagnostics[0].file == "capability.yaml"
    assert diagnostics[0].line is not None


# -- paths and names the bundle host would write or export unsafely -----------

_NAMED_MANIFEST = """\
apiVersion: runwhen.com/custom-capability/v1
name: {name}
inputs:
  {input}: {{ type: secret, optional: true }}
tasks:
  - name: {task}
    file: tasks/t.sh
"""


def _named(name="x", input_name="token", task="t") -> dict[str, str]:
    return {
        "capability.yaml": _NAMED_MANIFEST.format(name=name, input=input_name, task=task),
        "tasks/t.sh": "echo hi\n",
    }


@pytest.mark.parametrize(
    "path",
    [
        "tasks/..",
        "lib/x/..",
        "tasks/./x.sh",
        "tasks//x.sh",
        "tasks/",
        "tasks/a\x00b.sh",
        "tasks/a\nb.sh",
        "tasks\\..\\x.sh",
        "/tasks/x.sh",
        "tasks/" + "a" * 300,
    ],
)
def test_e_path_not_allowed_on_a_path_that_is_not_a_plain_relative_file(path):
    files = {**_named(), path: "echo\n"}
    assert any(d.code == E_PATH_NOT_ALLOWED and d.file == path for d in validate(files))


def test_paths_that_collide_on_a_case_insensitive_filesystem_are_duplicates():
    files = {**_named(), "tasks/T.sh": "kubectl delete pod x\n"}
    assert any(d.code == E_DUPLICATE_NAME for d in validate(files))


def test_a_path_that_would_need_a_file_to_be_a_directory_is_not_allowed():
    files = {**_named(), "lib/a": "x\n", "lib/a/b.sh": "y\n"}
    assert any(d.code == E_PATH_NOT_ALLOWED and d.file == "lib/a/b.sh" for d in validate(files))


@pytest.mark.parametrize("input_name", ['"/tmp/x"', '"../x"', '"a-b"', '"a b"', "_x"])
def test_an_input_name_that_is_not_an_identifier_is_rejected(input_name):
    diagnostics = validate(_named(input_name=input_name))
    assert any(d.code == E_MANIFEST_SCHEMA and "identifier" in d.message for d in diagnostics)


@pytest.mark.parametrize(
    "input_name", ["path", "home", "ldPreload", "bashEnv", "rwOutputFd", "ifs"]
)
def test_an_input_name_that_maps_onto_a_reserved_env_var_is_rejected(input_name):
    diagnostics = validate(_named(input_name=input_name))
    assert any(d.code == E_MANIFEST_SCHEMA and "reserved" in d.message for d in diagnostics)


def test_a_kubeconfig_credential_input_may_still_set_kubeconfig():
    assert validate(load_bundle("pgbouncer-health")) == []


def test_two_inputs_that_arrive_under_the_same_name_are_duplicates():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
inputs:
  maxWait: { type: integer, default: 1 }
tasks:
  - name: t
    file: tasks/t.sh
    inputs:
      max_wait: { type: integer, default: 2 }
""",
        "tasks/t.sh": "echo $MAX_WAIT\n",
    }
    assert any(d.code == E_DUPLICATE_NAME for d in validate(files))


@pytest.mark.parametrize("name", ["Bad", "has space", "under_score", "-lead", "trail-", "a" * 64])
def test_capability_and_task_names_must_be_lowercase_slugs(name):
    as_capability = validate(_named(name=f'"{name}"'))
    as_task = validate(_named(task=f'"{name}"'))
    assert any(d.code == E_MANIFEST_SCHEMA and d.path == "name" for d in as_capability)
    assert any(d.code == E_MANIFEST_SCHEMA and d.path == "tasks[0].name" for d in as_task)
