from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4


_DEFAULT_REQUESTS_PER_WINDOW = 10
_DEFAULT_RATE_WINDOW_SECONDS = 5 * 60
_DEFAULT_USER_CONCURRENCY = 1
_DEFAULT_GLOBAL_CONCURRENCY = 2
_DEFAULT_LEASE_SECONDS = 60


class SttAdmissionUnavailable(RuntimeError):
    """The durable admission database could not make a safe decision."""


class SttAdmissionRejected(RuntimeError):
    def __init__(self, reason_code: str, retry_after: int) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retry_after = max(1, int(retry_after))


@dataclass(frozen=True)
class SttAdmissionConfig:
    requests_per_window: int = _DEFAULT_REQUESTS_PER_WINDOW
    rate_window_seconds: int = _DEFAULT_RATE_WINDOW_SECONDS
    user_concurrency: int = _DEFAULT_USER_CONCURRENCY
    global_concurrency: int = _DEFAULT_GLOBAL_CONCURRENCY
    lease_seconds: int = _DEFAULT_LEASE_SECONDS

    @classmethod
    def from_environment(cls) -> "SttAdmissionConfig":
        body_read_timeout = _environment_int(
            "MOUCHEN_STT_BODY_READ_TIMEOUT_SECONDS",
            30,
            minimum=5,
            maximum=120,
        )
        inference_timeout = _environment_int(
            "MOUCHEN_LOCAL_STT_TIMEOUT_SECONDS",
            15,
            minimum=5,
            maximum=120,
        )
        configured_lease = _environment_int(
            "MOUCHEN_STT_LEASE_SECONDS",
            _DEFAULT_LEASE_SECONDS,
            minimum=5,
            maximum=3_600,
        )
        return cls(
            requests_per_window=_environment_int(
                "MOUCHEN_STT_REQUESTS_PER_WINDOW",
                _DEFAULT_REQUESTS_PER_WINDOW,
                minimum=1,
                maximum=10_000,
            ),
            rate_window_seconds=_environment_int(
                "MOUCHEN_STT_RATE_WINDOW_SECONDS",
                _DEFAULT_RATE_WINDOW_SECONDS,
                minimum=1,
                maximum=86_400,
            ),
            user_concurrency=_environment_int(
                "MOUCHEN_STT_USER_CONCURRENCY",
                _DEFAULT_USER_CONCURRENCY,
                minimum=1,
                maximum=100,
            ),
            global_concurrency=_environment_int(
                "MOUCHEN_STT_GLOBAL_CONCURRENCY",
                _DEFAULT_GLOBAL_CONCURRENCY,
                minimum=1,
                maximum=1_000,
            ),
            # Keep the lease alive for the bounded upload plus the configured
            # inference timeout. Defaults still resolve to a 60-second lease.
            lease_seconds=max(
                configured_lease,
                body_read_timeout + inference_timeout + 15,
            ),
        )


@dataclass
class SttAdmissionLease:
    lease_id: str
    user_id: str
    expires_at: datetime
    _controller: "SttAdmissionController"

    def release(self, *, now: datetime | None = None) -> bool:
        return self._controller.release(self.lease_id, self.user_id, now=now)


