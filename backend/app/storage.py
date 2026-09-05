from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .auth import (
    AuthPrincipal,
    constant_time_token_match,
    hash_password,
    new_access_token,
    normalize_username,
    token_hash,
    token_locator,
    utc_now as auth_utc_now,
    validate_password,
    verify_password,
)

from .domain.models import (
    AdviceLevel,
    AdviceRecord,
    AdviceStatus,
    Event,
    FeedbackCreate,
    Goal,
    OutcomeCreate,
    OutcomeStatus,
)
from .domain.preferences import (
    AdvicePreference,
    EffectivePreference,
    GLOBAL_SCOPE_KEY,
    REASON_PREFERENCE_GLOBAL_LIMIT,
    REASON_PREFERENCE_GOAL_COOLDOWN,
    REASON_PREFERENCE_GOAL_LIMIT,
    REASON_PREFERENCE_IMPORTANT_ONLY,
    REASON_PREFERENCE_PAUSED,
    automatic_emergency_record,
    default_global_preference,
    frequency_policy,
    goal_scope_key,
    is_manual_problem_event,
    resolve_effective,
)
from .domain.scoring import TrustSummary
from .localization import (
    DEFAULT_LOCALE,
    advice_display_source,
    advice_display_source_hash,
    advice_display_source_matches_locale,
    normalize_locale,
    validate_advice_display_translation,
)
from .privacy import sanitize_cloud_context


class GoalQuotaExceeded(Exception):
    """Raised when an atomic per-user goal insert would exceed its quota."""


class CharterQuotaExceeded(Exception):
    """Raised when an atomic per-user charter insert would exceed its quota."""


class FeedbackQuotaExceeded(Exception):
    """Raised when user feedback would exceed a persistent tenant quota."""


class EventQuotaExceeded(Exception):
    """Raised when an atomic event insert would exceed tenant storage."""


class DeviceQuotaExceeded(Exception):
    """Raised when a tenant tries to retain too many device identities."""


class AccountPasswordInvalid(Exception):
    """Raised when an authenticated account password cannot be verified."""


class AccountChangedConcurrently(Exception):
    """Raised when account credentials changed during expensive hash work."""


class AccountDeletionIncomplete(RuntimeError):
    """Raised when a tenant lifecycle transaction cannot prove zero residuals."""


class AccountNotFound(Exception):
    """Raised when an authenticated account disappeared before an operation."""


class AccountRetired(RuntimeError):
    """Raised when delayed work targets a permanently retired tenant id."""


class AccountExportTooLarge(RuntimeError):
    """Raised before publishing an export that exceeds the bounded spool."""


