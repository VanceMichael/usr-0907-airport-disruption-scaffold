"""Shared fixtures: an in-process service and a real HTTP client."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app.fixtures import load_airports, load_flights
from app.http_api import create_server
from app.service import DisruptionService
from app.storage import Storage

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def make_service(db_path) -> DisruptionService:
    airports = load_airports(FIXTURES_DIR)
    flights = load_flights(FIXTURES_DIR, airports)
    return DisruptionService(Storage(db_path), airports, flights)


@pytest.fixture()
def service(tmp_path):
    svc = make_service(tmp_path / "test.db")
    yield svc
    svc._storage.close()


class ApiClient:
    def __init__(self, host: str, port: int) -> None:
        self.base = f"http://{host}:{port}"

    def request(self, method, path, payload=None, raw_body=None, headers=None):
        data = None
        final_headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            final_headers["Content-Type"] = "application/json"
        if raw_body is not None:
            data = raw_body
        if headers:
            final_headers.update(headers)
        req = urllib.request.Request(
            self.base + path, data=data, headers=final_headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            return exc.code, json.loads(body) if body else None

    def get(self, path):
        return self.request("GET", path)

    def post_event(self, payload, **kwargs):
        return self.request("POST", "/events", payload=payload, **kwargs)


@pytest.fixture()
def client(service):
    server = create_server(("127.0.0.1", 0), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield ApiClient(host, port)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def closed_event(event_id, airport, version=1, **overrides):
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
