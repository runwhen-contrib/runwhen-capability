"""Fixtures for tests/test_auth_script.py: a copy of the run-mini plugin with one task that
reports what it can see of the credentials, and the archive of a fake integration plugin
whose `integration/auth/` holds the auth scripts a lease's script auth plans run.

    tree = build_task_plugin(tmp_path / "plugin")        # task "auth-report"
    data = integration_archive()                         # bytes of the integration's archive
"""

from __future__ import annotations

import shutil
from pathlib import Path

from runwhen_capability.contracts import TreeEntry, archive

HERE = Path(__file__).resolve().parent
RUN_MINI = HERE / "plugin" / "fixtures" / "run-mini"

#: What the reference's `key.json` holds; the task looks for it through the credential.
KEY_MARKER = "KEYMARKER-0123456789abcdef"
KEY_JSON = (
    f'{{"type": "service_account", "project_id": "acme-prod", "private_key_id": "{KEY_MARKER}"}}'
)
#: A second reference file; auth scripts that fail print it, so a test sees it scrubbed.
TOKEN = "ref-token-value-7788aabbcc"

REPORT_PY = """\
import os
import pathlib
import stat


def _mode(path):
    return oct(stat.S_IMODE(os.stat(path).st_mode))


def has_marker(path):
    return bool(path) and os.path.isfile(path) and "__KEY_MARKER__" in open(path).read()


def main(ctx, marker="", names=""):
    if marker:
        pathlib.Path(marker).write_text("ran")
    report = {"authVars": sorted(k for k in os.environ if k.startswith("RW_AUTH_")), "env": {}}
    for name in [n for n in names.split(",") if n]:
        report["env"][name] = os.environ.get(name)
    kubeconfig = os.environ.get("KUBECONFIG")
    if kubeconfig and os.path.isfile(kubeconfig):
        text = pathlib.Path(kubeconfig).read_text()
        key = next(
            (line[len("keyfile: "):] for line in text.splitlines() if line.startswith("keyfile: ")),
            "",
        )
        report["kubeconfig"] = {
            "mode": _mode(kubeconfig),
            "mentionsProject": "project: acme-prod" in text,
            "content": text,
            "keyReadable": bool(key) and os.path.isfile(key),
            "keyHasMarker": has_marker(key),
            "keyMode": _mode(key) if key and os.path.isfile(key) else None,
            "keyDirMode": _mode(os.path.dirname(key)) if key and os.path.isfile(key) else None,
        }
    return {"report": report}
""".replace("__KEY_MARKER__", KEY_MARKER)

#: The auth scripts, by path under integration/ (`method.script.path` is relative to that).
SCRIPTS = {
    # the GKE-shaped one: reads the reference key and an input, writes a kubeconfig
    "auth/fake.sh": """\
key="$RW_AUTH_REF_DIR/key.json"
test -r "$key"
cat > "$RW_AUTH_OUT_FILE" <<EOF
apiVersion: v1
kind: Config
project: $RW_AUTH_INPUT_PROJECT
location: $RW_AUTH_INPUT_LOCATION
keyfile: $key
EOF
echo "chatty stdout: $(cat "$key")"
echo "chatty stderr: $(cat "$RW_AUTH_REF_DIR/token")" >&2
""",
    # an env-type credential, in Python
    "auth/envpy.py": """\
import os
import pathlib

password = pathlib.Path(os.environ["RW_AUTH_REF_DIR"], "value").read_text().strip()
with open(os.environ["RW_AUTH_OUT_ENV"], "w") as out:
    out.write("PGUSER=" + os.environ["RW_AUTH_INPUT_DB_USER"] + "\\n")
    out.write("PGPASSWORD=" + password + "\\n")
print("on stdout:", password)
""",
    # a postgres-shaped env credential: two non-secret values and the password
    "auth/pgenv.py": """\
import os
import pathlib

password = pathlib.Path(os.environ["RW_AUTH_REF_DIR"], "value").read_text().strip()
with open(os.environ["RW_AUTH_OUT_ENV"], "w") as out:
    out.write("PGHOST=db.prod.internal\\n")
    out.write("PGDATABASE=" + os.environ["RW_AUTH_INPUT_DATABASE"] + "\\n")
    out.write("PGPASSWORD=" + password + "\\n")
""",
    # fails with the {code, message} the input names, and a secret in the message
    "auth/fail.sh": """\
token="$(cat "$RW_AUTH_REF_DIR/token")"
echo "{\\"code\\": \\"$RW_AUTH_INPUT_CODE\\", \\"message\\": \\"denied: $token\\"}" \\
  > "$RW_AUTH_OUT_ERROR"
exit 3
""",
    # fails without saying why
    "auth/crash.sh": """\
echo "going down with $(cat "$RW_AUTH_REF_DIR/token")" >&2
exit 2
""",
    "auth/nothing.sh": "true\n",
    # writes exactly the NAME=value lines the input holds
    "auth/lines.py": """\
import os

with open(os.environ["RW_AUTH_OUT_ENV"], "w") as out:
    out.write(os.environ["RW_AUTH_INPUT_LINES"])
""",
    "auth/slow.sh": "sleep 30\n",
    "auth/symlink.sh": 'ln -s /etc/hosts "$RW_AUTH_OUT_FILE"\n',
    # reports what it sees to the file the input names, then writes a credential
    "auth/probe.sh": """\
{
  echo "== env"; env | sort
  echo "== pwd"; pwd -P
  echo "== integration"; ls integration/auth
  echo "== ref"; ls -A "$RW_AUTH_REF_DIR"
} > "$RW_AUTH_INPUT_REPORT"
echo "project: acme-prod" > "$RW_AUTH_OUT_FILE"
""",
}


def integration_files(extra: dict[str, str] | None = None) -> list[TreeEntry]:
    files = {
        "plugin.yaml": (
            "apiVersion: runwhen.com/plugin/v1\nname: authint\nversion: 0.1.0\n"
            "description: Auth scripts for tests.\nlicense: Apache-2.0\n"
        ),
        **{f"integration/{path}": text for path, text in SCRIPTS.items()},
        **{f"integration/{path}": text for path, text in (extra or {}).items()},
    }
    return [TreeEntry(path, text.encode(), False) for path, text in sorted(files.items())]


def integration_archive(extra: dict[str, str] | None = None) -> bytes:
    return archive.build(integration_files(extra))


def build_task_plugin(dest: Path) -> Path:
    shutil.copytree(RUN_MINI, dest)
    (dest / "skills" / "probe" / "scripts" / "auth_report.py").write_text(REPORT_PY)
    with (dest / "skills" / "probe" / "tasks.yaml").open("a") as f:
        f.write(
            """\
  - name: auth-report
    title: Python, what the credentials look like
    description: Reports the credential files and variables the task can see.
    script: scripts/auth_report.py
    readOnly: true
    inputs:
      marker: { type: string, default: "", description: A file to create when the task runs }
      names: { type: string, default: "", description: Variables to report }
    outputs:
      report: { schema: ./schemas/object.v1.json }
"""
        )
    return dest
