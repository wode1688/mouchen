"""Global / per-goal advice preferences.

One authoritative catalog of automatically observed event types, one named
frequency-policy table, and the inheritance semantics for goal overrides.
Preferences steer WHERE the counselor looks and HOW OFTEN it speaks; they are
untrusted user preference data, never evidence, and they can only reduce
proactive output — they never lower evidence bars, raise speaking levels, or
bypass trust/error-budget/topic-suppression/dedupe/quiet-hours gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from ..localization import is_english
from .models import AdviceLevel, Event, utc_now


GLOBAL_SCOPE_KEY = "global"
DIRECTION_MAX_CHARS = 2000
DIRECTION_MODES = ("inherit", "append", "replace")

# The single authoritative catalog: stable IDs plus the Chinese labels every
# client must display. Desktop/mobile render from this list; they never copy it.
EVENT_TYPE_CATALOG: tuple[tuple[str, str], ...] = (
    ("notification.posted", "通知"),
    ("ui.visible_text", "屏幕内容"),
    ("ime.text_committed", "我的输入"),
    ("speech.transcript", "语音"),
    ("mail.received", "邮件"),
    ("calendar.scheduled", "日历"),
    ("app.foreground_session", "应用使用"),
    ("message.sms", "短信"),
    ("shared.text", "主动分享"),
    ("thought.note", "随手记/思考"),
)
EVENT_TYPE_IDS: tuple[str, ...] = tuple(item[0] for item in EVENT_TYPE_CATALOG)
EVENT_TYPE_LABELS: dict[str, str] = dict(EVENT_TYPE_CATALOG)
EVENT_TYPE_LABELS_EN: dict[str, str] = {
    "notification.posted": "Notifications",
    "ui.visible_text": "Visible screen text",
    "ime.text_committed": "My typing",
    "speech.transcript": "Speech",
    "mail.received": "Email",
    "calendar.scheduled": "Calendar",
    "app.foreground_session": "App usage",
    "message.sms": "SMS",
    "shared.text": "Shared with My AI Twin",
    "thought.note": "Notes and thoughts",
}

# Internal derived types are never shown directly; they gate through the
# original type they were derived from.
DERIVED_EVENT_TYPE_ALIASES: dict[str, str] = {
    "time.allocation": "app.foreground_session",
}


@dataclass(frozen=True)
class FrequencyPolicy:
    """Named proactive-output policy. All numbers live here, nowhere else."""

    mode: str
    label: str
    max_per_window: int | None  # None = no numeric cap
    window: timedelta
    cooldown: timedelta
    min_level: AdviceLevel | None  # publish requires effective_level >= min_level
    paused: bool


FREQUENCY_POLICIES: dict[str, FrequencyPolicy] = {
    "active": FrequencyPolicy(
        "active", "积极", 8, timedelta(hours=24), timedelta(minutes=60), None, False
    ),
    "balanced": FrequencyPolicy(
        "balanced", "均衡", 3, timedelta(hours=24), timedelta(hours=4), None, False
    ),
    "quiet": FrequencyPolicy(
        "quiet", "克制", 1, timedelta(hours=24), timedelta(hours=12), None, False
    ),
    "important_only": FrequencyPolicy(
        "important_only",
        "仅重要",
        None,
        timedelta(hours=24),
        timedelta(0),
        AdviceLevel.L3,
        False,
    ),
    "paused": FrequencyPolicy(
        "paused", "暂停主动建言", 0, timedelta(hours=24), timedelta(0), None, True
    ),
}
FREQUENCY_MODES: tuple[str, ...] = tuple(FREQUENCY_POLICIES)
DEFAULT_FREQUENCY_MODE = "active"
FREQUENCY_LABELS_EN: dict[str, str] = {
    "active": "Active",
    "balanced": "Balanced",
    "quiet": "Quiet",
    "important_only": "Important only",
    "paused": "Pause proactive counsel",
}

# Stable reason codes (evaluation reason_codes keep the project's uppercase
# style; analysis-job reasons are normalized to lowercase by _analysis_reason).
REASON_PREFERENCE_PAUSED = "PREFERENCE_PAUSED"
REASON_PREFERENCE_EVENT_TYPE_DISABLED = "PREFERENCE_EVENT_TYPE_DISABLED"
REASON_PREFERENCE_IMPORTANT_ONLY = "PREFERENCE_IMPORTANT_ONLY"
REASON_PREFERENCE_GLOBAL_LIMIT = "PREFERENCE_GLOBAL_LIMIT"
REASON_PREFERENCE_GOAL_LIMIT = "PREFERENCE_GOAL_LIMIT"
REASON_PREFERENCE_GOAL_COOLDOWN = "PREFERENCE_GOAL_COOLDOWN"

# Advice statuses that were genuinely published once; provisional never counts.
PUBLISHED_ADVICE_STATUSES: tuple[str, ...] = (
    "active",
    "adopted",
    "dismissed",
    "withdrawn",
    "verified",
)


def goal_scope_key(goal_id: UUID | str) -> str:
    return f"goal:{goal_id}"


def canonical_event_type(event_type: str) -> str:
    value = str(event_type or "").strip()
    return DERIVED_EVENT_TYPE_ALIASES.get(value, value)


def is_catalog_event_type(event_type: str) -> bool:
    return canonical_event_type(event_type) in EVENT_TYPE_LABELS


def normalize_event_types(values: list[str]) -> list[str]:
    """Deduplicate in catalog order; unknown IDs raise ValueError (422)."""

    requested = {str(item).strip() for item in values}
    unknown = sorted(item for item in requested if item not in EVENT_TYPE_LABELS)
    if unknown:
        raise ValueError(f"unknown event types: {', '.join(unknown)}")
    return [item for item in EVENT_TYPE_IDS if item in requested]


def event_type_allowed(effective_event_types: list[str], event_type: str) -> bool:
    """Whether automatic advice may be generated from this event type.

    Types outside the catalog (after derived-alias mapping) are internal and
    stay allowed — their gating happens where they are composed. An empty list
    is an explicit "no automatic event advice at all" and must never be treated
    as a missing value.
    """

    canonical = canonical_event_type(event_type)
    if canonical not in EVENT_TYPE_LABELS:
        return True
    return canonical in effective_event_types


def is_manual_problem_event(event: Event | None) -> bool:
    """User-submitted problems bypass frequency and event filtering (only)."""

    if event is None:
        return False
    return str(event.facts.get("context", "")) == "manual_problem"


def is_internal_composite_event(event: Event) -> bool:
    """Synthesized goal-review events are gated by their constituents, not by
    their own carrier type. time.allocation is NOT exempt: it gates through
    app.foreground_session via the derived-alias table."""

    return (
        event.source == "backend.goal_review"
        and str(event.facts.get("context", "")) == "proactive_goal_review"
    )


class AdvicePreference(BaseModel):
    user_id: str
    scope_key: str
    goal_id: UUID | None = None
    direction: str = ""
    direction_mode: str = "inherit"
    frequency_mode: str | None = None  # None on a goal row = inherit global
    event_types: list[str] | None = None  # None = inherit; [] = block all
    revision: int = Field(default=0, ge=0)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("direction")
    @classmethod
    def _strip_direction(cls, value: str) -> str:
        stripped = str(value or "").strip()
        if len(stripped) > DIRECTION_MAX_CHARS:
            raise ValueError(f"direction exceeds {DIRECTION_MAX_CHARS} characters")
        return stripped

    @field_validator("direction_mode")
    @classmethod
    def _check_direction_mode(cls, value: str) -> str:
        if value not in DIRECTION_MODES:
            raise ValueError(f"direction_mode must be one of {DIRECTION_MODES}")
        return value

    @field_validator("frequency_mode")
    @classmethod
    def _check_frequency_mode(cls, value: str | None) -> str | None:
        if value is not None and value not in FREQUENCY_POLICIES:
            raise ValueError(f"frequency_mode must be one of {FREQUENCY_MODES}")
        return value

    @field_validator("event_types")
    @classmethod
    def _check_event_types(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return normalize_event_types(value)


def default_global_preference(user_id: str) -> AdvicePreference:
    """Cold-start default: explore actively across every event type with no
    direction bias, so early feedback can train the counselor quickly."""

    return AdvicePreference(
        user_id=user_id,
        scope_key=GLOBAL_SCOPE_KEY,
        goal_id=None,
        direction="",
        direction_mode="replace",
        frequency_mode=DEFAULT_FREQUENCY_MODE,
        event_types=list(EVENT_TYPE_IDS),
        revision=0,
    )


class EffectivePreference(BaseModel):
    """The resolved view one goal (or the global scope) actually operates under."""

    goal_id: UUID | None = None
    global_direction: str = ""
    goal_direction: str = ""
    direction_mode: str = "inherit"
    directions: list[str] = Field(default_factory=list)  # prompt order
    global_frequency_mode: str = DEFAULT_FREQUENCY_MODE
    frequency_mode: str = DEFAULT_FREQUENCY_MODE  # goal-effective (inherit resolved)
    goal_frequency_override: str | None = None
    event_types: list[str] = Field(default_factory=list)
    goal_event_types_override: list[str] | None = None
    paused: bool = False
    important_only: bool = False

    def model_context(self) -> dict[str, Any]:
        """Compact structure for prompt injection (before cloud redaction)."""

        return {
            "global_direction": self.global_direction,
            "goal_direction": self.goal_direction,
            "direction_mode": self.direction_mode,
            "frequency_mode": self.frequency_mode,
            "event_types": list(self.event_types),
        }


def resolve_effective(
    global_pref: AdvicePreference,
    goal_pref: AdvicePreference | None,
) -> EffectivePreference:
    global_frequency = global_pref.frequency_mode or DEFAULT_FREQUENCY_MODE
    global_events = (
        list(global_pref.event_types)
        if global_pref.event_types is not None
        else list(EVENT_TYPE_IDS)
    )
    if goal_pref is None:
        directions = [global_pref.direction] if global_pref.direction else []
        return EffectivePreference(
            goal_id=None,
            global_direction=global_pref.direction,
            goal_direction="",
            direction_mode="inherit",
            directions=directions,
            global_frequency_mode=global_frequency,
            frequency_mode=global_frequency,
            goal_frequency_override=None,
            event_types=global_events,
            goal_event_types_override=None,
            paused=FREQUENCY_POLICIES[global_frequency].paused,
            important_only=global_frequency == "important_only",
        )

    if goal_pref.direction_mode == "replace":
        directions = [goal_pref.direction] if goal_pref.direction else []
    elif goal_pref.direction_mode == "append":
        # Global first, goal appended; the prompt states the goal direction
        # wins whenever the two conflict.
        directions = [
            item
            for item in (global_pref.direction, goal_pref.direction)
            if item
        ]
    else:  # inherit
        directions = [global_pref.direction] if global_pref.direction else []

    # Inheritance MUST use explicit None checks: [] is a real value meaning
    # "no automatic event advice for this goal" and `or` would destroy it.
    if goal_pref.frequency_mode is not None:
        frequency = goal_pref.frequency_mode
    else:
        frequency = global_frequency
    if goal_pref.event_types is not None:
        event_types = list(goal_pref.event_types)
    else:
        event_types = global_events

    global_policy = FREQUENCY_POLICIES[global_frequency]
    goal_policy = FREQUENCY_POLICIES[frequency]
    return EffectivePreference(
        goal_id=goal_pref.goal_id,
        global_direction=global_pref.direction,
        goal_direction=goal_pref.direction,
        direction_mode=goal_pref.direction_mode,
        directions=directions,
        global_frequency_mode=global_frequency,
        frequency_mode=frequency,
        goal_frequency_override=goal_pref.frequency_mode,
        event_types=event_types,
        goal_event_types_override=(
            list(goal_pref.event_types) if goal_pref.event_types is not None else None
        ),
        # Global paused is the master switch; a goal override cannot escape it.
        paused=global_policy.paused or goal_policy.paused,
        important_only=(
            global_frequency == "important_only" or frequency == "important_only"
        ),
    )


def frequency_policy(mode: str) -> FrequencyPolicy:
    return FREQUENCY_POLICIES[mode]


def frequency_policy_payload(locale: str = "zh-CN") -> dict[str, dict[str, Any]]:
    """Serialized policy table for GET /v1/advice-preferences (single source)."""

    payload: dict[str, dict[str, Any]] = {}
    for mode, policy in FREQUENCY_POLICIES.items():
        payload[mode] = {
            "label": FREQUENCY_LABELS_EN[mode] if is_english(locale) else policy.label,
            "max_per_window": policy.max_per_window,
            "window_hours": policy.window.total_seconds() / 3600,
            "cooldown_minutes": policy.cooldown.total_seconds() / 60,
            "min_level": policy.min_level.name if policy.min_level else None,
            "paused": policy.paused,
        }
    return payload


def event_type_catalog_payload(locale: str = "zh-CN") -> list[dict[str, str]]:
    if is_english(locale):
        return [
            {"id": item, "label": EVENT_TYPE_LABELS_EN[item]}
            for item, _label in EVENT_TYPE_CATALOG
        ]
    return [{"id": item, "label": label} for item, label in EVENT_TYPE_CATALOG]


def automatic_emergency_record(record: Any) -> bool:
    """Mirror of the service's automatic_emergency rule for the publish gate:
    such advice bypasses NUMERIC quotas only — never paused, never disabled
    event types, never consent/evidence/review/external-action gates."""

    try:
        return (
            record.requested_level >= AdviceLevel.L3
            and float(record.urgency) >= 0.95
            and float(record.impact) >= 0.90
        )
    except (AttributeError, TypeError, ValueError):
        return False
