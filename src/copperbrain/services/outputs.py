"""Project-local paths and atomic publication for user-facing artifacts."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from copperbrain.errors import CopperbrainError
from copperbrain.models import ErrorCode

OUTPUT_DIRECTORY = "copperbrain-output"
PROJECT_COPY_IGNORE = shutil.ignore_patterns(
    OUTPUT_DIRECTORY,
    ".git",
    ".history",
    "*-backups",
    "*.lck",
    ".*.lck",
    "~*.lck",
)


def project_output_root(project_root: Path) -> Path:
    """Return the only directory allowed for deliverable project artifacts."""
    return project_root.expanduser().resolve() / OUTPUT_DIRECTORY


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


def publish_preview(workspace: Path, project_root: Path, identifier: str) -> Path:
    """Atomically publish a prepared project copy below the live project's output folder."""
    if not identifier or Path(identifier).name != identifier:
        raise CopperbrainError(ErrorCode.INVALID_INPUT, "Invalid preview identifier")
    parent = project_output_root(project_root) / "previews"
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / identifier
    if destination.exists():
        raise CopperbrainError(ErrorCode.CONFLICT, "Preview output already exists")
    temporary = parent / f".{identifier}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copytree(workspace, temporary, ignore=PROJECT_COPY_IGNORE)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination
