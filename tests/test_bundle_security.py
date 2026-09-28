"""run_bundle_request's isolation properties: how bundle files and secrets
land on disk, what a task's process inherits, and what it can reach."""

from __future__ import annotations

import json
import os
import stat
import time

import pytest

from runwhen_capability.bundle import (
    MAX_SCHEMA_ERROR_CHARS,
    MAX_SCHEMA_ERRORS,
    _validate_output_schema,
    run_bundle_request,
)
from runwhen_capability.custom.hashing import content_hash
from runwhen_capability.models import Bundle, BundleFile, BundleRequestEnvelope

_MANIFEST = """\
apiVersion: runwhen.com/custom-capability/v1
name: probe
inputs:
  token: {{ type: secret, optional: true }}
  other: {{ type: secret, optional: true }}
tasks:
  - name: t
    file: tasks/{file}
    outputs:
      o: {{ schema: "{schema}" }}
"""


def _files(file: str, source: str, schema: str = "string", **extra) -> dict[str, str]:
    return {
        "capability.yaml": _MANIFEST.format(file=file, schema=schema),
        f"tasks/{file}": source,
        **extra,
    }


def _request(files: dict[str, str], hash_: str | None = None) -> BundleRequestEnvelope:
    return BundleRequestEnvelope(
        bundle=Bundle(
            hash=hash_ or content_hash(files),
            files=[BundleFile(path=p, content=c) for p, c in files.items()],
        ),
        tasks=["t"],
    )


def _run(files, scope, credentials=None, deadline_seconds=30):
    return run_bundle_request(
        _request(files), credentials or {}, scope, deadline_seconds=deadline_seconds
    )


_PY_REPORT = """\
import json, os, stat, sys


def main(ctx, token=None):
    info = {
        "env": sorted(k for k in os.environ if k.startswith("RW_")),
        "hostOnly": [k for k in ("EXECUTOR_TOKEN_FILE", "RELAY_URL", "POOL_ID") if k in os.environ],
        "home": os.environ["HOME"],
        "tmpdir": os.environ["TMPDIR"],
        "stdin": sys.stdin.read(),
        "credentials": sorted(ctx._credentials) if hasattr(ctx, "_credentials") else None,
        "tokenMode": oct(stat.S_IMODE(os.stat(token).st_mode)) if token else None,
        "tokenPath": token,
        "cwdOnPath": os.getcwd() in sys.path or "" in sys.path,
    }
    return {"o": json.dumps(info)}
"""


def test_a_bundle_listing_one_path_twice_fails_closed(tmp_path):
    files = _files("t.sh", "echo\n")
    request = _request(files)
    request.bundle.files.append(BundleFile(path="tasks/t.sh", content="echo other\n"))
    result = run_bundle_request(request, {}, tmp_path)
    assert result.setup.status == "failed"
    assert "more than once" in result.setup.error
    assert result.tasks == []


