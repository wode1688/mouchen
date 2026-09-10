import json

from app.context_retrieval import build_user_question_context
from app.domain.models import Event, Goal, Sensitivity
from app.storage import Repository


def test_user_question_retrieval_is_local_relevant_and_bounded(tmp_path):
    repo = Repository(tmp_path / "question-context.db")
    repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="发布AI替身",
            quote="本周发布AI替身，联系 owner@example.com 核验",
            target={"keywords": ["发布", "AI替身"]},
        )
    )
    repo.insert_goal(
        Goal(
            user_id="u1",
            domain="health",
            title="稳定睡眠",
            quote="本月稳定睡眠",
        )
    )
    private_prefix = "LOCAL-ONLY-SENTINEL " * 40
    relevant = Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={
            "title": "状态更新",
            "text": (
                f"{private_prefix}发布失败，联系 owner@example.com 查看错误，但不要重复提交"
            ),
        },
    )
    repo.insert_event(relevant)
    repo.insert_event(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={"title": "健康提醒", "text": "今天完成了健身目标"},
        )
    )
    repo.insert_event(
        Event(
            user_id="u1",
            source="android.accessibility",
            type="ui.visible_text",
            facts={"visible_text": "发布失败，受限页面私密内容"},
            sensitivity=Sensitivity.RESTRICTED,
        )
    )

    context = build_user_question_context(
        repo,
        "u1",
        "AI替身发布为什么失败？",
        {
            "active_advice_count": 2,
            "mail_body": "客户端不应决定云上下文",
        },
    )
    serialized = json.dumps(context, ensure_ascii=False)

    assert len(context["goals"]) == 1
    assert context["goals"][0]["domain"] == "work"
    assert "owner@example.com" not in serialized
    assert "[email]" in serialized
    assert len(context["recent_relevant_events"]) == 1
    assert "发布失败" in context["recent_relevant_events"][0]["excerpt"]
    assert "LOCAL-ONLY-SENTINEL" not in serialized
    assert "受限页面私密内容" not in serialized
    assert "健身目标" not in serialized
    assert context["client_state"] == {"active_advice_count": 2}
    stored = next(
        event for event in repo.recent_events("u1", limit=10) if event.event_id == relevant.event_id
    )
    assert "LOCAL-ONLY-SENTINEL" in stored.facts["text"]
    repo.close()


def test_greeting_does_not_export_unrelated_recent_history(tmp_path):
    repo = Repository(tmp_path / "greeting-context.db")
    repo.insert_goal(
        Goal(user_id="u1", domain="work", title="发布AI替身", quote="本周发布AI替身")
    )
    repo.insert_event(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={"title": "私人消息", "text": "与问候无关的敏感日常"},
        )
    )

    context = build_user_question_context(repo, "u1", "你好", {})

    assert context["recent_relevant_events"] == []
    assert len(context["goals"]) == 1
    repo.close()
