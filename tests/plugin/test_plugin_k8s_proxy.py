"""`rwtask test` records and replays `k8s-proxy-get` calls (ctx.k8s_proxy_get and bash's
rw_k8s_proxy_get) and `k8s-proxy-request` calls (ctx.k8s_proxy_request, rw_k8s_proxy_request)
between ordinary CLI calls. The plugin is a tmp copy of run-mini with four extra tasks; a fake
kubectl (tests/k8s_proxy_fake.py) serves both `get ...` and `proxy`."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml
from k8s_proxy_fake import install_fake_kubectl

from runwhen_capability.cli import main
from runwhen_capability.contracts import load_yaml

HERE = Path(__file__).resolve().parent
RUN_MINI = HERE / "fixtures" / "run-mini"
DEPLOYMENT = "kubernetes/clusters/kind/namespaces/runwhen/deployments/runwhen-papi"
HEALTH = "/api/v1/namespaces/runwhen/services/vault:8200/proxy/v1/sys/health"
BODY = '{"sealed":false}'
RING = "/api/v1/namespaces/runwhen/services/loki:3100/proxy/ring"
RING_BODY = '{"shards":[]}'
needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="the bash task reads with jq")

TASKS = """\
  - name: proxy-mixed-sh
    title: Proxied GET between two kubectl calls (bash)
    description: kubectl, rw_k8s_proxy_get, kubectl.
    script: scripts/proxy_mixed.sh
    readOnly: true
    outputs:
      probe: { schema: "{ status: integer, body: string }" }
  - name: proxy-mixed-py
    title: Proxied GET between two kubectl calls (Python)
    description: ctx.run, ctx.k8s_proxy_get, ctx.run.
    script: scripts/proxy_mixed.py
    readOnly: true
    outputs:
      probe: { schema: "{ status: integer, body: string }" }
"""

WRITE_TASKS = """\
  - name: proxy-write-sh
    title: Proxied GET, PUT, DELETE (bash)
    description: rw_k8s_proxy_get with a header, then rw_k8s_proxy_request twice.
    script: scripts/proxy_write.sh
    readOnly: false
    effects: ["Removes a ring member"]
    destructive: true
    outputs:
      probe: { schema: "{ get: integer, put: integer, delete: integer, body: string }" }
  - name: proxy-write-py
    title: Proxied GET, PUT, DELETE (Python)
    description: ctx.k8s_proxy_get with a header, then ctx.k8s_proxy_request twice.
    script: scripts/proxy_write.py
    readOnly: false
    effects: ["Removes a ring member"]
    destructive: true
    outputs:
      probe: { schema: "{ get: integer, put: integer, delete: integer, body: string }" }
"""

WRITE_SH = """\
#!/usr/bin/env bash
ns="$RW_RESOURCE_NAMESPACE"
got=$(rw_k8s_proxy_get "$ns" services loki 3100 /ring --header 'Accept: application/json')
put=$(rw_k8s_proxy_request PUT "$ns" services loki 3100 /ring/a \\
    --data '{"state":"LEAVING"}' --header 'Content-Type: application/json')
del=$(rw_k8s_proxy_request DELETE "$ns" services loki 3100 /ring/a)
rw_output_set probe "$(jq -c -n --argjson g "$got" --argjson p "$put" --argjson d "$del" \\
    '{get: $g.status, put: $p.status, delete: $d.status, body: $g.body}')"
"""

WRITE_PY = """\
def main(ctx):
    ns = ctx.resource.namespace
    got = ctx.k8s_proxy_get(
        ns, "services", "loki", 3100, "/ring", headers={"Accept": "application/json"}
    )
    put = ctx.k8s_proxy_request(
        "PUT", ns, "services", "loki", 3100, "/ring/a",
        body='{"state":"LEAVING"}', headers={"Content-Type": "application/json"},
    )
    gone = ctx.k8s_proxy_request("DELETE", ns, "services", "loki", 3100, "/ring/a")
    return {"probe": {"get": got["status"], "put": put["status"], "delete": gone["status"],
                      "body": got["body"]}}
