from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.analysis_queue import AnalysisDrainResult, AnalysisQueueProcessor
from app.domain.models import (
    AdviceCandidate,
    AdviceLevel,
    ContextSnapshot,
    EvaluationResult,
    Event,
    FeedbackCreate,
    Goal,
)
from app.domain.problem_signals import choose_goal, detect_problem
from app.service import ProactiveService
from app.storage import Repository


def _goal(
    *,
    title: str = "Keep the AI business operational",
    quote: str = "I will resolve material AI business problems",
    keywords: list[str] | None = None,
    domain: str = "business",
) -> Goal:
    return Goal(
        user_id="u1",
        domain=domain,
        title=title,
        quote=quote,
        target={"keywords": keywords or ["AI", "business"]},
    )


def test_code_editor_text_is_not_a_deterministic_runtime_failure_or_deadline():
    future = (datetime.now(timezone.utc) + timedelta(days=90)).date().isoformat()
    event = Event(
        user_id="u1",
        source="windows.ui_automation",
        type="ui.visible_text",
        facts={
            "process": "Code.exe",
            "window_title": "problem_signals.py - Visual Studio Code",
            "visible_text": (
                "if response.error: raise TimeoutError; "
                f'deadline = "{future}"'
            ),
        },
    )

    assert detect_problem(event, _goal(), 0.9) is None


def test_future_calendar_date_is_not_treated_as_a_near_term_deadline():
    future = (datetime.now(timezone.utc) + timedelta(days=60)).date().isoformat()
    event = Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={
            "title": "Project Atlas planning",
            "text": f"The project deadline is {future}",
            "content_kind": "system_notice",
            "speaker": "system",
            "evidence_strength": "direct",
            "resolution_state": "unresolved",
        },
    )

    assert detect_problem(event, _goal(), 0.9) is None


def test_direct_near_term_deadline_remains_eligible():
    event = Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={
            "title": "Project Atlas",
            "text": "The project deadline is tomorrow",
            "content_kind": "system_notice",
            "speaker": "system",
            "evidence_strength": "direct",
            "resolution_state": "unresolved",
        },
    )

    candidate = detect_problem(event, _goal(), 0.9)

    assert candidate is not None
    assert candidate.dedupe_key.startswith("problem:deadline_risk:")


def test_concrete_business_goal_outranks_assistant_behavior_meta_goal():
    meta = _goal(
        title="AI替身要主动发现问题并给建议",
        quote="AI替身应该尽可能了解我并主动介入",
        keywords=["AI", "业务", "问题", "建议"],
        domain="general",
    )
    business = _goal(
        title="AI业务年收入达到100万",
        quote="我要把AI业务做到100万收入",
        keywords=["AI", "客户"],
        domain="business",
    )
    event = Event(
        user_id="u1",
        source="windows.ime",
        type="ime.text_committed",
        facts={
            "text": "AI业务客户转化遇到问题，需要调整报价建议",
            "content_kind": "user_input",
            "speaker": "user",
            "evidence_strength": "direct",
        },
    )

    selected, relevance = choose_goal(event, [meta, business])

    assert selected is not None and selected.id == business.id
    assert relevance > 0


def _template_candidate(goal: Goal, subject: str, topic_key: str) -> AdviceCandidate:
    now = datetime.now(timezone.utc)
    return AdviceCandidate(
        user_id=goal.user_id,
        domain=goal.domain,
        requested_level=AdviceLevel.L2,
        goal_id=goal.id,
        goal_quote=goal.quote,
        evidence=[
            {
                "event_id": uuid4(),
                "source": "windows.ui_automation",
                "fact": subject,
                "observed_at": now,
                "confidence": 0.95,
            }
        ],
        action=subject,
        first_step=subject,
        alternative="稍后从原始记录重新核验",
        prediction={
            "outcome": "下次核验时该事项仍未处理",
            "deadline": now + timedelta(days=1),
            "confidence": 0.85,
        },
        adopted_expected_result="下次核验时形成明确处理结果",
        adopted_confidence=0.82,
        urgency=0.85,
        impact=0.85,
        novelty=0.90,
        relevance=0.95,
        context_fit=1.0,
        interruption_cost=0.1,
        dedupe_key=topic_key,
        topic_key=topic_key,
        issue_subject=subject,
    )


@pytest.mark.parametrize(
    ("first_subject", "paraphrase"),
    [
        ("Email inbox backlog", "Unread mailbox queue"),
        ("保留错误证据", "保存异常日志与错误码"),
        ("确认截止时间", "核实到期时间"),
    ],
)
def test_irrelevant_feedback_suppresses_semantic_template_paraphrases(
    tmp_path,
    first_subject: str,
    paraphrase: str,
):
    repo = Repository(tmp_path / "semantic-suppression.db")
    goal = repo.insert_goal(_goal())
    service = ProactiveService(repo)
    first = service.evaluate(
        _template_candidate(goal, first_subject, f"model-problem:{uuid4().hex}"),
        ContextSnapshot(),
    )
    assert first.advice is not None

    repo.record_feedback(
        goal.user_id,
        first.advice.id,
        FeedbackCreate(kind="irrelevant"),
    )
    suppression = repo.active_topic_suppression(
        goal.user_id,
        repo.topic_key_for(first.advice),
    )
    assert suppression is not None
    assert suppression["reason"] == "USER_IRRELEVANT_TOPIC"

    repeated = service.evaluate(
        _template_candidate(goal, paraphrase, f"model-problem:{uuid4().hex}"),
        ContextSnapshot(),
    )

    assert repeated.decision == "hold"
    assert repeated.advice is None
    assert "TOPIC_SUPPRESSED_BY_USER" in repeated.reason_codes
    repo.close()


def test_evaluation_hold_persists_specific_reason_codes(tmp_path):
    repo = Repository(tmp_path / "evaluation-reasons.db")
    event = Event(
        user_id="u1",
        source="test",
        type="thought.note",
        facts={"text": "A concrete problem needs review"},
    )
    assert repo.insert_event(event)
    assert repo.enqueue_analysis_job(
        event.user_id,
        event.event_id,
        cloud_approved=True,
    )
    claim = repo.claim_analysis_job(event.user_id, event.event_id)
    assert claim is not None
    evaluation = EvaluationResult(
        decision="hold",
        reason_codes=["TOPIC_SUPPRESSED_BY_USER", "PREFERENCE_GOAL_COOLDOWN"],
        score=0.75,
        effective_level=AdviceLevel.L2,
    )
    result = AnalysisDrainResult(user_id=event.user_id, attempted=1)

    AnalysisQueueProcessor(repo, ProactiveService(repo), object())._finish_evaluation(
        claim,
        evaluation,
        result,
    )

    stored = repo.get_analysis_job(event.user_id, event.event_id)
    assert stored is not None
    assert stored.last_reason == (
        "evaluation_hold__topic_suppressed_by_user__preference_goal_cooldown"
    )
    repo.close()
