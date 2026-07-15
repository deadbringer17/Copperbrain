from pathlib import Path

import pytest

from copperbrain.adapters.library_tables import ensure_library_entry, validate_nickname
from copperbrain.errors import CopperbrainError


def test_validate_nickname() -> None:
    assert validate_nickname("Copperbrain_C1") == "Copperbrain_C1"
    with pytest.raises(CopperbrainError, match="Invalid"):
        validate_nickname("bad nickname")


def test_ensure_library_entry_is_idempotent(tmp_path: Path) -> None:
    table = tmp_path / "sym-lib-table"
    assert ensure_library_entry(
        table, table_kind="sym_lib_table", nickname="CB", uri="${KIPRJMOD}/CB.kicad_sym"
    )
    original = table.read_bytes()
    assert not ensure_library_entry(
        table, table_kind="sym_lib_table", nickname="CB", uri="${KIPRJMOD}/CB.kicad_sym"
    )
    assert table.read_bytes() == original


def test_ensure_library_entry_rejects_invalid_existing_table(tmp_path: Path) -> None:
    table = tmp_path / "fp-lib-table"
    table.write_text("(broken", encoding="utf-8")
    with pytest.raises(CopperbrainError, match="invalid"):
        ensure_library_entry(table, table_kind="fp_lib_table", nickname="CB", uri="x")
