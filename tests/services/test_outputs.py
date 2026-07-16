from pathlib import Path

import pytest

from copperbrain.errors import CopperbrainError
from copperbrain.services.outputs import (
    OUTPUT_DIRECTORY,
    output_path,
    project_output_root,
    publish_preview,
)


def test_output_path_is_always_below_project_root(tmp_path: Path) -> None:
    destination = output_path(tmp_path, "bom", "Copperbrain-BOM.csv")
    assert destination == tmp_path / OUTPUT_DIRECTORY / "bom" / "Copperbrain-BOM.csv"
    assert destination.parent.is_dir()
    assert project_output_root(tmp_path) == tmp_path / OUTPUT_DIRECTORY


@pytest.mark.parametrize("filename", ["../bom.csv", "folder/bom.csv", "/tmp/bom.csv"])
def test_output_path_rejects_directories(filename: str, tmp_path: Path) -> None:
    with pytest.raises(CopperbrainError, match="filename"):
        output_path(tmp_path, "bom", filename)


def test_publish_preview_is_project_local_and_excludes_nested_outputs(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    (workspace / "demo.kicad_sch").write_text("preview", encoding="utf-8")
    nested = workspace / OUTPUT_DIRECTORY
    nested.mkdir()
    (nested / "stale.txt").write_text("stale", encoding="utf-8")
    history = workspace / ".history" / ".git"
    history.mkdir(parents=True)
    (history / "config").write_text("private", encoding="utf-8")
    backups = workspace / "demo-backups"
    backups.mkdir()
    (backups / "old.zip").write_bytes(b"backup")
    (workspace / "demo.kicad_prl").write_text("local paths", encoding="utf-8")

    published = publish_preview(workspace, project, "change-1")

    assert published == project / OUTPUT_DIRECTORY / "previews" / "change-1"
    assert (published / "demo.kicad_sch").read_text(encoding="utf-8") == "preview"
    assert not (published / OUTPUT_DIRECTORY).exists()
    assert not (published / ".history").exists()
    assert not (published / "demo-backups").exists()
    assert not (published / "demo.kicad_prl").exists()


def test_publish_preview_rejects_recursive_output_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output_copy = tmp_path / OUTPUT_DIRECTORY / "previews" / "old"
    output_copy.mkdir(parents=True)

    with pytest.raises(CopperbrainError, match="cannot be used as a source"):
        publish_preview(workspace, output_copy, "nested")
