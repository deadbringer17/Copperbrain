"""Isolated component-catalog adapters; no dependency on plugin source layout."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from platformdirs import user_documents_path
from pydantic import HttpUrl, TypeAdapter

from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    AssetAvailability,
    ComponentCandidate,
    ErrorCode,
    PriceBreak,
)


class CatalogAdapter(Protocol):
    """Minimal normalized interface implemented by JLC integrations."""

    @property
    def source_name(self) -> str: ...

    def search(self, query: str) -> tuple[ComponentCandidate, ...]: ...

    def details(self, lcsc: str) -> ComponentCandidate: ...


class JsonCatalogAdapter:
    """Deterministic recorded-response adapter used offline and in tests."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def source_name(self) -> str:
        return f"recorded:{self.path.name}"

    def _load(self) -> tuple[ComponentCandidate, ...]:
        payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        return tuple(TypeAdapter(list[ComponentCandidate]).validate_python(payload))

    def search(self, query: str) -> tuple[ComponentCandidate, ...]:
        terms = query.casefold().split()
        return tuple(
            item
            for item in self._load()
            if all(
                term
                in " ".join(
                    (item.lcsc, item.mpn, item.manufacturer, item.description, item.package)
                ).casefold()
                for term in terms
            )
        )

    def details(self, lcsc: str) -> ComponentCandidate:
        match = next(
            (item for item in self._load() if item.lcsc.casefold() == lcsc.casefold()), None
        )
        if match is None:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "Component was not found in the catalog",
                details={"lcsc": lcsc},
            )
        return match


class UnavailableCatalogAdapter:
    """Actionable safe fallback when no supported JLC module is configured."""

    @property
    def source_name(self) -> str:
        return "unavailable"

    def _raise(self) -> None:
        raise CopperbrainError(
            ErrorCode.INTEGRATION_UNAVAILABLE,
            "No supported JLC component catalog is available",
            actionable_hint=(
                "Install JLCImport/JLCPCB Tools or set COPPERBRAIN_JLC_CATALOG to a "
                "normalized recorded response."
            ),
        )

    def search(self, query: str) -> tuple[ComponentCandidate, ...]:
        self._raise()
        return ()

    def details(self, lcsc: str) -> ComponentCandidate:
        self._raise()
        raise AssertionError("unreachable")


def parse_jlc_price_breaks(value: str) -> tuple[PriceBreak, ...]:
    """Normalize JLCPCB Tools' compact `start-end:price` representation."""
    breaks: list[PriceBreak] = []
    for entry in value.split(","):
        match = re.fullmatch(r"\s*(\d+)(?:-\d*)?\s*:\s*(\d+(?:\.\d+)?)\s*", entry)
        if match:
            breaks.append(
                PriceBreak(quantity=int(match.group(1)), unit_price=float(match.group(2)))
            )
    return tuple(
        sorted({item.quantity: item for item in breaks}.values(), key=lambda item: item.quantity)
    )


class JlcpcbToolsDatabaseAdapter:
    """Read-only adapter for the installed JLCPCB Tools FTS5 parts database."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        if not self.path.is_file():
            raise CopperbrainError(ErrorCode.NOT_FOUND, "JLCPCB Tools database was not found")

    @property
    def source_name(self) -> str:
        return f"JLCPCB Tools:{self.path.name}"

    def _timestamp(self) -> datetime:
        return datetime.fromtimestamp(self.path.stat().st_mtime, UTC)

    def _normalize(self, row: sqlite3.Row) -> ComponentCandidate:
        datasheet = str(row["Datasheet"] or "").strip() or None
        category = str(row["Library Type"] or "unknown").casefold()
        normalized_category = category if category in {"basic", "extended"} else "unknown"
        return ComponentCandidate(
            lcsc=str(row["LCSC Part"]),
            mpn=str(row["MFR.Part"] or ""),
            manufacturer=str(row["Manufacturer"] or ""),
            description=str(row["Description"] or ""),
            package=str(row["Package"] or ""),
            basic_extended=normalized_category,  # type: ignore[arg-type]
            stock=max(0, int(row["Stock"] or 0)),
            price_breaks=parse_jlc_price_breaks(str(row["Price"] or "")),
            datasheet_url=HttpUrl(datasheet) if datasheet else None,
            asset_availability=AssetAvailability(datasheet=bool(datasheet)),
            source=self.source_name,
            retrieved_at=self._timestamp(),
        )

    def search(self, query: str) -> tuple[ComponentCandidate, ...]:
        terms = re.findall(r"[\w.+-]+", query, flags=re.UNICODE)
        if not terms:
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "Component search query is empty")
        expression = " AND ".join(f'"{term.replace(chr(34), "")}"' for term in terms)
        try:
            with closing(
                sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
            ) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT * FROM parts WHERE parts MATCH ? LIMIT 100", (expression,)
                ).fetchall()
        except sqlite3.Error as exc:
            raise CopperbrainError(
                ErrorCode.INTEGRATION_UNAVAILABLE,
                "JLCPCB Tools database cannot be queried",
                details={"reason": str(exc)},
            ) from exc
        return tuple(self._normalize(row) for row in rows)

    def details(self, lcsc: str) -> ComponentCandidate:
        try:
            with closing(
                sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
            ) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    'SELECT * FROM parts WHERE "LCSC Part" = ? LIMIT 1', (lcsc,)
                ).fetchone()
        except sqlite3.Error as exc:
            raise CopperbrainError(
                ErrorCode.INTEGRATION_UNAVAILABLE,
                "JLCPCB Tools database cannot be queried",
                details={"reason": str(exc)},
            ) from exc
        if row is None:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND, "Component was not found", details={"lcsc": lcsc}
            )
        return self._normalize(row)


def discover_jlcpcb_tools_database() -> Path | None:
    """Find the newest standard KiCad PCM database without fixed user/version paths."""
    root = user_documents_path() / "KiCad"
    if not root.is_dir():
        return None
    candidates = tuple(root.glob("*/3rdparty/plugins/*/jlcpcb/current-parts-fts5.db"))
    files = [path for path in candidates if path.is_file()]
    return max(files, key=lambda path: path.stat().st_mtime) if files else None


def configured_catalog() -> CatalogAdapter:
    """Select an explicit recorded catalog without inspecting plugin internals."""
    configured = os.getenv("COPPERBRAIN_JLC_CATALOG")
    if configured and Path(configured).is_file():
        return JsonCatalogAdapter(Path(configured))
    if database := discover_jlcpcb_tools_database():
        return JlcpcbToolsDatabaseAdapter(database)
    return UnavailableCatalogAdapter()
