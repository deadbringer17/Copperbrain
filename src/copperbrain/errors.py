"""Actionable exceptions mapped to structured MCP errors."""

from __future__ import annotations

from typing import Any

from copperbrain.models import ErrorCode, StructuredError


class CopperbrainError(Exception):
    """Expected application failure with a stable machine-readable code."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        actionable_hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error = StructuredError(
            code=code,
            message=message,
            actionable_hint=actionable_hint,
            details=details or {},
        )
