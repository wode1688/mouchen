from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import math
import os
from pathlib import Path
import sqlite3
from uuid import uuid4


class AccountExportBusy(RuntimeError):
    def __init__(self, reason_code: str, retry_after: int) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retry_after = max(1, int(retry_after))


class AccountExportAdmissionUnavailable(RuntimeError):
    pass


@dataclass
class AccountExportLease:
    lease_id: str
    expires_at: datetime
    _controller: "AccountExportAdmission"

    def release(self) -> bool:
        return self._controller.release(self.lease_id)


class AccountExportAdmission:
    """Cross-process cooldown and single-flight gate for full account exports."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self._database_path = Path(database_path) if database_path else None

    def acquire(
        self, user_id: str, *, now: datetime | None = None
    ) -> AccountExportLease:
        current = _as_utc(now)
        cooldown_seconds = _environment_int(
            "MOUCHEN_ACCOUNT_EXPORT_COOLDOWN_SECONDS", 300, 1, 86_400
        )
        lease_seconds = _environment_int(
            "MOUCHEN_ACCOUNT_EXPORT_LEASE_SECONDS", 3600, 60, 86_400
        )
        user_hash = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()
        try:
            connection = self._connect()
        except (OSError, sqlite3.Error) as exc:
            raise AccountExportAdmissionUnavailable from exc
        try:
            self._ensure_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM account_export_leases WHERE expires_at<=?",
                (current.isoformat(),),
            )
            connection.execute(
                "DELETE FROM account_export_requests WHERE requested_at<=?",
                ((current - timedelta(days=7)).isoformat(),),
            )
            prior = connection.execute(
                """SELECT MAX(requested_at) AS requested_at
                FROM account_export_requests WHERE user_id_hash=?""",
                (user_hash,),
            ).fetchone()
            if prior["requested_at"]:
                retry_at = _parse_utc(prior["requested_at"]) + timedelta(
                    seconds=cooldown_seconds
                )
                if retry_at > current:
                    connection.commit()
                    raise AccountExportBusy(
                        "account_export_cooldown", _retry_after(current, retry_at)
                    )
            active = connection.execute(
                "SELECT MIN(expires_at) AS expires_at FROM account_export_leases"
            ).fetchone()
            if active["expires_at"]:
                connection.commit()
                raise AccountExportBusy(
                    "account_export_in_progress",
                    _retry_after(current, _parse_utc(active["expires_at"])),
                )
            lease_id = uuid4().hex
            expires_at = current + timedelta(seconds=lease_seconds)
            connection.execute(
                """INSERT INTO account_export_requests(user_id_hash,requested_at)
                VALUES(?,?)""",
                (user_hash, current.isoformat()),
            )
            connection.execute(
                """INSERT INTO account_export_leases(id,user_id_hash,acquired_at,expires_at)
                VALUES(?,?,?,?)""",
                (lease_id, user_hash, current.isoformat(), expires_at.isoformat()),
            )
            connection.commit()
            return AccountExportLease(lease_id, expires_at, self)
        except AccountExportBusy:
            raise
        except (OSError, sqlite3.Error) as exc:
            connection.rollback()
            raise AccountExportAdmissionUnavailable from exc
        finally:
            connection.close()

    def release(self, lease_id: str) -> bool:
        try:
            connection = self._connect()
        except (OSError, sqlite3.Error) as exc:
            raise AccountExportAdmissionUnavailable from exc
        try:
            self._ensure_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM account_export_leases WHERE id=?", (lease_id,)
            )
            connection.commit()
            return cursor.rowcount == 1
        except (OSError, sqlite3.Error) as exc:
            connection.rollback()
            raise AccountExportAdmissionUnavailable from exc
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        path = self._database_path
        if path is None:
            configured = os.getenv("MOUCHEN_DB_PATH", "").strip()
            path = (
                Path(configured)
                if configured
                else Path(__file__).resolve().parents[1] / "data" / "mouchen.db"
            )
        connection = sqlite3.connect(path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS account_export_requests(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id_hash TEXT NOT NULL,
              requested_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_account_export_requests_user
              ON account_export_requests(user_id_hash,requested_at);
            CREATE TABLE IF NOT EXISTS account_export_leases(
              id TEXT PRIMARY KEY,
              user_id_hash TEXT NOT NULL,
              acquired_at TEXT NOT NULL,
              expires_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_account_export_single_flight
              ON account_export_leases((1));
            """
        )


def _environment_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _as_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return (
        current.replace(tzinfo=timezone.utc)
        if current.tzinfo is None
        else current.astimezone(timezone.utc)
    )


def _parse_utc(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value))


def _retry_after(current: datetime, retry_at: datetime) -> int:
    return max(1, math.ceil((retry_at - current).total_seconds()))


account_export_admission = AccountExportAdmission()
