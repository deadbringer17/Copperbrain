from pathlib import Path

import pytest

from copperbrain.errors import CopperbrainError
from copperbrain.models import ComponentAssetBundle
from copperbrain.services.assets import AssetService


def test_import_bundle_is_valid_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source"
    project = tmp_path / "project"
    source.mkdir()
    project.mkdir()
    symbol = source / "part.kicad_sym"
    footprint = source / "part.kicad_mod"
    model = source / "part.step"
    sheet = source / "part.pdf"
    for path in (symbol, footprint, model, sheet):
        path.write_bytes(path.name.encode())
    symbol.write_text(
        '(kicad_symbol_lib (symbol "Part" (pin passive line (number "1"))))',
        encoding="utf-8",
    )
    footprint.write_text('(footprint "Part" (layer "F.Cu") (pad "1" smd rect))', encoding="utf-8")
    bundle = ComponentAssetBundle(
        lcsc="C1",
        nickname="CB_C1",
        symbol=symbol,
        footprint=footprint,
        model_3d=model,
        datasheet=sheet,
    )
    first = AssetService().import_bundle(project, bundle)
    second = AssetService().import_bundle(project, bundle)
    assert first.validation.valid
    assert not first.idempotent
    assert second.idempotent


def test_import_bundle_rejects_wrong_extension(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    wrong = tmp_path / "part.txt"
    wrong.write_text("x", encoding="utf-8")
    with pytest.raises(CopperbrainError, match="unsupported"):
        AssetService().import_bundle(
            project, ComponentAssetBundle(lcsc="C1", nickname="CB", symbol=wrong, footprint=wrong)
        )


def test_import_bundle_rejects_pin_pad_mismatch(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    symbol = tmp_path / "part.kicad_sym"
    footprint = tmp_path / "part.kicad_mod"
    symbol.write_text(
        '(kicad_symbol_lib (symbol "Part" (pin passive line (number "1"))))',
        encoding="utf-8",
    )
    footprint.write_text('(footprint "Part" (layer "F.Cu") (pad "2" smd rect))', encoding="utf-8")
    with pytest.raises(CopperbrainError, match="do not correspond"):
        AssetService().import_bundle(
            project,
            ComponentAssetBundle(lcsc="C1", nickname="CB", symbol=symbol, footprint=footprint),
        )
