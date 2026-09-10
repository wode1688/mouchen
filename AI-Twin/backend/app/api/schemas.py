from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from ..domain.models import AdviceLevel, ContextSnapshot, Event, Goal
from ..domain.preferences import (
    DIRECTION_MAX_CHARS,
    DIRECTION_MODES,
    FREQUENCY_POLICIES,
    normalize_event_types,
)
from ..localization import DEFAULT_LOCALE, normalize_locale


class CharterUpdate(BaseModel):
    max_level: AdviceLevel = AdviceLevel.L2
    redline_authorized: bool = False


class AuthRegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)
    device_id: str = Field(min_length=1, max_length=160)
    device_name: str | None = Field(default=None, max_length=120)
    registration_code: str | None = Field(default=None, max_length=512)
    locale: str = DEFAULT_LOCALE

    @field_validator("locale")
    @classmethod
    def _check_locale(cls, value: str) -> str:
        return normalize_locale(value)


class AuthLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)
    device_id: str = Field(min_length=1, max_length=160)
    device_name: str | None = Field(default=None, max_length=120)


class AccountPasswordChangeRequest(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(min_length=10, max_length=1024)


class AccountDeleteRequest(BaseModel):
    current_password: str = Field(max_length=1024)


class AccountExportRequest(BaseModel):
    current_password: str = Field(max_length=1024)


class AuthSessionView(BaseModel):
    user_id: str
    username: str | None
    device_id: str
    scopes: list[str]
    expires_at: datetime | None
    locale: str = DEFAULT_LOCALE


class AccountPreferencesUpdate(BaseModel):
    locale: str

    @field_validator("locale")
    @classmethod
    def _check_locale(cls, value: str) -> str:
        return normalize_locale(value)


class AccountPreferencesView(BaseModel):
    locale: str
    updated_at: datetime | None = None


class AuthTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    session: AuthSessionView


class EventIngestResponse(BaseModel):
    event: Event
    evaluation: Any | None = None
    queued: bool = False


class ModelAnalyzeRequest(BaseModel):
    level: AdviceLevel
    purpose: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=8000)
    redacted_context: dict[str, Any] = Field(default_factory=dict)
    outbound_approved: bool = False
    force_private_7b: bool = False


class ModelAnalyzeResponse(BaseModel):
    provider: str
    model: str
    content: str
    second_opinion: str | None = None
    degraded: bool = False


class DemoSeedResponse(BaseModel):
    goal: Goal
    event: Event
    evaluation: Any


class ContextEventRequest(BaseModel):
    context: ContextSnapshot = Field(default_factory=ContextSnapshot)


class AdviceAttentionClaimRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=160)
    platform: str | None = Field(default=None, max_length=32)
    app_version: str | None = Field(default=None, max_length=80)


class AdviceAttentionCompleteRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=160)
    claim_token: UUID


class AdviceAttentionFailRequest(AdviceAttentionCompleteRequest):
    reason: str | None = Field(default=None, max_length=240)


class AdvicePreferenceUpdate(BaseModel):
    """PUT body for /v1/advice-preferences/{global|goals/{goal_id}}.

    For the goal scope, frequency_mode=None / event_types=None mean "inherit
    the global setting" while event_types=[] explicitly blocks every automatic
    event type; the global scope must always be concrete (enforced in main).
    """

    direction: str = ""
    direction_mode: str = "inherit"
    frequency_mode: str | None = None
    event_types: list[str] | None = None
    revision: int = Field(default=0, ge=0)

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
            raise ValueError(
                f"frequency_mode must be one of {tuple(FREQUENCY_POLICIES)}"
            )
        return value

    @field_validator("event_types")
    @classmethod
    def _check_event_types(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return normalize_event_types(value)
