from pathlib import Path

from copperbrain.models import AssetAvailability, ComponentCandidate, PriceBreak, RequirementSet
from copperbrain.services.sourcing import (
    CatalogCache,
    SourcingService,
    rank_candidates,
    unit_price_at,
)


def candidate(
    lcsc: str, *, basic: str = "basic", stock: int = 100, price: float = 0.2
) -> ComponentCandidate:
    return ComponentCandidate(
        lcsc=lcsc,
        mpn=f"MPN-{lcsc}",
        manufacturer="Acme",
        description="buck converter",
        package="SOT-23",
        basic_extended=basic,  # type: ignore[arg-type]
        stock=stock,
        price_breaks=(PriceBreak(quantity=1, unit_price=price),),
        asset_availability=AssetAvailability(symbol=True, footprint=True),
    )


def test_unit_price_at_respects_moq_and_breaks() -> None:
    breaks = (PriceBreak(quantity=10, unit_price=1), PriceBreak(quantity=100, unit_price=0.5))
    assert unit_price_at(breaks, 1) is None
    assert unit_price_at(breaks, 99) == 1
    assert unit_price_at(breaks, 100) == 0.5


def test_unit_price_at_handles_unsorted_breaks() -> None:
    breaks = (
        PriceBreak(quantity=100, unit_price=0.5),
        PriceBreak(quantity=10, unit_price=0.8),
        PriceBreak(quantity=1, unit_price=1.0),
    )
    assert unit_price_at(breaks, 50) == 0.8
    assert unit_price_at(breaks, 100) == 0.5
    assert unit_price_at(breaks, 5) == 1.0


def test_rank_filters_and_is_deterministic() -> None:
    requirements = RequirementSet(
        sourcing={"prefer_basic": True, "min_stock": 10, "category": "basic"}
    )
    result = rank_candidates(
        (candidate("C2"), candidate("C1"), candidate("C3", basic="extended")),
        requirements,
        quantity=10,
    )
    assert [item.lcsc for item in result] == ["C1", "C2"]
    assert "Basic" in result[0].evidence[0]


class FakeAdapter:
    source_name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def search(self, query: str) -> tuple[ComponentCandidate, ...]:
        self.calls += 1
        return (candidate("C1"),)

    def details(self, lcsc: str) -> ComponentCandidate:
        return candidate(lcsc)


def test_service_caches_search_and_compares(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    service = SourcingService(adapter, CatalogCache(tmp_path / "cache.sqlite"))
    requirements = RequirementSet()
    assert service.search("buck", requirements, quantity=10)[0].lcsc == "C1"
    assert service.search("buck", requirements, quantity=10)[0].lcsc == "C1"
    assert adapter.calls == 1
    assert service.details("C2").lcsc == "C2"
    assert service.compare((candidate("C1"),), requirements, quantity=10)[0]["stock"] == 100
