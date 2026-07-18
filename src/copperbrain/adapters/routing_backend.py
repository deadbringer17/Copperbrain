"""Backend-neutral contract for isolated PCB autorouting engines."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from copperbrain.models import RoutingBackendStatus, RoutingPassMetric, RoutingRequest

RoutingStrategy = Literal["mps", "inside_out", "original"]
ROUTING_STRATEGIES: tuple[RoutingStrategy, ...] = ("mps", "inside_out", "original")


@dataclass(frozen=True)
class RoutedBoardCandidate:
    """One isolated board returned by an external routing backend."""

    strategy: RoutingStrategy
    pcb: Path
    elapsed_seconds: float
    stdout_tail: str = ""
    stderr_tail: str = ""
    pass_metrics: tuple[RoutingPassMetric, ...] = ()
    normalization_count: int = 0
    watchdog_reason: str | None = None


class RoutingBackend(Protocol):
    """Fixed-command backend used only against Copperbrain private workspaces."""

    def status(self) -> RoutingBackendStatus: ...

    def strategies(self, request: RoutingRequest) -> tuple[RoutingStrategy, ...]: ...

    def refill_zones(self, pcb: Path) -> None: ...

    def route(
        self,
        pcb: Path,
        workspace: Path,
        request: RoutingRequest,
        strategy: RoutingStrategy,
    ) -> RoutedBoardCandidate: ...
