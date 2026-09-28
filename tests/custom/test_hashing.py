"""content_hash/task_hash -- determinism, path-order independence, and the
narrower set of files task_hash actually covers."""

from __future__ import annotations

import pytest
from bundle_fixtures import load_bundle

from runwhen_capability.custom.hashing import content_hash, task_files, task_hash


def test_content_hash_is_a_sha256_uri():
    files = load_bundle("pgbouncer-health")
    h = content_hash(files)
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 64


def test_content_hash_is_deterministic_regardless_of_dict_order():
    files = load_bundle("pgbouncer-health")
    reordered = dict(reversed(list(files.items())))
    assert content_hash(files) == content_hash(reordered)


def test_content_hash_changes_when_any_file_changes():
    files = load_bundle("pgbouncer-health")
    changed = dict(files)
    changed["tasks/pool_errors.sh"] += "\n# a comment\n"
    assert content_hash(files) != content_hash(changed)


def test_content_hash_changes_on_rename_even_with_identical_content():
    a = {"capability.yaml": "x", "tasks/a.sh": "same"}
    b = {"capability.yaml": "x", "tasks/b.sh": "same"}
    assert content_hash(a) != content_hash(b)


def test_task_files_covers_capability_yaml_lib_and_the_tasks_own_file():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
setup: { file: lib/setup.py }
tasks:
  - name: a
    file: tasks/a.sh
  - name: b
    file: tasks/b.sh
""",
        "lib/setup.py": "def main(ctx):\n    return {}\n",
        "lib/helpers.sh": "helper() { :; }\n",
        "tasks/a.sh": "echo a\n",
        "tasks/b.sh": "echo b\n",
    }
    assert task_files(files, "a") == [
        "capability.yaml",
        "lib/helpers.sh",
        "lib/setup.py",
        "tasks/a.sh",
    ]


def test_task_hash_is_unaffected_by_an_unrelated_tasks_file():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: a
    file: tasks/a.sh
  - name: b
    file: tasks/b.sh
""",
        "tasks/a.sh": "echo a\n",
        "tasks/b.sh": "echo b\n",
    }
    before = task_hash(files, "a")
    changed = dict(files)
    changed["tasks/b.sh"] = "echo changed\n"
    after = task_hash(changed, "a")
    assert before == after


def test_task_hash_changes_when_the_tasks_own_file_changes():
    files = load_bundle("pgbouncer-health")
    before = task_hash(files, "pool-errors")
    changed = dict(files)
    changed["tasks/pool_errors.sh"] += "\necho more\n"
    after = task_hash(changed, "pool-errors")
    assert before != after


def test_task_hash_changes_when_a_shared_lib_file_changes():
    files = {
        "capability.yaml": """\
apiVersion: runwhen.com/custom-capability/v1
name: x
tasks:
  - name: a
    file: tasks/a.sh
""",
        "lib/shared.sh": "shared() { :; }\n",
        "tasks/a.sh": 'source "$RW_SDK/rw.sh"\nsource "$(dirname "$0")/../lib/shared.sh"\n',
    }
    before = task_hash(files, "a")
    changed = dict(files)
    changed["lib/shared.sh"] = "shared() { echo changed; }\n"
    after = task_hash(changed, "a")
    assert before != after


def test_task_hash_raises_key_error_for_an_unknown_task():
    files = load_bundle("pgbouncer-health")
    with pytest.raises(KeyError):
        task_hash(files, "no-such-task")
