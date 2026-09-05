from __future__ import annotations

import re
from datetime import datetime, timedelta

from .domain.detectors import detect
from .domain.derivation import derive_for_detection
from .domain.gates import evaluate_context
from .domain.problem_signals import choose_goal, detect_problem
from .domain.models import (
    AdviceLevel,
    AdviceRecord,
    AdviceStatus,
    ContextSnapshot,
    EvaluationResult,
    Event,
    utc_now,
)
from .domain.preferences import (
    REASON_PREFERENCE_IMPORTANT_ONLY,
    REASON_PREFERENCE_PAUSED,
    event_type_allowed,
    is_internal_composite_event,
    is_manual_problem_event,
)
from .domain.scoring import proactive_score
from .domain.trust import speaking_ceiling
from .storage import AdviceConflict, PreferenceLimitExceeded, Repository


class ProactiveService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def ingest(self, event: Event, context: ContextSnapshot | None = None) -> EvaluationResult | None:
        if not self.repository.insert_event(event):
            return None
        candidate = self.candidate_for_event(event)
        if candidate is None:
            return None
        return self.evaluate(candidate, context or ContextSnapshot())

    def candidate_for_event(self, event: Event, goal_id=None):
        """Rebuild the deterministic candidate from durable event and goal state."""

        detector_event = derive_for_detection(event)
        if goal_id is None:
            goals = self.repository.current_goals(event.user_id)
        else:
            goal = self.repository.get_goal(event.user_id, goal_id)
            goals = [goal] if goal is not None else []
        goal, relevance = choose_goal(detector_event, goals)
        if not goal:
            return None
        if not self._event_allowed_for_goal(event, goal.id):
            return None
        detector_event = self._usage_allocation(detector_event, goal)
        locale = self.repository.user_locale(event.user_id)
        return detect(detector_event, goal, locale) or detect_problem(
            detector_event,
            goal,
            relevance,
            locale,
        )

    def _event_allowed_for_goal(self, event: Event, goal_id) -> bool:
        """Goal-effective event-type filter for the deterministic path.

        Disabling an event type only stops proactive advice derived from it —
        collection, storage, and consent are untouched. Manually submitted
        problems and synthesized goal-review composites (already filtered at
        composition) bypass this direct check; time.allocation gates through
        app.foreground_session via the derived-alias table.
        """

        if is_manual_problem_event(event) or is_internal_composite_event(event):
            return True
        effective = self.repository.effective_preference(event.user_id, goal_id)
        return event_type_allowed(effective.event_types, event.type)

    def evaluate(
        self,
        candidate,
        context: ContextSnapshot,
        *,
        l3_review_passed: bool = False,
    ) -> EvaluationResult:
        score = proactive_score(candidate)
        charter_level, redline = self.repository.get_charter(candidate.user_id, candidate.domain)
        trust = self.repository.speaking_trust_summary(
            candidate.user_id,
            candidate.domain,
            candidate.requested_level,
        )
        ceiling = speaking_ceiling(trust, charter_level, redline)
        effective_level = min(candidate.requested_level, ceiling)
        effective_level, frozen_levels = self.repository.available_speaking_level(
            candidate.user_id,
            effective_level,
        )
        budget_reason_codes = [
            f"ERROR_BUDGET_{level.name}_FROZEN" for level in frozen_levels
        ]
        if frozen_levels:
            budget_reason_codes.append("ERROR_BUDGET_DOWNGRADED")
        if (
            not (candidate.adopted_expected_result or "").strip()
            or candidate.adopted_confidence is None
        ):
            return EvaluationResult(
                decision="hold",
                reason_codes=budget_reason_codes + ["ADOPTED_RESULT_PREDICTION_REQUIRED"],
                score=score,
                effective_level=effective_level,
            )
        topic_key = self.repository.topic_key_for(candidate)
        if self.repository.active_topic_suppression(
            candidate.user_id,
            topic_key,
        ) is not None:
            return EvaluationResult(
                decision="hold",
                reason_codes=budget_reason_codes + ["TOPIC_SUPPRESSED_BY_USER"],
                score=score,
                effective_level=effective_level,
            )
        # Advice-preference gates. They can only REDUCE proactive output: the
        # evidence bar, speaking-level ceilings, trust/error budgets, topic
        # suppression, dedupe and quiet-hours above/below all stay untouched.
        # A user-submitted manual problem bypasses event filtering and numeric
        # publish quotas/cooldown only. Paused, important-only, trust, safety,
        # evidence, suppression and dedupe remain authoritative.
        preference = self.repository.effective_preference(
            candidate.user_id, candidate.goal_id
        )
        if preference.paused:
            return EvaluationResult(
                decision="hold",
                reason_codes=budget_reason_codes + [REASON_PREFERENCE_PAUSED],
                score=score,
                effective_level=effective_level,
            )
        if preference.important_only and effective_level < AdviceLevel.L3:
            return EvaluationResult(
                decision="hold",
                reason_codes=budget_reason_codes
                + [REASON_PREFERENCE_IMPORTANT_ONLY],
                score=score,
                effective_level=effective_level,
            )
        automatic_emergency = (
            candidate.requested_level >= AdviceLevel.L3
            and candidate.urgency >= 0.95
            and candidate.impact >= 0.90
        )
        timing_blocked = context.sleeping or context.driving or context.in_meeting or context.quiet_hours
        emergency_override = timing_blocked and automatic_emergency and not context.emergency
        gate_context = context.model_copy(
            update={"emergency": context.emergency or automatic_emergency}
        )
        gate = evaluate_context(
            candidate,
            gate_context,
            effective_level,
            self.repository.active_duplicate(candidate.user_id, topic_key),
        )
        reason_codes = list(gate.reasons) + budget_reason_codes
        if emergency_override:
            reason_codes.append("EMERGENCY_TIMING_OVERRIDE")
        if not gate.allowed or gate.delivery == "merge":
            return EvaluationResult(
                decision=gate.delivery,
                reason_codes=reason_codes,
                score=score,
                effective_level=effective_level,
            )
        if score < 0.50:
            return EvaluationResult(
                decision="hold",
                reason_codes=reason_codes + ["EVIDENCE_VALUE_TOO_LOW"],
                score=score,
                effective_level=effective_level,
            )
        delivery = gate.delivery if score >= 0.72 else "brief"
        record = AdviceRecord(
            **candidate.model_dump(exclude={"topic_key"}),
            topic_key=topic_key,
            effective_level=effective_level,
            proactive_score=score,
            delivery=delivery,
            status=(
                AdviceStatus.ACTIVE
                if candidate.requested_level < AdviceLevel.L3 or l3_review_passed
                else AdviceStatus.PROVISIONAL
            ),
        )
        try:
            record = self.repository.insert_advice(record)
        except AdviceConflict:
            return EvaluationResult(
                decision="merge",
                reason_codes=reason_codes + ["EVIDENCE_ALREADY_HAS_ACTIVE_ADVICE"],
                score=score,
                effective_level=effective_level,
            )
        except PreferenceLimitExceeded as exc:
            return EvaluationResult(
                decision="hold",
                reason_codes=reason_codes + [exc.reason_code],
                score=score,
                effective_level=effective_level,
            )
        return EvaluationResult(
            decision="publish",
            reason_codes=reason_codes,
            score=score,
            effective_level=effective_level,
            advice=record,
        )

    def _candidate_is_manual_problem(self, candidate) -> bool:
        try:
            evidence = candidate.evidence[0]
        except (IndexError, TypeError):
            return False
        event = self.repository.get_event(candidate.user_id, evidence.event_id)
        return is_manual_problem_event(event)

    def discard_unreviewed(
        self,
        evaluation: EvaluationResult | None,
    ) -> EvaluationResult | None:
        """Fail closed when a required L3 review cannot be run."""

        if evaluation is None or evaluation.advice is None:
            return evaluation
        if evaluation.advice.status != AdviceStatus.PROVISIONAL:
            return evaluation
        self.repository.delete_advice(
            evaluation.advice.user_id,
            evaluation.advice.id,
        )
        return evaluation.model_copy(
            update={
                "decision": "hold",
                "advice": None,
                "reason_codes": evaluation.reason_codes
                + ["L3_SECOND_OPINION_REQUIRED"],
            }
        )

    def _usage_allocation(self, event: Event, goal) -> Event:
        if event.type != "app.foreground_session":
            return event
        expected = _positive_float(goal.target.get("weekly_hours"))
        packages = _string_list(goal.target.get("packages"))
        if expected <= 0 or not _session_matches_goal(event, goal, packages):
            return event
        now = utc_now()
        timezone_offset_minutes = _bounded_timezone_offset(
            event.facts.get("timezone_offset_minutes")
        )
        week_start, week_end = _local_week_window(now, timezone_offset_minutes)
        elapsed_ratio = min(
            1.0,
            max(0.0, (now - week_start).total_seconds() / (week_end - week_start).total_seconds()),
        )
        expected_to_date = expected * elapsed_ratio
        total_ms = 0.0
        for recent in self.repository.events_since(
            event.user_id,
            "app.foreground_session",
            week_start,
        ):
            if recent.occurred_at > now:
                continue
            if _session_matches_goal(recent, goal, packages):
                total_ms += _positive_float(recent.facts.get("duration_ms"))
        facts = dict(event.facts)
        facts.update(
            {
                "domain": goal.domain,
                "expected_hours": expected_to_date,
                "weekly_target_hours": expected,
                "week_elapsed_ratio": elapsed_ratio,
                "period_start_at": week_start.isoformat(),
                "period_end_at": week_end.isoformat(),
                "period_total_duration_ms": total_ms,
            }
        )
        return derive_for_detection(event.model_copy(update={"facts": facts}))


