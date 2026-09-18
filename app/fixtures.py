"""Loading of the read-only reference fixtures (airports and flights).

The fixtures are part of the service contract: identifiers and field
meanings must not be silently changed, so loading validates the expected
shape and fails fast at startup on any inconsistency.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .timeutil import parse_instant


@dataclass(frozen=True)
class Airport:
    code: str
    name: str
    timezone: str
    reopen_buffer_minutes: int

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class Flight:
    flight_id: str
    flight_number: str
    origin: str
    destination: str
    scheduled_departure: datetime
    scheduled_arrival: datetime
    passenger_count: int
    can_retime: bool
    max_delay_minutes: int


def _load_json(path: Path) -> object:
    if not path.is_file():
        raise RuntimeError(f"fixture file missing: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"fixture file {path} is not valid JSON: {exc}") from exc


def _require(entry: dict, field: str, kind: type, source: str) -> object:
    value = entry.get(field)
    if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
        raise RuntimeError(
            f"{source}: field {field!r} must be of type {kind.__name__}, got {value!r}"
        )
    return value


def load_airports(fixtures_dir: str | Path) -> dict[str, Airport]:
    source = "fixtures/airports.json"
    raw = _load_json(Path(fixtures_dir) / "airports.json")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError(f"{source}: expected a non-empty JSON array")
    airports: dict[str, Airport] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise RuntimeError(f"{source}: every airport must be an object")
        code = _require(entry, "code", str, source)
        name = _require(entry, "name", str, source)
        timezone = _require(entry, "timezone", str, source)
        buffer = _require(entry, "reopen_buffer_minutes", int, source)
        if code in airports:
            raise RuntimeError(f"{source}: duplicate airport code {code!r}")
        airport = Airport(str(code), str(name), str(timezone), int(buffer))
        # Resolve the zone now so a missing tz database fails at startup.
        _ = airport.tz
        airports[airport.code] = airport
    return airports


def load_flights(fixtures_dir: str | Path, airports: dict[str, Airport]) -> list[Flight]:
    source = "fixtures/flights.json"
    raw = _load_json(Path(fixtures_dir) / "flights.json")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError(f"{source}: expected a non-empty JSON array")
    flights: list[Flight] = []
    seen_ids: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise RuntimeError(f"{source}: every flight must be an object")
        flight_id = _require(entry, "flight_id", str, source)
        if flight_id in seen_ids:
            raise RuntimeError(f"{source}: duplicate flight_id {flight_id!r}")
        seen_ids.add(str(flight_id))
        origin = _require(entry, "origin", str, source)
        destination = _require(entry, "destination", str, source)
        for code in (origin, destination):
            if code not in airports:
                raise RuntimeError(
                    f"{source}: flight {flight_id} references unknown airport {code!r}"
                )
        try:
            departure = parse_instant(entry.get("scheduled_departure"), "scheduled_departure")
            arrival = parse_instant(entry.get("scheduled_arrival"), "scheduled_arrival")
        except ValueError as exc:
            raise RuntimeError(f"{source}: flight {flight_id}: {exc}") from exc
        if arrival <= departure:
            raise RuntimeError(
                f"{source}: flight {flight_id} arrives before it departs"
            )
        flights.append(
            Flight(
                flight_id=str(flight_id),
                flight_number=str(_require(entry, "flight_number", str, source)),
                origin=str(origin),
                destination=str(destination),
                scheduled_departure=departure,
                scheduled_arrival=arrival,
                passenger_count=int(_require(entry, "passenger_count", int, source)),
                can_retime=bool(_require(entry, "can_retime", bool, source)),
                max_delay_minutes=int(_require(entry, "max_delay_minutes", int, source)),
            )
        )
    flights.sort(key=lambda f: (f.scheduled_departure, f.flight_id))
    return flights
