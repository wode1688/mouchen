from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from .advice_refiner import ProactiveAdviceRefiner
from .analysis_queue import AnalysisQueueProcessor
from .domain.models import (
    ContextSnapshot,
    EvaluationResult,
    Event,
    Goal,
    Sensitivity,
    utc_now,
)
from .domain.preferences import event_type_allowed
from .domain.problem_signals import event_text
from .model_gateway import ModelGateway
from .privacy import (
    consent_allows_restricted_minimized,
    consent_allows_restricted_raw,
)
from .service import (
    ProactiveService,
    _bounded_timezone_offset,
    _local_week_window,
    _session_matches_goal,
    _string_list,
)
from .storage import Repository


class ReviewTickResult(BaseModel):
    user_id: str
    reviewed_goals: int = 0
    evidence_events: int = 0
    duplicate_windows: int = 0
    evaluations: list[EvaluationResult] = Field(default_factory=list)


class GoalReviewEngine:
    """Review recent evidence against goals without requiring an error keyword."""

    def __init__(
        self,
        repository: Repository,
        service: ProactiveService,
        gateway: ModelGateway,
        *,
        review_window_hours: int = 24,
        recent_event_limit: int = 500,
    ) -> None:
        self.repository = repository
        self.service = service
        self.gateway = gateway
        self.review_window = timedelta(hours=max(1, min(168, review_window_hours)))
        self.recent_event_limit = max(20, min(2_000, recent_event_limit))

    async def tick(
        self,
        user_id: str,
        *,
        cloud_approved: bool = False,
        raw_cloud_approved: bool = False,
        context: ContextSnapshot | None = None,
        now: datetime | None = None,
    ) -> ReviewTickResult:
        moment = now or utc_now()
        gate_context = context or ContextSnapshot()
        goals = self.repository.current_goals(user_id)
        recent = [
            event
            for event in self.repository.recent_events(user_id, self.recent_event_limit)
            if moment - self.review_window <= event.occurred_at <= moment
            and not _is_review_event(event)
            and not bool(event.facts.get("historical_backfill"))
        ]
        result = ReviewTickResult(user_id=user_id, evidence_events=len(recent))
        queue_processor = AnalysisQueueProcessor(
            self.repository,
            self.service,
            self.gateway,
        )
        if cloud_approved:
            pending = await queue_processor.drain(user_id, gate_context, limit=10)
            result.evaluations.extend(pending.evaluations)

        for goal in goals:
            # Periodic reviews respect the goal's effective preference: a
            # paused goal produces nothing, the time review obeys the
            # app.foreground_session toggle, and the synthesized review event
            # is composed only from allowed original event types below.
            effective = self.repository.effective_preference(user_id, goal.id)
            if effective.paused:
                continue
            time_review = (
                self._time_review_event(user_id, goal, moment)
                if event_type_allowed(effective.event_types, "app.foreground_session")
                else None
            )
            if time_review is not None:
                result.reviewed_goals += 1
                evaluation = self.service.ingest(time_review, gate_context)
                if evaluation is not None:
                    if cloud_approved:
                        evaluation = await ProactiveAdviceRefiner(
                            self.repository,
                            self.gateway,
                        ).refine(
                            evaluation,
                            user_id,
                            allow_raw_cloud=raw_cloud_approved,
                        )
                        if (
                            evaluation.decision == "hold"
                            and any(
                                reason in evaluation.reason_codes
                                for reason in {
                                    "AI_REFINEMENT_UNAVAILABLE",
                                    "MODEL_OUTPUT_LOCALE_MISMATCH",
                                }
                            )
                            and self.repository.enqueue_analysis_job(
                                user_id,
                                time_review.event_id,
                                job_kind="refine_deterministic",
                                goal_id=goal.id,
                                cloud_approved=True,
                                raw_cloud_approved=raw_cloud_approved,
                            )
                        ):
                            analyzed = await queue_processor.process_event(
                                user_id,
                                time_review.event_id,
                                gate_context,
                            )
                            if analyzed.evaluations:
                                evaluation = analyzed.evaluations[0]
                    else:
                        evaluation = self.service.discard_unreviewed(evaluation)
                    result.evaluations.append(
                        evaluation.model_copy(
                            update={
                                "reason_codes": evaluation.reason_codes
                                + ["PERIODIC_GOAL_REVIEW"]
                            }
                        )
                    )
                else:
                    result.duplicate_windows += 1
                # A concrete allocation drift is stronger than a second model opinion
                # over the same evidence window.
                continue

            if not cloud_approved:
                continue
            selected = _select_goal_events(
                goal,
                recent,
                single_goal=len(goals) == 1,
                allowed_event_types=effective.event_types,
            )
            review_event = _build_review_event(user_id, goal, selected, moment, self.review_window)
            if review_event is None:
                continue
            result.reviewed_goals += 1
            if not self.repository.insert_event(review_event):
                result.duplicate_windows += 1
                continue
            review_raw_approved = raw_cloud_approved and (
                review_event.sensitivity != Sensitivity.RESTRICTED
                or consent_allows_restricted_raw(review_event.consent_scope)
            )
            if not self.repository.enqueue_analysis_job(
                user_id,
                review_event.event_id,
                cloud_approved=True,
                raw_cloud_approved=review_raw_approved,
            ):
                result.duplicate_windows += 1
                continue
            analyzed = await queue_processor.process_event(
                user_id,
                review_event.event_id,
                gate_context,
            )
            for evaluation in analyzed.evaluations:
                result.evaluations.append(
                    evaluation.model_copy(
                        update={
                            "reason_codes": evaluation.reason_codes
                            + ["PERIODIC_GOAL_REVIEW"]
                        }
                    )
                )
        return result

    def _time_review_event(self, user_id: str, goal: Goal, now: datetime) -> Event | None:
        weekly_hours = _positive_float(goal.target.get("weekly_hours"))
        if weekly_hours <= 0:
            return None
        recent_sessions = self.repository.events_since(
            user_id,
            "app.foreground_session",
            now - timedelta(days=8),
        )
        if not recent_sessions:
            return None
        timezone_offset = _bounded_timezone_offset(
            next(
                (
                    event.facts.get("timezone_offset_minutes")
                    for event in recent_sessions
                    if event.facts.get("timezone_offset_minutes") is not None
                ),
                0,
            )
        )
        week_start, week_end = _local_week_window(now, timezone_offset)
        sessions = [event for event in recent_sessions if week_start <= event.occurred_at <= now]
        if not sessions:
            return None
        elapsed_ratio = min(
            1.0,
            max(0.0, (now - week_start).total_seconds() / (week_end - week_start).total_seconds()),
        )
        expected_to_date = weekly_hours * elapsed_ratio
        if expected_to_date < 0.5:
            return None

        packages = _string_list(goal.target.get("packages"))
        total_ms = sum(_positive_float(event.facts.get("duration_ms")) for event in sessions)
        matched_ms = sum(
            _positive_float(event.facts.get("duration_ms"))
            for event in sessions
            if _session_matches_goal(event, goal, packages)
        )
        actual_hours = matched_ms / 3_600_000
        if actual_hours / expected_to_date >= 0.70:
            return None
        observed_hours = total_ms / 3_600_000
        if matched_ms <= 0 and observed_hours < max(0.5, expected_to_date * 0.25):
            # No matching activity is meaningful only when the collector has enough
            # foreground coverage to distinguish drift from missing data.
            return None

        latest = max(sessions, key=lambda item: (item.occurred_at, str(item.event_id)))
        reference = f"backend:time-review:{goal.id}:{latest.event_id}"
        return Event(
            user_id=user_id,
            source="backend.goal_review",
            type="time.allocation",
            occurred_at=now,
            facts={
                "domain": goal.domain,
                "actual_hours": actual_hours,
                "expected_hours": expected_to_date,
                "weekly_target_hours": weekly_hours,
                "week_elapsed_ratio": elapsed_ratio,
                "period_start_at": week_start.isoformat(),
                "period_end_at": week_end.isoformat(),
                "period_total_duration_ms": matched_ms,
                "observed_foreground_hours": observed_hours,
                "context": "periodic_goal_review",
            },
            confidence=0.94 if matched_ms > 0 else 0.88,
            sensitivity=Sensitivity.PERSONAL,
            consent_scope="alpha.local",
            evidence_ref=reference,
        )


