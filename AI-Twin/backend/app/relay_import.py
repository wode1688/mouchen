"""Authenticated, bounded imports into the existing local business account."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid5, NAMESPACE_URL

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .domain.models import Event, EventCreate, FeedbackCreate, Goal, GoalCreate
from .storage import EventQuotaExceeded, FeedbackQuotaExceeded, GoalQuotaExceeded

MAX_RESPONSE_BYTES = 512 * 1024
SCHEMA = "ai-twin.sync/v1"


class RelayEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_name: Literal["ai-twin.sync/v1"] = Field(alias="schema")
    kind: Literal["request"]
    message_id: UUID
    sender: str = Field(min_length=1, max_length=48, pattern=r"^[a-zA-Z0-9_-]+$")
    created_at: datetime
    operation: Literal["goal.create", "event.create", "feedback.create", "snapshot.get"]
    body: dict[str, Any]

    @field_validator("created_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at requires a timezone")
        return value.astimezone(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _bounded_snapshot(repo, uid: str) -> dict[str, Any]:
    # Reserve room for the envelope. Keep the newest complete records and say
    # when the caller needs a later snapshot; never cut a JSON record in half.
    result: dict[str, Any] = {"goals": [], "advice": [], "truncated": False}
    remaining = MAX_RESPONSE_BYTES - 4096
    groups = (("goals", repo.list_goals(uid)), ("advice", repo.list_advice(uid, limit=101)))
    for name, records in groups:
        for record in records[:100]:
            value = record.model_dump(mode="json")
            size = len(_json(value).encode("utf-8")) + 1
            if size > remaining:
                result["truncated"] = True
                continue
            result[name].append(value)
            remaining -= size
        if len(records) > 100:
            result["truncated"] = True
    return result


def import_request(repo, service, uid: str, envelope: RelayEnvelope, *,
                   enforce_goal_limits, goal_count_limit: int, max_facts_bytes: int,
                   max_feedback: int, max_guidance: int, event_is_live,
                   analysis_preference_block) -> dict[str, Any]:
    try:
        canonical = _json(envelope.model_dump(mode="json", by_alias=True))
    except ValueError as exc:
        raise HTTPException(422, "relay request must contain finite JSON values") from exc
    if len(canonical.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise HTTPException(413, "relay request exceeds 512 KiB")
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    stable_id = uuid5(NAMESPACE_URL, f"{SCHEMA}:{uid}:{envelope.message_id}")

    def apply() -> dict[str, Any]:
        response: dict[str, Any] = {
            "schema": SCHEMA, "kind": "response",
            "message_id": str(uuid5(stable_id, "response")),
            "sender": os.getenv("MOUCHEN_RELAY_DEVICE_ID", "desktop"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "in_reply_to": str(envelope.message_id), "operation": envelope.operation,
            "ok": True,
        }
        try:
            # The authenticated local session is the only authority for tenant
            # identity. Reject identity fields instead of silently ignoring them.
            if any(name in envelope.body for name in ("user_id", "session_user_id")):
                raise HTTPException(422, "body must not contain account identity")
            if envelope.operation == "goal.create":
                body = GoalCreate.model_validate(envelope.body)
                enforce_goal_limits(body, uid, goal_id=stable_id)
                repo.insert_goal(Goal(id=stable_id, user_id=uid, **body.model_dump()),
                                 max_goals=goal_count_limit)
            elif envelope.operation == "event.create":
                body = EventCreate.model_validate(envelope.body)
                if len(_json(body.facts).encode("utf-8")) > max_facts_bytes:
                    raise HTTPException(413, "event facts are too large")
                event = Event(event_id=stable_id, user_id=uid, **body.model_dump())
                if event_is_live(event) and analysis_preference_block(event) is None:
                    service.discard_unreviewed(service.ingest(event))
                else:
                    repo.insert_event(event)
            elif envelope.operation == "feedback.create":
                advice_id = UUID(str(envelope.body.get("advice_id", "")))
                body = FeedbackCreate.model_validate({**envelope.body, "feedback_id": stable_id})
                if repo.get_advice(uid, advice_id) is None:
                    raise HTTPException(404, "advice not found")
                repo.record_feedback(uid, advice_id, body, max_feedback_per_user=max_feedback,
                                     max_guidance_per_advice=max_guidance)
            elif envelope.body:
                raise HTTPException(422, "snapshot.get requires an empty body")
            response["result"] = _bounded_snapshot(repo, uid)
        except (ValidationError, ValueError, HTTPException,
                EventQuotaExceeded, FeedbackQuotaExceeded, GoalQuotaExceeded) as exc:
            # Existing repository methods can write several tables. Roll back
            # those changes before persisting a terminal protocol error receipt.
            repo._connection.rollback()
            status = exc.status_code if isinstance(exc, HTTPException) else (
                507 if isinstance(exc, (EventQuotaExceeded, FeedbackQuotaExceeded, GoalQuotaExceeded))
                else 422)
            messages = {404: "advice not found", 413: "request is too large",
                        422: "invalid request body", 507: "local account quota exceeded",
                        409: "request conflicts with current account state"}
            response.update(ok=False, error={"code": str(status), "message": messages.get(status, "request rejected")})
        if len(_json(response).encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise RuntimeError("relay response exceeds its bound")
        return response

    try:
        return repo.atomic_relay_request(uid, str(envelope.message_id), digest, apply)
    except RelayMessageConflict as exc:
        raise HTTPException(409, "message_id was already used for different content") from exc


class RelayMessageConflict(ValueError):
    pass


class RelayTransactionConnection:
    """Reuse existing repository methods inside one locked outer transaction.

    Repository methods own BEGIN/commit/rollback. During the synchronous relay
    callback their transaction boundaries join the outer transaction instead.
    The Repository RLock remains held throughout; no network/await is permitted.
    """
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, parameters=()):
        if sql.strip().upper() in {"BEGIN", "BEGIN IMMEDIATE"}:
            return self.connection.execute("SELECT 1")
        return self.connection.execute(sql, parameters)

    def commit(self):
        pass

    def rollback(self):
        self.connection.execute("ROLLBACK TO relay_operation")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            self.rollback()
        return False
