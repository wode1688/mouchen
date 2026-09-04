from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AdviceLevel(IntEnum):
    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4

    @property
    def label(self) -> str:
        return self.name

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str) and value.upper() in cls.__members__:
            return cls[value.upper()]
        return None


class Sensitivity(str, Enum):
    PUBLIC = "public"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


class AdviceStatus(str, Enum):
    PROVISIONAL = "provisional"
    ACTIVE = "active"
    ADOPTED = "adopted"
    DISMISSED = "dismissed"
    WITHDRAWN = "withdrawn"
    VERIFIED = "verified"


class OutcomeStatus(str, Enum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNKNOWN = "unknown"


class GoalCreate(BaseModel):
    domain: str = Field(min_length=1, max_length=80)
    quote: str = Field(min_length=1, max_length=4000)
    title: str = Field(min_length=1, max_length=200)
    target: dict[str, Any] = Field(default_factory=dict)
    is_redline: bool = False
    valid_until: datetime | None = None


class Goal(GoalCreate):
    id: UUID = Field(default_factory=uuid4)
    user_id: str
    version: int = 1
    created_at: datetime = Field(default_factory=utc_now)
    reaffirmed_at: datetime = Field(default_factory=utc_now)


class EventCreate(BaseModel):
    source: str = Field(min_length=1, max_length=80)
    type: str = Field(pattern=r"^[a-z0-9_.-]+$")
    occurred_at: datetime = Field(default_factory=utc_now)
    facts: dict[str, Any] = Field(default_factory=dict)
    entities: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0, le=1)
    sensitivity: Sensitivity = Sensitivity.PERSONAL
    consent_scope: str = "alpha.local"
    evidence_ref: str | None = None


class Event(EventCreate):
    event_id: UUID = Field(default_factory=uuid4)
    user_id: str
    created_at: datetime = Field(default_factory=utc_now)


class Evidence(BaseModel):
    event_id: UUID
    source: str
    fact: str
    observed_at: datetime
    confidence: float = Field(ge=0, le=1)


class Prediction(BaseModel):
    outcome: str = Field(min_length=1, max_length=1000)
    deadline: datetime
    confidence: float = Field(ge=0, le=1)
    observable: bool = True


class AdviceCandidate(BaseModel):
    user_id: str
    domain: str
    requested_level: AdviceLevel
    goal_id: UUID
    goal_quote: str
    evidence: list[Evidence] = Field(min_length=1)
    action: str = Field(min_length=1, max_length=2000)
    first_step: str = Field(min_length=1, max_length=1000)
    alternative: str | None = Field(default=None, max_length=2000)
    prediction: Prediction
    # ``prediction`` remains the counterfactual risk when the user does not act.
    # These optional fields describe the separately observable result expected
    # after adoption, so successful prevention is not scored as a failed risk
    # prediction. They are optional for compatibility with existing ledgers.
    adopted_expected_result: str | None = Field(default=None, max_length=1000)
    adopted_confidence: float | None = Field(default=None, ge=0, le=1)
    urgency: float = Field(ge=0, le=1)
    impact: float = Field(ge=0, le=1)
    novelty: float = Field(ge=0, le=1)
    relevance: float = Field(ge=0, le=1)
    context_fit: float = Field(default=1.0, ge=0, le=1)
    interruption_cost: float = Field(default=0.0, ge=0, le=1)
    dedupe_key: str = Field(min_length=1, max_length=240)
    # Stable issue identity used for cross-event and cross-device de-duplication.
    # ``dedupe_key`` is retained for wire/storage compatibility with existing
    # clients, while older records are migrated to a canonical topic key.
    topic_key: str | None = Field(default=None, min_length=1, max_length=240)
    # Stable object-plus-problem-type phrase returned by semantic discovery.
    # Volatile dates, counts, urgency and proposed solutions are excluded so
    # the same real-world issue keeps one identity across changing events.
    issue_subject: str | None = Field(default=None, min_length=1, max_length=240)
    requires_external_action: bool = False

    @field_validator("alternative")
    @classmethod
    def warning_requires_alternative(cls, value: str | None, info):
        level = info.data.get("requested_level")
        if level is not None and level >= AdviceLevel.L3 and not value:
            raise ValueError("L3/L4 advice requires an alternative path")
        return value


class AdviceRecord(AdviceCandidate):
    id: UUID = Field(default_factory=uuid4)
    effective_level: AdviceLevel
    proactive_score: float = Field(ge=0, le=1)
    delivery: str
    status: AdviceStatus = AdviceStatus.ACTIVE
    snoozed_until: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class FeedbackCreate(BaseModel):
    feedback_id: UUID | None = None
    kind: str = Field(
        pattern=(
            r"^(adopted|useful|irrelevant|fact_error|prediction_error|timing_error|"
            r"dismissed|stop_topic|guidance|later|acknowledged)$"
        )
    )
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def guidance_requires_note(self):
        if self.kind == "guidance" and not (self.note or "").strip():
            raise ValueError("guidance feedback requires a non-empty note")
        return self


class OutcomeCreate(BaseModel):
    status: OutcomeStatus
    actual_result: str = Field(min_length=1, max_length=4000)
    utility: float | None = Field(default=None, ge=0, le=1)
    timing_quality: float | None = Field(default=None, ge=0, le=1)


class ContextSnapshot(BaseModel):
    consent_active: bool = True
    sleeping: bool = False
    driving: bool = False
    in_meeting: bool = False
    quiet_hours: bool = False
    emergency: bool = False
    user_requested_pause: bool = False


class EvaluationRequest(BaseModel):
    candidate: AdviceCandidate
    context: ContextSnapshot = Field(default_factory=ContextSnapshot)


class EvaluationResult(BaseModel):
    decision: str
    reason_codes: list[str]
    score: float
    effective_level: AdviceLevel
    advice: AdviceRecord | None = None