def _select_goal_events(
    goal: Goal,
    events: list[Event],
    *,
    single_goal: bool,
    allowed_event_types: list[str] | None = None,
) -> list[Event]:
    # The composite review event is filtered HERE, on its constituent original
    # event types, before synthesis — the synthesized carrier event itself is
    # internal and exempt from direct type gating.
    eligible = [
        event
        for event in events
        if _cloud_review_eligible(event)
        and (
            allowed_event_types is None
            or event_type_allowed(allowed_event_types, event.type)
        )
    ]
    if single_goal:
        return eligible[:12]
    return [event for event in eligible if _event_matches_goal(event, goal)][:12]


def _cloud_review_eligible(event: Event) -> bool:
    if event.sensitivity == Sensitivity.RESTRICTED:
        if not consent_allows_restricted_minimized(event.consent_scope):
            return False
    return bool(_event_search_text(event))


def _event_matches_goal(event: Event, goal: Goal) -> bool:
    explicit_domain = str(event.facts.get("domain", "")).strip().casefold()
    if explicit_domain:
        return explicit_domain == goal.domain.casefold()
    searchable = _event_search_text(event).casefold()
    package = str(event.facts.get("package", "")).casefold()
    packages = _string_list(goal.target.get("packages"))
    if any(package == item or package.startswith(f"{item}.") for item in packages):
        return True
    keywords = _string_list(goal.target.get("keywords"))
    if any(keyword in searchable for keyword in keywords):
        return True
    return any(term in searchable for term in _goal_terms(goal))


