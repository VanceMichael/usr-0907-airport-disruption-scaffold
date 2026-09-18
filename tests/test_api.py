"""End-to-end API tests against a real (in-process) HTTP server."""
from __future__ import annotations

from conftest import closed_event

# ---------------------------------------------------------------------------
# event intake
# ---------------------------------------------------------------------------


def test_valid_closure_event_computes_impacts(client):
    status, body = client.post_event(closed_event("evt-aps-close-001", "APS"))
    assert status == 201
    assert body["status"] == "processed"
    assert body["closure_window"] == {
        "from": "2026-09-07T15:00:00Z",
        "until": "2026-09-07T16:00:00Z",
        "usable_at": "2026-09-07T16:20:00Z",
    }
    assert body["impact_counts"] == {
        "cancelled": 0,
        "delayed": 1,
        "pending": 0,
        "total": 1,
    }
    (impact,) = body["impacts"]
    assert impact["flight_id"] == "AX-410-20260907"
    assert impact["impact_status"] == "delayed"
    assert impact["delay_minutes"] == 50


def test_idempotent_replay_returns_original_result(client):
    payload = closed_event("evt-aps-close-001", "APS")
    first_status, first = client.post_event(payload)
    assert first_status == 201
    replay_status, replay = client.post_event(payload)
    assert replay_status == 200
    assert replay == first  # byte-identical content, including received_at


def test_same_id_different_payload_is_conflict(client):
    client.post_event(closed_event("evt-aps-close-001", "APS"))
    status, body = client.post_event(
        closed_event("evt-aps-close-001", "APS", reason="changed")
    )
    assert status == 409
    assert body["error"]["code"] == "event_id_conflict"


def test_unknown_airport_rejected(client):
    status, body = client.post_event(closed_event("evt-zzz-close-001", "ZZZ"))
    assert status == 422
    assert body["error"]["code"] == "unknown_airport"


def test_inverted_window_rejected(client):
    status, body = client.post_event(
        closed_event(
            "evt-aps-close-001",
            "APS",
            effective_until="2026-09-07T14:00:00Z",
        )
    )
    assert status == 422
    assert body["error"]["code"] == "invalid_time_window"


def test_schema_violation_has_stable_structure(client):
    status, body = client.post_event({"event_id": "nope"})
    assert status == 400
    assert body["error"]["code"] == "schema_violation"
    assert isinstance(body["error"]["details"], list)
    assert all("field" in d and "issue" in d for d in body["error"]["details"])