"""

SH = """\
#!/usr/bin/env bash
ns="$RW_RESOURCE_NAMESPACE"
kubectl -n "$ns" get pods -o json > "$RW_OUT/pods.json"
resp=$(rw_k8s_proxy_get "$ns" services vault 8200 /v1/sys/health)
kubectl -n "$ns" get events -o json > "$RW_OUT/events.json"
rw_output_set probe "$(jq -c '{status: .status, body: .body}' <<<"$resp")"
"""

PY = """\
def main(ctx):
    ns = ctx.resource.namespace
    ctx.run(["kubectl", "-n", ns, "get", "pods", "-o", "json"])
    resp = ctx.k8s_proxy_get(ns, "services", "vault", 8200, "/v1/sys/health")
    ctx.run(["kubectl", "-n", ns, "get", "events", "-o", "json"])
    return {"probe": {"status": resp["status"], "body": resp["body"]}}
"""


@pytest.fixture
def plugin(tmp_path):
    dest = tmp_path / "plugin"
    shutil.copytree(RUN_MINI, dest)
    skill = dest / "skills" / "probe"
    with open(skill / "tasks.yaml", "a") as handle:
        handle.write(TASKS)
        handle.write(WRITE_TASKS)
    (skill / "scripts" / "proxy_write.sh").write_text(WRITE_SH)
    (skill / "scripts" / "proxy_write.py").write_text(WRITE_PY)
    (skill / "scripts" / "proxy_mixed.sh").write_text(SH)
    (skill / "scripts" / "proxy_mixed.py").write_text(PY)
    return dest


@pytest.fixture
def kubectl(tmp_path, monkeypatch):
    return install_fake_kubectl(
        tmp_path,
        monkeypatch,
        routes={
            HEALTH: {"headers": {"X-A": "1"}, "body": BODY},
            RING: {"body": RING_BODY},
            "PUT " + RING + "/a": {"status": 202},
            "DELETE " + RING + "/a": {"status": 204},
        },
        other_stdout='{"items": []}\n',
    )


@pytest.fixture
def rwtask(plugin, capsys):
    def go(*args, expect=None):
        code = main(["test", *args, "--plugin-dir", str(plugin)])
        captured = capsys.readouterr()
        if expect is not None:
            assert code == expect, captured.out + captured.err
        return code, captured.out, captured.err

    return go


def scenario_path(plugin, task, name="basic"):
    return plugin / "skills" / "probe" / "tests" / task / f"{name}.yaml"


def record(rwtask, task, name="basic"):
    # a task that writes is only recorded on request: recording really runs it
    allow = ["--allow-write"] if task.startswith("proxy-write") else []
    return rwtask(
        f"probe/{task}", "--record", "--resource", DEPLOYMENT, "--scenario", name, *allow, expect=0
    )


def without_kubectl(monkeypatch):
    """PATH with no kubectl on it. A directory that holds one is replaced by a mirror of its
    other entries, not dropped: on GitHub's runners kubectl shares /usr/bin with bash and jq."""
    kept = []
    for d in os.environ["PATH"].split(os.pathsep):
        if not d or not os.path.exists(os.path.join(d, "kubectl")):
            kept.append(d)
            continue
        mirror = Path(tempfile.mkdtemp(prefix="rw-no-kubectl-"))
        for entry in os.scandir(d):
            if entry.name != "kubectl":
                (mirror / entry.name).symlink_to(entry.path)
        kept.append(str(mirror))
    monkeypatch.setenv("PATH", os.pathsep.join(kept))
    assert shutil.which("kubectl") is None
    assert shutil.which("bash") is not None


def expect_outputs(path):
    path.write_text(path.read_text() + f"  outputs:\n    probe: {{status: 200, body: '{BODY}'}}\n")


TASK_NAMES = [
    pytest.param("proxy-mixed-sh", marks=needs_jq),
    "proxy-mixed-py",
]


