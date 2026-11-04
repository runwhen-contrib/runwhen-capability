"""contracts-v5 `materialize.nonSecret`: the credential type's (integration schema) and the
auth plan's. Absent, a document means what it did under contracts-v4."""

from __future__ import annotations

import pytest

from runwhen_capability.contracts import validate

PLAN = {
    "connection": "prod-db",
    "integration": "postgres@1.0.0",
    "authMethod": "password",
    "credentialType": "postgres/connection",
    "reference": {"kind": "k8s-secret", "ref": "payments/pg-creds", "content": "postgres"},
    "method": {"native": "passthrough"},
}
ENV = ["PGHOST", "PGPASSWORD"]


def integration(materialize: dict) -> dict:
    return {
        "platform": "postgres",
        "credentialTypes": {"postgres/connection": {"materialize": materialize}},
    }


def plan(materialize: dict) -> dict:
    return {**PLAN, "materialize": materialize}


def paths(name: str, doc: dict) -> list[str]:
    return [v.path for v in validate(name, doc)]


@pytest.mark.parametrize(
    "materialize",
    [
        {"env": ENV},
        {"env": ENV, "nonSecret": ["PGHOST"]},
        {"env": ENV, "nonSecret": ENV},
        {"file": {"env": "KUBECONFIG"}},
    ],
)
def test_valid_materialize(materialize):
    assert validate("integration", integration(materialize)) == []
    if len(materialize) == 1 or "file" not in materialize:
        assert validate("auth-plan", plan(materialize)) == []


@pytest.mark.parametrize(
    "materialize",
    [
        {"file": {"env": "KUBECONFIG"}, "nonSecret": ["KUBECONFIG"]},
        {"nonSecret": ["PGHOST"]},
        {"env": {"fromTaskVars": True}, "nonSecret": ["REGION"]},
        {"file": {"env": "KUBECONFIG"}, "env": ENV, "nonSecret": ["PGHOST"]},
        {"env": ENV, "nonSecret": []},
        {"env": ENV, "nonSecret": ["PGHOST", "PGHOST"]},
        {"env": ENV, "nonSecret": ["not-a-name"]},
    ],
)
def test_invalid_integration_materialize(materialize):
    assert paths("integration", integration(materialize))


@pytest.mark.parametrize(
    "materialize",
    [
        {"file": {"env": "KUBECONFIG"}, "nonSecret": ["KUBECONFIG"]},
        {"nonSecret": ["PGHOST"]},
        {"file": {"env": "KUBECONFIG"}, "env": ENV},
        {"file": {"env": "KUBECONFIG"}, "env": ENV, "nonSecret": ["PGHOST"]},
        {"env": ENV, "nonSecret": []},
        {"env": ENV, "nonSecret": ["PGHOST", "PGHOST"]},
        {"env": ENV, "nonSecret": ["PGHOST"], "other": 1},
    ],
)
def test_invalid_plan_materialize(materialize):
    assert paths("auth-plan", plan(materialize))
