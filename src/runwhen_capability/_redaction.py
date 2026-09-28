"""Redactor -- removes a run's secret and credential values from what a bundle
task hands back (outputs, error messages, the log tail).

Redaction is a guard against accidental disclosure by an honest task: a
`set -x` trace, a debug print, a tool that echoes its config. It is not a
boundary against a task that wants to leak a value -- reversing or
re-encoding a string defeats any matcher. Within that scope it covers the
ways an honest task most often prints a secret:

- the value itself;
- JSON-, Python-repr- and URL-escaped forms;
- base64 (standard and URL-safe) at every alignment, so the value is found
  inside a longer encoded string (a basic-auth header), and wrapped at 64
  or 76 columns, as `base64` and PEM print it;
- each whitespace-free line of a multi-line value (PEM bodies, wrapped
  encodings), so printing one line of a key is caught;
- for a structured value (a kubeconfig, a JSON key file), each string under
  a key that names secret material (token, password, secret, private key,
  client-key-data, ...), plus the decoded form of a base64 `*-data` field.

Values shorter than MIN_SUBSTRING_CHARS are only redacted where they stand
alone as a word: as substrings they would blank out common text ("abc"
inside "abcdef"), and a string that short is no secret to begin with.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import urllib.parse
from typing import Any

import yaml

from .custom.yaml_lines import safe_load

REDACTED = "***REDACTED***"
MIN_SUBSTRING_CHARS = 8
_MIN_LINE_CHARS = 16
_MAX_LEAVES = 200

_SENSITIVE_KEY_RE = re.compile(
    r"token|passw|secret|private|credential|api[-_]?key|access[-_]?key|key[-_]data|^key$",
    re.IGNORECASE,
)
# ...except keys that name where or what kind of secret, not the secret
# itself (a key file's "token_uri", an OAuth "token_type").
_NOT_SECRET_KEY_RE = re.compile(r"(ur[il]|type|endpoint|expir\w*)$", re.IGNORECASE)
_WORD = r"A-Za-z0-9_"


class Redactor:
    def __init__(self, secrets: list[str]) -> None:
        substrings: set[str] = set()
        words: set[str] = set()
        for secret in secrets:
            self._add(secret, substrings, words, structured=True)
        # Longest first, so a value is replaced whole before any shorter
        # variant of it (one of its lines, say) could split it.
        self._substrings = sorted(substrings, key=len, reverse=True)
        # Each one as it appears inside JSON text, for value()'s prefilter.
        self._json_forms = [json.dumps(s)[1:-1] for s in self._substrings]
        self._words_re = (
            re.compile(
                f"(?<![{_WORD}])(?:"
                + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
                + f")(?![{_WORD}])"
            )
            if words
            else None
        )
        self.longest = max((len(s) for s in self._substrings), default=0)

    def __bool__(self) -> bool:
        return bool(self._substrings) or self._words_re is not None

    def text(self, text: str) -> str:
        for secret in self._substrings:
            if secret in text:
                text = text.replace(secret, REDACTED)
        if self._words_re is not None:
            text = self._words_re.sub(REDACTED, text)
        return text

    def value(self, value: Any) -> Any:
        """`value` with every string in it -- dict keys included --
        redacted. One pass over its JSON text first finds which secrets
        occur at all, so a large output with none in it is not walked
        string by string against every variant."""
        if not self:
            return value
        blob = json.dumps(value)
        present = [
            secret
            for secret, json_form in zip(self._substrings, self._json_forms, strict=True)
            if json_form in blob
        ]
        if not present and self._words_re is None:
            return value

        def redact(text: str) -> str:
            for secret in present:
                if secret in text:
                    text = text.replace(secret, REDACTED)
            if self._words_re is not None:
                text = self._words_re.sub(REDACTED, text)
            return text

        return _map_strings(value, redact)

    # -- building the match set -------------------------------------------

    def _add(self, secret: str, substrings: set[str], words: set[str], structured: bool) -> None:
        if not secret or not secret.strip():
            return
        if len(secret) < MIN_SUBSTRING_CHARS:
            words.add(secret)
            return
        substrings.add(secret)
        substrings.update(v for v in _encodings(secret) if len(v) >= MIN_SUBSTRING_CHARS)
        if "\n" in secret:
            for line in secret.splitlines():
                line = line.strip()
                if len(line) >= _MIN_LINE_CHARS and not any(c.isspace() for c in line):
                    substrings.add(line)
        if structured:
            for leaf in _sensitive_leaves(secret):
                self._add(leaf, substrings, words, structured=False)


def _encodings(secret: str) -> set[str]:
    out = {
        json.dumps(secret)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
        repr(secret)[1:-1],
        urllib.parse.quote(secret, safe=""),
        urllib.parse.quote_plus(secret, safe=""),
    }
    raw = secret.encode("utf-8")
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        full = encode(raw).decode("ascii")
        out.add(full)
        for width in (64, 76):
            out.update(full[i : i + width] for i in range(0, len(full), width))
        for offset in (1, 2):
            # The secret as it appears inside a longer base64 string: encode
            # it behind `offset` filler bytes and keep only the characters
            # that depend on the secret's bytes alone.
            shifted = encode(b"\0" * offset + raw).decode("ascii").rstrip("=")
            skip = {1: 2, 2: 3}[offset]
            end = len(shifted) - (1 if (offset + len(raw)) % 3 else 0)
            out.add(shifted[skip:end])
        core = full.rstrip("=")
        out.add(core[: len(core) - (1 if len(raw) % 3 else 0)])
    out.discard(secret)
    return out


def _sensitive_leaves(secret: str) -> list[str]:
    """String values under secret-naming keys, when `secret` is a JSON or
    YAML document (a kubeconfig, a cloud key file); plus the decoded text
    of a base64 `...-data` value, such as a kubeconfig's client-key-data."""
    stripped = secret.lstrip()
    if not stripped or ("\n" not in secret and stripped[0] not in "{["):
        return []
    try:
        doc = json.loads(secret)
    except ValueError:
        try:
            doc = safe_load(secret)
        except (yaml.YAMLError, RecursionError, ValueError):
            return []
    leaves: list[str] = []
    stack = [doc]
    while stack and len(leaves) < _MAX_LEAVES:
        node = stack.pop()
        if isinstance(node, dict):
            for key, child in node.items():
                if (
                    isinstance(child, str)
                    and _SENSITIVE_KEY_RE.search(str(key))
                    and not _NOT_SECRET_KEY_RE.search(str(key))
                ):
                    leaves.append(child)
                    if str(key).lower().endswith("data"):
                        decoded = _b64_text(child)
                        if decoded:
                            leaves.append(decoded)
                else:
                    stack.append(child)
        elif isinstance(node, list):
            stack.extend(node)
    return leaves


def _b64_text(value: str) -> str | None:
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return None


def _map_strings(value: Any, fn) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {_map_strings(k, fn): _map_strings(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [_map_strings(v, fn) for v in value]
    return value
