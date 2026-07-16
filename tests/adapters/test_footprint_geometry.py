from pathlib import Path

import pytest

from copperbrain.adapters.footprint_geometry import (
    FootprintGeometry,
    PadGeometry,
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


def test_geometry_applies_pad_rotation_to_clearance_bounds(tmp_path: Path) -> None:
    footprint = tmp_path / "Rotated.kicad_mod"
    footprint.write_text(
        """(footprint "Rotated"
  (version 20241229)
  (layer "F.Cu")
  (pad "1" smd rect (at 0 0 90) (size 0.3 0.7) (layers "F.Cu" "F.Mask"))
  (pad "2" smd rect (at 0 0.5 90) (size 0.3 0.7) (layers "F.Cu" "F.Mask"))
)""",
        encoding="utf-8",
    )

    geometry = parse_footprint_geometry(footprint, reference="U1", library_id="CB:Rotated")

    assert geometry.pads[0].width_mm == pytest.approx(0.7)
    assert geometry.pads[0].height_mm == pytest.approx(0.3)
    assert geometry.pad_min_clearance_mm == pytest.approx(0.2)


def test_geometry_uses_custom_pad_primitive_bounds(tmp_path: Path) -> None:
    footprint = tmp_path / "Custom.kicad_mod"
    footprint.write_text(
        """(footprint "Custom"
  (version 20241229)
  (layer "F.Cu")
  (pad "1" smd custom
    (at 0 0)
    (size 0.143934 0.143934)
    (layers "F.Cu" "F.Mask")
    (options (clearance outline) (anchor circle))
    (primitives
      (gr_poly (pts (xy -0.375 -0.075) (xy 0.375 -0.075)
                    (xy 0.375 0.075) (xy -0.375 0.075))
        (width 0.1) (fill yes))))
  (pad "2" smd rect (at 0 0.45) (size 0.85 0.25) (layers "F.Cu" "F.Mask"))
)""",
        encoding="utf-8",
    )

    geometry = parse_footprint_geometry(footprint, reference="U1", library_id="CB:Custom")

    assert geometry.pads[0].width_mm == pytest.approx(0.85)
    assert geometry.pads[0].height_mm == pytest.approx(0.25)
    assert geometry.pad_min_dimension_mm == pytest.approx(0.25)
    assert geometry.pad_min_clearance_mm == pytest.approx(0.2)


def test_geometry_ignores_chamfered_custom_corner_bbox_overlap(tmp_path: Path) -> None:
    geometry = FootprintGeometry(
        reference="U2",
        library_id="CB:QFN",
        source=tmp_path / "qfn.kicad_mod",
        pads=(
            PadGeometry(
                number="1",
                x_mm=-1.45,
                y_mm=-0.9,
                width_mm=0.85,
                height_mm=0.25,
                custom=True,
            ),
            PadGeometry(
                number="20",
                x_mm=-0.9,
                y_mm=-1.45,
                width_mm=0.25,
                height_mm=0.85,
                custom=True,
            ),
            PadGeometry(number="2", x_mm=-1.45, y_mm=-0.45, width_mm=0.85, height_mm=0.25),
        ),
        has_courtyard=True,
        min_x_mm=-1.875,
        min_y_mm=-1.875,
        max_x_mm=-0.775,
        max_y_mm=-0.325,
    )

    assert geometry.pad_min_clearance_mm == pytest.approx(0.2)


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


def test_geometry_ignores_duplicate_primitives_for_the_same_electrical_pad(
    tmp_path: Path,
) -> None:
    geometry = FootprintGeometry(
        reference="Q1",
        library_id="CB:MOSFET",
        source=tmp_path / "mosfet.kicad_mod",
        pads=(
            PadGeometry(number="8", x_mm=0, y_mm=0, width_mm=2, height_mm=2),
            PadGeometry(number="8", x_mm=0.5, y_mm=0, width_mm=2, height_mm=2),
            PadGeometry(number="4", x_mm=3, y_mm=0, width_mm=1, height_mm=1),
        ),
        has_courtyard=True,
        min_x_mm=-1,
        min_y_mm=-1,
        max_x_mm=3.5,
        max_y_mm=1,
    )

    assert geometry.min_pitch_mm == pytest.approx(2.5)
    assert geometry.pad_min_clearance_mm == pytest.approx(1.0)


def test_geometry_allows_overlapping_drain_pads_on_the_same_net(tmp_path: Path) -> None:
    geometry = FootprintGeometry(
        reference="Q1",
        library_id="CB:MOSFET",
        source=tmp_path / "mosfet.kicad_mod",
        pads=(
            PadGeometry(number="8", x_mm=0, y_mm=0, width_mm=4, height_mm=4),
            PadGeometry(number="7", x_mm=0, y_mm=2, width_mm=1, height_mm=1),
            PadGeometry(number="4", x_mm=4, y_mm=0, width_mm=1, height_mm=1),
        ),
        has_courtyard=True,
        min_x_mm=-2,
        min_y_mm=-2,
        max_x_mm=4.5,
        max_y_mm=2.5,
    )

    assert geometry.pad_min_clearance_mm == 0
    assert geometry.pad_min_clearance_for_pin_nets(
        {"8": "/VMOTOR", "7": "/VMOTOR", "4": "/GATE"}
    ) == pytest.approx(1.5)


def test_safe_clearance_tolerates_sub_micron_conversion_rounding(tmp_path: Path) -> None:
    library = tmp_path / "copperbrain-libs" / "CB.pretty"
    library.mkdir(parents=True)
    footprint = library / "Rounded.kicad_mod"
    footprint.write_text(
        """(footprint "Rounded"
  (version 20241229)
  (layer "F.Cu")
  (pad "1" smd rect (at 0 0) (size 0.3 0.7) (layers "F.Cu" "F.Mask"))
  (pad "2" smd rect (at 0.499834 0) (size 0.3 0.7) (layers "F.Cu" "F.Mask"))
)""",
        encoding="utf-8",
    )
    (tmp_path / "fp-lib-table").write_text(
        '(fp_lib_table (lib (name "CB")(type "KiCad")'
        '(uri "${KIPRJMOD}/copperbrain-libs/CB.pretty")))',
        encoding="utf-8",
    )

    _, candidate = analyze_component_footprint(
        tmp_path,
        reference="U1",
        library_id="CB:Rounded",
        width_ratio=0.8,
    )

    assert candidate.safe_clearance_mm == 0.2
