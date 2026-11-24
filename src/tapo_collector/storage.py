"""Durable local queue backed by SQLite.

Every reading is committed here *before* any upload attempt. A row moves from ``pending`` to
``delivered`` only after the website acknowledged that measurement ID, or to ``rejected`` when
the website permanently refused it (the reason is kept in ``last_error``). The database uses WAL
and ``synchronous=FULL`` so committed readings survive crashes and power loss.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tapo_collector.measurements import METRICS, Measurement, format_timestamp, parse_timestamp

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    measurement_id   TEXT PRIMARY KEY,
    device_id        TEXT NOT NULL,
    timestamp        TEXT NOT NULL,              -- ISO 8601 UTC, millisecond precision
    online           INTEGER NOT NULL CHECK (online IN (0, 1)),
    power_w          REAL,
    energy_today_kwh REAL,
    energy_month_kwh REAL,
    voltage_v        REAL,
    current_a        REAL,
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered', 'rejected')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  REAL NOT NULL DEFAULT 0,    -- unix time; per-record deferral (e.g. unknown device)
    last_error       TEXT,
    created_at       REAL NOT NULL,
    delivered_at     REAL
);
CREATE INDEX IF NOT EXISTS measurements_queue ON measurements (status, next_attempt_at, timestamp);
CREATE INDEX IF NOT EXISTS measurements_device ON measurements (device_id, timestamp);
CREATE TABLE IF NOT EXISTS device_connections (
    device_id   TEXT PRIMARY KEY,
    host        TEXT NOT NULL,
    config_json TEXT NOT NULL,                  -- python-kasa connection parameters, never credentials
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Added in 1.1 (additive, so older collector versions still open the database).
CREATE TABLE IF NOT EXISTS device_identities (
    device_id   TEXT PRIMARY KEY,
    mac         TEXT NOT NULL,                  -- verified after login: the plug's permanent identity
    source      TEXT NOT NULL,                  -- 'config' or 'learned'
    updated_at  REAL NOT NULL
);
"""


@dataclass(frozen=True)
class QueueStats:
    pending: int
    delivered: int
    rejected: int
    oldest_pending: str | None
    deferred: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "pending": self.pending,
            "delivered": self.delivered,
            "rejected": self.rejected,
            "oldest_pending": self.oldest_pending,
            "deferred": self.deferred,
        }