@pytest.mark.parametrize("task", TASK_NAMES)
def test_record_then_replay_with_no_kubectl_on_path(rwtask, plugin, kubectl, monkeypatch, task):
    _, _, err = record(rwtask, task)
    assert "recorded 3 calls" in err
    # the real calls: two `get`s and ONE `proxy`, which is not a recorded call
    assert [c[0] for c in kubectl.commands()] == ["-n", "proxy", "-n"]
    assert len(kubectl.requests()) == 1
    path = scenario_path(plugin, task)
    doc = load_yaml(path.read_text())
    first, middle, last = doc["calls"]
    assert first["argv"][:4] == ["kubectl", "-n", "runwhen", "get"]
    assert last["argv"][:4] == ["kubectl", "-n", "runwhen", "get"]
    assert middle["type"] == "k8s-proxy-get"
    assert middle["request"] == {
        "namespace": "runwhen",
        "kind": "services",
        "name": "vault",
        "port": 8200 if task.endswith("py") else "8200",
        "path": "/v1/sys/health",
        "scheme": "http",
    }
    assert middle["response"]["status"] == 200
    assert middle["response"]["body"] == BODY
    assert middle["response"]["headers"]["x-a"] == "1"
    assert middle["response"]["truncated"] is False and middle["response"]["location"] is None
    assert doc["expect"] == {"status": "ok", "errorCodes": []}
    expect_outputs(path)

    before = len(kubectl.commands())
    without_kubectl(monkeypatch)
    _, out, _ = rwtask(f"probe/{task}", expect=0)
    assert f"PASS probe/{task}/basic" in out
    assert len(kubectl.commands()) == before  # replay ran nothing: no proxy, no kubectl


@pytest.fixture
def recorded(rwtask, plugin, kubectl, monkeypatch):
    def go(task):
        record(rwtask, task)
        expect_outputs(scenario_path(plugin, task))
        without_kubectl(monkeypatch)
        return scenario_path(plugin, task)

    return go


@pytest.mark.parametrize("task", TASK_NAMES)
def test_a_reordered_proxy_call_is_a_mismatch(rwtask, recorded, task):
    path = recorded(task)
    doc = load_yaml(path.read_text())
    doc["calls"][0], doc["calls"][1] = doc["calls"][1], doc["calls"][0]
    path.write_text(yaml.safe_dump(doc))
    code, out, _ = rwtask(f"probe/{task}")
    assert code == 1
    assert f"FAIL probe/{task}/basic" in out
    assert (
        "E_FIXTURE_MISMATCH call 1: expected k8s-proxy-get http runwhen/services/vault:8200 "
        "/v1/sys/health got kubectl -n runwhen get pods -o json"
    ) in out


@pytest.mark.parametrize("task", TASK_NAMES)
def test_a_proxy_call_moved_later_is_a_mismatch_at_the_cli_call(rwtask, recorded, task):
    path = recorded(task)
    doc = load_yaml(path.read_text())
    doc["calls"][1], doc["calls"][2] = doc["calls"][2], doc["calls"][1]
    path.write_text(yaml.safe_dump(doc))
    code, out, _ = rwtask(f"probe/{task}")
    assert code == 1
    assert "E_FIXTURE_MISMATCH call 2: expected kubectl -n runwhen get events -o json" in out


@pytest.mark.parametrize("task", TASK_NAMES)
def test_a_different_proxy_request_is_a_mismatch(rwtask, recorded, task):
    path = recorded(task)
    doc = load_yaml(path.read_text())
    doc["calls"][1]["request"]["path"] = "/v1/sys/seal-status"
    path.write_text(yaml.safe_dump(doc))
    code, out, _ = rwtask(f"probe/{task}")
    assert code == 1
    assert (
        "E_FIXTURE_MISMATCH call 2: expected k8s-proxy-get http runwhen/services/vault:8200 "
        "/v1/sys/seal-status got k8s-proxy-get http runwhen/services/vault:8200 /v1/sys/health"
    ) in out


@pytest.mark.parametrize("task", TASK_NAMES)
def test_recorded_calls_the_task_never_makes_fail(rwtask, recorded, task):
    path = recorded(task)
    doc = load_yaml(path.read_text())
    doc["calls"].append(doc["calls"][1])
    path.write_text(yaml.safe_dump(doc))
    code, out, _ = rwtask(f"probe/{task}")
    assert code == 1
    assert "1 recorded calls not made" in out


