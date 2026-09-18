import unittest
from datetime import timezone

from app.timeutil import format_local, format_utc, parse_timestamp


class ParseTimestampTest(unittest.TestCase):
    def test_zulu_timestamp(self):
        dt = parse_timestamp("2026-09-07T15:30:00Z")
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual((dt.hour, dt.minute), (15, 30))

    def test_offset_is_normalized_to_utc(self):
        dt = parse_timestamp("2026-09-08T02:00:00+08:00")
        self.assertEqual(format_utc(dt), "2026-09-07T18:00:00Z")

    def test_negative_offset(self):
        dt = parse_timestamp("2026-09-07T10:30:00-05:00")
        self.assertEqual(format_utc(dt), "2026-09-07T15:30:00Z")

    def test_fractional_seconds(self):
        dt = parse_timestamp("2026-09-07T15:30:00.500Z")
        self.assertEqual(dt.microsecond, 500000)

    def test_naive_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            parse_timestamp("2026-09-07T15:30:00")

    def test_date_only_rejected(self):
        with self.assertRaises(ValueError):
            parse_timestamp("2026-09-07")

    def test_space_separator_rejected(self):
        with self.assertRaises(ValueError):
            parse_timestamp("2026-09-07 15:30:00Z")

    def test_missing_seconds_rejected(self):
        with self.assertRaises(ValueError):
            parse_timestamp("2026-09-07T15:30Z")

    def test_invalid_calendar_value_rejected(self):
        with self.assertRaises(ValueError):
            parse_timestamp("2026-13-40T25:70:00Z")

    def test_non_string_rejected(self):
        for value in (None, 123, {}, []):
            with self.assertRaises(ValueError):
                parse_timestamp(value)


class FormatTest(unittest.TestCase):
    def test_format_utc_roundtrip(self):
        self.assertEqual(format_utc(parse_timestamp("2026-09-07T15:30:00Z")),
                         "2026-09-07T15:30:00Z")

    def test_format_local_uses_airport_zone(self):
        dt = parse_timestamp("2026-09-07T16:00:00Z")
        self.assertEqual(format_local(dt, "Asia/Makassar"), "2026-09-08T00:00:00+08:00")
        self.assertEqual(format_local(dt, "Asia/Jakarta"), "2026-09-07T23:00:00+07:00")


if __name__ == "__main__":
    unittest.main()
