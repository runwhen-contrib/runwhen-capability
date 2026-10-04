"""An input's env var name (sdlc trial, 2026-10-04, H48): `THRESHOLD` mapped to
`T_H_R_E_S_H_O_L_D`, so a task reading `$THRESHOLD` failed E_UNDECLARED_INPUT and the
runtime would have delivered the value under the mangled name."""

from __future__ import annotations

import pytest

from runwhen_capability.custom import validate
from runwhen_capability.custom.manifest import input_env_name


@pytest.mark.parametrize(
    ("name", "env"),
    [
        # unchanged behaviour for the conventions already in use
        ("since", "SINCE"),
        ("maxWait", "MAX_WAIT"),
        ("maxTimeouts", "MAX_TIMEOUTS"),
        ("dry_run", "DRY_RUN"),
        ("tail_lines", "TAIL_LINES"),
        ("fillThresholdPct", "FILL_THRESHOLD_PCT"),
        # all-caps and acronyms no longer split per letter
        ("THRESHOLD", "THRESHOLD"),
        ("DRY_RUN", "DRY_RUN"),
        ("MAX_AUTH_FAILURES", "MAX_AUTH_FAILURES"),
        ("HTTPTimeout", "HTTP_TIMEOUT"),
        ("apiURL", "API_URL"),
        ("k8sNamespace", "K8S_NAMESPACE"),
    ],
)
def test_input_env_name(name, env):
    assert input_env_name(name) == env


def test_an_all_caps_input_is_read_under_its_own_name():
    files = {
        "capability.yaml": (
            "apiVersion: runwhen.com/custom-capability/v1\n"
            "name: vac\n"
            "appliesTo:\n"
            "  - {platform: kubernetes, type: statefulset}\n"
            "inputs:\n"
            "  target: { type: resource }\n"
            "tasks:\n"
            "  - name: run\n"
            "    file: tasks/run.sh\n"
            "    readOnly: true\n"
            "    inputs:\n"
            "      THRESHOLD: { type: number, default: 0.3, runtime: true }\n"
            "      DRY_RUN: { type: boolean, default: true, runtime: true }\n"
            "    outputs:\n"
            '      summary: { schema: "{ dryRun: boolean }" }\n'
        ),
        "tasks/run.sh": (
            'source "$RW_SDK/rw.sh"\n'
            'echo "threshold=$THRESHOLD dry=${DRY_RUN}"\n'
            "rw_set summary '{\"dryRun\": true}'\n"
        ),
    }
    codes = [d.code for d in validate(files) if d.severity == "error"]
    assert "E_UNDECLARED_INPUT" not in codes
