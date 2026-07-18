"""Managed fixed-command adapter for the KiCadRoutingTools A* router."""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.adapters.routing_backend import (
    ROUTING_STRATEGIES,
    RoutedBoardCandidate,
    RoutingStrategy,
)
from copperbrain.errors import CopperbrainError
from copperbrain.models import ErrorCode, RoutingBackendStatus, RoutingPassMetric, RoutingRequest

_MAX_OUTPUT_BYTES = 100_000_000
_TAIL_CHARACTERS = 16_000
_SUMMARY = re.compile(
    r"Single-ended:\s+(?P<routed>\d+)/(?P<total>\d+) routed"
    r"(?:\s+\((?P<failed>\d+) FAILED\))?",
    re.IGNORECASE,
)
_TOTAL_TIME = re.compile(r"Total time:\s+(?P<seconds>\d+(?:\.\d+)?)s", re.IGNORECASE)
_MEMORY = re.compile(r"(?:Peak|Final|After).*?memory.*?(?P<mb>\d+(?:\.\d+)?)\s*MB", re.I)


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value))


def _runtime_version(root: Path | None) -> str | None:
    if root is None:
        return None
    version_file = root / "VERSION"
    if not version_file.is_file():
        return None
    value = version_file.read_text(encoding="utf-8", errors="replace").strip()
    return value or None


def _candidate_roots(data_dir: Path, explicit: Path | None) -> tuple[Path, ...]:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.extend((explicit, explicit / "plugins"))
    managed = data_dir / "integrations" / "kicad-routing-tools"
    if managed.is_dir():
        candidates.extend(path for path in managed.iterdir() if path.is_dir())
        candidates.extend(path / "plugins" for path in managed.iterdir() if path.is_dir())
        candidates.extend((managed, managed / "plugins"))
    valid = {
        path.resolve()
        for path in candidates
        if (path / "route.py").is_file() and (path / "VERSION").is_file()
    }
    return tuple(
        sorted(
            valid,
            key=lambda path: (_version_key(_runtime_version(path) or "0"), str(path).lower()),
            reverse=True,
        )
    )


def _canonical_rust_core(root: Path | None) -> Path | None:
    if root is None:
        return None
    rust = root / "rust_router"
    canonical = rust / ("grid_router.pyd" if os.name == "nt" else "grid_router.so")
    return canonical if canonical.is_file() else None


def _kicad_python() -> Path | None:
    cli = detect_kicad().selected_cli
    if cli is None:
        return None
    names = ("python.exe",) if os.name == "nt" else ("python3", "python")
    return next((cli.parent / name for name in names if (cli.parent / name).is_file()), None)


