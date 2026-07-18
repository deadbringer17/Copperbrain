"""BOM normalization, component-only cost estimates, and deterministic exports."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path

from copperbrain.models import BomLine, ComponentCandidate, CostEstimate, ProjectSummary
from copperbrain.services.sourcing import unit_price_at


def generate_bom(summary: ProjectSummary) -> tuple[BomLine, ...]:
    """Group physical components by value, footprint, and sourcing identifiers."""
    groups: dict[tuple[str, str | None, str | None, str | None], list[str]] = defaultdict(list)
    for component in summary.components:
        if component.reference.startswith("#"):
            continue
        key = (
            component.value,
            component.footprint,
            component.properties.get("LCSC") or None,
            component.properties.get("MPN") or None,
        )
        groups[key].append(component.reference)
    return tuple(
        BomLine(
            references=tuple(sorted(references)),
            quantity_per_board=len(references),
            value=value,
            footprint=footprint,
            lcsc=lcsc,
            mpn=mpn,
        )
        for (value, footprint, lcsc, mpn), references in sorted(
            groups.items(), key=lambda item: min(item[1])
        )
    )


def enrich_bom(
    lines: tuple[BomLine, ...],
    candidates: dict[str, ComponentCandidate],
    quantities: tuple[int, ...],
) -> tuple[BomLine, ...]:
    """Attach catalog evidence without guessing identifiers or prices."""
    enriched: list[BomLine] = []
    for line in lines:
        candidate = candidates.get(line.lcsc or "")
        if candidate is None:
            enriched.append(line)
            continue
        prices = {
            quantity: price
            for quantity in quantities
            if (price := unit_price_at(candidate.price_breaks, line.quantity_per_board * quantity))
            is not None
        }
        enriched.append(
            line.model_copy(
                update={
                    "mpn": line.mpn or candidate.mpn,
                    "basic_extended": candidate.basic_extended,
                    "unit_prices": prices,
                    "stock": candidate.stock,
                    "price_timestamp": candidate.retrieved_at,
                }
            )
        )
    return tuple(enriched)


def estimate_bom_cost(lines: tuple[BomLine, ...], quantity: int) -> CostEstimate:
    """Calculate only component cost and surface every missing assumption."""
    total = 0.0
    missing: list[str] = []
    insufficient: list[str] = []
    for line in lines:
        identifier = line.lcsc or ",".join(line.references)
        unit_price = line.unit_prices.get(quantity)
        if unit_price is None:
            missing.append(identifier)
            continue
        needed = line.quantity_per_board * quantity
        total += unit_price * needed
        if line.stock is not None and line.stock < needed:
            insufficient.append(identifier)
    return CostEstimate(
        quantity=quantity,
        currency="USD",
        component_cost=round(total, 6),
        missing_prices=tuple(missing),
        insufficient_stock=tuple(insufficient),
        assumptions=("Unit prices use the best recorded break at the required component quantity",),
    )


def render_bom(lines: tuple[BomLine, ...], output_format: str) -> str:
    """Render normalized BOM data as JSON, CSV, or a concise Markdown report."""
    if output_format == "json":
        return json.dumps([line.model_dump(mode="json") for line in lines], indent=2) + "\n"
    headers = [
        "References",
        "Qty/board",
        "Value",
        "Footprint",
        "LCSC",
        "MPN",
        "Category",
        "Stock",
        "Price timestamp",
    ]
    rows = [
        [
            ",".join(line.references),
            str(line.quantity_per_board),
            line.value,
            line.footprint or "",
            line.lcsc or "",
            line.mpn or "",
            line.basic_extended,
            "" if line.stock is None else str(line.stock),
            "" if line.price_timestamp is None else line.price_timestamp.isoformat(),
        ]
        for line in lines
    ]
    if output_format == "csv":
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)
        return stream.getvalue()
    if output_format == "markdown":
        output = ["# Copperbrain BOM", "", "| " + " | ".join(headers) + " |"]
        output.append("| " + " | ".join("---" for _ in headers) + " |")
        output.extend(
            "| " + " | ".join(value.replace("|", "\\|") for value in row) + " |" for row in rows
        )
        output.extend(
            [
                "",
                (
                    "> Component prices only; PCB, assembly, stencil, shipping, taxes, "
                    "and duties are excluded."
                ),
                "",
            ]
        )
        return "\n".join(output)
    raise ValueError("output_format must be json, csv, or markdown")


def export_bom(lines: tuple[BomLine, ...], destination: Path, output_format: str) -> Path:
    """Atomically export a BOM after validating its requested format and suffix."""
    expected = {"json": ".json", "csv": ".csv", "markdown": ".md"}
    if output_format not in expected or destination.suffix.casefold() != expected[output_format]:
        raise ValueError("BOM destination suffix does not match output format")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(render_bom(lines, output_format))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination
