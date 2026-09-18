"""Persistence: data and idempotency survive a full restart."""
from __future__ import annotations

import pytest

from app.errors import ApiError
from conftest import closed_event, make_service


def test_events_and_impacts_survive_restart(tmp_path):
    db_path = tmp_path / "restart.db"

    service = make_service(db_path)
    status, first = service.submit_event(closed_event("evt-aps-close-001", "APS"))
    assert status == 201
    service.submit_event(
        closed_event(
            "evt-kta-close-001",
            "KTA",
            effective_from="2026-09-07T16:00:00Z",
            effective_until="2026-09-08T02:00:00Z",
        )
    )
    service._storage.close()

    # Reopen the same database file with a fresh service instance.
    restarted = make_service(db_path)
    try:
        event = restarted.get_event("evt-aps-close-001")
        assert event["status"] == "processed"
        assert event["impact_counts"]["delayed"] == 1

        summary = restarted.airport_summary("KTA")
        assert summary["current_impacts"]["cancelled"] == 2
        assert summary["open_closure"]["event_id"] == "evt-kta-close-001"

        # Idempotent replay still returns the original, unchanged result.
        status, replay = restarted.submit_event(
            closed_event("evt-aps-close-001", "APS")
        )
        assert status == 200
        assert replay == first

        # Chain rules still apply after restart.
        with pytest.raises(ApiError) as excinfo:
            restarted.submit_event(closed_event("evt-kta-close-002", "KTA"))
        assert excinfo.value.code == "invalid_state_transition"
    finally:
        restarted._storage.close()


def test_response_bodies_are_stored_verbatim(tmp_path):
    db_path = tmp_path / "verbatim.db"
    service = make_service(db_path)
    payload = closed_event("evt-aps-close-001", "APS")
    _, first = service.submit_event(payload)
    service._storage.close()

    restarted = make_service(db_path)
    try:
        _, replay = restarted.submit_event(payload)
        assert replay == first
        assert replay["received_at"] == first["received_at"]
    finally:
        restarted._storage.close()
