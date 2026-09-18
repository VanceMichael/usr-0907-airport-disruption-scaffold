"""Business logic: event intake, closure-chain semantics, impact queries.

Event chain model
-----------------
* ``airport.closed`` opens a new closure chain for an airport. It is
  rejected while another chain is still open for that airport.
* ``airport.extended`` supersedes the current chain head and moves the
  closure end later. It must restate the original ``effective_from`` and
  carry a strictly greater ``event_version``.
* ``airport.reopened`` supersedes the current chain head and closes the
  chain; the closure effectively ends at its ``effective_from``.

Idempotency
-----------
Submission is idempotent on ``event_id``: replaying a byte-identical event
returns the originally computed result; the same id with a different
payload is rejected with 409 ``event_id_conflict``.
"""
from __future__ import annotations

import json
import math
import threading
from datetime import datetime
from typing import Any

from .errors import ApiError
from .fixtures import Airport, Flight
from .impacts import (
    IMPACT_STATUSES,
    Impact,
    closure_usable_at,
    compute_impacts,
)
from .storage import EventStoreConflict, Storage
from .timeutil import format_instant, now_utc, parse_instant
from .validation import validate_event_payload

TYPE_CLOSED = "airport.closed"
TYPE_EXTENDED = "airport.extended"
TYPE_REOPENED = "airport.reopened"


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class DisruptionService:
    def __init__(
        self,
        storage: Storage,
        airports: dict[str, Airport],
        flights: list[Flight],
    ) -> None:
        self._storage = storage
        self._airports = airports
        self._flights = flights
        # Serialises intake so chain validation and the write that follows
        # it are atomic with respect to concurrent submissions.
        self._submit_lock = threading.Lock()

    # ------------------------------------------------------------------
    # intake
    # ------------------------------------------------------------------
    def submit_event(self, raw_payload: object) -> tuple[int, dict[str, Any]]:
        """Validate, deduplicate, compute, and persist one event.

        Returns ``(http_status, response_body)``. Raises ``ApiError`` for
        every rejection; nothing is persisted on rejection.
        """
        with self._submit_lock:
            return self._submit_event_locked(raw_payload)

    def _submit_event_locked(
        self, raw_payload: object
    ) -> tuple[int, dict[str, Any]]:
        canonical = _canonical(raw_payload)
        event = validate_event_payload(raw_payload)

        existing = self._storage.get_event(event["event_id"])
        if existing is not None:
            if existing["request_canonical"] == canonical:
                # Idempotent replay: return the original result unchanged.
                return 200, json.loads(existing["response_body"])
            raise ApiError(
                409,
                "event_id_conflict",
                f"event_id {event['event_id']!r} was already submitted with a "
                "different payload; event ids must be unique per event.",
            )

        airport = self._airports.get(event["airport_code"])
        if airport is None:
            raise ApiError(
                422,
                "unknown_airport",
                f"airport_code {event['airport_code']!r} is not a known airport.",
            )

        head = self._storage.get_open_closure_head(airport.code)
        window_from, window_until = self._validate_chain(event, head)

        impacts = compute_impacts(airport, self._flights, window_from, window_until)
        received_at = now_utc()
        superseded_impacts = (
            self._storage.count_impacts_for_event(head["event_id"])
            if head is not None and event["event_type"] in (TYPE_EXTENDED, TYPE_REOPENED)
            else 0
        )

        response = self._build_submit_response(
            event, window_from, window_until, impacts, received_at, superseded_impacts
        )
        event_row = {
            "event_id": event["event_id"],
            "event_version": event["event_version"],
            "event_type": event["event_type"],
            "airport_code": airport.code,
            "effective_from": format_instant(event["effective_from"]),
            "effective_until": (
                format_instant(event["effective_until"])
                if event["effective_until"] is not None
                else None
            ),
            "reported_at": format_instant(event["reported_at"]),
            "supersedes_event_id": event["supersedes_event_id"],
            "reason": event["reason"],
            "request_canonical": canonical,
            "response_body": json.dumps(response, ensure_ascii=False),
            "status": "processed",
            "superseded_by": None,
            "received_at": format_instant(received_at),
        }
        impact_rows = [
            self._impact_row(event["event_id"], impact, received_at)
            for impact in impacts
        ]
        supersede_id = (
            event["supersedes_event_id"]
            if event["event_type"] in (TYPE_EXTENDED, TYPE_REOPENED)
            else None
        )
        try:
            self._storage.record_event(event_row, impact_rows, supersede_id)
        except EventStoreConflict as exc:
            # Lost a race against a concurrent submission of the same id.
            raise ApiError(
                409,
                "event_id_conflict",
                f"event_id {event['event_id']!r} was already submitted.",
            ) from exc
        return 201, response

    # ------------------------------------------------------------------
    # closure-chain semantics
    # ------------------------------------------------------------------
    def _validate_chain(
        self, event: dict[str, Any], head: dict[str, Any] | None
    ) -> tuple[datetime, datetime | None]:
        """Enforce chain rules; return the effective closure window."""
        event_type = event["event_type"]
        if event_type == TYPE_CLOSED:
            if event["supersedes_event_id"] is not None:
                raise ApiError(
                    422,
                    "invalid_supersede_target",
                    "airport.closed events open a new chain and must not set "
                    "supersedes_event_id.",
                )
            if head is not None:
                raise ApiError(
                    409,
                    "invalid_state_transition",
                    f"airport {event['airport_code']} already has an open closure "
                    f"(event {head['event_id']!r}); extend or reopen it first.",
                )
            self._validate_window_order(
                event["effective_from"], event["effective_until"]
            )
            return event["effective_from"], event["effective_until"]

        # extended / reopened both continue the existing chain.
        target = self._validate_supersede_target(event)
        if head is None or target["event_id"] != head["event_id"]:
            raise ApiError(
                409,
                "invalid_state_transition",
                f"event {target['event_id']!r} is not the head of an open closure "
                "chain; it may already have been superseded.",
            )
        if event["event_version"] <= target["event_version"]:
            raise ApiError(
                409,
                "event_version_conflict",
                f"event_version {event['event_version']} must be greater than the "
                f"superseded event's version {target['event_version']}.",
            )

        chain_from = parse_instant(target["effective_from"], "effective_from")
        if event_type == TYPE_EXTENDED:
            if event["effective_until"] is None:
                raise ApiError(
                    422,
                    "invalid_time_window",
                    "airport.extended events must set effective_until.",
                )
            if event["effective_from"] != chain_from:
                raise ApiError(
                    422,
                    "invalid_time_window",
                    "airport.extended events must restate the active window's "
                    f"effective_from ({format_instant(chain_from)}).",
                )
            self._validate_window_order(
                event["effective_from"], event["effective_until"]
            )
            if (
                target["effective_until"] is not None
                and event["effective_until"]
                <= parse_instant(target["effective_until"], "effective_until")
            ):
                raise ApiError(
                    422,
                    "invalid_time_window",
                    "effective_until must be later than the superseded event's "
                    f"effective_until ({target['effective_until']}).",
                )
            return chain_from, event["effective_until"]

        # airport.reopened
        if event["effective_until"] is not None:
            raise ApiError(
                422,
                "invalid_time_window",
                "airport.reopened events must not set effective_until.",
            )
        if event["effective_from"] < chain_from:
            raise ApiError(
                422,
                "invalid_time_window",
                "effective_from of a reopening cannot precede the closure start "
                f"({format_instant(chain_from)}).",
            )
        if (
            target["effective_until"] is not None
            and event["effective_from"]
            > parse_instant(target["effective_until"], "effective_until")
        ):
            raise ApiError(
                422,
                "invalid_time_window",
                "the closure already ended at "
                f"{target['effective_until']}; nothing to reopen.",
            )
        return chain_from, event["effective_from"]

    def _validate_supersede_target(self, event: dict[str, Any]) -> dict[str, Any]:
        supersedes = event["supersedes_event_id"]
        if supersedes is None:
            raise ApiError(
                422,
                "invalid_supersede_target",
                f"{event['event_type']} events must set supersedes_event_id.",
            )
        target = self._storage.get_event(supersedes)
        if target is None:
            raise ApiError(
                422,
                "unknown_superseded_event",
                f"supersedes_event_id {supersedes!r} does not match a known event.",
            )
        if target["airport_code"] != event["airport_code"]:
            raise ApiError(
                422,
                "invalid_supersede_target",
                f"event {supersedes!r} belongs to airport "
                f"{target['airport_code']}, not {event['airport_code']}.",
            )
        if target["event_type"] not in (TYPE_CLOSED, TYPE_EXTENDED):
            raise ApiError(
                422,
                "invalid_supersede_target",
                f"event {supersedes!r} has type {target['event_type']!r} and "
                "cannot be superseded.",
            )
        return target

    @staticmethod
    def _validate_window_order(
        effective_from: datetime, effective_until: datetime | None
    ) -> None:
        if effective_until is not None and effective_until <= effective_from:
            raise ApiError(
                422,
                "invalid_time_window",
                "effective_until must be later than effective_from.",
            )

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def get_event(self, event_id: str) -> dict[str, Any]:
        row = self._storage.get_event(event_id)
        if row is None:
            raise ApiError(
                404, "not_found", f"event {event_id!r} does not exist."
            )
        impacts = self._storage.impacts_for_event(event_id)
        return {
            "event_id": row["event_id"],
            "event_version": row["event_version"],
            "event_type": row["event_type"],
            "airport_code": row["airport_code"],
            "status": row["status"],
            "superseded_by": row["superseded_by"],
            "effective_from": row["effective_from"],
            "effective_until": row["effective_until"],
            "reported_at": row["reported_at"],
            "supersedes_event_id": row["supersedes_event_id"],
            "reason": row["reason"],
            "received_at": row["received_at"],
            "impacts": [self._impact_view(impact) for impact in impacts],
            "impact_counts": self._count_statuses(impacts),
        }

    def airport_summary(self, airport_code: str) -> dict[str, Any]:
        airport = self._airports.get(airport_code)
        if airport is None:
            raise ApiError(
                404,
                "unknown_airport",
                f"airport {airport_code!r} is not a known airport.",
            )
        impacts = self._storage.current_impacts_for_airport(airport.code)
        head = self._storage.get_open_closure_head(airport.code)
        summary: dict[str, Any] = {
            "airport_code": airport.code,
            "airport_name": airport.name,
            "timezone": airport.timezone,
            "reopen_buffer_minutes": airport.reopen_buffer_minutes,
            "events_processed": self._storage.count_events_for_airport(airport.code),
            "open_closure": self._closure_view(head, airport) if head else None,
            "current_impacts": self._count_statuses(impacts),
            "affected_flights": len({impact["flight_id"] for impact in impacts}),
            "affected_passengers": sum(
                impact["passenger_count"] for impact in impacts
            ),
        }
        return summary

    def list_flight_impacts(self, params: dict[str, str]) -> dict[str, Any]:
        airport_code = params.get("airport_code")
        if airport_code is not None and airport_code not in self._airports:
            # A well-formed but unknown code filters to nothing; a malformed
            # one is a client error.
            if not airport_code.isalpha() or len(airport_code) != 3:
                raise ApiError(
                    400,
                    "invalid_parameter",
                    "airport_code must be a three-letter code.",
                )
        impact_status = params.get("impact_status")
        if impact_status is not None and impact_status not in IMPACT_STATUSES:
            raise ApiError(
                400,
                "invalid_parameter",
                f"impact_status must be one of {', '.join(IMPACT_STATUSES)}.",
            )
        include_superseded = self._parse_bool(
            params.get("include_superseded", "false"), "include_superseded"
        )
        page = self._parse_int(params.get("page", "1"), "page", minimum=1)
        page_size = self._parse_int(
            params.get("page_size", "20"), "page_size", minimum=1, maximum=100
        )
        rows, total = self._storage.query_impacts(
            airport_code=airport_code,
            impact_status=impact_status,
            include_superseded=include_superseded,
            page=page,
            page_size=page_size,
        )
        return {
            "items": [self._impact_view(row) for row in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": math.ceil(total / page_size) if total else 0,
        }

    def health(self) -> tuple[int, dict[str, Any]]:
        db_ok = self._storage.health_check()
        body = {
            "status": "ok" if db_ok else "error",
            "database": "ok" if db_ok else "error",
            "fixtures": {
                "airports": len(self._airports),
                "flights": len(self._flights),
            },
            "time": format_instant(now_utc()),
        }
        return (200 if db_ok else 503), body

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------
    def _build_submit_response(
        self,
        event: dict[str, Any],
        window_from: datetime,
        window_until: datetime | None,
        impacts: list[Impact],
        received_at: datetime,
        superseded_impacts: int,
    ) -> dict[str, Any]:
        airport = self._airports[event["airport_code"]]
        usable_at = closure_usable_at(window_until, airport.reopen_buffer_minutes)
        return {
            "event_id": event["event_id"],
            "event_version": event["event_version"],
            "event_type": event["event_type"],
            "airport_code": event["airport_code"],
            "status": "processed",
            "received_at": format_instant(received_at),
            "supersedes_event_id": event["supersedes_event_id"],
            "superseded_impacts": superseded_impacts,
            "closure_window": {
                "from": format_instant(window_from),
                "until": format_instant(window_until) if window_until else None,
                "usable_at": format_instant(usable_at) if usable_at else None,
            },
            "impacts": [self._impact_to_view(event["event_id"], i) for i in impacts],
            "impact_counts": self._count_statuses(
                [{"impact_status": i.status} for i in impacts]
            ),
        }

    @staticmethod
    def _count_statuses(impacts: list[dict[str, Any]]) -> dict[str, int]:
        counts = {status: 0 for status in IMPACT_STATUSES}
        for impact in impacts:
            counts[impact["impact_status"]] += 1
        counts["total"] = len(impacts)
        return counts

    def _closure_view(
        self, head: dict[str, Any], airport: Airport
    ) -> dict[str, Any]:
        window_from = parse_instant(head["effective_from"], "effective_from")
        window_until = (
            parse_instant(head["effective_until"], "effective_until")
            if head["effective_until"]
            else None
        )
        usable_at = closure_usable_at(window_until, airport.reopen_buffer_minutes)
        return {
            "event_id": head["event_id"],
            "event_type": head["event_type"],
            "effective_from": head["effective_from"],
            "effective_until": head["effective_until"],
            "usable_at": format_instant(usable_at) if usable_at else None,
            "local": {
                "timezone": airport.timezone,
                "effective_from": window_from.astimezone(airport.tz).isoformat(),
                "effective_until": (
                    window_until.astimezone(airport.tz).isoformat()
                    if window_until
                    else None
                ),
            },
        }

    @staticmethod
    def _impact_row(event_id: str, impact: Impact, created_at: datetime) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "flight_id": impact.flight.flight_id,
            "flight_number": impact.flight.flight_number,
            "origin": impact.flight.origin,
            "destination": impact.flight.destination,
            "airport_code": impact.airport_code,
            "affected_point": impact.affected_point,
            "impact_status": impact.status,
            "delay_minutes": impact.delay_minutes,
            "passenger_count": impact.flight.passenger_count,
            "scheduled_departure": format_instant(impact.flight.scheduled_departure),
            "scheduled_arrival": format_instant(impact.flight.scheduled_arrival),
            "closure_from": format_instant(impact.closure_from),
            "closure_until": (
                format_instant(impact.closure_until) if impact.closure_until else None
            ),
            "usable_at": format_instant(impact.usable_at) if impact.usable_at else None,
            "superseded": 0,
            "created_at": format_instant(created_at),
        }

    @staticmethod
    def _impact_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "flight_id": row["flight_id"],
            "flight_number": row["flight_number"],
            "origin": row["origin"],
            "destination": row["destination"],
            "airport_code": row["airport_code"],
            "affected_point": row["affected_point"],
            "impact_status": row["impact_status"],
            "delay_minutes": row["delay_minutes"],
            "passenger_count": row["passenger_count"],
            "scheduled_departure": row["scheduled_departure"],
            "scheduled_arrival": row["scheduled_arrival"],
            "superseded": bool(row["superseded"]),
            "closure_window": {
                "from": row["closure_from"],
                "until": row["closure_until"],
                "usable_at": row["usable_at"],
            },
        }

    def _impact_to_view(self, event_id: str, impact: Impact) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "flight_id": impact.flight.flight_id,
            "flight_number": impact.flight.flight_number,
            "origin": impact.flight.origin,
            "destination": impact.flight.destination,
            "airport_code": impact.airport_code,
            "affected_point": impact.affected_point,
            "impact_status": impact.status,
            "delay_minutes": impact.delay_minutes,
            "passenger_count": impact.flight.passenger_count,
            "scheduled_departure": format_instant(impact.flight.scheduled_departure),
            "scheduled_arrival": format_instant(impact.flight.scheduled_arrival),
            "superseded": False,
            "closure_window": {
                "from": format_instant(impact.closure_from),
                "until": (
                    format_instant(impact.closure_until)
                    if impact.closure_until
                    else None
                ),
                "usable_at": (
                    format_instant(impact.usable_at) if impact.usable_at else None
                ),
            },
        }

    # ------------------------------------------------------------------
    # parameter parsing helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_bool(value: str, field: str) -> bool:
        if value.lower() in ("true", "1"):
            return True
        if value.lower() in ("false", "0"):
            return False
        raise ApiError(
            400, "invalid_parameter", f"{field} must be 'true' or 'false'."
        )

    @staticmethod
    def _parse_int(
        value: str, field: str, minimum: int, maximum: int | None = None
    ) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ApiError(
                400, "invalid_parameter", f"{field} must be an integer."
            ) from None
        if parsed < minimum or (maximum is not None and parsed > maximum):
            bound = f">= {minimum}"
            if maximum is not None:
                bound += f" and <= {maximum}"
            raise ApiError(
                400, "invalid_parameter", f"{field} must be {bound}."
            )
        return parsed
