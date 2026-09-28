"""`rwtask schemas` -- export each configured output schema from its pydantic
model into <capability-dir>/schemas/, or (`--check`) verify the committed
files match the models. Configured in the capability repo's pyproject.toml:

    [tool.rwtask.schemas."capabilities/<name>"]
    "<file>.json" = "<module>:<model>"
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from runwhen_capability.cli import main
from runwhen_capability.models import FindingsResult
from runwhen_capability.schemas import SchemaConfigError, load_targets

MODELS_SOURCE = """
from pydantic import BaseModel


class Summary(BaseModel):
    count: int
    names: list[str] = []


class Other(BaseModel):
    ok: bool
"""


def run_schemas(capsys, *args: str) -> tuple[int, str, str]:
    try:
        code = main(["schemas", *args])
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    """A capability repo with its own models module (uniquely named, so no
    two tests share an import) and one SDK model."""
    module = f"models_{uuid.uuid4().hex}"
    (tmp_path / f"{module}.py").write_text(MODELS_SOURCE)
    (tmp_path / "capabilities" / "cap").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\n\n'
        '[tool.rwtask.schemas."capabilities/cap"]\n'
        f'"summary.v1.json" = "{module}:Summary"\n'
        '"findings.v1.json" = "runwhen_capability.models:FindingsResult"\n'
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def expected_text(model) -> str:
    return json.dumps(TypeAdapter(model).json_schema(), indent=2) + "\n"


def test_writes_each_configured_schema_from_its_model(project, capsys):
    code, out, err = run_schemas(capsys)

    assert code == 0, err
    schemas = project / "capabilities" / "cap" / "schemas"
    assert (schemas / "findings.v1.json").read_text() == expected_text(FindingsResult)
    summary = json.loads((schemas / "summary.v1.json").read_text())
    assert summary["title"] == "Summary"
    assert set(summary["required"]) == {"count"}
    assert out.splitlines() == [
        "wrote capabilities/cap/schemas/summary.v1.json",
        "wrote capabilities/cap/schemas/findings.v1.json",
    ]


def test_check_passes_when_the_files_match_the_models(project, capsys):
    run_schemas(capsys)

    code, out, err = run_schemas(capsys, "--check")

    assert code == 0, err
    assert "2 schema file(s) up to date" in out


def test_check_fails_on_a_missing_file_and_writes_nothing(project, capsys):
    code, _, err = run_schemas(capsys, "--check")

    assert code == 1
    assert "capabilities/cap/schemas/summary.v1.json: missing" in err
    assert "capabilities/cap/schemas/findings.v1.json: missing" in err
    assert not (project / "capabilities" / "cap" / "schemas").exists()


def test_check_fails_on_a_stale_file_and_leaves_it_alone(project, capsys):
    run_schemas(capsys)
    stale = project / "capabilities" / "cap" / "schemas" / "findings.v1.json"
    stale.write_text('{"title": "FindingsResult"}\n')

    code, _, err = run_schemas(capsys, "--check")

    assert code == 1
    assert "capabilities/cap/schemas/findings.v1.json: out of date" in err
    assert "summary.v1.json" not in err
    assert stale.read_text() == '{"title": "FindingsResult"}\n'


def test_check_compares_text_not_just_json_values(project, capsys):
    """Same JSON, different formatting is still a diff -- the committed file
    must be exactly what the export writes."""
    run_schemas(capsys)
    path = project / "capabilities" / "cap" / "schemas" / "findings.v1.json"
    path.write_text(json.dumps(json.loads(path.read_text())) + "\n")

    code, _, err = run_schemas(capsys, "--check")

    assert code == 1
    assert "findings.v1.json: out of date" in err


def test_files_not_in_the_config_are_left_alone(project, capsys):
    """An older published version (not generated any more) stays on disk
    untouched and is not the check's concern."""
    schemas = project / "capabilities" / "cap" / "schemas"
    schemas.mkdir()
    (schemas / "summary.v0.json").write_text('{"old": true}\n')

    run_schemas(capsys)
    code, _, err = run_schemas(capsys, "--check")

    assert code == 0, err
    assert (schemas / "summary.v0.json").read_text() == '{"old": true}\n'


def test_project_dir_option_works_from_elsewhere(project, capsys, tmp_path_factory, monkeypatch):
    monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))

    code, out, err = run_schemas(capsys, "--project-dir", str(project))

    assert code == 0, err
    assert (project / "capabilities" / "cap" / "schemas" / "summary.v1.json").is_file()
    assert "wrote capabilities/cap/schemas/summary.v1.json" in out


def test_load_targets_keeps_config_order(project):
    targets = load_targets(project)

    assert [(t.capability_dir, t.filename) for t in targets] == [
        ("capabilities/cap", "summary.v1.json"),
        ("capabilities/cap", "findings.v1.json"),
    ]


# --- configuration errors --------------------------------------------------------


def test_no_pyproject_is_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    code, _, err = run_schemas(capsys)

    assert code == 1
    assert "pyproject.toml" in err


def test_no_schemas_table_is_an_error(tmp_path, monkeypatch, capsys):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    monkeypatch.chdir(tmp_path)

    code, _, err = run_schemas(capsys)

    assert code == 1
    assert "[tool.rwtask.schemas]" in err


@pytest.mark.parametrize(
    ("capability_dir", "filename", "model", "message"),
    [
        ("capabilities/cap", "a.v1.json", "no_colon_here", "module:attribute"),
        ("capabilities/cap", "a.v1.json", "no_such_module_xyz:Model", "no_such_module_xyz"),
        ("capabilities/cap", "a.v1.json", "runwhen_capability.models:Nope", "Nope"),
        ("capabilities/cap", "sub/a.v1.json", "runwhen_capability.models:Finding", "file name"),
        ("capabilities/cap", "a.v1.yaml", "runwhen_capability.models:Finding", ".json"),
        ("/abs/cap", "a.v1.json", "runwhen_capability.models:Finding", "relative"),
        ("../outside", "a.v1.json", "runwhen_capability.models:Finding", "relative"),
    ],
)
def test_bad_entries_are_errors_naming_the_entry(
    tmp_path, monkeypatch, capsys, capability_dir, filename, model, message
):
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.rwtask.schemas."{capability_dir}"]\n"{filename}" = "{model}"\n'
    )
    monkeypatch.chdir(tmp_path)

    code, _, err = run_schemas(capsys)

    assert code == 1
    assert message in err


def test_a_capability_entry_must_be_a_table(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.rwtask.schemas]\n"capabilities/cap" = "x"\n')

    with pytest.raises(SchemaConfigError, match="capabilities/cap"):
        load_targets(tmp_path)
