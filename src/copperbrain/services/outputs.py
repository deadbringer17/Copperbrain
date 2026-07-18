"""Project-local paths and atomic publication for user-facing artifacts."""

from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from copperbrain.errors import CopperbrainError
from copperbrain.models import ErrorCode

OUTPUT_DIRECTORY = "copperbrain-output"
PreviewPhase = Literal["schematic", "design-rules", "pcb"]
PREVIEW_PHASES = frozenset({"schematic", "design-rules", "pcb"})
PREVIEW_MARKER = ".copperbrain-preview.json"
PROJECT_COPY_IGNORE = shutil.ignore_patterns(
    OUTPUT_DIRECTORY,
    ".git",
    ".history",
    "*-backups",
    "*.kicad_prl",
    "*.lck",
    ".*.lck",
    "~*.lck",
)


def _remove_tree(path: Path) -> None:
    """Remove generated trees even when Windows preserved read-only VCS objects."""

    def make_writable_and_retry(
        function: Callable[[str], object], name: str, _error: object
    ) -> None:
        os.chmod(name, stat.S_IWRITE)
        function(name)

    shutil.rmtree(path, onerror=make_writable_and_retry)


def is_output_tree(path: Path) -> bool:
    """Return whether a path is inside a generated Copperbrain deliverable tree."""
    return any(part.casefold() == OUTPUT_DIRECTORY.casefold() for part in path.resolve().parts)


def require_source_project_root(project_root: Path) -> Path:
    """Refuse to publish relative to an output copy, preventing recursive previews."""
    resolved = project_root.expanduser().resolve()
    if is_output_tree(resolved):
        raise CopperbrainError(
            ErrorCode.CONFLICT,
            "A Copperbrain output copy cannot be used as a source project",
            actionable_hint="Open the original KiCad project outside copperbrain-output/.",
            details={"path": str(resolved)},
        )
    return resolved


def project_output_root(project_root: Path) -> Path:
    """Return the only directory allowed for deliverable project artifacts."""
    return require_source_project_root(project_root) / OUTPUT_DIRECTORY


def output_path(project_root: Path, category: str, filename: str) -> Path:
    """Resolve a simple filename below a validated project output category."""
    if not category or Path(category).name != category or category in {".", ".."}:
        raise CopperbrainError(ErrorCode.INVALID_INPUT, "Invalid output category")
    name = Path(filename)
    if not filename or name.name != filename or name.is_absolute() or filename in {".", ".."}:
        raise CopperbrainError(
            ErrorCode.INVALID_INPUT,
            "Output destination must be a filename without directories",
            actionable_hint=f"Files are always written below {OUTPUT_DIRECTORY}/.",
        )
    destination = project_output_root(project_root) / category / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def publish_preview(
    workspace: Path,
    project_root: Path,
    identifier: str,
    *,
    phase: PreviewPhase | None = None,
) -> Path:
    """Atomically publish a prepared project copy below the live project's output folder."""
    if not identifier or Path(identifier).name != identifier:
        raise CopperbrainError(ErrorCode.INVALID_INPUT, "Invalid preview identifier")
    parent = project_output_root(project_root) / "previews"
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / (phase or identifier)
    if destination.exists() and phase is None:
        raise CopperbrainError(ErrorCode.CONFLICT, "Preview output already exists")
    temporary = parent / f".{identifier}.{uuid.uuid4().hex}.tmp"
    previous = parent / f".{destination.name}.{uuid.uuid4().hex}.previous"
    try:
        shutil.copytree(workspace, temporary, ignore=PROJECT_COPY_IGNORE)
        if phase is not None:
            (temporary / PREVIEW_MARKER).write_text(
                json.dumps(
                    {"schema_version": 1, "phase": phase, "change_set_id": identifier},
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        if destination.exists():
            os.replace(destination, previous)
        os.replace(temporary, destination)
        if previous.exists():
            _remove_tree(previous)
        if phase is not None:
            for obsolete in parent.iterdir():
                if obsolete.name in PREVIEW_PHASES or obsolete.name.startswith("."):
                    continue
                if obsolete.is_symlink() or obsolete.is_file():
                    obsolete.unlink()
                elif obsolete.is_dir():
                    _remove_tree(obsolete)
    except Exception:
        if previous.exists() and not destination.exists():
            os.replace(previous, destination)
        raise
    finally:
        if temporary.exists():
            _remove_tree(temporary)
        if previous.exists():
            _remove_tree(previous)
    return destination


def require_current_preview(preview_directory: Path, change_set_id: str) -> None:
    """Refuse an acceptance when its bounded phase slot has since been replaced."""
    try:
        marker = json.loads((preview_directory / PREVIEW_MARKER).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise CopperbrainError(
            ErrorCode.CONFLICT,
            "The reviewed phase preview is unavailable or invalid",
            actionable_hint="Prepare and review the phase again before accepting it.",
        ) from exc
    if marker.get("change_set_id") != change_set_id:
        raise CopperbrainError(
            ErrorCode.CONFLICT,
            "The reviewed phase preview has been superseded",
            actionable_hint="Accept the current preview or prepare the phase again.",
        )
