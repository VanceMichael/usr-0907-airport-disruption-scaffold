import unittest
from datetime import timedelta

from app.fixtures import Airport, Flight
from app.impacts import compute_impacts
from app.timeutil import parse_timestamp

AIRPORT = Airport("APS", "Awan Pura International", "Asia/Makassar", 20)


def make_flight(flight_id, origin, destination, departure, arrival,
                can_retime=True, max_delay_minutes=120):
    return Flight(
        flight_id=flight_id,
        flight_number=flight_id[:5],
        origin=origin,
        destination=destination,
        scheduled_departure=parse_timestamp(departure),
        scheduled_arrival=parse_timestamp(arrival),
        passenger_count=100,
        can_retime=can_retime,
        max_delay_minutes=max_delay_minutes,
    )


def impacts_by_id(impacts):
    return {impact.flight_id: impact for impact in impacts}


class WindowMatchingTest(unittest.TestCase):
    def test_departure_and_arrival_both_counted(self):
        out = make_flight("OUT-1", "APS", "BSR", "2026-09-07T12:00:00Z", "2026-09-07T13:00:00Z")
        inbound = make_flight("IN-1", "BSR", "APS", "2026-09-07T10:00:00Z", "2026-09-07T12:30:00Z")
        start = parse_timestamp("2026-09-07T11:00:00Z")
        end = parse_timestamp("2026-09-07T13:00:00Z")
        found = impacts_by_id(compute_impacts(AIRPORT, [out, inbound], start, end))
        self.assertEqual(found["OUT-1"].touch_kind, "departure")
        self.assertEqual(found["IN-1"].touch_kind, "arrival")

    def test_other_airports_untouched(self):
        flight = make_flight("X-1", "BSR", "KTA", "2026-09-07T12:00:00Z", "2026-09-07T13:00:00Z")
        start = parse_timestamp("2026-09-07T11:00:00Z")
        self.assertEqual(compute_impacts(AIRPORT, [flight], start, None), [])

    def test_window_is_half_open(self):
        flight = make_flight("F-1", "APS", "BSR", "2026-09-07T13:00:00Z", "2026-09-07T14:00:00Z")
        start = parse_timestamp("2026-09-07T12:00:00Z")
        end = parse_timestamp("2026-09-07T13:00:00Z")
        # touch exactly at the end -> outside [start, end)
        self.assertEqual(compute_impacts(AIRPORT, [flight], start, end), [])
        # touch exactly at the start -> inside
        self.assertEqual(len(compute_impacts(AIRPORT, [flight], end, parse_timestamp("2026-09-07T14:00:00Z"))), 1)


class CrossMidnightTest(unittest.TestCase):
    """Windows crossing midnight (UTC or local) must match absolute instants."""

    def test_window_crossing_utc_midnight(self):
        before = make_flight("F-BEFORE", "APS", "BSR", "2026-09-07T21:30:00Z", "2026-09-07T22:00:00Z")
        late = make_flight("F-LATE", "APS", "BSR", "2026-09-07T23:30:00Z", "2026-09-08T00:30:00Z")
        after_midnight = make_flight("F-AM", "APS", "BSR", "2026-09-08T00:15:00Z", "2026-09-08T01:30:00Z")
        too_late = make_flight("F-TOOLATE", "APS", "BSR", "2026-09-08T01:00:00Z", "2026-09-08T02:00:00Z")
        start = parse_timestamp("2026-09-07T22:00:00Z")
        end = parse_timestamp("2026-09-08T01:00:00Z")
        found = impacts_by_id(compute_impacts(AIRPORT, [before, late, after_midnight, too_late], start, end))
        self.assertEqual(set(found), {"F-LATE", "F-AM"})
        self.assertEqual(found["F-AM"].delay_minutes, 45)  # 00:15 -> 01:00 next day

    def test_window_crossing_local_midnight(self):
        # APS is UTC+8; 16:00Z is local midnight. A closure 15:00-17:00Z
        # straddles the local date change and must still match by instant.
        flight = make_flight("F-LOCAL", "BSR", "APS", "2026-09-07T14:30:00Z", "2026-09-07T16:30:00Z")
        start = parse_timestamp("2026-09-07T15:00:00Z")
        end = parse_timestamp("2026-09-07T17:00:00Z")
        found = impacts_by_id(compute_impacts(AIRPORT, [flight], start, end))
        self.assertIn("F-LOCAL", found)
        self.assertEqual(found["F-LOCAL"].delay_minutes, 30)


class ClassificationTest(unittest.TestCase):
    def setUp(self):
        self.start = parse_timestamp("2026-09-07T12:00:00Z")
        self.end = parse_timestamp("2026-09-07T14:00:00Z")

    def test_delayed_when_retime_fits(self):
        flight = make_flight("F-1", "APS", "BSR", "2026-09-07T12:30:00Z", "2026-09-07T13:30:00Z",
                             can_retime=True, max_delay_minutes=120)
        (impact,) = compute_impacts(AIRPORT, [flight], self.start, self.end)
        self.assertEqual(impact.status, "delayed")
        self.assertEqual(impact.delay_minutes, 90)

    def test_cancelled_when_delay_exceeds_allowance(self):
        flight = make_flight("F-1", "APS", "BSR", "2026-09-07T12:30:00Z", "2026-09-07T13:30:00Z",
                             can_retime=True, max_delay_minutes=30)
        (impact,) = compute_impacts(AIRPORT, [flight], self.start, self.end)
        self.assertEqual(impact.status, "cancelled")
        self.assertIsNone(impact.delay_minutes)

    def test_cancelled_when_not_retimeable(self):
        flight = make_flight("F-1", "APS", "BSR", "2026-09-07T12:30:00Z", "2026-09-07T13:30:00Z",
                             can_retime=False, max_delay_minutes=0)
        (impact,) = compute_impacts(AIRPORT, [flight], self.start, self.end)
        self.assertEqual(impact.status, "cancelled")

    def test_pending_when_window_open_ended(self):
        flight = make_flight("F-1", "APS", "BSR", "2026-09-07T12:30:00Z", "2026-09-07T13:30:00Z")
        (impact,) = compute_impacts(AIRPORT, [flight], self.start, None)
        self.assertEqual(impact.status, "pending")

    def test_delay_rounds_up_to_whole_minutes(self):
        flight = make_flight("F-1", "APS", "BSR", "2026-09-07T12:00:00Z", "2026-09-07T13:00:00Z",
                             max_delay_minutes=121)
        end = self.start + timedelta(seconds=120 * 60 + 30)
        (impact,) = compute_impacts(AIRPORT, [flight], self.start, end)
        self.assertEqual(impact.delay_minutes, 121)


if __name__ == "__main__":
    unittest.main()
