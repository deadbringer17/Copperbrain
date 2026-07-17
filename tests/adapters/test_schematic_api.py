import shutil
from pathlib import Path
from types import SimpleNamespace

import kicad_sch_api
import pytest
from sexpdata import Symbol

from copperbrain.adapters import schematic_api
from copperbrain.adapters.schematic_api import (
    SchematicApiAdapter,
    _number,
    _remove_private_library_properties,
    _string,
)
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


def test_private_library_property_workaround_preserves_public_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbol = SimpleNamespace(
        raw_kicad_data=[
            Symbol("symbol"),
            "DRV8311S",
            [Symbol("property"), Symbol("private"), "KLC_NOTE", "documentation"],
            [Symbol("property"), "Reference", "U"],
        ],
        property_positions={"private": (0, 0, 0), "Reference": (1, 1, 0)},
    )
    cache = SimpleNamespace(get_symbol=lambda lib_id: symbol)
    monkeypatch.setattr(schematic_api, "get_symbol_cache", lambda: cache)

    _remove_private_library_properties("Driver_Motor:DRV8311S")

    assert symbol.raw_kicad_data == [
        Symbol("symbol"),
        "DRV8311S",
        [Symbol("property"), "Reference", "U"],
    ]
    assert symbol.property_positions == {"Reference": (1, 1, 0)}


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


def test_set_paper_size_accepts_allowlisted_size(tmp_path: Path) -> None:
    schematic = tmp_path / "paper.kicad_sch"
    created = kicad_sch_api.create_schematic("paper")
    created.save(schematic)

    SchematicApiAdapter().apply(
        schematic,
        (
            ChangeOperation(
                kind="set_paper_size",
                target="schematic",
                parameters={"paper": "A3"},
            ),
        ),
    )

    assert '(paper "A3")' in schematic.read_text(encoding="utf-8-sig")


def test_set_paper_size_rejects_non_allowlisted_size(tmp_path: Path) -> None:
    schematic = tmp_path / "paper.kicad_sch"
    created = kicad_sch_api.create_schematic("paper")
    created.save(schematic)

    with pytest.raises(CopperbrainError, match="paper size"):
        SchematicApiAdapter().apply(
            schematic,
            (
                ChangeOperation(
                    kind="set_paper_size",
                    target="schematic",
                    parameters={"paper": "custom"},
                ),
            ),
        )
