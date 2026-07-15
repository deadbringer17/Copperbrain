"""Transport-independent domain and boundary models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class FrozenModel(BaseModel):
    """Immutable base model used by domain values."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INTEGRATION_UNAVAILABLE = "integration_unavailable"
    VALIDATION_FAILED = "validation_failed"
    CONFIRMATION_REQUIRED = "confirmation_required"
    UNSAFE_EDITOR_STATE = "unsafe_editor_state"
    NETWORK_ERROR = "network_error"
    INTERNAL_ERROR = "internal_error"


class StructuredError(FrozenModel):
    code: ErrorCode
    message: str
    actionable_hint: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class IntegrationStatus(FrozenModel):
    name: str
    available: bool
    path: Path | None = None
    version: str | None = None
    details: dict[str, str] = Field(default_factory=dict)


class KicadDetection(FrozenModel):
    installations: tuple[IntegrationStatus, ...]
    selected_cli: Path | None
    user_data_directories: tuple[Path, ...]
    plugins: tuple[IntegrationStatus, ...]


class ProjectSession(FrozenModel):
    id: str
    root: Path
    project_file: Path
    schematic_files: tuple[Path, ...]
    pcb_file: Path | None = None
    hashes: dict[str, str]
    kicad_version: str | None = None
    opened_at: datetime = Field(default_factory=utc_now)


class Component(FrozenModel):
    reference: str
    value: str
    lib_id: str | None = None
    footprint: str | None = None
    properties: dict[str, str] = Field(default_factory=dict)


class NetPin(FrozenModel):
    reference: str
    pin: str
    pin_name: str | None = None


class Net(FrozenModel):
    name: str
    pins: tuple[NetPin, ...] = ()


class ProjectSummary(FrozenModel):
    session_id: str
    sheets: tuple[str, ...]
    components: tuple[Component, ...]
    nets: tuple[Net, ...]
    power_symbols: tuple[str, ...]
    warnings: tuple[str, ...] = ()


class ErcViolation(FrozenModel):
    severity: Literal["error", "warning", "info", "unknown"]
    code: str | None = None
    message: str
    items: tuple[str, ...] = ()


class ErcReport(FrozenModel):
    available: bool
    violations: tuple[ErcViolation, ...] = ()
    source: str = "kicad-cli"
    checked_at: datetime = Field(default_factory=utc_now)
    error: StructuredError | None = None


class DrcViolation(FrozenModel):
    severity: Literal["error", "warning", "info", "unknown"]
    code: str | None = None
    message: str
    items: tuple[str, ...] = ()


class DrcReport(FrozenModel):
    available: bool
    violations: tuple[DrcViolation, ...] = ()
    unconnected_items: tuple[str, ...] = ()
    source: str = "kicad-cli"
    checked_at: datetime = Field(default_factory=utc_now)
    error: StructuredError | None = None


class RequirementSet(FrozenModel):
    functional: dict[str, str | int | float | bool] = Field(default_factory=dict)
    electrical: dict[str, str | int | float | bool] = Field(default_factory=dict)
    mechanical: dict[str, str | int | float | bool] = Field(default_factory=dict)
    commercial: dict[str, str | int | float | bool] = Field(default_factory=dict)
    sourcing: dict[str, str | int | float | bool] = Field(default_factory=dict)
    assumptions: tuple[str, ...] = ()


class PriceBreak(FrozenModel):
    quantity: Annotated[int, Field(gt=0)]
    unit_price: Annotated[float, Field(ge=0)]
    currency: str = "USD"


class AssetAvailability(FrozenModel):
    symbol: bool = False
    footprint: bool = False
    model_3d: bool = False
    datasheet: bool = False


class ComponentCandidate(FrozenModel):
    lcsc: str
    mpn: str
    manufacturer: str
    description: str
    package: str
    basic_extended: Literal["basic", "extended", "unknown"] = "unknown"
    stock: Annotated[int, Field(ge=0)] = 0
    price_breaks: tuple[PriceBreak, ...] = ()
    datasheet_url: HttpUrl | None = None
    asset_availability: AssetAvailability = Field(default_factory=AssetAvailability)
    score: float = 0
    evidence: tuple[str, ...] = ()
    source: str = "JLCPCB/LCSC"
    retrieved_at: datetime = Field(default_factory=utc_now)

    @field_validator("price_breaks")
    @classmethod
    def unique_sorted_breaks(cls, value: tuple[PriceBreak, ...]) -> tuple[PriceBreak, ...]:
        quantities = [item.quantity for item in value]
        if quantities != sorted(set(quantities)):
            raise ValueError("price breaks must have unique, ascending quantities")
        return value


