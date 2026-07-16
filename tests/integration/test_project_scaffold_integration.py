from pathlib import Path

import pytest

from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.adapters.pcb_design import PcbFileAdapter
from copperbrain.adapters.project_scaffold import ProjectScaffoldAdapter
from copperbrain.adapters.schematic_api import SchematicApiAdapter
from copperbrain.models import ProjectCreationSpec


@pytest.mark.live
def test_project_scaffold_uses_supported_kicad_apis(tmp_path: Path) -> None:
    detection = detect_kicad()
    if detection.selected_cli is None:
        pytest.skip("KiCad is not installed")
    destination = tmp_path / "bench"
    project, schematic, pcb = ProjectScaffoldAdapter().create(
        destination, ProjectCreationSpec(name="test_bench", copper_layers=4)
    )

    assert project.is_file()
    assert SchematicApiAdapter().validate(schematic).valid
    assert PcbFileAdapter().validate(pcb).valid
    text = pcb.read_text(encoding="utf-8")
    assert '"In1.Cu"' in text
    assert '"In2.Cu"' in text
