"""HTTP layer: routing, request parsing, and the stable error envelope.

Implemented on the standard library only. Every response — success or
failure — is JSON; errors always use the envelope
``{"error": {"code", "message", "details"?}}``.
"""
from __future__ import annotations

import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .errors import ApiError
from .service import DisruptionService

logger = logging.getLogger("disruption.http")

MAX_BODY_BYTES = 1024 * 1024  # 1 MiB is far beyond a legitimate event.

RouteHandler = Callable[..., tuple[int, dict[str, Any]]]


def _index() -> tuple[int, dict[str, Any]]:
    return 200, {
        "service": "airport-disruption-service",
        "version": __version__,
        "endpoints": [
            "POST /events",
            "GET /events/{event_id}",
            "GET /airports/{airport_code}/impact-summary",
            "GET /impacts/flights",
            "GET /health",
        ],
    }


class DisruptionHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: DisruptionService) -> None:
        self.service = service
        super().__init__(address, RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = f"DisruptionService/{__version__}"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    @property
    def service(self) -> DisruptionService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _routes(self):
        service = self.service
        return [
            ("GET", re.compile(r"/"), lambda **_: _index()),
            ("GET", re.compile(r"/health"), lambda **_: service.health()),
            ("POST", re.compile(r"/events"), self._post_event),
            (
                "GET",
                re.compile(r"/events/(?P<event_id>[^/]+)"),
                lambda event_id, **_: (200, service.get_event(event_id)),
            ),
            (
                "GET",
                re.compile(r"/airports/(?P<airport_code>[^/]+)/impact-summary"),
                lambda airport_code, **_: (200, service.airport_summary(airport_code)),
            ),
            (
                "GET",
                re.compile(r"/impacts/flights"),
                lambda query, **_: (200, service.list_flight_impacts(query)),
            ),
        ]

    def _send_json(
        self,
        status: int,
        body: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _send_error(
        self, error: ApiError, extra_headers: dict[str, str] | None = None
    ) -> None:
        self._send_json(error.status, error.to_body(), extra_headers)

    def _dispatch(self, body_reader: Callable[[], bytes] | None = None) -> None:
        split = urlsplit(self.path)
        path = split.path
        query = {key: values[0] for key, values in parse_qs(split.query).items()}
        allowed_methods: list[str] = []
        try:
            for method, pattern, handler in self._routes():
                match = pattern.fullmatch(path)
                if not match:
                    continue
                if method != self.command:
                    allowed_methods.append(method)
                    continue
                kwargs: dict[str, Any] = dict(match.groupdict())
                kwargs["query"] = query
                if body_reader is not None:
                    kwargs["body"] = body_reader()
                status, response = handler(**kwargs)
                self._send_json(status, response)
                return
            if allowed_methods:
                allow = ", ".join(sorted(allowed_methods))
                self._send_error(
                    ApiError(
                        405,
                        "method_not_allowed",
                        f"{self.command} is not allowed on {path!r}; use {allow}.",
                    ),
                    {"Allow": allow},
                )
                return
            raise ApiError(404, "not_found", f"no route matches {path!r}.")
        except ApiError as error:
            self._send_error(error)
        except Exception:  # pragma: no cover - defensive
            logger.exception("unhandled error while processing %s", self.path)
            self._send_error(
                ApiError(500, "internal_error", "an unexpected error occurred")
            )

    # ------------------------------------------------------------------
    # method entry points
    # ------------------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        # Read (and bound) the body before routing so the connection stays
        # usable even when the path turns out to be unknown.
        body = self._read_body()
        if body is None:
            return  # an error response was already sent
        self._dispatch(body_reader=lambda: body)

    def do_PUT(self) -> None:
        self._unsupported_method()

    def do_PATCH(self) -> None:
        self._unsupported_method()

    def do_DELETE(self) -> None:
        self._unsupported_method()

    def _unsupported_method(self) -> None:
        # Drain any request body so the connection stays consistent, then
        # let the router produce 404/405 with the usual error envelope.
        length = self.headers.get("Content-Length")
        if length is not None:
            try:
                remaining = min(int(length), MAX_BODY_BYTES)
            except ValueError:
                remaining = 0
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        self._dispatch()

    def _read_body(self) -> bytes | None:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self._send_error(
                ApiError(411, "length_required", "Content-Length header is required.")
            )
            return None
        try:
            length = int(length_header)
        except ValueError:
            self._send_error(
                ApiError(400, "malformed_request", "Content-Length is not a number.")
            )
            return None
        if length > MAX_BODY_BYTES:
            self._send_error(
                ApiError(413, "payload_too_large", "request body exceeds 1 MiB.")
            )
            return None
        return self.rfile.read(length)

    # ------------------------------------------------------------------
    # POST /events
    # ------------------------------------------------------------------
    def _post_event(self, body: bytes, **_: Any) -> tuple[int, dict[str, Any]]:
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type.lower():
            raise ApiError(
                415,
                "unsupported_media_type",
                "Content-Type must be application/json.",
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(
                400, "malformed_json", f"request body is not valid JSON: {exc}"
            ) from exc
        return self.service.submit_event(payload)


def create_server(
    address: tuple[str, int], service: DisruptionService
) -> DisruptionHTTPServer:
    return DisruptionHTTPServer(address, service)
