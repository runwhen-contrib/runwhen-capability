"""A copy of the run-mini fixture plugin with one task on the platform-minted
`runwhen/sync` credential. That credential is allowed only on the integration's discovery
entry-point task, and a plugin has one of those, so each of the two variants (Python and
bash, both read the JSON at $RW_SYNC_CREDENTIAL) gets its own copy:

    plugin_dir = build_sync_plugin(tmp_path / "plugin", "py-sync")   # or "sh-sync"
"""

from __future__ import annotations

import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN_MINI = HERE / "plugin" / "fixtures" / "run-mini"
SCRIPTS = HERE / "fixtures" / "sync"

SYNC_TOKEN = "sync-token-7f3a9c1e5b"
SYNC_DOC = {
    "apiBaseUrl": "https://papi.example.invalid/api/v1",
    "token": SYNC_TOKEN,
    "workspace": "ws-1",
    "expiresAt": "2030-01-01T00:00:00Z",
    "scopes": ["kubernetes/clusters/kind"],
}

_TASKS = {
    "py-sync": ("py_sync.py", "Python"),
    "sh-sync": ("sh_sync.sh", "Bash"),
}


def build_sync_plugin(dest: Path, task: str) -> Path:
    script, label = _TASKS[task]
    shutil.copytree(RUN_MINI, dest)
    scripts = dest / "skills" / "probe" / "scripts"
    for name in {script, "py_sync.py"}:
        shutil.copy(SCRIPTS / name, scripts / name)
        (scripts / name).chmod(0o755)
    with (dest / "skills" / "probe" / "tasks.yaml").open("a") as f:
        f.write(
            f"""\
  - name: {task}
    title: {label}, the sync credential
    description: Reads the JSON at $RW_SYNC_CREDENTIAL (platform-minted runwhen/sync).
    script: scripts/{script}
    readOnly: true
    credentials: runwhen/sync
    outputs:
      sync: {{ schema: ./schemas/object.v1.json }}
  - name: py-sync-undeclared
    title: Python, the sync script without the credential
    description: The same script as py-sync, on a task that declares only a kubeconfig.
    script: scripts/py_sync.py
    readOnly: true
    credentials: kubernetes/kubeconfig
    outputs:
      sync: {{ schema: ./schemas/object.v1.json }}
"""
        )
    with (dest / "integration" / "integration.yaml").open("a") as f:
        f.write(f"entryPoints:\n  discovery:\n    task: probe/{task}\n")
    return dest