class ChangeOperation(FrozenModel):
    kind: Literal[
        "add_component",
        "replace_component",
        "update_property",
        "connect",
        "label",
        "no_connect",
    ]
    target: str
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)


class ValidationReport(FrozenModel):
    valid: bool
    checks: dict[str, bool] = Field(default_factory=dict)
    messages: tuple[str, ...] = ()
    erc: ErcReport | None = None


class ChangeStatus(StrEnum):
    PREPARED = "prepared"
    VALIDATED = "validated"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    STALE = "stale"


class ChangeSet(FrozenModel):
    id: str
    session_id: str
    project_hash: str
    operations: tuple[ChangeOperation, ...]
    affected_files: tuple[Path, ...]
    source_hashes: dict[str, str]
    semantic_diff: tuple[str, ...]
    risks: tuple[str, ...]
    validation_report: ValidationReport
    preview_directory: Path
    status: ChangeStatus = ChangeStatus.PREPARED
    snapshot_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ManufacturingProfile(FrozenModel):
    """Fabrication and thermal limits used by the deterministic rule engine."""

    min_clearance_mm: Annotated[float, Field(gt=0)] = 0.2
    min_track_width_mm: Annotated[float, Field(gt=0)] = 0.2
    min_via_diameter_mm: Annotated[float, Field(gt=0)] = 0.6
    min_via_drill_mm: Annotated[float, Field(gt=0)] = 0.3
    copper_thickness_um: Annotated[float, Field(gt=0)] = 35.0
    allowed_temperature_rise_c: Annotated[float, Field(gt=0)] = 10.0
    current_layer: Literal["external", "internal"] = "external"
    fanout_width_ratio: Annotated[float, Field(gt=0, le=1)] = 0.8
    courtyard_margin_mm: Annotated[float, Field(gt=0)] = 0.25

    @field_validator("min_via_drill_mm")
    @classmethod
    def drill_smaller_than_via(cls, value: float, info: Any) -> float:
        diameter = info.data.get("min_via_diameter_mm")
        if isinstance(diameter, (int, float)) and value >= diameter:
            raise ValueError("min_via_drill_mm must be smaller than min_via_diameter_mm")
        return value


