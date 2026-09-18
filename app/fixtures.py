"""Loading and validation of the static base data (airports and flights).

Fixtures are read once at startup and treated as immutable reference data.
The loader validates them strictly: a service that starts up has known-good
airport codes, time zones and schedules.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .timeutil import parse_timestamp

_AIRPORT_CODE_RE = re.compile(r"^[A-Z]{3}$")


class FixtureError(Exception):
    """Raised when a fixture file is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class Airport:
    code: str
    name: str
    timezone: str
    reopen_buffer_minutes: int


@dataclass(frozen=True)
class Flight:
    flight_id: str
    flight_number: str
    origin: str
    destination: str
    scheduled_departure: datetime  # aware, UTC
    scheduled_arrival: datetime    # aware, UTC
    passenger_count: int
    can_retime: bool
    max_delay_minutes: int


def _load_json(path: Path):
    if not path.is_file():
        raise FixtureError(f"missing fixture file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"cannot read {path}: {exc}") from exc


def _load_airports(path: Path) -> dict[str, Airport]:
    raw = _load_json(path)
    if not isinstance(raw, list) or not raw:
        raise FixtureError(f"{path}: expected a non-empty JSON array")
    airports: dict[str, Airport] = {}
    for i, entry in enumerate(raw):
        where = f"{path}[{i}]"
        if not isinstance(entry, dict):
            raise FixtureError(f"{where}: expected an object")
        code = entry.get("code")
        if not isinstance(code, str) or not _AIRPORT_CODE_RE.fullmatch(code):
            raise FixtureError(f"{where}: invalid airport code {code!r}")
        if code in airports:
            raise FixtureError(f"{where}: duplicate airport code {code}")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise FixtureError(f"{where}: invalid name {name!r}")
        timezone = entry.get("timezone")
        if not isinstance(timezone, str):
            raise FixtureError(f"{where}: invalid timezone {timezone!r}")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise FixtureError(f"{where}: unknown timezone {timezone!r}") from exc
        buffer = entry.get("reopen_buffer_minutes")
        if not isinstance(buffer, int) or isinstance(buffer, bool) or buffer < 0:
            raise FixtureError(f"{where}: invalid reopen_buffer_minutes {buffer!r}")
        airports[code] = Airport(code, name, timezone, buffer)
    return airports


def _load_flights(path: Path, airports: dict[str, Airport]) -> list[Flight]:
    raw = _load_json(path)
    if not isinstance(raw, list) or not raw:
        raise FixtureError(f"{path}: expected a non-empty JSON array")
    flights: list[Flight] = []
    seen_ids: set[str] = set()
    for i, entry in enumerate(raw):
        where = f"{path}[{i}]"
        if not isinstance(entry, dict):
            raise FixtureError(f"{where}: expected an object")
        flight_id = entry.get("flight_id")
        if not isinstance(flight_id, str) or not flight_id:
            raise FixtureError(f"{where}: invalid flight_id {flight_id!r}")
        if flight_id in seen_ids:
            raise FixtureError(f"{where}: duplicate flight_id {flight_id}")
        seen_ids.add(flight_id)
        flight_number = entry.get("flight_number")
        if not isinstance(flight_number, str) or not flight_number:
            raise FixtureError(f"{where}: invalid flight_number {flight_number!r}")
        origin, destination = entry.get("origin"), entry.get("destination")
        for field, code in (("origin", origin), ("destination", destination)):
            if not isinstance(code, str) or code not in airports:
                raise FixtureError(f"{where}: {field} {code!r} is not a known airport")
        try:
            departure = parse_timestamp(entry.get("scheduled_departure"))
            arrival = parse_timestamp(entry.get("scheduled_arrival"))
        except ValueError as exc:
            raise FixtureError(f"{where}: {exc}") from exc
        if arrival <= departure:
            raise FixtureError(f"{where}: scheduled_arrival must be after scheduled_departure")
        passengers = entry.get("passenger_count")
        if not isinstance(passengers, int) or isinstance(passengers, bool) or passengers < 0:
            raise FixtureError(f"{where}: invalid passenger_count {passengers!r}")
        can_retime = entry.get("can_retime")
        if not isinstance(can_retime, bool):
            raise FixtureError(f"{where}: invalid can_retime {can_retime!r}")
        max_delay = entry.get("max_delay_minutes")
        if not isinstance(max_delay, int) or isinstance(max_delay, bool) or max_delay < 0:
            raise FixtureError(f"{where}: invalid max_delay_minutes {max_delay!r}")
        flights.append(Flight(
            flight_id, flight_number, origin, destination,
            departure, arrival, passengers, can_retime, max_delay,
        ))
    return flights


def load_fixtures(fixtures_dir) -> tuple[dict[str, Airport], list[Flight]]:
    base = Path(fixtures_dir)
    airports = _load_airports(base / "airports.json")
    flights = _load_flights(base / "flights.json", airports)
    return airports, flights
