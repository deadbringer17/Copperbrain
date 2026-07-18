import shutil
import uuid
from pathlib import Path
from typing import cast

import pytest

from copperbrain.adapters.routing_backend import RoutingBackend
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeStatus,
    DrcReport,
    PcbPhaseChangeSet,
    PcbPhaseRequest,
    RoutingRequest,
    ValidationReport,
)
from copperbrain.services.outputs import publish_preview
from copperbrain.services.pcb_phase import PcbPhaseService, _PreparedPcbPhase
from copperbrain.services.projects import ProjectService, aggregate_hash

FIXTURE = Path(__file__).parents[1] / "fixtures" / "kicad10_placement"
PROJECT_METADATA = Path(__file__).parents[1] / "fixtures" / "kicad10_minimal" / "demo.kicad_pro"
SCHEMATIC = Path(__file__).parents[1] / "fixtures" / "kicad10_minimal" / "demo.kicad_sch"


def test_aggregate_pcb_acceptance_is_atomic_persistent_and_reversible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "live"
    shutil.copytree(FIXTURE, project_root)
    shutil.copy2(PROJECT_METADATA, project_root / "placement.kicad_pro")
    shutil.copy2(SCHEMATIC, project_root / "placement.kicad_sch")
    projects = ProjectService()
    session = projects.open_project(project_root)
    assert session.pcb_file is not None
    original = session.pcb_file.read_bytes()

    data_dir = tmp_path / "private"
    identifier = uuid.uuid4().hex
    workspace = data_dir / "workspaces" / identifier
    shutil.copytree(project_root, workspace)
    workspace_pcb = workspace / session.pcb_file.relative_to(session.root)
    workspace_pcb.write_bytes(original + b"\n")

    backend = cast(RoutingBackend, object())
    service = PcbPhaseService(projects, data_dir, backend)
    change_set = PcbPhaseChangeSet(
        id=identifier,
        session_id=session.id,
        project_hash=aggregate_hash(session.hashes),
        request=PcbPhaseRequest(
            routing_batches=(RoutingRequest(nets=("/SIG",), require_complete=False),),
            require_board_complete=False,
        ),
        affected_files=(session.pcb_file,),
        source_hashes=session.hashes,
        child_change_set_ids=(uuid.uuid4().hex,),
        metrics_run_ids=(uuid.uuid4().hex,),
        semantic_diff=("Composed grounding and one routing batch",),
        risks=("Engineering review remains required",),
        validation_report=ValidationReport(valid=True),
        drc=DrcReport(available=True),
        routing_analysis=service.adapter.analyze_routing(session.pcb_file, session.id),
        preview_directory=project_root / "copperbrain-output" / "previews" / "pcb",
        status=ChangeStatus.VALIDATED,
    )
    prepared = _PreparedPcbPhase(change_set=change_set, workspace=workspace)
    service._changes[identifier] = prepared
    service._persist(prepared, project_root)
    publish_preview(workspace, project_root, identifier, phase="pcb")

    with pytest.raises(CopperbrainError, match="acceptance"):
        service.apply(identifier, confirmed=False, editor_closed=True)
    assert session.pcb_file.read_bytes() == original

    monkeypatch.setattr(service, "validate", lambda _identifier: ValidationReport(valid=True))
    applied = service.apply(identifier, confirmed=True, editor_closed=True)
    assert applied.status is ChangeStatus.APPLIED
    assert session.pcb_file.read_bytes() == original + b"\n"

    restarted_projects = ProjectService()
    restarted = PcbPhaseService(restarted_projects, data_dir, backend)
    rolled_back = restarted.rollback(identifier, confirmed=True, editor_closed=True)
    assert rolled_back.status is ChangeStatus.ROLLED_BACK
    assert session.pcb_file.read_bytes() == original
