"""ISO 8601 parsing and normalisation.

All instants are normalised to UTC as soon as they enter the system so that
interval arithmetic — including closure windows that cross local or UTC
midnight — is plain, unambiguous ``datetime`` comparison. Consumers may
submit any valid offset form (``Z``, ``+07:00``, ...); equivalent instants
compare equal after normalisation.
"""
from __future__ import annotations

from datetime import datetime, timezone


def parse_instant(value: object, field: str = "timestamp") -> datetime:
    """Parse an ISO 8601 timestamp into an aware UTC ``datetime``.

    Raises ``ValueError`` for non-strings, unparseable values, and naive
    timestamps (a timezone offset is mandatory).
    """
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO 8601 string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field} is not a valid ISO 8601 timestamp: {value!r}") from None
    if parsed.tzinfo is None:
        raise ValueError(
            f"{field} must include a timezone offset (e.g. 'Z' or '+07:00')"
        )
    return parsed.astimezone(timezone.utc)


def format_instant(moment: datetime) -> str:
    """Render an aware datetime in the canonical UTC form used for storage."""
    utc = moment.astimezone(timezone.utc)
    timespec = "microseconds" if utc.microsecond else "seconds"
    return utc.isoformat(timespec=timespec).replace("+00:00", "Z")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
