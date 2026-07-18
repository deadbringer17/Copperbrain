import json
import sqlite3
from pathlib import Path

import pytest

from copperbrain.adapters import jlc_catalog
from copperbrain.adapters.jlc_catalog import (
    JlcpcbToolsDatabaseAdapter,
    JsonCatalogAdapter,
    UnavailableCatalogAdapter,
    parse_jlc_price_breaks,
)
from copperbrain.errors import CopperbrainError


def test_json_catalog_search_and_details(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            [
                {
                    "lcsc": "C1",
                    "mpn": "ABC",
                    "manufacturer": "Acme",
                    "description": "buck converter",
                    "package": "SOT-23",
                }
            ]
        ),
        encoding="utf-8",
    )
    adapter = JsonCatalogAdapter(path)
    assert adapter.search("buck")[0].lcsc == "C1"
    assert adapter.details("c1").mpn == "ABC"
    with pytest.raises(CopperbrainError, match="not found"):
        adapter.details("C2")


def test_unavailable_catalog_is_actionable() -> None:
    with pytest.raises(CopperbrainError, match="No supported"):
        UnavailableCatalogAdapter().search("buck")


def make_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE VIRTUAL TABLE parts USING fts5("
            "'LCSC Part', 'First Category', 'Second Category', 'MFR.Part', 'Package', "
            "'Solder Joint', 'Manufacturer', 'Library Type', 'Description', 'Datasheet', "
            "'Price', 'Stock', tokenize='trigram')"
        )
        connection.execute(
            "INSERT INTO parts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "C1",
                "Power",
                "DC-DC",
                "ABC",
                "SOT-23",
                "3",
                "Acme",
                "Basic",
                "buck converter",
                "https://lcsc.com/a.pdf",
                "1-9:0.2,10-:0.1",
                "1000",
            ),
        )


def test_parse_price_breaks_and_database_adapter(tmp_path: Path) -> None:
    assert [item.quantity for item in parse_jlc_price_breaks("1-9:0.2,10-:0.1,bad")] == [1, 10]
    database = tmp_path / "parts.db"
    make_database(database)
    adapter = JlcpcbToolsDatabaseAdapter(database)
    result = adapter.search("buck converter")
    assert result[0].lcsc == "C1"
    assert result[0].basic_extended == "basic"
    assert result[0].price_breaks[1].unit_price == 0.1
    assert adapter.details("C1").stock == 1000
    with pytest.raises(CopperbrainError, match="not found"):
        adapter.details("C2")


def test_database_adapter_wraps_sqlite_errors(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a sqlite database")
    adapter = JlcpcbToolsDatabaseAdapter(corrupt)
    with pytest.raises(CopperbrainError, match="cannot be queried"):
        adapter.search("buck")
    with pytest.raises(CopperbrainError, match="cannot be queried"):
        adapter.details("C1")


def test_configured_catalog_prefers_recording_then_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recording = tmp_path / "catalog.json"
    recording.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("COPPERBRAIN_JLC_CATALOG", str(recording))
    assert isinstance(jlc_catalog.configured_catalog(), JsonCatalogAdapter)
    monkeypatch.delenv("COPPERBRAIN_JLC_CATALOG")
    database = tmp_path / "parts.db"
    make_database(database)
    monkeypatch.setattr(jlc_catalog, "discover_jlcpcb_tools_database", lambda: database)
    assert isinstance(jlc_catalog.configured_catalog(), JlcpcbToolsDatabaseAdapter)