def _goal_terms(goal: Goal) -> set[str]:
    source = f"{goal.title} {goal.quote[:240]}".casefold()
    terms = set(re.findall(r"[a-z0-9_-]{3,}", source))
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", source):
        terms.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return {term for term in terms if term not in {"目标", "完成", "本周", "事情", "重要"}}


def _event_search_text(event: Event) -> str:
    direct = event_text(event)
    structured = " ".join(
        str(event.facts.get(key, ""))
        for key in (
            "title",
            "subject",
            "summary",
            "app_label",
            "package",
            "window_title",
            "page_title",
            "site_domain",
        )
    )
    return re.sub(r"\s+", " ", f"{direct} {structured}").strip()


def _build_review_event(
    user_id: str,
    goal: Goal,
    selected: list[Event],
    now: datetime,
    window: timedelta,
) -> Event | None:
    if len(selected) < 2:
        return None
    lines: list[str] = []
    source_counts: Counter[str] = Counter()
    included: list[Event] = []
    size = 0
    for event in selected:
        excerpt = _event_search_text(event)[:280]
        if not excerpt:
            continue
        line = f"{event.type} | {event.source} | {excerpt}"
        if size + len(line) > 1_600:
            break
        lines.append(line)
        size += len(line) + 1
        source_counts[event.source] += 1
        included.append(event)
    if len(included) < 2:
        return None
    latest = max(included, key=lambda item: (item.occurred_at, str(item.event_id)))
    reference = f"backend:goal-review:{goal.id}:{latest.event_id}"
    restricted = any(item.sensitivity == Sensitivity.RESTRICTED for item in included)
    sensitivity = Sensitivity.RESTRICTED if restricted else Sensitivity.SENSITIVE
    confidence = sum(item.confidence for item in included) / len(included)
    return Event(
        user_id=user_id,
        source="backend.goal_review",
        type="ui.visible_text",
        occurred_at=now,
        facts={
            "domain": goal.domain,
            "context": "proactive_goal_review",
            "visible_text": "\n".join(lines),
            "activity_count": len(included),
            "source_counts": dict(source_counts),
            "included_event_ids": [str(item.event_id) for item in included],
            "window_started_at": (now - window).isoformat(),
            "window_ended_at": now.isoformat(),
        },
        confidence=min(0.96, confidence),
        sensitivity=sensitivity,
        consent_scope=(
            "owner_full_context"
            if restricted
            and all(
                item.sensitivity != Sensitivity.RESTRICTED
                or consent_allows_restricted_raw(item.consent_scope)
                for item in included
            )
            else "alpha.minimized_context"
            if restricted
            else "alpha.local"
        ),
        evidence_ref=reference,
    )


def _is_review_event(event: Event) -> bool:
    return event.source == "backend.goal_review" or event.facts.get("context") in {
        "periodic_goal_review",
        "proactive_goal_review",
    }


def _positive_float(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0
