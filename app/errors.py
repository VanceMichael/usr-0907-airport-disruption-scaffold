"""Structured API errors.

Every client-visible failure is an :class:`ApiError` carrying a stable
machine-readable ``code`` plus a human-readable ``message`` (and optional
per-field ``details``). The HTTP layer serialises it as::

    {"error": {"code": ..., "message": ..., "details": [...]}}

so consumers can rely on a single, stable error envelope.
"""
from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """An error that maps directly onto the JSON error envelope."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def to_body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}
