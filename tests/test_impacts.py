"""Impact computation tests, incl. cross-midnight and offset handling."""
from __future__ import annotations

from datetime import datetime, timezone

from app.fixtures import Airport, Flight
from app.impacts import compute_impacts
from app.timeutil import parse_instant

AIRPORT = Airport("APS", "Awan Pura International", "Asia/Makassar", 20)


def make_flight(
    flight_id="F-1",
    origin="APS",
    destination="BSR",
    dep="2026-09-07T15:30:00Z",
    arr="2026-09-07T17:10:00Z",
    can_retime=True,
    max_delay=120,
    pax=100,
):
    return Flight(
        flight_id=flight_id,
        flight_number=flight_id,
        origin=origin,
        destination=destination,
        scheduled_departure=parse_instant(dep),
        scheduled_arrival=parse_instant(arr),
        passenger_count=pax,
        can_retime=can_retime,
        max_delay_minutes=max_delay,
    )


def utc(text):
    return parse_instant(text).astimezone(timezone.utc)


def test_departure_inside_window_is_delayed_within_allowance():
    # Window 15:00-16:00 + 20 min buffer -> usable 16:20; dep 15:30 -> 50 min.
    impacts = compute_impacts(AIRPORT, [make_flight()], utc("2026-09-07T15:00:00Z"), utc("2026-09-07T16:00:00Z"))
    assert len(impacts) == 1
    assert impacts[0].status == "delayed"
    assert impacts[0].delay_minutes == 50
    assert impacts[0].affected_point == "departure"


def test_delay_exactly_at_allowance_is_delayed_not_cancelled():
    flight = make_flight(dep="2026-09-07T15:00:00Z", max_delay=80)
    # usable at 16:20 -> exactly 80 minutes of delay.
    impacts = compute_impacts(AIRPORT, [flight], utc("2026-09-07T15:00:00Z"), utc("2026-09-07T16:00:00Z"))
    assert impacts[0].status == "delayed"
    assert impacts[0].delay_minutes == 80


def test_delay_beyond_allowance_is_cancelled():
    flight = make_flight(dep="2026-09-07T15:00:00Z", max_delay=79)
    impacts = compute_impacts(AIRPORT, [flight], utc("2026-09-07T15:00:00Z"), utc("2026-09-07T16:00:00Z"))
    assert impacts[0].status == "cancelled"


def test_non_retunable_flight_is_cancelled():
    flight = make_flight(can_retime=False, max_delay=0)
    impacts = compute_impacts(AIRPORT, [flight], utc("2026-09-07T15:00:00Z"), utc("2026-09-07T16:00:00Z"))
    assert impacts[0].status == "cancelled"


def test_open_ended_window_yields_pending():
    impacts = compute_impacts(AIRPORT, [make_flight()], utc("2026-09-07T15:00:00Z"), None)
    assert impacts[0].status == "pending"
    assert impacts[0].delay_minutes is None


def test_departure_before_window_is_unaffected():
    flight = make_flight(dep="2026-09-07T14:59:00Z")
    impacts = compute_impacts(AIRPORT, [flight], utc("2026-09-07T15:00:00Z"), utc("2026-09-07T16:00:00Z"))
    assert impacts == []


def test_departure_after_usable_at_is_unaffected():
    flight = make_flight(dep="2026-09-07T16:20:00Z")
    impacts = compute_impacts(AIRPORT, [flight], utc("2026-09-07T15:00:00Z"), utc("2026-09-07T16:00:00Z"))
    assert impacts == []


def test_arrival_at_closed_destination():
    flight = make_flight(origin="BSR", destination="APS", dep="2026-09-07T15:05:00Z", arr="2026-09-07T16:10:00Z")
    impacts = compute_impacts(AIRPORT, [flight], utc("2026-09-07T15:00:00Z"), utc("2026-09-07T16:00:00Z"))
    assert len(impacts) == 1
    assert impacts[0].affected_point == "arrival"
    assert impacts[0].delay_minutes == 10  # usable 16:20, arrival 16:10


def test_arrival_after_reopen_is_unaffected():
    flight = make_flight(origin="BSR", destination="APS", dep="2026-09-07T15:05:00Z", arr="2026-09-07T16:45:00Z")
    impacts = compute_impacts(AIRPORT, [flight], utc("2026-09-07T15:00:00Z"), utc("2026-09-07T16:00:00Z"))
    assert impacts == []


def test_cross_midnight_window():
    # 16:00Z -> 02:00Z next day (+ 20 min buffer -> usable 02:10).
    night_owl = make_flight(dep="2026-09-07T23:30:00Z", max_delay=200)
    before = make_flight(flight_id="F-2", dep="2026-09-07T15:30:00Z")
    after = make_flight(flight_id="F-3", dep="2026-09-08T02:30:00Z")
    impacts = compute_impacts(
        AIRPORT,
        [night_owl, before, after],
        utc("2026-09-07T16:00:00Z"),
        utc("2026-09-08T02:00:00Z"),
    )
    assert [i.flight.flight_id for i in impacts] == ["F-1"]
    assert impacts[0].delay_minutes == 170  # 23:30 -> 02:20 next day (buffer 20)


def test_offset_timestamps_normalise_to_same_window():
    # The same instants expressed with +08:00 offsets must behave identically.
    window_from = parse_instant("2026-09-07T23:00:00+08:00")
    window_until = parse_instant("2026-09-08T00:00:00+08:00")
    impacts_offset = compute_impacts(AIRPORT, [make_flight()], window_from, window_until)
    impacts_utc = compute_impacts(
        AIRPORT, [make_flight()], utc("2026-09-07T15:00:00Z"), utc("2026-09-07T16:00:00Z")
    )
    assert [(i.status, i.delay_minutes) for i in impacts_offset] == [
        (i.status, i.delay_minutes) for i in impacts_utc
    ]


def test_window_shorter_than_buffer_still_blocks_operations():
    # A 5-minute closure with a 20-minute buffer blocks 25 minutes.
    flight = make_flight(dep="2026-09-07T15:20:00Z", max_delay=30)
    impacts = compute_impacts(
        AIRPORT, [flight], utc("2026-09-07T15:00:00Z"), utc("2026-09-07T15:05:00Z")
    )
    assert impacts[0].status == "delayed"
    assert impacts[0].delay_minutes == 5  # usable 15:25, dep 15:20
