import subprocess
from pathlib import Path

import pytest

from copperbrain.adapters import kicad_cli
from copperbrain.errors import CopperbrainError

NETLIST = """<?xml version="1.0"?><export><components>
<comp ref="R1"><value>10k</value><footprint>Resistor:R_0603</footprint>
<fields><field name="LCSC">C25804</field></fields>
<libsource lib="Device" part="R"/></comp></components><nets>
<net code="1" name="VCC"><node ref="R1" pin="1" pinfunction="~"/></net>
</nets></export>"""


def test_parse_kicad_xml_netlist() -> None:
    components, nets = kicad_cli.parse_kicad_xml_netlist(NETLIST)
    assert components[0].reference == "R1"
    assert components[0].lib_id == "Device:R"
    assert components[0].properties["LCSC"] == "C25804"
    assert nets[0].pins[0].reference == "R1"


def test_parse_erc_json() -> None:
    payload = {
        "sheets": [
            {
                "violations": [
                    {
                        "severity": "warning",
                        "type": "x",
                        "description": "bad",
                        "items": [{"description": "R1 pin 1"}],
                    }
                ]
            }
        ]
    }
    result = kicad_cli.parse_erc_json(payload)
    assert result[0].severity == "warning"
    assert result[0].items == ("R1 pin 1",)


def test_parse_drc_json() -> None:
    payload = {
        "violations": [
            {
                "severity": "error",
                "type": "clearance",
                "description": "too close",
                "items": [{"description": "Track on /+5V"}],
            }
        ]
    }
    result = kicad_cli.parse_drc_json(payload)
    assert result[0].code == "clearance"
    assert result[0].items == ("Track on /+5V",)


def test_run_drc_reports_missing_board(tmp_path: Path) -> None:
    report = kicad_cli.run_drc(None, tmp_path / "missing.kicad_pcb")
    assert not report.available
    assert report.error is not None


def test_validate_footprint_uses_temporary_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cli = tmp_path / "kicad-cli.exe"
    cli.write_bytes(b"")
    footprint = tmp_path / "part.kicad_mod"
    footprint.write_text('(footprint "Part")', encoding="utf-8")

    def run(command: list[str], *, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        output = Path(command[command.index("--output") + 1])
        output.mkdir()
        (output / footprint.name).write_text(footprint.read_text(), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(kicad_cli, "_run", run)
    report = kicad_cli.validate_footprint(cli, footprint)
    assert report.valid
    assert report.checks["kicad_footprint_parse"]


def test_run_erc_reports_missing_cli(tmp_path: Path) -> None:
    report = kicad_cli.run_erc(None, tmp_path / "x.kicad_sch")
    assert not report.available
    assert report.error is not None


def test_export_schematic_pdf_is_atomic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cli = tmp_path / "kicad-cli.exe"
    cli.write_bytes(b"")
    schematic = tmp_path / "demo.kicad_sch"
    schematic.write_text("fixture", encoding="utf-8")
    destination = tmp_path / "out" / "preview.pdf"

    def run(command: list[str], *, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        Path(command[command.index("--output") + 1]).write_bytes(b"pdf")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(kicad_cli, "_run", run)
    assert kicad_cli.export_schematic_pdf(cli, schematic, destination) == destination
    assert destination.read_bytes() == b"pdf"


@pytest.mark.parametrize("existing", [False, True])
def test_read_only_cli_restores_incidental_project_local_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, existing: bool
) -> None:
    cli = tmp_path / "kicad-cli.exe"
    cli.write_bytes(b"")
    schematic = tmp_path / "demo.kicad_sch"
    schematic.write_text("fixture", encoding="utf-8")
    local_state = schematic.with_suffix(".kicad_prl")
    if existing:
        local_state.write_bytes(b"user-state")

    def run(command: list[str], *, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        local_state.write_bytes(b"incidental-cli-state")
        Path(command[command.index("--output") + 1]).write_bytes(b"pdf")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(kicad_cli, "_run", run)
    kicad_cli.export_schematic_pdf(cli, schematic, tmp_path / "preview.pdf")

    if existing:
        assert local_state.read_bytes() == b"user-state"
    else:
        assert not local_state.exists()


def test_export_schematic_pdf_requires_cli(tmp_path: Path) -> None:
    with pytest.raises(CopperbrainError, match="unavailable"):
        kicad_cli.export_schematic_pdf(None, tmp_path / "x.kicad_sch", tmp_path / "x.pdf")


def test_export_pcb_pdf_is_atomic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cli = tmp_path / "kicad-cli.exe"
    cli.write_bytes(b"")
    pcb = tmp_path / "demo.kicad_pcb"
    pcb.write_text("fixture", encoding="utf-8")
    destination = tmp_path / "out" / "preview.pdf"

    def run(command: list[str], *, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        assert command[1:4] == ["pcb", "export", "pdf"]
        assert command[command.index("--layers") + 1].endswith("Edge.Cuts")
        Path(command[command.index("--output") + 1]).write_bytes(b"pcb-pdf")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(kicad_cli, "_run", run)
    assert kicad_cli.export_pcb_pdf(cli, pcb, destination) == destination
    assert destination.read_bytes() == b"pcb-pdf"


def test_export_pcb_pdf_requires_cli(tmp_path: Path) -> None:
    with pytest.raises(CopperbrainError, match="unavailable"):
        kicad_cli.export_pcb_pdf(None, tmp_path / "x.kicad_pcb", tmp_path / "x.pdf")
