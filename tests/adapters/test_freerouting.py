"""FreeRouting discovery and fixed-command adapter tests."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from copperbrain.adapters.freerouting import FreeRoutingAdapter, _FreeRoutingProgress
from copperbrain.errors import CopperbrainError
from copperbrain.models import RoutingRequest

FIXTURE = Path("tests/fixtures/kicad10_placement/placement.kicad_pcb")


def _write_scoped_capability(jar: Path) -> Path:
    path = jar.with_name(f"{jar.name}.capabilities.json")
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "jar_sha256": hashlib.sha256(jar.read_bytes()).hexdigest(),
                "scoped_net_classes_cli": True,
                "source_commit": "20f1a72e546b9b23c7ba5127086885cfacbdd4be",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_status_reports_every_missing_runtime() -> None:
    status = FreeRoutingAdapter(
        jar_path=None,
        java_path=None,
        kicad_python_path=None,
    ).status()
    assert not status.available
    assert status.reason is not None
    assert "Java runtime" in status.reason
    assert "FreeRouting JAR" in status.reason
    assert "KiCad Python runtime" in status.reason


def test_route_uses_only_fixed_argument_lists(tmp_path: Path) -> None:
    java = tmp_path / "java.exe"
    jar = tmp_path / "freerouting-2.2.4.jar"
    kicad_python = tmp_path / "python.exe"
    for executable in (java, jar, kicad_python):
        executable.write_bytes(b"fixture")
    capability = _write_scoped_capability(jar)
    pcb = tmp_path / "input.kicad_pcb"
    shutil.copy2(FIXTURE, pcb)
    commands: list[list[str]] = []
    invocations: list[dict[str, object]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        invocations.append(kwargs)
        if "export" in command:
            Path(command[-1]).write_text(
                """(pcb Ω-board
  (structure
    (plane /DROP (polygon F.Cu 0 0 0 100 0 100 100 0 100))
  )
  (network
    (net /KEEP (pins U1-1 U2-1))
    (net /DROP (pins U1-2 U2-2))
    (class default /KEEP /DROP (rule (width 200) (clearance 200)))
  )
  (wiring
    (wire (path F.Cu 200 0 0 10 0 10 10)(net /DROP)(type route))
  )
)
""",
                encoding="utf-8",
            )
        elif "-do" in command:
            Path(command[command.index("-do") + 1]).write_text("(session routed)\n")
        elif "import" in command:
            shutil.copy2(command[-3], command[-1])
        return subprocess.CompletedProcess(command, 0, "ok", "")

    adapter = FreeRoutingAdapter(
        jar_path=jar,
        java_path=java,
        java_major_version=25,
        kicad_python_path=kicad_python,
        runner=runner,
    )
    candidate = adapter.route(
        pcb,
        tmp_path / "candidate",
        RoutingRequest(
            nets=("/KEEP",),
            excluded_plane_nets=("/DROP",),
            max_passes=7,
            thread_count=2,
        ),
        "prioritized_single_thread",
    )
    adapter.refill_zones(pcb)
    assert candidate.pcb.is_file()
    assert all(isinstance(command, list) for command in commands)
    assert all(invocation["stdin"] is subprocess.DEVNULL for invocation in invocations)
    router = commands[1]
    assert router[:3] == [str(java.resolve()), "-jar", str(jar.resolve())]
    assert router[router.index("-mp") + 1] == "7"
    assert router[router.index("-mt") + 1] == "1"
    assert router[router.index("-us") + 1] == "prioritized"
    assert router[router.index("-inc") + 1] == "__copperbrain_preserve_1"
    assert "--gui.enabled=false" in router
    dsn = (tmp_path / "candidate" / "freerouting-input.dsn").read_text(encoding="utf-8")
    assert "Ω" not in dsn
    assert "(net /KEEP" in dsn
    assert "(net /DROP" in dsn
    assert "(plane /DROP" not in dsn
    assert "(class default /KEEP" in dsn
    assert "(class __copperbrain_preserve_1 /DROP" in dsn
    assert "(path F.Cu 200 0 0 10 0 10 10)" not in dsn
    assert "(wire (path F.Cu 200 0 0 10 0)(net /DROP)(type route))" in dsn
    assert "(wire (path F.Cu 200 10 0 10 10)(net /DROP)(type route))" in dsn
    status = adapter.status()
    assert status.scoped_routing_supported
    assert status.capability_path == capability
    assert commands[-1][2:4] == ["refill", str(pcb)]
    assert FreeRoutingAdapter.strategies(RoutingRequest()) == ("prioritized",)
    assert FreeRoutingAdapter.strategies(RoutingRequest(candidate_count=3)) == (
        "prioritized",
        "sequential",
        "prioritized_single_thread",
    )


def test_route_refuses_scoped_headless_freerouting(tmp_path: Path) -> None:
    java = tmp_path / "java.exe"
    jar = tmp_path / "freerouting-2.2.4.jar"
    kicad_python = tmp_path / "python.exe"
    for executable in (java, jar, kicad_python):
        executable.write_bytes(b"fixture")
    pcb = tmp_path / "input.kicad_pcb"
    shutil.copy2(FIXTURE, pcb)
    commands: list[list[str]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "export" in command:
            Path(command[-1]).write_text(
                """(pcb board
  (structure (plane /DROP (polygon F.Cu 0 0 0 100 0 100 100 0 100)))
  (network
    (net /KEEP (pins U1-1 U2-1))
    (net /DROP (pins U1-2 U2-2))
    (class default /KEEP /DROP (rule (width 200) (clearance 200)))
  )
  (wiring)
)
""",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, "ok", "")

    adapter = FreeRoutingAdapter(
        jar_path=jar,
        java_path=java,
        java_major_version=25,
        kicad_python_path=kicad_python,
        runner=runner,
    )

    with pytest.raises(CopperbrainError, match="cannot safely honor") as caught:
        adapter.route(
            pcb,
            tmp_path / "candidate",
            RoutingRequest(nets=("/KEEP",)),
            "prioritized",
        )

    assert len(commands) == 1
    assert caught.value.error.details["preserve_class_count"] == 1
    dsn = (tmp_path / "candidate" / "freerouting-input.dsn").read_text(encoding="utf-8")
    assert "(net /DROP" in dsn
    assert "(plane /DROP" in dsn
    assert "(class __copperbrain_preserve_1 /DROP" in dsn


def test_status_rejects_capability_record_after_jar_changes(tmp_path: Path) -> None:
    java = tmp_path / "java.exe"
    jar = tmp_path / "freerouting-2.2.4.jar"
    kicad_python = tmp_path / "python.exe"
    for executable in (java, jar, kicad_python):
        executable.write_bytes(b"fixture")
    capability = _write_scoped_capability(jar)
    jar.write_bytes(b"changed fixture")

    status = FreeRoutingAdapter(
        jar_path=jar,
        java_path=java,
        java_major_version=25,
        kicad_python_path=kicad_python,
    ).status()

    assert not status.scoped_routing_supported
    assert status.capability_path == capability
    assert status.capability_reason == "Capability record hash does not match the selected JAR"


def test_route_refuses_requested_net_missing_from_dsn(tmp_path: Path) -> None:
    java = tmp_path / "java.exe"
    jar = tmp_path / "freerouting-2.2.4.jar"
    kicad_python = tmp_path / "python.exe"
    for executable in (java, jar, kicad_python):
        executable.write_bytes(b"fixture")
    pcb = tmp_path / "input.kicad_pcb"
    shutil.copy2(FIXTURE, pcb)

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "export" in command:
            Path(command[-1]).write_text(
                "(pcb board\n  (network (net /KEEP (pins U1-1 U2-1)))\n  (wiring)\n)\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, "ok", "")

    adapter = FreeRoutingAdapter(
        jar_path=jar,
        java_path=java,
        java_major_version=25,
        kicad_python_path=kicad_python,
        runner=runner,
    )
    with pytest.raises(CopperbrainError, match="absent") as caught:
        adapter.route(
            pcb,
            tmp_path / "candidate",
            RoutingRequest(nets=("/MISSING",)),
            "prioritized",
        )
    assert caught.value.error.details["missing_nets"] == ["/MISSING"]


def test_route_refuses_unavailable_backend(tmp_path: Path) -> None:
    with pytest.raises(CopperbrainError, match="unavailable"):
        FreeRoutingAdapter(
            jar_path=None,
            java_path=None,
            kicad_python_path=None,
        ).route(
            FIXTURE,
            tmp_path / "candidate",
            RoutingRequest(),
            "prioritized",
        )


def test_status_rejects_java_older_than_required(tmp_path: Path) -> None:
    java = tmp_path / "java.exe"
    jar = tmp_path / "freerouting-2.2.4.jar"
    kicad_python = tmp_path / "python.exe"
    for executable in (java, jar, kicad_python):
        executable.write_bytes(b"fixture")
    status = FreeRoutingAdapter(
        jar_path=jar,
        java_path=java,
        java_major_version=17,
        kicad_python_path=kicad_python,
    ).status()
    assert not status.available
    assert status.reason is not None
    assert "Java 25+ runtime (found 17)" in status.reason


def test_watchdog_stops_known_normalization_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    java = tmp_path / "java.exe"
    jar = tmp_path / "freerouting-2.2.4.jar"
    kicad_python = tmp_path / "python.exe"
    for executable in (java, jar, kicad_python):
        executable.write_bytes(b"fixture")
    pcb = tmp_path / "input.kicad_pcb"
    shutil.copy2(FIXTURE, pcb)

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "export" in command:
            Path(command[-1]).write_text("(pcb board\n)\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    class FakeProcess:
        pid = 123
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def communicate(self) -> tuple[str, str]:
            return "", "normalization loop"

    process = FakeProcess()

    def process_factory(command: list[str], **kwargs: object) -> FakeProcess:
        cwd = Path(str(kwargs["cwd"]))
        (cwd / "freerouting.log").write_text(
            ("PolylineTrace.normalize: max normalization depth (16)\n" * 5),
            encoding="utf-8",
        )
        return process

    adapter = FreeRoutingAdapter(
        jar_path=jar,
        java_path=java,
        java_major_version=25,
        kicad_python_path=kicad_python,
        runner=runner,
        process_factory=process_factory,  # type: ignore[arg-type]
        normalization_limit=3,
        poll_interval_seconds=0.001,
    )

    def stop(fake: FakeProcess) -> None:
        fake.returncode = -1

    monkeypatch.setattr(adapter, "_terminate_process_tree", stop)
    with pytest.raises(CopperbrainError, match="watchdog") as caught:
        adapter.route(pcb, tmp_path / "candidate", RoutingRequest(), "prioritized")
    assert caught.value.error.details["watchdog"] == "normalization_loop"
    assert caught.value.error.details["freerouting_normalization_count"] == 5


def test_route_collects_bounded_freerouting_pass_metrics(tmp_path: Path) -> None:
    java = tmp_path / "java.exe"
    jar = tmp_path / "freerouting-2.2.4.jar"
    kicad_python = tmp_path / "python.exe"
    for executable in (java, jar, kicad_python):
        executable.write_bytes(b"fixture")
    pcb = tmp_path / "input.kicad_pcb"
    shutil.copy2(FIXTURE, pcb)

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "export" in command:
            Path(command[-1]).write_text("(pcb board\n)\n", encoding="utf-8")
        elif "import" in command:
            shutil.copy2(command[-3], command[-1])
        return subprocess.CompletedProcess(command, 0, "", "")

    class CompletedProcess:
        pid = 123
        returncode = 0

        def poll(self) -> int:
            return 0

        def communicate(self) -> tuple[str, str]:
            return "", ""

    def process_factory(command: list[str], **kwargs: object) -> CompletedProcess:
        cwd = Path(str(kwargs["cwd"]))
        (cwd / "freerouting-output.ses").write_text("(session routed)\n", encoding="utf-8")
        (cwd / "freerouting.log").write_text(
            "Pass #1: 12 incompletes across 20 items to route\n"
            "Pass #1: Failed to route Pin on net '/N' (1 items remaining, 1 failures). "
            "State: FAILED\n"
            "Auto-router pass #1 on board 'hash' was completed in 1.25 seconds with the "
            "score of 8.5 (3 unrouted), using 1.10 CPU seconds and the job allocated "
            "0.50 GB of memory so far.\n",
            encoding="utf-8",
        )
        return CompletedProcess()

    adapter = FreeRoutingAdapter(
        jar_path=jar,
        java_path=java,
        java_major_version=25,
        kicad_python_path=kicad_python,
        runner=runner,
        process_factory=process_factory,  # type: ignore[arg-type]
    )

    candidate = adapter.route(pcb, tmp_path / "candidate", RoutingRequest(), "prioritized")

    assert len(candidate.pass_metrics) == 1
    metric = candidate.pass_metrics[0]
    assert metric.pass_number == 1
    assert metric.board_incomplete_count == 12
    assert metric.queued_item_count == 20
    assert metric.board_unrouted_count == 3
    assert metric.failure_count == 1
    assert metric.duration_seconds == 1.25
    assert metric.cpu_seconds == 1.1
    assert metric.allocated_memory_gb == 0.5


def test_semantic_watchdog_counts_completed_passes_without_improvement() -> None:
    progress = _FreeRoutingProgress()
    progress.feed(
        "Pass #1: 10 incompletes across 10 items to route\n"
        "Auto-router pass #1 on board 'x' was completed in 1 seconds with the score of "
        "1 (8 unrouted), using 1 CPU seconds and the job allocated 1 GB\n"
        "Pass #2: 8 incompletes across 8 items to route\n"
        "Auto-router pass #2 on board 'x' was completed in 1 seconds with the score of "
        "1 (8 unrouted), using 1 CPU seconds and the job allocated 1 GB\n"
        "Pass #3: 8 incompletes across 8 items to route\n"
        "Auto-router pass #3 on board 'x' was completed in 1 seconds with the score of "
        "1 (8 unrouted), using 1 CPU seconds and the job allocated 1 GB\n"
        "Pass #4: 8 incompletes across 8 items to route\n"
        "Auto-router pass #4 on board 'x' was completed in 1 seconds with the score of "
        "1 (8 unrouted), using 1 CPU seconds and the job allocated 1 GB\n",
        final=True,
    )

    assert progress.semantic_stagnation_streak() == 3
    assert [item.connections_resolved for item in progress.metrics()] == [2, 0, 0, 0]


def _progress_for_passes(passes: list[tuple[int, float | None]]) -> _FreeRoutingProgress:
    lines: list[str] = []
    for number, (unrouted, score) in enumerate(passes, start=1):
        lines.append(f"Pass #{number}: {unrouted} incompletes across 10 items to route\n")
        score_text = "-1" if score is None else str(score)
        lines.append(
            f"Auto-router pass #{number} on board 'x' was completed in 1 seconds with "
            f"the score of {score_text} ({unrouted} unrouted), using 1 CPU seconds and "
            "the job allocated 1 GB\n"
        )
    progress = _FreeRoutingProgress()
    progress.feed("".join(lines), final=True)
    return progress


def test_semantic_watchdog_ignores_flat_opens_with_improving_score() -> None:
    progress = _progress_for_passes([(8, 10.0), (8, 9.0), (8, 8.5), (8, 8.0)])

    assert progress.semantic_stagnation_streak() == 0


def test_semantic_watchdog_counts_flat_opens_with_flat_score() -> None:
    progress = _progress_for_passes([(8, 10.0), (8, 10.0), (8, 10.0)])

    assert progress.semantic_stagnation_streak() == 2


def test_semantic_watchdog_never_counts_completed_board_passes() -> None:
    progress = _progress_for_passes([(8, 10.0), (0, 4.0), (0, 5.0), (0, 5.0)])

    assert progress.semantic_stagnation_streak() == 0


def test_semantic_watchdog_counts_flat_opens_when_score_stays_flat() -> None:
    progress = _FreeRoutingProgress()
    progress.feed(
        "Pass #1: 10 incompletes across 10 items to route\n"
        "Pass #2: 8 incompletes across 8 items to route\n"
        "Pass #3: 8 incompletes across 8 items to route\n",
        final=True,
    )

    # No completed-pass lines: board_unrouted_count is unknown, so nothing counts.
    assert progress.semantic_stagnation_streak() == 0


def test_routing_request_defaults_allow_longer_autorouter_progress() -> None:
    request = RoutingRequest()

    assert request.semantic_stagnation_passes == 8
    assert request.candidate_count == 1
    assert (
        FreeRoutingAdapter(
            jar_path=None,
            java_path=None,
            kicad_python_path=None,
        ).semantic_stagnation_passes
        == 8
    )
