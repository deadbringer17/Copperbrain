"""Local fixed-command FreeRouting backend with official KiCad DSN/SES exchange."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from platformdirs import user_documents_path

from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.errors import CopperbrainError
from copperbrain.models import ErrorCode, RoutingBackendStatus, RoutingRequest

_MAX_ROUTER_OUTPUT_BYTES = 100_000_000
_MINIMUM_JAVA_MAJOR = 25
_STRATEGIES = ("prioritized", "sequential")
_NORMALIZATION_LOOP = "PolylineTrace.normalize: max normalization depth"


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


def _expression_head(content: str, start: int) -> tuple[str, str | None]:
    """Read the first two top-level atoms without interpreting nested DSN syntax."""

    def atom(index: int) -> tuple[str, int]:
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

    tag, cursor = atom(start + 1)
    try:
        name, _ = atom(cursor)
    except ValueError:
        name = None
    return tag, name


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


@dataclass(frozen=True)
class RoutedBoardCandidate:
    """One isolated board returned by an external routing backend."""

    strategy: Literal["prioritized", "sequential"]
    pcb: Path
    elapsed_seconds: float
    stdout_tail: str = ""
    stderr_tail: str = ""


class RoutingBackend(Protocol):
    def status(self) -> RoutingBackendStatus: ...

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
        return RoutingBackendStatus(
            available=not missing,
            version=_version_from_jar(self.jar_path),
            java_major_version=self.java_major_version,
            java_path=self.java_path,
            jar_path=self.jar_path,
            kicad_python_path=self.kicad_python_path,
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
    ) -> subprocess.CompletedProcess[str]:
        """Run FreeRouting with wall-time, stall, and known normalization-loop watchdogs."""
        if self.process_factory is None:
            return self._run(command, cwd=cwd)
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
        normalization_count = 0
        log_file = cwd / "freerouting.log"
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
                    normalization_count += chunk.count(_NORMALIZATION_LOOP)
                    last_activity = now
            if session_file.is_file() and session_file.stat().st_size > 0:
                last_activity = now
            if normalization_count >= self.normalization_limit:
                watchdog = "normalization_loop"
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
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "FreeRouting was stopped by the routing watchdog",
                actionable_hint=(
                    "Retry from an unrouted board, review placement and rules, "
                    "or reduce routing scope."
                ),
                details={
                    "watchdog": watchdog,
                    "normalization_count": normalization_count,
                    "stdout_tail": self._tail(stdout or ""),
                    "stderr_tail": self._tail(stderr or ""),
                },
            )
        stdout, stderr = process.communicate()
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    @staticmethod
    def _tail(value: str, limit: int = 4_000) -> str:
        return value[-limit:]

    @staticmethod
    def _sanitize_dsn(path: Path, target_nets: tuple[str, ...] = ()) -> None:
        content = path.read_text(encoding="utf-8", errors="strict")
        sanitized = re.sub("[ΩµΦ]", "", content)
        sanitized = re.sub(r"\A\(pcb\s+[^\r\n]+", f"(pcb {path.name}", sanitized, count=1)
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
            removals = [
                (start, end)
                for start, end, tag, name in children
                if tag == "net" and name not in target
            ]
            for start, end in reversed(removals):
                sanitized = sanitized[:start] + sanitized[end:]
            remaining = {
                name
                for _, _, tag, name in _network_children(sanitized)
                if tag == "net" and name is not None
            }
            if remaining != target:
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "Specctra routing-net filtering did not preserve the exact requested set",
                    details={"expected": sorted(target), "actual": sorted(remaining)},
                )
        path.write_text(sanitized, encoding="utf-8", newline="\n")

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
        self._sanitize_dsn(dsn, request.nets)

        thread_count = request.thread_count or max(1, (os.cpu_count() or 2) - 1)
        started = time.monotonic()
        routed = self._run_router(
            [
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
                "--gui.enabled=false",
                f"--logging.file.location={workspace}",
            ],
            cwd=workspace,
            session_file=ses,
        )
        elapsed = time.monotonic() - started
        if (
            routed.returncode != 0
            or not ses.is_file()
            or ses.stat().st_size <= 0
            or ses.stat().st_size > _MAX_ROUTER_OUTPUT_BYTES
        ):
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "FreeRouting did not produce a valid Specctra session",
                details={"reason": self._tail(routed.stderr or routed.stdout)},
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
        )
