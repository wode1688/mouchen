from __future__ import annotations

from datetime import timedelta

from ..localization import is_english
from .models import (
    AdviceCandidate,
    AdviceLevel,
    Event,
    Evidence,
    Goal,
    Prediction,
    utc_now,
)


def detect(event: Event, goal: Goal, locale: str = "zh-CN") -> AdviceCandidate | None:
    if event.type == "time.allocation":
        return _time_allocation(event, goal, locale)
    if event.type == "commitment.slipped":
        return _commitment_slip(event, goal, locale)
    if event.type == "threat.detected":
        return _external_threat(event, goal, locale)
    return None


def _evidence(event: Event, fact: str) -> Evidence:
    return Evidence(
        event_id=event.event_id,
        source=event.source,
        fact=fact,
        observed_at=event.occurred_at,
        confidence=event.confidence,
    )


def _time_allocation(event: Event, goal: Goal, locale: str) -> AdviceCandidate | None:
    expected = float(event.facts.get("expected_hours", goal.target.get("weekly_hours", 0)))
    weekly_target = float(event.facts.get("weekly_target_hours", expected))
    actual = float(event.facts.get("actual_hours", 0))
    if expected <= 0:
        return None
    ratio = actual / expected
    if ratio >= 0.70:
        return None
    level = AdviceLevel.L3 if ratio < 0.25 else AdviceLevel.L2
    english = is_english(locale)
    evidence_fact = (
        f"{actual:.1f} hours invested so far this week; progress toward the "
        f"{weekly_target:.1f}-hour weekly target calls for {expected:.1f} hours by now"
        if english
        else f"本周截至目前投入 {actual:.1f} 小时，按周目标 {weekly_target:.1f} 小时的进度应投入 {expected:.1f} 小时"
    )
    missing_hours = max(0.5, expected - actual)
    return AdviceCandidate(
        user_id=event.user_id,
        domain=goal.domain,
        requested_level=level,
        goal_id=goal.id,
        goal_quote=goal.quote,
        evidence=[
            _evidence(
                event,
                evidence_fact,
            )
        ],
        action=(
            f'Schedule at least {missing_hours:.1f} additional hours for "{goal.title}" during the rest of this week'
            if english
            else f"在本周剩余时间为“{goal.title}”补排至少 {missing_hours:.1f} 小时"
        ),
        first_step=(
            "Choose the least interruptible time block now and create a calendar draft"
            if english
            else "现在选择一个最不容易被打断的时间段并建立日历草稿"
        ),
        alternative=(
            "If this week's commitment is objectively infeasible, revise the goal version and record the trade-off"
            if english
            else "若本周客观无法投入，明确调整目标版本并记录取舍"
        ),
        prediction=Prediction(
            outcome=(
                "At the current allocation rate, this week's planned investment for the goal will not be completed"
                if english
                else "若本周继续维持当前投入比例，该目标的本周投入计划将无法完成"
            ),
            deadline=utc_now()
            + timedelta(
                days=max(1, int(7 * (1 - float(event.facts.get("week_elapsed_ratio", 0)))))
            ),
            confidence=min(0.95, 0.65 + (1 - ratio) * 0.25),
        ),
        adopted_expected_result=(
            f'By the end of this week, "{goal.title}" has received {weekly_target:.1f} total hours, or a revised goal version with an explicit trade-off is saved'
            if english
            else (
                f"本周结束前，“{goal.title}”累计投入达到 {weekly_target:.1f} 小时，"
                "或已保存一版明确记录取舍的新目标版本"
            )
        ),
        adopted_confidence=min(0.90, 0.68 + (1 - ratio) * 0.18),
        urgency=min(1.0, 0.45 + (1 - ratio) * 0.45),
        impact=float(event.facts.get("impact", 0.75)),
        novelty=float(event.facts.get("novelty", 0.70)),
        relevance=1.0,
        context_fit=float(event.facts.get("context_fit", 1.0)),
        interruption_cost=0.25,
        dedupe_key=f"time-drift:{goal.id}",
    )


