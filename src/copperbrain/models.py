"""Transport-independent domain and boundary models."""

from __future__ import annotations

import re
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
        "set_paper_size",
        "move_component",
        "relayout_pin_label",
    ]
    target: str
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)


class ValidationReport(FrozenModel):
    valid: bool
    checks: dict[str, bool] = Field(default_factory=dict)
    messages: tuple[str, ...] = ()
    erc: ErcReport | None = None


class SchematicReadabilityReport(FrozenModel):
    """Deterministic layout evidence extracted from a rendered schematic source."""

    schematic_file: Path
    component_count: Annotated[int, Field(ge=0)]
    label_count: Annotated[int, Field(ge=0)]
    wire_count: Annotated[int, Field(ge=0)]
    labels_directly_on_pins: Annotated[int, Field(ge=0)]
    labels_without_wire_connection: Annotated[int, Field(ge=0)] = 0
    duplicate_label_positions: Annotated[int, Field(ge=0)]
    label_overlap_count: Annotated[int, Field(ge=0)]
    minimum_component_spacing_mm: Annotated[float | None, Field(ge=0)] = None
    occupied_width_mm: Annotated[float, Field(ge=0)] = 0
    occupied_height_mm: Annotated[float, Field(ge=0)] = 0
    readability_score: Annotated[float, Field(ge=0, le=100)]
    valid: bool
    messages: tuple[str, ...] = ()


class ChangeStatus(StrEnum):
    PREPARED = "prepared"
    VALIDATED = "validated"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    STALE = "stale"


class ProjectCreationSpec(FrozenModel):
    """Bounded empty-project creation request."""

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    copper_layers: Literal[2, 4] = 2


