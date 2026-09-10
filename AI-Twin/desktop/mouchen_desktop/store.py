from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .security import Protector, WindowsDataProtector
from .settings import default_data_dir


MAX_EVENT_ATTEMPTS = 3


class EventStore:
    def __init__(self, path: Path | None = None, protector: Protector | None = None) -> None:
        self.path = path or default_data_dir() / "mouchen-desktop.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.protector = protector or WindowsDataProtector()
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def enqueue(self, event: dict[str, Any]) -> str:
        event_id = str(event["local_id"])
        payload = self._encode(event)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT OR IGNORE INTO events(id, created_at, payload, synced, attempts, last_error)
                   VALUES(?, ?, ?, 0, 0, NULL)""",
                (event_id, now, payload),
            )
        return event_id

    def pending(
        self,
        limit: int = 100,
        max_attempts: int = MAX_EVENT_ATTEMPTS,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT payload FROM events
                   WHERE synced=0 AND attempts<?
                   ORDER BY attempts, created_at
                   LIMIT ?""",
                (max_attempts, limit),
            ).fetchall()
        return [self._decode(row["payload"]) for row in rows]

    def mark_synced(self, event_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE events SET synced=1, last_error=NULL WHERE id=?", (event_id,)
            )

    def mark_failed(self, event_id: str, error: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE events SET attempts=attempts+1, last_error=? WHERE id=?",
                (error[:500], event_id),
            )

    def save_advice(self, advice: dict[str, Any]) -> bool:
        advice_id = str(advice["id"])
        created_at = str(advice.get("created_at") or datetime.now(timezone.utc).isoformat())
        status = str(advice.get("status", "active"))
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT created_at, status, payload FROM advice WHERE id=?", (advice_id,)
            ).fetchone()
            if existing is not None:
                try:
                    unchanged = (
                        str(existing["created_at"]) == created_at
                        and str(existing["status"]) == status
                        and self._decode(existing["payload"]) == advice
                    )
                except (OSError, ValueError, TypeError, UnicodeDecodeError):
                    unchanged = False
                if unchanged:
                    return False

            payload = self._encode(advice)
            self._connection.execute(
                """INSERT INTO advice(id, created_at, status, payload)
                   VALUES(?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     created_at=excluded.created_at,
                     status=excluded.status,
                     payload=excluded.payload""",
                (advice_id, created_at, status, payload),
            )
        return True

    def update_advice_status(self, advice_id: str, status: str) -> None:
        with self._lock, self._connection:
            self._connection.execute("UPDATE advice SET status=? WHERE id=?", (status, advice_id))

    def has_advice(self, advice_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM advice WHERE id=?", (advice_id,)
            ).fetchone()
        return row is not None

    def list_advice(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, payload FROM advice ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            value = self._decode(row["payload"])
            value["status"] = row["status"]
            result.append(value)
        return result

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT synced, attempts, last_error, payload
                   FROM events ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            value = self._decode(row["payload"])
            value["synced"] = bool(row["synced"])
            value["attempts"] = int(row["attempts"])
            value["last_error"] = row["last_error"]
            value["quarantined"] = not bool(row["synced"]) and int(row["attempts"]) >= MAX_EVENT_ATTEMPTS
            result.append(value)
        return result

    def stats(self) -> dict[str, int]:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with self._lock:
            event_count = self._connection.execute(
                "SELECT COUNT(*) FROM events WHERE created_at>=?", (since,)
            ).fetchone()[0]
            pending = self._connection.execute(
                "SELECT COUNT(*) FROM events WHERE synced=0 AND attempts<?",
                (MAX_EVENT_ATTEMPTS,),
            ).fetchone()[0]
            quarantined = self._connection.execute(
                "SELECT COUNT(*) FROM events WHERE synced=0 AND attempts>=?",
                (MAX_EVENT_ATTEMPTS,),
            ).fetchone()[0]
            advice = self._connection.execute("SELECT COUNT(*) FROM advice").fetchone()[0]
        return {
            "events_24h": int(event_count),
            "pending": int(pending),
            "quarantined": int(quarantined),
            "advice": int(advice),
        }

    def purge(self, retention_days: int = 90) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM events WHERE created_at<? AND synced=1", (cutoff,)
            )
        return cursor.rowcount

    def clear_account_data(self) -> None:
        """Remove one account's local cache while preserving this device id."""
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM events")
            self._connection.execute("DELETE FROM advice")
            self._connection.execute(
                "DELETE FROM collector_state WHERE key NOT LIKE 'device_identity:%'"
            )

    def load_state(self, key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM collector_state WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return dict(default or {})
        value = self._decode(row["payload"])
        return value if isinstance(value, dict) else dict(default or {})

    def save_state(self, key: str, value: dict[str, Any]) -> None:
        payload = self._encode(value)
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO collector_state(key, updated_at, payload)
                   VALUES(?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     updated_at=excluded.updated_at,
                     payload=excluded.payload""",
                (key, updated_at, payload),
            )

    def list_states(self, prefix: str) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT key, payload FROM collector_state
                   WHERE substr(key, 1, ?)=? ORDER BY updated_at""",
                (len(prefix), prefix),
            ).fetchall()
        result: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            try:
                value = self._decode(row["payload"])
            except (OSError, ValueError, TypeError, UnicodeDecodeError):
                continue
            if isinstance(value, dict):
                result.append((str(row["key"]), value))
        return result

    def get_or_create_device_id(self, platform: str = "windows") -> str:
        """Return an encrypted, install-stable identity without touching user settings."""
        normalized = platform.strip().casefold() or "windows"
        key = f"device_identity:{normalized}"
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM collector_state WHERE key=?", (key,)
            ).fetchone()
            if row is not None:
                try:
                    value = self._decode(row["payload"])
                    existing = str(value.get("device_id", "")).strip()
                    if existing:
                        return existing
                except (OSError, ValueError, TypeError, UnicodeDecodeError):
                    pass

            device_id = f"{normalized}-{uuid.uuid4()}"
            payload = self._encode({"device_id": device_id, "platform": normalized})
            updated_at = datetime.now(timezone.utc).isoformat()
            with self._connection:
                self._connection.execute(
                    """INSERT INTO collector_state(key, updated_at, payload)
                       VALUES(?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                         updated_at=excluded.updated_at,
                         payload=excluded.payload""",
                    (key, updated_at, payload),
                )
            return device_id

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events(
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    synced INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_desktop_events_sync ON events(synced, created_at);
                CREATE INDEX IF NOT EXISTS idx_desktop_events_created ON events(created_at DESC);
                CREATE TABLE IF NOT EXISTS advice(
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_desktop_advice_created ON advice(created_at DESC);
                CREATE TABLE IF NOT EXISTS collector_state(
                    key TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    payload BLOB NOT NULL
                );
                """
            )

    def _encode(self, value: dict[str, Any]) -> bytes:
        plain = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self.protector.protect(plain)

    def _decode(self, value: bytes) -> dict[str, Any]:
        return json.loads(self.protector.unprotect(bytes(value)).decode("utf-8"))
