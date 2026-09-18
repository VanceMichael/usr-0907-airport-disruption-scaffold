#!/usr/bin/env python3
"""End-to-end checks for the airport disruption API, run inside the container.

Executed by scripts/selftest.sh via ``docker exec`` so the whole self-test
needs nothing on the host except Docker itself.  Uses only the standard
library and never talks to anything but the local service.

    python selftest_client.py --base-url http://127.0.0.1:8080 --phase before-restart
    python selftest_client.py --base-url http://127.0.0.1:8080 --phase after-restart
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

CHECKS = 0


def check(name, condition, extra=""):
    global CHECKS
    if not condition:
        print(f"FAIL: {name} {extra}", flush=True)
        sys.exit(1)
    CHECKS += 1
    print(f"ok {CHECKS:02d} - {name}", flush=True)


class Client:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def request(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def post_event(self, payload):
        return self.request("POST", "/v1/events", payload)

    def get(self, path):
        return self.request("GET", path)


def event(event_id, event_type="airport.closed", airport="APS", version=1,
          effective_from="2026-09-07T15:00:00Z", effective_until="__omit__",
          supersedes=None, reason=None):
    payload = {
        "event_id": event_id,
        "event_version": version,
        "event_type": event_type,
        "airport_code": airport,
        "effective_from": effective_from,
        "reported_at": "2026-09-07T14:00:00Z",
    }
    if effective_until != "__omit__":
        payload["effective_until"] = effective_until
    if supersedes is not None:
        payload["supersedes_event_id"] = supersedes
    if reason is not None:
        payload["reason"] = reason
    return payload


def expect_error(client, name, payload, expected_status, expected_code):
    status, body = client.post_event(payload)
    check(f"{name}: HTTP {expected_status}", status == expected_status, f"got {status}: {body}")
    check(f"{name}: error code {expected_code}",
          body.get("error", {}).get("code") == expected_code, f"got {body}")
    check(f"{name}: stable error envelope",
          isinstance(body.get("error", {}).get("message"), str)
          and isinstance(body.get("error", {}).get("details"), list))
    # no partial write: the rejected event must not exist
    follow_status, follow_body = client.get(f"/v1/events/{payload['event_id']}")
    check(f"{name}: no partial write",
          follow_status == 404 and follow_body.get("error", {}).get("code") == "event_not_found",
          f"got {follow_status}: {follow_body}")


# --------------------------------------------------------------------- phases

def phase_before_restart(client):
    status, body = client.get("/healthz")
    check("health endpoint reports ok", status == 200 and body.get("status") == "ok", body)

    # ---- invalid submissions: stable structured errors, no partial writes
    expect_error(client, "lowercase airport code",
                 event("e2e-bad-airport-01", airport="aps"), 400, "validation_error")
    expect_error(client, "unknown airport code",
                 event("e2e-bad-airport-02", airport="ZZZ"), 422, "unknown_airport")
    expect_error(client, "inverted time window",
                 event("e2e-bad-window-01", effective_until="2026-09-07T14:00:00Z"),
                 422, "invalid_time_window")
    expect_error(client, "event_version below minimum",
                 {**event("e2e-bad-version-01"), "event_version": 0}, 400, "validation_error")
    expect_error(client, "additional property",
                 {**event("e2e-bad-extra-001"), "operator": "nobody"}, 400, "validation_error")
    expect_error(client, "naive timestamp",
                 event("e2e-bad-naive-001", effective_from="2026-09-07 15:00"), 400, "validation_error")
    expect_error(client, "unknown event type",
                 event("e2e-bad-type-0001", event_type="airport.cancelled"), 400, "validation_error")
    expect_error(client, "extension without supersedes",
                 event("e2e-bad-seq-00001", event_type="airport.extended",
                       effective_until="2026-09-07T18:00:00Z"), 422, "invalid_event_sequence")

    # ---- valid closure with a window crossing UTC and local midnight
    # APS (UTC+8): 18:00Z -> 00:30Z next day.  Only KX-099 (arrival 19:15Z,
    # i.e. 03:15 local next day) falls inside; 335 min needed > 45 allowed.
    status, body = client.post_event(event(
        "e2e-aps-close-01", effective_from="2026-09-07T18:00:00Z",
        effective_until="2026-09-08T00:30:00Z", reason="volcanic ash"))
    check("cross-midnight closure accepted", status == 201, body)
    check("cross-midnight window normalized",
          body["window"]["start_utc"] == "2026-09-07T18:00:00Z"
          and body["window"]["end_utc"] == "2026-09-08T00:30:00Z", body["window"])
    affected = {f["flight_id"]: f for f in body["affected_flights"]}
    check("cross-midnight affects exactly KX-099", set(affected) == {"KX-099-20260908"},
          body["affected_flights"])
    check("KX-099 cancelled (delay exceeds allowance)",
          affected.get("KX-099-20260908", {}).get("impact_status") == "cancelled")
    check("cross-midnight summary", body["impact_summary"] == {
        "cancelled": 1, "delayed": 0, "pending": 0,
        "affected_flights": 1, "affected_passengers": 74}, body["impact_summary"])

    # ---- idempotency
    replay = event("e2e-aps-close-01", effective_from="2026-09-07T18:00:00Z",
                   effective_until="2026-09-08T00:30:00Z", reason="volcanic ash")
    status, body = client.post_event(replay)
    check("identical replay returns HTTP 200", status == 200, body)
    check("replay flagged as idempotent", body.get("idempotent_replay") is True)
    check("replay returns original result",
          body["impact_summary"]["cancelled"] == 1
          and body["closure_id"] == "e2e-aps-close-01", body)
    status, body = client.post_event(event(
        "e2e-aps-close-01", effective_from="2026-09-07T18:00:00Z",
        effective_until="2026-09-08T01:00:00Z"))
    check("same id with different payload conflicts",
          status == 409 and body.get("error", {}).get("code") == "event_conflict", body)

    # ---- state machine rejections
    expect_error(client, "second active closure",
                 event("e2e-aps-close-02", effective_from="2026-09-07T20:00:00Z"),
                 409, "state_conflict")
    expect_error(client, "extension with unknown supersedes",
                 event("e2e-aps-ext-bad1", event_type="airport.extended", version=2,
                       effective_until="2026-09-08T02:00:00Z",
                       supersedes="e2e-no-such-event"), 409, "state_conflict")
    expect_error(client, "extension without version bump",
                 event("e2e-aps-ext-bad2", event_type="airport.extended", version=1,
                       effective_until="2026-09-08T02:00:00Z",
                       supersedes="e2e-aps-close-01"), 409, "state_conflict")
    expect_error(client, "extension that shortens the window",
                 event("e2e-aps-ext-bad3", event_type="airport.extended", version=2,
                       effective_until="2026-09-08T00:00:00Z",
                       supersedes="e2e-aps-close-01"), 422, "invalid_time_window")

    # ---- delayed -> extended -> reopened lifecycle on BSR (buffer 15 min)
    status, body = client.post_event(event(
        "e2e-bsr-close-01", airport="BSR",
        effective_from="2026-09-07T15:00:00Z", effective_until="2026-09-07T16:00:00Z"))
    check("BSR closure accepted", status == 201, body)
    check("BY-205 delayed 70 min (window end + buffer)",
          body["impact_summary"]["delayed"] == 1
          and body["affected_flights"][0]["flight_id"] == "BY-205-20260908"
          and body["affected_flights"][0]["delay_minutes"] == 70, body)

    status, body = client.post_event(event(
        "e2e-bsr-extend-01", event_type="airport.extended", airport="BSR", version=2,
        effective_until="2026-09-07T18:00:00Z", supersedes="e2e-bsr-close-01"))
    check("BSR extension accepted", status == 201, body)
    by_flight = {f["flight_id"]: f for f in body["affected_flights"]}
    check("extension recomputes impacts",
          body["impact_summary"]["cancelled"] == 1 and body["impact_summary"]["delayed"] == 1
          and by_flight.get("BY-205-20260908", {}).get("impact_status") == "cancelled"
          and by_flight.get("AX-410-20260907", {}).get("delay_minutes") == 65, body)

    status, body = client.post_event(event(
        "e2e-bsr-reopen-01", event_type="airport.reopened", airport="BSR", version=3,
        effective_from="2026-09-07T15:30:00Z", supersedes="e2e-bsr-extend-01"))
    check("BSR reopening accepted", status == 201, body)
    check("reopening shortens window and recomputes",
          body["closure_status"] == "reopened"
          and body["impact_summary"]["delayed"] == 1
          and body["affected_flights"][0]["delay_minutes"] == 40, body)

    # ---- indefinite closure on KTA -> pending
    status, body = client.post_event(event(
        "e2e-kta-close-01", airport="KTA",
        effective_from="2026-09-07T16:00:00Z", effective_until=None))
    check("indefinite closure accepted", status == 201, body)
    check("indefinite closure has no window end", body["window"]["end_utc"] is None)
    check("indefinite closure marks flights pending",
          body["impact_summary"]["pending"] == 2
          and body["impact_summary"]["affected_passengers"] == 216, body["impact_summary"])

    # ---- read endpoints
    status, body = client.get("/v1/events/e2e-bsr-extend-01")
    check("event status by id", status == 200 and body["status"] == "processed", body)
    check("event status shows current closure state",
          body["closure_status"] == "reopened"
          and body["impact_summary"]["delayed"] == 1, body)
    status, body = client.get("/v1/events/e2e-no-such-event")
    check("unknown event returns 404",
          status == 404 and body.get("error", {}).get("code") == "event_not_found", body)

    status, body = client.get("/v1/airports/APS/impact-summary")
    check("APS summary", status == 200
          and body["totals"]["cancelled"] == 1
          and body["active_closure"]["closure_id"] == "e2e-aps-close-01", body)
    status, body = client.get("/v1/airports/KTA/impact-summary")
    check("KTA summary pending", status == 200 and body["totals"]["pending"] == 2, body)
    status, body = client.get("/v1/airports/ZZZ/impact-summary")
    check("unknown airport summary returns 404",
          status == 404 and body.get("error", {}).get("code") == "airport_not_found", body)

    status, page1 = client.get("/v1/impacts?airport_code=KTA&page=1&page_size=1")
    check("pagination page 1", status == 200 and page1["total"] == 2
          and len(page1["items"]) == 1, page1)
    status, page2 = client.get("/v1/impacts?airport_code=KTA&page=2&page_size=1")
    check("pagination page 2", status == 200 and len(page2["items"]) == 1
          and page2["items"][0]["flight_id"] != page1["items"][0]["flight_id"], page2)
    status, body = client.get("/v1/impacts?impact_status=delayed")
    check("filter by status", status == 200 and body["total"] == 1
          and body["items"][0]["flight_id"] == "BY-205-20260908", body)
    status, body = client.get("/v1/impacts")
    check("total impacts across airports", status == 200 and body["total"] == 4, body)
    for path in ("page=0", "page_size=101", "impact_status=bogus", "airport_code=aps"):
        status, body = client.get(f"/v1/impacts?{path}")
        check(f"invalid query rejected ({path})",
              status == 400 and body.get("error", {}).get("code") == "invalid_query", body)


def phase_after_restart(client):
    status, body = client.get("/healthz")
    check("health ok after restart", status == 200 and body.get("status") == "ok", body)

    status, body = client.get("/v1/events/e2e-aps-close-01")
    check("closure event persisted", status == 200 and body["status"] == "processed", body)
    check("computed impacts persisted",
          body["impact_summary"]["cancelled"] == 1
          and body["affected_flights"][0]["flight_id"] == "KX-099-20260908", body)

    status, body = client.get("/v1/events/e2e-bsr-reopen-01")
    check("reopened closure state persisted",
          status == 200 and body["closure_status"] == "reopened", body)

    status, body = client.get("/v1/airports/KTA/impact-summary")
    check("airport summary persisted", status == 200 and body["totals"]["pending"] == 2, body)

    status, body = client.get("/v1/impacts")
    check("impact rows persisted", status == 200 and body["total"] == 4, body)

    # idempotency survives the restart as well
    status, body = client.post_event(event(
        "e2e-aps-close-01", effective_from="2026-09-07T18:00:00Z",
        effective_until="2026-09-08T00:30:00Z", reason="volcanic ash"))
    check("idempotent replay after restart",
          status == 200 and body.get("idempotent_replay") is True, body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--phase", choices=["before-restart", "after-restart"], required=True)
    args = parser.parse_args()

    client = Client(args.base_url)
    if args.phase == "before-restart":
        phase_before_restart(client)
    else:
        phase_after_restart(client)
    print(f"PHASE {args.phase}: all {CHECKS} checks passed", flush=True)


if __name__ == "__main__":
    main()