def test_bundle_files_are_private_to_the_host_user(tmp_path):
    _run(_files("t.sh", "echo\n"), tmp_path)
    bundle_dir = tmp_path / "bundle"
    assert stat.S_IMODE(bundle_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((bundle_dir / "tasks" / "t.sh").stat().st_mode) == 0o600


def test_a_python_task_sees_a_private_home_tmpdir_and_no_host_or_wrapper_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EXECUTOR_TOKEN_FILE", "/var/run/executor/token")
    monkeypatch.setenv("RELAY_URL", "http://relay")
    monkeypatch.setenv("POOL_ID", "pool-1")
    monkeypatch.setenv("RW_HOST_SETTING", "x")
    result = _run(
        _files("t.py", _PY_REPORT),
        tmp_path,
        credentials={"token": "tok-value-123456", "other": "not-for-this-task"},
    )
    [task] = result.tasks
    assert task.status == "ok", task
    info = json.loads(task.outputs["o"])
    assert info["hostOnly"] == []
    assert info["env"] == ["RW_CAPABILITY", "RW_OPERATION", "RW_OUTPUT_FD", "RW_WORKDIR"]
    assert info["home"] == str(tmp_path / ".rw-home")
    assert info["tmpdir"] == str(tmp_path / ".rw-tmp")
    assert info["stdin"] == ""
    assert info["tokenMode"] == "0o600"
    assert info["cwdOnPath"] is False
    # the secret file and its directory are gone once the task has finished
    assert not os.path.exists(info["tokenPath"])
    assert not os.path.exists(os.path.dirname(info["tokenPath"]))


def test_a_python_task_only_gets_credentials_its_inputs_declare(tmp_path):
    files = _files(
        "t.py",
        "def main(ctx):\n    return {'o': ','.join(sorted(ctx._credentials))}\n",
    )
    files["capability.yaml"] = files["capability.yaml"].replace(
        "  other: { type: secret, optional: true }\n", ""
    )
    result = _run(files, tmp_path, credentials={"token": "abcdefgh1", "unrelated": "zzzzzzzz9"})
    assert result.tasks[0].outputs == {"o": "token"}


def test_a_bash_task_reads_eof_on_stdin_and_sees_no_host_only_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_URL", "http://relay")
    source = (
        'source "$RW_SDK/rw.sh"\n'
        'line=""\n'
        "read -r line || true\n"
        "relay=$(printenv RELAY_URL || true)\n"
        'rw_set o "\\"stdin=[${line}] relay=[${relay}] home=[${HOME}]\\""\n'
    )
    files = _files("t.sh", source)
    result = run_bundle_request(_request(files), {}, tmp_path)
    [task] = result.tasks
    assert task.outputs == {"o": f"stdin=[] relay=[] home=[{tmp_path / '.rw-home'}]"}


def test_a_reused_workdir_replaces_a_stale_bundle_and_never_follows_a_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    scope = tmp_path / "scope"
    scope.mkdir()
    (scope / "bundle").symlink_to(outside, target_is_directory=True)
    result = _run(_files("t.sh", "echo\n"), scope)
    assert result.tasks[0].status == "ok"
    assert list(outside.iterdir()) == []
    assert not (scope / "bundle").is_symlink()


def test_an_unrelated_directory_named_bundle_is_never_deleted(tmp_path):
    (tmp_path / "bundle").mkdir()
    (tmp_path / "bundle" / "keep.txt").write_text("user data")
    result = _run(_files("t.sh", "echo\n"), tmp_path)
    assert result.setup.status == "failed"
    assert "not a bundle directory" in result.setup.error
    assert (tmp_path / "bundle" / "keep.txt").read_text() == "user data"


@pytest.mark.parametrize("bad_path", ["tasks/../../escape.sh", "/abs.sh", "tasks/a\x00.sh"])
def test_a_path_outside_the_bundle_is_never_written(tmp_path, bad_path):
    files = {**_files("t.sh", "echo\n"), bad_path: "echo pwned\n"}
    result = _run(files, tmp_path / "scope")
    assert result.setup.status == "failed"
    assert not (tmp_path / "escape.sh").exists()


def _wait_until_gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_a_background_child_does_not_outlive_its_task(tmp_path):
    pid_file = tmp_path / "bg.pid"
    source = f'sleep 60 &\necho $! > "{pid_file}"\nexit 0\n'
    started = time.monotonic()
    result = _run(_files("t.sh", source), tmp_path / "scope")
    assert result.tasks[0].status == "ok"
    # the run no longer waits out the background child's hold on the pipes
    assert time.monotonic() - started < 4
    assert _wait_until_gone(int(pid_file.read_text()))


def test_a_python_tasks_subprocess_does_not_outlive_it(tmp_path):
    pid_file = tmp_path / "bg.pid"
    source = (
        "import subprocess\n"
        "def main(ctx):\n"
        "    proc = subprocess.Popen(['sleep', '60'])\n"
        f"    open({str(pid_file)!r}, 'w').write(str(proc.pid))\n"
        "    return {}\n"
    )
    result = _run(_files("t.py", source), tmp_path / "scope")
    assert result.tasks[0].status == "ok"
    assert _wait_until_gone(int(pid_file.read_text()))


# -- the private output channel is untrusted input ------------------------------

_BASH = 'source "$RW_SDK/rw.sh"\n'


@pytest.mark.parametrize(
    "line",
    [
        "[1, 2]",
        '"just a string"',
        '{"op": "set", "name": ["o"], "value": 1}',
        '{"op": "skip", "reason": {"a": 1}}',
        '{"op": "set", "name": "o", "value": NaN}',
        '{"op": "set", "name": "o", "value": 1, "extra": 2}',
        '{"op": "explode"}',
        '{"op": "set", "name": "o", "value": ' + "[" * 100 + "]" * 100 + "}",
    ],
)
def test_a_malformed_event_line_is_ignored_not_fatal(tmp_path, line):
    source = _BASH + f'echo {json.dumps(line)} >&"$RW_OUTPUT_FD"\nrw_set o \'"kept"\'\n'
    result = _run(_files("t.sh", source), tmp_path)
    [task] = result.tasks
    assert task.status == "ok"
    assert task.outputs == {"o": "kept"}
    assert any("were ignored" in e for e in task.errors)


@pytest.mark.parametrize(
    "value",
    [
        '"x","op":"skip","reason":"spoofed"',  # rewrites the event via a duplicate key
        '"x"}\n{"op":"skip","reason":"spoofed"',  # a second event smuggled on a new line
    ],
    ids=["duplicate-key", "newline"],
)
def test_a_value_cannot_inject_a_second_event(tmp_path, value):
    source = _BASH + f'v={json.dumps(value)}\nrw_set o "$v"\n'
    source = source.replace("\\n", "\n")  # a real newline inside the value
    result = _run(_files("t.sh", source), tmp_path)
    [task] = result.tasks
    assert task.status == "ok"
    assert task.reason is None
    assert task.outputs == {}


def test_an_endless_output_stream_is_capped_and_fails_the_task(tmp_path):
    source = (
        "import os\n"
        "def main(ctx):\n"
        "    fd = int(os.environ['RW_OUTPUT_FD'])\n"
        "    chunk = b'x' * (1 << 20)\n"
        "    for _ in range(12):\n"  # one 12 MiB line with no newline
        "        os.write(fd, chunk)\n"
        "    return {'o': 'done'}\n"
    )
    started = time.monotonic()
    result = _run(_files("t.py", source), tmp_path)
    [task] = result.tasks
    assert time.monotonic() - started < 10
    assert task.status == "failed"
    assert any(e.startswith("E_OUTPUT_TOO_LARGE") for e in task.errors)


def test_many_small_events_are_parsed_in_linear_time(tmp_path):
    source = (
        "import json, os\n"
        "def main(ctx):\n"
        "    fd = int(os.environ['RW_OUTPUT_FD'])\n"
        "    line = json.dumps({'op': 'append', 'name': 'o', 'value': 1}) + '\\n'\n"
        "    os.write(fd, (line * 100000).encode())\n"
        "    return {}\n"
    )
    files = _files("t.py", source, schema="integer[]")
    started = time.monotonic()
    result = _run(files, tmp_path)
    assert time.monotonic() - started < 10
    [task] = result.tasks
    assert task.status == "ok"
    assert len(task.outputs["o"]) > 1000


# -- output schemas are evaluated locally and contained ---------------------------


def test_a_non_local_ref_is_never_fetched(tmp_path):
    target = tmp_path / "leak.json"
    target.write_text('{"type": "string", "enum": ["only-this"]}')
    messages = _validate_output_schema("o", "x", {"$ref": target.as_uri()})
    assert messages and "could not be evaluated" in messages[0]
    assert "only-this" not in messages[0]


def test_a_local_ref_still_resolves():
    schema = {"$defs": {"n": {"type": "integer"}}, "$ref": "#/$defs/n"}
    assert _validate_output_schema("o", 1, schema) == []
    assert _validate_output_schema("o", "x", schema)


def test_an_invalid_schema_fails_only_its_output():
    messages = _validate_output_schema("o", 1, {"type": 5})
    assert messages and messages[0].startswith("E_OUTPUT_SCHEMA outputs.o")


def test_schema_errors_are_capped_in_number_and_length():
    schema = {"type": "array", "items": {"type": "integer"}}
    messages = _validate_output_schema("o", ["x" * 5000] * 1000, schema)
    assert len(messages) == MAX_SCHEMA_ERRORS + 1
    assert all(len(m) <= MAX_SCHEMA_ERROR_CHARS for m in messages)
