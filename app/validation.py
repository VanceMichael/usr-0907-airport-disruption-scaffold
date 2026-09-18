"""Payload validation against the shipped contract, plus normalization.

Schema-level rules (types, patterns, required fields, ...) come from the
JSON Schema contract file itself via :mod:`app.jsonschema_lite`.  This
module adds timestamp parsing and produces the canonical payload used for
idempotency hashing: two submissions are "the same event" iff their
normalized canonical forms are byte-identical, so equivalent offset
timestamps (``+08:00`` vs ``Z``) still hash equally.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from . import jsonschema_lite
from .errors import validation_error
from .timeutil import format_utc, parse_timestamp

EVENT_CLOSED = "airport.closed"
EVENT_EXTENDED = "airport.extended"
EVENT_REOPENED = "airport.reopened"


@dataclass(frozen=True)
class NormalizedEvent:
    event_id: str
    event_version: int
    event_type: str
    airport_code: str
    effective_from: datetime
    effective_until: datetime | None
    reported_at: datetime
    supersedes_event_id: str | None
    reason: str | None
    canonical_json: str
    payload_hash: str


def load_contract(path) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        schema = json.load(handle)
    jsonschema_lite.check_supported(schema)
    return schema


def _check_datetime(value: str) -> None:
    parse_timestamp(value)  # raises ValueError with a client-readable message


def validate_and_normalize(payload, schema: dict) -> NormalizedEvent:
    if not isinstance(payload, dict):
        raise validation_error([{"field": "(root)", "issue": "expected a JSON object"}])
    errors = jsonschema_lite.validate(payload, schema, {"date-time": _check_datetime})
    if errors:
        raise validation_error(errors)

    effective_from = parse_timestamp(payload["effective_from"])
    effective_until = payload.get("effective_until")
    effective_until = parse_timestamp(effective_until) if effective_until is not None else None
    reported_at = parse_timestamp(payload["reported_at"])

    canonical = {
        "event_id": payload["event_id"],
        "event_version": payload["event_version"],
        "event_type": payload["event_type"],
        "airport_code": payload["airport_code"],
        "effective_from": format_utc(effective_from),
        "effective_until": format_utc(effective_until) if effective_until else None,
        "reported_at": format_utc(reported_at),
        "supersedes_event_id": payload.get("supersedes_event_id"),
        "reason": payload.get("reason"),
    }
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return NormalizedEvent(
        event_id=canonical["event_id"],
        event_version=canonical["event_version"],
        event_type=canonical["event_type"],
        airport_code=canonical["airport_code"],
        effective_from=effective_from,
        effective_until=effective_until,
        reported_at=reported_at,
        supersedes_event_id=canonical["supersedes_event_id"],
        reason=canonical["reason"],
        canonical_json=canonical_json,
        payload_hash=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
    )