class Storage:
    """Thread-safe wrapper around one SQLite connection. All operations are short transactions."""

    def __init__(self, path: Path, *, max_pending: int = 2_000_000) -> None:
        self.path = path
        self.max_pending = max_pending
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA busy_timeout=30000")
        # Autocommit mode: each CREATE ... IF NOT EXISTS is idempotent on its own.
        self._db.executescript(SCHEMA)
        self._db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        version = int(self._db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"{path} was created by a newer collector version (schema {version}).")
        # Rows left 'in flight' by a crash are simply pending: delivery is idempotent on the server.

    # ------------------------------------------------------------------ plumbing

    class _Tx:
        def __init__(self, storage: Storage) -> None:
            self.storage = storage

        def __enter__(self) -> sqlite3.Connection:
            self.storage._lock.acquire()
            self.storage._db.execute("BEGIN IMMEDIATE")
            return self.storage._db

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            try:
                self.storage._db.execute("ROLLBACK" if exc_type else "COMMIT")
            finally:
                self.storage._lock.release()

    def _transaction(self) -> Storage._Tx:
        return Storage._Tx(self)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ queue

    def save(self, measurement: Measurement, *, now: float | None = None) -> bool:
        """Persist one reading. Returns False when that measurement ID was already stored."""
        now = time.time() if now is None else now
        with self._transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO measurements (measurement_id, device_id, timestamp, online, power_w, "
                "energy_today_kwh, energy_month_kwh, voltage_v, current_a, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    measurement.measurement_id,
                    measurement.device_id,
                    format_timestamp(measurement.timestamp),
                    int(measurement.online),
                    measurement.power_w,
                    measurement.energy_today_kwh,
                    measurement.energy_month_kwh,
                    measurement.voltage_v,
                    measurement.current_a,
                    now,
                ),
            )
            inserted = cursor.rowcount == 1
            if inserted:
                self._enforce_queue_limit(db)
        return inserted

    def _enforce_queue_limit(self, db: sqlite3.Connection) -> None:
        pending = db.execute("SELECT count(*) FROM measurements WHERE status='pending'").fetchone()[0]
        excess = pending - self.max_pending
        if excess > 0:
            db.execute(
                "DELETE FROM measurements WHERE measurement_id IN (SELECT measurement_id FROM measurements WHERE status='pending' ORDER BY timestamp LIMIT ?)",
                (excess,),
            )
            log.error(
                "Local queue limit of %d pending readings reached: dropped the %d oldest undelivered reading(s). "
                "Fix the upload problem or raise storage.max_pending_measurements.",
                self.max_pending,
                excess,
            )

    def next_batch(self, limit: int, *, now: float | None = None) -> list[Measurement]:
        """Oldest pending readings that are due for (re)delivery."""
        now = time.time() if now is None else now
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM measurements WHERE status='pending' AND next_attempt_at<=? ORDER BY timestamp LIMIT ?",
                (now, limit),
            ).fetchall()
        return [self._row_to_measurement(row) for row in rows]

    @staticmethod
    def _row_to_measurement(row: sqlite3.Row) -> Measurement:
        return Measurement(
            measurement_id=row["measurement_id"],
            device_id=row["device_id"],
            timestamp=parse_timestamp(row["timestamp"]),
            online=bool(row["online"]),
            **{name: row[name] for name in METRICS},
        )

    def mark_delivered(self, ids: Iterable[str], *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return self._update_many(
            "UPDATE measurements SET status='delivered', delivered_at=?, last_error=NULL WHERE measurement_id=? AND status='pending'",
            [(now, i) for i in ids],
        )

    def mark_rejected(self, reasons: dict[str, str]) -> int:
        return self._update_many(
            "UPDATE measurements SET status='rejected', attempts=attempts+1, last_error=? WHERE measurement_id=? AND status='pending'",
            [(reason[:500], i) for i, reason in reasons.items()],
        )

    def defer(self, ids: Iterable[str], until: float, error: str) -> int:
        """Keep readings pending but skip them until ``until`` (unix time)."""
        return self._update_many(
            "UPDATE measurements SET attempts=attempts+1, next_attempt_at=?, last_error=? WHERE measurement_id=? AND status='pending'",
            [(until, error[:500], i) for i in ids],
        )

    def record_attempt(self, ids: Iterable[str], error: str) -> int:
        return self._update_many(
            "UPDATE measurements SET attempts=attempts+1, last_error=? WHERE measurement_id=? AND status='pending'",
            [(error[:500], i) for i in ids],
        )

    def _update_many(self, sql: str, params: Sequence[tuple[Any, ...]]) -> int:
        if not params:
            return 0
        with self._transaction() as db:
            changed = 0
            for item in params:
                changed += db.execute(sql, item).rowcount
        return changed

    def requeue_rejected(self) -> int:
        """Put permanently rejected readings back in the queue (after fixing the cause, e.g. a server bug)."""
        with self._transaction() as db:
            return db.execute("UPDATE measurements SET status='pending', next_attempt_at=0 WHERE status='rejected'").rowcount

    def release_deferred(self) -> int:
        """Make deferred readings due immediately (e.g. after registering a device on the website)."""
        with self._transaction() as db:
            return db.execute("UPDATE measurements SET next_attempt_at=0 WHERE status='pending' AND next_attempt_at>0").rowcount

    def prune(self, *, delivered_days: float, rejected_days: float, now: float | None = None) -> int:
        """Delete delivered/rejected rows older than their retention. Pending rows are never pruned here."""
        now = time.time() if now is None else now
        with self._transaction() as db:
            removed = db.execute("DELETE FROM measurements WHERE status='delivered' AND delivered_at<?", (now - delivered_days * 86400,)).rowcount
            removed += db.execute("DELETE FROM measurements WHERE status='rejected' AND created_at<?", (now - rejected_days * 86400,)).rowcount
        return removed

    def stats(self, *, now: float | None = None) -> QueueStats:
        now = time.time() if now is None else now
        with self._lock:
            counts = dict(self._db.execute("SELECT status, count(*) FROM measurements GROUP BY status").fetchall())
            oldest = self._db.execute("SELECT min(timestamp) FROM measurements WHERE status='pending'").fetchone()[0]
            deferred = self._db.execute("SELECT count(*) FROM measurements WHERE status='pending' AND next_attempt_at>?", (now,)).fetchone()[0]
        return QueueStats(
            pending=counts.get("pending", 0),
            delivered=counts.get("delivered", 0),
            rejected=counts.get("rejected", 0),
            oldest_pending=oldest,
            deferred=deferred,
        )

    def recent_rejections(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT measurement_id, device_id, timestamp, last_error FROM measurements WHERE status='rejected' ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def last_reading(self, device_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM measurements WHERE device_id=? ORDER BY timestamp DESC LIMIT 1", (device_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ connection cache

    def get_connection(self, device_id: str, host: str) -> dict[str, Any] | None:
        """Cached protocol parameters of this physical plug. They do not depend on its address, so they are reused
        after an IP change (``host`` is accepted for interface compatibility; the caller supplies the address)."""
        with self._lock:
            row = self._db.execute("SELECT config_json FROM device_connections WHERE device_id=?", (device_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_last_host(self, device_id: str) -> str | None:
        """The address at which the plug was last connected *and* verified (survives restarts)."""
        with self._lock:
            row = self._db.execute("SELECT host FROM device_connections WHERE device_id=?", (device_id,)).fetchone()
        return str(row[0]) if row else None

    def set_connection(self, device_id: str, host: str, config: dict[str, Any]) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO device_connections (device_id, host, config_json, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(device_id) DO UPDATE SET host=excluded.host, config_json=excluded.config_json, "
                "updated_at=excluded.updated_at",
                (device_id, host, json.dumps(config, sort_keys=True), time.time()),
            )

    def forget_connection(self, device_id: str) -> None:
        with self._transaction() as db:
            db.execute("DELETE FROM device_connections WHERE device_id=?", (device_id,))

    # ------------------------------------------------------------------ device identities

    def get_identity(self, device_id: str) -> tuple[str, str] | None:
        """(mac, source) of a plug, or None when it has never been verified."""
        with self._lock:
            row = self._db.execute("SELECT mac, source FROM device_identities WHERE device_id=?", (device_id,)).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def set_identity(self, device_id: str, mac: str, source: str) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO device_identities (device_id, mac, source, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(device_id) DO UPDATE SET mac=excluded.mac, source=excluded.source, updated_at=excluded.updated_at",
                (device_id, mac, source, time.time()),
            )

    def identity_owner(self, mac: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT device_id FROM device_identities WHERE mac=? ORDER BY updated_at DESC LIMIT 1", (mac,)).fetchone()
        return str(row[0]) if row else None

    def forget_identity(self, device_id: str) -> None:
        with self._transaction() as db:
            db.execute("DELETE FROM device_identities WHERE device_id=?", (device_id,))

    def forget_identities_except(self, device_ids: Iterable[str]) -> int:
        """Drop identities of plugs that were removed from the configuration (so their MAC can be reused)."""
        keep = list(device_ids)
        with self._transaction() as db:
            placeholders = ",".join("?" * len(keep)) or "''"
            return db.execute(f"DELETE FROM device_identities WHERE device_id NOT IN ({placeholders})", keep).rowcount  # noqa: S608
