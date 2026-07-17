"""Private, bounded persistence for reusable connectivity and routing metrics."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ConnectivityMetricRecord,
    ConnectivityMetricRunSummary,
    ErrorCode,
    RoutingBatchComparison,
)


def _observed_open_improvement(record: ConnectivityMetricRecord) -> int:
    if record.open_connection_delta is not None:
        return max(0, record.open_connection_delta)
    observed = tuple(
        value
        for metric in record.freerouting_pass_metrics
        if (
            value := (
                metric.board_unrouted_count
                if metric.board_unrouted_count is not None
                else metric.board_incomplete_count
            )
        )
        is not None
    )
    return max(0, observed[0] - min(observed)) if observed else 0


def _copper_rate(record: ConnectivityMetricRecord) -> float:
    if record.copper_produced_per_second > 0:
        return record.copper_produced_per_second
    return (
        round(record.routed_length_mm / record.duration_seconds, 6)
        if record.routed_length_mm > 0 and record.duration_seconds > 0
        else 0
    )


def _connections_per_pass(record: ConnectivityMetricRecord) -> float:
    if record.connections_resolved_per_pass > 0:
        return record.connections_resolved_per_pass
    return round(
        _observed_open_improvement(record)
        / max(1, record.best_pass_number or len(record.freerouting_pass_metrics)),
        6,
    )


class ConnectivityMetricsStore:
    """Write one typed JSON record per routing phase below the private data root."""

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "metrics" / "connectivity"

    def write(self, record: ConnectivityMetricRecord) -> Path:
        day = record.started_at.strftime("%Y-%m-%d")
        directory = self.root / day / record.run_id
        directory.mkdir(parents=True, exist_ok=True)
        suffix = f"-{record.strategy}" if record.strategy is not None else ""
        path = directory / f"{record.phase}{suffix}.json"
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(record.model_dump_json(indent=2))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return path

    def read_run(self, run_id: str) -> ConnectivityMetricRunSummary:
        """Return one bounded, sanitized optimization view without accepting a filesystem path."""
        if re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT, "Connectivity metrics run ID is invalid"
            )
        paths = sorted(self.root.glob(f"*/{run_id}/*.json"))
        if not paths:
            raise CopperbrainError(ErrorCode.NOT_FOUND, "Connectivity metrics run was not found")
        if len(paths) > 32:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Connectivity metrics run contains too many phase records",
            )
        records: list[ConnectivityMetricRecord] = []
        for path in paths:
            try:
                records.append(
                    ConnectivityMetricRecord.model_validate_json(path.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError) as exc:
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "Connectivity metrics record is invalid",
                    details={"record": path.name, "reason": str(exc)},
                ) from exc
        child_paths = sorted(self.root.glob("*/*/*.json"), reverse=True)[:500]
        for path in child_paths:
            if path in paths:
                continue
            try:
                child = ConnectivityMetricRecord.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue
            if child.parent_run_id == run_id:
                records.append(child)
                if len(records) >= 64:
                    break
        candidates = tuple(
            item
            for item in records
            if item.phase == "candidate"
            and item.outcome == "success"
            and item.final_open_connection_count is not None
        )
        best = min(
            candidates,
            key=lambda item: (
                item.final_open_connection_count or 0,
                item.new_drc_error_count if item.new_drc_error_count is not None else 1_000_000,
                item.via_count,
                item.routed_length_mm,
                item.strategy or "",
            ),
            default=None,
        )
        all_candidates = tuple(item for item in records if item.phase == "candidate")
        observed_passes = tuple(
            item.best_pass_number for item in all_candidates if item.best_pass_number is not None
        )
        baseline = next((item for item in records if item.phase == "baseline"), None)
        corpus: list[ConnectivityMetricRecord] = []
        if baseline is not None:
            for path in child_paths:
                try:
                    item = ConnectivityMetricRecord.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    continue
                if item.phase == "candidate" and item.project_fingerprint == (
                    baseline.project_fingerprint
                ):
                    corpus.append(item)
        improving = tuple(
            item
            for item in corpus
            if item.best_pass_number is not None and _observed_open_improvement(item) > 0
        )
        recommended_max_passes = (
            min(200, max(2, max(item.best_pass_number or 1 for item in improving) + 1))
            if improving
            else None
        )
        batches: list[RoutingBatchComparison] = []
        for candidate in sorted(corpus, key=lambda item: item.finished_at, reverse=True):
            if any(item.run_id == candidate.run_id for item in batches):
                continue
            batches.append(
                RoutingBatchComparison(
                    run_id=candidate.run_id,
                    requested_net_count=candidate.requested_net_count,
                    requested_net_role_counts=candidate.requested_net_role_counts,
                    best_open_connection_delta=candidate.open_connection_delta,
                    best_pass_number=candidate.best_pass_number,
                    duration_seconds=candidate.duration_seconds,
                    copper_produced_per_second=_copper_rate(candidate),
                    connections_resolved_per_pass=_connections_per_pass(candidate),
                )
            )
            if len(batches) == 12:
                break
        return ConnectivityMetricRunSummary(
            run_id=run_id,
            record_count=len(records),
            records=tuple(records),
            best_strategy=best.strategy if best is not None else None,
            best_open_connection_delta=best.open_connection_delta if best is not None else None,
            comparable_candidate_count=len(candidates),
            failed_candidate_count=sum(item.outcome == "failure" for item in all_candidates),
            best_observed_pass_number=min(observed_passes, default=None),
            highest_stagnation_count=max(
                (item.stagnation_count for item in all_candidates), default=0
            ),
            watchdog_reasons=tuple(
                sorted(
                    {
                        item.watchdog_reason
                        for item in all_candidates
                        if item.watchdog_reason is not None
                    }
                )
            ),
            recommended_max_passes=recommended_max_passes,
            same_baseline_batches=tuple(batches),
        )

    def recommended_max_passes(self, project_fingerprint: str) -> int | None:
        """Suggest a bounded pass budget from prior improving runs on the same board."""
        best_passes: list[int] = []
        for path in sorted(self.root.glob("*/*/candidate-*.json"), reverse=True)[:500]:
            try:
                item = ConnectivityMetricRecord.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue
            if (
                item.project_fingerprint == project_fingerprint
                and item.best_pass_number is not None
                and _observed_open_improvement(item) > 0
            ):
                best_passes.append(item.best_pass_number)
        return min(200, max(2, max(best_passes) + 1)) if best_passes else None