class SttAdmissionController:
    """SQLite-backed STT rate and concurrency admission.

    Each operation opens its own connection. ``BEGIN IMMEDIATE`` serializes the
    count-and-insert decision across processes, while expiring leases recover
    capacity after a process crash or host restart.
    """

    def __init__(
        self,
        database_path: str | Path | None = None,
        *,
        config: SttAdmissionConfig | None = None,
    ) -> None:
        self._database_path = Path(database_path) if database_path is not None else None
        self._config = config
        self._schema_lock = threading.Lock()
        self._initialized_paths: set[Path] = set()

    def acquire(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> SttAdmissionLease:
        normalized_user_id = str(user_id).strip()
        if not normalized_user_id:
            raise ValueError("user_id is required")
        current = _as_utc(now)
        config = self._config or SttAdmissionConfig.from_environment()
        window_start = current - timedelta(seconds=config.rate_window_seconds)
        lease_expires_at = current + timedelta(seconds=config.lease_seconds)
        path = self._resolved_database_path()

        connection = self._connect(path)
        try:
            self._ensure_schema(connection, path)
            connection.execute("BEGIN IMMEDIATE")
            retired = connection.execute(
                "SELECT 1 FROM retired_user_ids WHERE user_id_hash=?",
                (hashlib.sha256(normalized_user_id.encode("utf-8")).hexdigest(),),
            ).fetchone()
            if retired is not None:
                connection.commit()
                raise SttAdmissionRejected("account_retired", 3600)
            self._prune_expired(
                connection,
                current=current,
                window_start=window_start,
            )

            rate_row = connection.execute(
                """SELECT COUNT(*) AS request_count,
                          MIN(requested_at) AS oldest_request
                   FROM stt_admission_requests
                   WHERE user_id=? AND requested_at>?""",
                (normalized_user_id, window_start.isoformat()),
            ).fetchone()
            if int(rate_row["request_count"]) >= config.requests_per_window:
                retry_at = _parse_utc(rate_row["oldest_request"]) + timedelta(
                    seconds=config.rate_window_seconds
                )
                connection.commit()
                raise SttAdmissionRejected(
                    "stt_rate_limit_exhausted",
                    _retry_after(current, retry_at),
                )

            # Count an admitted request even when concurrency is currently full.
            # Repeated polling therefore consumes the same rolling allowance
            # without ever reading or retaining the request body.
            connection.execute(
                """INSERT INTO stt_admission_requests(user_id, requested_at)
                   VALUES(?,?)""",
                (normalized_user_id, current.isoformat()),
            )

            user_row = connection.execute(
                """SELECT COUNT(*) AS active_count,
                          MIN(expires_at) AS earliest_expiry
                   FROM stt_admission_leases
                   WHERE user_id=? AND released_at IS NULL AND expires_at>?""",
                (normalized_user_id, current.isoformat()),
            ).fetchone()
            if int(user_row["active_count"]) >= config.user_concurrency:
                connection.commit()
                raise SttAdmissionRejected(
                    "stt_user_concurrency_exhausted",
                    _retry_after(current, _parse_utc(user_row["earliest_expiry"])),
                )

            global_row = connection.execute(
                """SELECT COUNT(*) AS active_count,
                          MIN(expires_at) AS earliest_expiry
                   FROM stt_admission_leases
                   WHERE released_at IS NULL AND expires_at>?""",
                (current.isoformat(),),
            ).fetchone()
            if int(global_row["active_count"]) >= config.global_concurrency:
                connection.commit()
                raise SttAdmissionRejected(
                    "stt_global_concurrency_exhausted",
                    _retry_after(current, _parse_utc(global_row["earliest_expiry"])),
                )

            lease_id = str(uuid4())
            connection.execute(
                """INSERT INTO stt_admission_leases(
                     id,user_id,acquired_at,expires_at,released_at
                   ) VALUES(?,?,?,?,NULL)""",
                (
                    lease_id,
                    normalized_user_id,
                    current.isoformat(),
                    lease_expires_at.isoformat(),
                ),
            )
            connection.commit()
        except SttAdmissionRejected:
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise SttAdmissionUnavailable("STT admission is temporarily unavailable") from exc
        except BaseException:
            _rollback_quietly(connection)
            raise
        finally:
            connection.close()

        return SttAdmissionLease(
            lease_id=lease_id,
            user_id=normalized_user_id,
            expires_at=lease_expires_at,
            _controller=self,
        )

    def release(
        self,
        lease_id: str,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        normalized_lease_id = str(lease_id).strip()
        normalized_user_id = str(user_id).strip()
        if not normalized_lease_id or not normalized_user_id:
            return False
        current = _as_utc(now)
        path = self._resolved_database_path()
        connection = self._connect(path)
        try:
            self._ensure_schema(connection, path)
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE stt_admission_leases SET released_at=?
                   WHERE id=? AND user_id=? AND released_at IS NULL""",
                (current.isoformat(), normalized_lease_id, normalized_user_id),
            )
            released = cursor.rowcount == 1
            connection.commit()
            return released
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise SttAdmissionUnavailable("STT admission is temporarily unavailable") from exc
        except BaseException:
            _rollback_quietly(connection)
            raise
        finally:
            connection.close()

    def _resolved_database_path(self) -> Path:
        if self._database_path is not None:
            return self._database_path.resolve()
        configured = os.getenv("MOUCHEN_DB_PATH", "").strip()
        if configured:
            return Path(configured).resolve()
        return (Path(__file__).resolve().parents[1] / "data" / "mouchen.db").resolve()

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.create_function(
                "mouchen_user_id_hash",
                1,
                lambda value: hashlib.sha256(
                    str(value or "").encode("utf-8")
                ).hexdigest(),
                deterministic=True,
            )
            connection.execute("PRAGMA busy_timeout=5000")
            return connection
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise SttAdmissionUnavailable(
                "STT admission is temporarily unavailable"
            ) from exc

    def _ensure_schema(self, connection: sqlite3.Connection, path: Path) -> None:
        with self._schema_lock:
            if path in self._initialized_paths:
                return
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS retired_user_ids (
                  user_id_hash TEXT PRIMARY KEY,
                  retired_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stt_admission_requests (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id TEXT NOT NULL,
                  requested_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_stt_admission_requests_user_window
                  ON stt_admission_requests(user_id, requested_at);
                CREATE INDEX IF NOT EXISTS idx_stt_admission_requests_window
                  ON stt_admission_requests(requested_at);
                CREATE TABLE IF NOT EXISTS stt_admission_leases (
                  id TEXT PRIMARY KEY,
                  user_id TEXT NOT NULL,
                  acquired_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  released_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_stt_admission_leases_active
                  ON stt_admission_leases(released_at, expires_at);
                CREATE INDEX IF NOT EXISTS idx_stt_admission_leases_user_active
                  ON stt_admission_leases(user_id, released_at, expires_at);
                """
            )
            connection.commit()
            self._initialized_paths.add(path)

    @staticmethod
    def _prune_expired(
        connection: sqlite3.Connection,
        *,
        current: datetime,
        window_start: datetime,
    ) -> None:
        connection.execute(
            "DELETE FROM stt_admission_requests WHERE requested_at<=?",
            (window_start.isoformat(),),
        )
        connection.execute(
            """UPDATE stt_admission_leases SET released_at=expires_at
               WHERE released_at IS NULL AND expires_at<=?""",
            (current.isoformat(),),
        )
        connection.execute(
            "DELETE FROM stt_admission_leases WHERE released_at IS NOT NULL AND released_at<=?",
            (window_start.isoformat(),),
        )


def _environment_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_utc(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value))


def _retry_after(current: datetime, retry_at: datetime) -> int:
    return max(1, math.ceil((retry_at - current).total_seconds()))


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    try:
        connection.rollback()
    except sqlite3.Error:
        pass


stt_admission = SttAdmissionController()