def test_a_failed_proxy_call_is_recorded_and_replays_as_the_same_error(
    rwtask, plugin, kubectl, monkeypatch
):
    status = {
        "kind": "Status",
        "apiVersion": "v1",
        "status": "Failure",
        "message": 'services "vault" is forbidden: cannot get proxy',
        "code": 403,
    }
    import json

    kubectl.configure(api_status={"status": 403, "body": json.dumps(status)})
    _, _, err = record(rwtask, "proxy-mixed-py")
    assert "recorded 2 calls" in err and "permission_denied" in err
    doc = load_yaml(scenario_path(plugin, "proxy-mixed-py").read_text())
    assert doc["expect"] == {"status": "failed", "errorCodes": ["permission_denied"]}
    assert doc["calls"][1]["type"] == "k8s-proxy-get"
    assert doc["calls"][1]["error"]["code"] == "permission_denied"
    assert "response" not in doc["calls"][1]
    without_kubectl(monkeypatch)
    _, out, _ = rwtask("probe/proxy-mixed-py", expect=0)
    assert "PASS probe/proxy-mixed-py/basic" in out


@needs_jq
def test_a_failed_proxy_call_in_a_bash_task_is_recorded_and_replays(
    rwtask, plugin, kubectl, monkeypatch
):
    import json

    status = {
        "kind": "Status",
        "apiVersion": "v1",
        "status": "Failure",
        "message": "denied",
        "code": 403,
    }
    kubectl.configure(api_status={"status": 403, "body": json.dumps(status)})
    record(rwtask, "proxy-mixed-sh")
    doc = load_yaml(scenario_path(plugin, "proxy-mixed-sh").read_text())
    assert doc["expect"]["status"] == "failed"
    assert doc["expect"]["errorCodes"] == ["permission_denied"]
    without_kubectl(monkeypatch)
    _, out, _ = rwtask("probe/proxy-mixed-sh", expect=0)
    assert "PASS probe/proxy-mixed-sh/basic" in out


# -- writes: k8s-proxy-request -----------------------------------------------------------------

WRITE_TASK_NAMES = [
    pytest.param("proxy-write-sh", marks=needs_jq),
    "proxy-write-py",
]


def expect_write_outputs(path):
    path.write_text(
        path.read_text()
        + f"  outputs:\n    probe: {{get: 200, put: 202, delete: 204, body: '{RING_BODY}'}}\n"
    )


@pytest.fixture
def recorded_writes(rwtask, plugin, kubectl, monkeypatch):
    def go(task):
        record(rwtask, task)
        expect_write_outputs(scenario_path(plugin, task))
        without_kubectl(monkeypatch)
        return scenario_path(plugin, task)

    return go


@pytest.mark.parametrize("task", WRITE_TASK_NAMES)
def test_record_then_replay_get_put_delete_with_no_kubectl_on_path(
    rwtask, plugin, kubectl, monkeypatch, task
):
    _, _, err = record(rwtask, task)
    assert "recorded 3 calls" in err
    # three real proxied requests, each with its own method and body, one proxy each
    assert [r["method"] for r in kubectl.requests()] == ["GET", "PUT", "DELETE"]
    assert kubectl.bodies() == [b"", b'{"state":"LEAVING"}', b""]
    assert kubectl.requests()[0]["headers"]["Accept"] == "application/json"
    assert kubectl.requests()[1]["headers"]["Content-Type"] == "application/json"
    path = scenario_path(plugin, task)
    doc = load_yaml(path.read_text())
    got, put, gone = doc["calls"]
    port = 3100 if task.endswith("py") else "3100"
    base = {"namespace": "runwhen", "kind": "services", "name": "loki", "port": port}
    assert got["type"] == "k8s-proxy-get"  # lane F's existing fixtures keep their type
    assert got["request"] == {
        **base,
        "path": "/ring",
        "scheme": "http",
        "headers": {"accept": "application/json"},
    }
    assert put["type"] == "k8s-proxy-request"
    assert put["request"] == {
        "method": "PUT",
        **base,
        "path": "/ring/a",
        "scheme": "http",
        "headers": {"content-type": "application/json"},
        "body": '{"state":"LEAVING"}',
    }
    assert gone["type"] == "k8s-proxy-request"
    assert gone["request"] == {"method": "DELETE", **base, "path": "/ring/a", "scheme": "http"}
    assert [c["response"]["status"] for c in doc["calls"]] == [200, 202, 204]
    expect_write_outputs(path)

    before = len(kubectl.commands())
    without_kubectl(monkeypatch)
    _, out, _ = rwtask(f"probe/{task}", expect=0)
    assert f"PASS probe/{task}/basic" in out
    assert len(kubectl.commands()) == before  # replay started no proxy