class NetRuleRequirement(FrozenModel):
    """Explicit electrical intent for one or more exact KiCad net names."""

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    nets: Annotated[tuple[str, ...], Field(min_length=1)]
    role: Literal[
        "signal",
        "power",
        "high_current",
        "high_voltage",
        "differential",
        "switching",
    ]
    current_a: Annotated[float | None, Field(gt=0)] = None
    voltage_v: Annotated[float | None, Field(gt=0)] = None
    clearance_mm: Annotated[float | None, Field(gt=0)] = None
    track_width_mm: Annotated[float | None, Field(gt=0)] = None
    diff_pair_width_mm: Annotated[float | None, Field(gt=0)] = None
    diff_pair_gap_mm: Annotated[float | None, Field(gt=0)] = None
    creepage_mm: Annotated[float | None, Field(gt=0)] = None
    max_length_mm: Annotated[float | None, Field(gt=0)] = None
    diff_pair_max_uncoupled_mm: Annotated[float | None, Field(gt=0)] = None

    @field_validator("nets")
    @classmethod
    def unique_nonempty_nets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("net names must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("net names must be unique within a requirement")
        return value


class NetClassRule(FrozenModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    clearance_mm: Annotated[float, Field(gt=0)]
    track_width_min_mm: Annotated[float, Field(gt=0)]
    track_width_preferred_mm: Annotated[float, Field(gt=0)]
    via_diameter_mm: Annotated[float, Field(gt=0)]
    via_drill_mm: Annotated[float, Field(gt=0)]
    diff_pair_width_mm: Annotated[float | None, Field(gt=0)] = None
    diff_pair_gap_mm: Annotated[float | None, Field(gt=0)] = None
    creepage_mm: Annotated[float | None, Field(gt=0)] = None
    max_length_mm: Annotated[float | None, Field(gt=0)] = None
    diff_pair_max_uncoupled_mm: Annotated[float | None, Field(gt=0)] = None
    rationale: tuple[str, ...] = ()

    @model_validator(mode="after")
    def preferred_not_below_minimum(self) -> NetClassRule:
        if self.track_width_preferred_mm < self.track_width_min_mm:
            raise ValueError("preferred track width must not be below its minimum")
        if self.via_drill_mm >= self.via_diameter_mm:
            raise ValueError("via drill must be smaller than via diameter")
        return self


class NetClassAssignment(FrozenModel):
    net: str
    netclass: str


class FanoutConstraint(FrozenModel):
    """Local routing limits scoped to one footprint courtyard."""

    reference: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
    footprint: str
    min_track_width_mm: Annotated[float, Field(gt=0)]
    max_track_width_mm: Annotated[float, Field(gt=0)]
    clearance_mm: Annotated[float, Field(gt=0)]
    pad_min_dimension_mm: Annotated[float, Field(gt=0)]
    pad_min_clearance_mm: Annotated[float, Field(gt=0)]
    min_pitch_mm: Annotated[float | None, Field(gt=0)] = None
    rationale: tuple[str, ...] = ()

    @model_validator(mode="after")
    def maximum_not_below_minimum(self) -> FanoutConstraint:
        if self.max_track_width_mm < self.min_track_width_mm:
            raise ValueError("fanout maximum must not be below fabrication minimum")
        return self


class CourtyardAddition(FrozenModel):
    """Allowlisted generated courtyard for one project-local footprint library item."""

    footprint: str
    min_x_mm: float
    min_y_mm: float
    max_x_mm: float
    max_y_mm: float
    line_width_mm: Annotated[float, Field(gt=0)] = 0.05

    @model_validator(mode="after")
    def valid_bounds(self) -> CourtyardAddition:
        if self.min_x_mm >= self.max_x_mm or self.min_y_mm >= self.max_y_mm:
            raise ValueError("courtyard bounds must have positive area")
        return self


class PcbRuleSet(FrozenModel):
    """Fully typed rule set; callers never provide raw KiCad rule syntax."""

    manufacturing: ManufacturingProfile
    classes: Annotated[tuple[NetClassRule, ...], Field(min_length=1)]
    assignments: Annotated[tuple[NetClassAssignment, ...], Field(min_length=1)]
    fanout_constraints: tuple[FanoutConstraint, ...] = ()
    courtyard_additions: tuple[CourtyardAddition, ...] = ()
    evidence: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    generated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def assignments_are_unambiguous(self) -> PcbRuleSet:
        class_names = [item.name for item in self.classes]
        if len(class_names) != len(set(class_names)):
            raise ValueError("netclass names must be unique")
        assigned_nets = [item.net for item in self.assignments]
        if len(assigned_nets) != len(set(assigned_nets)):
            raise ValueError("each net may be assigned only once")
        known_classes = set(class_names)
        if any(item.netclass not in known_classes for item in self.assignments):
            raise ValueError("assignments must reference a rule-set netclass")
        references = [item.reference for item in self.fanout_constraints]
        if len(references) != len(set(references)):
            raise ValueError("each footprint may have only one fanout constraint")
        courtyard_footprints = [item.footprint for item in self.courtyard_additions]
        if len(courtyard_footprints) != len(set(courtyard_footprints)):
            raise ValueError("each footprint may have only one generated courtyard")
        return self


class ExistingNetClass(FrozenModel):
    name: str
    clearance_mm: float
    track_width_mm: float
    via_diameter_mm: float
    via_drill_mm: float


class NetConstraintCandidate(FrozenModel):
    net: str
    suggested_role: Literal["signal", "power", "differential", "switching"]
    connected_references: tuple[str, ...] = ()
    rationale: str


class FootprintConstraintCandidate(FrozenModel):
    reference: str
    footprint: str
    source: Path | None = None
    pad_count: int = 0
    pad_min_dimension_mm: float | None = None
    min_pitch_mm: float | None = None
    safe_fanout_width_mm: float | None = None
    safe_clearance_mm: float | None = None
    has_courtyard: bool = False
    warnings: tuple[str, ...] = ()


class PcbConstraintAnalysis(FrozenModel):
    session_id: str
    pcb_available: bool
    existing_classes: tuple[ExistingNetClass, ...]
    assignments: tuple[NetClassAssignment, ...]
    candidates: tuple[NetConstraintCandidate, ...]
    footprints: tuple[FootprintConstraintCandidate, ...] = ()
    custom_rule_file: Path | None = None
    warnings: tuple[str, ...] = ()


class PcbRuleChangeSet(FrozenModel):
    id: str
    session_id: str
    project_hash: str
    rule_set: PcbRuleSet
    affected_files: tuple[Path, ...]
    source_hashes: dict[str, str]
    semantic_diff: tuple[str, ...]
    risks: tuple[str, ...]
    validation_report: ValidationReport
    drc: DrcReport | None = None
    preview_directory: Path
    status: ChangeStatus = ChangeStatus.PREPARED
    snapshot_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class PcbBounds(FrozenModel):
    min_x_mm: float
    min_y_mm: float
    max_x_mm: float
    max_y_mm: float

    @model_validator(mode="after")
    def positive_area(self) -> PcbBounds:
        if self.min_x_mm >= self.max_x_mm or self.min_y_mm >= self.max_y_mm:
            raise ValueError("PCB bounds must have positive area")
        return self


class PcbFootprintPlacement(FrozenModel):
    reference: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
    footprint: str
    value: str | None = None
    x_mm: float
    y_mm: float
    rotation_deg: float = 0
    layer: Literal["F.Cu", "B.Cu"]
    locked: bool = False
    bounds: PcbBounds


class PcbSummary(FrozenModel):
    session_id: str
    pcb_file: Path
    board_bounds: PcbBounds | None = None
    footprints: tuple[PcbFootprintPlacement, ...] = ()
    net_count: Annotated[int, Field(ge=0)] = 0
    track_count: Annotated[int, Field(ge=0)] = 0
    via_count: Annotated[int, Field(ge=0)] = 0
    zone_count: Annotated[int, Field(ge=0)] = 0
    ipc: IntegrationStatus
    warnings: tuple[str, ...] = ()


class PcbNetInspection(FrozenModel):
    session_id: str
    net: str
    code: Annotated[int, Field(ge=0)]
    connected_references: tuple[str, ...] = ()
    pad_count: Annotated[int, Field(ge=0)] = 0
    track_count: Annotated[int, Field(ge=0)] = 0
    via_count: Annotated[int, Field(ge=0)] = 0
    routed_length_mm: Annotated[float, Field(ge=0)] = 0
    layers: tuple[str, ...] = ()


class PcbPadInspection(FrozenModel):
    """Absolute pad geometry used by the deterministic routing engine."""

    reference: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
    number: str
    net: str
    x_mm: float
    y_mm: float
    width_mm: Annotated[float, Field(gt=0)]
    height_mm: Annotated[float, Field(gt=0)]
    rotation_deg: float = 0
    layers: Annotated[tuple[Literal["F.Cu", "B.Cu"], ...], Field(min_length=1)]


class UnroutedConnection(FrozenModel):
    net: str
    start_reference: str
    start_pad: str
    end_reference: str
    end_pad: str
    distance_mm: Annotated[float, Field(ge=0)]


class RoutingAnalysis(FrozenModel):
    session_id: str
    complete: bool
    net_count: Annotated[int, Field(ge=0)]
    routed_net_count: Annotated[int, Field(ge=0)]
    unrouted_net_count: Annotated[int, Field(ge=0)]
    unrouted_connection_count: Annotated[int, Field(ge=0)]
    unrouted_connections: tuple[UnroutedConnection, ...] = ()
    ignored_single_pad_nets: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()


class RouteSegment(FrozenModel):
    net: str
    start_x_mm: float
    start_y_mm: float
    end_x_mm: float
    end_y_mm: float
    width_mm: Annotated[float, Field(gt=0)]
    layer: Literal["F.Cu", "B.Cu"] = "F.Cu"

    @model_validator(mode="after")
    def nonzero_length(self) -> RouteSegment:
        if self.start_x_mm == self.end_x_mm and self.start_y_mm == self.end_y_mm:
            raise ValueError("route segments must have nonzero length")
        return self


class RouteVia(FrozenModel):
    net: str
    x_mm: float
    y_mm: float
    diameter_mm: Annotated[float, Field(gt=0)] = 0.6
    drill_mm: Annotated[float, Field(gt=0)] = 0.3
    layers: tuple[Literal["F.Cu", "B.Cu"], Literal["F.Cu", "B.Cu"]] = (
        "F.Cu",
        "B.Cu",
    )

    @model_validator(mode="after")
    def valid_via(self) -> RouteVia:
        if self.drill_mm >= self.diameter_mm:
            raise ValueError("via drill must be smaller than via diameter")
        if self.layers[0] == self.layers[1]:
            raise ValueError("via layers must differ")
        return self


class RoutingRequest(FrozenModel):
    """Deterministic routing intent; no raw KiCad syntax is accepted."""

    nets: tuple[str, ...] = ()
    preferred_layer: Literal["F.Cu", "B.Cu"] = "F.Cu"
    default_track_width_mm: Annotated[float, Field(gt=0)] = 0.25
    default_clearance_mm: Annotated[float, Field(gt=0)] = 0.2
    via_diameter_mm: Annotated[float, Field(gt=0)] = 0.6
    via_drill_mm: Annotated[float, Field(gt=0)] = 0.3
    grid_mm: Annotated[float, Field(gt=0)] = 0.05
    allow_vias: bool = True
    require_complete: bool = True
    existing_copper_policy: Literal["reject", "preserve"] = "reject"
    candidate_count: Annotated[int, Field(ge=1, le=2)] = 1
    max_passes: Annotated[int, Field(ge=1, le=200)] = 30
    thread_count: Annotated[int, Field(ge=0, le=64)] = 0

    @model_validator(mode="after")
    def valid_request(self) -> RoutingRequest:
        if self.via_drill_mm >= self.via_diameter_mm:
            raise ValueError("via drill must be smaller than via diameter")
        if len(self.nets) != len(set(self.nets)):
            raise ValueError("routing net names must be unique")
        if any(not item.strip() for item in self.nets):
            raise ValueError("routing net names must not be empty")
        return self


class RoutingCandidateEvaluation(FrozenModel):
    """Deterministic evidence used to rank one external autorouter result."""

    strategy: Literal["prioritized", "sequential"]
    selected: bool = False
    complete: bool
    unrouted_connection_count: Annotated[int, Field(ge=0)]
    drc_available: bool
    new_drc_error_count: Annotated[int, Field(ge=0)] | None = None
    segment_count: Annotated[int, Field(ge=0)]
    via_count: Annotated[int, Field(ge=0)]
    track_length_mm: Annotated[float, Field(ge=0)]
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    duplicate_of: Literal["prioritized", "sequential"] | None = None


class RoutingBackendStatus(FrozenModel):
    """Availability of the fixed-command local PCB autorouting backend."""

    name: Literal["FreeRouting"] = "FreeRouting"
    available: bool
    version: str | None = None
    java_major_version: Annotated[int, Field(ge=1)] | None = None
    java_path: Path | None = None
    jar_path: Path | None = None
    kicad_python_path: Path | None = None
    reason: str | None = None


class RoutingPlan(FrozenModel):
    session_id: str
    request: RoutingRequest
    segments: tuple[RouteSegment, ...] = ()
    vias: tuple[RouteVia, ...] = ()
    target_nets: Annotated[tuple[str, ...], Field(min_length=1)]
    analysis_before: RoutingAnalysis
    predicted_complete: bool
    backend: Literal["freerouting", "test"] = "freerouting"
    backend_version: str | None = None
    candidate_evaluations: tuple[RoutingCandidateEvaluation, ...] = ()
    evidence: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def has_operations(self) -> RoutingPlan:
        if not self.segments and not self.vias:
            raise ValueError("routing plan must contain at least one operation")
        return self


class PcbRoutingChangeSet(FrozenModel):
    id: str
    session_id: str
    project_hash: str
    plan: RoutingPlan
    affected_files: tuple[Path, ...]
    source_hashes: dict[str, str]
    semantic_diff: tuple[str, ...]
    risks: tuple[str, ...]
    validation_report: ValidationReport
    drc: DrcReport
    routing_analysis: RoutingAnalysis
    preview_directory: Path
    preview_pdf: Path | None = None
    status: ChangeStatus = ChangeStatus.PREPARED
    snapshot_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class RoutingChangeRecord(FrozenModel):
    """Private, versioned persistence envelope for a prepared routing change."""

    schema_version: Literal[1] = 1
    project_root: Path
    workspace: Path
    affected_relative_files: Annotated[tuple[Path, ...], Field(min_length=1)]
    change_set: PcbRoutingChangeSet
    snapshot: Path | None = None

    @field_validator("affected_relative_files")
    @classmethod
    def relative_files_only(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        if any(path.is_absolute() or ".." in path.parts for path in value):
            raise ValueError("affected routing files must be project-relative")
        return value


class RoutingReviewSummary(FrozenModel):
    """Compact review surface for a potentially very large routing change set."""

    change_set_id: str
    status: ChangeStatus
    target_nets: tuple[str, ...]
    validation_valid: bool
    routing_complete: bool
    unrouted_connection_count: Annotated[int, Field(ge=0)]
    segment_count: Annotated[int, Field(ge=0)]
    via_count: Annotated[int, Field(ge=0)]
    drc_error_count: Annotated[int, Field(ge=0)]
    drc_warning_count: Annotated[int, Field(ge=0)]
    preview_directory: Path
    preview_pdf: Path | None = None
    risks: tuple[str, ...] = ()


class RoutingSnapshotRestoreResult(FrozenModel):
    """Evidence returned after restoring a private routing snapshot."""

    status: Literal["restored"] = "restored"
    restored_snapshot_id: str
    recovery_snapshot_id: str
    affected_file: Path
    validation_report: ValidationReport
    drc: DrcReport


class PlacementIssue(FrozenModel):
    kind: Literal[
        "overlap",
        "outside_board",
        "locked",
        "missing_reference",
        "missing_outline",
        "empty_board",
    ]
    severity: Literal["error", "warning"]
    references: tuple[str, ...]
    message: str


class PlacementAnalysis(FrozenModel):
    session_id: str
    score: Annotated[int, Field(ge=0, le=100)]
    issues: tuple[PlacementIssue, ...] = ()
    overlap_pairs: tuple[tuple[str, str], ...] = ()
    outside_board: tuple[str, ...] = ()
    footprint_count: Annotated[int, Field(ge=0)] = 0
    assumptions: tuple[str, ...] = ()


class PcbReadinessCheck(FrozenModel):
    """One deterministic gate or explicitly unassessed production concern."""

    name: str
    status: Literal["pass", "warning", "fail", "not_assessed"]
    message: str


class PcbProductionReadiness(FrozenModel):
    """Distinguish electrically validated routing from production certification."""

    session_id: str
    status: Literal["blocked", "routing_validated", "review_required"]
    electrically_validated: bool
    production_ready: bool = False
    checks: tuple[PcbReadinessCheck, ...]
    blocking_reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    assessed_at: datetime = Field(default_factory=utc_now)


class PcbFinalizationResult(FrozenModel):
    """Compact state returned by the routing finalization orchestrator."""

    stage: Literal["prepared", "validated", "applied", "rolled_back", "stale"]
    routing: RoutingReviewSummary
    readiness: PcbProductionReadiness
    confirmation_required: bool


class PlacementRequest(FrozenModel):
    references: Annotated[tuple[str, ...], Field(min_length=1)]
    strategy: Literal["grid", "compact"] = "compact"
    region: PcbBounds | None = None
    spacing_mm: Annotated[float, Field(ge=0)] = 1.0
    grid_mm: Annotated[float, Field(gt=0)] = 0.5
    rotation_deg: float = 0

    @field_validator("references")
    @classmethod
    def unique_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("placement references must be unique")
        return value


class PlacementOperation(FrozenModel):
    reference: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
    x_mm: float
    y_mm: float
    rotation_deg: float = 0
    layer: Literal["F.Cu", "B.Cu"] | None = None


class PlacementProposal(FrozenModel):
    session_id: str
    request: PlacementRequest
    operations: Annotated[tuple[PlacementOperation, ...], Field(min_length=1)]
    analysis_before: PlacementAnalysis
    analysis_after: PlacementAnalysis
    evidence: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()


class PcbPlacementChangeSet(FrozenModel):
    id: str
    session_id: str
    project_hash: str
    operations: Annotated[tuple[PlacementOperation, ...], Field(min_length=1)]
    affected_files: tuple[Path, ...]
    source_hashes: dict[str, str]
    semantic_diff: tuple[str, ...]
    risks: tuple[str, ...]
    validation_report: ValidationReport
    drc: DrcReport
    preview_directory: Path
    preview_pdf: Path | None = None
    status: ChangeStatus = ChangeStatus.PREPARED
    snapshot_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class RectangularBoardOutline(FrozenModel):
    min_x_mm: float = 80.0
    min_y_mm: float = 80.0
    width_mm: Annotated[float, Field(gt=0)]
    height_mm: Annotated[float, Field(gt=0)]
    line_width_mm: Annotated[float, Field(gt=0, le=1)] = 0.05


class MountingHoleSpec(FrozenModel):
    reference: str = Field(pattern=r"^H[1-9][0-9]{0,2}$")
    x_mm: float
    y_mm: float
    preset: Literal["M3_3.2MM_NPTH"] = "M3_3.2MM_NPTH"


class PcbLayoutPlan(FrozenModel):
    outline: RectangularBoardOutline
    placements: Annotated[tuple[PlacementOperation, ...], Field(min_length=1)]
    mounting_holes: tuple[MountingHoleSpec, ...] = ()
    footprint_overrides: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_layout_references(self) -> PcbLayoutPlan:
        references = [item.reference for item in self.placements]
        holes = [item.reference for item in self.mounting_holes]
        if len(references) != len(set(references)):
            raise ValueError("layout placements must have unique references")
        if len(holes) != len(set(holes)) or set(references) & set(holes):
            raise ValueError("mounting-hole references must be unique")
        return self


class PcbLayoutChangeSet(FrozenModel):
    id: str
    session_id: str
    project_hash: str
    plan: PcbLayoutPlan
    affected_files: tuple[Path, ...]
    source_hashes: dict[str, str]
    semantic_diff: tuple[str, ...]
    risks: tuple[str, ...]
    validation_report: ValidationReport
    erc: ErcReport
    drc: DrcReport
    placement_analysis: PlacementAnalysis
    preview_directory: Path
    preview_pdf: Path | None = None
    status: ChangeStatus = ChangeStatus.PREPARED
    snapshot_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class BomLine(FrozenModel):
    references: tuple[str, ...]
    quantity_per_board: Annotated[int, Field(gt=0)]
    value: str
    footprint: str | None = None
    lcsc: str | None = None
    mpn: str | None = None
    basic_extended: Literal["basic", "extended", "unknown"] = "unknown"
    unit_prices: dict[int, float] = Field(default_factory=dict)
    stock: int | None = Field(default=None, ge=0)
    price_timestamp: datetime | None = None


class CostEstimate(FrozenModel):
    quantity: Annotated[int, Field(gt=0)]
    currency: str
    component_cost: float
    missing_prices: tuple[str, ...] = ()
    insufficient_stock: tuple[str, ...] = ()
    excluded_costs: tuple[str, ...] = ("PCB", "assembly", "stencil", "shipping", "taxes", "duties")
    assumptions: tuple[str, ...] = ()
    priced_at: datetime = Field(default_factory=utc_now)


class ComponentAssetBundle(FrozenModel):
    lcsc: str
    nickname: str
    symbol: Path
    footprint: Path
    model_3d: Path | None = None
    datasheet: Path | None = None


class AssetImportResult(FrozenModel):
    lcsc: str
    imported_files: tuple[Path, ...]
    library_tables: tuple[Path, ...]
    idempotent: bool
    validation: ValidationReport
