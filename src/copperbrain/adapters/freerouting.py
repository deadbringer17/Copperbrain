"""Local fixed-command FreeRouting backend with official KiCad DSN/SES exchange."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Literal, Protocol, TypedDict

from platformdirs import user_documents_path

from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ErrorCode,
    FreeRoutingCapabilityRecord,
    FreeRoutingPassMetric,
    RoutingBackendStatus,
    RoutingRequest,
)

_MAX_ROUTER_OUTPUT_BYTES = 100_000_000
_MINIMUM_JAVA_MAJOR = 25
_STRATEGIES = ("prioritized", "sequential")
_NORMALIZATION_LOOP = "PolylineTrace.normalize: max normalization depth"
_PASS_START = re.compile(
    r"Pass #(?P<pass>\d+): (?P<incomplete>\d+) incompletes across "
    r"(?P<items>\d+) items to route"
)
_PASS_FAILURE = re.compile(r"Pass #(?P<pass>\d+): Failed to route")
_PASS_COMPLETED = re.compile(
    r"Auto-router pass #(?P<pass>\d+).*?completed in "
    r"(?P<duration>\d+(?:\.\d+)?) seconds with the score of "
    r"(?P<score>-?\d+(?:\.\d+)?) \((?P<unrouted>\d+) unrouted\), using "
    r"(?P<cpu>\d+(?:\.\d+)?) CPU seconds and the job allocated "
    r"(?P<memory>\d+(?:\.\d+)?) GB",
    re.IGNORECASE,
)


def _sexpr_end(content: str, start: int) -> int:
    """Return the exclusive end of one balanced DSN expression."""
    if start >= len(content) or content[start] != "(":
        raise ValueError("DSN expression must start with '('")
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(content)):
        character = content[index]
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index + 1
            if depth < 0:
                break
    raise ValueError("DSN expression is unbalanced")


def _read_atom(content: str, index: int) -> tuple[str, int]:
    while index < len(content) and content[index].isspace():
        index += 1
    if index >= len(content) or content[index] in "()":
        raise ValueError("DSN expression is missing an atom")
    if content[index] == '"':
        index += 1
        value: list[str] = []
        escaped = False
        while index < len(content):
            character = content[index]
            index += 1
            if escaped:
                value.append(character)
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                return "".join(value), index
            else:
                value.append(character)
        raise ValueError("DSN quoted atom is unterminated")
    end = index
    while end < len(content) and not content[end].isspace() and content[end] not in "()":
        end += 1
    return content[index:end], end


def _expression_head(content: str, start: int) -> tuple[str, str | None]:
    """Read the first two top-level atoms without interpreting nested DSN syntax."""

    tag, cursor = _read_atom(content, start + 1)
    try:
        name, _ = _read_atom(content, cursor)
    except ValueError:
        name = None
    return tag, name


def _dsn_atom(value: str) -> str:
    if value and not any(character.isspace() or character in '()"\\' for character in value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _class_parts(content: str, start: int, end: int) -> tuple[str, tuple[str, ...], str]:
    tag, cursor = _read_atom(content, start + 1)
    if tag != "class":
        raise ValueError("DSN expression is not a class")
    name, cursor = _read_atom(content, cursor)
    members: list[str] = []
    while cursor < end - 1:
        while cursor < end - 1 and content[cursor].isspace():
            cursor += 1
        if cursor >= end - 1 or content[cursor] == "(":
            break
        member, cursor = _read_atom(content, cursor)
        members.append(member)
    suffix = content[cursor : end - 1].strip()
    if suffix:
        suffix_cursor = 0
        while suffix_cursor < len(suffix):
            while suffix_cursor < len(suffix) and suffix[suffix_cursor].isspace():
                suffix_cursor += 1
            if suffix_cursor >= len(suffix):
                break
            if suffix[suffix_cursor] != "(":
                raise ValueError("DSN class contains an atom after its rule expressions")
            suffix_cursor = _sexpr_end(suffix, suffix_cursor)
    return name, tuple(members), suffix


def _render_class(name: str, members: tuple[str, ...], suffix: str) -> str:
    body = " ".join(_dsn_atom(member) for member in members)
    expression = f"(class {_dsn_atom(name)}"
    if body:
        expression += f" {body}"
    if suffix:
        expression += f"\n      {suffix}"
    return expression + ")"


def _network_children(content: str) -> tuple[tuple[int, int, str, str | None], ...]:
    matches = tuple(re.finditer(r"(?m)^\s*\(network(?=\s|\()", content))
    if len(matches) != 1:
        raise ValueError("DSN must contain exactly one network expression")
    network_start = content.find("(", matches[0].start())
    network_end = _sexpr_end(content, network_start)
    children: list[tuple[int, int, str, str | None]] = []
    cursor = network_start + len("(network")
    while cursor < network_end - 1:
        character = content[cursor]
        if character.isspace():
            cursor += 1
            continue
        if character != "(":
            raise ValueError("DSN network contains an unexpected atom")
        child_end = _sexpr_end(content, cursor)
        tag, name = _expression_head(content, cursor)
        children.append((cursor, child_end, tag, name))
        cursor = child_end
    return tuple(children)


def _split_wiring_polylines(content: str) -> str:
    """Render each multi-segment DSN wire path as independent two-point wires.

    KiCad can merge adjacent tracks into one Specctra path.  FreeRouting 2.2.4 may
    stall while normalizing those paths after a routed board is exported again,
    even when the same geometry originally came from FreeRouting.  Independent
    two-point wires preserve the exact copper geometry and load deterministically.
    """

    matches = tuple(re.finditer(r"(?m)^\s*\(wiring(?=\s|\))", content))
    if not matches:
        return content
    if len(matches) != 1:
        raise ValueError("DSN must contain exactly one wiring expression")
    wiring_start = content.find("(", matches[0].start())
    wiring_end = _sexpr_end(content, wiring_start)
    tag, cursor = _read_atom(content, wiring_start + 1)
    if tag != "wiring":
        raise ValueError("DSN wiring expression is invalid")

    replacements: list[tuple[int, int, str]] = []
    while cursor < wiring_end - 1:
        while cursor < wiring_end - 1 and content[cursor].isspace():
            cursor += 1
        if cursor >= wiring_end - 1:
            break
        if content[cursor] != "(":
            raise ValueError("DSN wiring contains an atom outside an item")
        item_start = cursor
        item_end = _sexpr_end(content, item_start)
        item_tag, item_cursor = _read_atom(content, item_start + 1)
        if item_tag == "wire":
            while item_cursor < item_end - 1 and content[item_cursor].isspace():
                item_cursor += 1
            if item_cursor < item_end - 1 and content[item_cursor] == "(":
                path_start = item_cursor
                path_end = _sexpr_end(content, path_start)
                path_tag, path_cursor = _read_atom(content, path_start + 1)
                if path_tag == "path":
                    atoms: list[str] = []
                    while path_cursor < path_end - 1:
                        while path_cursor < path_end - 1 and content[path_cursor].isspace():
                            path_cursor += 1
                        if path_cursor >= path_end - 1:
                            break
                        atom, path_cursor = _read_atom(content, path_cursor)
                        atoms.append(atom)
                    if len(atoms) < 6 or (len(atoms) - 2) % 2:
                        raise ValueError("DSN wire path contains invalid coordinates")
                    coordinates = atoms[2:]
                    if len(coordinates) > 4:
                        points = tuple(zip(coordinates[::2], coordinates[1::2], strict=True))
                        indent_start = content.rfind("\n", 0, item_start) + 1
                        indent = content[indent_start:item_start]
                        prefix = content[item_start:path_start]
                        suffix = content[path_end:item_end]
                        wires = []
                        for first, second in pairwise(points):
                            path = " ".join(
                                (
                                    "(path",
                                    _dsn_atom(atoms[0]),
                                    _dsn_atom(atoms[1]),
                                    _dsn_atom(first[0]),
                                    _dsn_atom(first[1]),
                                    _dsn_atom(second[0]),
                                    _dsn_atom(second[1]) + ")",
                                )
                            )
                            wires.append(prefix + path + suffix)
                        replacements.append((item_start, item_end, ("\n" + indent).join(wires)))
        cursor = item_end

    for start, end, replacement in reversed(replacements):
        content = content[:start] + replacement + content[end:]
    return content


@dataclass(frozen=True)
class RoutedBoardCandidate:
    """One isolated board returned by an external routing backend."""

    strategy: Literal["prioritized", "sequential"]
    pcb: Path
    elapsed_seconds: float
    stdout_tail: str = ""
    stderr_tail: str = ""
    pass_metrics: tuple[FreeRoutingPassMetric, ...] = ()
    normalization_count: int = 0
    watchdog_reason: str | None = None


@dataclass(frozen=True)
class _RouterExecution:
    result: subprocess.CompletedProcess[str]
    pass_metrics: tuple[FreeRoutingPassMetric, ...] = ()
    normalization_count: int = 0
    watchdog_reason: str | None = None


class _PassValues(TypedDict):
    board_incomplete_count: int | None
    queued_item_count: int | None
    board_unrouted_count: int | None
    failure_count: int
    duration_seconds: float | None
    score: float | None
    cpu_seconds: float | None
    allocated_memory_gb: float | None


class _FreeRoutingProgress:
    """Incrementally parse bounded, non-sensitive metrics from FreeRouting logs."""

    def __init__(self) -> None:
        self.normalization_count = 0
        self._passes: dict[int, _PassValues] = {}
        self._carry = ""

    def feed(self, chunk: str, *, final: bool = False) -> None:
        content = self._carry + chunk
        lines = content.splitlines(keepends=True)
        self._carry = ""
        if lines and not lines[-1].endswith(("\n", "\r")) and not final:
            self._carry = lines.pop()
        for line in lines:
            self.normalization_count += line.count(_NORMALIZATION_LOOP)
            if match := _PASS_START.search(line):
                values = self._values(int(match.group("pass")))
                values["board_incomplete_count"] = int(match.group("incomplete"))
                values["queued_item_count"] = int(match.group("items"))
            if match := _PASS_FAILURE.search(line):
                values = self._values(int(match.group("pass")))
                values["failure_count"] = int(values["failure_count"] or 0) + 1
            if match := _PASS_COMPLETED.search(line):
                values = self._values(int(match.group("pass")))
                values["board_unrouted_count"] = int(match.group("unrouted"))
                values["duration_seconds"] = float(match.group("duration"))
                values["score"] = float(match.group("score"))
                values["cpu_seconds"] = float(match.group("cpu"))
                values["allocated_memory_gb"] = float(match.group("memory"))

    def _values(self, pass_number: int) -> _PassValues:
        return self._passes.setdefault(
            pass_number,
            {
                "board_incomplete_count": None,
                "queued_item_count": None,
                "board_unrouted_count": None,
                "failure_count": 0,
                "duration_seconds": None,
                "score": None,
                "cpu_seconds": None,
                "allocated_memory_gb": None,
            },
        )

    def metrics(self) -> tuple[FreeRoutingPassMetric, ...]:
        metrics: list[FreeRoutingPassMetric] = []
        previous_open: int | None = None
        for pass_number, values in sorted(self._passes.items()):
            current_open = (
                values["board_unrouted_count"]
                if values["board_unrouted_count"] is not None
                else values["board_incomplete_count"]
            )
            starting_open = previous_open
            if starting_open is None:
                starting_open = values["board_incomplete_count"]
            resolved = (
                max(0, starting_open - current_open)
                if starting_open is not None and current_open is not None
                else 0
            )
            metrics.append(
                FreeRoutingPassMetric(
                    pass_number=pass_number,
                    **values,
                    connections_resolved=resolved,
                    connections_resolved_per_pass=float(resolved),
                )
            )
            if current_open is not None:
                previous_open = current_open
        return tuple(metrics)

    def semantic_stagnation_streak(self) -> int:
        """Count completed consecutive passes without fewer board-wide opens."""
        streak = 0
        previous: int | None = None
        for metric in self.metrics():
            current = metric.board_unrouted_count
            if current is None:
                continue
            if previous is not None:
                streak = streak + 1 if current >= previous else 0
            previous = current
        return streak

    def error_details(self) -> dict[str, object]:
        return {
            "freerouting_pass_metrics": [
                metric.model_dump(mode="json") for metric in self.metrics()
            ],
            "freerouting_normalization_count": self.normalization_count,
        }


class RoutingBackend(Protocol):
    def status(self) -> RoutingBackendStatus: ...

    def refill_zones(self, pcb: Path) -> None: ...

    def route(
        self,
        pcb: Path,
        workspace: Path,
        request: RoutingRequest,
        strategy: Literal["prioritized", "sequential"],
    ) -> RoutedBoardCandidate: ...


def _version_from_jar(path: Path | None) -> str | None:
    if path is None:
        return None
    match = re.search(r"freerouting[-_](\d+(?:\.\d+){1,3})", path.name, re.IGNORECASE)
    return match.group(1) if match else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _capability_path(jar_path: Path) -> Path:
    return jar_path.with_name(f"{jar_path.name}.capabilities.json")


def _verified_capabilities(
    jar_path: Path | None,
) -> tuple[FreeRoutingCapabilityRecord | None, Path | None, str | None]:
    if jar_path is None or not jar_path.is_file():
        return None, None, "FreeRouting JAR is unavailable"
    path = _capability_path(jar_path)
    if not path.is_file():
        return None, path, "No hash-bound capability record is installed for this JAR"
    try:
        record = FreeRoutingCapabilityRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, path, f"Capability record is invalid: {exc}"
    actual_hash = _sha256(jar_path)
    if record.jar_sha256 != actual_hash:
        return None, path, "Capability record hash does not match the selected JAR"
    return record, path, None


def _candidate_jars(data_dir: Path, explicit: Path | None) -> tuple[Path, ...]:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    candidates.extend((data_dir / "integrations" / "freerouting").glob("freerouting*.jar"))
    detection = detect_kicad()
    roots = [*detection.user_data_directories]
    documents = user_documents_path() / "KiCad"
    roots.extend(path for path in documents.glob("*/3rdparty/plugins") if path.is_dir())
    for root in roots:
        if root.is_dir():
            candidates.extend(root.rglob("freerouting*.jar"))
    unique = tuple(dict.fromkeys(path.resolve() for path in candidates if path.is_file()))
    return tuple(
        sorted(
            unique,
            key=lambda path: (
                bool(
                    (capabilities := _verified_capabilities(path)[0]) is not None
                    and capabilities.scoped_net_classes_cli
                ),
                tuple(int(part) for part in (_version_from_jar(path) or "0").split(".")),
                str(path).lower(),
            ),
            reverse=True,
        )
    )


def _kicad_python() -> Path | None:
    cli = detect_kicad().selected_cli
    if cli is None:
        return None
    names = ("python.exe",) if os.name == "nt" else ("python3", "python")
    return next((cli.parent / name for name in names if (cli.parent / name).is_file()), None)


def _candidate_java_paths(data_dir: Path, explicit: Path | None) -> tuple[Path, ...]:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    private_java = data_dir / "integrations" / "java"
    executable_name = "java.exe" if os.name == "nt" else "java"
    if private_java.is_dir():
        candidates.extend(private_java.glob(f"**/bin/{executable_name}"))
    system_java = shutil.which("java")
    if system_java:
        candidates.append(Path(system_java))
    return tuple(dict.fromkeys(path.resolve() for path in candidates if path.is_file()))


def _java_major(path: Path) -> int | None:
    try:
        result = subprocess.run(
            [str(path), "-version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = f"{result.stdout}\n{result.stderr}"
    match = re.search(r'version\s+"(?P<major>\d+)', output)
    return int(match.group("major")) if match else None


class FreeRoutingAdapter:
    """Run a discovered local FreeRouting JAR without a shell or user-supplied commands."""

    def __init__(
        self,
        *,
        jar_path: Path | None,
        java_path: Path | None,
        java_major_version: int | None = None,
        kicad_python_path: Path | None,
        timeout_seconds: float = 900,
        stall_seconds: float = 180,
        normalization_limit: int = 100,
        semantic_stagnation_passes: int = 3,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        process_factory: Callable[..., subprocess.Popen[str]] | None = None,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        self.jar_path = jar_path.resolve() if jar_path is not None else None
        self.java_path = java_path.resolve() if java_path is not None else None
        self.java_major_version = java_major_version
        self.kicad_python_path = (
            kicad_python_path.resolve() if kicad_python_path is not None else None
        )
        self.timeout_seconds = timeout_seconds
        self.stall_seconds = stall_seconds
        self.normalization_limit = normalization_limit
        self.semantic_stagnation_passes = semantic_stagnation_passes
        self.runner = runner
        self.process_factory: Callable[..., subprocess.Popen[str]] | None = process_factory or (
            subprocess.Popen if runner is subprocess.run else None
        )
        self.poll_interval_seconds = poll_interval_seconds

    @classmethod
    def discover(
        cls,
        data_dir: Path,
        *,
        explicit_jar: Path | None = None,
        explicit_java: Path | None = None,
        timeout_seconds: float = 900,
        stall_seconds: float = 180,
        normalization_limit: int = 100,
        semantic_stagnation_passes: int = 3,
    ) -> FreeRoutingAdapter:
        jars = _candidate_jars(data_dir, explicit_jar)
        selected_java: tuple[Path | None, int | None]
        if explicit_java is not None:
            explicit_java = explicit_java.resolve()
            selected_java = (
                explicit_java,
                _java_major(explicit_java) if explicit_java.is_file() else None,
            )
        else:
            java_candidates = _candidate_java_paths(data_dir, None)
            java_versions = tuple((path, _java_major(path)) for path in java_candidates)
            compatible = tuple(
                (path, major)
                for path, major in java_versions
                if major is not None and major >= _MINIMUM_JAVA_MAJOR
            )
            selected_java = (
                max(compatible, key=lambda item: (item[1], str(item[0]).lower()))
                if compatible
                else (java_versions[0] if java_versions else (None, None))
            )
        return cls(
            jar_path=explicit_jar.resolve()
            if explicit_jar is not None
            else (jars[0] if jars else None),
            java_path=selected_java[0],
            java_major_version=selected_java[1],
            kicad_python_path=_kicad_python(),
            timeout_seconds=timeout_seconds,
            stall_seconds=stall_seconds,
            normalization_limit=normalization_limit,
            semantic_stagnation_passes=semantic_stagnation_passes,
        )

    def status(self) -> RoutingBackendStatus:
        missing: list[str] = []
        if self.java_path is None or not self.java_path.is_file():
            missing.append("Java runtime")
        elif self.java_major_version is None:
            missing.append("detectable Java version")
        elif self.java_major_version < _MINIMUM_JAVA_MAJOR:
            missing.append(f"Java {_MINIMUM_JAVA_MAJOR}+ runtime (found {self.java_major_version})")
        if self.jar_path is None or not self.jar_path.is_file():
            missing.append("FreeRouting JAR")
        if self.kicad_python_path is None or not self.kicad_python_path.is_file():
            missing.append("KiCad Python runtime")
        capabilities, capability_path, capability_reason = _verified_capabilities(self.jar_path)
        return RoutingBackendStatus(
            available=not missing,
            version=_version_from_jar(self.jar_path),
            java_major_version=self.java_major_version,
            java_path=self.java_path,
            jar_path=self.jar_path,
            kicad_python_path=self.kicad_python_path,
            scoped_routing_supported=bool(
                capabilities is not None and capabilities.scoped_net_classes_cli
            ),
            capability_path=capability_path,
            capability_reason=capability_reason,
            reason=f"Missing: {', '.join(missing)}" if missing else None,
        )

    @staticmethod
    def strategies(request: RoutingRequest) -> tuple[Literal["prioritized", "sequential"], ...]:
        return _STRATEGIES[: request.candidate_count]  # type: ignore[return-value]

    def _run(self, command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
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
                "FreeRouting execution failed",
                actionable_hint="Check the local Java, FreeRouting, and KiCad Python installation.",
                details={"reason": str(exc)},
            ) from exc

    def refill_zones(self, pcb: Path) -> None:
        """Refill zones on an isolated routed copy through KiCad's fixed-action worker."""
        if self.kicad_python_path is None or not self.kicad_python_path.is_file():
            raise CopperbrainError(
                ErrorCode.INTEGRATION_UNAVAILABLE,
                "KiCad Python is unavailable for routed-board zone refill",
            )
        worker = Path(__file__).with_name("kicad_specctra_worker.py")
        result = self._run(
            [str(self.kicad_python_path), str(worker), "refill", str(pcb)],
            cwd=pcb.parent,
        )
        if result.returncode != 0:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "KiCad failed to refill zones on the routed board",
                details={"reason": self._tail(result.stderr or result.stdout)},
            )

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        """Best-effort cleanup of Java and its process group after watchdog aborts."""
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
        self,
        command: list[str],
        *,
        cwd: Path,
        session_file: Path,
        semantic_stagnation_passes: int | None = None,
    ) -> _RouterExecution:
        """Run FreeRouting with wall-time, stall, and known normalization-loop watchdogs."""
        progress = _FreeRoutingProgress()
        log_file = cwd / "freerouting.log"
        if self.process_factory is None:
            result = self._run(command, cwd=cwd)
            if log_file.is_file():
                progress.feed(log_file.read_text(encoding="utf-8", errors="replace"), final=True)
            return _RouterExecution(result, progress.metrics(), progress.normalization_count)
        process_kwargs: dict[str, object] = {
            "cwd": cwd,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            process_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_kwargs["start_new_session"] = True
        try:
            process = self.process_factory(command, **process_kwargs)
        except OSError as exc:
            raise CopperbrainError(
                ErrorCode.INTEGRATION_UNAVAILABLE,
                "FreeRouting execution failed",
                details={"reason": str(exc)},
            ) from exc

        started = time.monotonic()
        last_activity = started
        log_offset = 0
        watchdog: str | None = None
        while process.poll() is None:
            now = time.monotonic()
            if log_file.is_file():
                size = log_file.stat().st_size
                if size > log_offset:
                    with log_file.open("r", encoding="utf-8", errors="replace") as stream:
                        stream.seek(log_offset)
                        chunk = stream.read(min(size - log_offset, 1_000_000))
                        log_offset = stream.tell()
                    progress.feed(chunk)
                    last_activity = now
            if session_file.is_file() and session_file.stat().st_size > 0:
                last_activity = now
            if progress.normalization_count >= self.normalization_limit:
                watchdog = "normalization_loop"
                break
            if progress.semantic_stagnation_streak() >= (
                semantic_stagnation_passes or self.semantic_stagnation_passes
            ):
                watchdog = "semantic_stagnation"
                break
            if now - started >= self.timeout_seconds:
                watchdog = "timeout"
                break
            if now - last_activity >= self.stall_seconds:
                watchdog = "stalled"
                break
            time.sleep(self.poll_interval_seconds)

        if watchdog is not None:
            self._terminate_process_tree(process)
            stdout, stderr = process.communicate()
            progress.feed("", final=True)
            return _RouterExecution(
                subprocess.CompletedProcess(
                    command,
                    process.returncode if process.returncode is not None else -1,
                    stdout or "",
                    stderr or "",
                ),
                progress.metrics(),
                progress.normalization_count,
                watchdog,
            )
        stdout, stderr = process.communicate()
        if log_file.is_file() and log_file.stat().st_size > log_offset:
            with log_file.open("r", encoding="utf-8", errors="replace") as stream:
                stream.seek(log_offset)
                progress.feed(stream.read(), final=True)
        else:
            progress.feed("", final=True)
        return _RouterExecution(
            subprocess.CompletedProcess(command, process.returncode, stdout, stderr),
            progress.metrics(),
            progress.normalization_count,
        )

    @staticmethod
    def _tail(value: str, limit: int = 4_000) -> str:
        return value[-limit:]

    @staticmethod
    def _sanitize_dsn(path: Path, target_nets: tuple[str, ...] = ()) -> tuple[str, ...]:
        content = path.read_text(encoding="utf-8", errors="strict")
        sanitized = re.sub("[ΩµΦ]", "", content)
        sanitized = re.sub(r"\A\(pcb\s+[^\r\n]+", f"(pcb {path.name}", sanitized, count=1)
        ignored_classes: tuple[str, ...] = ()
        if target_nets:
            try:
                children = _network_children(sanitized)
            except ValueError as exc:
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "KiCad exported an unsupported Specctra network structure",
                    details={"reason": str(exc)},
                ) from exc
            available = {name for _, _, tag, name in children if tag == "net" and name is not None}
            missing = sorted(set(target_nets) - available)
            if missing:
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "Requested routing nets are absent from the KiCad Specctra export",
                    details={"missing_nets": missing},
                )
            target = set(target_nets)
            assigned: dict[str, int] = {}
            replacements: list[tuple[int, int, str]] = []
            ignored: list[str] = []
            class_index = 0
            try:
                for start, end, tag, _ in children:
                    if tag != "class":
                        continue
                    name, members, suffix = _class_parts(sanitized, start, end)
                    for member in members:
                        assigned[member] = assigned.get(member, 0) + 1
                    routed_members = tuple(member for member in members if member in target)
                    preserved_members = tuple(member for member in members if member not in target)
                    rendered: list[str] = []
                    if routed_members:
                        rendered.append(_render_class(name, routed_members, suffix))
                    if preserved_members:
                        class_index += 1
                        ignored_name = f"__copperbrain_preserve_{class_index}"
                        ignored.append(ignored_name)
                        rendered.append(_render_class(ignored_name, preserved_members, suffix))
                    replacements.append((start, end, "\n    ".join(rendered)))
            except ValueError as exc:
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "KiCad exported an unsupported Specctra net-class structure",
                    details={"reason": str(exc)},
                ) from exc
            unclassified = sorted(name for name in available if assigned.get(name, 0) == 0)
            multiply_classified = sorted(name for name in available if assigned.get(name, 0) > 1)
            if unclassified or multiply_classified:
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "Specctra net classes cannot be isolated safely for scoped routing",
                    details={
                        "unclassified_net_count": len(unclassified),
                        "multiply_classified_net_count": len(multiply_classified),
                    },
                )
            for start, end, replacement in reversed(replacements):
                sanitized = sanitized[:start] + replacement + sanitized[end:]
            remaining = {
                name
                for _, _, tag, name in _network_children(sanitized)
                if tag == "net" and name is not None
            }
            if remaining != available:
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "Specctra scope isolation changed the exported net definitions",
                )
            ignored_classes = tuple(ignored)
        try:
            sanitized = _split_wiring_polylines(sanitized)
        except ValueError as exc:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "KiCad exported unsupported Specctra wiring geometry",
                details={"reason": str(exc)},
            ) from exc
        path.write_text(sanitized, encoding="utf-8", newline="\n")
        return ignored_classes

    def route(
        self,
        pcb: Path,
        workspace: Path,
        request: RoutingRequest,
        strategy: Literal["prioritized", "sequential"],
    ) -> RoutedBoardCandidate:
        status = self.status()
        if not status.available:
            raise CopperbrainError(
                ErrorCode.INTEGRATION_UNAVAILABLE,
                "The local FreeRouting backend is unavailable",
                actionable_hint=(
                    "Install Java 25+ and the FreeRouting JAR, optionally set "
                    "COPPERBRAIN_FREEROUTING_JAVA and COPPERBRAIN_FREEROUTING_JAR, "
                    "then restart Copperbrain."
                ),
                details={"reason": status.reason or "unknown"},
            )
        if strategy not in _STRATEGIES:
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "Unsupported routing strategy")
        if not pcb.is_file() or pcb.suffix.lower() != ".kicad_pcb":
            raise CopperbrainError(ErrorCode.NOT_FOUND, "PCB input for FreeRouting was not found")
        workspace.mkdir(parents=True, exist_ok=False)
        dsn = workspace / "freerouting-input.dsn"
        ses = workspace / "freerouting-output.ses"
        routed_pcb = workspace / "freerouting-routed.kicad_pcb"
        worker = Path(__file__).with_name("kicad_specctra_worker.py")
        assert self.kicad_python_path is not None
        assert self.java_path is not None
        assert self.jar_path is not None

        exported = self._run(
            [str(self.kicad_python_path), str(worker), "export", str(pcb), str(dsn)],
            cwd=workspace,
        )
        if exported.returncode != 0 or not dsn.is_file():
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "KiCad failed to export the PCB as Specctra DSN",
                details={"reason": self._tail(exported.stderr or exported.stdout)},
            )
        ignored_classes = self._sanitize_dsn(dsn, request.nets)
        capabilities, capability_path, capability_reason = _verified_capabilities(self.jar_path)
        if ignored_classes and not (
            capabilities is not None and capabilities.scoped_net_classes_cli
        ):
            raise CopperbrainError(
                ErrorCode.INTEGRATION_UNAVAILABLE,
                "Headless FreeRouting cannot safely honor a scoped net selection",
                actionable_hint=(
                    "Install a FreeRouting JAR with verified headless class exclusion and its "
                    "hash-bound .capabilities.json record, or route only when every exported net "
                    "is intentionally in scope on a clean board."
                ),
                details={
                    "preserve_class_count": len(ignored_classes),
                    "capability_path": (
                        str(capability_path) if capability_path is not None else None
                    ),
                    "capability_reason": capability_reason,
                },
            )

        thread_count = request.thread_count or max(1, (os.cpu_count() or 2) - 1)
        started = time.monotonic()
        router_command = [
            str(self.java_path),
            "-jar",
            str(self.jar_path),
            "-de",
            str(dsn),
            "-do",
            str(ses),
            "-mp",
            str(request.max_passes),
            "-mt",
            str(thread_count),
            "-us",
            strategy,
        ]
        if ignored_classes:
            router_command.extend(("-inc", ",".join(ignored_classes)))
        router_command.extend(("--gui.enabled=false", f"--logging.file.location={workspace}"))
        execution = self._run_router(
            router_command,
            cwd=workspace,
            session_file=ses,
            semantic_stagnation_passes=request.semantic_stagnation_passes,
        )
        routed = execution.result
        elapsed = time.monotonic() - started
        if (
            (routed.returncode != 0 and execution.watchdog_reason is None)
            or not ses.is_file()
            or ses.stat().st_size <= 0
            or ses.stat().st_size > _MAX_ROUTER_OUTPUT_BYTES
        ):
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                (
                    "FreeRouting was stopped by the routing watchdog without an importable "
                    "partial Specctra session"
                    if execution.watchdog_reason is not None
                    else "FreeRouting did not produce a valid Specctra session"
                ),
                details={
                    "reason": self._tail(routed.stderr or routed.stdout),
                    "watchdog": execution.watchdog_reason,
                    "partial_session_available": bool(ses.is_file() and ses.stat().st_size > 0),
                    "freerouting_pass_metrics": [
                        item.model_dump(mode="json") for item in execution.pass_metrics
                    ],
                    "freerouting_normalization_count": execution.normalization_count,
                },
            )

        imported = self._run(
            [
                str(self.kicad_python_path),
                str(worker),
                "import",
                str(pcb),
                str(ses),
                str(routed_pcb),
            ],
            cwd=workspace,
        )
        if imported.returncode != 0 or not routed_pcb.is_file():
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "KiCad failed to import the FreeRouting Specctra session",
                details={"reason": self._tail(imported.stderr or imported.stdout)},
            )
        return RoutedBoardCandidate(
            strategy=strategy,
            pcb=routed_pcb,
            elapsed_seconds=elapsed,
            stdout_tail=self._tail(routed.stdout),
            stderr_tail=self._tail(routed.stderr),
            pass_metrics=execution.pass_metrics,
            normalization_count=execution.normalization_count,
            watchdog_reason=execution.watchdog_reason,
        )
