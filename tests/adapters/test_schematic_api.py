from pathlib import Path

import pytest

from copperbrain.adapters.schematic_api import _number, _string
from copperbrain.errors import CopperbrainError


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
    from copperbrain.adapters.schematic_api import SchematicApiAdapter

    report = SchematicApiAdapter().validate(path)
    assert not report.valid
