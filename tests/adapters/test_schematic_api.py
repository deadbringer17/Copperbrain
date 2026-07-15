import shutil
from pathlib import Path

import kicad_sch_api
import pytest

from copperbrain.adapters.schematic_api import SchematicApiAdapter, _number, _string
from copperbrain.errors import CopperbrainError
from copperbrain.models import ChangeOperation

FIXTURE = Path(__file__).parents[1] / "fixtures" / "kicad10_minimal" / "demo.kicad_sch"


def test_operation_parameter_helpers() -> None:
    assert _number({"x": 1}, "x") == 1.0
    assert _string({"name": "LCSC"}, "name") == "LCSC"
    with pytest.raises(CopperbrainError):
        _number({"x": True}, "x")
    with pytest.raises(CopperbrainError):
        _string({}, "name")


def test_validate_rejects_non_schematic(tmp_path: Path) -> None:
    path = tmp_path / "bad.kicad_sch"
    path.write_text("bad", encoding="utf-8")
    report = SchematicApiAdapter().validate(path)
    assert not report.valid


def test_update_property_replaces_existing_standard_footprint(tmp_path: Path) -> None:
    schematic = tmp_path / "demo.kicad_sch"
    shutil.copy2(FIXTURE, schematic)
    footprint = "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-3-2-5.08"

    SchematicApiAdapter().apply(
        schematic,
        (
            ChangeOperation(
                kind="update_property",
                target="J1",
                parameters={
                    "name": "Footprint",
                    "value": footprint,
                    "hidden": True,
                },
            ),
        ),
    )

    reloaded = kicad_sch_api.load_schematic(str(schematic))
    assert reloaded.components.get("J1").footprint == footprint
    assert footprint in schematic.read_text(encoding="utf-8-sig")
