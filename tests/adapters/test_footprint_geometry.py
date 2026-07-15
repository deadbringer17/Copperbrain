from pathlib import Path

import pytest

from copperbrain.adapters.footprint_geometry import (
    add_generated_courtyard,
    analyze_component_footprint,
    parse_footprint_geometry,
)
from copperbrain.models import CourtyardAddition

FOOTPRINT = """(footprint "FinePitch"
  (version 20241229)
  (layer "F.Cu")
  (fp_line (start -1 -1) (end 1 -1) (stroke (width 0.1) (type solid)) (layer "F.SilkS"))
  (pad "1" smd rect (at -0.325 0) (size 0.343 1.2) (layers "F.Cu" "F.Mask"))
  (pad "2" smd rect (at 0.325 0) (size 0.343 1.2) (layers "F.Cu" "F.Mask"))
)"""


def test_geometry_measures_pad_width_pitch_and_safe_fanout(tmp_path: Path) -> None:
    library = tmp_path / "copperbrain-libs" / "CB.pretty"
    library.mkdir(parents=True)
    footprint = library / "FinePitch.kicad_mod"
    footprint.write_text(FOOTPRINT, encoding="utf-8")
    (tmp_path / "fp-lib-table").write_text(
        '(fp_lib_table (lib (name "CB")(type "KiCad")'
        '(uri "${KIPRJMOD}/copperbrain-libs/CB.pretty")))',
        encoding="utf-8",
    )
    geometry, candidate = analyze_component_footprint(
        tmp_path,
        reference="U1",
        library_id="CB:FinePitch",
        width_ratio=0.8,
    )
    assert geometry is not None
    assert geometry.pad_min_dimension_mm == pytest.approx(0.343)
    assert geometry.min_pitch_mm == pytest.approx(0.65)
    assert geometry.pad_min_clearance_mm == pytest.approx(0.307)
    assert candidate.safe_fanout_width_mm == 0.27
    assert candidate.safe_clearance_mm == 0.3
    assert not candidate.has_courtyard


def test_generated_courtyard_is_atomic_and_idempotent(tmp_path: Path) -> None:
    footprint = tmp_path / "FinePitch.kicad_mod"
    footprint.write_text(FOOTPRINT, encoding="utf-8")
    addition = CourtyardAddition(
        footprint="CB:FinePitch",
        min_x_mm=-1.25,
        min_y_mm=-1.25,
        max_x_mm=1.25,
        max_y_mm=1.25,
    )
    add_generated_courtyard(footprint, addition)
    first = footprint.read_bytes()
    add_generated_courtyard(footprint, addition)
    assert footprint.read_bytes() == first
    geometry = parse_footprint_geometry(footprint, reference="U1", library_id="CB:FinePitch")
    assert geometry.has_courtyard
    assert '(layer "F.CrtYd")' in footprint.read_text(encoding="utf-8")
