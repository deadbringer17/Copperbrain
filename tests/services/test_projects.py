import json
from pathlib import Path

import pytest

from copperbrain.errors import CopperbrainError
from copperbrain.models import Component, KicadDetection, Net
from copperbrain.services import projects


def make_project(tmp_path: Path) -> Path:
    project = tmp_path / "demo.kicad_pro"
    project.write_text(json.dumps({"meta": {"version": 1}}), encoding="utf-8")
    (tmp_path / "demo.kicad_sch").write_text("fixture", encoding="utf-8")
    return project


def test_hash_file_and_aggregate_are_deterministic(tmp_path: Path) -> None:
    file = tmp_path / "x"
    file.write_bytes(b"abc")
    assert (
        projects.hash_file(file)
        == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert projects.aggregate_hash({"b": "2", "a": "1"}) == projects.aggregate_hash(
        {"a": "1", "b": "2"}
    )


def test_open_project_and_get_session(tmp_path: Path) -> None:
    service = projects.ProjectService()
    session = service.open_project(make_project(tmp_path))
    assert service.get_session(session.id) == session
    assert session.schematic_files[0].name == "demo.kicad_sch"


def test_open_project_ignores_copperbrain_outputs(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    output = tmp_path / "copperbrain-output" / "previews" / "old"
    output.mkdir(parents=True)
    (output / "preview.kicad_sch").write_text("preview", encoding="utf-8")

    session = projects.ProjectService().open_project(project)

    assert [item.name for item in session.schematic_files] == ["demo.kicad_sch"]


def test_open_project_rejects_a_published_preview_as_source(tmp_path: Path) -> None:
    output = tmp_path / "copperbrain-output" / "previews" / "old"
    output.mkdir(parents=True)
    project = make_project(output)

    with pytest.raises(CopperbrainError, match="cannot be used as a source") as caught:
        projects.ProjectService().open_project(project)
    assert caught.value.error.actionable_hint is not None
    assert "original KiCad project" in caught.value.error.actionable_hint


def test_open_project_ignores_history_and_backup_schematics(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    history = tmp_path / ".history"
    backups = tmp_path / "demo-backups"
    history.mkdir()
    backups.mkdir()
    (history / "demo.kicad_sch").write_text("history", encoding="utf-8")
    (backups / "demo.kicad_sch").write_text("backup", encoding="utf-8")

    session = projects.ProjectService().open_project(project)

    assert session.schematic_files == (tmp_path / "demo.kicad_sch",)


def test_open_project_rejects_invalid_directory(tmp_path: Path) -> None:
    with pytest.raises(CopperbrainError, match="exactly one"):
        projects.ProjectService().open_project(tmp_path)


def test_get_session_rejects_unknown_id() -> None:
    with pytest.raises(CopperbrainError, match="not found"):
        projects.ProjectService().get_session("missing")


def test_summary_trace_analyze_and_erc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    service = projects.ProjectService()
    session = service.open_project(make_project(tmp_path))
    detection = KicadDetection(
        installations=(), selected_cli=tmp_path / "cli", user_data_directories=(), plugins=()
    )
    monkeypatch.setattr(projects, "detect_kicad", lambda: detection)
    monkeypatch.setattr(
        projects,
        "export_netlist",
        lambda cli, sch: ((Component(reference="R1", value="10k"),), (Net(name="VCC"),)),
    )
    monkeypatch.setattr(projects, "run_erc", lambda cli, sch: "erc")
    summary = service.summary(session.id)
    assert summary.components[0].reference == "R1"
    assert service.trace_net(session.id, "VCC").name == "VCC"
    assert service.analyze(session.id)["component_count"] == 1
    assert service.run_erc(session.id) == "erc"


def test_trace_net_rejects_unknown_net(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    service = projects.ProjectService()
    session = service.open_project(make_project(tmp_path))
    monkeypatch.setattr(
        service,
        "summary",
        lambda session_id: projects.ProjectSummary(
            session_id=session_id, sheets=(), components=(), nets=(), power_symbols=()
        ),
    )
    with pytest.raises(CopperbrainError, match="Net was not found"):
        service.trace_net(session.id, "missing")
