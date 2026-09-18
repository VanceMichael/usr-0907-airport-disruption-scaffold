import copy
import unittest
from pathlib import Path

from app.errors import ApiError
from app.validation import load_contract, validate_and_normalize

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = load_contract(REPO_ROOT / "contracts" / "disruption-event.schema.json")


def valid_payload():
    return {
        "event_id": "evt-valid-001",
        "event_version": 1,
        "event_type": "airport.closed",
        "airport_code": "APS",
        "effective_from": "2026-09-07T15:00:00Z",
        "effective_until": "2026-09-07T18:00:00Z",
        "reported_at": "2026-09-07T14:55:00Z",
        "reason": "volcanic ash cloud",
    }


class ContractValidationTest(unittest.TestCase):
    def assert_invalid(self, payload, expected_field):
        with self.assertRaises(ApiError) as ctx:
            validate_and_normalize(payload, SCHEMA)
        err = ctx.exception
        self.assertEqual(err.status, 400)
        self.assertEqual(err.code, "validation_error")
        fields = {detail["field"] for detail in err.details}
        self.assertIn(expected_field, fields, f"details: {err.details}")

    def test_valid_payload_normalizes(self):
        event = validate_and_normalize(valid_payload(), SCHEMA)
        self.assertEqual(event.event_id, "evt-valid-001")
        self.assertIsNotNone(event.payload_hash)

    def test_offset_timestamps_canonicalize_to_same_hash(self):
        a = validate_and_normalize(valid_payload(), SCHEMA)
        payload = valid_payload()
        payload["effective_from"] = "2026-09-07T23:00:00+08:00"  # same instant as 15:00Z
        b = validate_and_normalize(payload, SCHEMA)
        self.assertEqual(a.payload_hash, b.payload_hash)

    def test_missing_required_field(self):
        payload = valid_payload()
        del payload["event_id"]
        self.assert_invalid(payload, "event_id")

    def test_additional_property_rejected(self):
        payload = valid_payload()
        payload["unexpected"] = True
        self.assert_invalid(payload, "unexpected")

    def test_event_id_pattern(self):
        for bad in ("short", "Upper-0001", "-leading-dash", "has space 01", 123):
            payload = valid_payload()
            payload["event_id"] = bad
            self.assert_invalid(payload, "event_id")

    def test_event_id_trailing_newline_rejected(self):
        payload = valid_payload()
        payload["event_id"] = "evt-valid-001\n"
        self.assert_invalid(payload, "event_id")

    def test_event_version_minimum_and_type(self):
        for bad in (0, -1, "1", 1.5, True):
            payload = valid_payload()
            payload["event_version"] = bad
            self.assert_invalid(payload, "event_version")

    def test_event_type_enum(self):
        payload = valid_payload()
        payload["event_type"] = "airport.cancelled"
        self.assert_invalid(payload, "event_type")

    def test_airport_code_pattern(self):
        for bad in ("aps", "AP", "APSS", "A1S"):
            payload = valid_payload()
            payload["airport_code"] = bad
            self.assert_invalid(payload, "airport_code")

    def test_effective_until_may_be_null(self):
        payload = valid_payload()
        payload["effective_until"] = None
        event = validate_and_normalize(payload, SCHEMA)
        self.assertIsNone(event.effective_until)

    def test_effective_until_must_be_datetime_when_present(self):
        payload = valid_payload()
        payload["effective_until"] = "soon"
        self.assert_invalid(payload, "effective_until")

    def test_naive_timestamp_rejected(self):
        payload = valid_payload()
        payload["effective_from"] = "2026-09-07T15:00:00"
        self.assert_invalid(payload, "effective_from")

    def test_reported_at_required_format(self):
        payload = valid_payload()
        payload["reported_at"] = "2026-09-07"
        self.assert_invalid(payload, "reported_at")

    def test_supersedes_pattern(self):
        payload = valid_payload()
        payload["supersedes_event_id"] = "BAD"
        self.assert_invalid(payload, "supersedes_event_id")

    def test_reason_length_bounds(self):
        payload = valid_payload()
        payload["reason"] = ""
        self.assert_invalid(payload, "reason")
        payload = valid_payload()
        payload["reason"] = "x" * 241
        self.assert_invalid(payload, "reason")

    def test_non_object_payload_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            validate_and_normalize(["not", "an", "object"], SCHEMA)
        self.assertEqual(ctx.exception.code, "validation_error")


if __name__ == "__main__":
    unittest.main()
