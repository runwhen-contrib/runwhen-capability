"""Declared effects: required on a task that isn't readOnly, optional on one that is."""

from __future__ import annotations

from runwhen_capability.custom import E_EFFECTS_REQUIRED, compile_manifest, validate

TASK = "print('hi')\n"


def _files(task_block: str) -> dict[str, str]:
    manifest = (
        "apiVersion: runwhen.com/custom-capability/v1\n"
        "name: storage-maintenance\n"
        "appliesTo:\n"
        "  - { platform: kubernetes, type: statefulset }\n"
        "tasks:\n" + task_block
    )
    return {"capability.yaml": manifest, "tasks/vacuum.py": "def main(ctx):\n    return {}\n"}


WRITE_NO_EFFECTS = "  - name: vacuum\n    file: tasks/vacuum.py\n    readOnly: false\n"
WRITE_WITH_EFFECTS = WRITE_NO_EFFECTS + (
    "    effects:\n      - Compacts SeaweedFS volumes whose garbage ratio exceeds the threshold\n"
)
READ_WITH_EFFECTS = (
    "  - name: vacuum\n"
    "    file: tasks/vacuum.py\n"
    "    readOnly: true\n"
    "    effects: [Reads volume status from the master]\n"
)


def _codes(files):
    return [(d.code, d.path, d.line) for d in validate(files) if d.severity == "error"]


def test_a_write_task_without_effects_is_refused_at_its_line():
    assert _codes(_files(WRITE_NO_EFFECTS)) == [(E_EFFECTS_REQUIRED, "tasks[0].effects", 6)]


def test_a_write_task_with_effects_validates():
    assert _codes(_files(WRITE_WITH_EFFECTS)) == []


def test_blank_effects_do_not_count():
    blank = WRITE_NO_EFFECTS + "    effects: ['  ']\n"
    assert [c for c, _p, _l in _codes(_files(blank))] == [E_EFFECTS_REQUIRED]


def test_effects_are_allowed_on_a_read_only_task():
    assert _codes(_files(READ_WITH_EFFECTS)) == []


def test_effects_are_compiled_beside_read_only():
    [task] = compile_manifest(_files(WRITE_WITH_EFFECTS))["tasks"]
    assert task["readOnly"] is False
    assert task["effects"] == [
        "Compacts SeaweedFS volumes whose garbage ratio exceeds the threshold"
    ]
