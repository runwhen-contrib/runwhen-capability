"""validate() -- one test per static error code, plus the clean-bundle case."""

from __future__ import annotations

import json
import time

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
    E_SCHEMA_FEATURE,
    E_SCHEMA_NOTATION,
    E_TASK_FILE_MISSING,
    E_UNDECLARED_INPUT,
    W_DATA_AS_CODE,
    W_UNUSED_INPUT,
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
    effects: [Test fixture]
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


# -- validate() on crafted input: bounded time, never raises ------------------

_ONE_TASK = """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: t
    file: tasks/{file}
    effects: [Test fixture]
    outputs:
      o: {{ schema: "{schema}" }}
"""


def _one_task(source: str, file: str = "t.sh", schema: str = "string", **extra) -> dict:
    return {
        "capability.yaml": _ONE_TASK.format(file=file, schema=schema),
        f"tasks/{file}": source,
        **extra,
    }


def _timed_validate(files):
    started = time.monotonic()
    diagnostics = validate(files)
    return diagnostics, time.monotonic() - started


@pytest.mark.parametrize(
    "source",
    [
        "# " + "a" * 60000 + "\n",  # a long comment line is blanked to spaces before scanning
        " " * 60000 + "x\n",
        "echo " + "${a-" * 15000 + "\n",  # unclosed expansions
    ],
    ids=["long-comment", "long-blank-run", "unclosed-expansions"],
)
def test_bash_scans_stay_linear_on_crafted_source(source):
    _, elapsed = _timed_validate(_one_task(source))
    assert elapsed < 2


def test_deeply_nested_python_does_not_raise():
    source = "def main(ctx):\n    x = 1" + "+1" * 20000 + "\n    return {}\n"
    diagnostics = validate(_one_task(source, file="t.py"))
    assert all(d.code != E_MANIFEST_SCHEMA for d in diagnostics)


@pytest.mark.parametrize("schema", ["{a:" * 40 + "string" + "}" * 40, "integer" + "[]" * 40])
def test_a_compact_schema_nested_too_deep_is_e_schema_notation(schema):
    assert E_SCHEMA_NOTATION in _codes(_one_task("echo\n", schema=schema))


def test_deeply_nested_yaml_is_e_manifest_schema_not_an_exception():
    assert E_MANIFEST_SCHEMA in _codes({"capability.yaml": "a: " + "[" * 5000 + "]" * 5000})


def test_yaml_aliases_are_refused():
    bomb = "\n".join(
        ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
        + [f"a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 10) + "]" for i in range(1, 9)]
    )
    manifest = bomb + "\n" + _ONE_TASK.format(file="t.sh", schema="string")
    diagnostics = validate({"capability.yaml": manifest, "tasks/t.sh": "echo\n"})
    assert any(d.code == E_MANIFEST_SCHEMA and "alias" in d.message for d in diagnostics)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("[" * 20000 + "]" * 20000, "not valid JSON"),
        ('{"$ref": "file:///etc/hosts"}', "not a local reference"),
        ('{"$ref": "https://example.com/s.json"}', "not a local reference"),
        ('{"type": 5}', "not a valid JSON Schema"),
        ("{" + '"items": {' * 70 + "}" * 71, "nested deeper"),
    ],
)
def test_an_escape_hatch_schema_file_must_be_local_valid_and_bounded(content, expected):
    files = _one_task("echo\n", schema="./schemas/s.json", **{"schemas/s.json": content})
    diagnostics = [d for d in validate(files) if d.code == E_SCHEMA_NOTATION]
    assert diagnostics and expected in diagnostics[0].message


def test_an_escape_hatch_schema_with_a_local_ref_is_fine():
    content = '{"$defs": {"n": {"type": "integer"}}, "$ref": "#/$defs/n"}'
    files = _one_task("echo\n", schema="./schemas/s.json", **{"schemas/s.json": content})
    assert validate(files) == []


# -- no regex keywords in custom task schemas (E_SCHEMA_FEATURE) ------------------

_REGEX_HINT = (
    "regex patterns aren't supported in custom task schemas; validate the format in the task code"
)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "pattern": "^a+$"},
        {"type": "object", "patternProperties": {"^x-": {"type": "string"}}},
        {"type": "array", "items": {"type": "string", "pattern": "^(a+)+$"}},
        {"type": "object", "properties": {"id": {"type": "string", "pattern": "^[0-9]+$"}}},
        {"anyOf": [{"type": "integer"}, {"type": "string", "pattern": "x"}]},
        {"$defs": {"s": {"pattern": "x"}}, "$ref": "#/$defs/s"},
        {"type": "object", "propertyNames": {"pattern": "^[a-z]+$"}},
    ],
    ids=["top", "patternProperties", "items", "property", "anyOf", "defs", "propertyNames"],
)
def test_a_regex_keyword_anywhere_in_a_schema_file_is_e_schema_feature(schema):
    files = _one_task("echo\n", schema="./schemas/s.json", **{"schemas/s.json": json.dumps(schema)})
    diagnostics = [d for d in validate(files) if d.code == E_SCHEMA_FEATURE]
    assert diagnostics
    assert all(d.hint == _REGEX_HINT and d.file == "schemas/s.json" for d in diagnostics)


def test_a_property_named_pattern_is_not_the_keyword():
    compact = "{ pattern: string, count: integer }[]"
    as_file = {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "enum": [{"pattern": "data, not a keyword"}],
    }
    assert validate(_one_task("echo\n", schema=compact)) == []
    files = _one_task(
        "echo\n", schema="./schemas/s.json", **{"schemas/s.json": json.dumps(as_file)}
    )
    assert validate(files) == []


def test_a_regex_keyword_on_an_input_is_e_schema_feature():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
inputs:
  host: { type: string, default: a, pattern: "^[a-z]+$" }
tasks:
  - name: t
    file: tasks/t.sh
    inputs:
      port: { type: string, default: "1", patternProperties: {} }
""",
        "tasks/t.sh": "echo $HOST $PORT\n",
    }
    diagnostics = [d for d in validate(files) if d.code == E_SCHEMA_FEATURE]
    assert {d.path for d in diagnostics} == {
        "inputs.host.pattern",
        "tasks[0].inputs.port.patternProperties",
    }
    assert all(d.hint == _REGEX_HINT for d in diagnostics)


# -- readOnly covers every file a readOnly task can run --------------------------

_READ_ONLY_WITH_SHARED_CODE = """\
apiVersion: runwhen.com/custom-capability/v1
name: x
setup: {{ file: {setup} }}
tasks:
  - name: t
    file: tasks/t.sh
    readOnly: {read_only}
"""


def _shared_code_bundle(*, setup="lib/setup.py", read_only="true", **extra) -> dict[str, str]:
    return {
        "capability.yaml": _READ_ONLY_WITH_SHARED_CODE.format(setup=setup, read_only=read_only),
        "tasks/t.sh": 'source "$RW_SDK/rw.sh"\nkubectl get pods\n',
        "lib/setup.py": "def main(ctx):\n    return {}\n",
        **extra,
    }


@pytest.mark.parametrize(
    ("path", "source", "line"),
    [
        ("lib/helpers.sh", 'helper() {\n  kubectl delete pod "$1"\n}\n', 2),
        ("lib/k8s.py", "def restart(ctx):\n    ctx.run(['kubectl', 'rollout', 'restart'])\n", 2),
        ("lib/setup.py", "def main(ctx):\n    ctx.run(['kubectl', 'apply', '-f', 'x'])\n", 2),
    ],
    ids=["lib-bash", "lib-python", "setup"],
)
def test_a_mutating_command_in_shared_code_fails_a_read_only_task(path, source, line):
    files = _shared_code_bundle(**{path: source})
    diagnostics = [d for d in validate(files) if d.code == E_READONLY_WRITE]
    assert [(d.file, d.line) for d in diagnostics] == [(path, line)]
    assert "'t'" in diagnostics[0].message


def test_a_mutating_command_in_a_setup_outside_lib_fails_a_read_only_task():
    files = _shared_code_bundle(
        setup="tasks/setup.sh", **{"tasks/setup.sh": "kubectl scale deploy/x --replicas=0\n"}
    )
    diagnostics = [d for d in validate(files) if d.code == E_READONLY_WRITE]
    assert [(d.file, d.line) for d in diagnostics] == [("tasks/setup.sh", 1)]


def test_shared_code_may_mutate_when_no_task_is_read_only():
    files = _shared_code_bundle(read_only="false", **{"lib/helpers.sh": "kubectl delete pod x\n"})
    assert E_READONLY_WRITE not in _codes(files)


# -- warnings: W_DATA_AS_CODE and W_UNUSED_INPUT ------------------------------

_WARN_MANIFEST = """\
apiVersion: runwhen.com/custom-capability/v1
name: w
inputs:
{inputs}
tasks:
  - name: t
    file: {file}
    readOnly: true
