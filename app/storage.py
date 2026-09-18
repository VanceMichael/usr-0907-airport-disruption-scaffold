"""SQLite persistence for closures, events and computed impacts.

All writes for a single event submission happen inside one IMMEDIATE
transaction, so a failed request never leaves partial state behind.  The
database lives on a mounted volume in the container, which is what makes
computed results survive restarts.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS closures (
  closure_id    TEXT PRIMARY KEY,
  airport_code  TEXT NOT NULL,
  start_utc     TEXT NOT NULL,
  end_utc       TEXT,               -- current raw window end; NULL = indefinite
  status        TEXT NOT NULL CHECK (status IN ('active', 'reopened')),
  head_event_id TEXT NOT NULL,      -- latest event in the chain
  head_version  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_closures_airport ON closures(airport_code);

CREATE TABLE IF NOT EXISTS events (
  event_id            TEXT PRIMARY KEY,
  event_version       INTEGER NOT NULL,
  event_type          TEXT NOT NULL,
  airport_code        TEXT NOT NULL,
  effective_from_utc  TEXT NOT NULL,
  effective_until_utc TEXT,
  reported_at_utc     TEXT NOT NULL,
  supersedes_event_id TEXT,
  reason              TEXT,
  closure_id          TEXT NOT NULL REFERENCES closures(closure_id),
  payload_hash        TEXT NOT NULL,
  payload_json        TEXT NOT NULL,   -- canonical normalized payload
  result_json         TEXT NOT NULL,   -- response returned for this event
  received_at_utc     TEXT NOT NULL,
  processed_at_utc    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_closure ON events(closure_id);

CREATE TABLE IF NOT EXISTS impacts (
  closure_id           TEXT NOT NULL REFERENCES closures(closure_id),
  airport_code         TEXT NOT NULL,
  flight_id            TEXT NOT NULL,
  impact_status        TEXT NOT NULL CHECK (impact_status IN ('cancelled', 'delayed', 'pending')),
  delay_minutes        INTEGER,
  touch_kind           TEXT NOT NULL CHECK (touch_kind IN ('departure', 'arrival')),
  touch_time_utc       TEXT NOT NULL,
  window_start_utc     TEXT NOT NULL,
  window_end_utc       TEXT,          -- raw window end at computation time; NULL = indefinite
  computed_by_event_id TEXT NOT NULL,
  PRIMARY KEY (closure_id, flight_id)
);
CREATE INDEX IF NOT EXISTS idx_impacts_airport ON impacts(airport_code);
"""


class Storage:
    def __init__(self, path):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def init_schema(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        """One atomic read-modify-write unit; rolls back on any exception."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ reads

    def get_event(self, event_id: str, conn: sqlite3.Connection | None = None):
        return self._one(
            conn, "SELECT * FROM events WHERE event_id = ?", (event_id,))

    def get_closure(self, closure_id: str, conn: sqlite3.Connection | None = None):
        return self._one(
            conn, "SELECT * FROM closures WHERE closure_id = ?", (closure_id,))

    def get_active_closure(self, airport_code: str, conn: sqlite3.Connection | None = None):
        return self._one(
            conn,
            "SELECT * FROM closures WHERE airport_code = ? AND status = 'active'",
            (airport_code,),
        )

    def closures_for_airport(self, airport_code: str):
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM closures WHERE airport_code = ? ORDER BY start_utc, closure_id",
                (airport_code,),
            ).fetchall()
        return [dict(row) for row in rows]

    def impacts_for_closure(self, closure_id: str):
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM impacts WHERE closure_id = ? ORDER BY touch_time_utc, flight_id",
                (closure_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def query_impacts(self, airport_code: str | None, impact_status: str | None,
                      page: int, page_size: int):
        where, params = [], []
        if airport_code is not None:
            where.append("airport_code = ?")
            params.append(airport_code)
        if impact_status is not None:
            where.append("impact_status = ?")
            params.append(impact_status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with closing(self.connect()) as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM impacts{clause}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM impacts{clause}"
                " ORDER BY touch_time_utc, flight_id LIMIT ? OFFSET ?",
                (*params, page_size, (page - 1) * page_size),
            ).fetchall()
        return total, [dict(row) for row in rows]

    def health_check(self) -> bool:
        try:
            with closing(self.connect()) as conn:
                conn.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False

    # ----------------------------------------------------------------- writes

    @staticmethod
    def insert_closure(conn: sqlite3.Connection, closure: dict) -> None:
        conn.execute(
            "INSERT INTO closures (closure_id, airport_code, start_utc, end_utc,"
            " status, head_event_id, head_version)"
            " VALUES (:closure_id, :airport_code, :start_utc, :end_utc,"
            " :status, :head_event_id, :head_version)",
            closure,
        )

    @staticmethod
    def update_closure_head(conn: sqlite3.Connection, closure_id: str, *,
                            end_utc, status: str, head_event_id: str, head_version: int) -> None:
        conn.execute(
            "UPDATE closures SET end_utc = ?, status = ?, head_event_id = ?,"
            " head_version = ? WHERE closure_id = ?",
            (end_utc, status, head_event_id, head_version, closure_id),
        )

    @staticmethod
    def insert_event(conn: sqlite3.Connection, record: dict) -> None:
        conn.execute(
            "INSERT INTO events (event_id, event_version, event_type, airport_code,"
            " effective_from_utc, effective_until_utc, reported_at_utc,"
            " supersedes_event_id, reason, closure_id, payload_hash, payload_json,"
            " result_json, received_at_utc, processed_at_utc)"
            " VALUES (:event_id, :event_version, :event_type, :airport_code,"
            " :effective_from_utc, :effective_until_utc, :reported_at_utc,"
            " :supersedes_event_id, :reason, :closure_id, :payload_hash, :payload_json,"
            " :result_json, :received_at_utc, :processed_at_utc)",
            record,
        )

    @staticmethod
    def replace_impacts(conn: sqlite3.Connection, closure_id: str, impacts: list[dict]) -> None:
        conn.execute("DELETE FROM impacts WHERE closure_id = ?", (closure_id,))
        conn.executemany(
            "INSERT INTO impacts (closure_id, airport_code, flight_id, impact_status,"
            " delay_minutes, touch_kind, touch_time_utc, window_start_utc,"
            " window_end_utc, computed_by_event_id)"
            " VALUES (:closure_id, :airport_code, :flight_id, :impact_status,"
            " :delay_minutes, :touch_kind, :touch_time_utc, :window_start_utc,"
            " :window_end_utc, :computed_by_event_id)",
            impacts,
        )

    # ----------------------------------------------------------------- helpers

    def _one(self, conn, sql: str, params: tuple):
        if conn is not None:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None
        with closing(self.connect()) as owned:
            row = owned.execute(sql, params).fetchone()
            return dict(row) if row is not None else None
