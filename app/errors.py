"""Structured API errors.

Every client-facing failure is an :class:`ApiError` carrying a stable
machine-readable ``code`` plus an HTTP status.  Handlers serialize it with
:meth:`ApiError.to_body` so error envelopes are identical across endpoints.
"""
from __future__ import annotations


class ApiError(Exception):
    """An error that maps directly onto the JSON error envelope."""

    def __init__(self, status: int, code: str, message: str, details: list | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []

    def to_body(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


def validation_error(details: list, message: str = "Payload does not match the disruption event contract") -> ApiError:
    return ApiError(400, "validation_error", message, details)