class ProjectCreationChangeSet(FrozenModel):
    """Review evidence for creating a new project root."""

    id: str
    spec: ProjectCreationSpec
    target_root: Path
    affected_files: tuple[Path, ...]
    semantic_diff: tuple[str, ...]
    risks: tuple[str, ...]
    validation_report: ValidationReport
    preview_directory: Path
    status: ChangeStatus = ChangeStatus.PREPARED
    applied_hashes: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ProjectCreationRecord(FrozenModel):
    """Private restart-safe envelope for an empty-project creation."""

    schema_version: Literal[1] = 1
    workspace: Path
    change_set: ProjectCreationChangeSet


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
    readability_report: SchematicReadabilityReport | None = None
    preview_directory: Path
    status: ChangeStatus = ChangeStatus.PREPARED
    snapshot_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class SchematicChangeRecord(FrozenModel):
    """Private, versioned persistence envelope for a prepared schematic change."""

    schema_version: Literal[1] = 1
    project_root: Path
    workspace: Path
    affected_relative_files: Annotated[tuple[Path, ...], Field(min_length=1)]
    change_set: ChangeSet
    snapshot: Path | None = None

    @field_validator("affected_relative_files")
    @classmethod
    def relative_files_only(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        if any(path.is_absolute() or ".." in path.parts for path in value):
            raise ValueError("affected schematic files must be project-relative")
        return value


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
    class_roles: dict[
        str,
        Literal[
            "signal",
            "power",
            "high_current",
            "high_voltage",
            "differential",
            "switching",
        ],
    ] = Field(default_factory=dict)
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
        if self.class_roles and set(self.class_roles) != known_classes:
            raise ValueError("class_roles must explicitly classify every rule-set netclass")
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


class PcbRuleChangeRecord(FrozenModel):
    """Private restart-safe state for a prepared PCB-rule mutation."""

    project_root: Path
    workspace: Path
    affected_relative_files: tuple[Path, ...]
    originally_existing: frozenset[str]
    change_set: PcbRuleChangeSet
    snapshot: Path | None = None


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
    mount_type: Literal["smd", "through_hole", "mixed", "unknown"] = "unknown"
    locked: bool = False
    bounds: PcbBounds
    local_bounds: PcbBounds | None = None


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
    layers: Annotated[tuple[str, ...], Field(min_length=1)]

    @field_validator("layers")
    @classmethod
    def copper_layers_only(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            re.fullmatch(r"(?:F|B|In(?:[1-9]|[12][0-9]|30))\.Cu", item) is None for item in value
        ):
            raise ValueError("pad layers must be copper layer names")
        return value


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
    layer: str = "F.Cu"

    @field_validator("layer")
    @classmethod
    def copper_layer_only(cls, value: str) -> str:
        if re.fullmatch(r"(?:F|B|In(?:[1-9]|[12][0-9]|30))\.Cu", value) is None:
            raise ValueError("route segment layer must be a copper layer name")
        return value

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
    layers: tuple[str, str] = ("F.Cu", "B.Cu")

    @model_validator(mode="after")
    def valid_via(self) -> RouteVia:
        if self.drill_mm >= self.diameter_mm:
            raise ValueError("via drill must be smaller than via diameter")
        if self.layers[0] == self.layers[1]:
            raise ValueError("via layers must differ")
        if any(
            re.fullmatch(r"(?:F|B|In(?:[1-9]|[12][0-9]|30))\.Cu", item) is None
            for item in self.layers
        ):
            raise ValueError("via layers must be copper layer names")
        return self


class GroundDomainRequest(FrozenModel):
    """One reviewed ground domain and its optional primary/dedicated copper layers."""

    net_name: str = Field(min_length=1, max_length=128)
    layers: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    pad_connection: Literal["thermal", "solid"] = "thermal"

    @model_validator(mode="after")
    def valid_domain(self) -> GroundDomainRequest:
        if not self.net_name.strip():
            raise ValueError("ground domain net name must not be empty")
        if len(self.layers) != len(set(self.layers)):
            raise ValueError("ground domain layers must be unique")
        if any(
            re.fullmatch(r"(?:F|B|In(?:[1-9]|[12][0-9]|30))\.Cu", item) is None
            for item in self.layers
        ):
            raise ValueError("ground domain layers must be copper layer names")
        return self


class GroundingRequest(FrozenModel):
    """Typed intent for one plane or multiple bridge-connected ground domains."""

    copper_layers: Literal[2, 4] = 2
    net_name: str | None = None
    domains: Annotated[tuple[GroundDomainRequest, ...], Field(max_length=32)] = ()
    layers: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    bridge_references: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    replace_existing_planes: bool = False
    edge_clearance_mm: Annotated[float, Field(gt=0)] = 0.5
    clearance_mm: Annotated[float, Field(ge=0)] = 0.2
    min_thickness_mm: Annotated[float, Field(gt=0)] = 0.25
    thermal_gap_mm: Annotated[float, Field(gt=0)] = 0.3
    thermal_spoke_width_mm: Annotated[float, Field(gt=0)] = 0.3
    region_margin_mm: Annotated[float, Field(gt=0)] = 0.8
    fanout_width_mm: Annotated[float, Field(gt=0)] = 0.2
    allow_vias: bool = True
    allow_via_in_pad: bool = False
    via_diameter_mm: Annotated[float, Field(gt=0)] = 0.6
    via_drill_mm: Annotated[float, Field(gt=0)] = 0.3
    via_spacing_mm: Annotated[float, Field(gt=0)] = 10.0
    max_stitching_vias: Annotated[int, Field(ge=0, le=512)] = 64

    @model_validator(mode="after")
    def valid_grounding_request(self) -> GroundingRequest:
        if self.net_name is not None and not self.net_name.strip():
            raise ValueError("ground net name must not be empty")
        if self.net_name is not None and self.domains:
            raise ValueError("net_name and domains are mutually exclusive")
        domain_names = tuple(item.net_name for item in self.domains)
        if len(domain_names) != len(set(domain_names)):
            raise ValueError("ground domain net names must be unique")
        if len(self.bridge_references) != len(set(self.bridge_references)):
            raise ValueError("ground bridge references must be unique")
        if any(
            re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", item) is None
            for item in self.bridge_references
        ):
            raise ValueError("ground bridge references are invalid")
        if len(self.layers) != len(set(self.layers)):
            raise ValueError("ground plane layers must be unique")
        if any(
            re.fullmatch(r"(?:F|B|In(?:[1-9]|[12][0-9]|30))\.Cu", item) is None
            for item in self.layers
        ):
            raise ValueError("ground plane layers must be copper layer names")
        explicit_domain_layers = tuple(layer for item in self.domains for layer in item.layers)
        if self.layers and explicit_domain_layers:
            raise ValueError("global layers cannot be combined with per-domain layers")
        if self.copper_layers == 4 and len(explicit_domain_layers) != len(
            set(explicit_domain_layers)
        ):
            raise ValueError("dedicated ground domain layers must not overlap")
        requested_layers = (*self.layers, *explicit_domain_layers)
        if self.copper_layers == 2 and any(item.startswith("In") for item in requested_layers):
            raise ValueError("two-layer grounding cannot target inner copper layers")
        if self.via_drill_mm >= self.via_diameter_mm:
            raise ValueError("via drill must be smaller than via diameter")
        if (
            self.allow_vias
            and (len(self.layers) > 1 or len(self.domains) > 1)
            and self.max_stitching_vias == 0
        ):
            raise ValueError("multi-layer grounding with vias requires a positive via limit")
        return self


class GroundBridge(FrozenModel):
    """Reviewed two-terminal component joining two otherwise separate ground domains."""

    reference: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
    net_a: str
    pad_a: str
    net_b: str
    pad_b: str

    @model_validator(mode="after")
    def different_nets(self) -> GroundBridge:
        if self.net_a == self.net_b:
            raise ValueError("a ground bridge must join different nets")
        return self


class GroundZoneRegion(FrozenModel):
    """A planner-derived board region or axis-aligned local copper region."""

    layer: str
    kind: Literal["board", "local"]
    min_x_mm: float | None = None
    min_y_mm: float | None = None
    max_x_mm: float | None = None
    max_y_mm: float | None = None

    @model_validator(mode="after")
    def valid_region(self) -> GroundZoneRegion:
        if re.fullmatch(r"(?:F|B|In(?:[1-9]|[12][0-9]|30))\.Cu", self.layer) is None:
            raise ValueError("ground region layer must be a copper layer")
        bounds = (self.min_x_mm, self.min_y_mm, self.max_x_mm, self.max_y_mm)
        if self.kind == "board":
            if any(item is not None for item in bounds):
                raise ValueError("board ground regions derive their outline from Edge.Cuts")
            return self
        if self.layer not in {"F.Cu", "B.Cu"}:
            raise ValueError("local shaped ground regions require an outer copper layer")
        if any(item is None for item in bounds):
            raise ValueError("local ground regions require complete bounds")
        min_x, min_y, max_x, max_y = bounds
        assert min_x is not None and min_y is not None and max_x is not None and max_y is not None
        if min_x >= max_x or min_y >= max_y:
            raise ValueError("local ground region bounds must have positive area")
        return self


class GroundDomainPlan(FrozenModel):
    """Primary plane, shaped regions, and deterministic fanout for one exact ground net."""

    net_name: str
    primary_layer: str
    plane_layers: Annotated[tuple[str, ...], Field(min_length=1, max_length=32)]
    regions: Annotated[tuple[GroundZoneRegion, ...], Field(min_length=1, max_length=512)]
    pad_connection: Literal["thermal", "solid"] = "thermal"
    replaced_plane_layers: tuple[str, ...] = ()
    fanout_segments: tuple[RouteSegment, ...] = ()
    vias: tuple[RouteVia, ...] = ()
    target_pad_count: Annotated[int, Field(ge=1)]
    target_references: Annotated[tuple[str, ...], Field(min_length=1)]
    planes_connected: bool

    @model_validator(mode="after")
    def valid_domain_plan(self) -> GroundDomainPlan:
        if len(self.plane_layers) != len(set(self.plane_layers)):
            raise ValueError("ground plane layers must be unique")
        if any(
            re.fullmatch(r"(?:F|B|In(?:[1-9]|[12][0-9]|30))\.Cu", item) is None
            for item in (*self.plane_layers, *self.replaced_plane_layers)
        ):
            raise ValueError("ground plane layers must be copper layer names")
        if self.primary_layer not in self.plane_layers:
            raise ValueError("ground primary layer must belong to its plane layers")
        if set(item.layer for item in self.regions) != set(self.plane_layers):
            raise ValueError("ground region layers must match the planned plane layers")
        if not any(item.kind == "board" for item in self.regions):
            raise ValueError("each ground domain requires at least one board region")
        if not any(
            item.kind == "board" and item.layer == self.primary_layer for item in self.regions
        ):
            raise ValueError("the primary ground layer must contain the board region")
        if any(item.net != self.net_name for item in self.vias):
            raise ValueError("all grounding vias must belong to their domain net")
        if any(item.net != self.net_name for item in self.fanout_segments):
            raise ValueError("all grounding fanouts must belong to their domain net")
        if len(self.plane_layers) > 1 and not self.planes_connected:
            raise ValueError("ground planes on multiple layers must be electrically connected")
        return self


class GroundingPlan(FrozenModel):
    """Deterministic shaped ground domains derived from placed PCB geometry."""

    session_id: str
    request: GroundingRequest
    domains: Annotated[tuple[GroundDomainPlan, ...], Field(min_length=1, max_length=32)]
    bridges: tuple[GroundBridge, ...] = ()
    evidence: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_grounding_plan(self) -> GroundingPlan:
        names = tuple(item.net_name for item in self.domains)
        if len(names) != len(set(names)):
            raise ValueError("grounding plan domain nets must be unique")
        board_region_layers = tuple(
            region.layer
            for domain in self.domains
            for region in domain.regions
            if region.kind == "board"
        )
        if len(board_region_layers) != len(set(board_region_layers)):
            raise ValueError("only one ground domain may own the board region on a layer")
        if any(item.net_a not in names or item.net_b not in names for item in self.bridges):
            raise ValueError("ground bridge nets must belong to planned domains")
        if len(self.domains) > 1 and not self.bridges:
            raise ValueError("multiple ground domains require reviewed bridges")
        return self


class GroundDomainAnalysis(FrozenModel):
    net_name: str
    complete: bool
    target_pad_count: Annotated[int, Field(ge=0)]
    connected_references: tuple[str, ...] = ()
    zone_layers: tuple[str, ...] = ()
    via_count: Annotated[int, Field(ge=0)] = 0
    fanout_segment_count: Annotated[int, Field(ge=0)] = 0
    unrouted_connection_count: Annotated[int, Field(ge=0)] = 0
    missing_pad_references: tuple[str, ...] = ()
    planes_connected: bool = False


class GroundingAnalysis(FrozenModel):
    session_id: str
    complete: bool
    domains: Annotated[tuple[GroundDomainAnalysis, ...], Field(min_length=1)]
    bridge_references: tuple[str, ...] = ()
    bridges_connected: bool = False
    assumptions: tuple[str, ...] = ()


class PcbGroundingChangeSet(FrozenModel):
    id: str
    session_id: str
    project_hash: str
    plan: GroundingPlan
    affected_files: tuple[Path, ...]
    source_hashes: dict[str, str]
    semantic_diff: tuple[str, ...]
    risks: tuple[str, ...]
    validation_report: ValidationReport
    drc: DrcReport
    grounding_analysis: GroundingAnalysis
    preview_directory: Path
    preview_pdf: Path | None = None
    status: ChangeStatus = ChangeStatus.PREPARED
    snapshot_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class PcbGroundingChangeRecord(FrozenModel):
    """Private restart-safe state for a prepared PCB-grounding mutation."""

    project_root: Path
    workspace: Path
    affected_relative_files: tuple[Path, ...]
    change_set: PcbGroundingChangeSet
    snapshot: Path | None = None


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
    candidate_count: Annotated[int, Field(ge=1, le=3)] = 1
    max_passes: Annotated[int, Field(ge=1, le=200)] = 30
    semantic_stagnation_passes: Annotated[int, Field(ge=1, le=50)] = 8
    thread_count: Annotated[int, Field(ge=0, le=64)] = 0
    excluded_plane_nets: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    allow_fine_pitch_escape_stubs: bool = False
    seed_segments: Annotated[tuple[RouteSegment, ...], Field(max_length=512)] = ()
    seed_vias: Annotated[tuple[RouteVia, ...], Field(max_length=256)] = ()
    net_roles: dict[
        str,
        Literal[
            "signal",
            "ground",
            "power",
            "high_current",
            "high_voltage",
            "differential",
            "switching",
            "motor_phase",
        ],
    ] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_request(self) -> RoutingRequest:
        if self.via_drill_mm >= self.via_diameter_mm:
            raise ValueError("via drill must be smaller than via diameter")
        if len(self.nets) != len(set(self.nets)):
            raise ValueError("routing net names must be unique")
        if any(not item.strip() for item in self.nets):
            raise ValueError("routing net names must not be empty")
        if any(not item.strip() for item in self.net_roles):
            raise ValueError("routing net role names must not be empty")
        if len(self.excluded_plane_nets) != len(set(self.excluded_plane_nets)):
            raise ValueError("excluded routing plane net names must be unique")
        if any(not item.strip() for item in self.excluded_plane_nets):
            raise ValueError("excluded routing plane net names must not be empty")
        seed_nets = {item.net for item in self.seed_segments} | {
            item.net for item in self.seed_vias
        }
        if self.nets and not seed_nets.issubset(self.nets):
            raise ValueError("routing seeds must reference requested nets")
        if self.seed_vias and not self.allow_vias:
            raise ValueError("routing seed vias require allow_vias")
        if self.nets and not set(self.net_roles).issubset(self.nets):
            raise ValueError("explicit routing net roles must reference requested nets")
        return self


class FreeRoutingPassMetric(FrozenModel):
    """Bounded progress evidence parsed from one FreeRouting autorouter pass."""

    pass_number: Annotated[int, Field(ge=1)]
    board_incomplete_count: Annotated[int, Field(ge=0)] | None = None
    queued_item_count: Annotated[int, Field(ge=0)] | None = None
    board_unrouted_count: Annotated[int, Field(ge=0)] | None = None
    failure_count: Annotated[int, Field(ge=0)] = 0
    duration_seconds: Annotated[float, Field(ge=0)] | None = None
    score: float | None = None
    cpu_seconds: Annotated[float, Field(ge=0)] | None = None
    allocated_memory_gb: Annotated[float, Field(ge=0)] | None = None
    connections_resolved: Annotated[int, Field(ge=0)] = 0
    connections_resolved_per_pass: Annotated[float, Field(ge=0)] = 0


class ConnectivityMetricRecord(FrozenModel):
    """Versioned private observation for connectivity and routing regression analysis."""

    schema_version: Literal[2, 3, 4] = 4
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    parent_run_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    operation: Literal["routing_proposal", "routing_change"] = "routing_proposal"
    phase: Literal["baseline", "candidate", "prepare", "validate", "apply", "rollback"]
    outcome: Literal["success", "failure"]
    started_at: datetime
    finished_at: datetime
    duration_seconds: Annotated[float, Field(ge=0)]
    project_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_hashes: dict[str, str] = Field(default_factory=dict)
    board_width_mm: Annotated[float, Field(gt=0)] | None = None
    board_height_mm: Annotated[float, Field(gt=0)] | None = None
    copper_layer_count: Annotated[int, Field(ge=0)] = 0
    footprint_count: Annotated[int, Field(ge=0)] = 0
    pad_count: Annotated[int, Field(ge=0)] = 0
    placement_density_percent: Annotated[float, Field(ge=0, le=100)] | None = None
    backend: str
    backend_version: str | None = None
    strategy: Literal["prioritized", "sequential", "prioritized_single_thread"] | None = None
    effective_configuration: dict[str, str | int | float | bool] = Field(default_factory=dict)
    requested_net_count: Annotated[int, Field(ge=0)] = 0
    requested_net_role_counts: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    baseline_routed_net_count: Annotated[int, Field(ge=0)] = 0
    baseline_unrouted_net_count: Annotated[int, Field(ge=0)] = 0
    final_routed_net_count: Annotated[int, Field(ge=0)] | None = None
    final_unrouted_net_count: Annotated[int, Field(ge=0)] | None = None
    baseline_open_connection_count: Annotated[int, Field(ge=0)]
    final_open_connection_count: Annotated[int, Field(ge=0)] | None = None
    open_connection_delta: int | None = None
    board_baseline_open_connection_count: Annotated[int, Field(ge=0)] | None = None
    board_final_open_connection_count: Annotated[int, Field(ge=0)] | None = None
    segment_count: Annotated[int, Field(ge=0)] = 0
    via_count: Annotated[int, Field(ge=0)] = 0
    routed_length_mm: Annotated[float, Field(ge=0)] = 0
    baseline_drc_error_count: Annotated[int, Field(ge=0)] | None = None
    final_drc_error_count: Annotated[int, Field(ge=0)] | None = None
    new_drc_error_count: Annotated[int, Field(ge=0)] | None = None
    baseline_drc_warning_count: Annotated[int, Field(ge=0)] | None = None
    final_drc_warning_count: Annotated[int, Field(ge=0)] | None = None
    new_drc_warning_count: Annotated[int, Field(ge=0)] | None = None
    error_code: ErrorCode | None = None
    watchdog_reason: str | None = None
    freerouting_pass_metrics: tuple[FreeRoutingPassMetric, ...] = ()
    freerouting_normalization_count: Annotated[int, Field(ge=0)] = 0
    best_pass_number: Annotated[int, Field(ge=1)] | None = None
    failed_route_count: Annotated[int, Field(ge=0)] = 0
    stagnation_count: Annotated[int, Field(ge=0)] = 0
    cpu_seconds: Annotated[float, Field(ge=0)] | None = None
    peak_memory_gb: Annotated[float, Field(ge=0)] | None = None
    copper_produced_per_second: Annotated[float, Field(ge=0)] = 0
    connections_resolved_per_pass: Annotated[float, Field(ge=0)] = 0
    diagnostic_only: bool = False
    applicable: bool = True


class RoutingBatchComparison(FrozenModel):
    """Sanitized comparison of one batch against an identical board baseline."""

    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    requested_net_count: Annotated[int, Field(ge=0)]
    requested_net_role_counts: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    best_open_connection_delta: int | None = None
    best_pass_number: Annotated[int, Field(ge=1)] | None = None
    duration_seconds: Annotated[float, Field(ge=0)] = 0
    copper_produced_per_second: Annotated[float, Field(ge=0)] = 0
    connections_resolved_per_pass: Annotated[float, Field(ge=0)] = 0


class ConnectivityMetricRunSummary(FrozenModel):
    """Sanitized optimization view over the records emitted for one routing run."""

    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    record_count: Annotated[int, Field(ge=1)]
    records: Annotated[tuple[ConnectivityMetricRecord, ...], Field(min_length=1)]
    best_strategy: Literal["prioritized", "sequential", "prioritized_single_thread"] | None = None
    best_open_connection_delta: int | None = None
    comparable_candidate_count: Annotated[int, Field(ge=0)] = 0
    failed_candidate_count: Annotated[int, Field(ge=0)] = 0
    best_observed_pass_number: Annotated[int, Field(ge=1)] | None = None
    highest_stagnation_count: Annotated[int, Field(ge=0)] = 0
    watchdog_reasons: tuple[str, ...] = ()
    recommended_max_passes: Annotated[int, Field(ge=1, le=200)] | None = None
    same_baseline_batches: tuple[RoutingBatchComparison, ...] = ()


class RoutingCandidateEvaluation(FrozenModel):
    """Deterministic evidence used to rank one external autorouter result."""

    strategy: Literal["prioritized", "sequential", "prioritized_single_thread"]
    selected: bool = False
    complete: bool
    unrouted_connection_count: Annotated[int, Field(ge=0)]
    drc_available: bool
    new_drc_error_count: Annotated[int, Field(ge=0)] | None = None
    segment_count: Annotated[int, Field(ge=0)]
    via_count: Annotated[int, Field(ge=0)]
    track_length_mm: Annotated[float, Field(ge=0)]
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    duplicate_of: Literal["prioritized", "sequential", "prioritized_single_thread"] | None = None
    backend_elapsed_seconds: Annotated[float, Field(ge=0)] = 0
    freerouting_pass_metrics: tuple[FreeRoutingPassMetric, ...] = ()
    freerouting_normalization_count: Annotated[int, Field(ge=0)] = 0
    applicable: bool = True
    diagnostic_only: bool = False
    failure_reason: str | None = None
    copper_produced_per_second: Annotated[float, Field(ge=0)] = 0
    connections_resolved_per_pass: Annotated[float, Field(ge=0)] = 0


class RoutingHotspot(FrozenModel):
    """Local congestion evidence suitable for a placement rework proposal."""

    references: Annotated[tuple[str, ...], Field(min_length=1, max_length=12)]
    connection_count: Annotated[int, Field(ge=1)]
    total_airwire_length_mm: Annotated[float, Field(gt=0)]
    center_x_mm: float
    center_y_mm: float
    radius_mm: Annotated[float, Field(gt=0)]
    recommendation: str


class RoutingBackendStatus(FrozenModel):
    """Availability of the fixed-command local PCB autorouting backend."""

    name: Literal["FreeRouting"] = "FreeRouting"
    available: bool
    version: str | None = None
    java_major_version: Annotated[int, Field(ge=1)] | None = None
    java_path: Path | None = None
    jar_path: Path | None = None
    kicad_python_path: Path | None = None
    scoped_routing_supported: bool = False
    capability_path: Path | None = None
    capability_reason: str | None = None
    reason: str | None = None


class FreeRoutingCapabilityRecord(FrozenModel):
    """Hash-bound claims for behavior not discoverable from the FreeRouting version alone."""

    schema_version: Literal[1] = 1
    jar_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scoped_net_classes_cli: bool = False
    source_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    description: str | None = None


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
    metrics_run_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    candidate_evaluations: tuple[RoutingCandidateEvaluation, ...] = ()
    routing_hotspots: tuple[RoutingHotspot, ...] = ()
    placement_rework_recommended: bool = False
    recommended_max_passes: Annotated[int, Field(ge=1, le=200)] | None = None
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
    estimated_wire_length_mm: Annotated[float, Field(ge=0)] = 0
    placement_area_mm2: Annotated[float, Field(ge=0)] = 0
    compactness_percent: Annotated[float, Field(ge=0, le=100)] = 0
    cross_layer_net_count: Annotated[int, Field(ge=0)] = 0
    top_footprint_count: Annotated[int, Field(ge=0)] = 0
    bottom_footprint_count: Annotated[int, Field(ge=0)] = 0
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


class PcbPhaseRequest(FrozenModel):
    """One aggregate PCB recipe covered by the final PCB acceptance gate."""

    placement_operations: tuple[PlacementOperation, ...] = ()
    grounding: GroundingRequest = Field(default_factory=GroundingRequest)
    routing_batches: Annotated[tuple[RoutingRequest, ...], Field(min_length=1)]
    require_board_complete: bool = True


class PcbPhaseChangeSet(FrozenModel):
    """Validated placement, grounding, and routing composed in one private workspace."""

    id: str
    session_id: str
    project_hash: str
    request: PcbPhaseRequest
    affected_files: tuple[Path, ...]
    source_hashes: dict[str, str]
    child_change_set_ids: tuple[str, ...]
    metrics_run_ids: tuple[str, ...] = ()
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


class PcbPhaseChangeRecord(FrozenModel):
    """Private restart-safe state for an aggregate PCB acceptance."""

    project_root: Path
    workspace: Path
    affected_relative_files: tuple[Path, ...]
    change_set: PcbPhaseChangeSet
    snapshot: Path | None = None


class PlacementRequest(FrozenModel):
    references: Annotated[tuple[str, ...], Field(min_length=1)]
    strategy: Literal["grid", "compact", "routing_coherent"] = "compact"
    existing_copper_policy: Literal["ignore", "preserve_anchors"] = "ignore"
    anchor_ground_copper: bool = True
    region: PcbBounds | None = None
    spacing_mm: Annotated[float, Field(ge=0)] = 1.0
    routing_corridor_mm: Annotated[float, Field(ge=0)] = 0.8
    power_corridor_mm: Annotated[float, Field(ge=0)] = 2.0
    grid_mm: Annotated[float, Field(gt=0)] = 0.5
    rotation_deg: float = 0
    rotation_policy: Literal["fixed", "orthogonal_auto"] = "orthogonal_auto"
    layer_policy: Literal["preserve", "auto", "front", "back"] = "auto"

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


class PcbPlacementChangeRecord(FrozenModel):
    """Private restart-safe state for a prepared PCB-placement mutation."""

    project_root: Path
    workspace: Path
    affected_relative_files: tuple[Path, ...]
    change_set: PcbPlacementChangeSet
    snapshot: Path | None = None


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


class PcbLayoutChangeRecord(FrozenModel):
    """Private restart-safe envelope for a prepared PCB layout."""

    schema_version: Literal[1] = 1
    project_root: Path
    workspace: Path
    affected_relative_files: Annotated[tuple[Path, ...], Field(min_length=1)]
    change_set: PcbLayoutChangeSet
    snapshot: Path | None = None

    @field_validator("affected_relative_files")
    @classmethod
    def relative_layout_files(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        if any(path.is_absolute() or ".." in path.parts for path in value):
            raise ValueError("affected layout files must be project-relative")
        return value


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
