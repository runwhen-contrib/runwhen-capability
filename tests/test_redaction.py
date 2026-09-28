"""Redactor -- the forms of a secret it removes, and the ones it leaves."""

from __future__ import annotations

import base64
import json
import textwrap

import pytest

from runwhen_capability._redaction import REDACTED, Redactor

SECRET = "s3cr3t-Value_With/Plus+And=Eq"


@pytest.mark.parametrize(
    "printed",
    [
        SECRET,
        base64.b64encode(SECRET.encode()).decode(),
        base64.urlsafe_b64encode(SECRET.encode()).decode(),
        # inside a longer base64 string, at each alignment (a basic-auth header)
        base64.b64encode(b"user:" + SECRET.encode()).decode(),
        base64.b64encode(b"ab:" + SECRET.encode() + b":tail").decode(),
        base64.b64encode(b"xyz:" + SECRET.encode()).decode(),
        json.dumps({"k": SECRET + '"\n'}),
        repr(SECRET + "'"),
        "https://host/?p=" + "s3cr3t-Value_With%2FPlus%2BAnd%3DEq",
    ],
    ids=[
        "raw",
        "b64",
        "b64url",
        "b64-offset-2",
        "b64-offset-0",
        "b64-offset-1",
        "json",
        "repr",
        "url",
    ],
)
def test_common_encodings_of_a_secret_are_redacted(printed):
    redactor = Redactor([SECRET + '"\n', SECRET + "'", SECRET])
    out = redactor.text(f"before {printed} after")
    assert REDACTED in out
    assert printed not in out
    assert SECRET not in out


def test_a_wrapped_base64_encoding_is_redacted_line_by_line():
    long_secret = "k" * 20 + SECRET * 4
    wrapped = "\n".join(textwrap.wrap(base64.b64encode(long_secret.encode()).decode(), 76))
    out = Redactor([long_secret]).text(wrapped)
    assert all(line == REDACTED or len(line) < 8 for line in out.splitlines())


def test_one_line_of_a_pem_key_is_redacted():
    body = [base64.b64encode(bytes([i]) * 48).decode() for i in range(1, 5)]
    pem = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(body) + "\n-----END PRIVATE KEY-----\n"
    out = Redactor([pem]).text(f"line: {body[2]}\n")
    assert body[2] not in out


def test_a_kubeconfig_token_or_decoded_client_key_printed_alone_is_redacted():
    key_pem = (
        "-----BEGIN EC PRIVATE KEY-----\n"
        "MHcCAQEEIIr4nd0mK3yB0dyL1n3F0rT3st1ngPurp0s3s\n"
        "-----END EC PRIVATE KEY-----\n"
    )
    kubeconfig = textwrap.dedent(
        f"""\
        apiVersion: v1
        kind: Config
        clusters:
        - name: prod-east
          cluster: {{server: "https://10.0.0.1"}}
        users:
        - name: admin
          user:
            token: eyJhbGciOiJSUzI1NiJ9.payload.signature
            client-key-data: {base64.b64encode(key_pem.encode()).decode()}
        """
    )
    redactor = Redactor([kubeconfig])
    assert "eyJhbGci" not in redactor.text("token=eyJhbGciOiJSUzI1NiJ9.payload.signature")
    assert "MHcCAQEE" not in redactor.text(key_pem)
    # structural names in the same document are not secrets and stay readable
    assert redactor.text("cluster prod-east, kind: Config") == "cluster prod-east, kind: Config"


def test_a_short_secret_is_redacted_only_as_a_whole_word():
    redactor = Redactor(["abc"])
    assert redactor.text("abcdef xabc") == "abcdef xabc"
    assert redactor.text("password=abc;") == f"password={REDACTED};"


def test_value_redacts_keys_and_nested_strings_and_skips_clean_values():
    redactor = Redactor([SECRET])
    clean = {"a": ["x", {"b": 1}]}
    assert redactor.value(clean) is clean
    assert redactor.value({SECRET: [f"x{SECRET}"]}) == {REDACTED: [f"x{REDACTED}"]}


def test_no_secrets_means_no_change():
    redactor = Redactor(["", "   "])
    assert not redactor
    assert redactor.text("anything") == "anything"
