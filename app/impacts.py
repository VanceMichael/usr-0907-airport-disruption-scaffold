"""Flight impact computation.

A flight is affected by a closure when its operation *at the closed
airport* — its departure from it, or its arrival at it — falls inside the
effective closure window ``[from, until + reopen_buffer)``. Interval
endpoints are compared as absolute UTC instants, so windows crossing local
or UTC midnight behave like any other window.

Impact status rules:

* ``pending``   — the closure is open-ended (``effective_until`` unknown),
  so no cancel/delay decision can be made yet;
* ``delayed``   — the airport becomes usable again (closure end plus the
  airport's reopen buffer) and the flight can absorb the required shift
  within its ``max_delay_minutes`` allowance;
* ``cancelled`` — the flight cannot be retimed, or the required shift
  exceeds its allowance.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .fixtures import Airport, Flight

STATUS_CANCELLED = "cancelled"
STATUS_DELAYED = "delayed"
STATUS_PENDING = "pending"
IMPACT_STATUSES = (STATUS_CANCELLED, STATUS_DELAYED, STATUS_PENDING)


@dataclass(frozen=True)
class Impact:
    flight: Flight
    airport_code: str
    affected_point: str  # "departure" | "arrival"
    status: str
    delay_minutes: int | None
    closure_from: datetime
    closure_until: datetime | None
    usable_at: datetime | None


def closure_usable_at(
    window_until: datetime | None, buffer_minutes: int
) -> datetime | None:
    """When the airport can serve flights again (closure end + buffer)."""
    if window_until is None:
        return None
    return window_until + timedelta(minutes=buffer_minutes)


def compute_impacts(
    airport: Airport,
    flights: list[Flight],
    window_from: datetime,
    window_until: datetime | None,
) -> list[Impact]:
    """Compute the impact of one closure window on the fixture schedule."""
    usable_at = closure_usable_at(window_until, airport.reopen_buffer_minutes)
    impacts: list[Impact] = []
    for flight in flights:
        touch: tuple[str, datetime] | None = None
        if (
            flight.origin == airport.code
            and flight.scheduled_departure >= window_from
            and (usable_at is None or flight.scheduled_departure < usable_at)
        ):
            touch = ("departure", flight.scheduled_departure)
        elif (
            flight.destination == airport.code
            and flight.scheduled_arrival >= window_from
            and (usable_at is None or flight.scheduled_arrival < usable_at)
        ):
            touch = ("arrival", flight.scheduled_arrival)
        if touch is None:
            continue

        affected_point, moment = touch
        if usable_at is None:
            status, delay_minutes = STATUS_PENDING, None
        else:
            delay_seconds = int((usable_at - moment).total_seconds())
            delay_minutes = math.ceil(delay_seconds / 60)
            if flight.can_retime and delay_seconds <= flight.max_delay_minutes * 60:
                status = STATUS_DELAYED
            else:
                status = STATUS_CANCELLED
        impacts.append(
            Impact(
                flight=flight,
                airport_code=airport.code,
                affected_point=affected_point,
                status=status,
                delay_minutes=delay_minutes,
                closure_from=window_from,
                closure_until=window_until,
                usable_at=usable_at,
            )
        )
    return impacts