def _commitment_slip(event: Event, goal: Goal, locale: str) -> AdviceCandidate | None:
    slips = int(event.facts.get("reschedule_count", 0))
    if slips < 2:
        return None
    level = AdviceLevel.L3 if slips >= 3 else AdviceLevel.L2
    english = is_english(locale)
    return AdviceCandidate(
        user_id=event.user_id,
        domain=goal.domain,
        requested_level=level,
        goal_id=goal.id,
        goal_quote=goal.quote,
        evidence=[
            _evidence(
                event,
                f"The same commitment has been rescheduled {slips} consecutive times"
                if english
                else f"同一承诺已连续改期 {slips} 次",
            )
        ],
        action=(
            "Reduce the commitment to a deliverable that fits within 30 minutes, or cancel it explicitly and notify the people involved"
            if english
            else "把承诺拆成一个30分钟内可完成的交付，或明确撤销并通知相关人"
        ),
        first_step=(
            "Write down the smallest deliverable and one new, final deadline"
            if english
            else "写下当前承诺最小可交付物和新的唯一截止时间"
        ),
        alternative=(
            "If the commitment is no longer valid, cancel it proactively instead of moving the date again"
            if english
            else "若承诺已经失效，主动撤销而不是继续滑动日期"
        ),
        prediction=Prediction(
            outcome=(
                "Without changing the task definition, this commitment will be rescheduled again"
                if english
                else "若不改变任务定义，该承诺会再次改期"
            ),
            deadline=utc_now() + timedelta(days=3),
            confidence=min(0.95, 0.60 + 0.08 * slips),
        ),
        adopted_expected_result=(
            "Within three days, the commitment is reduced to a deliverable that fits within 30 minutes and completed by one final deadline, or it is explicitly cancelled and the people involved are notified"
            if english
            else (
                "未来三天内，该承诺已形成一个30分钟内可完成的交付并按唯一截止时间完成，"
                "或已明确撤销并通知相关人"
            )
        ),
        adopted_confidence=min(0.90, 0.66 + 0.06 * slips),
        urgency=0.75,
        impact=float(event.facts.get("impact", 0.70)),
        novelty=0.60,
        relevance=1.0,
        context_fit=float(event.facts.get("context_fit", 1.0)),
        interruption_cost=0.20,
        dedupe_key=f"commitment-slip:{goal.id}",
    )


def _external_threat(event: Event, goal: Goal, locale: str) -> AdviceCandidate | None:
    relevance = float(event.facts.get("relevance", 0))
    source_count = int(event.facts.get("independent_sources", 1))
    primary = bool(event.facts.get("primary_source", False))
    if relevance < 0.70:
        return None
    qualified = primary or source_count >= 2
    level = AdviceLevel.L3 if qualified and relevance >= 0.90 else AdviceLevel.L2
    english = is_english(locale)
    summary = str(
        event.facts.get(
            "summary",
            "Goal-relevant external change" if english else "目标相关外部变化",
        )
    )[:500]
    horizon_days = int(event.facts.get("horizon_days", 14))
    return AdviceCandidate(
        user_id=event.user_id,
        domain=goal.domain,
        requested_level=level,
        goal_id=goal.id,
        goal_quote=goal.quote,
        evidence=[_evidence(event, summary)],
        action=(
            "Verify the original source and assess whether the current plan should change"
            if english
            else "核对原始来源，并评估是否需要调整当前计划"
        ),
        first_step=(
            "Open the original source and confirm the change's scope and effective date"
            if english
            else "打开原始来源，确认变化的适用范围和生效时间"
        ),
        alternative=(
            "If evidence is insufficient, add it to a watchlist and wait for confirmation from a second source"
            if english
            else "证据不足时加入观察列表，等待第二来源确认"
        ),
        prediction=Prediction(
            outcome=(
                "This change will affect the current plan within the goal period"
                if english
                else "该变化将在目标周期内影响当前计划"
            ),
            deadline=utc_now() + timedelta(days=horizon_days),
            confidence=min(event.confidence, 0.90 if qualified else 0.65),
        ),
        adopted_expected_result=(
            f'Within {horizon_days} days, the original source is verified and a clear "adjust the current plan" or "continue monitoring" decision is recorded'
            if english
            else f"在 {horizon_days} 天内完成原始来源核验，并留下“调整当前计划”或“继续观察”的明确记录"
        ),
        adopted_confidence=min(event.confidence, 0.84 if qualified else 0.72),
        urgency=float(event.facts.get("urgency", 0.65)),
        impact=float(event.facts.get("impact", 0.75)),
        novelty=float(event.facts.get("novelty", 0.90)),
        relevance=relevance,
        context_fit=float(event.facts.get("context_fit", 1.0)),
        interruption_cost=0.15,
        dedupe_key=f"external-threat:{goal.id}:{event.facts.get('topic', 'general')}",
    )
