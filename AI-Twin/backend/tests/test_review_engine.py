import json
from datetime import datetime, timedelta, timezone

from app.domain.models import Event, Goal, Sensitivity
from app.review_engine import GoalReviewEngine, _build_review_event
from app.service import ProactiveService
from app.storage import Repository


class ReviewGateway:
    def __init__(self) -> None:
        self.calls = []

    async def generate(self, route, prompt, context, user_id=None):
        self.calls.append((route, prompt, context, user_id))
        assert "proactive activity reviewer" in prompt
        assert "发布构建仍在排队" in context["event_text"]
        return json.dumps(
            {
                "intervene": True,
                "category": "milestone_drift",
                "issue_subject": "release build queue delay",
                "requested_level": 2,
                "evidence_quote": "发布构建仍在排队",
                "action": "核对构建队列并明确今天的发布路径",
                "first_step": "打开构建详情并确认当前队列位置",
                "alternative": "切换到已验证的备用构建通道",
                "prediction_outcome": "若今天不处理，发布里程碑将继续停滞",
                "deadline_hours": 12,
                "confidence": 0.88,
                "adopted_expected_result": "今天明确发布路径并解除构建队列阻塞",
                "adopted_confidence": 0.81,
                "urgency": 0.82,
                "impact": 0.84,
            },
            ensure_ascii=False,
        )

    def second_opinion_route(self):
        return "anthropic_via_claude_code", "claude-fable-5"

    async def second_opinion(self, prompt, context):
        return '{"supported": true, "reason": "evidence supported"}'


class UnexpectedGateway:
    async def generate(self, *_args, **_kwargs):
        raise AssertionError("deterministic review must not call a cloud model")


class ChineseRefinementGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, route, prompt, context, user_id=None):
        self.calls += 1
        assert context["response_locale"] == "en-US"
        assert "You are My AI Twin's solution writer" in prompt
        return json.dumps(
            {
                "action": "立即重新安排本周的发布时间",
                "first_step": "现在打开日历并选择时间段",
                "alternative": "如果本周不可行，就明确调整目标",
                "prediction_outcome": "如果不处理，本周目标可能无法完成",
                "deadline_hours": 24,
                "confidence": 0.86,
                "adopted_expected_result": "本周发布时间已经重新安排",
                "adopted_confidence": 0.82,
            },
            ensure_ascii=False,
        )

    def second_opinion_route(self):
        return "disabled", "none"


def test_periodic_time_review_publishes_without_problem_keyword_and_is_idempotent(tmp_path):
    repository = Repository(tmp_path / "time-review.db")
    service = ProactiveService(repository)
    now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
    goal = repository.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="发布AI替身",
            quote="本周优先完成AI替身发布",
            target={"weekly_hours": 14, "packages": ["com.example.builder"]},
        )
    )
    repository.insert_event(
        Event(
            user_id="u1",
            source="android.usage",
            type="app.foreground_session",
            occurred_at=now - timedelta(hours=1),
            facts={
                "package": "com.example.chat",
                "app_label": "聊天",
                "duration_ms": 2 * 3_600_000,
                "timezone_offset_minutes": 0,
            },
            evidence_ref="usage:unrelated:1",
        )
    )
    repository.insert_event(
        Event(
            user_id="u1",
            source="android.usage",
            type="app.foreground_session",
            occurred_at=now - timedelta(hours=2),
            facts={
                "package": "com.example.builder",
                "app_label": "构建器",
                "duration_ms": 2 * 3_600_000,
                "timezone_offset_minutes": 0,
            },
            evidence_ref="usage:matched:1",
        )
    )
    engine = GoalReviewEngine(repository, service, UnexpectedGateway())

    import asyncio

    first = asyncio.run(engine.tick("u1", now=now))
    second = asyncio.run(engine.tick("u1", now=now))

    assert first.reviewed_goals == 1
    assert len(first.evaluations) == 1
    assert first.evaluations[0].decision == "publish"
    assert "PERIODIC_GOAL_REVIEW" in first.evaluations[0].reason_codes
    assert first.evaluations[0].advice.goal_id == goal.id
    assert "本周截至目前投入 2.0 小时" in first.evaluations[0].advice.evidence[0].fact
    assert second.evaluations == []
    assert second.duplicate_windows == 1
    assert len(repository.list_advice("u1")) == 1
    repository.close()


