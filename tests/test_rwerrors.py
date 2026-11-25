"""RwError / RwCommandError and classify_command_error: the closed D64 error
codes, and the stderr patterns that map a failed CLI to one of them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runwhen_capability.rwerrors import (
    ERROR_CODES,
    RwCommandError,
    RwError,
    classify_command_error,
)

_ERROR_SCHEMA = Path(__file__).parent.parent / "contracts" / "schemas" / "error.schema.json"


def test_error_codes_are_exactly_the_d64_enum():
    schema = json.loads(_ERROR_SCHEMA.read_text())
    assert list(ERROR_CODES) == schema["properties"]["code"]["enum"]


def test_rw_error_carries_its_fields():
    err = RwError(
        "not_found", "no such pod", scope="ns/web", hint="check the name", details={"n": 1}
    )
    assert (err.code, err.message, err.scope, err.hint, err.details) == (
        "not_found",
        "no such pod",
        "ns/web",
        "check the name",
        {"n": 1},
    )
    assert str(err) == "no such pod"
    assert isinstance(err, Exception)


def test_rw_error_optional_fields_default_to_none():
    err = RwError("internal", "boom")
    assert (err.scope, err.hint, err.details) == (None, None, None)


@pytest.mark.parametrize("code", ["", "E_INTERNAL", "Not_Found", "nope", None, 3])
def test_rw_error_rejects_a_code_outside_the_closed_list(code):
    with pytest.raises(ValueError, match="error code"):
        RwError(code, "x")


def test_rw_command_error_is_an_rw_error_with_the_command_fields():
    err = RwCommandError(
        "unreachable", "kubectl failed", argv=["kubectl", "get", "pods"], returncode=1, stderr="x"
    )
    assert isinstance(err, RwError)
    assert (err.code, err.argv, err.returncode, err.stderr) == (
        "unreachable",
        ["kubectl", "get", "pods"],
        1,
        "x",
    )


def test_rw_command_error_validates_its_code_too():
    with pytest.raises(ValueError, match="error code"):
        RwCommandError("bogus", "m", argv=["x"], returncode=1, stderr="")


KUBECTL = ["kubectl", "get", "pods"]

_EXEC_MISSING_BINARY = (
    "error: Internal error occurred: error executing command in container: "
    'failed to exec in container: failed to start exec "4b1c": OCI runtime exec failed: '
    "exec failed: unable to start container process: "
    'exec: "df": executable file not found in $PATH: unknown'
)

# One real-looking sample per rule, plus the precedence and fall-through cases.
_CASES = [
    # dependency: a binary missing inside `kubectl exec` (kubectl returns 1 or 126)
    (["kubectl", "exec", "pvc-pod", "--", "df"], 1, _EXEC_MISSING_BINARY, "dependency"),
    (["kubectl", "exec", "pvc-pod", "--", "df"], 126, _EXEC_MISSING_BINARY, "dependency"),
    # permission_denied
    (
        ["kubectl", "get", "pods", "-n", "kube-system"],
        1,
        'Error from server (Forbidden): pods is forbidden: User "system:serviceaccount:default:rw" '
        'cannot list resource "pods" in API group "" in the namespace "kube-system"',
        "permission_denied",
    ),
    (
        ["kubectl", "get", "secret", "x"],
        1,
        'error: secrets "x" is forbidden: cannot get resource',
        "permission_denied",
    ),
    (
        ["psql", "-h", "db", "-c", "select 1"],
        2,
        'psql: error: connection to server at "db" (10.0.0.5), port 5432 failed: FATAL:  '
        "permission denied for table accounts",
        "permission_denied",
    ),
    (
        ["curl", "-sS", "https://x"],
        23,
        "curl: (23) Failure writing output: Permission denied",
        "permission_denied",
    ),
    (
        ["az", "vm", "list"],
        1,
        "(AuthorizationFailed) The client 'a@b.c' with object id '1' does not have authorization "
        "to perform action 'Microsoft.Compute/virtualMachines/read' over scope '/subscriptions/s'",
        "permission_denied",
    ),
    (
        ["aws", "s3", "ls", "s3://b"],
        254,
        "An error occurred (AccessDenied) when calling the ListObjectsV2 operation: Access Denied",
        "permission_denied",
    ),
    (
        ["gcloud", "compute", "instances", "list"],
        1,
        "ERROR: (gcloud.compute.instances.list) HTTPError 403: Required 'compute.instances.list' "
        "permission for 'projects/p'\n  PERMISSION_DENIED: forbidden: caller lacks access",
        "permission_denied",
    ),
    # not_found
    (
        ["kubectl", "get", "pod", "web-1"],
        1,
        'Error from server (NotFound): pods "web-1" not found',
        "not_found",
    ),
    (
        ["kubectl", "get", "ns", "gone"],
        1,
        'Error from server (NotFound): namespaces "gone" not found',
        "not_found",
    ),
    (
        ["az", "group", "show", "-n", "rg"],
        3,
        "(ResourceGroupNotFound) Resource group 'rg' could not be found.",
        "not_found",
    ),
    (
        ["aws", "ec2", "describe-instances", "--instance-ids", "i-0"],
        254,
        "An error occurred (InvalidInstanceID.NotFound) when calling the DescribeInstances "
        "operation: The instance ID 'i-0' does not exist",
        "not_found",
    ),
    (
        ["psql", "-d", "nope"],
        2,
        'psql: error: connection to server on socket failed: FATAL:  database "nope" not found',
        "not_found",
    ),
    (
        ["gcloud", "sql", "instances", "describe", "x"],
        1,
        "ERROR: (gcloud.sql.instances.describe) HTTPError 404: The Cloud SQL instance does "
        "not exist. Resource not found",
        "not_found",
    ),
    # unreachable
    (
        ["kubectl", "get", "nodes"],
        1,
        "Unable to connect to the server: dial tcp 10.0.0.1:6443: i/o timeout",
        "unreachable",
    ),
    (
        ["kubectl", "get", "nodes"],
        1,
        "The connection to the server localhost:8080 was refused - did you specify the right "
        "host or port?\nUnable to connect to the server: dial tcp 127.0.0.1:8080: connect: "
        "connection refused",
        "unreachable",
    ),
    (
        ["curl", "-sS", "https://nope.invalid"],
        6,
        "curl: (6) Could not resolve host: nope.invalid",
        "unreachable",
    ),
    (
        ["psql", "-h", "nope.invalid"],
        2,
        'psql: error: could not translate host name "nope.invalid" to address: '
        "Name or service not known",
        "unreachable",
    ),
    (
        ["psql", "-h", "10.0.0.9"],
        2,
        'psql: error: connection to server at "10.0.0.9", port 5432 failed: Connection refused\n'
        "\tIs the server running on that host and accepting TCP/IP connections?",
        "unreachable",
    ),
    (
        ["curl", "-sS", "https://expired.example"],
        35,
        "curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL; TLS handshake failure",
        "unreachable",
    ),
    (
        ["kubectl", "get", "pods"],
        1,
        "Unable to connect to the server: x509: certificate signed by unknown authority",
        "unreachable",
    ),
    (
        ["kubectl", "get", "pods"],
        1,
        'Get "https://api.example:6443/api": dial tcp: lookup api.example: no such host',
        "unreachable",
    ),
    (
        ["aws", "sts", "get-caller-identity"],
        255,
        'Could not connect to the endpoint URL: "https://sts.us-east-1.amazonaws.com/" '
        "(no such host)",
        "unreachable",
    ),
    # timeout
    (["timeout", "5", "sleep", "9"], 124, "", "timeout"),
    (
        ["kubectl", "wait", "--for=condition=Ready", "pod/x"],
        1,
        "error: timed out waiting for the condition on pods/x",
        "timeout",
    ),
    (
        ["gcloud", "compute", "ssh", "vm"],
        1,
        "ERROR: (gcloud.compute.ssh) Operation timed out: context deadline exceeded",
        "timeout",
    ),
    (
        ["kubectl", "logs", "p"],
        1,
        "Error from server: rpc error: code = DeadlineExceeded desc = context deadline exceeded",
        "timeout",
    ),
    (
        ["az", "aks", "show"],
        1,
        "ERROR: Deadline Exceeded while waiting for the operation",
        "timeout",
    ),
    # rate_limited
    (
        ["curl", "-sS", "-i", "https://api.example/x"],
        22,
        "curl: (22) The requested URL returned error: 429",
        "rate_limited",
    ),
    (
        ["gh", "api", "/repos"],
        1,
        "gh: API rate limit exceeded for user ID 1. (HTTP 403)",
        "rate_limited",
    ),
    (
        ["curl", "-sS", "https://api.example/x"],
        22,
        "HTTP/1.1 429 Too Many Requests",
        "rate_limited",
    ),
    (
        ["aws", "ec2", "describe-instances"],
        254,
        "An error occurred (Throttling) when calling the DescribeInstances operation: "
        "Rate exceeded",
        "rate_limited",
    ),
    (
        ["az", "monitor", "metrics", "list"],
        1,
        "(TooManyRequests) Too Many Requests: slow down",
        "rate_limited",
    ),
    # storage
    (
        ["tar", "xf", "big.tar"],
        2,
        "tar: big.bin: Cannot write: No space left on device",
        "storage",
    ),
    (
        ["pg_dump", "db"],
        1,
        "pg_dump: error: could not write to output file: No space left on device",
        "storage",
    ),
    (
        ["aws", "s3", "cp", "x", "y"],
        1,
        "write /tmp/x: Disk quota exceeded",
        "storage",
    ),
    # dependency
    (["jq", "."], 127, "bash: jq: command not found", "dependency"),
    (["nosuchtool"], 127, "", "dependency"),
    (["sh", "-c", "foo"], 1, "sh: 1: foo: command not found", "dependency"),
    # internal
    (["kubectl", "get", "pods"], 1, "error: unknown flag: --bogus", "internal"),
    (["psql", "-c", "selec 1"], 1, 'ERROR:  syntax error at or near "selec"', "internal"),
    (["false"], 1, "", "internal"),
    # precedence
    (
        ["kubectl", "get", "x"],
        1,
        'Error from server (Forbidden): x is forbidden; also pods "p" not found',
        "permission_denied",
    ),
    (
        ["jq", "."],
        127,
        "jq: not found",
        "dependency",  # `not found` with rc 127 is a missing binary, not a missing resource
    ),
    (
        ["kubectl", "get", "x"],
        1,
        "dial tcp 1.2.3.4:443: i/o timeout, context deadline exceeded",
        "unreachable",
    ),
    # From lane F's kubectl pilots (2026-11-04).
    (
        KUBECTL,
        1,
        "Error from server (Timeout): the server was unable to return a response in the "
        "time allotted, but may still be processing the request (get pods)",
        "timeout",
    ),
    (
        KUBECTL,
        1,
        "Error from server (ServiceUnavailable): the server is currently unable to handle the "
        "request (get pods.metrics.k8s.io)",
        "unreachable",
    ),
    (KUBECTL, 1, 'error: Get "https://10.0.0.1:443/api/v1/pods": EOF', "unreachable"),
    (KUBECTL, 1, "error: unexpected EOF", "unreachable"),
    (
        KUBECTL,
        1,
        'error: the server doesn\'t have a resource type "helmreleases"',
        "dependency",
    ),
    (KUBECTL, 1, "error: You must be logged in to the server (Unauthorized)", "permission_denied"),
]


@pytest.mark.parametrize(
    ("argv", "returncode", "stderr", "expected"),
    _CASES,
    ids=[f"{i:02d}-{c[3]}" for i, c in enumerate(_CASES)],
)
def test_classify_command_error(argv, returncode, stderr, expected):
    assert classify_command_error(argv, returncode, stderr) == expected


def test_there_are_at_least_twenty_samples():
    assert len(_CASES) >= 20


def test_every_classified_code_is_in_the_closed_list():
    for argv, rc, stderr, _ in _CASES:
        assert classify_command_error(argv, rc, stderr) in ERROR_CODES


def test_matching_is_case_insensitive():
    assert classify_command_error(["x"], 1, "Permission Denied") == "permission_denied"
    assert classify_command_error(["x"], 1, "PERMISSION DENIED (publickey)") == "permission_denied"
    assert classify_command_error(["x"], 1, "pod NOTFOUND") == "not_found"


def test_command_not_found_is_a_dependency_not_a_missing_resource():
    assert classify_command_error(["x"], 1, "sh: x: command not found") == "dependency"
    assert classify_command_error(["x"], 1, "x: Command Not Found") == "dependency"


def test_exec_missing_binary_is_a_dependency_by_either_wording():
    runtime = 'OCI runtime exec failed: exec: "df": executable file not found in $PATH'
    assert classify_command_error(["x"], 1, runtime.upper()) == "dependency"
    assert (
        classify_command_error(
            ["x"], 126, "failed to exec in container: fork/exec /bin/x: no such file or directory"
        )
        == "dependency"
    )


def test_a_plain_missing_file_is_not_taken_for_a_missing_binary():
    # No "failed to exec": an ordinary command naming a missing path stays out of the new rule.
    assert (
        classify_command_error(["cat", "/data/x"], 1, "cat: /data/x: No such file or directory")
        != "dependency"
    )


def test_not_found_at_returncode_127_is_a_missing_binary():
    assert classify_command_error(["x"], 127, "x: not found") == "dependency"


def test_empty_stderr_with_an_ordinary_code_is_internal():
    assert classify_command_error([], 2, "") == "internal"