def mutate(path, change):
    doc = load_yaml(path.read_text())
    change(doc["calls"])
    path.write_text(yaml.safe_dump(doc))


@pytest.mark.parametrize("task", WRITE_TASK_NAMES)
@pytest.mark.parametrize(
    ("change", "needle"),
    [
        (
            lambda calls: calls[1]["request"].update(body='{"state":"ACTIVE"}'),
            'body="{\\"state\\":\\"ACTIVE\\"}" got k8s-proxy-request PUT',
        ),
        (
            lambda calls: calls[1]["request"].update(method="POST"),
            "expected k8s-proxy-request POST http runwhen/services/loki:3100 /ring/a",
        ),
        (
            lambda calls: calls[1]["request"].update(headers={"content-type": "text/plain"}),
            'headers={"content-type": "text/plain"}',
        ),
        (
            lambda calls: calls[1]["request"].pop("body"),
            "got k8s-proxy-request PUT http runwhen/services/loki:3100 /ring/a headers=",
        ),
        (
            lambda calls: calls[0]["request"].update(headers={"accept": "text/html"}),
            'expected k8s-proxy-get http runwhen/services/loki:3100 /ring headers={"accept": '
            '"text/html"} got k8s-proxy-get http runwhen/services/loki:3100 /ring headers=',
        ),
        (
            lambda calls: calls[2]["request"].update(method="PATCH"),
            "expected k8s-proxy-request PATCH http runwhen/services/loki:3100 /ring/a "
            "got k8s-proxy-request DELETE http",
        ),
    ],
)
def test_a_changed_body_method_or_header_is_a_mismatch(
    rwtask, recorded_writes, task, change, needle
):
    path = recorded_writes(task)
    mutate(path, change)
    code, out, _ = rwtask(f"probe/{task}")
    assert code == 1
    assert f"FAIL probe/{task}/basic" in out
    assert "E_FIXTURE_MISMATCH" in out
    assert needle in out


@pytest.mark.parametrize("task", WRITE_TASK_NAMES)
def test_a_recorded_get_replays_as_either_type(rwtask, recorded_writes, task):
    path = recorded_writes(task)

    def retype(calls):
        calls[0]["type"] = "k8s-proxy-request"
        calls[0]["request"] = {"method": "GET", **calls[0]["request"]}
        calls[1]["request"]["headers"] = {"Content-Type": "application/json"}  # any case

    mutate(path, retype)
    _, out, _ = rwtask(f"probe/{task}", expect=0)
    assert f"PASS probe/{task}/basic" in out


OLD_SCENARIO = """\
resource: DEPLOYMENT
calls:
  - argv: [kubectl, -n, runwhen, get, pods, -o, json]
    stdout: '{"items": []}'
  - type: k8s-proxy-get
    request: {namespace: runwhen, kind: services, name: vault, port: 8200, path: /v1/sys/health}
    response: {status: 200, headers: {x - a: '1'}, body: '{"sealed":false}'}
  - argv: [kubectl, -n, runwhen, get, events, -o, json]
    stdout: '{"items": []}'
expect:
  status: ok
  outputs:
    probe: {status: 200, body: '{"sealed":false}'}
"""


@pytest.mark.parametrize("task", TASK_NAMES)
def test_a_fixture_written_before_methods_existed_still_replays(rwtask, plugin, monkeypatch, task):
    path = scenario_path(plugin, task)
    path.parent.mkdir(parents=True)
    path.write_text(OLD_SCENARIO.replace("DEPLOYMENT", DEPLOYMENT))
    without_kubectl(monkeypatch)
    _, out, _ = rwtask(f"probe/{task}", expect=0)
    assert f"PASS probe/{task}/basic" in out
