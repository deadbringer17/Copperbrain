"""Validated, atomic KiCad project library-table updates."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from copperbrain.errors import CopperbrainError
from copperbrain.models import ErrorCode

_NICKNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


def validate_nickname(value: str) -> str:
    """Restrict library nicknames to a safe KiCad-compatible subset."""
    if not _NICKNAME.fullmatch(value):
        raise CopperbrainError(ErrorCode.INVALID_INPUT, "Invalid KiCad library nickname")
    return value


def _balanced(content: str) -> bool:
    depth = 0
    quoted = False
    escaped = False
    for character in content:
        if escaped:
            escaped = False
        elif character == "\\" and quoted:
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif not quoted and character == "(":
            depth += 1
        elif not quoted and character == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not quoted


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure_library_entry(
    table: Path,
    *,
    table_kind: str,
    nickname: str,
    uri: str,
) -> bool:
    """Add one validated project-relative entry and preserve an existing valid table."""
    validate_nickname(nickname)
    if table_kind not in {"sym_lib_table", "fp_lib_table"}:
        raise CopperbrainError(ErrorCode.INVALID_INPUT, "Invalid library table kind")
    if '"' in uri or "\n" in uri or "\r" in uri:
        raise CopperbrainError(ErrorCode.INVALID_INPUT, "Unsafe library URI")
    content = table.read_text(encoding="utf-8-sig") if table.exists() else f"({table_kind}\n)\n"
    if not _balanced(content) or not content.lstrip().startswith(f"({table_kind}"):
        raise CopperbrainError(
            ErrorCode.VALIDATION_FAILED, "Existing KiCad library table is invalid"
        )
    if re.search(rf'\(name\s+"{re.escape(nickname)}"\)', content):
        return False
    closing = content.rfind(")")
    entry = f'  (lib (name "{nickname}")(type "KiCad")(uri "{uri}")(options "")(descr ""))\n'
    updated = content[:closing] + entry + content[closing:]
    if not _balanced(updated):
        raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "Generated library table is invalid")
    _atomic_write(table, updated)
    return True
