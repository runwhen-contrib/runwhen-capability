"""compile_manifest() -- the packaged-capability shape, and that it refuses
an invalid bundle instead of guessing."""

from __future__ import annotations

import pytest
from bundle_fixtures import load_bundle

from runwhen_capability.custom import CompileError, compile_manifest


def test_compiles_the_pgbouncer_health_fixture():
    compiled = compile_manifest(load_bundle("pgbouncer-health"))

    assert compiled["capability"] == "pgbouncer-health"
    assert "execution" not in compiled
    assert compiled["appliesTo"] == [
        {
            "platform": "kubernetes",
            "type": "deployment",
            "where": {"namespace": "runwhen-env-staging", "labels.app": "pgbouncer"},
        }
    ]
    assert compiled["needs"] == {
        "credentials": [{"name": "kubeconfig", "kind": "k8s.kubeconfig", "optional": False}]
    }

    [task] = compiled["tasks"]
    assert task["name"] == "pool-errors"
    assert task["readOnly"] is True
    assert task["file"] == "tasks/pool_errors.sh"
    # Capability-level inputs (target, kubeconfig, statsDsn) are merged into
    # every task's own inputs -- the compiled shape carries no separate
    # top-level `inputs`.
    assert set(task["inputs"]) == {"target", "kubeconfig", "statsDsn", "since", "maxWait"}
    assert task["inputs"]["since"] == {
        "type": "duration",
        "kind": None,
        "optional": False,
        "default": "30m",
        "runtime": True,
    }
    assert task["outputs"]["errors"]["schema"]["type"] == "array"
    assert task["outputs"]["summary"]["schema"] == {
        "type": "object",
        "properties": {
            "windowMinutes": {"type": "integer"},
            "pods": {"type": "integer"},
            "total": {"type": "integer"},
        },
        "required": ["windowMinutes", "pods", "total"],
    }
    # The escape hatch: a full JSON Schema file, read verbatim.
    assert task["outputs"]["detail"]["schema"] == {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }


def test_a_task_level_input_overrides_a_capability_level_one_of_the_same_name():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
inputs:
  since: { type: duration, default: 1h }
tasks:
  - name: t
    file: tasks/t.sh
    inputs:
      since: { type: duration, default: 5m, runtime: true }
""",
        "tasks/t.sh": "echo hi\n",
    }
    [task] = compile_manifest(files)["tasks"]
    assert task["inputs"]["since"]["default"] == "5m"
    assert task["inputs"]["since"]["runtime"] is True


def test_credentials_are_deduped_across_capability_and_task_level():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
inputs:
  kubeconfig: { type: credential, kind: k8s.kubeconfig }
tasks:
  - name: a
    file: tasks/a.sh
  - name: b
    file: tasks/b.sh
    inputs:
      kubeconfig: { type: credential, kind: k8s.kubeconfig }
""",
        "tasks/a.sh": "echo a\n",
        "tasks/b.sh": "echo b\n",
    }
    compiled = compile_manifest(files)
    assert compiled["needs"]["credentials"] == [
        {"name": "kubeconfig", "kind": "k8s.kubeconfig", "optional": False}
    ]


def test_raises_compile_error_when_validate_has_errors():
    files = {"capability.yaml": "apiVersion: runwhen.com/custom-capability/v1\n"}  # no name
    with pytest.raises(CompileError) as exc_info:
        compile_manifest(files)
    assert exc_info.value.diagnostics
    assert "E_MANIFEST_SCHEMA" in str(exc_info.value)
