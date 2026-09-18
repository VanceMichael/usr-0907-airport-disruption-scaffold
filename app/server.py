"""HTTP transport: routing, JSON envelopes and error mapping.

Implemented on the standard library only — the service has no third-party
dependencies, which keeps the runtime image hermetic and reproducible.
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .errors import ApiError

MAX_BODY_BYTES = 1 << 20  # 1 MiB is far beyond any legitimate event payload

_EVENT_PATH = re.compile(r"^/v1/events/([A-Za-z0-9-]+)$")
_AIRPORT_SUMMARY_PATH = re.compile(r"^/v1/airports/([A-Za-z]{3})/impact-summary$")

SERVICE_INFO = {
    "service": "airport-disruption-api",
    "version": __version__,
    "endpoints": [
        "POST /v1/events",
        "GET /v1/events/{event_id}",
        "GET /v1/airports/{code}/impact-summary",
        "GET /v1/impacts?airport_code=&impact_status=&page=&page_size=",
        "GET /healthz",
    ],
}


def make_server(host: str, port: int, service) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"DisruptionAPI/{__version__}"
        protocol_version = "HTTP/1.1"

        # ---------------------------------------------------------- plumbing

        def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
            print(f"{self.address_string()} - {fmt % args}", flush=True)

        def _send_json(self, status: int, body: dict, extra_headers=None) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _send_error(self, err: ApiError) -> None:
            headers = {"Allow": "GET, POST"} if err.status == 405 else None
            self._send_json(err.status, err.to_body(), headers)

        def _read_json_body(self):
            length = self.headers.get("Content-Length")
            if length is None:
                raise ApiError(400, "invalid_json", "request body must be a JSON object")
            try:
                size = int(length)
            except ValueError:
                raise ApiError(400, "invalid_json", "invalid Content-Length header") from None
            if size > MAX_BODY_BYTES:
                raise ApiError(413, "payload_too_large", "request body exceeds 1 MiB")
            raw = self.rfile.read(size)
            try:
                return json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError(400, "invalid_json", f"request body is not valid JSON: {exc}") from None

        # ------------------------------------------------------------ verbs

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

        # ---------------------------------------------------------- routing

        def _dispatch(self, method: str) -> None:
            try:
                self._route(method)
            except ApiError as err:
                self._send_error(err)
            except Exception:  # pragma: no cover - last-resort guard
                import traceback
                traceback.print_exc()
                self._send_error(ApiError(500, "internal_error", "unexpected server error"))

        def _route(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path if parsed.path == "/" else parsed.path.rstrip("/")

            if path == "/":
                self._require(method, "GET")
                self._send_json(200, SERVICE_INFO)
                return
            if path == "/healthz":
                self._require(method, "GET")
                health = service.health()
                self._send_json(200 if health["database"] == "ok" else 503, health)
                return
            if path == "/v1/events":
                self._require(method, "POST")
                status, body = service.submit(self._read_json_body())
                self._send_json(status, body)
                return
            match = _EVENT_PATH.match(path)
            if match:
                self._require(method, "GET")
                self._send_json(200, service.event_status(match.group(1)))
                return
            match = _AIRPORT_SUMMARY_PATH.match(path)
            if match:
                self._require(method, "GET")
                self._send_json(200, service.airport_summary(match.group(1).upper()))
                return
            if path == "/v1/impacts":
                self._require(method, "GET")
                params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
                self._send_json(200, service.list_impacts(params))
                return
            raise ApiError(404, "not_found", f"no route for {method} {path}")

        @staticmethod
        def _require(method: str, expected: str) -> None:
            if method != expected:
                raise ApiError(405, "method_not_allowed", f"use {expected} for this resource")

    return ThreadingHTTPServer((host, port), Handler)
