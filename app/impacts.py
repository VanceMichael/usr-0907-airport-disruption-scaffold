"""Impact classification of flights against a closure window.

All comparisons use absolute UTC instants, so windows crossing midnight
(UTC or airport-local) behave correctly with no special casing.  The window
end handed to :func:`compute_impacts` already includes the airport's
reopen buffer; it may be ``None`` for an indefinite closure.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

STATUS_CANCELLED = "cancelled"
STATUS_DELAYED = "delayed"
STATUS_PENDING = "pending"
STATUSES = (STATUS_CANCELLED, STATUS_DELAYED, STATUS_PENDING)


@dataclass(frozen=True)
class Impact:
    flight_id: str
    touch_kind: str        # "departure" | "arrival"
    touch_time: datetime   # aware, UTC
    status: str            # one of STATUSES
    delay_minutes: int | None  # set only for "delayed"


def compute_impacts(airport, flights, window_start: datetime, window_end: datetime | None) -> list[Impact]:
    """Classify every flight touching ``airport`` inside ``[window_start, window_end)``.

    A flight "touches" the airport when its scheduled departure (origin) or
    scheduled arrival (destination) falls inside the window:

    * window end unknown (indefinite closure) -> ``pending``
    * retimeable and the delay needed to clear the window fits within the
      flight's ``max_delay_minutes`` -> ``delayed``
    * anything else -> ``cancelled``
    """
    impacts: list[Impact] = []
    for flight in flights:
        touches = []
        if flight.origin == airport.code:
            touches.append(("departure", flight.scheduled_departure))
        if flight.destination == airport.code and flight.destination != flight.origin:
            touches.append(("arrival", flight.scheduled_arrival))
        for kind, instant in touches:
            if instant < window_start:
                continue
            if window_end is not None and instant >= window_end:
                continue
            if window_end is None:
                impacts.append(Impact(flight.flight_id, kind, instant, STATUS_PENDING, None))
                continue
            needed_minutes = math.ceil((window_end - instant).total_seconds() / 60)
            if flight.can_retime and needed_minutes <= flight.max_delay_minutes:
                impacts.append(Impact(flight.flight_id, kind, instant, STATUS_DELAYED, needed_minutes))
            else:
                impacts.append(Impact(flight.flight_id, kind, instant, STATUS_CANCELLED, None))
    impacts.sort(key=lambda impact: (impact.touch_time, impact.flight_id))
    return impacts