def test_malformed_json_rejected(client):
    status, body = client.request(
        "POST",
        "/events",
        raw_body=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert status == 400
    assert body["error"]["code"] == "malformed_json"


def test_wrong_content_type_rejected(client):
    status, body = client.request(
        "POST", "/events", raw_body=b"{}", headers={"Content-Type": "text/plain"}
    )
    assert status == 415
    assert body["error"]["code"] == "unsupported_media_type"


def test_failed_event_leaves_no_partial_state(client):
    # Invalid window: nothing may be persisted.
    client.post_event(
        closed_event(
            "evt-aps-close-001", "APS", effective_until="2026-09-07T14:00:00Z"
        )
    )
    status, _ = client.get("/events/evt-aps-close-001")
    assert status == 404
    _, summary = client.get("/airports/APS/impact-summary")
    assert summary["events_processed"] == 0
    assert summary["current_impacts"]["total"] == 0


# ---------------------------------------------------------------------------
# closure chain: closed -> extended -> reopened
# ---------------------------------------------------------------------------


def _close_bsr_indefinitely(client):
    return client.post_event(
        closed_event(
            "evt-bsr-close-001",
            "BSR",
            effective_until=None,
        )
    )


def test_open_ended_closure_marks_pending_then_extension_resolves(client):
    status, body = _close_bsr_indefinitely(client)
    assert status == 201
    assert body["impact_counts"]["pending"] == 2
    pending_flights = {i["flight_id"] for i in body["impacts"]}
    assert pending_flights == {"BY-205-20260908", "AX-410-20260907"}

    status, body = client.post_event(
        closed_event(
            "evt-bsr-extend-002",
            "BSR",
            version=2,
            event_type="airport.extended",
            effective_until="2026-09-07T16:30:00Z",
            supersedes_event_id="evt-bsr-close-001",
        )
    )
    assert status == 201
    assert body["superseded_impacts"] == 2
    (impact,) = body["impacts"]
    # BY205 would need 100 minutes but allows only 90 -> cancelled.
    assert impact["flight_id"] == "BY-205-20260908"
    assert impact["impact_status"] == "cancelled"
    assert impact["delay_minutes"] == 100

    # The superseded event's impacts are now historical.
    _, old = client.get("/events/evt-bsr-close-001")
    assert old["superseded_by"] == "evt-bsr-extend-002"
    assert all(i["superseded"] for i in old["impacts"])

    status, body = client.post_event(
        closed_event(
            "evt-bsr-reopen-003",
            "BSR",
            version=3,
            event_type="airport.reopened",
            effective_from="2026-09-07T16:00:00Z",
            effective_until=None,
            supersedes_event_id="evt-bsr-extend-002",
        )
    )
    assert status == 201
    (impact,) = body["impacts"]
    # Reopen 16:00 + 15 min buffer -> usable 16:15; BY205 dep 15:05 -> 70 min.
    assert impact["impact_status"] == "delayed"
    assert impact["delay_minutes"] == 70

    _, summary = client.get("/airports/BSR/impact-summary")
    assert summary["open_closure"] is None
    assert summary["current_impacts"] == {
        "cancelled": 0,
        "delayed": 1,
        "pending": 0,
        "total": 1,
    }


def test_second_closure_while_open_is_conflict(client):
    _close_bsr_indefinitely(client)
    status, body = client.post_event(closed_event("evt-bsr-close-002", "BSR"))
    assert status == 409
    assert body["error"]["code"] == "invalid_state_transition"


def test_extension_requires_higher_version(client):
    _close_bsr_indefinitely(client)
    status, body = client.post_event(
        closed_event(
            "evt-bsr-extend-002",
            "BSR",
            version=1,  # not greater than the head's version
            event_type="airport.extended",
            effective_until="2026-09-07T16:30:00Z",
            supersedes_event_id="evt-bsr-close-001",
        )
    )
    assert status == 409
    assert body["error"]["code"] == "event_version_conflict"


def test_cannot_extend_already_superseded_event(client):
    _close_bsr_indefinitely(client)
    client.post_event(
        closed_event(
            "evt-bsr-extend-002",
            "BSR",
            version=2,
            event_type="airport.extended",
            effective_until="2026-09-07T16:30:00Z",
            supersedes_event_id="evt-bsr-close-001",
        )
    )
    status, body = client.post_event(
        closed_event(
            "evt-bsr-extend-003",
            "BSR",
            version=3,
            event_type="airport.extended",
            effective_until="2026-09-07T17:30:00Z",
            supersedes_event_id="evt-bsr-close-001",  # no longer the head
        )
    )
    assert status == 409
    assert body["error"]["code"] == "invalid_state_transition"


def test_extension_must_move_until_forward(client):
    _close_bsr_indefinitely(client)
    client.post_event(
        closed_event(
            "evt-bsr-extend-002",
            "BSR",
            version=2,
            event_type="airport.extended",
            effective_until="2026-09-07T16:30:00Z",
            supersedes_event_id="evt-bsr-close-001",
        )
    )
    status, body = client.post_event(
        closed_event(
            "evt-bsr-extend-003",
            "BSR",
            version=3,
            event_type="airport.extended",
            effective_until="2026-09-07T16:00:00Z",  # earlier than 16:30
            supersedes_event_id="evt-bsr-extend-002",
        )
    )
    assert status == 422
    assert body["error"]["code"] == "invalid_time_window"


def test_extension_without_supersede_target_rejected(client):
    status, body = client.post_event(
        closed_event(
            "evt-bsr-extend-001",
            "BSR",
            version=2,
            event_type="airport.extended",
            effective_until="2026-09-07T16:30:00Z",
        )
    )
    assert status == 422
    assert body["error"]["code"] == "invalid_supersede_target"


def test_unknown_supersede_target_rejected(client):
    status, body = client.post_event(
        closed_event(
            "evt-bsr-extend-001",
            "BSR",
            version=2,
            event_type="airport.extended",
            effective_until="2026-09-07T16:30:00Z",
            supersedes_event_id="evt-bsr-ghost-000",
        )
    )
    assert status == 422
    assert body["error"]["code"] == "unknown_superseded_event"


def test_reopen_without_open_closure_rejected(client):
    status, body = client.post_event(
        closed_event(
            "evt-bsr-reopen-001",
            "BSR",
            version=2,
            event_type="airport.reopened",
            supersedes_event_id="evt-bsr-ghost-000",
        )
    )
    assert status == 422
    assert body["error"]["code"] == "unknown_superseded_event"


# ---------------------------------------------------------------------------
# cross-midnight + offset timestamps
# ---------------------------------------------------------------------------


def test_cross_midnight_closure_with_offset_timestamps(client):
    status, body = client.post_event(
        closed_event(
            "evt-kta-close-001",
            "KTA",
            effective_from="2026-09-07T23:00:00+07:00",
            effective_until="2026-09-08T09:00:00+07:00",
        )
    )
    assert status == 201
    # Normalised to UTC, the window crosses midnight.
    assert body["closure_window"]["from"] == "2026-09-07T16:00:00Z"
    assert body["closure_window"]["until"] == "2026-09-08T02:00:00Z"
    assert body["closure_window"]["usable_at"] == "2026-09-08T02:10:00Z"
    impacts = {i["flight_id"]: i for i in body["impacts"]}
    assert impacts["AX-412-20260908"]["impact_status"] == "cancelled"
    assert impacts["KX-099-20260908"]["impact_status"] == "cancelled"
    assert impacts["KX-099-20260908"]["delay_minutes"] == 510


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


def _seed_all(client):
    client.post_event(closed_event("evt-aps-close-001", "APS"))
    _close_bsr_indefinitely(client)
    client.post_event(
        closed_event(
            "evt-bsr-reopen-002",
            "BSR",
            version=2,
            event_type="airport.reopened",
            effective_from="2026-09-07T16:00:00Z",
            effective_until=None,
            supersedes_event_id="evt-bsr-close-001",
        )
    )
    client.post_event(
        closed_event(
            "evt-kta-close-001",
            "KTA",
            effective_from="2026-09-07T16:00:00Z",
            effective_until="2026-09-08T02:00:00Z",
        )
    )


def test_event_status_endpoint(client):
    _seed_all(client)
    status, body = client.get("/events/evt-aps-close-001")
    assert status == 200
    assert body["status"] == "processed"
    assert body["superseded_by"] is None
    assert body["impact_counts"]["delayed"] == 1
    status, body = client.get("/events/evt-bsr-close-001")
    assert body["superseded_by"] == "evt-bsr-reopen-002"
    status, _ = client.get("/events/evt-unknown-0000")
    assert status == 404


def test_airport_summary_aggregates_current_impacts(client):
    _seed_all(client)
    _, summary = client.get("/airports/KTA/impact-summary")
    assert summary["current_impacts"] == {
        "cancelled": 2,
        "delayed": 0,
        "pending": 0,
        "total": 2,
    }
    assert summary["affected_flights"] == 2
    assert summary["affected_passengers"] == 142 + 74
    assert summary["open_closure"]["event_id"] == "evt-kta-close-001"
    assert summary["open_closure"]["local"]["timezone"] == "Asia/Jakarta"

    _, summary = client.get("/airports/BSR/impact-summary")
    assert summary["open_closure"] is None
    assert summary["events_processed"] == 2


def test_unknown_airport_summary_is_404(client):
    status, body = client.get("/airports/ZZZ/impact-summary")
    assert status == 404
    assert body["error"]["code"] == "unknown_airport"


def test_flight_impacts_pagination(client):
    _seed_all(client)
    _, page1 = client.get("/impacts/flights?page_size=2&page=1")
    assert page1["total"] == 4
    assert page1["total_pages"] == 2
    assert [i["flight_id"] for i in page1["items"]] == [
        "BY-205-20260908",
        "AX-410-20260907",
    ]
    _, page2 = client.get("/impacts/flights?page_size=2&page=2")
    assert [i["flight_id"] for i in page2["items"]] == [
        "AX-412-20260908",
        "KX-099-20260908",
    ]
    _, page3 = client.get("/impacts/flights?page_size=2&page=3")
    assert page3["items"] == []


def test_flight_impacts_filters(client):
    _seed_all(client)
    _, body = client.get("/impacts/flights?airport_code=KTA")
    assert body["total"] == 2
    assert {i["impact_status"] for i in body["items"]} == {"cancelled"}

    _, body = client.get("/impacts/flights?impact_status=delayed")
    assert body["total"] == 2

    _, body = client.get("/impacts/flights?include_superseded=true")
    assert body["total"] == 6  # 4 current + 2 superseded BSR-chain impacts


def test_flight_impacts_rejects_bad_parameters(client):
    for query in ("page=0", "page_size=0", "page_size=101", "page=abc"):
        status, body = client.get(f"/impacts/flights?{query}")
        assert status == 400, query
        assert body["error"]["code"] == "invalid_parameter"
    status, body = client.get("/impacts/flights?impact_status=unknown")
    assert status == 400


def test_health_endpoint(client):
    status, body = client.get("/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["fixtures"] == {"airports": 3, "flights": 4}


def test_unknown_route_and_method(client):
    status, body = client.get("/does-not-exist")
    assert status == 404
    assert body["error"]["code"] == "not_found"
    status, body = client.request("PUT", "/events", payload={})
    assert status == 405
    assert body["error"]["code"] == "method_not_allowed"
