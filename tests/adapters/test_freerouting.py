"""FreeRouting discovery and fixed-command adapter tests."""

import shutil
import subprocess
from pathlib import Path

import pytest

from copperbrain.adapters.freerouting import FreeRoutingAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import RoutingRequest

FIXTURE = Path("tests/fixtures/kicad10_placement/placement.kicad_pcb")


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
    pcb = tmp_path / "input.kicad_pcb"
    shutil.copy2(FIXTURE, pcb)
    commands: list[list[str]] = []
    invocations: list[dict[str, object]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        invocations.append(kwargs)
        if "export" in command:
            Path(command[-1]).write_text("(pcb Ω-board\n)\n", encoding="utf-8")
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
        RoutingRequest(max_passes=7, thread_count=2),
        "prioritized",
    )
    assert candidate.pcb.is_file()
    assert all(isinstance(command, list) for command in commands)
    assert all(invocation["stdin"] is subprocess.DEVNULL for invocation in invocations)
    router = commands[1]
    assert router[:3] == [str(java.resolve()), "-jar", str(jar.resolve())]
    assert router[router.index("-mp") + 1] == "7"
    assert router[router.index("-mt") + 1] == "2"
    assert "--gui.enabled=false" in router
    assert "Ω" not in (tmp_path / "candidate" / "freerouting-input.dsn").read_text(encoding="utf-8")


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