"""


def _warn_bundle(file: str, source: str, inputs: str = "  {}") -> dict[str, str]:
    inputs_block = (
        inputs if inputs.strip() != "{}" else "  unused: { type: string, optional: true }"
    )
    return {
        "capability.yaml": _WARN_MANIFEST.format(inputs=inputs_block, file=file),
        file: source,
    }


def _of(files, code):
    return [d for d in validate(files) if d.code == code]


def test_w_data_as_code_is_a_warning_with_file_line_and_hint():
    files = _warn_bundle("tasks/t.sh", 'echo start\neval "$CMD"\n')
    (diag,) = _of(files, W_DATA_AS_CODE)
    assert (diag.severity, diag.file, diag.line) == ("warning", "tasks/t.sh", 2)
    assert diag.message == (
        "eval runs text as a command; if that text comes from data (a log line, an API body), "
        "a quote breaks it and the data can run commands"
    )
    assert diag.hint == "pass data as data: environment variables, stdin, jq --arg, or Python"


def test_w_data_as_code_flags_the_sdlc_t4_awk_getline():
    src = (
        "DETAILS=$(awk '\n"
        '  { cmd = "printf \\047%s\\047 \\047" $0 "\\047 | jq -r \\047.kind\\047"\n'
        "    cmd | getline kv\n"
        '    close(cmd) }\' "$CAUSES_FILE")\n'
    )
    (diag,) = _of(_warn_bundle("tasks/t.sh", src), W_DATA_AS_CODE)
    assert diag.line == 3


def test_w_data_as_code_not_raised_for_safe_bash():
    src = "awk '{print $1}' f\njq -n --arg a \"$X\" '$a'\nbash -c 'echo fixed'\n"
    assert _of(_warn_bundle("tasks/t.sh", src), W_DATA_AS_CODE) == []


def test_w_data_as_code_flags_python_shell_true_and_os_system():
    src = (
        "import os, subprocess\ndef main(ctx):\n"
        "    subprocess.run('x', shell=True)\n    os.system('y')\n"
    )
    diags = _of(_warn_bundle("tasks/t.py", src), W_DATA_AS_CODE)
    assert [(d.severity, d.line) for d in diags] == [("warning", 3), ("warning", 4)]


def test_w_data_as_code_not_raised_for_python_argv_lists():
    src = "import subprocess\ndef main(ctx):\n    subprocess.run(['ls'])\n"
    assert _of(_warn_bundle("tasks/t.py", src), W_DATA_AS_CODE) == []


def test_data_as_code_is_not_checked_in_a_python_tasks_bash_text():
    # a Python task mentioning eval in a string is not a bash eval
    src = "def main(ctx):\n    return {'s': 'eval $x'}\n"
    assert _of(_warn_bundle("tasks/t.py", src), W_DATA_AS_CODE) == []


def test_w_unused_input_bash_secret_never_read():
    files = _warn_bundle("tasks/t.sh", "echo hi\n", "  apiToken: { type: secret }")
    (diag,) = _of(files, W_UNUSED_INPUT)
    assert diag.severity == "warning"
    assert diag.file == "capability.yaml"
    assert diag.line == 4
    assert diag.message == (
        "'apiToken' is declared but never read; it still shows on the trust card and narrows "
        "where the capability is offered"
    )


def test_w_unused_input_bash_read_by_env_or_rw_input_is_clean():
    for src in ('curl -H "$API_TOKEN"\n', "x=$(rw_input apiToken)\n"):
        files = _warn_bundle("tasks/t.sh", src, "  apiToken: { type: secret }")
        assert _of(files, W_UNUSED_INPUT) == []


def test_w_unused_input_only_applies_to_secret_and_credential():
    files = _warn_bundle("tasks/t.sh", "echo hi\n", "  n: { type: integer, default: 1 }")
    assert _of(files, W_UNUSED_INPUT) == []


def test_w_unused_input_python_param_never_used_in_body():
    src = "def main(ctx, api_token):\n    return {}\n"
    files = _warn_bundle("tasks/t.py", src, "  apiToken: { type: secret }")
    assert len(_of(files, W_UNUSED_INPUT)) == 1
    used = "def main(ctx, api_token):\n    return {'t': api_token}\n"
    files = _warn_bundle("tasks/t.py", used, "  apiToken: { type: secret }")
    assert _of(files, W_UNUSED_INPUT) == []


def test_w_unused_input_sdlc_t9b_python_http_task_with_unused_kubeconfig():
    src = (
        "import urllib.request\n"
        "def main(ctx, url):\n"
        "    return {'body': urllib.request.urlopen(url).read().decode()}\n"
    )
    files = _warn_bundle(
        "tasks/t.py",
        src,
        "  kubeconfig: { type: credential, kind: k8s.kubeconfig }\n  url: { type: string }",
    )
    (diag,) = _of(files, W_UNUSED_INPUT)
    assert "'kubeconfig'" in diag.message


@pytest.mark.parametrize(
    ("file", "src"),
    [
        ("tasks/t.sh", "kubectl get pods\n"),
        (
            "tasks/t.py",
            "import subprocess\ndef main(ctx):\n    subprocess.run(['kubectl', 'get', 'pods'])\n",
        ),
    ],
)
def test_a_kubeconfig_credential_counts_as_used_when_the_task_calls_kubectl(file, src):
    files = _warn_bundle(file, src, "  kubeconfig: { type: credential, kind: k8s.kubeconfig }")
    assert _of(files, W_UNUSED_INPUT) == []


def test_the_kubectl_exception_is_only_for_kubeconfig_credentials():
    files = _warn_bundle("tasks/t.sh", "kubectl get pods\n", "  dsn: { type: secret }")
    assert len(_of(files, W_UNUSED_INPUT)) == 1


def test_a_task_level_unused_input_points_at_the_task_inputs_line():
    files = {
        "capability.yaml": (
            "apiVersion: runwhen.com/custom-capability/v1\nname: w\ntasks:\n"
            "  - name: t\n    file: tasks/t.sh\n    readOnly: true\n    inputs:\n"
            "      tok: { type: secret }\n"
        ),
        "tasks/t.sh": "echo hi\n",
    }
    (diag,) = _of(files, W_UNUSED_INPUT)
    assert diag.line == 8


def test_a_capability_input_used_by_the_setup_file_is_not_reported():
    files = {
        "capability.yaml": (
            "apiVersion: runwhen.com/custom-capability/v1\nname: w\n"
            "inputs:\n  tok: { type: secret }\n"
            "setup: { file: lib/setup.py }\n"
            "tasks:\n  - name: t\n    file: tasks/t.sh\n    readOnly: true\n"
        ),
        "tasks/t.sh": "echo hi\n",
        "lib/setup.py": "def main(ctx):\n    ctx.credential('tok')\n",
    }
    assert _of(files, W_UNUSED_INPUT) == []


def test_the_new_warnings_never_count_as_errors():
    files = _warn_bundle(
        "tasks/t.sh", 'eval "$DSN"\n', "  dsn: { type: string }\n  tok: { type: secret }"
    )
    diagnostics = validate(files)
    assert {W_DATA_AS_CODE, W_UNUSED_INPUT} <= {d.code for d in diagnostics}
    assert all(d.severity == "warning" for d in diagnostics)