def _dependencies_available() -> tuple[bool, str | None]:
    missing: list[str] = []
    for module in ("numpy", "scipy", "shapely"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    return (not missing, f"Missing Python packages: {', '.join(missing)}" if missing else None)


def _exact_net_pattern(net: str) -> str:
    """Escape one KiCad net name for KiCadRoutingTools' fnmatch selector."""
    pattern = net.replace("[", "[[]").replace("*", "[*]").replace("?", "[?]")
    return f"[-]{pattern[1:]}" if pattern.startswith("-") else pattern


class KiCadRoutingToolsAdapter:
    """Run a discovered managed KiCadRoutingTools release in an isolated workspace."""

    def __init__(
        self,
        *,
        runtime_root: Path | None,
        python_path: Path | None = None,
        kicad_python_path: Path | None = None,
        timeout_seconds: float = 900,
        stall_seconds: float = 180,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        self.runtime_root = runtime_root.resolve() if runtime_root is not None else None
        self.python_path = (python_path or Path(sys.executable)).resolve()
        self.kicad_python_path = (
            kicad_python_path.resolve() if kicad_python_path is not None else None
        )
        self.timeout_seconds = timeout_seconds
        self.stall_seconds = stall_seconds
        self.runner = runner
        self.poll_interval_seconds = poll_interval_seconds

    @classmethod
    def discover(
        cls,
        data_dir: Path,
        *,
        explicit_root: Path | None = None,
        explicit_python: Path | None = None,
        timeout_seconds: float = 900,
        stall_seconds: float = 180,
    ) -> KiCadRoutingToolsAdapter:
        roots = _candidate_roots(data_dir, explicit_root)
        return cls(
            runtime_root=roots[0] if roots else None,
            python_path=explicit_python,
            kicad_python_path=_kicad_python(),
            timeout_seconds=timeout_seconds,
            stall_seconds=stall_seconds,
        )

    def status(self) -> RoutingBackendStatus:
        missing: list[str] = []
        route_script = self.runtime_root / "route.py" if self.runtime_root else None
        rust_core = _canonical_rust_core(self.runtime_root)
        dependencies_ok, dependency_reason = _dependencies_available()
        if route_script is None or not route_script.is_file():
            missing.append("managed KiCadRoutingTools runtime")
        if rust_core is None:
            missing.append("platform-compatible Rust routing core")
        if not self.python_path.is_file():
            missing.append("Python runtime")
        if not dependencies_ok and dependency_reason is not None:
            missing.append(dependency_reason)
        if self.kicad_python_path is None or not self.kicad_python_path.is_file():
            missing.append("KiCad Python runtime")
        return RoutingBackendStatus(
            available=not missing,
            version=_runtime_version(self.runtime_root),
            runtime_root=self.runtime_root,
            python_path=self.python_path,
            rust_core_path=rust_core,
            kicad_python_path=self.kicad_python_path,
            reason=f"Missing: {', '.join(missing)}" if missing else None,
        )

    def strategies(self, request: RoutingRequest) -> tuple[RoutingStrategy, ...]:
        return ROUTING_STRATEGIES[: request.candidate_count]

    @staticmethod
    def _tail(value: str) -> str:
        return value[-_TAIL_CHARACTERS:]

    @staticmethod
    def _sanitize(value: str, nets: tuple[str, ...]) -> str:
        sanitized = value
        for net in sorted(nets, key=len, reverse=True):
            identifier = hashlib.sha256(net.encode("utf-8")).hexdigest()[:8]
            sanitized = sanitized.replace(net, f"<net:{identifier}>")
        return KiCadRoutingToolsAdapter._tail(sanitized)

    def _run_fixed(self, command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CopperbrainError(
                ErrorCode.INTEGRATION_UNAVAILABLE,
                "KiCadRoutingTools support command failed",
                details={"reason": str(exc)},
            ) from exc

    def refill_zones(self, pcb: Path) -> None:
        if self.kicad_python_path is None or not self.kicad_python_path.is_file():
            raise CopperbrainError(
                ErrorCode.INTEGRATION_UNAVAILABLE,
                "KiCad Python is unavailable for routed-board zone refill",
            )
        worker = Path(__file__).with_name("kicad_board_worker.py")
        result = self._run_fixed(
            [str(self.kicad_python_path), str(worker), "refill", str(pcb)], cwd=pcb.parent
        )
        if result.returncode != 0:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "KiCad failed to refill zones on the routed board",
                details={"reason": self._tail(result.stderr or result.stdout)},
            )

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            else:
                kill_process_group = getattr(os, "killpg", None)
                if kill_process_group is None:
                    process.terminate()
                else:
                    kill_process_group(process.pid, signal.SIGTERM)
        except (OSError, subprocess.TimeoutExpired):
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    def _run_router(
        self, command: list[str], *, cwd: Path
    ) -> tuple[subprocess.CompletedProcess[str], str | None]:
        if self.runner is not subprocess.run:
            return self._run_fixed(command, cwd=cwd), None
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                stdin=subprocess.DEVNULL,
                errors="replace",
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
                start_new_session=os.name != "nt",
            )
        except OSError as exc:
            raise CopperbrainError(
                ErrorCode.INTEGRATION_UNAVAILABLE,
                "KiCadRoutingTools execution failed",
                details={"reason": str(exc)},
            ) from exc

        stdout: deque[str] = deque(maxlen=512)
        stderr: deque[str] = deque(maxlen=512)
        activity = [time.monotonic()]

        def drain(stream: TextIO | None, target: deque[str]) -> None:
            if stream is None:
                return
            for line in stream:
                target.append(line[-4096:])
                activity[0] = time.monotonic()

        threads = (
            threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
        )
        for thread in threads:
            thread.start()
        started = time.monotonic()
        watchdog: str | None = None
        while process.poll() is None:
            now = time.monotonic()
            if now - started >= self.timeout_seconds:
                watchdog = "wall_time_budget_exceeded"
            elif now - activity[0] >= self.stall_seconds:
                watchdog = "output_stall_budget_exceeded"
            if watchdog is not None:
                self._terminate_process_tree(process)
                break
            time.sleep(self.poll_interval_seconds)
        for thread in threads:
            thread.join(timeout=2)
        return (
            subprocess.CompletedProcess(
                command,
                process.returncode if process.returncode is not None else -1,
                "".join(stdout),
                "".join(stderr),
            ),
            watchdog,
        )

    @staticmethod
    def _metrics(output: str, elapsed: float) -> tuple[RoutingPassMetric, ...]:
        summary = _SUMMARY.search(output)
        if summary is None:
            return ()
        total = int(summary.group("total"))
        routed = int(summary.group("routed"))
        duration = _TOTAL_TIME.search(output)
        memory = tuple(float(item.group("mb")) for item in _MEMORY.finditer(output))
        return (
            RoutingPassMetric(
                pass_number=1,
                queued_item_count=total,
                board_unrouted_count=max(0, total - routed),
                failure_count=max(0, total - routed),
                duration_seconds=(
                    float(duration.group("seconds")) if duration is not None else elapsed
                ),
                allocated_memory_gb=max(memory) / 1024 if memory else None,
                connections_resolved=routed,
                connections_resolved_per_pass=float(routed),
            ),
        )

    def route(
        self,
        pcb: Path,
        workspace: Path,
        request: RoutingRequest,
        strategy: RoutingStrategy,
    ) -> RoutedBoardCandidate:
        status = self.status()
        if not status.available:
            raise CopperbrainError(
                ErrorCode.INTEGRATION_UNAVAILABLE,
                "The local KiCadRoutingTools backend is unavailable",
                actionable_hint=(
                    "Run 'uv run python scripts/setup_dependencies.py' or set "
                    "COPPERBRAIN_KICAD_ROUTING_TOOLS_ROOT, then restart Copperbrain."
                ),
                details={"reason": status.reason or "unknown"},
            )
        if strategy not in ROUTING_STRATEGIES:
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "Unsupported routing strategy")
        if not request.nets:
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT,
                "KiCadRoutingTools requires an explicit nonempty routing net set",
            )
        if not pcb.is_file() or pcb.suffix.casefold() != ".kicad_pcb":
            raise CopperbrainError(ErrorCode.NOT_FOUND, "PCB input for routing was not found")
        workspace.mkdir(parents=True, exist_ok=False)
        routed_pcb = workspace / "kicad-routing-tools-routed.kicad_pcb"
        assert self.runtime_root is not None
        command = [
            str(self.python_path),
            str(self.runtime_root / "route.py"),
            str(pcb),
            str(routed_pcb),
            "--nets",
            *(_exact_net_pattern(net) for net in request.nets),
            "--ordering",
            strategy,
            "--track-width",
            str(request.default_track_width_mm),
            "--clearance",
            str(request.default_clearance_mm),
            "--via-size",
            str(request.via_diameter_mm),
            "--via-drill",
            str(request.via_drill_mm),
            "--grid-step",
            str(request.grid_mm),
            "--max-iterations",
            str(request.max_iterations),
            "--max-probe-iterations",
            str(request.max_probe_iterations),
            "--heuristic-weight",
            str(request.heuristic_weight),
            "--via-cost",
            str(request.via_cost),
            "--max-ripup",
            str(request.max_ripup),
            "--no-fix-drc-settings",
            "--stats",
        ]
        started = time.monotonic()
        result, watchdog = self._run_router(command, cwd=workspace)
        elapsed = time.monotonic() - started
        combined = f"{result.stdout}\n{result.stderr}"
        metrics = self._metrics(combined, elapsed)
        valid_output = routed_pcb.is_file() and 0 < routed_pcb.stat().st_size <= _MAX_OUTPUT_BYTES
        if (result.returncode != 0 and watchdog is None) or not valid_output:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                (
                    "KiCadRoutingTools was stopped without a usable routed board"
                    if watchdog is not None
                    else "KiCadRoutingTools did not produce a valid routed board"
                ),
                details={
                    "reason": self._sanitize(combined, request.nets),
                    "watchdog": watchdog,
                    "routing_pass_metrics": [item.model_dump(mode="json") for item in metrics],
                    "normalization_count": 0,
                },
            )
        return RoutedBoardCandidate(
            strategy=strategy,
            pcb=routed_pcb,
            elapsed_seconds=elapsed,
            stdout_tail=self._sanitize(result.stdout, request.nets),
            stderr_tail=self._sanitize(result.stderr, request.nets),
            pass_metrics=metrics,
            watchdog_reason=watchdog,
        )
