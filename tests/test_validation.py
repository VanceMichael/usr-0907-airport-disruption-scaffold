"""Contract (schema) validation tests."""
from __future__ import annotations

import pytest

from app.errors import ApiError
from app.validation import validate_event_payload

from conftest import closed_event


def test_valid_payload_normalises_timestamps():
    event = validate_event_payload(
        closed_event(
            "evt-aps-close-001",
            "APS",
            effective_from="2026-09-07T23:00:00+08:00",  # == 15:00Z
        )
    )
    assert event["effective_from"].isoformat() == "2026-09-07T15:00:00+00:00"
    assert event["effective_until"].isoformat() == "2026-09-07T16:00:00+00:00"


def test_non_object_payload_rejected():
    with pytest.raises(ApiError) as excinfo:
        validate_event_payload([1, 2, 3])
    assert excinfo.value.status == 400
    assert excinfo.value.code == "schema_violation"


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_id", "Bad_ID"),
        ("event_id", "short"),
        ("event_id", "evt-" + "x" * 64),
        ("event_version", 0),
        ("event_version", "1"),
        ("event_version", True),
        ("event_type", "airport.closed.x"),
        ("airport_code", "aps"),
        ("airport_code", "AP1"),
        ("airport_code", "APSS"),
    ],
)
def test_field_violations(field, value):
    payload = closed_event("evt-aps-close-001", "APS")
    payload[field] = value
    with pytest.raises(ApiError) as excinfo:
        validate_event_payload(payload)
    error = excinfo.value
    assert error.status == 400
    assert error.code == "schema_violation"
    assert any(d["field"] == field for d in error.details)


def test_missing_required_fields_all_reported():
    with pytest.raises(ApiError) as excinfo:
        validate_event_payload({"event_id": "evt-aps-close-001"})
    missing = {d["field"] for d in excinfo.value.details if "required" in d["issue"]}
    assert missing == {
        "event_version",
        "event_type",
        "airport_code",
        "effective_from",
        "reported_at",
    }


def test_additional_properties_rejected():
    with pytest.raises(ApiError) as excinfo:
        validate_event_payload(
            closed_event("evt-aps-close-001", "APS", operator="nobody")
        )
    assert any(d["field"] == "operator" for d in excinfo.value.details)


def test_naive_timestamp_rejected():
    with pytest.raises(ApiError) as excinfo:
        validate_event_payload(
            closed_event(
                "evt-aps-close-001", "APS", effective_from="2026-09-07T15:00:00"
            )
        )
    assert any(d["field"] == "effective_from" for d in excinfo.value.details)


def test_multiple_violations_reported_together():
    with pytest.raises(ApiError) as excinfo:
        validate_event_payload(
            closed_event(
                "evt-aps-close-001",
                "APS",
                event_version=0,
                effective_from="not-a-time",
                unexpected=1,
            )
        )
    fields = {d["field"] for d in excinfo.value.details}
    assert {"event_version", "effective_from", "unexpected"} <= fields


def test_reason_length_enforced():
    with pytest.raises(ApiError):
        validate_event_payload(closed_event("evt-aps-close-001", "APS", reason=""))
    with pytest.raises(ApiError):
        validate_event_payload(
            closed_event("evt-aps-close-001", "APS", reason="x" * 241)
        )
    event = validate_event_payload(
        closed_event("evt-aps-close-001", "APS", reason="x" * 240)
    )
    assert event["reason"] == "x" * 240