@dataclass(frozen=True)
class PreparedAccountExport:
    path: Path
    directory: Path
    bytes_written: int

    def cleanup(self) -> None:
        self.path.unlink(missing_ok=True)
        try:
            self.directory.rmdir()
        except (FileNotFoundError, OSError):
            pass


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS retired_user_ids (
  user_id_hash TEXT PRIMARY KEY,
  retired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username_normalized TEXT,
  username_display TEXT,
  password_hash TEXT,
  is_active INTEGER NOT NULL DEFAULT 1,
  locale TEXT NOT NULL DEFAULT 'zh-CN',
  locale_updated_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_tokens (
  id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  user_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  device_name TEXT,
  scopes_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_used_at TEXT,
  revoked_at TEXT,
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_user_device
  ON auth_tokens(user_id, device_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_expiry
  ON auth_tokens(expires_at, revoked_at);
CREATE TABLE IF NOT EXISTS auth_login_failures (
  identity_hash TEXT PRIMARY KEY,
  failed_count INTEGER NOT NULL,
  window_started_at TEXT NOT NULL,
  locked_until TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_login_failures_updated
  ON auth_login_failures(updated_at);
CREATE TABLE IF NOT EXISTS auth_registration_codes (
  code_hash TEXT PRIMARY KEY,
  consumed_by_user_id TEXT NOT NULL,
  consumed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_invites (
  id TEXT PRIMARY KEY,
  code_hash TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  consumed_at TEXT,
  consumed_by_user_id TEXT,
  consumed_by_user_hash TEXT,
  FOREIGN KEY(consumed_by_user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_auth_invites_state
  ON auth_invites(consumed_at, revoked_at, expires_at);
CREATE TABLE IF NOT EXISTS goals (
  id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  domain TEXT NOT NULL,
  version INTEGER NOT NULL,
  quote TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  reaffirmed_at TEXT NOT NULL,
  PRIMARY KEY(user_id, id)
);
CREATE INDEX IF NOT EXISTS idx_goals_user_domain ON goals(user_id, domain, version DESC);
CREATE TABLE IF NOT EXISTS events (
  id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  source TEXT NOT NULL,
  type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  evidence_ref TEXT,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(user_id, id)
);
CREATE INDEX IF NOT EXISTS idx_events_user_time ON events(user_id, occurred_at DESC);
CREATE TABLE IF NOT EXISTS tenant_storage_usage (
  user_id TEXT PRIMARY KEY,
  event_bytes INTEGER NOT NULL DEFAULT 0 CHECK(event_bytes >= 0),
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS charters (
  user_id TEXT NOT NULL,
  domain TEXT NOT NULL,
  max_level INTEGER NOT NULL DEFAULT 2,
  redline_authorized INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(user_id, domain)
);
CREATE TABLE IF NOT EXISTS advice (
  id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  domain TEXT NOT NULL,
  level INTEGER NOT NULL,
  dedupe_key TEXT NOT NULL,
  topic_key TEXT,
  evidence_ref TEXT,
  status TEXT NOT NULL,
  delivery TEXT NOT NULL,
  prediction_confidence REAL NOT NULL,
  prediction_deadline TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  goal_id TEXT,
  published_at TEXT,
  PRIMARY KEY(user_id, id)
);
CREATE INDEX IF NOT EXISTS idx_advice_user_created ON advice(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_advice_dedupe ON advice(user_id, dedupe_key, status);
CREATE TABLE IF NOT EXISTS advice_localizations (
  user_id TEXT NOT NULL,
  advice_id TEXT NOT NULL,
  locale TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','running','ready','unavailable')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at TEXT NOT NULL,
  translated_json TEXT,
  provider TEXT,
  model TEXT,
  last_error TEXT,
  lease_token TEXT,
  lease_expires_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(user_id, advice_id, locale),
  FOREIGN KEY(user_id, advice_id) REFERENCES advice(user_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_advice_localizations_due
  ON advice_localizations(status, next_attempt_at, updated_at);
CREATE TABLE IF NOT EXISTS advice_preferences (
  user_id TEXT NOT NULL,
  scope_key TEXT NOT NULL,
  goal_id TEXT,
  direction TEXT NOT NULL DEFAULT '',
  direction_mode TEXT NOT NULL DEFAULT 'inherit',
  frequency_mode TEXT,
  event_types_json TEXT,
  revision INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(user_id, scope_key)
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  advice_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  feedback_id TEXT,
  semantic_key TEXT,
  kind TEXT NOT NULL,
  note TEXT,
  origin TEXT NOT NULL DEFAULT 'user',
  signal_weight REAL NOT NULL DEFAULT 1.0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relevance_ledger (
  advice_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  topic_key TEXT NOT NULL,
  signal_kind TEXT NOT NULL,
  origin TEXT NOT NULL,
  weight REAL NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(user_id, advice_id, signal_kind, origin)
);
CREATE INDEX IF NOT EXISTS idx_relevance_ledger_topic
  ON relevance_ledger(user_id, topic_key, created_at DESC);
CREATE TABLE IF NOT EXISTS outcomes (
  advice_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  status TEXT NOT NULL,
  actual_result TEXT NOT NULL,
  utility REAL,
  timing_quality REAL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(user_id, advice_id)
);
CREATE TABLE IF NOT EXISTS trust_accounts (
  user_id TEXT NOT NULL,
  domain TEXT NOT NULL,
  level INTEGER NOT NULL,
  judged INTEGER NOT NULL DEFAULT 0,
  correct INTEGER NOT NULL DEFAULT 0,
  brier_sum REAL NOT NULL DEFAULT 0,
  utility_sum REAL NOT NULL DEFAULT 0,
  timing_sum REAL NOT NULL DEFAULT 0,
  catastrophic_errors INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(user_id, domain, level)
);
CREATE TABLE IF NOT EXISTS error_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  advice_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  domain TEXT NOT NULL,
  effective_level INTEGER NOT NULL,
  error_kind TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_error_ledger_budget
  ON error_ledger(user_id, effective_level, created_at DESC);
CREATE TABLE IF NOT EXISTS speaking_freezes (
  user_id TEXT NOT NULL,
  level INTEGER NOT NULL,
  error_count INTEGER NOT NULL,
  allowed_errors INTEGER NOT NULL,
  window_started_at TEXT NOT NULL,
  frozen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  reason TEXT NOT NULL,
  PRIMARY KEY(user_id, level)
);
CREATE INDEX IF NOT EXISTS idx_speaking_freezes_expiry
  ON speaking_freezes(user_id, expires_at);
CREATE TABLE IF NOT EXISTS topic_suppressions (
  user_id TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  source_advice_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  PRIMARY KEY(user_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_topic_suppressions_expiry
  ON topic_suppressions(user_id, expires_at);
CREATE TABLE IF NOT EXISTS cloud_slices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  purpose TEXT NOT NULL,
  redacted_context_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_drafts (
  id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  action_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  confirmed_at TEXT,
  executed_at TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(user_id, id)
);
CREATE TABLE IF NOT EXISTS analysis_jobs (
  event_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  job_kind TEXT NOT NULL DEFAULT 'discover' CHECK (
    job_kind IN ('discover','refine_deterministic')
  ),
  goal_id TEXT,
  cloud_approved INTEGER NOT NULL DEFAULT 0,
  raw_cloud_approved INTEGER NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN 0 AND 100),
  status TEXT NOT NULL CHECK (
    status IN ('pending','running','no_intervention','published','retry')
  ),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  last_reason TEXT,
  last_failure_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  PRIMARY KEY(user_id, event_id),
  FOREIGN KEY(user_id, event_id) REFERENCES events(user_id, id)
);
CREATE INDEX IF NOT EXISTS idx_analysis_jobs_due
  ON analysis_jobs(user_id, status, next_attempt_at, created_at);
CREATE TABLE IF NOT EXISTS analysis_model_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  calls INTEGER NOT NULL CHECK (calls BETWEEN 1 AND 2),
  used_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analysis_model_usage_window
  ON analysis_model_usage(user_id, used_at);
CREATE TABLE IF NOT EXISTS analysis_model_reservations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  reserved_calls INTEGER NOT NULL CHECK (reserved_calls BETWEEN 1 AND 2),
  actual_calls INTEGER NOT NULL DEFAULT 0 CHECK (
    actual_calls BETWEEN 0 AND reserved_calls
  ),
  created_at TEXT NOT NULL,
  settled_at TEXT,
  UNIQUE(user_id, event_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_analysis_model_reservations_window
  ON analysis_model_reservations(user_id, created_at);
CREATE TABLE IF NOT EXISTS global_model_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  purpose TEXT NOT NULL,
  calls INTEGER NOT NULL CHECK (calls=1),
  used_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cloud_slices_user_time
  ON cloud_slices(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_global_model_usage_window
  ON global_model_usage(used_at);
CREATE INDEX IF NOT EXISTS idx_global_model_usage_user_window
  ON global_model_usage(user_id, used_at);
CREATE TABLE IF NOT EXISTS direct_model_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  requested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_direct_model_requests_user_window
  ON direct_model_requests(user_id, requested_at);
CREATE INDEX IF NOT EXISTS idx_direct_model_requests_window
  ON direct_model_requests(requested_at);
CREATE TABLE IF NOT EXISTS direct_model_leases (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_direct_model_leases_active
  ON direct_model_leases(released_at, expires_at);
CREATE INDEX IF NOT EXISTS idx_direct_model_leases_user_active
  ON direct_model_leases(user_id, released_at, expires_at);
CREATE TABLE IF NOT EXISTS devices (
  user_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  platform TEXT,
  app_version TEXT,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  PRIMARY KEY(user_id, device_id)
);
CREATE TABLE IF NOT EXISTS advice_attention (
  advice_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  topic_key TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending','claimed','waiting','resolved')),
  delivery_count INTEGER NOT NULL DEFAULT 0 CHECK (delivery_count BETWEEN 0 AND 2),
  first_delivered_at TEXT,
  last_delivered_at TEXT,
  next_eligible_at TEXT,
  auto_close_at TEXT,
  claim_token TEXT,
  claim_device_id TEXT,
  claim_expires_at TEXT,
  resolved_at TEXT,
  resolved_reason TEXT,
  handled_at TEXT,
  handling_kind TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(user_id, advice_id)
);
CREATE INDEX IF NOT EXISTS idx_advice_attention_due
  ON advice_attention(user_id, state, delivery_count, next_eligible_at, created_at);
CREATE TABLE IF NOT EXISTS advice_attention_attempts (
  claim_token TEXT PRIMARY KEY,
  advice_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  delivery_number INTEGER NOT NULL CHECK (delivery_number BETWEEN 1 AND 2),
  state TEXT NOT NULL CHECK (state IN ('claimed','delivered','failed','superseded','cancelled')),
  claimed_at TEXT NOT NULL,
  lease_expires_at TEXT NOT NULL,
  completed_at TEXT,
  failure_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_attention_attempts_advice
  ON advice_attention_attempts(user_id, advice_id, claimed_at DESC);
"""


ERROR_FEEDBACK_KINDS = frozenset(
    {"fact_error", "prediction_error", "timing_error"}
)
RELEVANCE_FEEDBACK_KINDS = frozenset({"irrelevant"})
ERROR_BUDGETS: dict[AdviceLevel, tuple[timedelta, int]] = {
    AdviceLevel.L2: (timedelta(days=7), 2),
    AdviceLevel.L3: (timedelta(days=30), 1),
    AdviceLevel.L4: (timedelta(days=30), 0),
}
SPEAKING_FREEZE_DURATION = timedelta(days=7)
TOPIC_SUPPRESSION_DURATION = timedelta(days=30)
ANALYSIS_JOB_LEASE = timedelta(minutes=10)
ADVICE_LOCALIZATION_LEASE = timedelta(minutes=10)
ANALYSIS_DISCOVER_MAX_AGE = timedelta(hours=6)
ANALYSIS_HIGH_PRIORITY_DISCOVER_MAX_AGE = timedelta(hours=24)
ANALYSIS_BACKFILL_HARD_LIMIT = 100
ANALYSIS_JOB_RESERVED_MODEL_CALLS = 2
ANALYSIS_PRIORITY_USER_REQUEST = 80
ANALYSIS_PRIORITY_URGENT = 100
ATTENTION_REMINDER_INTERVAL = timedelta(hours=1)
ATTENTION_CLAIM_LEASE = timedelta(minutes=15)
AUTO_IRRELEVANT_WEIGHT = 0.25
AUTH_LOGIN_FAILURE_LIMIT = 5
AUTH_LOGIN_FAILURE_WINDOW = timedelta(minutes=15)
AUTH_LOGIN_LOCK_DURATION = timedelta(minutes=15)
AUTH_LAST_USED_WRITE_INTERVAL = timedelta(minutes=5)
_HIGH_VALUE_BACKFILL_PATTERN = re.compile(
    r"\u98ce\u9669|\u8b66\u544a|\u7d27\u6025|\u622a\u6b62|\u5230\u671f|\u5931\u8d25|\u5f02\u5e38|\u95ee\u9898|\u9ebb\u70e6|\u56f0\u96be|\u62c5\u5fc3|\u5a01\u80c1|\u673a\u4f1a|"
    r"\b(?:risk|warning|urgent|deadline|failed|failure|error|blocked|problem|threat|opportunity)\b",
    re.IGNORECASE,
)
_URGENT_ANALYSIS_PATTERN = re.compile(
    r"\u7d27\u6025|\u9a6c\u4e0a|\u7acb\u5373|\u5371\u9669|\u80f8\u75db|\u547c\u5438\u56f0\u96be|\u4e25\u91cd\u51fa\u8840|"
    r"\u8d26[\u53f7\u6237].{0,8}(?:\u88ab\u76d7|\u51bb\u7ed3|\u9501\u5b9a)|\u622a\u6b62|\u5230\u671f|\u903e\u671f|"
    r"\b(?:urgent|emergency|immediately|account (?:locked|suspended)|"
    r"suspicious (?:sign-?in|login)|overdue|deadline)\b",
    re.IGNORECASE,
)
_EXPLICIT_QUESTION_PATTERN = re.compile(
    r"[?\uff1f]|\u600e\u4e48(?:\u529e|\u505a)|\u5982\u4f55|\u4e3a\u4ec0\u4e48|\u8be5\u4e0d\u8be5|"
    r"\u80fd\u4e0d\u80fd|\u662f\u5426|\u8bf7\u95ee|\u5e2e\u6211|"
    r"(?:\u6709\u4ec0\u4e48|\u9700\u8981|\u7ed9\u6211).{0,6}\u5efa\u8bae|"
    r"(?:\u600e\u4e48|\u5982\u4f55|\u5e2e\u6211).{0,6}\u89e3\u51b3|"
    r"\b(?:what should i|how (?:do|can|should) i|can you help|please help|"
    r"need (?:help|advice)|any advice)\b",
    re.IGNORECASE,
)
_OWNER_REQUEST_EVENT_TYPES = frozenset(
    {"thought.note", "shared.text", "speech.transcript", "ime.text_committed"}
)
_EXPLICIT_OWNER_REQUEST_EVENT_TYPES = frozenset({"thought.note", "shared.text"})
_EXPLICIT_OWNER_REQUEST_CONTEXTS = frozenset(
    {"owner_self_report", "shared_text", "shared_image", "conversation_import"}
)
_EXPLICIT_OWNER_REQUEST_SOURCES = frozenset(
    {"android.share", "android.thought", "android.note", "android.conversation_import"}
)


class AdviceConflict(RuntimeError):
    """A nonterminal recommendation already owns this evidence."""


class AttentionConflict(RuntimeError):
    """An attention acknowledgement no longer owns the active delivery lease."""


class PreferenceRevisionConflict(ValueError):
    """The submitted revision no longer matches the stored preference."""

    def __init__(self, message: str, *, current: "AdvicePreference") -> None:
        super().__init__(message)
        self.current = current


class PreferenceLimitExceeded(RuntimeError):
    """Publishing was blocked by the user's advice-preference frequency gate."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class AnalysisJob:
    event_id: UUID
    user_id: str
    job_kind: str
    goal_id: UUID | None
    cloud_approved: bool
    raw_cloud_approved: bool
    priority: int
    status: str
    attempts: int
    next_attempt_at: datetime
    last_reason: str | None
    last_failure_reason: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class AnalysisJobClaim:
    job: AnalysisJob
    event: Event
    reservation_id: int | None = None


@dataclass(frozen=True)
class AdviceLocalizationClaim:
    user_id: str
    advice_id: UUID
    locale: str
    source_hash: str
    attempts: int
    lease_token: str
    advice: AdviceRecord


@dataclass(frozen=True)
class GlobalModelBudgetDecision:
    allowed: bool
    retry_after: int = 0
    retry_at: datetime | None = None
    reason_code: str | None = None


@dataclass(frozen=True)
class DirectModelAdmissionDecision:
    allowed: bool
    lease_id: str | None = None
    retry_after: int = 0
    retry_at: datetime | None = None
    reason_code: str | None = None


@dataclass(frozen=True)
class AdviceAttentionClaim:
    claim_token: UUID
    lease_expires_at: datetime
    delivery_number: int
    advice: AdviceRecord


@dataclass(frozen=True)
class IssuedAuthSession:
    access_token: str
    principal: AuthPrincipal


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _environment_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _advice_localization_daily_limit() -> int:
    # Historical display translation must leave the tenant's ordinary model
    # budget available for live proactive reasoning and direct questions.
    return _environment_int(
        "MOUCHEN_ADVICE_LOCALIZATION_CALLS_PER_USER_PER_DAY",
        6,
        minimum=1,
        maximum=1_000,
    )


def _as_utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _parse_utc(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value))


_ACCOUNT_REFERENCE_COLUMNS = frozenset(
    {"user_id", "consumed_by_user_id", "target_user_id"}
)
_ACCOUNT_EXPORT_SECRET_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "password",
        "password_hash",
        "token_hash",
    }
)


def _user_id_hash(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _retired_user_marker(user_id: str) -> str:
    return f"retired:{_user_id_hash(user_id)}"


def _quote_sqlite_identifier(value: str) -> str:
    """Quote a SQLite identifier obtained from sqlite_master/PRAGMA."""

    return '"' + str(value).replace('"', '""') + '"'


def _scrub_export_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _scrub_export_json(item)
            for key, item in value.items()
            if str(key).casefold() not in _ACCOUNT_EXPORT_SECRET_KEYS
            and not str(key).casefold().endswith("_hash")
            and "token" not in str(key).casefold()
        }
    if isinstance(value, list):
        return [_scrub_export_json(item) for item in value]
    return value


def _account_export_row(row: sqlite3.Row) -> dict[str, Any]:
    exported: dict[str, Any] = {}
    for column in row.keys():
        normalized = str(column).casefold()
        if (
            normalized in _ACCOUNT_EXPORT_SECRET_KEYS
            or normalized.endswith("_hash")
            or "token" in normalized
        ):
            continue
        value = row[column]
        if normalized.endswith("_json") and isinstance(value, str):
            try:
                value = _scrub_export_json(json.loads(value))
            except (TypeError, json.JSONDecodeError):
                pass
        exported[column] = value
    return exported


def _topic_component(value: Any, *, limit: int = 96) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    normalized = re.sub(r"\b\d{1,4}[-/:]\d{1,2}(?:[-/:]\d{1,4})?\b", " ", normalized)
    normalized = re.sub(r"\b\d+(?:\.\d+)?\b", " ", normalized)
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "-", normalized, flags=re.UNICODE)
    return normalized.strip("-_")[:limit] or "general"


_EMAIL_TOPIC = re.compile(r"\b(?:e-?mail|mailbox|inbox)\b|邮件|邮箱|收件箱", re.IGNORECASE)
_BACKLOG_TOPIC = re.compile(
    r"\b(?:backlog|unread|pending|queue|pile(?:d)?\s+up)\b|积压|未读|待处理|堆积",
    re.IGNORECASE,
)
_ERROR_TOPIC = re.compile(
    r"\b(?:error|failure|failed|exception|fault)\b|错误|失败|异常|故障",
    re.IGNORECASE,
)
_EVIDENCE_TOPIC = re.compile(
    r"\b(?:evidence|error\s*code|logs?|trace|screenshot|record|preserve|save)\b|"
    r"证据|错误码|日志|截图|记录|保留|保存",
    re.IGNORECASE,
)
_DEADLINE_TOPIC = re.compile(
    r"\b(?:deadline|due\s+date|expiry|expiration)\b|截止|到期|期限",
    re.IGNORECASE,
)
_VERIFY_TOPIC = re.compile(
    r"\b(?:confirm|verify|check|validate)\b|确认|核实|核验|查明",
    re.IGNORECASE,
)


def _semantic_template_family(advice: AdviceRecord | Any) -> str | None:
    """Normalize the generic templates observed in repeat-quality audits."""

    issue_subject = str(getattr(advice, "issue_subject", "") or "").strip()
    # A concrete model subject remains the primary identity. Only fall back to
    # the generic action template for legacy candidates that have no subject;
    # otherwise two independent runtime failures could be collapsed merely
    # because both correctly recommend preserving logs.
    inspected = unicodedata.normalize(
        "NFKC",
        issue_subject
        or " ".join(
            str(getattr(advice, name, "") or "")
            for name in ("action", "first_step")
        ),
    ).casefold()
    if _EMAIL_TOPIC.search(inspected) and _BACKLOG_TOPIC.search(inspected):
        return "email-backlog"
    if _ERROR_TOPIC.search(inspected) and _EVIDENCE_TOPIC.search(inspected):
        return "error-evidence"
    if _DEADLINE_TOPIC.search(inspected) and _VERIFY_TOPIC.search(inspected):
        return "confirm-deadline"
    return None


def _canonical_topic_key(advice: AdviceRecord | Any) -> str:
    semantic_family = _semantic_template_family(advice)
    if semantic_family:
        evidence = list(getattr(advice, "evidence", ()) or ())
        source = evidence[0].source if evidence else getattr(advice, "domain", "general")
        domain = _topic_component(getattr(advice, "domain", "general"), limit=48)
        stable_source = _topic_component(source, limit=80)
        return f"semantic-template:{domain}:{stable_source}:{semantic_family}"[:240]
    explicit = str(getattr(advice, "topic_key", "") or "").strip()
    if explicit:
        return explicit[:240]
    dedupe_key = str(advice.dedupe_key).strip()
    # Existing deterministic detectors already use a stable goal/topic key.
    if dedupe_key.startswith(("time-drift:", "commitment-slip:", "external-threat:")):
        return dedupe_key[:240]
    match = re.fullmatch(r"problem:([^:]+):[0-9a-f]{8,64}", dedupe_key, re.IGNORECASE)
    source = advice.evidence[0].source if advice.evidence else advice.domain
    action_signature = hashlib.sha256(
        _topic_component(advice.action, limit=400).encode("utf-8")
    ).hexdigest()[:20]
    if match:
        raw = (
            f"problem|{advice.goal_id}|{_topic_component(source)}|"
            f"{_topic_component(match.group(1))}|{action_signature}"
        )
        return f"problem:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"
    if re.fullmatch(r"model-problem:[0-9a-f]{8,64}", dedupe_key, re.IGNORECASE):
        raw = (
            f"model-problem|{advice.goal_id}|{_topic_component(source)}|"
            f"{action_signature}"
        )
        return f"model-problem:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"
    return dedupe_key[:240]


def _feedback_semantic_key(kind: str, note: str | None) -> str:
    normalized_kind = str(kind).strip().casefold()
    if normalized_kind != "guidance":
        # For fixed button actions, notes are ancillary. Two devices reporting
        # the same action still represent one user decision.
        return normalized_kind
    normalized_note = unicodedata.normalize("NFKC", str(note or ""))
    normalized_note = re.sub(r"\s+", " ", normalized_note).strip().casefold()
    digest = hashlib.sha256(normalized_note.encode("utf-8")).hexdigest()[:24]
    return f"guidance:{digest}"


def _analysis_reason(value: str) -> str:
    """Store a bounded reason code, never an exception or provider response."""

    normalized = "".join(
        character if character.isalnum() or character in {"_", "-"} else "_"
        for character in str(value or "analysis_unknown").strip().lower()
    )
    # Evaluation holds may carry several internal gate codes. Keep the field
    # bounded, but retain enough room to make every suppressed candidate
    # auditable instead of collapsing it to the opaque "evaluation_hold".
    return normalized[:240] or "analysis_unknown"


def _analysis_priority(event: Event, job_kind: str) -> int:
    """Derive bounded queue priority from explicit, inspectable event signals."""

    if job_kind == "refine_deterministic":
        return ANALYSIS_PRIORITY_URGENT
    facts = event.facts
    if any(_fact_flag(facts.get(name)) for name in ("emergency", "urgent", "high_priority")):
        return ANALYSIS_PRIORITY_URGENT
    try:
        urgency = float(facts.get("urgency", 0))
    except (TypeError, ValueError):
        urgency = 0
    if urgency >= 0.8:
        return ANALYSIS_PRIORITY_URGENT

    from .domain.problem_signals import event_text

    text = event_text(event)
    if text and _URGENT_ANALYSIS_PATTERN.search(text):
        return ANALYSIS_PRIORITY_URGENT
    if _fact_flag(facts.get("user_question")):
        return ANALYSIS_PRIORITY_USER_REQUEST
    if (
        event.type in _OWNER_REQUEST_EVENT_TYPES
        and text
        and _EXPLICIT_QUESTION_PATTERN.search(text)
    ):
        return ANALYSIS_PRIORITY_USER_REQUEST
    request_context = str(facts.get("context", "")).strip().casefold()
    request_source = event.source.strip().casefold()
    is_explicit_owner_entry = (
        event.type in _EXPLICIT_OWNER_REQUEST_EVENT_TYPES
        or request_context in _EXPLICIT_OWNER_REQUEST_CONTEXTS
        or request_source in _EXPLICIT_OWNER_REQUEST_SOURCES
    )
    if _fact_flag(facts.get("analysis_requested")) and is_explicit_owner_entry:
        return ANALYSIS_PRIORITY_USER_REQUEST
    return 0


def _fact_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return False


def _analysis_discover_max_ages() -> tuple[timedelta, timedelta]:
    def configured_seconds(name: str, default: timedelta) -> int:
        try:
            value = int(os.getenv(name, str(int(default.total_seconds()))))
        except ValueError:
            value = int(default.total_seconds())
        return max(300, min(7 * 24 * 60 * 60, value))

    regular = timedelta(
        seconds=configured_seconds(
            "MOUCHEN_ANALYSIS_DISCOVER_MAX_AGE_SECONDS",
            ANALYSIS_DISCOVER_MAX_AGE,
        )
    )
    high_priority = timedelta(
        seconds=configured_seconds(
            "MOUCHEN_ANALYSIS_HIGH_PRIORITY_MAX_AGE_SECONDS",
            ANALYSIS_HIGH_PRIORITY_DISCOVER_MAX_AGE,
        )
    )
    return regular, max(regular, high_priority)


def _analysis_job_from_row(row: sqlite3.Row) -> AnalysisJob:
    completed = row["completed_at"]
    return AnalysisJob(
        event_id=UUID(row["event_id"]),
        user_id=row["user_id"],
        job_kind=row["job_kind"],
        goal_id=UUID(row["goal_id"]) if row["goal_id"] else None,
        cloud_approved=bool(row["cloud_approved"]),
        raw_cloud_approved=bool(row["raw_cloud_approved"]),
        priority=int(row["priority"]),
        status=row["status"],
        attempts=int(row["attempts"]),
        next_attempt_at=_parse_utc(row["next_attempt_at"]),
        last_reason=row["last_reason"],
        last_failure_reason=row["last_failure_reason"],
        created_at=_parse_utc(row["created_at"]),
        updated_at=_parse_utc(row["updated_at"]),
        completed_at=_parse_utc(completed) if completed else None,
    )


class Repository:
    def __init__(self, path: str | Path | None = None) -> None:
        backend_root = Path(__file__).resolve().parents[1]
        configured = path or os.getenv("MOUCHEN_DB_PATH") or backend_root / "data" / "mouchen.db"
        self.path = Path(configured)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale_account_export_files()
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.create_function(
            "mouchen_user_id_hash", 1, _user_id_hash, deterministic=True
        )
        self.auth_migration_blocked = False
        self.auth_migration_reason: str | None = None
        with self._lock:
            self._connection.executescript(SCHEMA)
            invite_columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(auth_invites)"
                ).fetchall()
            }
            if "consumed_by_user_hash" not in invite_columns:
                self._connection.execute(
                    "ALTER TABLE auth_invites ADD COLUMN consumed_by_user_hash TEXT"
                )
            self._migrate_tenant_identity_schema_locked()
            self._prepare_auth_schema_locked()
            event_columns = {
                row["name"] for row in self._connection.execute("PRAGMA table_info(events)").fetchall()
            }
            if "evidence_ref" not in event_columns:
                self._connection.execute("ALTER TABLE events ADD COLUMN evidence_ref TEXT")
            self._connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_events_evidence_ref
                ON events(user_id, evidence_ref) WHERE evidence_ref IS NOT NULL"""
            )
            # Reconcile legacy counters without rewriting already-correct rows.
            # The release guard treats this durable quota state as protected
            # data, so a restart must not churn updated_at when no bytes changed.
            self._connection.execute(
                """INSERT INTO tenant_storage_usage(user_id,event_bytes,updated_at)
                SELECT user_id,
                       COALESCE(SUM(length(CAST(payload_json AS BLOB))),0),
                       ?
                FROM events GROUP BY user_id
                ON CONFLICT(user_id) DO UPDATE SET
                  event_bytes=excluded.event_bytes,
                  updated_at=excluded.updated_at
                WHERE tenant_storage_usage.event_bytes <> excluded.event_bytes""",
                (_now(),),
            )
            self._connection.execute(
                """DELETE FROM tenant_storage_usage
                WHERE NOT EXISTS(
                  SELECT 1 FROM events
                  WHERE events.user_id=tenant_storage_usage.user_id
                )"""
            )
            advice_columns = {
                row["name"] for row in self._connection.execute("PRAGMA table_info(advice)").fetchall()
            }
            if "evidence_ref" not in advice_columns:
                self._connection.execute("ALTER TABLE advice ADD COLUMN evidence_ref TEXT")
            if "topic_key" not in advice_columns:
                self._connection.execute("ALTER TABLE advice ADD COLUMN topic_key TEXT")
            feedback_columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(feedback)").fetchall()
            }
            if "origin" not in feedback_columns:
                self._connection.execute(
                    "ALTER TABLE feedback ADD COLUMN origin TEXT NOT NULL DEFAULT 'user'"
                )
            if "signal_weight" not in feedback_columns:
                self._connection.execute(
                    "ALTER TABLE feedback ADD COLUMN signal_weight REAL NOT NULL DEFAULT 1.0"
                )
            if "feedback_id" not in feedback_columns:
                self._connection.execute(
                    "ALTER TABLE feedback ADD COLUMN feedback_id TEXT"
                )
            if "semantic_key" not in feedback_columns:
                self._connection.execute(
                    "ALTER TABLE feedback ADD COLUMN semantic_key TEXT"
                )
            self._backfill_feedback_semantic_keys_locked()
            self._connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_idempotency
                ON feedback(user_id,feedback_id) WHERE feedback_id IS NOT NULL"""
            )
            self._connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_semantic
                ON feedback(user_id,advice_id,semantic_key)
                WHERE semantic_key IS NOT NULL"""
            )
            attention_columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(advice_attention)"
                ).fetchall()
            }
            if "handled_at" not in attention_columns:
                self._connection.execute(
                    "ALTER TABLE advice_attention ADD COLUMN handled_at TEXT"
                )
            if "handling_kind" not in attention_columns:
                self._connection.execute(
                    "ALTER TABLE advice_attention ADD COLUMN handling_kind TEXT"
                )
            if "goal_id" not in advice_columns:
                self._connection.execute("ALTER TABLE advice ADD COLUMN goal_id TEXT")
            if "published_at" not in advice_columns:
                self._connection.execute("ALTER TABLE advice ADD COLUMN published_at TEXT")
            # Idempotent backfill for pre-preference databases: anything that was
            # genuinely published once counts against rolling windows from its
            # creation moment; provisional rows never gain a published_at.
            self._connection.execute(
                """UPDATE advice SET published_at=created_at
                WHERE published_at IS NULL
                  AND status IN ('active','adopted','dismissed','withdrawn','verified')"""
            )
            self._connection.execute(
                """UPDATE advice SET goal_id=json_extract(
                     CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END,
                     '$.goal_id')
                WHERE goal_id IS NULL"""
            )
            self._connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_advice_user_published
                ON advice(user_id, published_at)"""
            )
            self._connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_advice_user_goal_published
                ON advice(user_id, goal_id, published_at)"""
            )
            analysis_columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(analysis_jobs)").fetchall()
            }
            for name, definition in (
                ("job_kind", "TEXT NOT NULL DEFAULT 'discover'"),
                ("goal_id", "TEXT"),
                ("cloud_approved", "INTEGER NOT NULL DEFAULT 0"),
                ("raw_cloud_approved", "INTEGER NOT NULL DEFAULT 0"),
                ("priority", "INTEGER NOT NULL DEFAULT 0"),
                ("last_failure_reason", "TEXT"),
            ):
                if name not in analysis_columns:
                    self._connection.execute(
                        f"ALTER TABLE analysis_jobs ADD COLUMN {name} {definition}"
                    )
            self._connection.execute("DROP INDEX IF EXISTS idx_analysis_jobs_due")
            self._connection.execute(
                """CREATE INDEX idx_analysis_jobs_due
                ON analysis_jobs(
                  user_id, cloud_approved, status, priority DESC,
                  next_attempt_at, created_at
                )"""
            )
            self._backfill_analysis_priorities_locked()
            # Earlier private-alpha builds only enforced one error per advice
            # in Python.  Preserve the first recorded judgment, remove any
            # duplicates that may already exist, then make the invariant
            # atomic for every process sharing this SQLite database.
            self._connection.execute(
                """DELETE FROM error_ledger
                WHERE id NOT IN (
                  SELECT MIN(id) FROM error_ledger GROUP BY user_id,advice_id
                )"""
            )
            self._connection.execute("DROP INDEX IF EXISTS idx_error_ledger_advice")
            self._connection.execute(
                """CREATE UNIQUE INDEX idx_error_ledger_advice
                ON error_ledger(user_id,advice_id)"""
            )
            # A second process may open the same database while a review is
            # still running. Only age-expired provisional rows are abandoned;
            # recent rows remain invisible and continue to own their evidence.
            provisional_cutoff = (
                datetime.now(timezone.utc) - ANALYSIS_JOB_LEASE
            ).isoformat()
            self._connection.execute(
                "DELETE FROM advice WHERE status='provisional' AND created_at<=?",
                (provisional_cutoff,),
            )
            self._backfill_advice_evidence_refs_locked()
            self._resolve_advice_evidence_duplicates_locked()
            self._backfill_advice_topic_keys_locked()
            self._resolve_advice_topic_duplicates_locked()
            self._migrate_irrelevant_feedback_locked()
            self._connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_advice_user_evidence_nonterminal
                ON advice(user_id, evidence_ref)
                WHERE evidence_ref IS NOT NULL
                  AND status IN ('provisional','active','adopted')"""
            )
            self._connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_advice_topic
                ON advice(user_id, topic_key, status)"""
            )
            self._connection.execute(
                "DROP INDEX IF EXISTS idx_advice_user_topic_nonterminal"
            )
            self._connection.execute(
                """CREATE UNIQUE INDEX idx_advice_user_topic_nonterminal
                ON advice(user_id, topic_key)
                WHERE topic_key IS NOT NULL
                  AND status IN ('provisional','active')"""
            )
            self._ensure_attention_rows_locked(datetime.now(timezone.utc))
            self._install_retired_user_guards_locked()
            self._connection.commit()

    def _install_retired_user_guards_locked(self) -> None:
        """Guard every Repository write without burdening raw utility connections."""

        guarded: list[tuple[str, str]] = [("users", "id")]
        for table, columns in self._account_reference_columns_locked():
            if table == "retired_user_ids":
                continue
            guarded.extend((table, column) for column in columns)
        for table, column in guarded:
            identity = hashlib.sha256(f"{table}\0{column}".encode("utf-8")).hexdigest()[:20]
            quoted_table = _quote_sqlite_identifier(table)
            quoted_column = _quote_sqlite_identifier(column)
            for operation, reference in (("insert", "NEW"), ("update", "NEW")):
                trigger = _quote_sqlite_identifier(
                    f"guard_retired_user_{operation}_{identity}"
                )
                # An earlier development build created persistent UDF-backed
                # triggers. Remove those before installing connection-local
                # TEMP triggers so backup/merge SQLite connections stay usable.
                self._connection.execute(f"DROP TRIGGER IF EXISTS main.{trigger}")
                update_of = f" OF {quoted_column}" if operation == "update" else ""
                self._connection.execute(
                    f"""CREATE TEMP TRIGGER IF NOT EXISTS {trigger}
                    BEFORE {operation.upper()}{update_of} ON {quoted_table}
                    WHEN EXISTS (
                      SELECT 1 FROM retired_user_ids
                      WHERE user_id_hash=mouchen_user_id_hash(
                        {reference}.{quoted_column}
                      )
                    )
                    BEGIN
                      SELECT RAISE(ABORT, 'account is retired');
                    END"""
                )

    def _migrate_tenant_identity_schema_locked(self) -> None:
        """Replace legacy globally keyed business objects with tenant keys."""

        expected_primary_keys = {
            "goals": ("user_id", "id"),
            "events": ("user_id", "id"),
            "advice": ("user_id", "id"),
            "relevance_ledger": ("user_id", "advice_id", "signal_kind", "origin"),
            "outcomes": ("user_id", "advice_id"),
            "action_drafts": ("user_id", "id"),
            "analysis_jobs": ("user_id", "event_id"),
            "advice_attention": ("user_id", "advice_id"),
        }
        rebuild: list[str] = []
        for table, expected in expected_primary_keys.items():
            primary_key = tuple(
                row["name"]
                for row in sorted(
                    (
                        row
                        for row in self._connection.execute(
                            f"PRAGMA table_info({table})"
                        ).fetchall()
                        if int(row["pk"])
                    ),
                    key=lambda row: int(row["pk"]),
                )
            )
            if primary_key != expected:
                rebuild.append(table)
        if not rebuild:
            return

        table_definitions: dict[str, str] = {}
        for table in rebuild:
            match = re.search(
                rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\(.*?\n\);",
                SCHEMA,
                re.DOTALL,
            )
            if match is None:
                raise RuntimeError("tenant schema migration is unavailable")
            table_definitions[table] = match.group(0).replace(" IF NOT EXISTS", "", 1)

        self._connection.commit()
        self._connection.execute("PRAGMA foreign_keys=OFF")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            old_counts = {
                table: int(
                    self._connection.execute(
                        f"SELECT COUNT(*) AS count FROM {table}"
                    ).fetchone()["count"]
                )
                for table in rebuild
            }
            for table in rebuild:
                self._connection.execute(
                    f"ALTER TABLE {table} RENAME TO __legacy_tenant_{table}"
                )
            for table in rebuild:
                self._connection.execute(table_definitions[table])
                old_columns = {
                    row["name"]
                    for row in self._connection.execute(
                        f"PRAGMA table_info(__legacy_tenant_{table})"
                    ).fetchall()
                }
                new_columns = [
                    row["name"]
                    for row in self._connection.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                    if row["name"] in old_columns
                ]
                quoted = ",".join(f'"{column}"' for column in new_columns)
                self._connection.execute(
                    f"INSERT INTO {table}({quoted}) SELECT {quoted} "
                    f"FROM __legacy_tenant_{table}"
                )
                new_count = int(
                    self._connection.execute(
                        f"SELECT COUNT(*) AS count FROM {table}"
                    ).fetchone()["count"]
                )
                if new_count != old_counts[table]:
                    raise RuntimeError("tenant schema migration row count mismatch")
            for table in reversed(rebuild):
                self._connection.execute(f"DROP TABLE __legacy_tenant_{table}")
            foreign_key_errors = self._connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_errors:
                raise RuntimeError("tenant schema migration foreign key check failed")
            integrity = self._connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError("tenant schema migration integrity check failed")
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        finally:
            self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(SCHEMA)

    def _prepare_auth_schema_locked(self) -> None:
        """Upgrade legacy user rows without ever guessing tenant ownership.

        A database containing multiple historical user ids needs an explicit
        username map before commercial authentication is allowed. The service
        can still open for backup/repair, but middleware fails every protected
        request with 503 until the ambiguity is resolved.
        """

        user_columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(users)").fetchall()
        }
        for name, definition in (
            ("username_normalized", "TEXT"),
            ("username_display", "TEXT"),
            ("password_hash", "TEXT"),
            ("is_active", "INTEGER NOT NULL DEFAULT 1"),
            ("locale", "TEXT NOT NULL DEFAULT 'zh-CN'"),
            ("locale_updated_at", "TEXT"),
        ):
            if name not in user_columns:
                self._connection.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
        self._connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_normalized
            ON users(username_normalized) WHERE username_normalized IS NOT NULL"""
        )

        tenant_tables = (
            "goals",
            "events",
            "charters",
            "advice",
            "feedback",
            "outcomes",
            "trust_accounts",
            "cloud_slices",
            "analysis_jobs",
            "devices",
        )
        existing_tables = {
            row["name"]
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        selects = ["SELECT id AS user_id FROM users"] + [
            f"SELECT user_id FROM {table}"
            for table in tenant_tables
            if table in existing_tables
        ]
        tenant_rows = self._connection.execute(
            "SELECT DISTINCT user_id FROM (" + " UNION ".join(selects) + ") "
            "WHERE user_id IS NOT NULL AND TRIM(user_id)<>''"
        ).fetchall()
        tenant_ids = {row["user_id"] for row in tenant_rows}
        for tenant_id in tenant_ids:
            self._connection.execute(
                "INSERT OR IGNORE INTO users(id,created_at) VALUES(?,?)",
                (tenant_id, _now()),
            )

        mapped_rows = self._connection.execute(
            "SELECT id FROM users WHERE username_normalized IS NOT NULL"
        ).fetchall()
        mapped_ids = {row["id"] for row in mapped_rows}
        unmapped_ids = tenant_ids - mapped_ids
        configured_mapping = self._legacy_username_mapping()
        for user_id, username in configured_mapping.items():
            if user_id not in unmapped_ids:
                continue
            display, normalized = normalize_username(username)
            self._connection.execute(
                """UPDATE users SET username_display=?,username_normalized=?
                WHERE id=? AND username_normalized IS NULL""",
                (display, normalized, user_id),
            )
            unmapped_ids.discard(user_id)

        # Never guess which login owns pre-account business data. Even one
        # unmapped legacy tenant must be bound explicitly by the operator;
        # otherwise a bootstrap registration could create a fresh UUID and
        # strand the owner's existing goals, events and advice.
        if unmapped_ids:
            self.auth_migration_blocked = True
            self.auth_migration_reason = "legacy tenants require an explicit username map"
        configured_single = os.getenv("MOUCHEN_SINGLE_USER_ID", "").strip()
        legacy_auth_enabled = os.getenv(
            "MOUCHEN_LEGACY_AUTH_ENABLED", ""
        ).strip().casefold() in {"1", "true", "yes", "on"}
        if (
            legacy_auth_enabled
            and len(tenant_ids) == 1
            and configured_single
            and configured_single not in tenant_ids
        ):
            self.auth_migration_blocked = True
            self.auth_migration_reason = "configured single user does not match legacy tenant"

    @staticmethod
    def _legacy_username_mapping() -> dict[str, str]:
        raw = os.getenv("MOUCHEN_LEGACY_USERNAMES_JSON", "").strip()
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            str(user_id): str(username)
            for user_id, username in payload.items()
            if str(user_id).strip() and str(username).strip()
        }

    def _backfill_analysis_priorities_locked(self) -> None:
        rows = self._connection.execute(
            """SELECT j.user_id,j.event_id,j.job_kind,j.priority,e.payload_json
            FROM analysis_jobs j JOIN events e
              ON e.id=j.event_id AND e.user_id=j.user_id
            WHERE j.status IN ('pending','retry')"""
        ).fetchall()
        for row in rows:
            try:
                priority = _analysis_priority(
                    Event.model_validate_json(row["payload_json"]),
                    row["job_kind"],
                )
            except ValueError:
                continue
            if priority == int(row["priority"]):
                continue
            self._connection.execute(
                "UPDATE analysis_jobs SET priority=? WHERE user_id=? AND event_id=?",
                (priority, row["user_id"], row["event_id"]),
            )

    def _backfill_advice_evidence_refs_locked(self) -> None:
        rows = self._connection.execute(
            "SELECT id, user_id, payload_json FROM advice WHERE evidence_ref IS NULL"
        ).fetchall()
        for row in rows:
            try:
                advice = AdviceRecord.model_validate_json(row["payload_json"])
                evidence = advice.evidence[0]
            except (ValueError, IndexError):
                continue
            event_row = self._connection.execute(
                "SELECT evidence_ref FROM events WHERE user_id=? AND id=?",
                (row["user_id"], str(evidence.event_id)),
            ).fetchone()
            evidence_ref = (
                event_row["evidence_ref"]
                if event_row is not None and event_row["evidence_ref"]
                else f"event:{evidence.event_id}"
            )
            self._connection.execute(
                "UPDATE advice SET evidence_ref=? WHERE user_id=? AND id=?",
                (evidence_ref, row["user_id"], row["id"]),
            )

    def _resolve_advice_evidence_duplicates_locked(self) -> None:
        duplicates = self._connection.execute(
            """SELECT user_id, evidence_ref
            FROM advice
            WHERE evidence_ref IS NOT NULL
              AND status IN ('provisional','active','adopted')
            GROUP BY user_id, evidence_ref HAVING COUNT(*) > 1"""
        ).fetchall()
        for duplicate in duplicates:
            rows = self._connection.execute(
                """SELECT id, payload_json FROM advice
                WHERE user_id=? AND evidence_ref=?
                  AND status IN ('provisional','active','adopted')
                ORDER BY created_at, id""",
                (duplicate["user_id"], duplicate["evidence_ref"]),
            ).fetchall()
            for row in rows[1:]:
                try:
                    advice = AdviceRecord.model_validate_json(row["payload_json"])
                    advice = advice.model_copy(update={"status": AdviceStatus.WITHDRAWN})
                    payload = advice.model_dump_json()
                except ValueError:
                    payload = row["payload_json"]
                self._connection.execute(
                    """UPDATE advice SET status='withdrawn', payload_json=?
                    WHERE user_id=? AND id=?""",
                    (payload, duplicate["user_id"], row["id"]),
                )
                self._connection.execute(
                    """UPDATE advice_attention SET
                      state='resolved',claim_token=NULL,claim_device_id=NULL,
                      claim_expires_at=NULL,next_eligible_at=NULL,auto_close_at=NULL,
                      resolved_at=COALESCE(resolved_at,?),
                      resolved_reason='evidence_duplicate_migration',updated_at=?
                    WHERE user_id=? AND advice_id=? AND state!='resolved'""",
                    (_now(), _now(), duplicate["user_id"], row["id"]),
                )

    def _backfill_advice_topic_keys_locked(self) -> None:
        rows = self._connection.execute(
            "SELECT id,user_id,payload_json FROM advice WHERE topic_key IS NULL OR topic_key=''"
        ).fetchall()
        for row in rows:
            try:
                advice = AdviceRecord.model_validate_json(row["payload_json"])
            except ValueError:
                continue
            topic_key = _canonical_topic_key(advice)
            advice = advice.model_copy(update={"topic_key": topic_key})
            self._connection.execute(
                "UPDATE advice SET topic_key=?, payload_json=? WHERE user_id=? AND id=?",
                (topic_key, advice.model_dump_json(), row["user_id"], row["id"]),
            )

    def _resolve_advice_topic_duplicates_locked(self) -> None:
        duplicates = self._connection.execute(
            """SELECT user_id,topic_key
            FROM advice
            WHERE topic_key IS NOT NULL
              AND status IN ('provisional','active','adopted')
            GROUP BY user_id,topic_key HAVING COUNT(*) > 1"""
        ).fetchall()
        for duplicate in duplicates:
            # Preserve an adopted decision if one exists; otherwise retain the
            # earliest recommendation and withdraw later semantic duplicates.
            rows = self._connection.execute(
                """SELECT id,payload_json,status FROM advice
                WHERE user_id=? AND topic_key=?
                  AND status IN ('provisional','active','adopted')
                ORDER BY CASE WHEN status='adopted' THEN 0 ELSE 1 END,
                         created_at,id""",
                (duplicate["user_id"], duplicate["topic_key"]),
            ).fetchall()
            for row in rows[1:]:
                try:
                    advice = AdviceRecord.model_validate_json(row["payload_json"])
                    advice = advice.model_copy(update={"status": AdviceStatus.WITHDRAWN})
                    payload = advice.model_dump_json()
                except ValueError:
                    payload = row["payload_json"]
                self._connection.execute(
                    """UPDATE advice SET status='withdrawn', payload_json=?
                    WHERE user_id=? AND id=?""",
                    (payload, duplicate["user_id"], row["id"]),
                )
                self._connection.execute(
                    """UPDATE advice_attention SET
                      state='resolved',claim_token=NULL,claim_device_id=NULL,
                      claim_expires_at=NULL,next_eligible_at=NULL,auto_close_at=NULL,
                      resolved_at=COALESCE(resolved_at,?),
                      resolved_reason='semantic_duplicate_migration',updated_at=?
                    WHERE user_id=? AND advice_id=? AND state!='resolved'""",
                    (_now(), _now(), duplicate["user_id"], row["id"]),
                )

    def _backfill_feedback_semantic_keys_locked(self) -> None:
        """Index one canonical copy while retaining all historical rows."""

        rows = self._connection.execute(
            """SELECT id,user_id,advice_id,kind,note FROM feedback
            WHERE origin='user' ORDER BY id"""
        ).fetchall()
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            semantic_key = _feedback_semantic_key(row["kind"], row["note"])
            identity = (row["user_id"], row["advice_id"], semantic_key)
            if identity in seen:
                # Duplicate history remains auditable. Only the first row is
                # canonicalized so the new uniqueness guard can be installed.
                self._connection.execute(
                    "UPDATE feedback SET semantic_key=NULL WHERE id=?",
                    (row["id"],),
                )
                continue
            seen.add(identity)
            self._connection.execute(
                "UPDATE feedback SET semantic_key=? WHERE id=?",
                (semantic_key, row["id"]),
            )

    def _migrate_irrelevant_feedback_locked(self) -> None:
        """Move legacy irrelevant judgments out of the hard-error budget.

        User feedback remains intact. Only the derived error ledger/freeze is
        corrected, while a full-weight relevance signal is backfilled.
        """

        self._connection.execute(
            """INSERT INTO relevance_ledger(
              advice_id,user_id,topic_key,signal_kind,origin,weight,created_at
            )
            SELECT f.advice_id,f.user_id,a.topic_key,'irrelevant','user',1.0,f.created_at
            FROM feedback f JOIN advice a
              ON a.id=f.advice_id AND a.user_id=f.user_id
            WHERE f.kind='irrelevant' AND a.topic_key IS NOT NULL
            ON CONFLICT(user_id,advice_id,signal_kind,origin) DO NOTHING"""
        )
        self._connection.execute(
            "DELETE FROM error_ledger WHERE error_kind='irrelevant'"
        )
        freezes = self._connection.execute(
            "SELECT * FROM speaking_freezes"
        ).fetchall()
        for freeze in freezes:
            remaining = self._connection.execute(
                """SELECT COUNT(*) AS count FROM error_ledger
                WHERE user_id=? AND effective_level=?
                  AND created_at>=? AND created_at<=?""",
                (
                    freeze["user_id"],
                    int(freeze["level"]),
                    freeze["window_started_at"],
                    freeze["frozen_at"],
                ),
            ).fetchone()
            error_count = int(remaining["count"])
            if error_count <= int(freeze["allowed_errors"]):
                self._connection.execute(
                    "DELETE FROM speaking_freezes WHERE user_id=? AND level=?",
                    (freeze["user_id"], int(freeze["level"])),
                )
            elif error_count != int(freeze["error_count"]):
                self._connection.execute(
                    """UPDATE speaking_freezes SET error_count=?
                    WHERE user_id=? AND level=?""",
                    (error_count, freeze["user_id"], int(freeze["level"])),
                )

    def _insert_attention_row_locked(self, advice: AdviceRecord) -> None:
        topic_key = _canonical_topic_key(advice)
        created_at = _as_utc(advice.created_at).isoformat()
        if advice.delivery != "immediate":
            self._connection.execute(
                """INSERT INTO advice_attention(
                  advice_id,user_id,topic_key,state,delivery_count,
                  resolved_at,resolved_reason,created_at,updated_at
                ) VALUES(?,?,?,'resolved',0,?,'brief_no_push',?,?)
                ON CONFLICT(user_id,advice_id) DO NOTHING""",
                (
                    str(advice.id),
                    advice.user_id,
                    topic_key,
                    created_at,
                    created_at,
                    created_at,
                ),
            )
            return
        self._connection.execute(
            """INSERT INTO advice_attention(
              advice_id,user_id,topic_key,state,delivery_count,created_at,updated_at
            ) VALUES(?,?,?,'pending',0,?,?)
            ON CONFLICT(user_id,advice_id) DO NOTHING""",
            (str(advice.id), advice.user_id, topic_key, created_at, created_at),
        )

    def _ensure_attention_rows_locked(self, current: datetime) -> None:
        rows = self._connection.execute(
            """SELECT a.id,a.user_id,a.topic_key,a.created_at FROM advice a
            WHERE a.status='active'
              AND NOT EXISTS (
                SELECT 1 FROM advice_attention aa
                WHERE aa.advice_id=a.id AND aa.user_id=a.user_id
              )"""
        ).fetchall()
        for row in rows:
            # Missing attention state means the advice predates the central
            # delivery protocol (or was imported). Keep it visible, but never
            # replay a backlog of old notifications after deployment.
            self._connection.execute(
                """INSERT INTO advice_attention(
                  advice_id,user_id,topic_key,state,delivery_count,
                  resolved_at,resolved_reason,created_at,updated_at
                ) VALUES(?,?,?,'resolved',0,?,'legacy_no_replay',?,?)
                ON CONFLICT(user_id,advice_id) DO NOTHING""",
                (
                    row["id"],
                    row["user_id"],
                    row["topic_key"],
                    current.isoformat(),
                    row["created_at"],
                    current.isoformat(),
                ),
            )

    def _record_relevance_signal_locked(
        self,
        advice: AdviceRecord,
        signal_kind: str,
        *,
        origin: str,
        weight: float,
        current: datetime,
    ) -> None:
        self._connection.execute(
            """INSERT INTO relevance_ledger(
              advice_id,user_id,topic_key,signal_kind,origin,weight,created_at
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(user_id,advice_id,signal_kind,origin) DO NOTHING""",
            (
                str(advice.id),
                advice.user_id,
                _canonical_topic_key(advice),
                signal_kind,
                origin,
                max(0.0, min(1.0, float(weight))),
                current.isoformat(),
            ),
        )

    def _resolve_attention_locked(
        self,
        advice_id: UUID | str,
        user_id: str,
        *,
        reason: str,
        current: datetime,
    ) -> None:
        row = self._connection.execute(
            """SELECT claim_token FROM advice_attention
            WHERE advice_id=? AND user_id=?""",
            (str(advice_id), user_id),
        ).fetchone()
        if row is None:
            return
        if row["claim_token"]:
            self._connection.execute(
                """UPDATE advice_attention_attempts SET state='cancelled',completed_at=?
                WHERE claim_token=? AND state='claimed'""",
                (current.isoformat(), row["claim_token"]),
            )
        self._connection.execute(
            """UPDATE advice_attention SET
              state='resolved',claim_token=NULL,claim_device_id=NULL,
              claim_expires_at=NULL,next_eligible_at=NULL,auto_close_at=NULL,
              resolved_at=?,resolved_reason=?,updated_at=?
            WHERE advice_id=? AND user_id=? AND state!='resolved'""",
            (
                current.isoformat(),
                reason[:120],
                current.isoformat(),
                str(advice_id),
                user_id,
            ),
        )

    def _defer_attention_locked(
        self,
        advice: AdviceRecord,
        *,
        current: datetime,
    ) -> None:
        """Honor an explicit "later" without misclassifying silence.

        A remaining permit may be claimed at the snooze time. If both
        delivery permits are already consumed, the explicit response is
        terminal and no automatic irrelevant signal is written.
        """

        row = self._connection.execute(
            """SELECT * FROM advice_attention
            WHERE advice_id=? AND user_id=?""",
            (str(advice.id), advice.user_id),
        ).fetchone()
        if row is None or row["state"] == "resolved":
            return
        if int(row["delivery_count"]) >= 2:
            self._resolve_attention_locked(
                advice.id,
                advice.user_id,
                reason="handled:deferred",
                current=current,
            )
            return
        snoozed_until = advice.snoozed_until
        if snoozed_until is None:
            self._resolve_attention_locked(
                advice.id,
                advice.user_id,
                reason="handled:deferred_expired",
                current=current,
            )
            return
        due_at = _as_utc(snoozed_until)
        state = "claimed" if row["state"] == "claimed" else "waiting"
        self._connection.execute(
            """UPDATE advice_attention SET
              state=?,next_eligible_at=?,auto_close_at=NULL,
              handled_at=?,handling_kind='later',updated_at=?
            WHERE advice_id=? AND user_id=? AND state!='resolved'""",
            (
                state,
                due_at.isoformat(),
                current.isoformat(),
                current.isoformat(),
                str(advice.id),
                advice.user_id,
            ),
        )

    def _expire_prediction_deadlines_locked(
        self,
        user_id: str,
        current: datetime,
    ) -> int:
        """Withdraw expired advice and its reminder in the caller's transaction."""

        changed = 0
        rows = self._connection.execute(
            """SELECT a.id,a.payload_json,
                      aa.next_eligible_at,aa.auto_close_at
            FROM advice a LEFT JOIN advice_attention aa
              ON aa.advice_id=a.id AND aa.user_id=a.user_id
            WHERE a.user_id=? AND a.status='active'""",
            (user_id,),
        ).fetchall()
        for row in rows:
            try:
                advice = AdviceRecord.model_validate_json(row["payload_json"])
            except ValueError:
                continue
            deadline = _as_utc(advice.prediction.deadline)
            if deadline > current:
                next_eligible_at = row["next_eligible_at"]
                auto_close_at = row["auto_close_at"]
                bounded_next = (
                    deadline.isoformat()
                    if next_eligible_at and _parse_utc(next_eligible_at) > deadline
                    else next_eligible_at
                )
                bounded_auto = (
                    deadline.isoformat()
                    if auto_close_at and _parse_utc(auto_close_at) > deadline
                    else auto_close_at
                )
                if bounded_next != next_eligible_at or bounded_auto != auto_close_at:
                    self._connection.execute(
                        """UPDATE advice_attention SET
                          next_eligible_at=?,auto_close_at=?,updated_at=?
                        WHERE user_id=? AND advice_id=?""",
                        (
                            bounded_next,
                            bounded_auto,
                            current.isoformat(),
                            user_id,
                            row["id"],
                        ),
                    )
                continue
            withdrawn = advice.model_copy(update={"status": AdviceStatus.WITHDRAWN})
            cursor = self._connection.execute(
                """UPDATE advice SET status='withdrawn',payload_json=?
                WHERE id=? AND user_id=? AND status='active'""",
                (withdrawn.model_dump_json(), str(advice.id), user_id),
            )
            if cursor.rowcount != 1:
                continue
            self._resolve_attention_locked(
                advice.id,
                user_id,
                reason="prediction_deadline_expired",
                current=current,
            )
            changed += 1
        return changed

    def _sweep_attention_locked(self, user_id: str, current: datetime) -> int:
        changed = self._expire_prediction_deadlines_locked(user_id, current)
        # An expired permit is never returned to the pool. The next claim, if
        # one remains, must use the next global delivery number.
        expired = self._connection.execute(
            """SELECT advice_id,claim_token,delivery_count
            FROM advice_attention
            WHERE user_id=? AND state='claimed' AND claim_expires_at<=?""",
            (user_id, current.isoformat()),
        ).fetchall()
        for row in expired:
            self._connection.execute(
                """UPDATE advice_attention_attempts SET state='superseded',completed_at=?
                WHERE claim_token=? AND state='claimed'""",
                (current.isoformat(), row["claim_token"]),
            )
            self._connection.execute(
                """UPDATE advice_attention SET state='waiting',claim_token=NULL,
                  claim_device_id=NULL,claim_expires_at=NULL,updated_at=?
                WHERE user_id=? AND advice_id=? AND claim_token=?""",
                (current.isoformat(), user_id, row["advice_id"], row["claim_token"]),
            )
            if int(row["delivery_count"]) >= 2:
                deferred = self._connection.execute(
                    """SELECT user_id,handling_kind FROM advice_attention
                    WHERE user_id=? AND advice_id=?""",
                    (user_id, row["advice_id"]),
                ).fetchone()
                if deferred is not None and deferred["handling_kind"] == "later":
                    self._resolve_attention_locked(
                        row["advice_id"],
                        deferred["user_id"],
                        reason="handled:deferred",
                        current=current,
                    )

        due = self._connection.execute(
            """SELECT aa.advice_id,a.payload_json,
                     (SELECT COUNT(*) FROM advice_attention_attempts aaa
                      WHERE aaa.advice_id=aa.advice_id AND aaa.user_id=aa.user_id
                        AND aaa.state='delivered') AS completed_deliveries
            FROM advice_attention aa JOIN advice a
              ON a.id=aa.advice_id AND a.user_id=aa.user_id
            WHERE aa.user_id=? AND aa.state='waiting' AND aa.delivery_count=2
              AND aa.auto_close_at IS NOT NULL AND aa.auto_close_at<=?
              AND a.status='active'""",
            (user_id, current.isoformat()),
        ).fetchall()
        for row in due:
            try:
                advice = AdviceRecord.model_validate_json(row["payload_json"])
            except ValueError:
                continue
            if advice.status != AdviceStatus.ACTIVE:
                self._resolve_attention_locked(
                    advice.id,
                    advice.user_id,
                    reason="advice_not_active",
                    current=current,
                )
                continue
            if int(row["completed_deliveries"]) != 2:
                # The global permit budget is exhausted, but the client never
                # proved both reminders were actually shown. End delivery
                # silently without teaching the relevance model that the user
                # ignored or rejected the advice.
                self._resolve_attention_locked(
                    advice.id,
                    advice.user_id,
                    reason="technical_delivery_exhausted",
                    current=current,
                )
                changed += 1
                continue
            dismissed = advice.model_copy(update={"status": AdviceStatus.DISMISSED})
            self._connection.execute(
                """UPDATE advice SET status='dismissed',payload_json=?
                WHERE id=? AND user_id=? AND status='active'""",
                (dismissed.model_dump_json(), str(advice.id), advice.user_id),
            )
            self._connection.execute(
                """INSERT INTO feedback(
                  advice_id,user_id,kind,note,origin,signal_weight,created_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    str(advice.id),
                    advice.user_id,
                    "auto_irrelevant",
                    "ignored_timeout after two completed reminders",
                    "system",
                    AUTO_IRRELEVANT_WEIGHT,
                    current.isoformat(),
                ),
            )
            self._record_relevance_signal_locked(
                advice,
                "auto_irrelevant",
                origin="system",
                weight=AUTO_IRRELEVANT_WEIGHT,
                current=current,
            )
            self._resolve_attention_locked(
                advice.id,
                advice.user_id,
                reason="ignored_timeout",
                current=current,
            )
            changed += 1
        return changed

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def ready(self) -> bool:
        """Return whether the active SQLite connection can answer a query."""

        with self._lock:
            row = self._connection.execute("SELECT 1 AS ready").fetchone()
        return bool(row and row["ready"] == 1)

    def credentialed_user_count(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM users WHERE password_hash IS NOT NULL"
            ).fetchone()
        return int(row["count"])

    def total_user_count(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM users"
            ).fetchone()
        return int(row["count"])

    def user_locale(self, user_id: str) -> str:
        """Return the account-wide output language, defaulting legacy owners safely."""

        with self._lock:
            row = self._connection.execute(
                "SELECT locale FROM users WHERE id=? AND is_active=1",
                (user_id,),
            ).fetchone()
        if row is None:
            return DEFAULT_LOCALE
        return normalize_locale(row["locale"], strict=False)

    def account_preferences(self, user_id: str) -> dict[str, str | None]:
        with self._lock:
            row = self._connection.execute(
                """SELECT locale,locale_updated_at FROM users
                WHERE id=? AND is_active=1""",
                (user_id,),
            ).fetchone()
        if row is None:
            raise AccountNotFound("account no longer exists")
        return {
            "locale": normalize_locale(row["locale"], strict=False),
            "updated_at": row["locale_updated_at"],
        }

    def set_account_locale(self, user_id: str, locale: str) -> dict[str, str | None]:
        normalized = normalize_locale(locale)
        changed_at = _now()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._assert_account_writable_locked(user_id)
                row = self._connection.execute(
                    "SELECT locale,locale_updated_at FROM users WHERE id=? AND is_active=1",
                    (user_id,),
                ).fetchone()
                if row is None:
                    raise AccountNotFound("account no longer exists")
                current = normalize_locale(row["locale"], strict=False)
                if current == normalized:
                    self._connection.commit()
                    return {
                        "locale": current,
                        "updated_at": row["locale_updated_at"],
                    }
                self._connection.execute(
                    """UPDATE users SET locale=?,locale_updated_at=?
                    WHERE id=? AND is_active=1""",
                    (normalized, changed_at, user_id),
                )
                self._connection.commit()
                return {"locale": normalized, "updated_at": changed_at}
            except BaseException:
                self._connection.rollback()
                raise

    def create_registration_invite(
        self,
        *,
        invite_id: str,
        code_hash: str,
        label: str,
        expires_at: datetime,
    ) -> None:
        """Persist only the digest and administrative metadata for an invite."""

        normalized_label = str(label or "").strip()[:120]
        if not normalized_label:
            raise ValueError("invite label is required")
        if not re.fullmatch(r"[0-9a-f]{64}", str(code_hash or "")):
            raise ValueError("invite digest is invalid")
        current = auth_utc_now()
        expiry = _as_utc(expires_at)
        if expiry <= current:
            raise ValueError("invite expiry must be in the future")
        with self._lock:
            self._connection.execute(
                """INSERT INTO auth_invites(
                  id,code_hash,label,created_at,expires_at,
                  revoked_at,consumed_at,consumed_by_user_id
                ) VALUES(?,?,?,?,?,NULL,NULL,NULL)""",
                (invite_id, code_hash, normalized_label, current.isoformat(), expiry.isoformat()),
            )
            self._connection.commit()

    def registration_invite_available(
        self, code_hash: str, *, now: datetime | None = None
    ) -> bool:
        current = _as_utc(now or auth_utc_now()).isoformat()
        with self._lock:
            row = self._connection.execute(
                """SELECT 1 FROM auth_invites
                WHERE code_hash=? AND consumed_at IS NULL AND revoked_at IS NULL
                  AND expires_at>?""",
                (code_hash, current),
            ).fetchone()
        return row is not None

    def list_registration_invites(self) -> list[dict[str, Any]]:
        current = auth_utc_now()
        with self._lock:
            rows = self._connection.execute(
                """SELECT id,label,created_at,expires_at,revoked_at,
                          consumed_at,consumed_by_user_id
                FROM auth_invites ORDER BY created_at DESC"""
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            if row["consumed_at"]:
                state = "consumed"
            elif row["revoked_at"]:
                state = "revoked"
            elif _parse_utc(row["expires_at"]) <= current:
                state = "expired"
            else:
                state = "active"
            result.append({**dict(row), "state": state})
        return result

    def revoke_registration_invite(self, invite_id: str) -> bool:
        current = auth_utc_now().isoformat()
        with self._lock:
            cursor = self._connection.execute(
                """UPDATE auth_invites SET revoked_at=?
                WHERE id=? AND revoked_at IS NULL AND consumed_at IS NULL""",
                (current, invite_id),
            )
            self._connection.commit()
        return cursor.rowcount == 1

    @staticmethod
    def _validate_device_id(device_id: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(device_id or "")).strip()
        if not normalized or len(normalized) > 160 or any(
            unicodedata.category(character).startswith("C") for character in normalized
        ):
            raise ValueError("device is invalid")
        return normalized

    @staticmethod
    def _login_identity_hash(username_normalized: str, client_key: str) -> str:
        return hashlib.sha256(
            f"{username_normalized}\0{client_key}".encode("utf-8")
        ).hexdigest()

    def _login_is_locked_locked(self, identity_hash: str, current: datetime) -> bool:
        row = self._connection.execute(
            "SELECT locked_until FROM auth_login_failures WHERE identity_hash=?",
            (identity_hash,),
        ).fetchone()
        return bool(
            row
            and row["locked_until"]
            and _parse_utc(row["locked_until"]) > current
        )

    def _record_login_failure_locked(self, identity_hash: str, current: datetime) -> None:
        retention_hours = _environment_int(
            "MOUCHEN_AUTH_FAILURE_RETENTION_HOURS",
            24,
            minimum=1,
            maximum=24 * 365,
        )
        self._connection.execute(
            "DELETE FROM auth_login_failures WHERE updated_at<?",
            ((current - timedelta(hours=retention_hours)).isoformat(),),
        )
        row = self._connection.execute(
            """SELECT failed_count,window_started_at FROM auth_login_failures
            WHERE identity_hash=?""",
            (identity_hash,),
        ).fetchone()
        if row is None:
            row_limit = _environment_int(
                "MOUCHEN_AUTH_FAILURE_MAX_ROWS",
                100_000,
                minimum=1,
                maximum=10_000_000,
            )
            current_rows = self._connection.execute(
                "SELECT COUNT(*) AS count FROM auth_login_failures"
            ).fetchone()
            if int(current_rows["count"]) >= row_limit:
                # Keep authentication fail-closed at the request limiter while
                # refusing unbounded random-identity disk growth.
                return
        if row is None or _parse_utc(row["window_started_at"]) <= current - AUTH_LOGIN_FAILURE_WINDOW:
            failed_count = 1
            window_started = current
        else:
            failed_count = int(row["failed_count"]) + 1
            window_started = _parse_utc(row["window_started_at"])
        locked_until = (
            current + AUTH_LOGIN_LOCK_DURATION
            if failed_count >= AUTH_LOGIN_FAILURE_LIMIT
            else None
        )
        self._connection.execute(
            """INSERT INTO auth_login_failures(
              identity_hash,failed_count,window_started_at,locked_until,updated_at
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(identity_hash) DO UPDATE SET
              failed_count=excluded.failed_count,
              window_started_at=excluded.window_started_at,
              locked_until=excluded.locked_until,
              updated_at=excluded.updated_at""",
            (
                identity_hash,
                failed_count,
                window_started.isoformat(),
                locked_until.isoformat() if locked_until else None,
                current.isoformat(),
            ),
        )

    def _issue_auth_session_locked(
        self,
        *,
        user_id: str,
        username: str,
        device_id: str,
        device_name: str | None,
        expires_at: datetime,
    ) -> IssuedAuthSession:
        token_id = uuid4().hex
        access_token = new_access_token(token_id)
        current = auth_utc_now()
        scopes = ("mouchen:read", "mouchen:write")
        # A device owns one live credential at a time. Logging in again rotates
        # that device only; other phones/computers remain signed in.
        self._connection.execute(
            """DELETE FROM auth_tokens
            WHERE user_id=? AND (
              expires_at<=? OR (revoked_at IS NOT NULL AND revoked_at<=?)
            )""",
            (
                user_id,
                current.isoformat(),
                (current - timedelta(days=7)).isoformat(),
            ),
        )
        self._connection.execute(
            """UPDATE auth_tokens SET revoked_at=?
            WHERE user_id=? AND device_id=? AND revoked_at IS NULL""",
            (current.isoformat(), user_id, device_id),
        )
        active_device_limit = _environment_int(
            "MOUCHEN_AUTH_ACTIVE_DEVICES_PER_USER",
            20,
            minimum=1,
            maximum=1_000,
        )
        active_rows = self._connection.execute(
            """SELECT id FROM auth_tokens
            WHERE user_id=? AND revoked_at IS NULL AND expires_at>?
            ORDER BY COALESCE(last_used_at,created_at) DESC, created_at DESC""",
            (user_id, current.isoformat()),
        ).fetchall()
        for stale in active_rows[max(0, active_device_limit - 1) :]:
            self._connection.execute(
                "UPDATE auth_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                (current.isoformat(), stale["id"]),
            )
        self._connection.execute(
            """INSERT INTO auth_tokens(
              id,token_hash,user_id,device_id,device_name,scopes_json,
              created_at,expires_at,last_used_at,revoked_at
            ) VALUES(?,?,?,?,?,?,?,?,?,NULL)""",
            (
                token_id,
                token_hash(access_token),
                user_id,
                device_id,
                (device_name or "").strip()[:120] or None,
                json.dumps(scopes),
                current.isoformat(),
                _as_utc(expires_at).isoformat(),
                current.isoformat(),
            ),
        )
        token_history_limit = _environment_int(
            "MOUCHEN_AUTH_TOKEN_HISTORY_PER_USER",
            200,
            minimum=active_device_limit,
            maximum=10_000,
        )
        excess_history = self._connection.execute(
            """SELECT id FROM auth_tokens
            WHERE user_id=? AND revoked_at IS NOT NULL
            ORDER BY revoked_at DESC, created_at DESC
            LIMIT -1 OFFSET ?""",
            (user_id, token_history_limit),
        ).fetchall()
        if excess_history:
            self._connection.executemany(
                "DELETE FROM auth_tokens WHERE id=? AND user_id=? AND revoked_at IS NOT NULL",
                ((row["id"], user_id) for row in excess_history),
            )
        return IssuedAuthSession(
            access_token=access_token,
            principal=AuthPrincipal(
                user_id=user_id,
                username=username,
                device_id=device_id,
                scopes=scopes,
                expires_at=_as_utc(expires_at),
                token_id=token_id,
            ),
        )

    def register_account(
        self,
        *,
        username: str,
        password: str,
        device_id: str,
        device_name: str | None,
        expires_at: datetime,
        registration_code_hash: str | None = None,
        registration_code_kind: str | None = None,
        locale: str = DEFAULT_LOCALE,
    ) -> IssuedAuthSession:
        display, normalized = normalize_username(username)
        password_digest = hash_password(password)
        normalized_device = self._validate_device_id(device_id)
        normalized_locale = normalize_locale(locale)
        user_id = str(uuid4())
        current = auth_utc_now().isoformat()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                legacy = self._connection.execute(
                    """SELECT id,password_hash FROM users
                    WHERE username_normalized=?""",
                    (normalized,),
                ).fetchone()
                if legacy is None and registration_code_kind == "bootstrap":
                    pending_legacy_owner = self._connection.execute(
                        """SELECT 1 FROM users
                        WHERE username_normalized IS NOT NULL
                          AND password_hash IS NULL
                        LIMIT 1"""
                    ).fetchone()
                    if pending_legacy_owner is not None:
                        # A configured legacy mapping is the authoritative
                        # owner identity. A typo must not create a second
                        # tenant or consume the one-time bootstrap secret.
                        raise ValueError("registration unavailable")
                if legacy is not None:
                    # Only the out-of-band bootstrap secret may claim an
                    # already-mapped legacy tenant. Ordinary user invitations
                    # can create new tenants only and must never inherit old
                    # owner data merely by guessing its login name.
                    if (
                        legacy["password_hash"] is not None
                        or not registration_code_hash
                        or registration_code_kind != "bootstrap"
                    ):
                        raise ValueError("registration unavailable")
                    user_id = legacy["id"]
                    cursor = self._connection.execute(
                        """UPDATE users SET username_display=?,password_hash=?,is_active=1,
                        locale=?,locale_updated_at=?
                        WHERE id=? AND username_normalized=? AND password_hash IS NULL""",
                        (
                            display,
                            password_digest,
                            normalized_locale,
                            current,
                            user_id,
                            normalized,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("registration unavailable")
                else:
                    self._connection.execute(
                        """INSERT INTO users(
                          id,username_normalized,username_display,password_hash,is_active,
                          locale,locale_updated_at,created_at
                        ) VALUES(?,?,?,?,1,?,?,?)""",
                        (
                            user_id,
                            normalized,
                            display,
                            password_digest,
                            normalized_locale,
                            current,
                            current,
                        ),
                    )
                if registration_code_kind == "invite":
                    cursor = self._connection.execute(
                        """UPDATE auth_invites
                        SET consumed_by_user_id=?,consumed_at=?
                        WHERE code_hash=? AND consumed_at IS NULL AND revoked_at IS NULL
                          AND expires_at>?""",
                        (user_id, current, registration_code_hash, current),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("registration unavailable")
                elif registration_code_kind == "bootstrap" and registration_code_hash:
                    self._connection.execute(
                        """INSERT INTO auth_registration_codes(
                          code_hash,consumed_by_user_id,consumed_at
                        ) VALUES(?,?,?)""",
                        (registration_code_hash, user_id, current),
                    )
                elif registration_code_hash or registration_code_kind:
                    raise ValueError("registration unavailable")
                issued = self._issue_auth_session_locked(
                    user_id=user_id,
                    username=display,
                    device_id=normalized_device,
                    device_name=device_name,
                    expires_at=expires_at,
                )
                self._connection.commit()
                return issued
            except sqlite3.IntegrityError as exc:
                self._connection.rollback()
                raise ValueError("registration unavailable") from exc
            except BaseException:
                self._connection.rollback()
                raise

    def login_account(
        self,
        *,
        username: str,
        password: str,
        device_id: str,
        device_name: str | None,
        client_key: str,
        expires_at: datetime,
    ) -> IssuedAuthSession | None:
        try:
            _, normalized = normalize_username(username)
        except ValueError:
            # Keep an invalid identifier on the same expensive verification path.
            normalized = hashlib.sha256(str(username).encode("utf-8")).hexdigest()
        try:
            supplied_password = validate_password(password)
        except ValueError:
            supplied_password = str(password or "")[:1024]
        normalized_device = self._validate_device_id(device_id)
        identity_hash = self._login_identity_hash(normalized, client_key)
        current = auth_utc_now()
        with self._lock:
            locked = self._login_is_locked_locked(identity_hash, current)
            row = self._connection.execute(
                """SELECT id,username_display,password_hash,is_active FROM users
                WHERE username_normalized=?""",
                (normalized,),
            ).fetchone()
        valid, replacement_hash = verify_password(
            row["password_hash"] if row is not None and not locked else None,
            supplied_password,
        )
        if locked or row is None or not bool(row["is_active"]) or not valid:
            with self._lock:
                self._record_login_failure_locked(identity_hash, current)
                self._connection.commit()
            return None
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                if replacement_hash:
                    self._connection.execute(
                        "UPDATE users SET password_hash=? WHERE id=?",
                        (replacement_hash, row["id"]),
                    )
                self._connection.execute(
                    "DELETE FROM auth_login_failures WHERE identity_hash=?",
                    (identity_hash,),
                )
                issued = self._issue_auth_session_locked(
                    user_id=row["id"],
                    username=row["username_display"],
                    device_id=normalized_device,
                    device_name=device_name,
                    expires_at=expires_at,
                )
                self._connection.commit()
                return issued
            except BaseException:
                self._connection.rollback()
                raise

    def authenticate_access_token(self, supplied_token: str) -> AuthPrincipal | None:
        locator = token_locator(supplied_token)
        if locator is None:
            return None
        current = auth_utc_now()
        with self._lock:
            row = self._connection.execute(
                """SELECT t.*,u.username_display,u.is_active
                FROM auth_tokens t JOIN users u ON u.id=t.user_id
                WHERE t.id=?""",
                (locator,),
            ).fetchone()
            if (
                row is None
                or row["revoked_at"] is not None
                or not bool(row["is_active"])
                or _parse_utc(row["expires_at"]) <= current
                or not constant_time_token_match(row["token_hash"], supplied_token)
            ):
                return None
            last_used = _parse_utc(row["last_used_at"]) if row["last_used_at"] else None
            if last_used is None or last_used <= current - AUTH_LAST_USED_WRITE_INTERVAL:
                self._connection.execute(
                    "UPDATE auth_tokens SET last_used_at=? WHERE id=? AND revoked_at IS NULL",
                    (current.isoformat(), locator),
                )
                self._connection.commit()
            try:
                scopes = tuple(json.loads(row["scopes_json"]))
            except (TypeError, json.JSONDecodeError):
                return None
        return AuthPrincipal(
            user_id=row["user_id"],
            username=row["username_display"],
            device_id=row["device_id"],
            scopes=scopes,
            expires_at=_parse_utc(row["expires_at"]),
            token_id=locator,
        )

    def legacy_principal(self, user_id: str) -> AuthPrincipal:
        with self._lock:
            row = self._connection.execute(
                "SELECT username_display FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
        return AuthPrincipal(
            user_id=user_id,
            username=row["username_display"] if row else None,
            device_id="legacy-single-user",
            scopes=("mouchen:read", "mouchen:write"),
            expires_at=None,
            token_id=None,
            auth_kind="legacy",
        )

    def revoke_auth_token(self, user_id: str, token_id: str | None) -> bool:
        if token_id is None:
            return False
        with self._lock:
            cursor = self._connection.execute(
                """UPDATE auth_tokens SET revoked_at=COALESCE(revoked_at,?)
                WHERE id=? AND user_id=? AND revoked_at IS NULL""",
                (_now(), token_id, user_id),
            )
            self._connection.commit()
        return cursor.rowcount == 1

    def _account_reference_columns_locked(self) -> list[tuple[str, tuple[str, ...]]]:
        """Discover every table column that can retain a tenant reference."""

        discovered: list[tuple[str, tuple[str, ...]]] = []
        tables = self._connection.execute(
            """SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name"""
        ).fetchall()
        for table_row in tables:
            table = str(table_row["name"])
            columns = self._connection.execute(
                f"PRAGMA table_info({_quote_sqlite_identifier(table)})"
            ).fetchall()
            references = tuple(
                str(column["name"])
                for column in columns
                if str(column["name"]).casefold() in _ACCOUNT_REFERENCE_COLUMNS
            )
            if references:
                discovered.append((table, references))
        return discovered

    def _assert_account_writable_locked(self, user_id: str) -> None:
        retired = self._connection.execute(
            "SELECT 1 FROM retired_user_ids WHERE user_id_hash=?",
            (_user_id_hash(user_id),),
        ).fetchone()
        if retired is not None:
            raise AccountRetired("account is retired")

    @staticmethod
    def _account_reference_predicate(columns: tuple[str, ...]) -> str:
        return " OR ".join(
            f"{_quote_sqlite_identifier(column)}=?" for column in columns
        )

    def verify_account_password(self, user_id: str, supplied_password: str) -> None:
        with self._lock:
            row = self._connection.execute(
                "SELECT password_hash,is_active FROM users WHERE id=?", (user_id,)
            ).fetchone()
        valid, _ = verify_password(
            row["password_hash"] if row is not None else None,
            str(supplied_password or ""),
        )
        if row is None or not bool(row["is_active"]) or not valid:
            raise AccountPasswordInvalid("current password is invalid")

    def stream_account_export(self, user_id: str, *, page_size: int = 100):
        """Open a snapshot and stream bounded row pages without the shared lock."""

        connection = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        try:
            account = connection.execute(
                """SELECT id,username_display,is_active,locale,locale_updated_at,created_at
                FROM users WHERE id=?""",
                (user_id,),
            ).fetchone()
            if account is None:
                raise AccountNotFound("account no longer exists")
            references: list[tuple[str, tuple[str, ...]]] = []
            tables = connection.execute(
                """SELECT name FROM sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"""
            ).fetchall()
            for table_row in tables:
                table = str(table_row["name"])
                columns = connection.execute(
                    f"PRAGMA table_info({_quote_sqlite_identifier(table)})"
                ).fetchall()
                reference_columns = tuple(
                    str(column["name"])
                    for column in columns
                    if str(column["name"]).casefold() in _ACCOUNT_REFERENCE_COLUMNS
                )
                if reference_columns:
                    references.append((table, reference_columns))
        except BaseException:
            connection.rollback()
            connection.close()
            raise

        bounded_page = max(1, min(1000, int(page_size)))

        def encode(value: Any) -> str:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

        def rows(cursor):
            first = True
            while True:
                page = cursor.fetchmany(bounded_page)
                if not page:
                    break
                for row in page:
                    if not first:
                        yield ","
                    first = False
                    yield encode(_account_export_row(row))

        def generate():
            try:
                account_payload = {
                    "id": account["id"],
                    "username": account["username_display"],
                    "is_active": bool(account["is_active"]),
                    "locale": normalize_locale(account["locale"], strict=False),
                    "locale_updated_at": account["locale_updated_at"],
                    "created_at": account["created_at"],
                }
                yield (
                    '{"schema_version":1,"exported_at":'
                    + encode(_now())
                    + ',"account":'
                    + encode(account_payload)
                    + ',"sessions":['
                )
                session_cursor = connection.execute(
                    """SELECT device_id,device_name,scopes_json,created_at,
                              expires_at,last_used_at,revoked_at
                    FROM auth_tokens WHERE user_id=? ORDER BY created_at""",
                    (user_id,),
                )
                yield from rows(session_cursor)
                yield '],"tables":{'
                first_table = True
                for table, columns in references:
                    if table == "auth_tokens":
                        continue
                    predicate = self._account_reference_predicate(columns)
                    cursor = connection.execute(
                        f"SELECT * FROM {_quote_sqlite_identifier(table)} "
                        f"WHERE {predicate}",
                        tuple(user_id for _ in columns),
                    )
                    first_page = cursor.fetchmany(bounded_page)
                    if not first_page:
                        continue
                    if not first_table:
                        yield ","
                    first_table = False
                    yield encode(table) + ":["
                    first_row = True
                    page = first_page
                    while page:
                        for row in page:
                            if not first_row:
                                yield ","
                            first_row = False
                            yield encode(_account_export_row(row))
                        page = cursor.fetchmany(bounded_page)
                    yield "]"
                yield "}}"
                connection.commit()
            except GeneratorExit:
                connection.rollback()
                raise
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

        return generate()

    def prepare_account_export_file(
        self,
        user_id: str,
        *,
        page_size: int = 100,
        maximum_bytes: int | None = None,
    ) -> PreparedAccountExport:
        """Materialize a bounded snapshot before the potentially slow download.

        The SQLite read transaction exists only while this method writes the
        private spool.  Client transfer therefore cannot pin the WAL or block a
        checkpoint.  A unique 0700 directory and 0600 file contain the export.
        """

        limit = (
            _environment_int(
                "MOUCHEN_ACCOUNT_EXPORT_MAX_BYTES",
                256 * 1024 * 1024,
                minimum=1024 * 1024,
                maximum=1024 * 1024 * 1024,
            )
            if maximum_bytes is None
            else max(1, int(maximum_bytes))
        )
        root = self._account_export_temp_root()
        self._cleanup_stale_account_export_files()
        directory = Path(
            tempfile.mkdtemp(
                prefix="mouchen-account-export-",
                dir=str(root),
            )
        )
        try:
            directory.chmod(0o700)
            path = directory / "account-export.json"
            written = 0
            stream = self.stream_account_export(user_id, page_size=page_size)
            try:
                with path.open("xb") as handle:
                    path.chmod(0o600)
                    for chunk in stream:
                        encoded = str(chunk).encode("utf-8")
                        written += len(encoded)
                        if written > limit:
                            raise AccountExportTooLarge(
                                "account export exceeds configured maximum"
                            )
                        handle.write(encoded)
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
            return PreparedAccountExport(path, directory, written)
        except BaseException:
            (directory / "account-export.json").unlink(missing_ok=True)
            try:
                directory.rmdir()
            except OSError:
                pass
            raise

    def _account_export_temp_root(self) -> Path:
        configured = os.getenv("MOUCHEN_ACCOUNT_EXPORT_TEMP_DIR", "").strip()
        root = Path(configured) if configured else self.path.parent / ".account-exports"
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            raise OSError("account export temp root is unsafe")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink() or not root.is_dir():
            raise OSError("account export temp root is unsafe")
        try:
            root.chmod(0o700)
        except OSError:
            pass
        return root.resolve()

    def _cleanup_stale_account_export_files(self) -> None:
        """Remove only old, exactly shaped crash spools; never recurse."""

        root = self._account_export_temp_root()
        lease_seconds = _environment_int(
            "MOUCHEN_ACCOUNT_EXPORT_LEASE_SECONDS",
            3600,
            minimum=60,
            maximum=86_400,
        )
        cutoff = time.time() - lease_seconds - 300
        for directory in root.glob("mouchen-account-export-*"):
            try:
                if directory.is_symlink() or not directory.is_dir():
                    continue
                if directory.stat().st_mtime > cutoff:
                    continue
                entries = list(directory.iterdir())
                if any(
                    entry.name != "account-export.json"
                    or entry.is_symlink()
                    or not entry.is_file()
                    for entry in entries
                ):
                    continue
                for entry in entries:
                    entry.unlink()
                directory.rmdir()
            except (FileNotFoundError, OSError):
                continue

    def change_account_password(
        self,
        *,
        user_id: str,
        token_id: str,
        current_password: str,
        new_password: str,
        device_id: str,
        expires_at: datetime,
    ) -> IssuedAuthSession:
        """CAS the password, revoke every old session, and issue one fresh token."""

        with self._lock:
            observed = self._connection.execute(
                """SELECT password_hash,is_active FROM users WHERE id=?""",
                (user_id,),
            ).fetchone()
        valid, _ = verify_password(
            observed["password_hash"] if observed is not None else None,
            str(current_password or ""),
        )
        if observed is None or not bool(observed["is_active"]) or not valid:
            raise AccountPasswordInvalid("current password is invalid")
        observed_hash = str(observed["password_hash"])
        replacement_hash = hash_password(new_password)
        normalized_device = self._validate_device_id(device_id)
        current = auth_utc_now()

        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                session = self._connection.execute(
                    """SELECT device_id,device_name FROM auth_tokens
                    WHERE id=? AND user_id=? AND revoked_at IS NULL AND expires_at>?""",
                    (token_id, user_id, current.isoformat()),
                ).fetchone()
                if session is None or session["device_id"] != normalized_device:
                    raise AccountChangedConcurrently("current session changed")
                cursor = self._connection.execute(
                    """UPDATE users SET password_hash=?
                    WHERE id=? AND password_hash=? AND is_active=1""",
                    (replacement_hash, user_id, observed_hash),
                )
                if cursor.rowcount != 1:
                    raise AccountChangedConcurrently("account password changed")
                account = self._connection.execute(
                    "SELECT username_display FROM users WHERE id=?",
                    (user_id,),
                ).fetchone()
                self._connection.execute(
                    """UPDATE auth_tokens SET revoked_at=COALESCE(revoked_at,?)
                    WHERE user_id=?""",
                    (current.isoformat(), user_id),
                )
                issued = self._issue_auth_session_locked(
                    user_id=user_id,
                    username=account["username_display"],
                    device_id=normalized_device,
                    device_name=session["device_name"],
                    expires_at=expires_at,
                )
                self._connection.commit()
                return issued
            except BaseException:
                self._connection.rollback()
                raise

    def delete_account(self, *, user_id: str, current_password: str) -> None:
        """Delete one tenant atomically and prove no tenant references remain."""

        with self._lock:
            observed = self._connection.execute(
                """SELECT password_hash,is_active FROM users WHERE id=?""",
                (user_id,),
            ).fetchone()
        valid, _ = verify_password(
            observed["password_hash"] if observed is not None else None,
            str(current_password or ""),
        )
        if observed is None or not bool(observed["is_active"]) or not valid:
            raise AccountPasswordInvalid("current password is invalid")
        observed_hash = str(observed["password_hash"])

        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute("PRAGMA defer_foreign_keys=ON")
                current = self._connection.execute(
                    "SELECT password_hash,is_active FROM users WHERE id=?",
                    (user_id,),
                ).fetchone()
                if (
                    current is None
                    or not bool(current["is_active"])
                    or current["password_hash"] != observed_hash
                ):
                    raise AccountChangedConcurrently("account password changed")

                retired_hash = _user_id_hash(user_id)
                retired_marker = _retired_user_marker(user_id)
                self._connection.execute(
                    """INSERT INTO retired_user_ids(user_id_hash,retired_at)
                    VALUES(?,?) ON CONFLICT(user_id_hash) DO NOTHING""",
                    (retired_hash, _now()),
                )
                # One-time registration evidence must remain consumed after
                # deletion, while the original user id must not remain.
                self._connection.execute(
                    """UPDATE auth_registration_codes SET consumed_by_user_id=?
                    WHERE consumed_by_user_id=?""",
                    (retired_marker, user_id),
                )
                self._connection.execute(
                    """UPDATE auth_invites
                    SET consumed_by_user_hash=?, consumed_by_user_id=NULL
                    WHERE consumed_by_user_id=?""",
                    (retired_hash, user_id),
                )
                # Export admission rows are operational metadata, not the
                # minimum anti-resurrection tombstone.  They may have been
                # created lazily by another process, so remove them only when
                # their runtime tables are present.
                runtime_tables = {
                    str(row["name"])
                    for row in self._connection.execute(
                        """SELECT name FROM sqlite_master
                        WHERE type='table' AND name IN(
                          'account_export_requests','account_export_leases'
                        )"""
                    ).fetchall()
                }
                for table in runtime_tables:
                    self._connection.execute(
                        f"DELETE FROM {_quote_sqlite_identifier(table)} "
                        "WHERE user_id_hash=?",
                        (retired_hash,),
                    )
                references = self._account_reference_columns_locked()
                for table, columns in references:
                    if table in {"users", "auth_registration_codes", "auth_invites"}:
                        continue
                    predicate = self._account_reference_predicate(columns)
                    self._connection.execute(
                        f"DELETE FROM {_quote_sqlite_identifier(table)} "
                        f"WHERE {predicate}",
                        tuple(user_id for _ in columns),
                    )
                deleted = self._connection.execute(
                    "DELETE FROM users WHERE id=? AND password_hash=?",
                    (user_id, observed_hash),
                )
                if deleted.rowcount != 1:
                    raise AccountChangedConcurrently("account changed")

                residuals: list[str] = []
                for table, columns in references:
                    if table in {"users", "auth_registration_codes", "auth_invites"}:
                        continue
                    predicate = self._account_reference_predicate(columns)
                    row = self._connection.execute(
                        f"SELECT COUNT(*) AS count "
                        f"FROM {_quote_sqlite_identifier(table)} WHERE {predicate}",
                        tuple(user_id for _ in columns),
                    ).fetchone()
                    if int(row["count"]):
                        residuals.append(table)
                user_row = self._connection.execute(
                    "SELECT COUNT(*) AS count FROM users WHERE id=?",
                    (user_id,),
                ).fetchone()
                if int(user_row["count"]):
                    residuals.append("users")
                for table in ("auth_registration_codes", "auth_invites"):
                    row = self._connection.execute(
                        f"SELECT COUNT(*) AS count FROM {table} "
                        "WHERE consumed_by_user_id=?",
                        (user_id,),
                    ).fetchone()
                    if int(row["count"]):
                        residuals.append(table)
                tombstone = self._connection.execute(
                    "SELECT COUNT(*) AS count FROM retired_user_ids WHERE user_id_hash=?",
                    (retired_hash,),
                ).fetchone()
                if int(tombstone["count"]) != 1:
                    residuals.append("retired_user_ids")
                for table in runtime_tables:
                    row = self._connection.execute(
                        f"SELECT COUNT(*) AS count "
                        f"FROM {_quote_sqlite_identifier(table)} "
                        "WHERE user_id_hash=?",
                        (retired_hash,),
                    ).fetchone()
                    if int(row["count"]):
                        residuals.append(table)
                if residuals:
                    raise AccountDeletionIncomplete("account data remains after deletion")
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def ensure_user(self, user_id: str) -> None:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._assert_account_writable_locked(user_id)
                self._connection.execute(
                    "INSERT OR IGNORE INTO users(id, created_at) VALUES(?, ?)",
                    (user_id, _now()),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def insert_goal(self, goal: Goal, *, max_goals: int | None = None) -> Goal:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._assert_account_writable_locked(goal.user_id)
                self._connection.execute(
                    "INSERT OR IGNORE INTO users(id,created_at) VALUES(?,?)",
                    (goal.user_id, _now()),
                )
                existing = self._connection.execute(
                    """SELECT user_id,payload_json FROM goals
                    WHERE user_id=? AND id=?""",
                    (goal.user_id, str(goal.id)),
                ).fetchone()
                if existing:
                    self._connection.commit()
                    return Goal.model_validate_json(existing["payload_json"])
                if max_goals is not None:
                    count_row = self._connection.execute(
                        "SELECT COUNT(*) AS count FROM goals WHERE user_id=?",
                        (goal.user_id,),
                    ).fetchone()
                    if int(count_row["count"]) >= max(0, int(max_goals)):
                        raise GoalQuotaExceeded("goal storage quota exceeded")
                row = self._connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM goals WHERE user_id=? AND domain=?",
                    (goal.user_id, goal.domain),
                ).fetchone()
                goal = goal.model_copy(update={"version": int(row["version"]) + 1})
                self._connection.execute(
                    "INSERT INTO goals VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(goal.id), goal.user_id, goal.domain, goal.version, goal.quote,
                        goal.model_dump_json(), goal.created_at.isoformat(), goal.reaffirmed_at.isoformat(),
                    ),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return goal

    def list_goals(self, user_id: str) -> list[Goal]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM goals WHERE user_id=? ORDER BY created_at DESC", (user_id,)
            ).fetchall()
        return [Goal.model_validate_json(row["payload_json"]) for row in rows]

    def get_goal(self, user_id: str, goal_id: UUID | str) -> Goal | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM goals WHERE user_id=? AND id=?", (user_id, str(goal_id))
            ).fetchone()
        return Goal.model_validate_json(row["payload_json"]) if row else None

    def current_goal(self, user_id: str, domain: str) -> Goal | None:
        goals = [goal for goal in self.current_goals(user_id) if goal.domain == domain]
        return max(goals, key=lambda goal: (goal.version, goal.created_at), default=None)

    def current_goals(self, user_id: str) -> list[Goal]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM goals WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        now = datetime.now(timezone.utc)
        current: list[Goal] = []
        for row in rows:
            goal = Goal.model_validate_json(row["payload_json"])
            valid_until = goal.valid_until
            if valid_until is not None:
                if valid_until.tzinfo is None:
                    valid_until = valid_until.replace(tzinfo=timezone.utc)
                else:
                    valid_until = valid_until.astimezone(timezone.utc)
                if valid_until <= now:
                    continue
            current.append(goal)
        return current

    def insert_event(self, event: Event) -> bool:
        payload = event.model_dump_json()
        payload_bytes = len(payload.encode("utf-8"))
        quota = _environment_int(
            "MOUCHEN_EVENT_STORAGE_BYTES_PER_USER",
            100 * 1024 * 1024,
            minimum=1,
            maximum=10 * 1024 * 1024 * 1024,
        )
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._assert_account_writable_locked(event.user_id)
                self._connection.execute(
                    "INSERT OR IGNORE INTO users(id,created_at) VALUES(?,?)",
                    (event.user_id, _now()),
                )
                duplicate = self._connection.execute(
                    """SELECT 1 FROM events
                    WHERE user_id=? AND (
                      id=? OR (? IS NOT NULL AND evidence_ref=?)
                    ) LIMIT 1""",
                    (
                        event.user_id,
                        str(event.event_id),
                        event.evidence_ref,
                        event.evidence_ref,
                    ),
                ).fetchone()
                if duplicate is not None:
                    self._connection.commit()
                    return False
                usage_row = self._connection.execute(
                    "SELECT event_bytes FROM tenant_storage_usage WHERE user_id=?",
                    (event.user_id,),
                ).fetchone()
                used = int(usage_row["event_bytes"]) if usage_row is not None else 0
                if used + payload_bytes > quota:
                    raise EventQuotaExceeded("event storage quota exceeded")
                self._connection.execute(
                    """INSERT INTO events(
                      id, user_id, source, type, occurred_at, evidence_ref, payload_json, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(event.event_id), event.user_id, event.source, event.type,
                        event.occurred_at.isoformat(), event.evidence_ref,
                        payload, event.created_at.isoformat(),
                    ),
                )
                self._connection.execute(
                    """INSERT INTO tenant_storage_usage(user_id,event_bytes,updated_at)
                    VALUES(?,?,?)
                    ON CONFLICT(user_id) DO UPDATE SET
                      event_bytes=excluded.event_bytes,
                      updated_at=excluded.updated_at""",
                    (event.user_id, used + payload_bytes, _now()),
                )
                self._connection.commit()
                return True
            except Exception:
                self._connection.rollback()
                raise

    def get_event(self, user_id: str, event_id: UUID | str) -> Event | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM events WHERE user_id=? AND id=?",
                (user_id, str(event_id)),
            ).fetchone()
        return Event.model_validate_json(row["payload_json"]) if row else None

    def enqueue_analysis_job(
        self,
        user_id: str,
        event_id: UUID | str,
        *,
        job_kind: str = "discover",
        goal_id: UUID | str | None = None,
        cloud_approved: bool = False,
        raw_cloud_approved: bool = False,
    ) -> bool:
        """Queue one stored event exactly once.

        The INSERT ... SELECT form deliberately refuses an event object that
        lost an evidence_ref idempotency race and therefore was never stored.
        """

        if job_kind not in {"discover", "refine_deterministic"}:
            raise ValueError("invalid analysis job kind")
        if job_kind == "refine_deterministic" and goal_id is None:
            raise ValueError("deterministic refinement requires goal_id")
        raw_cloud_approved = bool(raw_cloud_approved and cloud_approved)
        now = _now()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._assert_account_writable_locked(user_id)
                event_row = self._connection.execute(
                    "SELECT payload_json FROM events WHERE id=? AND user_id=?",
                    (str(event_id), user_id),
                ).fetchone()
                if event_row is None:
                    self._connection.commit()
                    return False
                priority = _analysis_priority(
                    Event.model_validate_json(event_row["payload_json"]),
                    job_kind,
                )
                existed = self._connection.execute(
                    "SELECT 1 FROM analysis_jobs WHERE event_id=? AND user_id=?",
                    (str(event_id), user_id),
                ).fetchone()
                self._connection.execute(
                    """INSERT INTO analysis_jobs(
                  event_id,user_id,job_kind,goal_id,cloud_approved,raw_cloud_approved,
                  priority,status,attempts,next_attempt_at,last_reason,
                  created_at,updated_at,completed_at
                ) VALUES(?,?,?,?,?,?,?,'pending',0,?,NULL,?,?,NULL)
                ON CONFLICT(user_id,event_id) DO UPDATE SET
                  cloud_approved=MAX(analysis_jobs.cloud_approved, excluded.cloud_approved),
                  raw_cloud_approved=MAX(
                    analysis_jobs.raw_cloud_approved, excluded.raw_cloud_approved
                  ),
                  priority=MAX(analysis_jobs.priority, excluded.priority),
                  job_kind=CASE
                    WHEN excluded.job_kind='refine_deterministic'
                    THEN excluded.job_kind ELSE analysis_jobs.job_kind END,
                  goal_id=CASE
                    WHEN excluded.job_kind='refine_deterministic'
                    THEN excluded.goal_id ELSE analysis_jobs.goal_id END
                WHERE analysis_jobs.user_id=excluded.user_id
                  AND analysis_jobs.status IN ('pending','retry')""",
                    (
                    str(event_id),
                    user_id,
                    job_kind,
                    str(goal_id) if goal_id is not None else None,
                    int(cloud_approved),
                    int(raw_cloud_approved),
                    priority,
                    now,
                    now,
                    now,
                    ),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return existed is None

    def retire_stale_analysis_jobs(
        self,
        user_id: str | None = None,
        *,
        now: datetime | None = None,
    ) -> int:
        """Finish stale discovery jobs while retaining their source events."""

        current = _as_utc(now)
        with self._lock:
            retired = self._retire_stale_analysis_jobs_locked(user_id, current)
            self._connection.commit()
        return retired

    def _retire_stale_analysis_jobs_locked(
        self,
        user_id: str | None,
        current: datetime,
    ) -> int:
        regular_max_age, high_priority_max_age = _analysis_discover_max_ages()
        regular_cutoff = (current - regular_max_age).isoformat()
        high_priority_cutoff = (current - high_priority_max_age).isoformat()
        user_filter = ""
        parameters: list[Any] = [current.isoformat(), current.isoformat()]
        if user_id is not None:
            user_filter = " AND j.user_id=?"
            parameters.append(user_id)
        parameters.extend(
            (
                ANALYSIS_PRIORITY_USER_REQUEST,
                regular_cutoff,
                ANALYSIS_PRIORITY_USER_REQUEST,
                high_priority_cutoff,
            )
        )
        cursor = self._connection.execute(
            f"""UPDATE analysis_jobs
            SET status='no_intervention', last_reason='stale_discover_retired',
                updated_at=?, completed_at=?
            WHERE (user_id,event_id) IN (
              SELECT j.user_id,j.event_id FROM analysis_jobs j
              JOIN events e ON e.id=j.event_id AND e.user_id=j.user_id
              WHERE j.job_kind='discover'
                AND j.status IN ('pending','retry'){user_filter}
                AND COALESCE(j.last_reason, '') NOT IN (
                  'analysis_budget_deferred','global_model_budget_deferred'
                )
                AND (
                  (j.priority<? AND julianday(e.occurred_at)<=julianday(?))
                  OR
                  (j.priority>=? AND julianday(e.occurred_at)<=julianday(?))
                )
            )""",
            tuple(parameters),
        )
        return cursor.rowcount

    def claim_analysis_job(
        self,
        user_id: str,
        event_id: UUID | str | None = None,
        *,
        now: datetime | None = None,
        hourly_call_limit: int | None = None,
        daily_call_limit: int | None = None,
        global_hourly_call_limit: int | None = None,
        global_daily_call_limit: int | None = None,
        max_attempts: int | None = None,
    ) -> AnalysisJobClaim | None:
        """Atomically lease one due job and reserve its worst-case model calls.

        A semantic job can make a primary and an independent critic call. The
        reservation counts at its worst case until the processor durably
        records and settles the calls it actually attempted.
        """

        current = _as_utc(now)
        current_text = current.isoformat()
        stale_before = (current - ANALYSIS_JOB_LEASE).isoformat()
        attempt_limit = max(1, int(max_attempts)) if max_attempts is not None else None
        reservation_id: int | None = None
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                if attempt_limit is not None:
                    self._connection.execute(
                        """UPDATE analysis_jobs
                        SET status='no_intervention',
                            last_reason='analysis_max_attempts_exhausted',
                            last_failure_reason=COALESCE(
                              last_failure_reason, 'worker_lease_expired'
                            ),
                            updated_at=?, completed_at=?
                        WHERE user_id=? AND status='running'
                          AND updated_at<=? AND attempts>=?""",
                        (
                            current_text,
                            current_text,
                            user_id,
                            stale_before,
                            attempt_limit,
                        ),
                    )
                    self._connection.execute(
                        """UPDATE analysis_jobs
                        SET status='retry', next_attempt_at=?,
                            last_reason='worker_lease_expired',
                            last_failure_reason='worker_lease_expired', updated_at=?
                        WHERE user_id=? AND status='running'
                          AND updated_at<=? AND attempts<?""",
                        (
                            current_text,
                            current_text,
                            user_id,
                            stale_before,
                            attempt_limit,
                        ),
                    )
                    self._connection.execute(
                        """UPDATE analysis_jobs
                        SET status='no_intervention',
                            last_reason='analysis_max_attempts_exhausted',
                            last_failure_reason=COALESCE(
                              last_failure_reason,
                              CASE WHEN last_reason IN (
                                'analysis_budget_deferred',
                                'global_model_budget_deferred'
                              ) THEN NULL ELSE last_reason END,
                              'analysis_max_attempts_exhausted'
                            ),
                            updated_at=?, completed_at=?
                        WHERE user_id=? AND status IN ('pending','retry')
                          AND attempts>=?""",
                        (
                            current_text,
                            current_text,
                            user_id,
                            attempt_limit,
                        ),
                    )
                else:
                    self._connection.execute(
                        """UPDATE analysis_jobs
                        SET status='retry', next_attempt_at=?,
                            last_reason='worker_lease_expired', updated_at=?
                        WHERE user_id=? AND status='running' AND updated_at<=?""",
                        (current_text, current_text, user_id, stale_before),
                    )
                self._settle_inactive_analysis_reservations_locked(user_id, current)
                self._retire_stale_analysis_jobs_locked(user_id, current)
                parameters: list[Any] = [user_id, current_text]
                event_filter = ""
                if event_id is not None:
                    event_filter = " AND event_id=?"
                    parameters.append(str(event_id))
                row = self._connection.execute(
                    f"""SELECT j.event_id FROM analysis_jobs j
                    JOIN events e ON e.id=j.event_id AND e.user_id=j.user_id
                    WHERE j.user_id=? AND j.status IN ('pending','retry')
                      AND j.cloud_approved=1 AND j.next_attempt_at<=?{event_filter}
                    ORDER BY j.priority DESC, e.occurred_at DESC,
                             j.created_at DESC LIMIT 1""",
                    tuple(parameters),
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return None
                claimed_id = row["event_id"]
                global_budget_defer_until = (
                    self._reserve_global_analysis_budget_locked(
                        current,
                        hourly_call_limit=global_hourly_call_limit,
                        daily_call_limit=global_daily_call_limit,
                    )
                )
                if global_budget_defer_until is not None:
                    self._defer_analysis_jobs_locked(
                        user_id,
                        current,
                        global_budget_defer_until,
                        "global_model_budget_deferred",
                        event_id=event_id,
                    )
                    self._connection.commit()
                    return None
                budget_defer_until = self._reserve_analysis_budget_locked(
                    user_id,
                    current,
                    hourly_call_limit=hourly_call_limit,
                    daily_call_limit=daily_call_limit,
                )
                if budget_defer_until is not None:
                    self._defer_analysis_jobs_locked(
                        user_id,
                        current,
                        budget_defer_until,
                        "analysis_budget_deferred",
                        event_id=event_id,
                    )
                    self._connection.commit()
                    return None
                cursor = self._connection.execute(
                    """UPDATE analysis_jobs
                    SET status='running', attempts=attempts+1, updated_at=?,
                        completed_at=NULL, last_reason=NULL
                    WHERE user_id=? AND event_id=?
                      AND status IN ('pending','retry')""",
                    (current_text, user_id, claimed_id),
                )
                if cursor.rowcount != 1:
                    self._connection.rollback()
                    return None
                job_row = self._connection.execute(
                    "SELECT * FROM analysis_jobs WHERE user_id=? AND event_id=?",
                    (user_id, claimed_id),
                ).fetchone()
                event_row = self._connection.execute(
                    "SELECT evidence_ref, payload_json FROM events WHERE user_id=? AND id=?",
                    (user_id, claimed_id),
                ).fetchone()
                if event_row is None:
                    self._connection.execute(
                        """UPDATE analysis_jobs SET status='no_intervention',
                        last_reason='event_missing', completed_at=?, updated_at=?
                        WHERE user_id=? AND event_id=?""",
                        (current_text, current_text, user_id, claimed_id),
                    )
                    self._connection.commit()
                    return None
                if any(
                    limit is not None
                    for limit in (
                        hourly_call_limit,
                        daily_call_limit,
                        global_hourly_call_limit,
                        global_daily_call_limit,
                    )
                ):
                    reservation = self._connection.execute(
                        """INSERT INTO analysis_model_reservations(
                          user_id,event_id,attempt,reserved_calls,actual_calls,
                          created_at,settled_at
                        ) VALUES(?,?,?,?,0,?,NULL)""",
                        (
                            user_id,
                            claimed_id,
                            int(job_row["attempts"]),
                            ANALYSIS_JOB_RESERVED_MODEL_CALLS,
                            current_text,
                        ),
                    )
                    reservation_id = int(reservation.lastrowid)
                claim_evidence_ref = event_row["evidence_ref"] or f"event:{claimed_id}"
                self._connection.execute(
                    """DELETE FROM advice
                    WHERE user_id=? AND evidence_ref=? AND status='provisional'""",
                    (user_id, claim_evidence_ref),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return AnalysisJobClaim(
            job=_analysis_job_from_row(job_row),
            event=Event.model_validate_json(event_row["payload_json"]),
            reservation_id=reservation_id,
        )

    def _defer_analysis_jobs_locked(
        self,
        user_id: str,
        current: datetime,
        defer_until: datetime,
        reason: str,
        *,
        event_id: UUID | str | None,
    ) -> None:
        parameters: list[Any] = [
            defer_until.isoformat(),
            _analysis_reason(reason),
            current.isoformat(),
            user_id,
            current.isoformat(),
        ]
        event_filter = ""
        if event_id is not None:
            event_filter = " AND event_id=?"
            parameters.append(str(event_id))
        self._connection.execute(
            f"""UPDATE analysis_jobs
            SET next_attempt_at=?, last_reason=?, updated_at=?
            WHERE user_id=? AND status IN ('pending','retry')
              AND next_attempt_at<=?{event_filter}""",
            tuple(parameters),
        )

    def _settle_inactive_analysis_reservations_locked(
        self,
        user_id: str | None,
        current: datetime,
    ) -> None:
        """Release unused capacity left by a completed or expired worker."""

        user_filter = ""
        parameters: list[Any] = [current.isoformat()]
        if user_id is not None:
            user_filter = " AND user_id=?"
            parameters.append(user_id)
        self._connection.execute(
            f"""UPDATE analysis_model_reservations
            SET settled_at=?
            WHERE settled_at IS NULL{user_filter}
              AND NOT EXISTS (
                SELECT 1 FROM analysis_jobs j
                WHERE j.user_id=analysis_model_reservations.user_id
                  AND j.event_id=analysis_model_reservations.event_id
                  AND j.attempts=analysis_model_reservations.attempt
                  AND j.status='running'
              )""",
            tuple(parameters),
        )

    def _reclaim_expired_analysis_leases_locked(self, current: datetime) -> int:
        """Return every expired cross-tenant worker lease to the retry queue."""

        current_text = current.isoformat()
        stale_before = (current - ANALYSIS_JOB_LEASE).isoformat()
        cursor = self._connection.execute(
            """UPDATE analysis_jobs
            SET status='retry', next_attempt_at=?,
                last_reason='worker_lease_expired',
                last_failure_reason=COALESCE(
                  last_failure_reason, 'worker_lease_expired'
                ),
                updated_at=?, completed_at=NULL
            WHERE status='running' AND updated_at<=?""",
            (current_text, current_text, stale_before),
        )
        return cursor.rowcount

    @staticmethod
    def _budget_defer_until(
        rows: list[tuple[int, datetime]],
        current: datetime,
        *,
        required_calls: int,
        hourly_call_limit: int,
        daily_call_limit: int,
    ) -> datetime | None:
        impossible_until: list[datetime] = []
        if required_calls > hourly_call_limit:
            impossible_until.append(current + timedelta(hours=1, seconds=1))
        if required_calls > daily_call_limit:
            impossible_until.append(current + timedelta(days=1, seconds=1))
        if impossible_until:
            return max(impossible_until)
        day_cutoff = current - timedelta(days=1)
        hour_cutoff = current - timedelta(hours=1)
        day_rows = [item for item in rows if item[1] > day_cutoff]
        hour_rows = [item for item in day_rows if item[1] > hour_cutoff]
        defer_until: list[datetime] = []
        for window_rows, limit, window in (
            (hour_rows, hourly_call_limit, timedelta(hours=1)),
            (day_rows, daily_call_limit, timedelta(days=1)),
        ):
            used = sum(calls for calls, _ in window_rows)
            required_release = used + required_calls - limit
            if required_release <= 0:
                continue
            released = 0
            for calls, used_at in window_rows:
                released += calls
                if released >= required_release:
                    defer_until.append(used_at + window + timedelta(seconds=1))
                    break
        return max(defer_until, default=None)

    def _reserve_global_analysis_budget_locked(
        self,
        current: datetime,
        *,
        hourly_call_limit: int | None,
        daily_call_limit: int | None,
    ) -> datetime | None:
        return self._reserve_global_model_budget_locked(
            current,
            required_calls=ANALYSIS_JOB_RESERVED_MODEL_CALLS,
            hourly_call_limit=hourly_call_limit,
            daily_call_limit=daily_call_limit,
        )

    def _reserve_global_model_budget_locked(
        self,
        current: datetime,
        *,
        required_calls: int,
        hourly_call_limit: int | None,
        daily_call_limit: int | None,
    ) -> datetime | None:
        if hourly_call_limit is None and daily_call_limit is None:
            return None
        hour_limit = max(0, int(hourly_call_limit or 10_000_000))
        day_limit = max(0, int(daily_call_limit or 10_000_000))
        day_cutoff = current - timedelta(days=1)
        cleanup_cutoff = (current - timedelta(days=8)).isoformat()
        # A worker may have died in another tenant or process. Reclaim every
        # expired job lease before settling reservations; otherwise its unused
        # worst-case reservation can block the global budget indefinitely.
        self._reclaim_expired_analysis_leases_locked(current)
        self._settle_inactive_analysis_reservations_locked(None, current)
        self._connection.execute(
            "DELETE FROM analysis_model_usage WHERE used_at<?",
            (cleanup_cutoff,),
        )
        self._connection.execute(
            """DELETE FROM analysis_model_reservations
            WHERE settled_at IS NOT NULL AND created_at<?""",
            (cleanup_cutoff,),
        )
        self._connection.execute(
            "DELETE FROM global_model_usage WHERE used_at<?",
            (cleanup_cutoff,),
        )
        rows = self._connection.execute(
            """SELECT calls,used_at FROM analysis_model_usage
            WHERE used_at>?
            UNION ALL
            SELECT CASE WHEN settled_at IS NULL
                        THEN reserved_calls ELSE actual_calls END AS calls,
                   created_at AS used_at
            FROM analysis_model_reservations WHERE created_at>?
            UNION ALL
            SELECT calls,used_at FROM global_model_usage WHERE used_at>?
            ORDER BY used_at""",
            (
                day_cutoff.isoformat(),
                day_cutoff.isoformat(),
                day_cutoff.isoformat(),
            ),
        ).fetchall()
        budget_rows = [
            (int(row["calls"]), _parse_utc(row["used_at"]))
            for row in rows
            if int(row["calls"]) > 0
        ]
        return self._budget_defer_until(
            budget_rows,
            current,
            required_calls=required_calls,
            hourly_call_limit=hour_limit,
            daily_call_limit=day_limit,
        )

    def _reserve_analysis_budget_locked(
        self,
        user_id: str,
        current: datetime,
        *,
        hourly_call_limit: int | None,
        daily_call_limit: int | None,
    ) -> datetime | None:
        return self._reserve_user_model_budget_locked(
            user_id,
            current,
            required_calls=ANALYSIS_JOB_RESERVED_MODEL_CALLS,
            hourly_call_limit=hourly_call_limit,
            daily_call_limit=daily_call_limit,
        )

    def _reserve_user_model_budget_locked(
        self,
        user_id: str,
        current: datetime,
        *,
        required_calls: int,
        hourly_call_limit: int | None,
        daily_call_limit: int | None,
    ) -> datetime | None:
        """Check one tenant's queued and direct calls in the current transaction."""

        if hourly_call_limit is None and daily_call_limit is None:
            return None
        hour_limit = max(0, int(hourly_call_limit or 10_000_000))
        day_limit = max(0, int(daily_call_limit or 10_000_000))
        day_cutoff = current - timedelta(days=1)
        cleanup_cutoff = (current - timedelta(days=8)).isoformat()
        self._settle_inactive_analysis_reservations_locked(user_id, current)
        self._connection.execute(
            "DELETE FROM analysis_model_usage WHERE used_at<?",
            (cleanup_cutoff,),
        )
        self._connection.execute(
            """DELETE FROM analysis_model_reservations
            WHERE settled_at IS NOT NULL AND created_at<?""",
            (cleanup_cutoff,),
        )
        legacy_rows = self._connection.execute(
            """SELECT calls,used_at FROM analysis_model_usage
            WHERE user_id=? AND used_at>? ORDER BY used_at""",
            (user_id, day_cutoff.isoformat()),
        ).fetchall()
        reservation_rows = self._connection.execute(
            """SELECT CASE WHEN settled_at IS NULL
                         THEN reserved_calls ELSE actual_calls END AS calls,
                      created_at AS used_at
            FROM analysis_model_reservations
            WHERE user_id=? AND created_at>? ORDER BY created_at""",
            (user_id, day_cutoff.isoformat()),
        ).fetchall()
        direct_rows = self._connection.execute(
            """SELECT calls,used_at FROM global_model_usage
            WHERE user_id=? AND used_at>? ORDER BY used_at""",
            (user_id, day_cutoff.isoformat()),
        ).fetchall()
        day_rows = [
            (int(row["calls"]), _parse_utc(row["used_at"]))
            for row in (*legacy_rows, *reservation_rows, *direct_rows)
            if int(row["calls"]) > 0
        ]
        day_rows.sort(key=lambda item: item[1])
        return self._budget_defer_until(
            day_rows,
            current,
            required_calls=required_calls,
            hourly_call_limit=hour_limit,
            daily_call_limit=day_limit,
        )

    def _advice_localization_budget_defer_until_locked(
        self,
        user_id: str,
        current: datetime,
    ) -> datetime | None:
        """Return the next durable per-tenant history-translation slot."""

        cutoff = current - timedelta(days=1)
        rows = self._connection.execute(
            """SELECT calls,used_at FROM global_model_usage
            WHERE user_id=? AND purpose='advice_translation' AND used_at>?
            ORDER BY used_at""",
            (user_id, cutoff.isoformat()),
        ).fetchall()
        usage = [
            (int(row["calls"]), _parse_utc(row["used_at"]))
            for row in rows
            if int(row["calls"]) > 0
        ]
        return self._budget_defer_until(
            usage,
            current,
            required_calls=1,
            hourly_call_limit=1_000_000,
            daily_call_limit=_advice_localization_daily_limit(),
        )

    def admit_direct_model_request(
        self,
        user_id: str,
        provider: str,
        *,
        now: datetime | None = None,
        requests_per_window: int | None = None,
        rate_window_seconds: int | None = None,
        user_concurrency_limit: int | None = None,
        global_concurrency_limit: int | None = None,
        lease_seconds: int | None = None,
    ) -> DirectModelAdmissionDecision:
        """Atomically admit a direct model request and, when needed, lease capacity.

        The sliding request window and active leases live in SQLite so separate
        service processes and restarts share the same limits. Every provider is
        rate limited. The deterministic template route does not consume a
        concurrency slot; all executable model providers do, including Ollama,
        OpenAI, and Codex CLI.
        """

        normalized_user_id = str(user_id).strip()
        if not normalized_user_id:
            raise ValueError("user_id is required")
        normalized_provider = str(provider).strip().casefold()[:80] or "unknown"
        current = _as_utc(now)

        if requests_per_window is None:
            requests_per_window = _environment_int(
                "MOUCHEN_DIRECT_MODEL_REQUESTS_PER_WINDOW",
                10,
                minimum=1,
                maximum=1_000_000,
            )
        else:
            requests_per_window = max(1, min(1_000_000, int(requests_per_window)))
        if rate_window_seconds is None:
            rate_window_seconds = _environment_int(
                "MOUCHEN_DIRECT_MODEL_RATE_WINDOW_SECONDS",
                60,
                minimum=1,
                maximum=86_400,
            )
        else:
            rate_window_seconds = max(1, min(86_400, int(rate_window_seconds)))
        if user_concurrency_limit is None:
            user_concurrency_limit = _environment_int(
                "MOUCHEN_DIRECT_MODEL_USER_CONCURRENCY",
                1,
                minimum=1,
                maximum=10_000,
            )
        else:
            user_concurrency_limit = max(
                1, min(10_000, int(user_concurrency_limit))
            )
        if global_concurrency_limit is None:
            global_concurrency_limit = _environment_int(
                "MOUCHEN_DIRECT_MODEL_GLOBAL_CONCURRENCY",
                4,
                minimum=1,
                maximum=100_000,
            )
        else:
            global_concurrency_limit = max(
                1, min(100_000, int(global_concurrency_limit))
            )
        if lease_seconds is None:
            lease_seconds = _environment_int(
                "MOUCHEN_DIRECT_MODEL_LEASE_SECONDS",
                600,
                minimum=1,
                maximum=86_400,
            )
        else:
            lease_seconds = max(1, min(86_400, int(lease_seconds)))

        window_start = current - timedelta(seconds=rate_window_seconds)
        lease_expires_at = current + timedelta(seconds=lease_seconds)

        def rejection(reason_code: str, retry_at: datetime) -> DirectModelAdmissionDecision:
            retry_after = max(
                1,
                int((retry_at - current).total_seconds() + 0.999),
            )
            return DirectModelAdmissionDecision(
                False,
                retry_after=retry_after,
                retry_at=retry_at,
                reason_code=reason_code,
            )

        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                # Keep both durable admission tables bounded while retaining
                # everything that can still influence the current decision.
                self._connection.execute(
                    "DELETE FROM direct_model_requests WHERE requested_at<=?",
                    (window_start.isoformat(),),
                )
                self._connection.execute(
                    """UPDATE direct_model_leases SET released_at=expires_at
                    WHERE released_at IS NULL AND expires_at<=?""",
                    (current.isoformat(),),
                )
                lease_history_cutoff = current - timedelta(
                    seconds=max(rate_window_seconds, lease_seconds)
                )
                self._connection.execute(
                    """DELETE FROM direct_model_leases
                    WHERE released_at IS NOT NULL AND released_at<=?""",
                    (lease_history_cutoff.isoformat(),),
                )

                rate_row = self._connection.execute(
                    """SELECT COUNT(*) AS request_count,
                              MIN(requested_at) AS oldest_request
                    FROM direct_model_requests
                    WHERE user_id=? AND requested_at>?""",
                    (normalized_user_id, window_start.isoformat()),
                ).fetchone()
                if int(rate_row["request_count"]) >= requests_per_window:
                    oldest_request = _parse_utc(rate_row["oldest_request"])
                    retry_at = oldest_request + timedelta(seconds=rate_window_seconds)
                    self._connection.commit()
                    return rejection("direct_model_rate_limit_exhausted", retry_at)

                # The request itself consumes sliding-window capacity even if
                # it cannot immediately obtain a concurrency lease. This makes
                # retry storms self-limiting without charging a model budget.
                self._connection.execute(
                    """INSERT INTO direct_model_requests(
                      user_id,provider,requested_at
                    ) VALUES(?,?,?)""",
                    (normalized_user_id, normalized_provider, current.isoformat()),
                )

                if normalized_provider == "template":
                    self._connection.commit()
                    return DirectModelAdmissionDecision(True)

                user_row = self._connection.execute(
                    """SELECT COUNT(*) AS active_count,
                              MIN(expires_at) AS earliest_expiry
                    FROM direct_model_leases
                    WHERE user_id=? AND released_at IS NULL AND expires_at>?""",
                    (normalized_user_id, current.isoformat()),
                ).fetchone()
                if int(user_row["active_count"]) >= user_concurrency_limit:
                    retry_at = _parse_utc(user_row["earliest_expiry"])
                    self._connection.commit()
                    return rejection(
                        "direct_model_user_concurrency_exhausted",
                        retry_at,
                    )

                global_row = self._connection.execute(
                    """SELECT COUNT(*) AS active_count,
                              MIN(expires_at) AS earliest_expiry
                    FROM direct_model_leases
                    WHERE released_at IS NULL AND expires_at>?""",
                    (current.isoformat(),),
                ).fetchone()
                if int(global_row["active_count"]) >= global_concurrency_limit:
                    retry_at = _parse_utc(global_row["earliest_expiry"])
                    self._connection.commit()
                    return rejection(
                        "direct_model_global_concurrency_exhausted",
                        retry_at,
                    )

                lease_id = str(uuid4())
                self._connection.execute(
                    """INSERT INTO direct_model_leases(
                      id,user_id,provider,acquired_at,expires_at,released_at
                    ) VALUES(?,?,?,?,?,NULL)""",
                    (
                        lease_id,
                        normalized_user_id,
                        normalized_provider,
                        current.isoformat(),
                        lease_expires_at.isoformat(),
                    ),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return DirectModelAdmissionDecision(True, lease_id=lease_id)

    def release_direct_model_lease(
        self,
        lease_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Release a direct-model capacity lease exactly once."""

        normalized_lease_id = str(lease_id).strip()
        if not normalized_lease_id:
            return False
        released_at = _as_utc(now).isoformat()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                cursor = self._connection.execute(
                    """UPDATE direct_model_leases SET released_at=?
                    WHERE id=? AND released_at IS NULL""",
                    (released_at, normalized_lease_id),
                )
                released = cursor.rowcount == 1
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return released

    def reserve_global_direct_model_call(
        self,
        user_id: str,
        provider: str,
        purpose: str,
        *,
        now: datetime | None = None,
        hourly_call_limit: int | None = None,
        daily_call_limit: int | None = None,
    ) -> GlobalModelBudgetDecision:
        """Atomically charge one direct paid-model attempt across all tenants."""

        if hourly_call_limit is None:
            hourly_call_limit = _environment_int(
                "MOUCHEN_GLOBAL_MODEL_CALLS_PER_HOUR",
                120,
                minimum=1,
                maximum=1_000_000,
            )
        if daily_call_limit is None:
            daily_call_limit = _environment_int(
                "MOUCHEN_GLOBAL_MODEL_CALLS_PER_DAY",
                600,
                minimum=1,
                maximum=10_000_000,
            )
        tenant_hourly_call_limit = _environment_int(
            "MOUCHEN_ANALYSIS_MODEL_CALLS_PER_HOUR",
            12,
            minimum=1,
            maximum=1_000_000,
        )
        tenant_daily_call_limit = _environment_int(
            "MOUCHEN_ANALYSIS_MODEL_CALLS_PER_DAY",
            288,
            minimum=1,
            maximum=10_000_000,
        )
        current = _as_utc(now)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                if str(purpose).strip() == "advice_translation":
                    localization_defer_until = (
                        self._advice_localization_budget_defer_until_locked(
                            user_id,
                            current,
                        )
                    )
                    if localization_defer_until is not None:
                        self._connection.commit()
                        return GlobalModelBudgetDecision(
                            False,
                            retry_after=max(
                                1,
                                int(
                                    (
                                        localization_defer_until - current
                                    ).total_seconds()
                                    + 0.999
                                ),
                            ),
                            retry_at=localization_defer_until,
                            reason_code=(
                                "advice_translation_daily_budget_exhausted"
                            ),
                        )
                tenant_defer_until = self._reserve_user_model_budget_locked(
                    user_id,
                    current,
                    required_calls=1,
                    hourly_call_limit=tenant_hourly_call_limit,
                    daily_call_limit=tenant_daily_call_limit,
                )
                if tenant_defer_until is not None:
                    self._connection.commit()
                    return GlobalModelBudgetDecision(
                        False,
                        retry_after=max(
                            1,
                            int((tenant_defer_until - current).total_seconds() + 0.999),
                        ),
                        retry_at=tenant_defer_until,
                        reason_code="user_model_budget_exhausted",
                    )
                defer_until = self._reserve_global_model_budget_locked(
                    current,
                    required_calls=1,
                    hourly_call_limit=hourly_call_limit,
                    daily_call_limit=daily_call_limit,
                )
                if defer_until is not None:
                    self._connection.commit()
                    retry_after = max(
                        1,
                        int((defer_until - current).total_seconds() + 0.999),
                    )
                    return GlobalModelBudgetDecision(
                        False,
                        retry_after=retry_after,
                        retry_at=defer_until,
                        reason_code="global_model_budget_exhausted",
                    )
                self._connection.execute(
                    """INSERT INTO global_model_usage(
                      user_id,provider,purpose,calls,used_at
                    ) VALUES(?,?,?,?,?)""",
                    (
                        str(user_id).strip(),
                        str(provider).strip().casefold()[:80] or "unknown",
                        str(purpose).strip()[:160] or "unspecified",
                        1,
                        current.isoformat(),
                    ),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return GlobalModelBudgetDecision(True)

    def global_model_calls_used(self, *, since: datetime) -> int:
        """Expose global direct calls plus queued actual/active reservations."""

        cutoff = _as_utc(since).isoformat()
        with self._lock:
            row = self._connection.execute(
                """SELECT COALESCE(SUM(calls),0) AS calls FROM (
                  SELECT calls FROM analysis_model_usage WHERE used_at>?
                  UNION ALL
                  SELECT CASE WHEN settled_at IS NULL
                              THEN reserved_calls ELSE actual_calls END AS calls
                  FROM analysis_model_reservations WHERE created_at>?
                  UNION ALL
                  SELECT calls FROM global_model_usage WHERE used_at>?
                )""",
                (cutoff, cutoff, cutoff),
            ).fetchone()
        return int(row["calls"])

    def analysis_model_calls_used(
        self,
        user_id: str,
        *,
        since: datetime,
    ) -> int:
        """Expose legacy usage plus active reservations or settled actual calls."""

        with self._lock:
            cutoff = _as_utc(since).isoformat()
            legacy = self._connection.execute(
                """SELECT COALESCE(SUM(calls),0) AS calls
                FROM analysis_model_usage WHERE user_id=? AND used_at>?""",
                (user_id, cutoff),
            ).fetchone()
            reservations = self._connection.execute(
                """SELECT COALESCE(SUM(
                         CASE WHEN settled_at IS NULL
                              THEN reserved_calls ELSE actual_calls END
                       ),0) AS calls
                FROM analysis_model_reservations
                WHERE user_id=? AND created_at>?""",
                (user_id, cutoff),
            ).fetchone()
            direct = self._connection.execute(
                """SELECT COALESCE(SUM(calls),0) AS calls
                FROM global_model_usage WHERE user_id=? AND used_at>?""",
                (user_id, cutoff),
            ).fetchone()
        return int(legacy["calls"]) + int(reservations["calls"]) + int(direct["calls"])

    def record_analysis_model_call(self, reservation_id: int) -> None:
        """Persist one provider attempt before allowing it to start."""

        with self._lock:
            cursor = self._connection.execute(
                """UPDATE analysis_model_reservations
                SET actual_calls=actual_calls+1
                WHERE id=? AND settled_at IS NULL
                  AND actual_calls<reserved_calls""",
                (reservation_id,),
            )
            self._connection.commit()
        if cursor.rowcount != 1:
            raise RuntimeError("analysis model reservation unavailable")

    def settle_analysis_model_reservation(
        self,
        reservation_id: int,
        user_id: str,
        event_id: UUID | str,
        *,
        attempt: int,
        now: datetime | None = None,
    ) -> bool:
        """Replace a worst-case reservation with its persisted actual calls."""

        current_datetime = _as_utc(now)
        current = current_datetime.isoformat()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                before = self._connection.execute(
                    """SELECT reserved_calls,actual_calls,settled_at
                    FROM analysis_model_reservations
                    WHERE id=? AND user_id=? AND event_id=? AND attempt=?""",
                    (reservation_id, user_id, str(event_id), attempt),
                ).fetchone()
                cursor = self._connection.execute(
                    """UPDATE analysis_model_reservations
                    SET settled_at=COALESCE(settled_at, ?)
                    WHERE id=? AND user_id=? AND event_id=? AND attempt=?""",
                    (
                        current,
                        reservation_id,
                        user_id,
                        str(event_id),
                        attempt,
                    ),
                )
                if (
                    cursor.rowcount == 1
                    and before is not None
                    and before["settled_at"] is None
                    and int(before["actual_calls"]) < int(before["reserved_calls"])
                ):
                    # Tenant capacity only belongs to the tenant that released
                    # it. Global capacity is shared, but waking one best job is
                    # enough; each subsequent settlement can wake the next and
                    # avoids a cross-tenant thundering herd.
                    self._connection.execute(
                        """UPDATE analysis_jobs
                        SET next_attempt_at=?, updated_at=?
                        WHERE user_id=? AND status IN ('pending','retry')
                          AND last_reason='analysis_budget_deferred'
                          AND next_attempt_at>?""",
                        (current, current, user_id, current),
                    )
                    self._connection.execute(
                        """UPDATE analysis_jobs
                        SET next_attempt_at=?, updated_at=?
                        WHERE (user_id,event_id) IN (
                          SELECT user_id,event_id FROM analysis_jobs
                          WHERE status IN ('pending','retry')
                            AND last_reason='global_model_budget_deferred'
                            AND next_attempt_at>?
                          ORDER BY priority DESC, next_attempt_at, created_at,
                                   user_id, event_id
                          LIMIT 1
                        )""",
                        (current, current, current),
                    )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return cursor.rowcount == 1

    def complete_analysis_job(
        self,
        user_id: str,
        event_id: UUID | str,
        status: str,
        reason: str,
        *,
        attempt: int,
        failure_reason: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        if status not in {"no_intervention", "published"}:
            raise ValueError("invalid terminal analysis job status")
        current = _as_utc(now).isoformat()
        safe_reason = _analysis_reason(reason)
        safe_failure_reason = (
            _analysis_reason(failure_reason) if failure_reason is not None else None
        )
        with self._lock:
            cursor = self._connection.execute(
                """UPDATE analysis_jobs
                SET status=?, last_reason=?,
                    last_failure_reason=?,
                    updated_at=?, completed_at=?
                WHERE user_id=? AND event_id=? AND status='running' AND attempts=?""",
                (
                    status,
                    safe_reason,
                    safe_failure_reason,
                    current,
                    current,
                    user_id,
                    str(event_id),
                    attempt,
                ),
            )
            self._connection.commit()
        return cursor.rowcount == 1

    def retry_analysis_job(
        self,
        user_id: str,
        event_id: UUID | str,
        reason: str,
        *,
        attempt: int,
        delay_seconds: float = 0,
        now: datetime | None = None,
    ) -> bool:
        current = _as_utc(now)
        due = current + timedelta(seconds=max(0.0, min(3_600.0, delay_seconds)))
        with self._lock:
            safe_reason = _analysis_reason(reason)
            cursor = self._connection.execute(
                """UPDATE analysis_jobs
                SET status='retry', next_attempt_at=?, last_reason=?,
                    last_failure_reason=?, updated_at=?, completed_at=NULL
                WHERE user_id=? AND event_id=? AND status='running' AND attempts=?""",
                (
                    due.isoformat(),
                    safe_reason,
                    safe_reason,
                    current.isoformat(),
                    user_id,
                    str(event_id),
                    attempt,
                ),
            )
            self._connection.commit()
        return cursor.rowcount == 1

    def get_analysis_job(
        self,
        user_id: str,
        event_id: UUID | str,
    ) -> AnalysisJob | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM analysis_jobs WHERE user_id=? AND event_id=?",
                (user_id, str(event_id)),
            ).fetchone()
        return _analysis_job_from_row(row) if row else None

    def analysis_status(self, user_id: str) -> dict[str, Any]:
        """Return queue telemetry without exposing source events or model context."""

        known_statuses = (
            "pending",
            "retry",
            "running",
            "published",
            "no_intervention",
        )
        with self._lock:
            rows = self._connection.execute(
                """SELECT status, COUNT(*) AS count
                FROM analysis_jobs WHERE user_id=? GROUP BY status""",
                (user_id,),
            ).fetchall()
            latest = self._connection.execute(
                """SELECT status, last_reason, last_failure_reason, updated_at
                FROM analysis_jobs WHERE user_id=?
                ORDER BY updated_at DESC, event_id DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
            waiting_reason_rows = self._connection.execute(
                """SELECT COALESCE(last_reason, '') AS reason, COUNT(*) AS count
                FROM analysis_jobs
                WHERE user_id=? AND status IN ('pending','retry','running')
                GROUP BY COALESCE(last_reason, '')""",
                (user_id,),
            ).fetchall()

        observed = {str(row["status"]): int(row["count"]) for row in rows}
        counts = {status: observed.get(status, 0) for status in known_statuses}
        waiting_reasons = {
            str(row["reason"]): int(row["count"])
            for row in waiting_reason_rows
            if row["reason"]
        }
        return {
            "counts": counts,
            "waiting": counts["pending"] + counts["retry"] + counts["running"],
            "waiting_reasons": waiting_reasons,
            "last_status": latest["status"] if latest else None,
            "last_reason": latest["last_reason"] if latest else None,
            "last_updated_at": latest["updated_at"] if latest else None,
            "last_failure_reason": latest["last_failure_reason"] if latest else None,
        }

    def recheck_budget_deferred_analysis_jobs(
        self,
        *,
        now: datetime | None = None,
    ) -> int:
        """Make persisted budget deferrals due so current limits are re-evaluated."""

        current = _as_utc(now).isoformat()
        with self._lock:
            cursor = self._connection.execute(
                """UPDATE analysis_jobs
                SET next_attempt_at=?, updated_at=?
                WHERE status IN ('pending','retry')
                  AND last_reason IN (
                    'analysis_budget_deferred','global_model_budget_deferred'
                  )
                  AND next_attempt_at>?""",
                (current, current, current),
            )
            self._connection.commit()
        return cursor.rowcount

    def next_due_analysis_user(self, *, now: datetime | None = None) -> str | None:
        current = _as_utc(now)
        stale_before = (current - ANALYSIS_JOB_LEASE).isoformat()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._retire_stale_analysis_jobs_locked(None, current)
                row = self._connection.execute(
                    """SELECT j.user_id FROM analysis_jobs j
                    JOIN events e ON e.id=j.event_id AND e.user_id=j.user_id
                    WHERE j.cloud_approved=1 AND (
                      (j.status IN ('pending','retry') AND j.next_attempt_at<=?)
                      OR (j.status='running' AND j.updated_at<=?)
                    )
                    ORDER BY CASE WHEN j.status='running' THEN 0 ELSE 1 END,
                             j.priority DESC, e.occurred_at DESC,
                             j.created_at DESC
                    LIMIT 1""",
                    (current.isoformat(), stale_before),
                ).fetchone()
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return row["user_id"] if row else None

    def analysis_backfill_candidates(
        self,
        user_id: str,
        *,
        limit: int = 25,
    ) -> list[Event]:
        """Return newest high-value supported events without mutating the queue."""

        from .domain.problem_signals import SUPPORTED_EVENT_TYPES, event_text

        bounded_limit = max(1, min(ANALYSIS_BACKFILL_HARD_LIMIT, int(limit)))
        scan_limit = min(2_000, max(200, bounded_limit * 20))
        placeholders = ",".join("?" for _ in SUPPORTED_EVENT_TYPES)
        parameters: list[Any] = [user_id, *sorted(SUPPORTED_EVENT_TYPES), scan_limit]
        with self._lock:
            rows = self._connection.execute(
                f"""SELECT e.payload_json FROM events e
                LEFT JOIN analysis_jobs j
                  ON j.event_id=e.id AND j.user_id=e.user_id
                WHERE e.user_id=? AND e.type IN ({placeholders}) AND j.event_id IS NULL
                ORDER BY e.occurred_at DESC LIMIT ?""",
                tuple(parameters),
            ).fetchall()
        selected: list[Event] = []
        for row in rows:
            event = Event.model_validate_json(row["payload_json"])
            text = event_text(event)
            if event.type in {"thought.note", "shared.text"}:
                if len(text) < 20:
                    continue
            elif not (text and _HIGH_VALUE_BACKFILL_PATTERN.search(text)):
                continue
            selected.append(event)
            if len(selected) == bounded_limit:
                break
        return selected

    def recent_events(self, user_id: str, limit: int = 100) -> list[Event]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM events WHERE user_id=? ORDER BY occurred_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [Event.model_validate_json(row["payload_json"]) for row in rows]

    def events_since(self, user_id: str, event_type: str, since: datetime) -> list[Event]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT payload_json FROM events
                WHERE user_id=? AND type=? AND occurred_at>=? ORDER BY occurred_at DESC""",
                (user_id, event_type, since.isoformat()),
            ).fetchall()
        return [Event.model_validate_json(row["payload_json"]) for row in rows]

    def set_charter(
        self,
        user_id: str,
        domain: str,
        max_level: AdviceLevel,
        redline: bool,
        *,
        max_charters: int | None = None,
    ) -> None:
        self.ensure_user(user_id)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    "SELECT 1 FROM charters WHERE user_id=? AND domain=?",
                    (user_id, domain),
                ).fetchone()
                if existing is None and max_charters is not None:
                    count_row = self._connection.execute(
                        "SELECT COUNT(*) AS count FROM charters WHERE user_id=?",
                        (user_id,),
                    ).fetchone()
                    if int(count_row["count"]) >= max(0, int(max_charters)):
                        raise CharterQuotaExceeded("charter storage quota exceeded")
                self._connection.execute(
                    """INSERT INTO charters VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, domain) DO UPDATE SET
                      max_level=excluded.max_level,
                      redline_authorized=excluded.redline_authorized,
                      updated_at=excluded.updated_at""",
                    (user_id, domain, int(max_level), int(redline), _now()),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def get_charter(self, user_id: str, domain: str) -> tuple[AdviceLevel, bool]:
        with self._lock:
            row = self._connection.execute(
                "SELECT max_level, redline_authorized FROM charters WHERE user_id=? AND domain=?",
                (user_id, domain),
            ).fetchone()
        if not row:
            return AdviceLevel.L2, False
        return AdviceLevel(row["max_level"]), bool(row["redline_authorized"])

    def topic_key_for(self, advice: AdviceRecord | Any) -> str:
        return _canonical_topic_key(advice)

    def active_duplicate(self, user_id: str, topic_key: str) -> bool:
        self._withdraw_expired_active(user_id)
        with self._lock:
            row = self._connection.execute(
                """SELECT 1 FROM advice
                WHERE user_id=? AND (topic_key=? OR (topic_key IS NULL AND dedupe_key=?))
                  AND status IN ('active','provisional','adopted')
                LIMIT 1""",
                (user_id, topic_key, topic_key),
            ).fetchone()
        return row is not None

    def _withdraw_expired_active(
        self,
        user_id: str,
        now: datetime | None = None,
    ) -> int:
        """Release expired active advice without changing resolved user decisions."""

        cutoff = now or datetime.now(timezone.utc)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        else:
            cutoff = cutoff.astimezone(timezone.utc)
        changed = 0
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, payload_json FROM advice WHERE user_id=? AND status='active'",
                (user_id,),
            ).fetchall()
            for row in rows:
                record = AdviceRecord.model_validate_json(row["payload_json"])
                deadline = record.prediction.deadline
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                else:
                    deadline = deadline.astimezone(timezone.utc)
                if deadline > cutoff:
                    continue

                # The status column is the duplicate gate while payload_json is the
                # API source. Repair a pre-existing mismatch by preserving the
                # non-active payload status; otherwise withdraw only expired ACTIVE.
                if record.status == AdviceStatus.ACTIVE:
                    target_status = AdviceStatus.WITHDRAWN
                    record = record.model_copy(update={"status": target_status})
                else:
                    target_status = record.status
                cursor = self._connection.execute(
                    """UPDATE advice SET status=?, payload_json=?
                    WHERE id=? AND user_id=? AND status='active'""",
                    (
                        target_status.value,
                        record.model_dump_json(),
                        row["id"],
                        user_id,
                    ),
                )
                changed += cursor.rowcount
            if changed:
                self._connection.commit()
        return changed

    def insert_advice(
        self,
        advice: AdviceRecord,
        *,
        enforce_publish_limits: bool = True,
    ) -> AdviceRecord:
        if (
            not (advice.adopted_expected_result or "").strip()
            or advice.adopted_confidence is None
        ):
            raise ValueError("new advice requires an adopted-result prediction")
        topic_key = _canonical_topic_key(advice)
        advice = advice.model_copy(update={"topic_key": topic_key})
        evidence_ref = self._advice_evidence_ref(advice)
        now = _as_utc(None)
        publishing = advice.status == AdviceStatus.ACTIVE
        published_at = now.isoformat() if publishing else None
        with self._lock:
            try:
                # The frequency check and the insert share one write transaction
                # so two concurrent publishes cannot both pass the same quota.
                self._connection.execute("BEGIN IMMEDIATE")
                self._assert_account_writable_locked(advice.user_id)
                if publishing and enforce_publish_limits:
                    block = self._advice_publish_block_locked(advice, now)
                    if block is not None:
                        raise PreferenceLimitExceeded(block)
                self._connection.execute(
                    """INSERT INTO advice(
                      id,user_id,domain,level,dedupe_key,topic_key,evidence_ref,status,delivery,
                      prediction_confidence,prediction_deadline,payload_json,created_at,
                      goal_id,published_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(advice.id), advice.user_id, advice.domain,
                        int(advice.effective_level), advice.dedupe_key, topic_key, evidence_ref,
                        advice.status.value, advice.delivery,
                        advice.prediction.confidence, advice.prediction.deadline.isoformat(),
                        advice.model_dump_json(), advice.created_at.isoformat(),
                        str(advice.goal_id), published_at,
                    ),
                )
                if advice.status == AdviceStatus.ACTIVE:
                    self._insert_attention_row_locked(advice)
                self._connection.commit()
            except sqlite3.IntegrityError as exc:
                self._connection.rollback()
                duplicate = self._connection.execute(
                    """SELECT 1 FROM advice
                    WHERE user_id=? AND evidence_ref=?
                      AND status IN ('provisional','active','adopted') LIMIT 1""",
                    (advice.user_id, evidence_ref),
                ).fetchone()
                if duplicate is not None:
                    raise AdviceConflict(
                        "nonterminal advice already exists for this evidence"
                    ) from exc
                duplicate_topic = self._connection.execute(
                    """SELECT 1 FROM advice
                    WHERE user_id=? AND topic_key=?
                      AND status IN ('provisional','active','adopted') LIMIT 1""",
                    (advice.user_id, topic_key),
                ).fetchone()
                if duplicate_topic is not None:
                    raise AdviceConflict(
                        "nonterminal advice already exists for this topic"
                    ) from exc
                raise
            except BaseException:
                self._connection.rollback()
                raise
        return advice

    def _advice_evidence_ref(self, advice: AdviceRecord) -> str:
        evidence = advice.evidence[0]
        with self._lock:
            row = self._connection.execute(
                "SELECT evidence_ref FROM events WHERE user_id=? AND id=?",
                (advice.user_id, str(evidence.event_id)),
            ).fetchone()
        if row is not None and row["evidence_ref"]:
            return str(row["evidence_ref"])
        return f"event:{evidence.event_id}"

    def update_advice(self, advice: AdviceRecord) -> AdviceRecord:
        topic_key = _canonical_topic_key(advice)
        advice = advice.model_copy(update={"topic_key": topic_key})
        current = _now()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                cursor = self._connection.execute(
                    """UPDATE advice SET level=?, prediction_confidence=?, prediction_deadline=?,
                    topic_key=?,payload_json=? WHERE id=? AND user_id=?""",
                    (
                        int(advice.effective_level), advice.prediction.confidence,
                        advice.prediction.deadline.isoformat(), topic_key,
                        advice.model_dump_json(), str(advice.id), advice.user_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("advice not found")
                account = self._connection.execute(
                    "SELECT locale FROM users WHERE id=? AND is_active=1",
                    (advice.user_id,),
                ).fetchone()
                if account is not None and account["locale"] == "en-US":
                    self._upsert_advice_localization_locked(
                        advice.user_id,
                        advice,
                        "en-US",
                        current,
                    )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return advice

    def promote_advice(
        self,
        advice: AdviceRecord,
        *,
        enforce_publish_limits: bool = True,
    ) -> AdviceRecord:
        """Atomically make a reviewed provisional recommendation visible.

        Promotion IS the publish moment for L3+ advice, so the same frequency
        gate runs here inside the same BEGIN IMMEDIATE transaction; a blocked
        promotion raises before anything becomes active (never insert-then-
        withdraw), leaving the provisional row for the caller to discard.
        """

        if (
            not (advice.adopted_expected_result or "").strip()
            or advice.adopted_confidence is None
        ):
            raise ValueError("reviewed advice requires an adopted-result prediction")
        topic_key = _canonical_topic_key(advice)
        active = advice.model_copy(
            update={"status": AdviceStatus.ACTIVE, "topic_key": topic_key}
        )
        now = _as_utc(None)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                if enforce_publish_limits:
                    block = self._advice_publish_block_locked(active, now)
                    if block is not None:
                        raise PreferenceLimitExceeded(block)
                cursor = self._connection.execute(
                    """UPDATE advice SET
                      level=?,topic_key=?,status='active',delivery=?,prediction_confidence=?,
                      prediction_deadline=?,payload_json=?,published_at=?
                    WHERE id=? AND user_id=? AND status='provisional'""",
                    (
                        int(active.effective_level),
                        topic_key,
                        active.delivery,
                        active.prediction.confidence,
                        active.prediction.deadline.isoformat(),
                        active.model_dump_json(),
                        now.isoformat(),
                        str(active.id),
                        active.user_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("provisional advice not found")
                self._insert_attention_row_locked(active)
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return active

    def claim_advice_attention(
        self,
        user_id: str,
        device_id: str,
        *,
        platform: str | None = None,
        app_version: str | None = None,
        now: datetime | None = None,
    ) -> AdviceAttentionClaim | None:
        """Atomically claim the next due notification across every device."""

        current = _as_utc(now)
        lease_expires_at = current + ATTENTION_CLAIM_LEASE
        token = uuid4()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    "INSERT OR IGNORE INTO users(id,created_at) VALUES(?,?)",
                    (user_id, current.isoformat()),
                )
                known_device = self._connection.execute(
                    "SELECT 1 FROM devices WHERE user_id=? AND device_id=?",
                    (user_id, device_id),
                ).fetchone()
                if known_device is None:
                    device_limit = _environment_int(
                        "MOUCHEN_MAX_DEVICES_PER_USER",
                        50,
                        minimum=1,
                        maximum=1_000,
                    )
                    device_count = self._connection.execute(
                        "SELECT COUNT(*) AS count FROM devices WHERE user_id=?",
                        (user_id,),
                    ).fetchone()
                    if int(device_count["count"]) >= device_limit:
                        raise DeviceQuotaExceeded("device storage quota exceeded")
                self._connection.execute(
                    """INSERT INTO devices(
                      user_id,device_id,platform,app_version,created_at,last_seen_at
                    ) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(user_id,device_id) DO UPDATE SET
                      platform=COALESCE(excluded.platform,devices.platform),
                      app_version=COALESCE(excluded.app_version,devices.app_version),
                      last_seen_at=excluded.last_seen_at""",
                    (
                        user_id,
                        device_id,
                        platform,
                        app_version,
                        current.isoformat(),
                        current.isoformat(),
                    ),
                )
                self._ensure_attention_rows_locked(current)
                self._sweep_attention_locked(user_id, current)
                # Pausing is an output master switch, including advice that was
                # published before the preference changed. It is checked in
                # the same transaction as claim, so a blocked reminder spends
                # no global delivery permit and becomes claimable after resume.
                if self._effective_preference_locked(user_id, None).paused:
                    self._connection.commit()
                    return None
                rows = self._connection.execute(
                    """SELECT aa.advice_id,aa.delivery_count,aa.handling_kind,
                              a.payload_json
                    FROM advice_attention aa JOIN advice a
                      ON a.id=aa.advice_id AND a.user_id=aa.user_id
                    WHERE aa.user_id=? AND a.status='active'
                      AND a.delivery='immediate'
                      AND aa.state IN ('pending','waiting')
                      AND aa.delivery_count<2
                      AND (aa.next_eligible_at IS NULL OR aa.next_eligible_at<=?)
                    ORDER BY a.level DESC,a.created_at,aa.advice_id
                    """,
                    (user_id, current.isoformat()),
                ).fetchall()
                row = None
                advice = None
                for candidate_row in rows:
                    candidate_advice = AdviceRecord.model_validate_json(
                        candidate_row["payload_json"]
                    )
                    if self._effective_preference_locked(
                        user_id, candidate_advice.goal_id
                    ).paused:
                        continue
                    row = candidate_row
                    advice = candidate_advice
                    break
                if row is None:
                    self._connection.commit()
                    return None
                delivery_number = int(row["delivery_count"]) + 1
                assert advice is not None
                deadline = _as_utc(advice.prediction.deadline)
                if deadline <= current:
                    # Defensive guard; the sweep above normally performs this
                    # transition before the candidate query.
                    self._expire_prediction_deadlines_locked(user_id, current)
                    self._connection.commit()
                    return None
                next_eligible_at = (
                    min(current + ATTENTION_REMINDER_INTERVAL, deadline)
                    if delivery_number == 1
                    else None
                )
                auto_close_at = (
                    min(current + ATTENTION_REMINDER_INTERVAL, deadline)
                    if delivery_number == 2 and row["handling_kind"] != "later"
                    else None
                )
                cursor = self._connection.execute(
                    """UPDATE advice_attention SET
                      state='claimed',delivery_count=?,
                      next_eligible_at=?,auto_close_at=?,
                      claim_token=?,claim_device_id=?,claim_expires_at=?,updated_at=?
                    WHERE advice_id=? AND user_id=?
                      AND state IN ('pending','waiting') AND delivery_count=?""",
                    (
                        delivery_number,
                        next_eligible_at.isoformat() if next_eligible_at else None,
                        auto_close_at.isoformat() if auto_close_at else None,
                        str(token),
                        device_id,
                        lease_expires_at.isoformat(),
                        current.isoformat(),
                        row["advice_id"],
                        user_id,
                        int(row["delivery_count"]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise AttentionConflict("attention was claimed concurrently")
                self._connection.execute(
                    """INSERT INTO advice_attention_attempts(
                      claim_token,advice_id,user_id,device_id,delivery_number,state,
                      claimed_at,lease_expires_at
                    ) VALUES(?,?,?,?,?,'claimed',?,?)""",
                    (
                        str(token),
                        row["advice_id"],
                        user_id,
                        device_id,
                        delivery_number,
                        current.isoformat(),
                        lease_expires_at.isoformat(),
                    ),
                )
                self._connection.commit()
                return AdviceAttentionClaim(
                    claim_token=token,
                    lease_expires_at=lease_expires_at,
                    delivery_number=delivery_number,
                    advice=advice,
                )
            except Exception:
                self._connection.rollback()
                raise

    def complete_advice_attention(
        self,
        user_id: str,
        advice_id: UUID | str,
        device_id: str,
        claim_token: UUID | str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Confirm display for a permit that was already consumed at claim."""

        current = _as_utc(now)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                attempt = self._connection.execute(
                    """SELECT * FROM advice_attention_attempts
                    WHERE claim_token=? AND advice_id=? AND user_id=? AND device_id=?""",
                    (str(claim_token), str(advice_id), user_id, device_id),
                ).fetchone()
                if attempt is None:
                    raise AttentionConflict("claim token is invalid for this device")
                if attempt["state"] == "delivered":
                    attention = self._connection.execute(
                        """SELECT delivery_count,next_eligible_at FROM advice_attention
                        WHERE advice_id=? AND user_id=?""",
                        (str(advice_id), user_id),
                    ).fetchone()
                    self._connection.commit()
                    return {
                        "status": "delivered",
                        "delivery_count": int(attempt["delivery_number"]),
                        "next_eligible_at": (
                            attention["next_eligible_at"] if attention is not None else None
                        ),
                    }
                if attempt["state"] != "claimed":
                    raise AttentionConflict("claim token is no longer active")
                attention = self._connection.execute(
                    """SELECT * FROM advice_attention
                    WHERE advice_id=? AND user_id=?""",
                    (str(advice_id), user_id),
                ).fetchone()
                if (
                    attention is None
                    or attention["state"] != "claimed"
                    or attention["claim_token"] != str(claim_token)
                    or attention["claim_device_id"] != device_id
                ):
                    raise AttentionConflict("claim token was superseded or resolved")
                delivery_number = int(attempt["delivery_number"])
                if delivery_number != int(attention["delivery_count"]):
                    raise AttentionConflict("claim delivery number is stale")
                if _parse_utc(attempt["lease_expires_at"]) <= current:
                    self._connection.execute(
                        """UPDATE advice_attention_attempts SET
                          state='superseded',completed_at=?
                        WHERE claim_token=? AND state='claimed'""",
                        (current.isoformat(), str(claim_token)),
                    )
                    self._connection.execute(
                        """UPDATE advice_attention SET state='waiting',
                          claim_token=NULL,claim_device_id=NULL,claim_expires_at=NULL,
                          updated_at=?
                        WHERE advice_id=? AND user_id=? AND claim_token=?""",
                        (
                            current.isoformat(),
                            str(advice_id),
                            user_id,
                            str(claim_token),
                        ),
                    )
                    if (
                        delivery_number >= 2
                        and attention["handling_kind"] == "later"
                    ):
                        self._resolve_attention_locked(
                            advice_id,
                            user_id,
                            reason="handled:deferred",
                            current=current,
                        )
                    self._connection.commit()
                    raise AttentionConflict("claim token expired")
                handled_deferred = (
                    delivery_number == 2 and attention["handling_kind"] == "later"
                )
                next_eligible_at = (
                    None if handled_deferred else attention["next_eligible_at"]
                )
                auto_close_at = None if handled_deferred else attention["auto_close_at"]
                self._connection.execute(
                    """UPDATE advice_attention_attempts SET
                      state='delivered',completed_at=? WHERE claim_token=?""",
                    (current.isoformat(), str(claim_token)),
                )
                self._connection.execute(
                    """UPDATE advice_attention SET
                      state=?,
                      first_delivered_at=COALESCE(first_delivered_at,?),
                      last_delivered_at=?,next_eligible_at=?,auto_close_at=?,
                      claim_token=NULL,claim_device_id=NULL,claim_expires_at=NULL,
                      resolved_at=?,resolved_reason=?,
                      updated_at=?
                    WHERE advice_id=? AND user_id=? AND claim_token=?""",
                    (
                        "resolved" if handled_deferred else "waiting",
                        current.isoformat(),
                        current.isoformat(),
                        next_eligible_at,
                        auto_close_at,
                        current.isoformat() if handled_deferred else None,
                        "handled:deferred" if handled_deferred else None,
                        current.isoformat(),
                        str(advice_id),
                        user_id,
                        str(claim_token),
                    ),
                )
                self._connection.commit()
                return {
                    "status": "delivered",
                    "delivery_count": delivery_number,
                    "next_eligible_at": next_eligible_at,
                }
            except Exception:
                self._connection.rollback()
                raise

    def fail_advice_attention(
        self,
        user_id: str,
        advice_id: UUID | str,
        device_id: str,
        claim_token: UUID | str,
        *,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Release the lease; its globally issued delivery permit stays consumed."""

        current = _as_utc(now)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                attempt = self._connection.execute(
                    """SELECT * FROM advice_attention_attempts
                    WHERE claim_token=? AND advice_id=? AND user_id=? AND device_id=?""",
                    (str(claim_token), str(advice_id), user_id, device_id),
                ).fetchone()
                if attempt is None:
                    raise AttentionConflict("claim token is invalid for this device")
                attention = self._connection.execute(
                    """SELECT * FROM advice_attention
                    WHERE advice_id=? AND user_id=?""",
                    (str(advice_id), user_id),
                ).fetchone()
                if attempt["state"] == "failed":
                    self._connection.commit()
                    return {
                        "status": "released",
                        "delivery_count": int(attention["delivery_count"]) if attention else 0,
                    }
                if (
                    attempt["state"] != "claimed"
                    or attention is None
                    or attention["state"] != "claimed"
                    or attention["claim_token"] != str(claim_token)
                    or attention["claim_device_id"] != device_id
                ):
                    raise AttentionConflict("claim token was superseded or resolved")
                delivery_count = int(attention["delivery_count"])
                handled_deferred = (
                    delivery_count >= 2 and attention["handling_kind"] == "later"
                )
                self._connection.execute(
                    """UPDATE advice_attention_attempts SET
                      state='failed',completed_at=?,failure_reason=?
                    WHERE claim_token=?""",
                    (current.isoformat(), (reason or "delivery_failed")[:240], str(claim_token)),
                )
                self._connection.execute(
                    """UPDATE advice_attention SET state='waiting',claim_token=NULL,
                      claim_device_id=NULL,claim_expires_at=NULL,updated_at=?
                    WHERE advice_id=? AND user_id=? AND claim_token=?""",
                    (
                        current.isoformat(),
                        str(advice_id),
                        user_id,
                        str(claim_token),
                    ),
                )
                if handled_deferred:
                    self._resolve_attention_locked(
                        advice_id,
                        user_id,
                        reason="handled:deferred",
                        current=current,
                    )
                self._connection.commit()
                return {"status": "released", "delivery_count": delivery_count}
            except Exception:
                self._connection.rollback()
                raise

    def sweep_advice_attention(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> int:
        current = _as_utc(now)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._ensure_attention_rows_locked(current)
                changed = self._sweep_attention_locked(user_id, current)
                self._connection.commit()
                return changed
            except Exception:
                self._connection.rollback()
                raise

    def _upsert_advice_localization_locked(
        self,
        user_id: str,
        advice: AdviceRecord,
        locale: str,
        current: str,
    ) -> int:
        advice_id = str(advice.id)
        if advice_display_source_matches_locale(advice, locale):
            cursor = self._connection.execute(
                """DELETE FROM advice_localizations
                WHERE user_id=? AND advice_id=? AND locale=?""",
                (user_id, advice_id, locale),
            )
            return max(0, int(cursor.rowcount))
        source_hash = advice_display_source_hash(advice)
        cursor = self._connection.execute(
            """INSERT INTO advice_localizations(
              user_id,advice_id,locale,source_hash,status,attempts,
              next_attempt_at,translated_json,provider,model,last_error,
              lease_token,lease_expires_at,created_at,updated_at
            ) VALUES(?,?,?,?, 'pending',0,?,NULL,NULL,NULL,NULL,NULL,NULL,?,?)
            ON CONFLICT(user_id,advice_id,locale) DO UPDATE SET
              source_hash=excluded.source_hash,
              status='pending',attempts=0,
              next_attempt_at=excluded.next_attempt_at,
              translated_json=NULL,provider=NULL,model=NULL,last_error=NULL,
              lease_token=NULL,lease_expires_at=NULL,
              updated_at=excluded.updated_at
            WHERE advice_localizations.source_hash<>excluded.source_hash""",
            (user_id, advice_id, locale, source_hash, current, current, current),
        )
        return max(0, int(cursor.rowcount))

    def enqueue_advice_localizations(
        self,
        user_id: str,
        locale: str,
        *,
        now: datetime | None = None,
    ) -> int:
        """Idempotently queue stale or missing historical display translations."""

        normalized_locale = normalize_locale(locale)
        if normalized_locale == DEFAULT_LOCALE:
            return 0
        current = _as_utc(now).isoformat()
        changed = 0
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                account = self._connection.execute(
                    "SELECT locale FROM users WHERE id=? AND is_active=1",
                    (user_id,),
                ).fetchone()
                if (
                    account is None
                    or normalize_locale(account["locale"], strict=False)
                    != normalized_locale
                ):
                    self._connection.commit()
                    return 0
                rows = self._connection.execute(
                    """SELECT id,payload_json FROM advice
                    WHERE user_id=? AND status!='provisional'
                    ORDER BY created_at,id""",
                    (user_id,),
                ).fetchall()
                for row in rows:
                    try:
                        advice = AdviceRecord.model_validate_json(row["payload_json"])
                    except (TypeError, ValueError):
                        continue
                    changed += self._upsert_advice_localization_locked(
                        user_id,
                        advice,
                        normalized_locale,
                        current,
                    )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return changed

    def enqueue_current_english_advice_localizations(
        self,
        *,
        now: datetime | None = None,
    ) -> int:
        """Startup backfill for every active account already using English."""

        with self._lock:
            rows = self._connection.execute(
                """SELECT id FROM users
                WHERE is_active=1 AND locale='en-US' ORDER BY id"""
            ).fetchall()
        return sum(
            self.enqueue_advice_localizations(row["id"], "en-US", now=now)
            for row in rows
        )

    def claim_next_advice_localization(
        self,
        *,
        now: datetime | None = None,
        max_attempts: int = 5,
    ) -> AdviceLocalizationClaim | None:
        """Atomically lease one due translation and recover expired workers."""

        current = _as_utc(now)
        current_text = current.isoformat()
        bounded_attempts = max(1, min(20, int(max_attempts)))
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    """UPDATE advice_localizations SET
                      status=CASE WHEN attempts>=? THEN 'unavailable' ELSE 'pending' END,
                      next_attempt_at=?,
                      last_error=CASE WHEN attempts>=?
                        THEN 'localization_lease_expired_max_attempts'
                        ELSE 'localization_lease_expired' END,
                      lease_token=NULL,lease_expires_at=NULL,updated_at=?
                    WHERE status='running' AND lease_expires_at IS NOT NULL
                      AND lease_expires_at<=?""",
                    (
                        bounded_attempts,
                        current_text,
                        bounded_attempts,
                        current_text,
                        current_text,
                    ),
                )
                # Live proactive analysis owns the shared model slot.  Check it
                # in the same write transaction as the translation lease so a
                # historical backfill cannot jump ahead across processes.
                self._retire_stale_analysis_jobs_locked(None, current)
                due_analysis = self._connection.execute(
                    """SELECT 1 FROM analysis_jobs j
                    JOIN events e
                      ON e.user_id=j.user_id AND e.id=j.event_id
                    WHERE j.cloud_approved=1
                      AND j.status IN ('pending','retry')
                      AND j.next_attempt_at<=?
                    LIMIT 1""",
                    (current_text,),
                ).fetchone()
                if due_analysis is not None:
                    self._connection.commit()
                    return None
                self._connection.execute(
                    """UPDATE advice_localizations SET
                      status='unavailable',last_error='localization_max_attempts_exhausted',
                      lease_token=NULL,lease_expires_at=NULL,updated_at=?
                    WHERE status='pending' AND attempts>=?""",
                    (current_text, bounded_attempts),
                )
                for _ in range(32):
                    row = self._connection.execute(
                        """SELECT l.*,a.payload_json
                        FROM advice_localizations l
                        JOIN advice a
                          ON a.user_id=l.user_id AND a.id=l.advice_id
                        JOIN users u ON u.id=l.user_id
                        WHERE l.status='pending' AND l.next_attempt_at<=?
                          AND l.attempts<? AND a.status!='provisional'
                          AND u.is_active=1 AND u.locale=l.locale
                        ORDER BY l.next_attempt_at,l.attempts,a.created_at DESC,
                                 l.updated_at,l.user_id,l.advice_id
                        LIMIT 1""",
                        (current_text, bounded_attempts),
                    ).fetchone()
                    if row is None:
                        self._connection.commit()
                        return None
                    identity = (row["user_id"], row["advice_id"], row["locale"])
                    localization_defer_until = (
                        self._advice_localization_budget_defer_until_locked(
                            row["user_id"],
                            current,
                        )
                    )
                    if localization_defer_until is not None:
                        defer_text = localization_defer_until.isoformat()
                        self._connection.execute(
                            """UPDATE advice_localizations SET
                              next_attempt_at=?,
                              last_error='localization_daily_budget_deferred',
                              updated_at=?
                            WHERE user_id=? AND locale=? AND status='pending'
                              AND next_attempt_at<?""",
                            (
                                defer_text,
                                current_text,
                                row["user_id"],
                                row["locale"],
                                defer_text,
                            ),
                        )
                        continue
                    try:
                        advice = AdviceRecord.model_validate_json(row["payload_json"])
                    except (TypeError, ValueError):
                        self._connection.execute(
                            """UPDATE advice_localizations SET
                              status='unavailable',last_error='localization_source_invalid',
                              lease_token=NULL,lease_expires_at=NULL,updated_at=?
                            WHERE user_id=? AND advice_id=? AND locale=?""",
                            (current_text, *identity),
                        )
                        continue
                    source_hash = advice_display_source_hash(advice)
                    if advice_display_source_matches_locale(advice, row["locale"]):
                        self._connection.execute(
                            """DELETE FROM advice_localizations
                            WHERE user_id=? AND advice_id=? AND locale=?""",
                            identity,
                        )
                        continue
                    if source_hash != row["source_hash"]:
                        self._connection.execute(
                            """UPDATE advice_localizations SET
                              source_hash=?,status='pending',attempts=0,
                              next_attempt_at=?,translated_json=NULL,
                              provider=NULL,model=NULL,last_error=NULL,
                              lease_token=NULL,lease_expires_at=NULL,updated_at=?
                            WHERE user_id=? AND advice_id=? AND locale=?""",
                            (source_hash, current_text, current_text, *identity),
                        )
                        continue
                    lease_token = str(uuid4())
                    lease_expires = (current + ADVICE_LOCALIZATION_LEASE).isoformat()
                    attempt = int(row["attempts"]) + 1
                    cursor = self._connection.execute(
                        """UPDATE advice_localizations SET
                          status='running',attempts=?,lease_token=?,lease_expires_at=?,
                          updated_at=?
                        WHERE user_id=? AND advice_id=? AND locale=?
                          AND source_hash=? AND status='pending'""",
                        (
                            attempt,
                            lease_token,
                            lease_expires,
                            current_text,
                            *identity,
                            source_hash,
                        ),
                    )
                    if cursor.rowcount != 1:
                        continue
                    self._connection.commit()
                    return AdviceLocalizationClaim(
                        user_id=row["user_id"],
                        advice_id=advice.id,
                        locale=row["locale"],
                        source_hash=source_hash,
                        attempts=attempt,
                        lease_token=lease_token,
                        advice=advice,
                    )
                self._connection.commit()
                return None
            except BaseException:
                self._connection.rollback()
                raise

    def complete_advice_localization(
        self,
        claim: AdviceLocalizationClaim,
        translated: dict[str, str | None],
        *,
        provider: str,
        model: str,
        now: datetime | None = None,
    ) -> bool:
        """Publish a translation only while its tenant, source hash and lease match."""

        current = _as_utc(now).isoformat()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    """SELECT payload_json FROM advice
                    WHERE user_id=? AND id=? AND status!='provisional'""",
                    (claim.user_id, str(claim.advice_id)),
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return False
                advice = AdviceRecord.model_validate_json(row["payload_json"])
                source_hash = advice_display_source_hash(advice)
                identity = (claim.user_id, str(claim.advice_id), claim.locale)
                if source_hash != claim.source_hash:
                    self._connection.execute(
                        """UPDATE advice_localizations SET
                          source_hash=?,status='pending',attempts=0,next_attempt_at=?,
                          translated_json=NULL,provider=NULL,model=NULL,last_error=NULL,
                          lease_token=NULL,lease_expires_at=NULL,updated_at=?
                        WHERE user_id=? AND advice_id=? AND locale=?
                          AND lease_token=? AND status='running'""",
                        (
                            source_hash,
                            current,
                            current,
                            *identity,
                            claim.lease_token,
                        ),
                    )
                    self._connection.commit()
                    return False
                validated = validate_advice_display_translation(
                    advice_display_source(advice), translated, claim.locale
                )
                serialized = json.dumps(
                    validated,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                cursor = self._connection.execute(
                    """UPDATE advice_localizations SET
                      status='ready',translated_json=?,provider=?,model=?,last_error=NULL,
                      lease_token=NULL,lease_expires_at=NULL,next_attempt_at=?,updated_at=?
                    WHERE user_id=? AND advice_id=? AND locale=? AND source_hash=?
                      AND lease_token=? AND status='running'""",
                    (
                        serialized,
                        str(provider)[:80],
                        str(model)[:160],
                        current,
                        current,
                        *identity,
                        claim.source_hash,
                        claim.lease_token,
                    ),
                )
                self._connection.commit()
                return cursor.rowcount == 1
            except BaseException:
                self._connection.rollback()
                raise

    def fail_advice_localization(
        self,
        claim: AdviceLocalizationClaim,
        reason: str,
        *,
        provider: str,
        model: str,
        retryable: bool,
        delay_seconds: float = 0.0,
        max_attempts: int = 5,
        now: datetime | None = None,
    ) -> str | None:
        """Retry with bounded backoff or terminally suppress a failed translation."""

        current = _as_utc(now)
        bounded_attempts = max(1, min(20, int(max_attempts)))
        retry = bool(retryable and claim.attempts < bounded_attempts)
        status = "pending" if retry else "unavailable"
        due = current + timedelta(
            seconds=max(0.0, min(86_400.0, float(delay_seconds)))
        )
        safe_reason = str(reason or "localization_failed").strip()[:160]
        with self._lock:
            cursor = self._connection.execute(
                """UPDATE advice_localizations SET
                  status=?,next_attempt_at=?,provider=?,model=?,last_error=?,
                  translated_json=NULL,lease_token=NULL,lease_expires_at=NULL,
                  updated_at=?
                WHERE user_id=? AND advice_id=? AND locale=? AND source_hash=?
                  AND lease_token=? AND status='running'""",
                (
                    status,
                    due.isoformat(),
                    str(provider)[:80],
                    str(model)[:160],
                    safe_reason,
                    current.isoformat(),
                    claim.user_id,
                    str(claim.advice_id),
                    claim.locale,
                    claim.source_hash,
                    claim.lease_token,
                ),
            )
            self._connection.commit()
        return status if cursor.rowcount == 1 else None

    def advice_display_envelope(
        self,
        user_id: str,
        advice: AdviceRecord,
        locale: str,
    ) -> dict[str, Any]:
        """Return additive display metadata without changing the source record."""

        normalized_locale = normalize_locale(locale, strict=False)
        if normalized_locale == DEFAULT_LOCALE or advice_display_source_matches_locale(
            advice,
            normalized_locale,
        ):
            return self._advice_display_envelope_from_row(
                advice,
                normalized_locale,
                None,
            )
        with self._lock:
            row = self._connection.execute(
                """SELECT source_hash,status,translated_json
                FROM advice_localizations
                WHERE user_id=? AND advice_id=? AND locale=?""",
                (user_id, str(advice.id), normalized_locale),
            ).fetchone()
        return self._advice_display_envelope_from_row(
            advice,
            normalized_locale,
            row,
        )

    def advice_display_envelopes(
        self,
        user_id: str,
        advice_items: list[AdviceRecord],
        locale: str,
    ) -> dict[str, dict[str, Any]]:
        """Batch the list-path cache lookup so 500 advice rows cost one query."""

        normalized_locale = normalize_locale(locale, strict=False)
        advice_by_id = {str(advice.id): advice for advice in advice_items}
        rows_by_id: dict[str, sqlite3.Row] = {}
        if advice_by_id and normalized_locale != DEFAULT_LOCALE:
            advice_ids = list(advice_by_id)
            placeholders = ",".join("?" for _ in advice_ids)
            with self._lock:
                rows = self._connection.execute(
                    f"""SELECT advice_id,source_hash,status,translated_json
                    FROM advice_localizations
                    WHERE user_id=? AND locale=?
                      AND advice_id IN ({placeholders})""",
                    (user_id, normalized_locale, *advice_ids),
                ).fetchall()
            rows_by_id = {row["advice_id"]: row for row in rows}
        return {
            advice_id: self._advice_display_envelope_from_row(
                advice,
                normalized_locale,
                rows_by_id.get(advice_id),
            )
            for advice_id, advice in advice_by_id.items()
        }

    def _advice_display_envelope_from_row(
        self,
        advice: AdviceRecord,
        normalized_locale: str,
        row: sqlite3.Row | None,
    ) -> dict[str, Any]:
        source = advice_display_source(advice)
        if normalized_locale == DEFAULT_LOCALE or advice_display_source_matches_locale(
            advice, normalized_locale
        ):
            return {
                "display_locale": normalized_locale,
                "display_translation_status": "original",
                "display": source,
            }
        source_hash = advice_display_source_hash(advice)
        if row is None or row["source_hash"] != source_hash:
            status = "pending"
            display = None
        elif row["status"] == "ready" and row["translated_json"]:
            try:
                display = validate_advice_display_translation(
                    source,
                    json.loads(row["translated_json"]),
                    normalized_locale,
                )
                status = "ready"
            except (TypeError, ValueError):
                display = None
                status = "unavailable"
        elif row["status"] == "unavailable":
            status = "unavailable"
            display = None
        else:
            status = "pending"
            display = None
        return {
            "display_locale": normalized_locale,
            "display_translation_status": status,
            "display": display,
        }

    def list_advice(self, user_id: str, limit: int = 100) -> list[AdviceRecord]:
        self.sweep_advice_attention(user_id)
        self._withdraw_expired_active(user_id)
        with self._lock:
            rows = self._connection.execute(
                """SELECT payload_json FROM advice
                WHERE user_id=? AND status!='provisional'
                ORDER BY created_at DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        return [AdviceRecord.model_validate_json(row["payload_json"]) for row in rows]

    def get_advice(self, user_id: str, advice_id: UUID | str) -> AdviceRecord | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT payload_json FROM advice
                WHERE user_id=? AND id=? AND status!='provisional'""",
                (user_id, str(advice_id)),
            ).fetchone()
        return AdviceRecord.model_validate_json(row["payload_json"]) if row else None

    # ------------------------------------------------------------------
    # Advice preferences (global / per-goal)
    # ------------------------------------------------------------------

    def _row_to_preference(self, row: sqlite3.Row) -> AdvicePreference:
        return AdvicePreference(
            user_id=row["user_id"],
            scope_key=row["scope_key"],
            goal_id=UUID(row["goal_id"]) if row["goal_id"] else None,
            direction=row["direction"] or "",
            direction_mode=row["direction_mode"] or "inherit",
            frequency_mode=row["frequency_mode"],
            event_types=(
                json.loads(row["event_types_json"])
                if row["event_types_json"] is not None
                else None
            ),
            revision=int(row["revision"]),
            updated_at=_parse_utc(row["updated_at"]),
        )

    def _load_preference_locked(
        self, user_id: str, scope_key: str
    ) -> AdvicePreference | None:
        row = self._connection.execute(
            "SELECT * FROM advice_preferences WHERE user_id=? AND scope_key=?",
            (user_id, scope_key),
        ).fetchone()
        return self._row_to_preference(row) if row is not None else None

    def global_preference(self, user_id: str) -> AdvicePreference:
        with self._lock:
            stored = self._load_preference_locked(user_id, GLOBAL_SCOPE_KEY)
        return stored if stored is not None else default_global_preference(user_id)

    def goal_preference(
        self, user_id: str, goal_id: UUID | str
    ) -> AdvicePreference | None:
        with self._lock:
            return self._load_preference_locked(user_id, goal_scope_key(goal_id))

    def list_goal_preferences(self, user_id: str) -> list[AdvicePreference]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM advice_preferences
                WHERE user_id=? AND scope_key!=? ORDER BY scope_key""",
                (user_id, GLOBAL_SCOPE_KEY),
            ).fetchall()
        return [self._row_to_preference(row) for row in rows]

    def _effective_preference_locked(
        self, user_id: str, goal_id: UUID | str | None
    ) -> EffectivePreference:
        stored_global = self._load_preference_locked(user_id, GLOBAL_SCOPE_KEY)
        global_pref = (
            stored_global
            if stored_global is not None
            else default_global_preference(user_id)
        )
        goal_pref = (
            self._load_preference_locked(user_id, goal_scope_key(goal_id))
            if goal_id is not None
            else None
        )
        effective = resolve_effective(global_pref, goal_pref)
        if goal_pref is None and goal_id is not None:
            effective = effective.model_copy(
                update={"goal_id": goal_id if isinstance(goal_id, UUID) else UUID(str(goal_id))}
            )
        return effective

    def effective_preference(
        self, user_id: str, goal_id: UUID | str | None = None
    ) -> EffectivePreference:
        with self._lock:
            return self._effective_preference_locked(user_id, goal_id)

    def upsert_advice_preference(
        self,
        user_id: str,
        *,
        goal_id: UUID | str | None,
        direction: str,
        direction_mode: str,
        frequency_mode: str | None,
        event_types: list[str] | None,
        expected_revision: int,
        now: datetime | None = None,
    ) -> AdvicePreference:
        """Idempotent, revision-guarded PUT for one scope.

        A body identical to the stored content is accepted without a bump
        (safe retry); a stale revision with different content raises
        PreferenceRevisionConflict carrying the current object (409).
        """

        scope_key = GLOBAL_SCOPE_KEY if goal_id is None else goal_scope_key(goal_id)
        current_time = _as_utc(now)
        candidate = AdvicePreference(
            user_id=user_id,
            scope_key=scope_key,
            goal_id=(
                goal_id if isinstance(goal_id, UUID) else UUID(str(goal_id))
            )
            if goal_id is not None
            else None,
            direction=direction,
            direction_mode=direction_mode,
            frequency_mode=frequency_mode,
            event_types=event_types,
            revision=0,
            updated_at=current_time,
        )
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                stored = self._load_preference_locked(user_id, scope_key)
                if stored is None and goal_id is None:
                    baseline: AdvicePreference | None = default_global_preference(user_id)
                else:
                    baseline = stored
                current_revision = stored.revision if stored is not None else 0
                same_content = baseline is not None and (
                    baseline.direction == candidate.direction
                    and baseline.direction_mode == candidate.direction_mode
                    and baseline.frequency_mode == candidate.frequency_mode
                    and baseline.event_types == candidate.event_types
                )
                if int(expected_revision) != current_revision:
                    if same_content:
                        self._connection.commit()
                        return baseline.model_copy(
                            update={"revision": current_revision}
                        )
                    self._connection.rollback()
                    raise PreferenceRevisionConflict(
                        "preference was changed elsewhere; reload before saving",
                        current=(
                            baseline
                            if baseline is not None
                            else candidate.model_copy(update={"revision": current_revision})
                        ),
                    )
                if same_content and stored is not None:
                    self._connection.commit()
                    return stored
                new_revision = current_revision + 1
                saved = candidate.model_copy(
                    update={"revision": new_revision, "updated_at": current_time}
                )
                self._connection.execute(
                    """INSERT INTO advice_preferences(
                      user_id,scope_key,goal_id,direction,direction_mode,
                      frequency_mode,event_types_json,revision,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(user_id,scope_key) DO UPDATE SET
                      goal_id=excluded.goal_id,
                      direction=excluded.direction,
                      direction_mode=excluded.direction_mode,
                      frequency_mode=excluded.frequency_mode,
                      event_types_json=excluded.event_types_json,
                      revision=excluded.revision,
                      updated_at=excluded.updated_at""",
                    (
                        user_id,
                        scope_key,
                        str(saved.goal_id) if saved.goal_id is not None else None,
                        saved.direction,
                        saved.direction_mode,
                        saved.frequency_mode,
                        (
                            json.dumps(saved.event_types)
                            if saved.event_types is not None
                            else None
                        ),
                        new_revision,
                        current_time.isoformat(),
                    ),
                )
                self._connection.commit()
                return saved
            except PreferenceRevisionConflict:
                raise
            except BaseException:
                self._connection.rollback()
                raise

    def delete_goal_preference(self, user_id: str, goal_id: UUID | str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM advice_preferences WHERE user_id=? AND scope_key=?",
                (user_id, goal_scope_key(goal_id)),
            )
            self._connection.commit()
        return cursor.rowcount == 1

    def _record_is_manual_problem_locked(self, record: AdviceRecord) -> bool:
        try:
            evidence = record.evidence[0]
        except (IndexError, TypeError):
            return False
        event = self.get_event(record.user_id, evidence.event_id)
        return is_manual_problem_event(event)

    def _advice_publish_block_locked(
        self, record: AdviceRecord, now: datetime
    ) -> str | None:
        """Reason code when publishing must be blocked, else None.

        Runs inside the caller's BEGIN IMMEDIATE transaction, so counting and
        the subsequent insert/promote are atomic: two concurrent publishes can
        never both pass the last remaining quota slot.
        """

        effective = self._effective_preference_locked(record.user_id, record.goal_id)
        # Global paused is the master switch: not even emergencies pass it.
        if effective.paused:
            return REASON_PREFERENCE_PAUSED
        if effective.important_only and record.effective_level < AdviceLevel.L3:
            return REASON_PREFERENCE_IMPORTANT_ONLY
        if self._record_is_manual_problem_locked(record) or automatic_emergency_record(record):
            # Manual problems and emergencies bypass NUMERIC quotas/cooldown
            # only. The master pause and important-only level floor above are
            # never bypassed.
            return None

        def published_count(since: datetime, goal_id: str | None) -> int:
            if goal_id is None:
                row = self._connection.execute(
                    """SELECT COUNT(*) AS count FROM advice
                    WHERE user_id=? AND published_at IS NOT NULL AND published_at>=?""",
                    (record.user_id, since.isoformat()),
                ).fetchone()
            else:
                row = self._connection.execute(
                    """SELECT COUNT(*) AS count FROM advice
                    WHERE user_id=? AND goal_id=? AND published_at IS NOT NULL
                      AND published_at>=?""",
                    (record.user_id, goal_id, since.isoformat()),
                ).fetchone()
            return int(row["count"])

        def last_published(goal_id: str | None) -> datetime | None:
            if goal_id is None:
                row = self._connection.execute(
                    """SELECT MAX(published_at) AS last FROM advice
                    WHERE user_id=? AND published_at IS NOT NULL""",
                    (record.user_id,),
                ).fetchone()
            else:
                row = self._connection.execute(
                    """SELECT MAX(published_at) AS last FROM advice
                    WHERE user_id=? AND goal_id=? AND published_at IS NOT NULL""",
                    (record.user_id, goal_id),
                ).fetchone()
            return _parse_utc(row["last"]) if row and row["last"] else None

        # Semantics mirror the three stable reason codes exactly:
        # - The GLOBAL policy count is the TOTAL rolling budget across every
        #   goal (PREFERENCE_GLOBAL_LIMIT).
        # - The minimum spacing (cooldown) is inherently goal-scoped: it uses
        #   the goal-EFFECTIVE policy, inherited or overridden
        #   (PREFERENCE_GOAL_COOLDOWN).
        # - A goal override adds a LOCAL count budget on top; an inherited
        #   frequency adds no second count check — the global aggregate already
        #   covers it (PREFERENCE_GOAL_LIMIT).
        global_policy = frequency_policy(effective.global_frequency_mode)
        if global_policy.max_per_window is not None:
            if (
                published_count(now - global_policy.window, None)
                >= global_policy.max_per_window
            ):
                return REASON_PREFERENCE_GLOBAL_LIMIT

        goal_key = str(record.goal_id) if record.goal_id is not None else None
        if goal_key is not None:
            effective_goal_policy = frequency_policy(effective.frequency_mode)
            if effective_goal_policy.cooldown:
                last = last_published(goal_key)
                if last is not None and now - last < effective_goal_policy.cooldown:
                    return REASON_PREFERENCE_GOAL_COOLDOWN
            if effective.goal_frequency_override is not None:
                goal_policy = frequency_policy(effective.goal_frequency_override)
                if goal_policy.max_per_window is not None:
                    if (
                        published_count(now - goal_policy.window, goal_key)
                        >= goal_policy.max_per_window
                    ):
                        return REASON_PREFERENCE_GOAL_LIMIT
        return None

    def recent_feedback_for_goal(
        self,
        user_id: str,
        goal_id: UUID | str,
        *,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(0, min(int(limit), 20))
        if bounded_limit == 0:
            return []
        expected_goal_id = str(goal_id)
        with self._lock:
            rows = self._connection.execute(
                """SELECT f.kind,f.note,f.origin,f.signal_weight,f.created_at,a.payload_json
                FROM feedback f
                JOIN advice a ON a.id=f.advice_id AND a.user_id=f.user_id
                WHERE f.user_id=?
                  AND json_extract(
                    CASE WHEN json_valid(a.payload_json)
                         THEN a.payload_json ELSE '{}' END,
                    '$.goal_id'
                  )=?
                ORDER BY f.created_at DESC,f.id DESC
                LIMIT ?""",
                (user_id, expected_goal_id, bounded_limit),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item: dict[str, Any] = {
                "kind": str(row["kind"]),
                "note": row["note"],
                "origin": str(row["origin"]),
                "signal_weight": float(row["signal_weight"]),
                "created_at": str(row["created_at"]),
            }
            results.append(item)
        return results

    def delete_advice(self, user_id: str, advice_id: UUID | str) -> bool:
        """Remove a provisional recommendation that failed a required review gate."""

        with self._lock:
            cursor = self._connection.execute(
                """DELETE FROM advice
                WHERE user_id=? AND id=? AND status='provisional'""",
                (user_id, str(advice_id)),
            )
            self._connection.commit()
        return cursor.rowcount == 1

    def record_feedback(
        self,
        user_id: str,
        advice_id: UUID | str,
        feedback: FeedbackCreate,
        *,
        now: datetime | None = None,
        max_feedback_per_user: int | None = None,
        max_guidance_per_advice: int | None = None,
    ) -> AdviceRecord:
        current = _as_utc(now)
        created_at = current.isoformat()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT payload_json FROM advice WHERE id=? AND user_id=?",
                    (str(advice_id), user_id),
                ).fetchone()
                if row is None:
                    raise ValueError("advice not found")
                record = AdviceRecord.model_validate_json(row["payload_json"])
                feedback_id = (
                    str(feedback.feedback_id) if feedback.feedback_id is not None else None
                )
                semantic_key = _feedback_semantic_key(feedback.kind, feedback.note)
                if feedback_id is not None:
                    prior = self._connection.execute(
                        """SELECT advice_id,kind,semantic_key FROM feedback
                        WHERE user_id=? AND feedback_id=? LIMIT 1""",
                        (user_id, feedback_id),
                    ).fetchone()
                    if prior is not None:
                        if (
                            prior["advice_id"] != str(advice_id)
                            or prior["kind"] != feedback.kind
                            or prior["semantic_key"] != semantic_key
                        ):
                            raise ValueError(
                                "feedback_id was already used for different feedback"
                            )
                        self._connection.commit()
                        return record
                semantic_duplicate = self._connection.execute(
                    """SELECT 1 FROM feedback
                    WHERE advice_id=? AND user_id=? AND semantic_key=? LIMIT 1""",
                    (str(advice_id), user_id, semantic_key),
                ).fetchone()
                if semantic_duplicate is not None:
                    self._connection.commit()
                    return record
                if feedback.kind != "guidance" and record.status != AdviceStatus.ACTIVE:
                    duplicate = self._connection.execute(
                        """SELECT 1 FROM feedback
                        WHERE advice_id=? AND user_id=? AND kind=? LIMIT 1""",
                        (str(advice_id), user_id, feedback.kind),
                    ).fetchone()
                    if duplicate is not None:
                        self._connection.commit()
                        return record
                    raise ValueError(
                        f"advice is already {record.status.value}; feedback action conflicts"
                    )
                if max_feedback_per_user is not None:
                    count_row = self._connection.execute(
                        "SELECT COUNT(*) AS count FROM feedback WHERE user_id=?",
                        (user_id,),
                    ).fetchone()
                    if int(count_row["count"]) >= max(0, int(max_feedback_per_user)):
                        raise FeedbackQuotaExceeded("feedback storage quota exceeded")
                if feedback.kind == "guidance" and max_guidance_per_advice is not None:
                    guidance_row = self._connection.execute(
                        """SELECT COUNT(*) AS count FROM feedback
                        WHERE user_id=? AND advice_id=? AND kind='guidance'""",
                        (user_id, str(advice_id)),
                    ).fetchone()
                    if int(guidance_row["count"]) >= max(
                        0, int(max_guidance_per_advice)
                    ):
                        raise FeedbackQuotaExceeded("guidance quota exceeded")
                self._connection.execute(
                    """INSERT INTO feedback(
                      advice_id,user_id,feedback_id,semantic_key,kind,note,
                      origin,signal_weight,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        str(advice_id),
                        user_id,
                        feedback_id,
                        semantic_key,
                        feedback.kind,
                        feedback.note,
                        "user",
                        1.0,
                        created_at,
                    ),
                )
                if feedback.kind in ERROR_FEEDBACK_KINDS:
                    self._record_error_locked(record, feedback.kind, current)
                if feedback.kind in RELEVANCE_FEEDBACK_KINDS:
                    self._record_relevance_signal_locked(
                        record,
                        feedback.kind,
                        origin="user",
                        weight=1.0,
                        current=current,
                    )
                if feedback.kind == "stop_topic":
                    self._suppress_topic_locked(
                        record,
                        current,
                        reason="USER_STOP_TOPIC",
                    )
                elif feedback.kind == "irrelevant":
                    # A direct relevance rejection is a topic-level preference,
                    # not just a weak prompt hint. Suppress the normalized topic
                    # so a paraphrased retry cannot immediately republish it.
                    self._suppress_topic_locked(
                        record,
                        current,
                        reason="USER_IRRELEVANT_TOPIC",
                    )

                status = (
                    AdviceStatus.DISMISSED
                    if feedback.kind in ERROR_FEEDBACK_KINDS
                    or feedback.kind in RELEVANCE_FEEDBACK_KINDS
                    or feedback.kind in {"dismissed", "stop_topic"}
                    else None
                )
                updates: dict[str, Any] = {}
                if feedback.kind == "later":
                    deadline = _as_utc(record.prediction.deadline)
                    snoozed_until = (
                        min(current + timedelta(hours=4), deadline)
                        if deadline > current
                        else None
                    )
                    updates["snoozed_until"] = snoozed_until
                elif feedback.kind in ERROR_FEEDBACK_KINDS or feedback.kind in RELEVANCE_FEEDBACK_KINDS or feedback.kind in {
                    "adopted",
                    "dismissed",
                    "stop_topic",
                }:
                    updates["snoozed_until"] = None
                if feedback.kind == "adopted":
                    status = AdviceStatus.ADOPTED
                if status is not None:
                    updates["status"] = status
                if updates:
                    record = record.model_copy(update=updates)
                    self._connection.execute(
                        "UPDATE advice SET status=?, payload_json=? WHERE id=? AND user_id=?",
                        (
                            record.status.value,
                            record.model_dump_json(),
                            str(advice_id),
                            user_id,
                        ),
                    )
                if feedback.kind == "later":
                    self._defer_attention_locked(record, current=current)
                else:
                    # Every other explicit response, including free-form
                    # guidance, ends this reminder cycle on all devices.
                    self._resolve_attention_locked(
                        advice_id,
                        user_id,
                        reason=f"feedback:{feedback.kind}",
                        current=current,
                    )
                self._connection.commit()
                return record
            except Exception:
                self._connection.rollback()
                raise

    def _record_error_locked(
        self,
        advice: AdviceRecord,
        error_kind: str,
        current: datetime,
    ) -> None:
        # Feedback delivery is retryable on mobile.  The database-level
        # conflict target makes the idempotency decision atomic across workers.
        level = advice.effective_level
        cursor = self._connection.execute(
            """INSERT INTO error_ledger(
              advice_id,user_id,domain,effective_level,error_kind,created_at
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(user_id,advice_id) DO NOTHING""",
            (
                str(advice.id),
                advice.user_id,
                advice.domain,
                int(level),
                error_kind,
                current.isoformat(),
            ),
        )
        if cursor.rowcount != 1:
            return
        policy = ERROR_BUDGETS.get(level)
        if policy is None:
            return
        window, allowed_errors = policy
        window_start = current - window
        row = self._connection.execute(
            """SELECT COUNT(*) AS error_count FROM error_ledger
            WHERE user_id=? AND effective_level=? AND created_at>=? AND created_at<=?""",
            (
                advice.user_id,
                int(level),
                window_start.isoformat(),
                current.isoformat(),
            ),
        ).fetchone()
        error_count = int(row["error_count"])
        if error_count <= allowed_errors:
            return
        expires_at = current + SPEAKING_FREEZE_DURATION
        reason = f"ERROR_BUDGET_{level.name}_EXCEEDED"
        self._connection.execute(
            """INSERT INTO speaking_freezes(
              user_id,level,error_count,allowed_errors,window_started_at,
              frozen_at,expires_at,reason
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id,level) DO UPDATE SET
              error_count=excluded.error_count,
              allowed_errors=excluded.allowed_errors,
              window_started_at=excluded.window_started_at,
              frozen_at=excluded.frozen_at,
              expires_at=excluded.expires_at,
              reason=excluded.reason""",
            (
                advice.user_id,
                int(level),
                error_count,
                allowed_errors,
                window_start.isoformat(),
                current.isoformat(),
                expires_at.isoformat(),
                reason,
            ),
        )

    def _suppress_topic_locked(
        self,
        advice: AdviceRecord,
        current: datetime,
        *,
        reason: str,
    ) -> None:
        expires_at = current + TOPIC_SUPPRESSION_DURATION
        self._connection.execute(
            """INSERT INTO topic_suppressions(
              user_id,dedupe_key,source_advice_id,reason,created_at,updated_at,expires_at
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(user_id,dedupe_key) DO UPDATE SET
              source_advice_id=excluded.source_advice_id,
              reason=excluded.reason,
              updated_at=excluded.updated_at,
              expires_at=excluded.expires_at""",
            (
                advice.user_id,
                _canonical_topic_key(advice),
                str(advice.id),
                reason,
                current.isoformat(),
                current.isoformat(),
                expires_at.isoformat(),
            ),
        )

    def error_count(
        self,
        user_id: str,
        level: AdviceLevel,
        *,
        since: datetime | None = None,
    ) -> int:
        query = "SELECT COUNT(*) AS error_count FROM error_ledger WHERE user_id=? AND effective_level=?"
        params: list[Any] = [user_id, int(level)]
        if since is not None:
            query += " AND created_at>=?"
            params.append(_as_utc(since).isoformat())
        with self._lock:
            row = self._connection.execute(query, params).fetchone()
        return int(row["error_count"])

    def active_speaking_freeze(
        self,
        user_id: str,
        level: AdviceLevel,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        current = _as_utc(now)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM speaking_freezes WHERE user_id=? AND level=?",
                (user_id, int(level)),
            ).fetchone()
        if row is None or _parse_utc(row["expires_at"]) <= current:
            return None
        return dict(row)

    def available_speaking_level(
        self,
        user_id: str,
        proposed_level: AdviceLevel,
        *,
        now: datetime | None = None,
    ) -> tuple[AdviceLevel, tuple[AdviceLevel, ...]]:
        current = _as_utc(now)
        level = proposed_level
        frozen: list[AdviceLevel] = []
        # A frozen tier is a ceiling barrier, not merely an unavailable exact
        # choice. A candidate cannot jump over a frozen lower tier by arriving
        # at L3/L4: crossing frozen L2 caps it at L1, crossing frozen L3 caps it
        # at L2, and multiple barriers are all applied from high to low.
        for numeric_level in range(int(proposed_level), int(AdviceLevel.L1), -1):
            inspected = AdviceLevel(numeric_level)
            if self.active_speaking_freeze(user_id, inspected, now=current) is None:
                continue
            frozen.append(inspected)
            level = min(level, AdviceLevel(numeric_level - 1))
        return level, tuple(frozen)

    def active_topic_suppression(
        self,
        user_id: str,
        dedupe_key: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        current = _as_utc(now)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM topic_suppressions WHERE user_id=? AND dedupe_key=?",
                (user_id, dedupe_key),
            ).fetchone()
        if row is None or _parse_utc(row["expires_at"]) <= current:
            return None
        return dict(row)

    def trust_summary(self, user_id: str, domain: str, level: AdviceLevel) -> TrustSummary:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM trust_accounts WHERE user_id=? AND domain=? AND level=?",
                (user_id, domain, int(level)),
            ).fetchone()
        if not row:
            return TrustSummary()
        return TrustSummary(
            judged=row["judged"], correct=row["correct"], brier_sum=row["brier_sum"],
            utility_sum=row["utility_sum"], timing_sum=row["timing_sum"],
            catastrophic_errors=row["catastrophic_errors"],
        )

    def speaking_trust_summary(
        self,
        user_id: str,
        domain: str,
        requested_level: AdviceLevel,
    ) -> TrustSummary:
        """Aggregate verified lower-level performance used to earn heavier speech rights.

        A cold-start L3 candidate is intentionally delivered as L2. Those verified L2
        outcomes must therefore count toward the L3 threshold or the higher level can
        never be earned.
        """

        with self._lock:
            row = self._connection.execute(
                """SELECT
                     COALESCE(SUM(judged), 0) AS judged,
                     COALESCE(SUM(correct), 0) AS correct,
                     COALESCE(SUM(brier_sum), 0) AS brier_sum,
                     COALESCE(SUM(utility_sum), 0) AS utility_sum,
                     COALESCE(SUM(timing_sum), 0) AS timing_sum,
                     COALESCE(SUM(catastrophic_errors), 0) AS catastrophic_errors
                   FROM trust_accounts
                   WHERE user_id=? AND domain=? AND level<=?""",
                (user_id, domain, int(requested_level)),
            ).fetchone()
        return TrustSummary(
            judged=row["judged"],
            correct=row["correct"],
            brier_sum=row["brier_sum"],
            utility_sum=row["utility_sum"],
            timing_sum=row["timing_sum"],
            catastrophic_errors=row["catastrophic_errors"],
        )

    def record_outcome(
        self,
        user_id: str,
        advice: AdviceRecord,
        outcome: OutcomeCreate,
    ) -> bool:
        """Record one terminal verification, returning False for an idempotent retry."""

        current = _as_utc()
        with self._lock:
            try:
                # Acquire the cross-process write reservation before checking
                # idempotency, so one outcome owns the complete verification
                # transaction from the first decision through final promotion.
                self._connection.execute("BEGIN IMMEDIATE")
                exists = self._connection.execute(
                    """SELECT 1 FROM outcomes
                    WHERE user_id=? AND advice_id=?""",
                    (user_id, str(advice.id)),
                ).fetchone()
                if exists:
                    self._connection.rollback()
                    return False
                self._connection.execute(
                    "INSERT INTO outcomes VALUES(?,?,?,?,?,?,?)",
                    (
                        str(advice.id), user_id, outcome.status.value, outcome.actual_result,
                        outcome.utility, outcome.timing_quality, current.isoformat(),
                    ),
                )
                # Android reports whether the separately predicted result after
                # adoption was achieved. A failed adopted-result prediction is a
                # real prediction error and must spend the error budget at the
                # recommendation's actual delivered level. The outcome primary key
                # makes retries idempotent before this ledger write is reached.
                if (
                    advice.status == AdviceStatus.ADOPTED
                    and outcome.status == OutcomeStatus.INCORRECT
                ):
                    self._record_error_locked(advice, "prediction_error", current)
                # ``prediction`` describes the untreated risk. Once the user adopts
                # the advice, calibrate the separately predicted adopted result
                # instead. Legacy advice may not have that optional prediction; in
                # that case retain the outcome but do not manufacture a Brier error.
                if advice.status == AdviceStatus.ADOPTED:
                    calibration_confidence = (
                        advice.adopted_confidence
                        if advice.adopted_expected_result and advice.adopted_confidence is not None
                        else None
                    )
                else:
                    calibration_confidence = (
                        advice.prediction.confidence if advice.prediction.observable else None
                    )
                if outcome.status != OutcomeStatus.UNKNOWN and calibration_confidence is not None:
                    correct = int(outcome.status == OutcomeStatus.CORRECT)
                    actual = float(correct)
                    brier = (calibration_confidence - actual) ** 2
                    catastrophic = int(outcome.status == OutcomeStatus.INCORRECT and advice.effective_level == AdviceLevel.L4)
                    utility = outcome.utility if outcome.utility is not None else 0.5
                    timing = outcome.timing_quality if outcome.timing_quality is not None else 0.5
                    self._connection.execute(
                        """INSERT INTO trust_accounts VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(user_id,domain,level) DO UPDATE SET
                          judged=judged+1,
                          correct=correct+excluded.correct,
                          brier_sum=brier_sum+excluded.brier_sum,
                          utility_sum=utility_sum+excluded.utility_sum,
                          timing_sum=timing_sum+excluded.timing_sum,
                          catastrophic_errors=catastrophic_errors+excluded.catastrophic_errors,
                          updated_at=excluded.updated_at""",
                        (
                            user_id, advice.domain, int(advice.effective_level), 1, correct, brier,
                            utility, timing, catastrophic, _now(),
                        ),
                    )
                verified = advice.model_copy(update={"status": AdviceStatus.VERIFIED})
                self._connection.execute(
                    """UPDATE advice SET status='verified',payload_json=?
                    WHERE user_id=? AND id=?""",
                    (verified.model_dump_json(), user_id, str(advice.id)),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return True

    def audit_cloud_slice(
        self,
        user_id: str,
        provider: str,
        model: str,
        purpose: str,
        context: dict[str, Any],
        *,
        prompt: str | None = None,
    ) -> None:
        safe_context = sanitize_cloud_context(context)
        if prompt is not None:
            # The prompt is part of the outbound payload too.  Keep a bounded,
            # redacted copy beside the context so the cloud-exit audit can
            # reconstruct what was actually asked without retaining secrets.
            from .privacy import MAX_CLOUD_PROMPT_CHARS, redact_text

            safe_payload: dict[str, Any] = {
                "prompt": redact_text(prompt, MAX_CLOUD_PROMPT_CHARS),
                "context": safe_context,
            }
        else:
            safe_payload = safe_context
        serialized = json.dumps(
            safe_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        quota = _environment_int(
            "MOUCHEN_CLOUD_AUDIT_BYTES_PER_USER",
            20 * 1024 * 1024,
            minimum=4 * 1024,
            maximum=10 * 1024 * 1024 * 1024,
        )
        encoded_size = len(serialized.encode("utf-8"))
        if encoded_size > quota:
            # Preserve proof that a slice left the machine without allowing a
            # single huge context to defeat the audit quota.
            serialized = json.dumps(
                {
                    "_audit_context_truncated": True,
                    "utf8_bytes": encoded_size,
                    "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                },
                separators=(",", ":"),
            )
            encoded_size = len(serialized.encode("utf-8"))
        retention_days = _environment_int(
            "MOUCHEN_CLOUD_AUDIT_RETENTION_DAYS",
            30,
            minimum=1,
            maximum=3650,
        )
        current = datetime.now(timezone.utc)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    "DELETE FROM cloud_slices WHERE created_at<?",
                    ((current - timedelta(days=retention_days)).isoformat(),),
                )
                rows = self._connection.execute(
                    """SELECT id,length(CAST(redacted_context_json AS BLOB)) AS bytes
                    FROM cloud_slices WHERE user_id=? ORDER BY created_at,id""",
                    (user_id,),
                ).fetchall()
                used = sum(int(row["bytes"] or 0) for row in rows)
                for row in rows:
                    if used + encoded_size <= quota:
                        break
                    self._connection.execute(
                        "DELETE FROM cloud_slices WHERE id=? AND user_id=?",
                        (row["id"], user_id),
                    )
                    used -= int(row["bytes"] or 0)
                self._connection.execute(
                    "INSERT INTO cloud_slices(user_id,provider,model,purpose,redacted_context_json,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        user_id,
                        provider,
                        model,
                        purpose,
                        serialized,
                        current.isoformat(),
                    ),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