def test_periodic_english_l3_wrong_language_is_queued_for_retry(tmp_path):
    repository = Repository(tmp_path / "periodic-english-locale-retry.db")
    repository.ensure_user("u1")
    repository.set_account_locale("u1", "en-US")
    service = ProactiveService(repository)
    gateway = ChineseRefinementGateway()
    now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
    repository.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="Release My AI Twin",
            quote="Release My AI Twin this week",
            target={"weekly_hours": 14, "packages": ["com.example.builder"]},
        )
    )
    repository.insert_event(
        Event(
            user_id="u1",
            source="android.usage",
            type="app.foreground_session",
            occurred_at=now - timedelta(hours=1),
            facts={
                "package": "com.example.builder",
                "app_label": "Builder",
                "duration_ms": 15 * 60_000,
                "timezone_offset_minutes": 0,
            },
            evidence_ref="usage:english-l3:1",
        )
    )
    engine = GoalReviewEngine(repository, service, gateway)

    import asyncio

    result = asyncio.run(engine.tick("u1", cloud_approved=True, now=now))

    assert len(result.evaluations) == 1
    assert result.evaluations[0].decision == "hold"
    assert result.evaluations[0].advice is None
    assert "MODEL_OUTPUT_LOCALE_MISMATCH" in result.evaluations[0].reason_codes
    assert "PERIODIC_GOAL_REVIEW" in result.evaluations[0].reason_codes
    review_event = next(
        event
        for event in repository.recent_events("u1")
        if event.source == "backend.goal_review"
    )
    job = repository.get_analysis_job("u1", review_event.event_id)
    assert job is not None
    assert job.status == "retry"
    assert job.attempts == 1
    assert job.last_reason == "model_output_locale_mismatch"
    assert gateway.calls == 2
    assert repository.list_advice("u1") == []
    repository.close()


def test_periodic_ai_review_uses_neutral_recent_evidence_and_avoids_duplicate_calls(tmp_path):
    repository = Repository(tmp_path / "ai-review.db")
    service = ProactiveService(repository)
    gateway = ReviewGateway()
    now = datetime.now(timezone.utc)
    goal = repository.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="发布AI替身",
            quote="本周完成AI替身发布",
            target={"keywords": ["发布", "构建"]},
        )
    )
    for index, text in enumerate(("发布构建仍在排队", "发布评审尚待确认"), start=1):
        repository.insert_event(
            Event(
                user_id="u1",
                source="android.notification",
                type="notification.posted",
                occurred_at=now - timedelta(minutes=index),
                facts={"package": "com.example.builder", "title": "发布动态", "text": text},
                confidence=0.95,
                evidence_ref=f"notification:neutral:{index}",
            )
        )
    engine = GoalReviewEngine(repository, service, gateway)
    source_event_ids = {
        str(event.event_id)
        for event in repository.recent_events("u1")
        if event.source == "android.notification"
    }

    import asyncio

    first = asyncio.run(engine.tick("u1", cloud_approved=True, now=now))
    second = asyncio.run(engine.tick("u1", cloud_approved=True, now=now))

    assert first.reviewed_goals == 1
    assert first.evidence_events == 2
    assert len(first.evaluations) == 1
    assert first.evaluations[0].decision == "publish"
    assert first.evaluations[0].advice.goal_id == goal.id
    assert "AI_DISCOVERED" in first.evaluations[0].reason_codes
    assert "PERIODIC_GOAL_REVIEW" in first.evaluations[0].reason_codes
    assert second.evaluations == []
    assert second.duplicate_windows == 1
    assert len(gateway.calls) == 1
    assert len(repository.list_advice("u1")) == 1
    review_event = next(
        event
        for event in repository.recent_events("u1")
        if event.source == "backend.goal_review"
    )
    assert set(review_event.facts["included_event_ids"]) == source_event_ids
    repository.close()


def test_aggregate_review_preserves_owner_full_context_scope_and_evidence_trace():
    now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
    goal = Goal(
        user_id="u1",
        domain="work",
        title="Release",
        quote="I will finish the release",
    )
    events = [
        Event(
            user_id="u1",
            source="android.accessibility",
            type="ui.visible_text",
            occurred_at=now - timedelta(minutes=index),
            facts={"visible_text": [f"release evidence {index}"]},
            sensitivity=Sensitivity.RESTRICTED,
            consent_scope="owner_full_context",
        )
        for index in (1, 2)
    ]

    review = _build_review_event("u1", goal, events, now, timedelta(hours=24))

    assert review is not None
    assert review.sensitivity == Sensitivity.RESTRICTED
    assert review.consent_scope == "owner_full_context"
    assert review.facts["included_event_ids"] == [
        str(event.event_id) for event in events
    ]
