"""Contract validation for incoming disruption events.

The accepted envelope is ``contracts/disruption-event.schema.json``. The
contract is small and fixed, so it is enforced directly here — keeping the
service dependency-free while still checking every rule in the schema:
required fields, ``additionalProperties: false``, patterns, enums, value
ranges, and the ``date-time`` format (timezone offset mandatory).

Schema violations are reported as HTTP 400 ``schema_violation`` with one
entry per offending field. Semantic rules (known airport, window ordering,
closure-chain integrity, event versioning) live in ``app.service``.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .errors import ApiError
from .timeutil import parse_instant

EVENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
AIRPORT_CODE_PATTERN = re.compile(r"^[A-Z]{3}$")

EVENT_TYPES = ("airport.closed", "airport.extended", "airport.reopened")

REQUIRED_FIELDS = (
    "event_id",
    "event_version",
    "event_type",
    "airport_code",
    "effective_from",
    "reported_at",
)
OPTIONAL_FIELDS = ("effective_until", "supersedes_event_id", "reason")
ALL_FIELDS = frozenset(REQUIRED_FIELDS + OPTIONAL_FIELDS)

REASON_MAX_LENGTH = 240


def _check_timestamp(details: list[dict], payload: dict, field: str) -> datetime | None:
    value = payload.get(field)
    if value is None:
        return None
    try:
        return parse_instant(value, field)
    except ValueError as exc:
        details.append({"field": field, "issue": str(exc)})
        return None


def validate_event_payload(payload: object) -> dict[str, Any]:
    """Validate a raw decoded JSON body against the event contract.

    Returns a normalised dict with timestamps parsed to aware UTC datetimes.
    Raises :class:`ApiError` (400 ``schema_violation``) listing every
    violation found.
    """
    if not isinstance(payload, dict):
        raise ApiError(
            400, "schema_violation", "Event payload must be a JSON object."
        )

    details: list[dict[str, str]] = []
    for key in sorted(payload):
        if key not in ALL_FIELDS:
            details.append({"field": key, "issue": "additional property is not allowed"})
    for field in REQUIRED_FIELDS:
        if field not in payload:
            details.append({"field": field, "issue": "field is required"})

    if "event_id" in payload:
        event_id = payload["event_id"]
        if not isinstance(event_id, str) or not EVENT_ID_PATTERN.fullmatch(event_id):
            details.append(
                {
                    "field": "event_id",
                    "issue": "must match ^[a-z0-9][a-z0-9-]{7,63}$",
                }
            )

    if "event_version" in payload:
        version = payload["event_version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            details.append(
                {"field": "event_version", "issue": "must be an integer >= 1"}
            )

    if "event_type" in payload:
        event_type = payload["event_type"]
        if event_type not in EVENT_TYPES:
            details.append(
                {
                    "field": "event_type",
                    "issue": f"must be one of {', '.join(EVENT_TYPES)}",
                }
            )

    if "airport_code" in payload:
        airport_code = payload["airport_code"]
        if not isinstance(airport_code, str) or not AIRPORT_CODE_PATTERN.fullmatch(
            airport_code
        ):
            details.append(
                {"field": "airport_code", "issue": "must match ^[A-Z]{3}$"}
            )

    effective_from = _check_timestamp(details, payload, "effective_from")
    effective_until = None
    if payload.get("effective_until") is not None:
        effective_until = _check_timestamp(details, payload, "effective_until")
    reported_at = _check_timestamp(details, payload, "reported_at")

    supersedes = payload.get("supersedes_event_id")
    if supersedes is not None and (
        not isinstance(supersedes, str) or not EVENT_ID_PATTERN.fullmatch(supersedes)
    ):
        details.append(
            {
                "field": "supersedes_event_id",
                "issue": "must be null or match ^[a-z0-9][a-z0-9-]{7,63}$",
            }
        )

    reason = payload.get("reason")
    if reason is not None and (
        not isinstance(reason, str)
        or not 1 <= len(reason) <= REASON_MAX_LENGTH
    ):
        details.append(
            {
                "field": "reason",
                "issue": f"must be a string of 1..{REASON_MAX_LENGTH} characters",
            }
        )

    if details:
        raise ApiError(
            400,
            "schema_violation",
            "Event payload violates the disruption event contract.",
            details,
        )

    return {
        "event_id": payload["event_id"],
        "event_version": payload["event_version"],
        "event_type": payload["event_type"],
        "airport_code": payload["airport_code"],
        "effective_from": effective_from,
        "effective_until": effective_until,
        "reported_at": reported_at,
        "supersedes_event_id": supersedes,
        "reason": reason,
    }
