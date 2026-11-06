"""contracts/registry-api.openapi.yaml is valid OpenAPI 3.1 and resolves offline.

The spec points into the JSON Schemas (`schemas/<name>.schema.json#/...`), and
those files reference each other by `$id` URL (`https://schemas.runwhen.com/
<name>/v1.json`). Every test here runs with sockets disabled; the validator
resolves those URLs through a handler backed by contracts/schemas, so a
resolution that needs the network fails instead of passing quietly.
"""

from __future__ import annotations

import json
import re
import socket
from pathlib import Path
from typing import Any

import pytest
from jsonschema_path import SchemaPath
from jsonschema_path.handlers import default_handlers
from openapi_spec_validator.validation import OpenAPIV31SpecValidator

from runwhen_capability.contracts import SCHEMA_NAMES, load_yaml, schema

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
SPEC_PATH = CONTRACTS / "registry-api.openapi.yaml"
SCHEMAS = CONTRACTS / "schemas"

_SCHEMA_URL = re.compile(r"https://schemas\.runwhen\.com/(?P<name>[^/]+)/v1\.json")
_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any):
        raise AssertionError("network access attempted; the OpenAPI checks must run offline")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return load_yaml(SPEC_PATH.read_text(encoding="utf-8"))


def _local_schema(url: str) -> Any:
    """`https://schemas.runwhen.com/<name>/v1.json` -> contracts/schemas/<name>.schema.json."""
    match = _SCHEMA_URL.fullmatch(url)
    if match is None:
        raise AssertionError(f"refusing to fetch {url}: not one of our schema ids")
    return json.loads((SCHEMAS / f"{match['name']}.schema.json").read_text(encoding="utf-8"))


def _validate(spec: dict[str, Any], handlers: dict[str, Any]) -> list[str]:
    """Validate with the given retrieval handlers; return the `$id` URLs fetched."""
    fetched: list[str] = []

    def retrieve(url: str) -> Any:
        fetched.append(url)
        return _local_schema(url)

    paths = SchemaPath.from_dict(
        spec,
        base_uri=SPEC_PATH.resolve().as_uri(),
        handlers={**handlers, "http": retrieve, "https": retrieve},
    )
    OpenAPIV31SpecValidator(paths).validate()
    return fetched


def _local_handlers() -> dict[str, Any]:
    # Local files only: the "file" handler reads schemas/<name>.schema.json.
    return {"file": default_handlers["file"]}


def test_spec_is_openapi_31(spec):
    assert spec["openapi"].startswith("3.1.")


def test_spec_validates_offline(spec):
    fetched = _validate(spec, _local_handlers())
    assert fetched, "the schemas' `$id` refs were never followed; the offline path is untested"
    assert all(_SCHEMA_URL.fullmatch(url) for url in fetched), fetched


def test_a_network_fetch_would_fail_here(spec):
    """Control: without the local handler the same spec cannot be resolved with sockets off."""
    paths = SchemaPath.from_dict(
        spec, base_uri=SPEC_PATH.resolve().as_uri(), handlers=dict(default_handlers)
    )
    with pytest.raises(Exception):  # noqa: B017 - Unresolvable or the refused socket
        OpenAPIV31SpecValidator(paths).validate()


def _external_refs(node: Any, where: str = "#"):
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            yield where, ref
        for key, value in node.items():
            yield from _external_refs(value, f"{where}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _external_refs(value, f"{where}/{index}")


def _pointer(doc: Any, pointer: str) -> Any:
    for token in pointer.lstrip("/").split("/") if pointer else []:
        token = token.replace("~1", "/").replace("~0", "~")
        doc = doc[int(token)] if isinstance(doc, list) else doc[token]
    return doc


def test_external_refs_resolve_into_contracts_schemas(spec):
    refs = list(_external_refs(spec))
    assert refs, "expected the registry API to reference the JSON Schemas"
    for where, ref in refs:
        file_part, _, fragment = ref.partition("#")
        match = re.fullmatch(r"schemas/(?P<name>[^/]+)\.schema\.json", file_part)
        assert match, f"{where}: {ref} is not a schemas/<name>.schema.json reference"
        name = match["name"]
        assert name in SCHEMA_NAMES, f"{where}: {ref}: no such schema {name!r}"
        assert (SCHEMAS / f"{name}.schema.json").is_file(), f"{where}: {ref}: file missing"
        try:
            # The packaged copy, as consumers of the wheel see it.
            _pointer(schema(name), fragment)
        except (KeyError, IndexError):
            pytest.fail(f"{where}: {ref}: JSON pointer {fragment!r} does not resolve")


def _operations(spec: dict[str, Any]):
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method in _HTTP_METHODS:
                yield path, method, op


def test_operation_ids_are_unique(spec):
    ids = [op["operationId"] for _, _, op in _operations(spec) if "operationId" in op]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicated, f"duplicate operationIds: {duplicated}"


def _resolve_local(spec: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        ref = node["$ref"]
        assert ref.startswith("#/"), f"parameter ref {ref} must be local"
        node = _pointer(spec, ref[1:])
    return node


def _path_params(spec: dict[str, Any], parameters: list[dict[str, Any]]) -> set[str]:
    resolved = [_resolve_local(spec, p) for p in parameters]
    return {p["name"] for p in resolved if p["in"] == "path"}


def test_every_path_parameter_is_declared_and_used(spec):
    for path, item in spec["paths"].items():
        templated = set(re.findall(r"\{([^}/]+)\}", path))
        shared = _path_params(spec, item.get("parameters", []))
        for method, op in ((m, o) for m, o in item.items() if m in _HTTP_METHODS):
            declared = shared | _path_params(spec, op.get("parameters", []))
            assert templated <= declared, (
                f"{method.upper()} {path}: undeclared path parameters "
                f"{sorted(templated - declared)}"
            )
            assert declared <= templated, (
                f"{method.upper()} {path}: declared but not in the path "
                f"{sorted(declared - templated)}"
            )


def test_path_parameters_are_required(spec):
    for path, item in spec["paths"].items():
        params = list(item.get("parameters", []))
        for method, op in ((m, o) for m, o in item.items() if m in _HTTP_METHODS):
            params += op.get("parameters", [])
        for param in (_resolve_local(spec, p) for p in params):
            if param["in"] == "path":
                assert param.get("required") is True, f"{path}: {param['name']} must be required"