def _string_list(value) -> list[str]:
    if isinstance(value, str):
        value = value.replace("，", ",").split(",")
    if not isinstance(value, list):
        return []
    return [str(item).strip().casefold() for item in value if str(item).strip()]


def _package_matches(package_name: str, packages: list[str]) -> bool:
    return any(package_name == item or package_name.startswith(f"{item}.") for item in packages)


def _session_matches_goal(event: Event, goal, packages: list[str]) -> bool:
    facts = event.facts
    package_name = str(facts.get("package", "")).casefold()
    searchable = " ".join(
        str(facts.get(key, ""))
        for key in ("package", "app_label", "window_title", "site_domain", "page_title")
    ).casefold()
    if packages and _package_matches(package_name, packages):
        return True
    keywords = _string_list(goal.target.get("keywords"))
    if any(keyword in searchable for keyword in keywords):
        return True
    if packages or keywords:
        return False
    return any(term in searchable for term in _goal_match_terms(goal.title, goal.quote))


def _goal_match_terms(title: str, quote: str) -> set[str]:
    source = f"{title} {quote[:240]}".casefold()
    stop = {
        "this", "that", "with", "from", "will", "week", "goal", "finish",
        "目标", "完成", "重要", "事情", "本周", "最近", "一年", "工作", "项目",
    }
    terms = {value for value in re.findall(r"[a-z0-9_-]{4,}", source) if value not in stop}
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", source):
        if len(chunk) <= 4 and chunk not in stop:
            terms.add(chunk)
        for index in range(max(0, len(chunk) - 1)):
            value = chunk[index : index + 2]
            if value not in stop:
                terms.add(value)
    return terms


def _positive_float(value) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _bounded_timezone_offset(value) -> int:
    try:
        return max(-14 * 60, min(14 * 60, int(value)))
    except (TypeError, ValueError):
        return 0


def _local_week_window(now: datetime, timezone_offset_minutes: int) -> tuple[datetime, datetime]:
    offset = timedelta(minutes=timezone_offset_minutes)
    local_now = now + offset
    local_week_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=local_now.weekday()
    )
    week_start = local_week_start - offset
    return week_start, week_start + timedelta(days=7)
