"""Managed KiCadRoutingTools adapter tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from copperbrain.adapters.kicad_routing_tools import KiCadRoutingToolsAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import RoutingRequest


def _runtime(root: Path, version: str = "0.18.2") -> Path:
    runtime = root / "integrations" / "kicad-routing-tools" / version / "plugins"
    (runtime / "rust_router").mkdir(parents=True)
    (runtime / "route.py").write_text("# fixture\n", encoding="utf-8")
    (runtime / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    extension = "grid_router.pyd" if os.name == "nt" else "grid_router.so"
    (runtime / "rust_router" / extension).write_bytes(b"fixture")
    return runtime


def test_discovery_selects_newest_managed_runtime(tmp_path: Path) -> None:
    older = _runtime(tmp_path, "0.17.0")
    newest = _runtime(tmp_path, "0.18.2")

    adapter = KiCadRoutingToolsAdapter.discover(tmp_path)

    assert adapter.runtime_root == newest
    assert adapter.runtime_root != older


def test_status_reports_complete_fixed_runtime(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    adapter = KiCadRoutingToolsAdapter(
        runtime_root=runtime,
        python_path=Path(sys.executable),
        kicad_python_path=Path(sys.executable),
    )

    status = adapter.status()

    assert status.available
    assert status.name == "KiCadRoutingTools"
    assert status.version == "0.18.2"
    assert status.runtime_root == runtime.resolve()
    assert status.rust_core_path is not None
    assert status.scoped_routing_supported


def test_route_uses_bounded_exact_net_command_and_sanitizes_output(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    source = tmp_path / "source.kicad_pcb"
    source.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    commands: list[list[str]] = []

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        output = Path(command[3])
        shutil.copy2(Path(command[2]), output)
        return subprocess.CompletedProcess(
            command,
            0,
            "Routing complete\n  Single-ended:  1/1 routed\n  Total time:    0.25s\nsecret-net\n",
            "",
        )

    adapter = KiCadRoutingToolsAdapter(
        runtime_root=runtime,
        python_path=Path(sys.executable),
        kicad_python_path=Path(sys.executable),
        runner=runner,
    )
    request = RoutingRequest(nets=("secret-net", "-literal*?[net]"))

    candidate = adapter.route(source, tmp_path / "candidate", request, "mps")

    assert candidate.pcb.is_file()
    assert candidate.pass_metrics[0].connections_resolved == 1
    assert "secret-net" not in candidate.stdout_tail
    assert "<net:" in candidate.stdout_tail
    command = commands[0]
    nets_index = command.index("--nets")
    assert command[nets_index + 1 : nets_index + 3] == [
        "secret-net",
        "[-]literal[*][?][[]net]",
    ]
    assert command[command.index("--ordering") + 1] == "mps"
    assert command[command.index("--max-iterations") + 1] == "200000"
    assert "--no-fix-drc-settings" in command
    assert "--rip-existing-nets" not in command


def test_route_failure_flushes_structured_metrics(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    source = tmp_path / "source.kicad_pcb"
    source.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            2,
            "Routing complete\n  Single-ended:  0/1 routed (1 FAILED)\n"
            "  Total time:    0.10s\nsecret-net\n",
            "failure",
        )

    adapter = KiCadRoutingToolsAdapter(
        runtime_root=runtime,
        python_path=Path(sys.executable),
        kicad_python_path=Path(sys.executable),
        runner=runner,
    )

    with pytest.raises(CopperbrainError) as caught:
        adapter.route(
            source,
            tmp_path / "candidate",
            RoutingRequest(nets=("secret-net",)),
            "mps",
        )

    details = caught.value.error.details
    assert details["routing_pass_metrics"][0]["failure_count"] == 1
    assert "secret-net" not in str(details["reason"])


def test_candidate_count_selects_three_kicad_orderings(tmp_path: Path) -> None:
    adapter = KiCadRoutingToolsAdapter(
        runtime_root=_runtime(tmp_path),
        python_path=Path(sys.executable),
        kicad_python_path=Path(sys.executable),
    )

    assert adapter.strategies(RoutingRequest(candidate_count=3)) == (
        "mps",
        "inside_out",
        "original",
    )


def test_adapter_refuses_an_empty_backend_scope(tmp_path: Path) -> None:
    adapter = KiCadRoutingToolsAdapter(
        runtime_root=_runtime(tmp_path),
        python_path=Path(sys.executable),
        kicad_python_path=Path(sys.executable),
    )
    source = tmp_path / "source.kicad_pcb"
    source.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")

    with pytest.raises(CopperbrainError, match="explicit nonempty"):
        adapter.route(source, tmp_path / "candidate", RoutingRequest(), "mps")


def test_wall_time_watchdog_terminates_router_process(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    (runtime / "route.py").write_text(
        "import time\nprint('started', flush=True)\ntime.sleep(10)\n",
        encoding="utf-8",
    )
    source = tmp_path / "source.kicad_pcb"
    source.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    adapter = KiCadRoutingToolsAdapter(
        runtime_root=runtime,
        python_path=Path(sys.executable),
        kicad_python_path=Path(sys.executable),
        timeout_seconds=0.2,
        stall_seconds=5,
        poll_interval_seconds=0.01,
    )

    with pytest.raises(CopperbrainError) as caught:
        adapter.route(
            source,
            tmp_path / "candidate",
            RoutingRequest(nets=("signal",)),
            "mps",
        )

    assert caught.value.error.details["watchdog"] == "wall_time_budget_exceeded"


def test_output_stall_watchdog_terminates_router_process(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    (runtime / "route.py").write_text(
        "import time\nprint('started', flush=True)\ntime.sleep(10)\n",
        encoding="utf-8",
    )
    source = tmp_path / "source.kicad_pcb"
    source.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    adapter = KiCadRoutingToolsAdapter(
        runtime_root=runtime,
        python_path=Path(sys.executable),
        kicad_python_path=Path(sys.executable),
        timeout_seconds=5,
        stall_seconds=0.2,
        poll_interval_seconds=0.01,
    )

    with pytest.raises(CopperbrainError) as caught:
        adapter.route(
            source,
            tmp_path / "candidate",
            RoutingRequest(nets=("signal",)),
            "mps",
        )

    assert caught.value.error.details["watchdog"] == "output_stall_budget_exceeded"
