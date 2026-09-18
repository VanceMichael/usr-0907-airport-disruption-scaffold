"""End-to-end API tests against an in-process server with a temporary database."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app.events import EventService
from app.fixtures import load_fixtures
from app.server import make_server
from app.storage import Storage
from app.validation import load_contract

REPO_ROOT = Path(__file__).resolve().parent.parent


def make_event(event_id, event_type="airport.closed", airport="APS", version=1,
               effective_from="2026-09-07T15:00:00Z", effective_until=None,
               supersedes=None, reason=None):
    payload = {
        "event_id": event_id,
        "event_version": version,
        "event_type": event_type,
        "airport_code": airport,
        "effective_from": effective_from,
        "reported_at": "2026-09-07T14:00:00Z",
    }
    if effective_until is not None:
        payload["effective_until"] = effective_until
    if supersedes is not None:
        payload["supersedes_event_id"] = supersedes
    if reason is not None:
        payload["reason"] = reason
    return payload


class ApiTestCase(unittest.TestCase):
    db_path = None  # set per-test so restart tests can reopen it

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test.db")
        self._start_server()

    def tearDown(self):
        self._stop_server()
        self._tmp.cleanup()

    def _start_server(self):
        storage = Storage(self.db_path)
        storage.init_schema()
        airports, flights = load_fixtures(REPO_ROOT / "fixtures")
        schema = load_contract(REPO_ROOT / "contracts" / "disruption-event.schema.json")
        service = EventService(storage, schema, airports, flights)
        self._server = make_server("127.0.0.1", 0, service)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def _stop_server(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # ------------------------------------------------------------- helpers

    def request(self, method, path, payload=None):
        url = self.base_url + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def post(self, payload):
        return self.request("POST", "/v1/events", payload)

    def get(self, path):
        return self.request("GET", path)

    def assert_error(self, status, body, expected_status, expected_code):
        self.assertEqual(status, expected_status, body)
        self.assertIn("error", body)
        self.assertEqual(body["error"]["code"], expected_code, body)
        self.assertIsInstance(body["error"]["message"], str)
        self.assertIsInstance(body["error"]["details"], list)


class HealthAndRoutingTest(ApiTestCase):
    def test_healthz(self):
        status, body = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["database"], "ok")

    def test_root_lists_endpoints(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("endpoints", body)

    def test_unknown_route(self):
        status, body = self.get("/v1/nope")
        self.assert_error(status, body, 404, "not_found")

    def test_method_not_allowed(self):
        status, body = self.request("PUT", "/v1/events", {})
        self.assert_error(status, body, 405, "method_not_allowed")
        status, body = self.request("POST", "/v1/impacts", {})
        self.assert_error(status, body, 405, "method_not_allowed")

    def test_invalid_json_body(self):
        req = urllib.request.Request(
            self.base_url + "/v1/events", data=b"{not json", method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                status, body = resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            status, body = exc.code, json.loads(exc.read())
        self.assert_error(status, body, 400, "invalid_json")


class SubmissionTest(ApiTestCase):
    def test_closed_event_computes_impacts(self):
        status, body = self.post(make_event(
            "t-close-0001", effective_from="2026-09-07T15:00:00Z",
            effective_until="2026-09-07T16:00:00Z"))
        self.assertEqual(status, 201, body)
        self.assertFalse(body["idempotent_replay"])
        self.assertEqual(body["status"], "processed")
        self.assertEqual(body["closure_status"], "active")
        # APS window 15:00-16:00Z + 20min buffer -> AX-410 (dep 15:30Z) delayed 50
        self.assertEqual(body["impact_summary"]["delayed"], 1)
        flight = body["affected_flights"][0]
        self.assertEqual(flight["flight_id"], "AX-410-20260907")
        self.assertEqual(flight["delay_minutes"], 50)

    def test_cross_midnight_window(self):
        status, body = self.post(make_event(
            "t-close-xmid", effective_from="2026-09-07T18:00:00Z",
            effective_until="2026-09-08T00:30:00Z"))
        self.assertEqual(status, 201, body)
        self.assertEqual(body["window"]["end_utc"], "2026-09-08T00:30:00Z")
        # Only KX-099 (arrival 19:15Z) falls inside; needed 335min > 45 -> cancelled
        self.assertEqual([f["flight_id"] for f in body["affected_flights"]],
                         ["KX-099-20260908"])
        self.assertEqual(body["impact_summary"]["cancelled"], 1)

    def test_indefinite_closure_marks_pending(self):
        status, body = self.post(make_event(
            "t-close-indef", airport="KTA", effective_from="2026-09-07T16:00:00Z"))
        self.assertEqual(status, 201, body)
        self.assertIsNone(body["window"]["end_utc"])
        self.assertEqual(body["impact_summary"]["pending"], 2)

    def test_idempotent_replay_returns_original_result(self):
        payload = make_event("t-replay-001", effective_until="2026-09-07T16:00:00Z")
        status1, body1 = self.post(payload)
        status2, body2 = self.post(payload)
        self.assertEqual(status1, 201)
        self.assertEqual(status2, 200)
        self.assertTrue(body2["idempotent_replay"])
        body2_clean = {**body2, "idempotent_replay": False}
        self.assertEqual(body1, body2_clean)
        # the replay must not duplicate impacts
        _, impacts = self.get("/v1/impacts")
        self.assertEqual(impacts["total"], body1["impact_summary"]["affected_flights"])

    def test_same_id_different_payload_conflicts(self):
        self.post(make_event("t-conflict-1", effective_until="2026-09-07T16:00:00Z"))
        status, body = self.post(make_event("t-conflict-1", effective_until="2026-09-07T17:00:00Z"))
        self.assert_error(status, body, 409, "event_conflict")

    def test_equivalent_offset_timestamp_is_a_replay(self):
        payload = make_event("t-offset-001", effective_until="2026-09-07T16:00:00Z")
        status1, _ = self.post(payload)
        replay = dict(payload, effective_from="2026-09-07T23:00:00+08:00",
                      effective_until="2026-09-08T00:00:00+08:00")
        status2, body2 = self.post(replay)
        self.assertEqual(status1, 201)
        self.assertEqual(status2, 200, body2)
        self.assertTrue(body2["idempotent_replay"])


class ValidationFailureTest(ApiTestCase):
    def test_invalid_payloads_write_nothing(self):
        bad_payloads = [
            make_event("t-bad-000001", airport="aps"),          # pattern violation
            make_event("t-bad-000002", airport="ZZZ"),          # unknown airport
            make_event("t-bad-000003", effective_from="2026-09-07T15:00:00Z",
                       effective_until="2026-09-07T14:00:00Z"),  # inverted window
            {**make_event("t-bad-000004"), "event_version": 0},  # schema minimum
            {**make_event("t-bad-000005"), "extra": 1},          # additional property
        ]
        for payload in bad_payloads:
            status, body = self.post(payload)
            self.assertIn(status, (400, 422), body)
            self.assertIn("code", body["error"])
            # no partial write: the event must not exist afterwards
            follow_status, follow_body = self.get(f"/v1/events/{payload['event_id']}")
            self.assert_error(follow_status, follow_body, 404, "event_not_found")
        # and no impacts were produced
        _, impacts = self.get("/v1/impacts")
        self.assertEqual(impacts["total"], 0)

    def test_error_codes_are_stable(self):
        cases = [
            (make_event("t-err-000001", airport="aps"), 400, "validation_error"),
            (make_event("t-err-000002", airport="ZZZ"), 422, "unknown_airport"),
            (make_event("t-err-000003", effective_until="2026-09-07T14:00:00Z"),
             422, "invalid_time_window"),
        ]
        for payload, expected_status, expected_code in cases:
            status, body = self.post(payload)
            self.assert_error(status, body, expected_status, expected_code)


class LifecycleTest(ApiTestCase):
    def test_extend_and_reopen_recompute_impacts(self):
        status, body = self.post(make_event(
            "t-life-close1", airport="BSR", effective_from="2026-09-07T15:00:00Z",
            effective_until="2026-09-07T16:00:00Z"))
        self.assertEqual(status, 201, body)
        self.assertEqual(body["impact_summary"]["delayed"], 1)  # BY-205, 70 min

        status, body = self.post(make_event(
            "t-life-extend1", event_type="airport.extended", airport="BSR", version=2,
            effective_until="2026-09-07T18:00:00Z", supersedes="t-life-close1"))
        self.assertEqual(status, 201, body)
        summary = body["impact_summary"]
        self.assertEqual((summary["cancelled"], summary["delayed"]), (1, 1))

        status, body = self.post(make_event(
            "t-life-reopen1", event_type="airport.reopened", airport="BSR", version=3,
            effective_from="2026-09-07T15:30:00Z", supersedes="t-life-extend1"))
        self.assertEqual(status, 201, body)
        self.assertEqual(body["closure_status"], "reopened")
        self.assertEqual(body["impact_summary"]["delayed"], 1)
        self.assertEqual(body["affected_flights"][0]["delay_minutes"], 40)

        # event status reflects the closure's current state
        status, body = self.get("/v1/events/t-life-close1")
        self.assertEqual(status, 200)
        self.assertEqual(body["closure_status"], "reopened")
        self.assertEqual(body["impact_summary"]["delayed"], 1)

    def test_second_closure_while_active_conflicts(self):
        self.post(make_event("t-dup-close01", effective_until="2026-09-07T16:00:00Z"))
        status, body = self.post(make_event("t-dup-close02", effective_from="2026-09-07T17:00:00Z"))
        self.assert_error(status, body, 409, "state_conflict")

    def test_extend_requires_active_closure(self):
        status, body = self.post(make_event(
            "t-ext-noact01", event_type="airport.extended", airport="KTA", version=2,
            effective_until="2026-09-07T18:00:00Z", supersedes="t-ext-unknown1"))
        self.assert_error(status, body, 409, "state_conflict")

    def test_extend_requires_supersedes(self):
        payload = make_event("t-ext-nosup1", event_type="airport.extended",
                             effective_until="2026-09-07T18:00:00Z")
        status, body = self.post(payload)
        self.assert_error(status, body, 422, "invalid_event_sequence")

    def test_extend_must_move_end_forward(self):
        self.post(make_event("t-ext-close01", effective_until="2026-09-07T16:00:00Z"))
        status, body = self.post(make_event(
            "t-ext-shrink1", event_type="airport.extended", version=2,
            effective_until="2026-09-07T15:30:00Z", supersedes="t-ext-close01"))
        self.assert_error(status, body, 422, "invalid_time_window")

    def test_version_must_increase_along_chain(self):
        self.post(make_event("t-ver-close01", effective_until="2026-09-07T16:00:00Z"))
        status, body = self.post(make_event(
            "t-ver-extend1", event_type="airport.extended", version=1,
            effective_until="2026-09-07T18:00:00Z", supersedes="t-ver-close01"))
        self.assert_error(status, body, 409, "state_conflict")

    def test_supersedes_must_be_chain_head(self):
        self.post(make_event("t-head-close1", effective_until="2026-09-07T16:00:00Z"))
        self.post(make_event("t-head-extend1", event_type="airport.extended", version=2,
                             effective_until="2026-09-07T18:00:00Z",
                             supersedes="t-head-close1"))
        # superseding the original close instead of the current head fails
        status, body = self.post(make_event(
            "t-head-extend2", event_type="airport.extended", version=3,
            effective_until="2026-09-07T19:00:00Z", supersedes="t-head-close1"))
        self.assert_error(status, body, 409, "state_conflict")

    def test_reopen_time_must_be_inside_window(self):
        self.post(make_event("t-reop-close1", effective_until="2026-09-07T16:00:00Z"))
        status, body = self.post(make_event(
            "t-reop-early1", event_type="airport.reopened", version=2,
            effective_from="2026-09-07T14:00:00Z", supersedes="t-reop-close1"))
        self.assert_error(status, body, 422, "invalid_time_window")
        status, body = self.post(make_event(
            "t-reop-late01", event_type="airport.reopened", version=2,
            effective_from="2026-09-07T17:00:00Z", supersedes="t-reop-close1"))
        self.assert_error(status, body, 422, "invalid_time_window")

    def test_closed_with_supersedes_rejected(self):
        payload = make_event("t-seq-000001", supersedes="t-seq-other01")
        status, body = self.post(payload)
        self.assert_error(status, body, 422, "invalid_event_sequence")


class QueryTest(ApiTestCase):
    def setUp(self):
        super().setUp()
        # APS: indefinite closure -> 4 pending flights
        self.post(make_event("q-aps-close1", effective_from="2026-09-07T15:00:00Z"))
        # KTA: short closure -> nothing (no KTA touch in 12:00-13:00Z)
        self.post(make_event("q-kta-close1", airport="KTA",
                             effective_from="2026-09-07T12:00:00Z",
                             effective_until="2026-09-07T13:00:00Z"))

    def test_event_status_not_found(self):
        status, body = self.get("/v1/events/q-missing-01")
        self.assert_error(status, body, 404, "event_not_found")

    def test_airport_summary(self):
        status, body = self.get("/v1/airports/APS/impact-summary")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["airport_code"], "APS")
        self.assertEqual(body["timezone"], "Asia/Makassar")
        self.assertEqual(body["totals"]["pending"], 4)
        self.assertEqual(body["totals"]["affected_passengers"], 168 + 142 + 131 + 74)
        self.assertEqual(body["active_closure"]["closure_id"], "q-aps-close1")
        self.assertEqual(len(body["closures"]), 1)

    def test_airport_summary_unknown_airport(self):
        status, body = self.get("/v1/airports/ZZZ/impact-summary")
        self.assert_error(status, body, 404, "airport_not_found")

    def test_impacts_pagination(self):
        status, page1 = self.get("/v1/impacts?airport_code=APS&page=1&page_size=3")
        self.assertEqual(status, 200, page1)
        self.assertEqual(page1["total"], 4)
        self.assertEqual(len(page1["items"]), 3)
        _, page2 = self.get("/v1/impacts?airport_code=APS&page=2&page_size=3")
        self.assertEqual(len(page2["items"]), 1)
        ids = {item["flight_id"] for item in page1["items"] + page2["items"]}
        self.assertEqual(len(ids), 4)
        _, beyond = self.get("/v1/impacts?airport_code=APS&page=3&page_size=3")
        self.assertEqual(beyond["items"], [])

    def test_impacts_filter_by_status(self):
        _, body = self.get("/v1/impacts?impact_status=pending")
        self.assertEqual(body["total"], 4)
        _, body = self.get("/v1/impacts?impact_status=cancelled")
        self.assertEqual(body["total"], 0)

    def test_impacts_invalid_query(self):
        for path, code in (
            ("/v1/impacts?page=0", "invalid_query"),
            ("/v1/impacts?page=abc", "invalid_query"),
            ("/v1/impacts?page_size=101", "invalid_query"),
            ("/v1/impacts?impact_status=bogus", "invalid_query"),
            ("/v1/impacts?airport_code=aps", "invalid_query"),
            ("/v1/impacts?bogus_param=1", "invalid_query"),
        ):
            status, body = self.get(path)
            self.assert_error(status, body, 400, code)
        status, body = self.get("/v1/impacts?airport_code=ZZZ")
        self.assert_error(status, body, 404, "airport_not_found")


class PersistenceTest(ApiTestCase):
    def test_data_survives_restart(self):
        status, body = self.post(make_event(
            "t-persist-001", effective_from="2026-09-07T18:00:00Z",
            effective_until="2026-09-08T00:30:00Z"))
        self.assertEqual(status, 201, body)

        # simulate a container restart: new server instance, same database file
        self._stop_server()
        self._start_server()

        status, body = self.get("/v1/events/t-persist-001")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "processed")
        self.assertEqual(body["impact_summary"]["cancelled"], 1)

        # idempotency also survives the restart
        status, body = self.post(make_event(
            "t-persist-001", effective_from="2026-09-07T18:00:00Z",
            effective_until="2026-09-08T00:30:00Z"))
        self.assertEqual(status, 200, body)
        self.assertTrue(body["idempotent_replay"])

        _, summary = self.get("/v1/airports/APS/impact-summary")
        self.assertEqual(summary["totals"]["cancelled"], 1)


if __name__ == "__main__":
    unittest.main()
