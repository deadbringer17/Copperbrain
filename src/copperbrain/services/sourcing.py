"""Deterministic component filtering, ranking, comparison, and price cache."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from copperbrain.adapters.jlc_catalog import CatalogAdapter
from copperbrain.models import ComponentCandidate, PriceBreak, RequirementSet


def unit_price_at(price_breaks: tuple[PriceBreak, ...], quantity: int) -> float | None:
    """Return the best applicable unit price without extrapolating below MOQ."""
    applicable = [item for item in price_breaks if item.quantity <= quantity]
    if not applicable:
        return None
    return max(applicable, key=lambda item: item.quantity).unit_price


def estimate_component_cost(candidate: ComponentCandidate, quantity: int) -> dict[str, object]:
    """Estimate one component line while retaining stock and pricing evidence."""
    price = unit_price_at(candidate.price_breaks, quantity)
    return {
        "lcsc": candidate.lcsc,
        "quantity": quantity,
        "currency": candidate.price_breaks[0].currency if candidate.price_breaks else "USD",
        "unit_price": price,
        "component_cost": None if price is None else round(price * quantity, 6),
        "stock_sufficient": candidate.stock >= quantity,
        "stock": candidate.stock,
        "source": candidate.source,
        "retrieved_at": candidate.retrieved_at.isoformat(),
        "assumptions": ("Component price only; fees and manufacturing costs are excluded",),
    }


def candidate_matches(candidate: ComponentCandidate, requirements: RequirementSet) -> bool:
    """Apply hard sourcing and mechanical constraints deterministically."""
    sourcing = requirements.sourcing
    mechanical = requirements.mechanical
    if int(sourcing.get("min_stock", 0)) > candidate.stock:
        return False
    category = str(sourcing.get("category", "any")).casefold()
    if category != "any" and candidate.basic_extended != category:
        return False
    package = str(mechanical.get("package", "")).casefold()
    if package and package not in candidate.package.casefold():
        return False
    manufacturer = str(sourcing.get("manufacturer", "")).casefold()
    return not manufacturer or manufacturer in candidate.manufacturer.casefold()


def score_candidate(
    candidate: ComponentCandidate,
    requirements: RequirementSet,
    *,
    quantity: int,
) -> tuple[float, tuple[str, ...]]:
    """Score only explicit, explainable commercial and asset preferences."""
    score = 0.0
    evidence: list[str] = []
    if requirements.sourcing.get("prefer_basic", False) and candidate.basic_extended == "basic":
        score += 30
        evidence.append("JLCPCB Basic preference matched")
    if candidate.stock >= quantity:
        score += 20
        evidence.append(f"stock covers {quantity} units")
    available_assets = sum(candidate.asset_availability.model_dump().values())
    score += available_assets * 5
    if available_assets:
        evidence.append(f"{available_assets}/4 KiCad/datasheet assets available")
    price = unit_price_at(candidate.price_breaks, quantity)
    if price is not None:
        score += max(0, 20 - min(price, 20))
        evidence.append(f"unit price at {quantity}: {price:.6f} USD")
    return round(score, 6), tuple(evidence)


def rank_candidates(
    candidates: tuple[ComponentCandidate, ...],
    requirements: RequirementSet,
    *,
    quantity: int,
    limit: int = 5,
) -> tuple[ComponentCandidate, ...]:
    """Filter, score, and sort with stable tie breakers; return at most five."""
    bounded_limit = max(1, min(limit, 5))
    scored: list[ComponentCandidate] = []
    for candidate in candidates:
        if candidate_matches(candidate, requirements):
            score, evidence = score_candidate(candidate, requirements, quantity=quantity)
            scored.append(candidate.model_copy(update={"score": score, "evidence": evidence}))
    return tuple(sorted(scored, key=lambda item: (-item.score, item.lcsc))[:bounded_limit])


class CatalogCache:
    """Small SQLite cache that preserves source and retrieval timestamp."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS search_cache "
                "(cache_key TEXT PRIMARY KEY, source TEXT NOT NULL, retrieved_at TEXT NOT NULL, "
                "payload TEXT NOT NULL)"
            )

    def put(self, key: str, source: str, candidates: tuple[ComponentCandidate, ...]) -> None:
        payload = json.dumps([item.model_dump(mode="json") for item in candidates])
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO search_cache VALUES (?, ?, ?, ?)",
                (key, source, datetime.now(UTC).isoformat(), payload),
            )

    def get(self, key: str) -> tuple[ComponentCandidate, ...] | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload FROM search_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return tuple(ComponentCandidate.model_validate(item) for item in json.loads(row[0]))


class SourcingService:
    def __init__(self, adapter: CatalogAdapter, cache: CatalogCache) -> None:
        self.adapter = adapter
        self.cache = cache

    def search(
        self,
        query: str,
        requirements: RequirementSet,
        *,
        quantity: int,
        limit: int = 5,
        refresh: bool = False,
    ) -> tuple[ComponentCandidate, ...]:
        """Search through the adapter, cache raw evidence, then deterministically rank."""
        key = query.strip().casefold()
        candidates = None if refresh else self.cache.get(key)
        if candidates is None:
            candidates = self.adapter.search(query)
            self.cache.put(key, self.adapter.source_name, candidates)
        return rank_candidates(candidates, requirements, quantity=quantity, limit=limit)

    def details(self, lcsc: str) -> ComponentCandidate:
        return self.adapter.details(lcsc)

    def alternatives(
        self,
        lcsc: str,
        requirements: RequirementSet,
        *,
        quantity: int,
    ) -> tuple[ComponentCandidate, ...]:
        """Search by the source part description and exclude that exact LCSC id."""
        source = self.details(lcsc)
        matches = self.search(source.description, requirements, quantity=quantity, refresh=True)
        return tuple(item for item in matches if item.lcsc != source.lcsc)

    def compare(
        self,
        candidates: tuple[ComponentCandidate, ...],
        requirements: RequirementSet,
        *,
        quantity: int,
    ) -> tuple[dict[str, object], ...]:
        """Build a compact requirement/candidate matrix for at most five candidates."""
        ranked = rank_candidates(candidates[:5], requirements, quantity=quantity)
        return tuple(
            {
                "lcsc": item.lcsc,
                "score": item.score,
                "matches_hard_constraints": candidate_matches(item, requirements),
                "unit_price": unit_price_at(item.price_breaks, quantity),
                "stock": item.stock,
                "evidence": item.evidence,
            }
            for item in ranked
        )
