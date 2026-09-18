"""SQLite persistence for events and their computed impacts.

Writes for one event (the event row, its impacts, and the supersession of
the previous chain head) happen in a single transaction, so a failed event
never leaves partial state behind. All rows survive restarts: the database
file lives on a mounted volume in the container deployment.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id            TEXT PRIMARY KEY,
    event_version       INTEGER NOT NULL,
    event_type          TEXT NOT NULL,
    airport_code        TEXT NOT NULL,
    effective_from      TEXT NOT NULL,
    effective_until     TEXT,
    reported_at         TEXT NOT NULL,
    supersedes_event_id TEXT,
    reason              TEXT,
    request_canonical   TEXT NOT NULL,
    response_body       TEXT NOT NULL,
    status              TEXT NOT NULL,
    superseded_by       TEXT,
    received_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS impacts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id            TEXT NOT NULL REFERENCES events(event_id),
    flight_id           TEXT NOT NULL,
    flight_number       TEXT NOT NULL,
    origin              TEXT NOT NULL,
    destination         TEXT NOT NULL,
    airport_code        TEXT NOT NULL,
    affected_point      TEXT NOT NULL,
    impact_status       TEXT NOT NULL,
    delay_minutes       INTEGER,
    passenger_count     INTEGER NOT NULL,
    scheduled_departure TEXT NOT NULL,
    scheduled_arrival   TEXT NOT NULL,
    closure_from        TEXT NOT NULL,
    closure_until       TEXT,
    usable_at           TEXT,
    superseded          INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    UNIQUE (event_id, flight_id)
);

CREATE INDEX IF NOT EXISTS idx_events_airport ON events(airport_code);
CREATE INDEX IF NOT EXISTS idx_impacts_airport
    ON impacts(airport_code, superseded, impact_status);
"""

_EVENT_COLUMNS = (
    "event_id",
    "event_version",
    "event_type",
    "airport_code",
    "effective_from",
    "effective_until",
    "reported_at",
    "supersedes_event_id",
    "reason",
    "request_canonical",
    "response_body",
    "status",
    "superseded_by",
    "received_at",
)

_IMPACT_COLUMNS = (
    "event_id",
    "flight_id",
    "flight_number",
    "origin",
    "destination",
    "airport_code",
    "affected_point",
    "impact_status",
    "delay_minutes",
    "passenger_count",
    "scheduled_departure",
    "scheduled_arrival",
    "closure_from",
    "closure_until",
    "usable_at",
    "superseded",
    "created_at",
)


class EventStoreConflict(Exception):
    """Raised when a uniqueness constraint fires on insert (retry race)."""


class Storage:
    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(
                "PRAGMA journal_mode=WAL;"
                "PRAGMA foreign_keys=ON;"
                "PRAGMA busy_timeout=5000;"
            )
            self._conn.executescript(SCHEMA)

    @property
    def path(self) -> str:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def health_check(self) -> bool:
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_open_closure_head(self, airport_code: str) -> dict[str, Any] | None:
        """The current head of the airport's open closure chain, if any.

        A chain stays "open" until an ``airport.reopened`` event closes it;
        extension events move the head forward by superseding it.
        """
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM events
                WHERE airport_code = ?
                  AND superseded_by IS NULL
                  AND event_type IN ('airport.closed', 'airport.extended')
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (airport_code,),
            ).fetchone()
        return dict(row) if row else None

    def count_events_for_airport(self, airport_code: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE airport_code = ?",
                (airport_code,),
            ).fetchone()
        return int(row["n"])

    def record_event(
        self,
        event_row: dict[str, Any],
        impact_rows: list[dict[str, Any]],
        supersede_event_id: str | None,
    ) -> None:
        """Persist one event and its impacts atomically.

        When ``supersede_event_id`` is given, that event and its impacts are
        marked superseded in the same transaction.
        """
        event_values = tuple(event_row[col] for col in _EVENT_COLUMNS)
        event_sql = (
            f"INSERT INTO events ({', '.join(_EVENT_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _EVENT_COLUMNS)})"
        )
        impact_sql = (
            f"INSERT INTO impacts ({', '.join(_IMPACT_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _IMPACT_COLUMNS)})"
        )
        impact_values = [tuple(row[col] for col in _IMPACT_COLUMNS) for row in impact_rows]
        with self._lock:
            try:
                with self._conn:  # commits on success, rolls back on error
                    if supersede_event_id is not None:
                        self._conn.execute(
                            "UPDATE events SET superseded_by = ? WHERE event_id = ?",
                            (event_row["event_id"], supersede_event_id),
                        )
                        self._conn.execute(
                            "UPDATE impacts SET superseded = 1 WHERE event_id = ?",
                            (supersede_event_id,),
                        )
                    self._conn.execute(event_sql, event_values)
                    if impact_values:
                        self._conn.executemany(impact_sql, impact_values)
            except sqlite3.IntegrityError as exc:
                raise EventStoreConflict(str(exc)) from exc

    # ------------------------------------------------------------------
    # impacts
    # ------------------------------------------------------------------
    def impacts_for_event(self, event_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM impacts
                WHERE event_id = ?
                ORDER BY scheduled_departure, flight_id
                """,
                (event_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_impacts_for_event(self, event_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM impacts WHERE event_id = ?", (event_id,)
            ).fetchone()
        return int(row["n"])

    def query_impacts(
        self,
        airport_code: str | None = None,
        impact_status: str | None = None,
        include_superseded: bool = False,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if airport_code is not None:
            clauses.append("airport_code = ?")
            params.append(airport_code)
        if impact_status is not None:
            clauses.append("impact_status = ?")
            params.append(impact_status)
        if not include_superseded:
            clauses.append("superseded = 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            total = int(
                self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM impacts {where}", params
                ).fetchone()["n"]
            )
            rows = self._conn.execute(
                f"""
                SELECT * FROM impacts {where}
                ORDER BY scheduled_departure, flight_id, id
                LIMIT ? OFFSET ?
                """,
                (*params, page_size, (page - 1) * page_size),
            ).fetchall()
        return [dict(row) for row in rows], total

    def current_impacts_for_airport(self, airport_code: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM impacts
                WHERE airport_code = ? AND superseded = 0
                ORDER BY scheduled_departure, flight_id
                """,
                (airport_code,),
            ).fetchall()
        return [dict(row) for row in rows]
