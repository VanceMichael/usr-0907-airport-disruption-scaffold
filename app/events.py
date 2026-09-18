"""Event intake, the closure lifecycle state machine and read-side queries.

Lifecycle per airport (a "closure chain"):

    airport.closed     opens a new closure (airport must not have an active one)
    airport.extended   moves the active closure's end (supersedes the chain head)
    airport.reopened   closes the window at effective_from (supersedes the head)

Every submission is validated, applied and persisted in a single SQLite
transaction; ``event_id`` is the idempotency key — a byte-identical replay
returns the originally computed result, a conflicting payload is rejected.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from .errors import ApiError
from .impacts import STATUSES, compute_impacts
from .timeutil import format_local, format_utc, parse_timestamp, utcnow
from .validation import (
    EVENT_CLOSED,
    EVENT_EXTENDED,
    EVENT_REOPENED,
    validate_and_normalize,
)

MAX_PAGE_SIZE = 100


class EventService:
    def __init__(self, storage, schema, airports, flights):
        self.storage = storage
        self.schema = schema
        self.airports = airports          # code -> Airport
        self.flights = flights            # list[Flight]
        self.flights_by_id = {f.flight_id: f for f in flights}

    # -------------------------------------------------------------- intake

    def submit(self, payload) -> tuple[int, dict]:
        event = validate_and_normalize(payload, self.schema)

        existing = self.storage.get_event(event.event_id)
        if existing is not None:
            return self._replay(existing, event)

        airport = self.airports.get(event.airport_code)
        if airport is None:
            raise ApiError(
                422, "unknown_airport",
                f"airport_code '{event.airport_code}' is not present in the airport fixtures",
                [{"field": "airport_code", "issue": "unknown airport"}],
            )
        self._check_payload_rules(event)

        received_at = utcnow()
        try:
            with self.storage.transaction() as conn:
                body = self._apply(conn, event, airport, received_at)
        except sqlite3.IntegrityError:
            # A concurrent request persisted the same event_id first.
            stored = self.storage.get_event(event.event_id)
            if stored is not None:
                return self._replay(stored, event)
            raise
        return 201, body

    @staticmethod
    def _replay(existing: dict, event) -> tuple[int, dict]:
        if existing["payload_hash"] != event.payload_hash:
            raise ApiError(
                409, "event_conflict",
                f"event_id '{event.event_id}' was already submitted with a different payload",
            )
        body = json.loads(existing["result_json"])
        body["idempotent_replay"] = True
        return 200, body

    @staticmethod
    def _check_payload_rules(event) -> None:
        if event.effective_until is not None and event.effective_until <= event.effective_from:
            raise ApiError(
                422, "invalid_time_window",
                "effective_until must be later than effective_from",
                [{"field": "effective_until", "issue": "window end is not after window start"}],
            )
        if event.event_type == EVENT_CLOSED and event.supersedes_event_id is not None:
            raise ApiError(
                422, "invalid_event_sequence",
                "airport.closed starts a new closure and must not set supersedes_event_id",
                [{"field": "supersedes_event_id", "issue": "not allowed for airport.closed"}],
            )
        if event.event_type in (EVENT_EXTENDED, EVENT_REOPENED) and event.supersedes_event_id is None:
            raise ApiError(
                422, "invalid_event_sequence",
                f"{event.event_type} must reference the active closure head via supersedes_event_id",
                [{"field": "supersedes_event_id", "issue": "field is required for this event type"}],
            )
        if event.event_type == EVENT_REOPENED and event.effective_until is not None:
            raise ApiError(
                422, "invalid_event_sequence",
                "airport.reopened must not set effective_until; effective_from is the reopen instant",
                [{"field": "effective_until", "issue": "not allowed for airport.reopened"}],
            )

    def _apply(self, conn, event, airport, received_at) -> dict:
        if event.event_type == EVENT_CLOSED:
            closure, impacts = self._apply_closed(conn, event, airport)
        elif event.event_type == EVENT_EXTENDED:
            closure, impacts = self._apply_extended(conn, event, airport)
        else:
            closure, impacts = self._apply_reopened(conn, event, airport)

        body = self._result_body(event, airport, closure, impacts)
        self.storage.insert_event(conn, {
            "event_id": event.event_id,
            "event_version": event.event_version,
            "event_type": event.event_type,
            "airport_code": event.airport_code,
            "effective_from_utc": format_utc(event.effective_from),
            "effective_until_utc": format_utc(event.effective_until) if event.effective_until else None,
            "reported_at_utc": format_utc(event.reported_at),
            "supersedes_event_id": event.supersedes_event_id,
            "reason": event.reason,
            "closure_id": closure["closure_id"],
            "payload_hash": event.payload_hash,
            "payload_json": event.canonical_json,
            "result_json": json.dumps(body),
            "received_at_utc": format_utc(received_at),
            "processed_at_utc": format_utc(utcnow()),
        })
        return body

    # ----------------------------------------------------- state machine

    def _apply_closed(self, conn, event, airport):
        active = self.storage.get_active_closure(airport.code, conn)
        if active is not None:
            raise ApiError(
                409, "state_conflict",
                f"airport {airport.code} already has active closure '{active['closure_id']}';"
                " use airport.extended or airport.reopened",
            )
        closure = {
            "closure_id": event.event_id,
            "airport_code": airport.code,
            "start_utc": format_utc(event.effective_from),
            "end_utc": format_utc(event.effective_until) if event.effective_until else None,
            "status": "active",
            "head_event_id": event.event_id,
            "head_version": event.event_version,
        }
        self.storage.insert_closure(conn, closure)
        impacts = self._compute_and_store(conn, airport, closure, event)
        return closure, impacts

    def _apply_extended(self, conn, event, airport):
        closure = self._active_closure_or_conflict(conn, event, airport)
        previous_end = parse_timestamp(closure["end_utc"]) if closure["end_utc"] else None
        new_end = event.effective_until
        if previous_end is None and new_end is None:
            raise ApiError(
                409, "state_conflict",
                f"closure '{closure['closure_id']}' is already indefinite",
            )
        if previous_end is not None and new_end is not None and new_end <= previous_end:
            raise ApiError(
                422, "invalid_time_window",
                f"effective_until {format_utc(new_end)} does not extend the current"
                f" closure end {closure['end_utc']}",
                [{"field": "effective_until", "issue": "must be later than the current closure end"}],
            )
        closure["end_utc"] = format_utc(new_end) if new_end else None
        closure["head_event_id"] = event.event_id
        closure["head_version"] = event.event_version
        self.storage.update_closure_head(
            conn, closure["closure_id"],
            end_utc=closure["end_utc"], status="active",
            head_event_id=event.event_id, head_version=event.event_version,
        )
        impacts = self._compute_and_store(conn, airport, closure, event)
        return closure, impacts

    def _apply_reopened(self, conn, event, airport):
        closure = self._active_closure_or_conflict(conn, event, airport)
        start = parse_timestamp(closure["start_utc"])
        scheduled_end = parse_timestamp(closure["end_utc"]) if closure["end_utc"] else None
        reopen_at = event.effective_from
        if reopen_at <= start:
            raise ApiError(
                422, "invalid_time_window",
                "reopen time (effective_from) must be later than the closure start",
                [{"field": "effective_from", "issue": "reopen instant is not after the closure start"}],
            )
        if scheduled_end is not None and reopen_at > scheduled_end:
            raise ApiError(
                422, "invalid_time_window",
                "reopen time (effective_from) is after the closure's scheduled end",
                [{"field": "effective_from", "issue": "closure already ends before this instant"}],
            )
        closure["end_utc"] = format_utc(reopen_at)
        closure["status"] = "reopened"
        closure["head_event_id"] = event.event_id
        closure["head_version"] = event.event_version
        self.storage.update_closure_head(
            conn, closure["closure_id"],
            end_utc=closure["end_utc"], status="reopened",
            head_event_id=event.event_id, head_version=event.event_version,
        )
        impacts = self._compute_and_store(conn, airport, closure, event)
        return closure, impacts

    def _active_closure_or_conflict(self, conn, event, airport):
        closure = self.storage.get_active_closure(airport.code, conn)
        if closure is None:
            raise ApiError(
                409, "state_conflict",
                f"airport {airport.code} has no active closure for {event.event_type}",
            )
        supersedes = event.supersedes_event_id
        if self.storage.get_event(supersedes, conn) is None:
            raise ApiError(
                409, "state_conflict",
                f"supersedes_event_id '{supersedes}' does not match a known event",
                [{"field": "supersedes_event_id", "issue": "unknown event"}],
            )
        if closure["head_event_id"] != supersedes:
            raise ApiError(
                409, "state_conflict",
                f"supersedes_event_id '{supersedes}' is not the head of the active closure"
                f" (current head is '{closure['head_event_id']}')",
                [{"field": "supersedes_event_id", "issue": "does not match the active closure head"}],
            )
        if event.event_version <= closure["head_version"]:
            raise ApiError(
                409, "state_conflict",
                f"event_version {event.event_version} must be greater than the superseded"
                f" event's version {closure['head_version']}",
                [{"field": "event_version", "issue": "version must increase along the closure chain"}],
            )
        return closure

    # -------------------------------------------------------- computation

    def _compute_and_store(self, conn, airport, closure, event) -> list[dict]:
        start = parse_timestamp(closure["start_utc"])
        end = parse_timestamp(closure["end_utc"]) if closure["end_utc"] else None
        buffered_end = end + timedelta(minutes=airport.reopen_buffer_minutes) if end else None
        impacts = compute_impacts(airport, self.flights, start, buffered_end)
        rows = [{
            "closure_id": closure["closure_id"],
            "airport_code": airport.code,
            "flight_id": impact.flight_id,
            "impact_status": impact.status,
            "delay_minutes": impact.delay_minutes,
            "touch_kind": impact.touch_kind,
            "touch_time_utc": format_utc(impact.touch_time),
            "window_start_utc": closure["start_utc"],
            "window_end_utc": closure["end_utc"],
            "computed_by_event_id": event.event_id,
        } for impact in impacts]
        self.storage.replace_impacts(conn, closure["closure_id"], rows)
        return rows

    # ------------------------------------------------------------- queries

    def event_status(self, event_id: str) -> dict:
        row = self.storage.get_event(event_id)
        if row is None:
            raise ApiError(404, "event_not_found", f"no event with id '{event_id}'")
        closure = self.storage.get_closure(row["closure_id"])
        airport = self.airports[row["airport_code"]]
        impacts = self.storage.impacts_for_closure(row["closure_id"])
        return {
            "event_id": row["event_id"],
            "status": "processed",
            "event": json.loads(row["payload_json"]),
            "event_type": row["event_type"],
            "airport_code": row["airport_code"],
            "closure_id": row["closure_id"],
            "closure_status": closure["status"],
            "window": self._window_body(airport, closure["start_utc"], closure["end_utc"]),
            "impact_summary": self._summarize(impacts),
            "affected_flights": [self._impact_body(impact) for impact in impacts],
            "received_at": row["received_at_utc"],
            "processed_at": row["processed_at_utc"],
        }

    def airport_summary(self, airport_code: str) -> dict:
        airport = self.airports.get(airport_code)
        if airport is None:
            raise ApiError(404, "airport_not_found", f"unknown airport code '{airport_code}'")
        closures = []
        totals = {status: 0 for status in STATUSES}
        totals.update({"affected_flights": 0, "affected_passengers": 0})
        active = None
        for closure in self.storage.closures_for_airport(airport_code):
            impacts = self.storage.impacts_for_closure(closure["closure_id"])
            summary = self._summarize(impacts)
            for key in totals:
                totals[key] += summary[key]
            window = self._window_body(airport, closure["start_utc"], closure["end_utc"])
            if closure["status"] == "active":
                active = {"closure_id": closure["closure_id"], "window": window}
            closures.append({
                "closure_id": closure["closure_id"],
                "status": closure["status"],
                "window": window,
                "impact_summary": summary,
            })
        return {
            "airport_code": airport.code,
            "airport_name": airport.name,
            "timezone": airport.timezone,
            "active_closure": active,
            "totals": totals,
            "closures": closures,
        }

    def list_impacts(self, params: dict) -> dict:
        allowed = {"airport_code", "impact_status", "page", "page_size"}
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise ApiError(
                400, "invalid_query",
                f"unsupported query parameter(s): {', '.join(unknown)}",
            )
        airport_code = params.get("airport_code")
        if airport_code is not None:
            if not isinstance(airport_code, str) or len(airport_code) != 3 \
                    or not airport_code.isalpha() or not airport_code.isupper():
                raise ApiError(
                    400, "invalid_query",
                    "airport_code must be a three-letter uppercase code",
                    [{"field": "airport_code", "issue": "expected pattern ^[A-Z]{3}$"}],
                )
            if airport_code not in self.airports:
                raise ApiError(404, "airport_not_found", f"unknown airport code '{airport_code}'")
        impact_status = params.get("impact_status")
        if impact_status is not None and impact_status not in STATUSES:
            raise ApiError(
                400, "invalid_query",
                f"impact_status must be one of {list(STATUSES)}",
                [{"field": "impact_status", "issue": "unknown impact status"}],
            )
        page = self._parse_int_param(params.get("page"), default=1, minimum=1, name="page")
        page_size = self._parse_int_param(
            params.get("page_size"), default=20, minimum=1, maximum=MAX_PAGE_SIZE, name="page_size")

        total, rows = self.storage.query_impacts(airport_code, impact_status, page, page_size)
        return {
            "page": page,
            "page_size": page_size,
            "total": total,
            "items": [self._impact_body(row) for row in rows],
        }

    @staticmethod
    def _parse_int_param(raw, *, default, minimum, maximum=None, name) -> int:
        if raw is None:
            return default
        try:
            value = int(raw, 10)
        except (TypeError, ValueError):
            raise ApiError(
                400, "invalid_query", f"{name} must be an integer",
                [{"field": name, "issue": "not an integer"}],
            ) from None
        if value < minimum or (maximum is not None and value > maximum):
            bound = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
            raise ApiError(
                400, "invalid_query", f"{name} must be {bound}",
                [{"field": name, "issue": "out of range"}],
            )
        return value

    def health(self) -> dict:
        return {"status": "ok", "database": "ok" if self.storage.health_check() else "unavailable"}

    # ------------------------------------------------------------ rendering

    def _result_body(self, event, airport, closure, impacts) -> dict:
        return {
            "event_id": event.event_id,
            "status": "processed",
            "idempotent_replay": False,
            "event_type": event.event_type,
            "airport_code": airport.code,
            "closure_id": closure["closure_id"],
            "closure_status": closure["status"],
            "window": self._window_body(airport, closure["start_utc"], closure["end_utc"]),
            "impact_summary": self._summarize(impacts),
            "affected_flights": [self._impact_body(impact) for impact in impacts],
        }

    def _window_body(self, airport, start_utc: str, end_utc: str | None) -> dict:
        start = parse_timestamp(start_utc)
        end = parse_timestamp(end_utc) if end_utc else None
        return {
            "start_utc": format_utc(start),
            "end_utc": format_utc(end) if end else None,
            "start_local": format_local(start, airport.timezone),
            "end_local": format_local(end, airport.timezone) if end else None,
            "reopen_buffer_minutes": airport.reopen_buffer_minutes,
        }

    def _impact_body(self, row: dict) -> dict:
        flight = self.flights_by_id.get(row["flight_id"])
        body = {
            "flight_id": row["flight_id"],
            "airport_code": row["airport_code"],
            "closure_id": row["closure_id"],
            "touch_kind": row["touch_kind"],
            "touch_time_utc": row["touch_time_utc"],
            "impact_status": row["impact_status"],
            "delay_minutes": row["delay_minutes"],
            "window": {"start_utc": row["window_start_utc"], "end_utc": row["window_end_utc"]},
        }
        if flight is not None:
            body.update({
                "flight_number": flight.flight_number,
                "origin": flight.origin,
                "destination": flight.destination,
                "scheduled_departure_utc": format_utc(flight.scheduled_departure),
                "scheduled_arrival_utc": format_utc(flight.scheduled_arrival),
                "passenger_count": flight.passenger_count,
            })
        return body

    def _summarize(self, impacts: list[dict]) -> dict:
        summary = {status: 0 for status in STATUSES}
        passengers = 0
        for row in impacts:
            summary[row["impact_status"]] += 1
            flight = self.flights_by_id.get(row["flight_id"])
            if flight is not None:
                passengers += flight.passenger_count
        summary["affected_flights"] = len(impacts)
        summary["affected_passengers"] = passengers
        return summary
