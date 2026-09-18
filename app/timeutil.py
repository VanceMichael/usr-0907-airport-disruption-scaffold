"""Strict RFC 3339 timestamp handling.

Every instant the service works with is normalized to UTC on ingestion.
Because all interval math runs on absolute UTC datetimes, closure windows
that cross midnight (in UTC or in an airport's local zone) need no special
casing anywhere else in the codebase.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# YYYY-MM-DDTHH:MM:SS[.ffffff] with a mandatory numeric offset or "Z".
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$"
)


def parse_timestamp(value) -> datetime:
    """Parse an offset-qualified ISO 8601 timestamp into an aware UTC datetime.

    Raises :class:`ValueError` for anything that is not a well-formed
    RFC 3339 ``date-time`` (naive timestamps are rejected on purpose).
    """
    if not isinstance(value, str) or not _RFC3339_RE.match(value):
        raise ValueError(
            "expected an RFC 3339 date-time with offset, e.g. 2026-09-07T15:30:00Z"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid calendar value: {exc}") from exc
    if parsed.tzinfo is None:  # defensive; the regex already requires an offset
        raise ValueError("timestamp must carry a UTC offset")
    return parsed.astimezone(timezone.utc)


def format_utc(dt: datetime) -> str:
    """Render an aware datetime as canonical UTC (``...Z``)."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def format_local(dt: datetime, tz_name: str) -> str:
    """Render an aware datetime in an airport's local zone (informational)."""
    return dt.astimezone(ZoneInfo(tz_name)).isoformat()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
