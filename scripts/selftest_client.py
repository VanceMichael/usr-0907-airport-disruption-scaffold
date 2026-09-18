#!/usr/bin/env python3
"""In-container self-test client for the airport disruption service.

Runs entirely on the Python standard library so it can execute inside the
service image (``docker compose exec disruption-service python ...``).

Phase 1 exercises the functional requirements against a fresh service:
valid events, invalid events, idempotent replay, closure chains, and
cross-midnight windows. Phase 2 runs after the container has been
recreated on the same volume and verifies that everything persisted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

FAILURES: list[str] = []
CHECKS = [0]

STATE_FILE = os.environ.get("SELFTEST_STATE_FILE", "/data/selftest-e1-response.json")


def request(base, method, path, payload=None, raw_body=None, content_type=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        content_type = content_type or "application/json"
    if raw_body is not None:
        data = raw_body
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body) if body else None
        except json.JSONDecodeError:
            return exc.code, None


def check(name, condition, detail=""):
    CHECKS[0] += 1
    if condition:
        print(f"  [PASS] {name}")
    else:
        FAILURES.append(name)
        print(f"  [FAIL] {name} {detail}")


def closed(event_id, airport, version=1, **overrides):
    payload = {
        "event_id": event_id,
        "event_version": version,
        "event_type": "airport.closed",
        "airport_code": airport,
        "effective_from": "2026-09-07T15:00:00Z",
        "effective_until": "2026-09-07T16:00:00Z",
        "reported_at": "2026-09-07T14:45:00Z",
        "reason": "volcanic ash",
    }
    payload.update(overrides)
    return payload


# Deterministic event set shared by both phases.
E1 = closed("evt-aps-close-001", "APS", reason="volcanic ash plume drift")
E2 = closed("evt-bsr-close-001", "BSR", effective_until=None,
            reason="ash cloud over airport")
E3 = closed("evt-bsr-extend-002", "BSR", version=2,
            event_type="airport.extended",
            effective_until="2026-09-07T16:30:00Z",
            supersedes_event_id="evt-bsr-close-001",
            reason="ash persists")
E4 = closed("evt-bsr-reopen-003", "BSR", version=3,
            event_type="airport.reopened",
            effective_from="2026-09-07T16:00:00Z",
            effective_until=None,
            supersedes_event_id="evt-bsr-extend-002",
            reason="ash cleared")
E5 = closed("evt-kta-close-001", "KTA",
            effective_from="2026-09-07T23:00:00+07:00",
            effective_until="2026-09-08T09:00:00+07:00",
            reported_at="2026-09-07T22:30:00+07:00",
            reason="night ash drift")


def phase1(base):
    print("== phase 1: functional checks ==")

    status, body = request(base, "GET", "/health")
    check("health endpoint reports ok", status == 200 and body["status"] == "ok",
          f"got {status} {body}")

    # -- valid closure event -------------------------------------------
    status, e1 = request(base, "POST", "/events", payload=E1)
    check("valid closure event accepted", status == 201, f"got {status} {e1}")
    check("closure window usable_at includes reopen buffer",
          e1["closure_window"]["usable_at"] == "2026-09-07T16:20:00Z",
          str(e1.get("closure_window")))
    impacts = {i["flight_id"]: i for i in e1["impacts"]}
    check("AX410 delayed by 50 minutes",
          impacts.get("AX-410-20260907", {}).get("impact_status") == "delayed"
          and impacts["AX-410-20260907"]["delay_minutes"] == 50,
          str(impacts))
    check("no other APS flights affected", e1["impact_counts"]["total"] == 1,
          str(e1["impact_counts"]))

    # -- idempotent replay ---------------------------------------------
    status, replay = request(base, "POST", "/events", payload=E1)
    check("idempotent replay returns 200 and the original result",
          status == 200 and replay == e1, f"got {status}")
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(e1, handle, sort_keys=True)

    # -- same id, different payload ------------------------------------
    changed = dict(E1, reason="different reason")
    status, body = request(base, "POST", "/events", payload=changed)
    check("same event_id with different payload is 409 event_id_conflict",
          status == 409 and body["error"]["code"] == "event_id_conflict",
          f"got {status} {body}")

    # -- invalid events -------------------------------------------------
    bad_cases = [
        ("lowercase airport code", dict(E1, event_id="evt-bad-000001",
                                        airport_code="aps"),
         400, "schema_violation"),
        ("unknown airport", dict(E1, event_id="evt-bad-000002",
                                 airport_code="ZZZ"),
         422, "unknown_airport"),
        ("effective_until before effective_from",
         dict(E1, event_id="evt-bad-000003", airport_code="BSR",
              effective_until="2026-09-07T14:00:00Z"),
         422, "invalid_time_window"),
        ("naive timestamp", dict(E1, event_id="evt-bad-000004",
                                 effective_from="2026-09-07T15:00:00"),
         400, "schema_violation"),
        ("missing event_version", {k: v for k, v in
                                   dict(E1, event_id="evt-bad-000005").items()
                                   if k != "event_version"},
         400, "schema_violation"),
        ("additional property", dict(E1, event_id="evt-bad-000006", extra=1),
         400, "schema_violation"),
        ("event_version zero", dict(E1, event_id="evt-bad-000007",
                                    event_version=0),
         400, "schema_violation"),
        ("unknown event_type", dict(E1, event_id="evt-bad-000008",
                                    event_type="airport.closed.x"),
         400, "schema_violation"),
    ]
    for name, payload, want_status, want_code in bad_cases:
        status, body = request(base, "POST", "/events", payload=payload)
        check(f"invalid event rejected: {name}",
              status == want_status and body["error"]["code"] == want_code,
              f"got {status} {body}")
        if "event_id" in payload:
            status, _ = request(base, "GET", f"/events/{payload['event_id']}")
            check(f"no partial write for: {name}", status == 404,
                  f"got {status}")

    status, body = request(base, "POST", "/events", raw_body=b"{broken",
                           content_type="application/json")
    check("malformed JSON is 400 malformed_json",
          status == 400 and body["error"]["code"] == "malformed_json",
          f"got {status} {body}")
    status, body = request(base, "POST", "/events", raw_body=b"{}",
                           content_type="text/plain")
    check("wrong content type is 415",
          status == 415 and body["error"]["code"] == "unsupported_media_type",
          f"got {status} {body}")

    # -- closure chain: closed -> extended -> reopened ------------------
    status, e2 = request(base, "POST", "/events", payload=E2)
    check("open-ended closure accepted", status == 201, f"got {status} {e2}")
    check("open-ended closure yields 2 pending impacts",
          e2["impact_counts"]["pending"] == 2, str(e2["impact_counts"]))

    status, e3 = request(base, "POST", "/events", payload=E3)
    check("extension accepted", status == 201, f"got {status} {e3}")
    check("extension supersedes previous impacts",
          e3["superseded_impacts"] == 2, str(e3["superseded_impacts"]))
    impacts = {i["flight_id"]: i for i in e3["impacts"]}
    check("BY205 cancelled by extension (100 min > 90 allowed)",
          impacts.get("BY-205-20260908", {}).get("impact_status") == "cancelled",
          str(impacts))
    check("AX410 clear after extension", "AX-410-20260907" not in impacts,
          str(impacts))

    status, old = request(base, "GET", "/events/evt-bsr-close-001")
    check("superseded event links to its successor",
          status == 200 and old["superseded_by"] == "evt-bsr-extend-002",
          f"got {status} {old}")

    status, e4 = request(base, "POST", "/events", payload=E4)
    check("reopen accepted", status == 201, f"got {status} {e4}")
    impacts = {i["flight_id"]: i for i in e4["impacts"]}
    check("BY205 delayed 70 minutes after reopen",
          impacts.get("BY-205-20260908", {}).get("impact_status") == "delayed"
          and impacts["BY-205-20260908"]["delay_minutes"] == 70,
          str(impacts))

    bad_chain = [
        ("second closure while open",
         closed("evt-bad-000010", "KTA", effective_from="2026-09-08T05:00:00Z"),
         409, "invalid_state_transition"),
        ("extension with stale version",
         closed("evt-bad-000011", "KTA", version=1,
                event_type="airport.extended",
                effective_from="2026-09-07T16:00:00Z",
                effective_until="2026-09-08T04:00:00Z",
                supersedes_event_id="evt-kta-close-001"),
         409, "event_version_conflict"),
        ("extension of unknown event",
         closed("evt-bad-000012", "KTA", version=2,
                event_type="airport.extended",
                effective_from="2026-09-07T16:00:00Z",
                effective_until="2026-09-08T04:00:00Z",
                supersedes_event_id="evt-kta-ghost-000"),
         422, "unknown_superseded_event"),
        ("extension of superseded (non-head) event",
         closed("evt-bad-000013", "BSR", version=9,
                event_type="airport.extended",
                effective_until="2026-09-07T17:00:00Z",
                supersedes_event_id="evt-bsr-close-001"),
         409, "invalid_state_transition"),
        ("extension that does not move until forward",
         closed("evt-bad-000014", "KTA", version=2,
                event_type="airport.extended",
                effective_from="2026-09-07T16:00:00Z",
                effective_until="2026-09-08T01:00:00Z",
                supersedes_event_id="evt-kta-close-001"),
         422, "invalid_time_window"),
    ]
    # KTA closure must exist before the KTA chain checks below run.
    status, e5 = request(base, "POST", "/events", payload=E5)
    check("cross-midnight closure accepted", status == 201, f"got {status} {e5}")
    check("offset timestamps normalised to UTC window",
          e5["closure_window"]["from"] == "2026-09-07T16:00:00Z"
          and e5["closure_window"]["until"] == "2026-09-08T02:00:00Z",
          str(e5["closure_window"]))
    impacts = {i["flight_id"]: i for i in e5["impacts"]}
    check("cross-midnight: AX412 cancelled (cannot retime)",
          impacts.get("AX-412-20260908", {}).get("impact_status") == "cancelled",
          str(impacts))
    check("cross-midnight: KX099 cancelled (510 min > 45 allowed)",
          impacts.get("KX-099-20260908", {}).get("impact_status") == "cancelled"
          and impacts["KX-099-20260908"]["delay_minutes"] == 510,
          str(impacts))

    for name, payload, want_status, want_code in bad_chain:
        status, body = request(base, "POST", "/events", payload=payload)
        check(f"chain rule enforced: {name}",
              status == want_status and body["error"]["code"] == want_code,
              f"got {status} {body}")
        status, _ = request(base, "GET", f"/events/{payload['event_id']}")
        check(f"no partial write for: {name}", status == 404, f"got {status}")

    # -- queries ---------------------------------------------------------
    status, summary = request(base, "GET", "/airports/BSR/impact-summary")
    check("BSR summary: one delayed flight, 131 passengers, no open closure",
          status == 200
          and summary["current_impacts"]["delayed"] == 1
          and summary["affected_passengers"] == 131
          and summary["open_closure"] is None,
          f"got {status} {summary}")

    status, summary = request(base, "GET", "/airports/KTA/impact-summary")
    check("KTA summary: two cancelled flights, 216 passengers, open closure",
          status == 200
          and summary["current_impacts"]["cancelled"] == 2
          and summary["affected_passengers"] == 216
          and summary["open_closure"]["event_id"] == "evt-kta-close-001",
          f"got {status} {summary}")

    status, page1 = request(base, "GET", "/impacts/flights?page_size=2&page=1")
    check("pagination page 1 of 2",
          status == 200 and page1["total"] == 4 and page1["total_pages"] == 2
          and [i["flight_id"] for i in page1["items"]]
          == ["BY-205-20260908", "AX-410-20260907"],
          f"got {status} {page1}")
    status, page2 = request(base, "GET", "/impacts/flights?page_size=2&page=2")
    check("pagination page 2 of 2",
          status == 200
          and [i["flight_id"] for i in page2["items"]]
          == ["AX-412-20260908", "KX-099-20260908"],
          f"got {status} {page2}")
    status, body = request(base, "GET", "/impacts/flights?page=0")
    check("invalid page rejected",
          status == 400 and body["error"]["code"] == "invalid_parameter",
          f"got {status} {body}")
    status, body = request(base, "GET", "/impacts/flights?impact_status=pending")
    check("no pending impacts remain after chain resolution",
          status == 200 and body["total"] == 0, f"got {status} {body}")


def phase2(base):
    print("== phase 2: persistence after container recreation ==")

    status, body = request(base, "GET", "/health")
    check("health endpoint reports ok", status == 200 and body["status"] == "ok",
          f"got {status} {body}")

    status, event = request(base, "GET", "/events/evt-kta-close-001")
    check("cross-midnight event survived restart",
          status == 200 and event["status"] == "processed"
          and event["impact_counts"]["cancelled"] == 2,
          f"got {status} {event}")

    status, event = request(base, "GET", "/events/evt-bsr-close-001")
    check("supersession state survived restart",
          status == 200 and event["superseded_by"] == "evt-bsr-extend-002",
          f"got {status} {event}")

    status, summary = request(base, "GET", "/airports/BSR/impact-summary")
    check("BSR summary survived restart",
          status == 200 and summary["current_impacts"]["delayed"] == 1
          and summary["affected_passengers"] == 131,
          f"got {status} {summary}")

    status, page = request(base, "GET", "/impacts/flights")
    check("impact list survived restart (total 4)",
          status == 200 and page["total"] == 4, f"got {status} {page}")

    with open(STATE_FILE, "r", encoding="utf-8") as handle:
        original = json.load(handle)
    status, replay = request(base, "POST", "/events", payload=E1)
    check("idempotent replay still returns the original result after restart",
          status == 200 and replay == original, f"got {status}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", type=int, choices=(1, 2), required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    args = parser.parse_args()

    if args.phase == 1:
        phase1(args.base_url)
    else:
        phase2(args.base_url)

    print(f"\n{CHECKS[0]} checks, {len(FAILURES)} failures")
    if FAILURES:
        print("FAILED: " + ", ".join(FAILURES))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
