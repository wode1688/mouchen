import asyncio

import pytest

from app.advice_refiner import ProactiveAdviceRefiner, ProactiveIssueDiscoverer, redact_for_cloud
from app.domain.models import (
    AdviceLevel,
    AdviceStatus,
    Event,
    FeedbackCreate,
    Goal,
    Sensitivity,
)
from app.model_gateway import ModelUnavailable
from app.service import ProactiveService
from app.storage import Repository


class FakeGateway:
    async def generate(self, route, prompt, context, user_id=None):
        assert route.model == "gpt-5.6-sol"
        assert route.mode == "pro"
        assert "adopted_expected_result" in prompt
        assert "[email]" in str(context)
        assert user_id == "u1"
        return """{
          "action": "先回滚最近一次部署，再核对失败阶段",
          "first_step": "打开发布记录并保存错误码",
          "alternative": "切换到上一稳定版本",
          "prediction_outcome": "若不处理，发布会继续阻塞",
          "deadline_hours": 6,
          "confidence": 0.84,
          "adopted_expected_result": "回滚后发布恢复到上一稳定版本",
          "adopted_confidence": 0.81
        }"""

    def second_opinion_route(self):
        return "anthropic_via_claude_code", "claude-fable-5"

    async def second_opinion(self, prompt, context):
        return '{"supported": true, "reason": "证据充分"}'


class DiscoveryGateway:
    async def generate(self, route, prompt, context, user_id=None):
        assert route.model == "gpt-5.6-sol"
        assert route.mode == "pro"
        assert "adopted_expected_result" in prompt
        assert "这些反馈不是事实证据" in prompt
        assert "本次 event_text 独立支持" in prompt
        assert "每个事件最多提出一条建议" in prompt
        assert "避免与 current_active_advice 重复" in prompt
        assert "执行、风险、机会、时间、资源、沟通" in prompt
        assert "owner@example.com" not in str(context)
        assert user_id == "u1"
        return """{
          "intervene": true,
          "issue_subject": "customer complaint response",
          "category": "客户投诉",
          "requested_level": 3,
          "evidence_quote": "发布质量不符合约定，需要今天回复",
          "action": "先确认不符合约定的具体项，再准备可兑现的回复",
          "first_step": "打开投诉原文并列出对方指出的三个事实",
          "alternative": "证据不全时先确认收到并约定核验时间",
          "prediction_outcome": "若今天不处理，客户关系和发布验收会继续恶化",
          "deadline_hours": 12,
          "confidence": 0.88,
          "adopted_expected_result": "今天形成一份基于投诉事实的可兑现回复",
          "adopted_confidence": 0.83,
          "urgency": 0.86,
          "impact": 0.84
        }"""

    def second_opinion_route(self):
        return "anthropic_via_claude_code", "claude-fable-5"

    async def second_opinion(self, prompt, context):
        assert "客户投诉" in str(context)
        return '{"supported": true, "reason": "原文支持"}'


class ActivityDiscoveryGateway:
    async def generate(self, route, prompt, context, user_id=None):
        assert "proactive activity reviewer" in prompt
        assert context["observation_window"]["activity_count"] == 6
        return """{
          "intervene": true,
          "issue_subject": "alpha milestone drift",
          "category": "goal_drift",
          "requested_level": 2,
          "evidence_quote": "APP | Code.exe | mouchen - Visual Studio Code | 3 min",
          "action": "Reserve one focused block for the alpha milestone",
          "first_step": "Open the milestone list and choose the next unfinished item",
          "alternative": "Reduce the milestone scope before adding more tasks",
          "prediction_outcome": "The alpha milestone remains unfinished at the next review",
          "deadline_hours": 24,
          "confidence": 0.87,
          "adopted_expected_result": "A focused block is reserved and one milestone item is completed",
          "adopted_confidence": 0.80,
          "urgency": 0.74,
          "impact": 0.78
        }"""

    def second_opinion_route(self):
        return "anthropic_via_claude_code", "claude-fable-5"

    async def second_opinion(self, prompt, context):
        return '{"supported": true}'


class OwnerContextDiscoveryGateway:
    async def generate(self, route, prompt, context, user_id=None):
        assert "这不是向你提问" in prompt
        assert context["event_text"] == "我发现这周总在绕开产品发布，转去整理不重要的资料"
        return """{
          "intervene": true,
          "issue_subject": "product release avoidance",
          "category": "avoidance_pattern",
          "requested_level": 2,
          "evidence_quote": "这周总在绕开产品发布",
          "action": "先把产品发布拆成一个今天可交付的最小结果",
          "first_step": "列出发布前唯一阻塞项并安排二十五分钟处理",
          "alternative": "若发布目标已经失效，明确修改目标并记录原因",
          "prediction_outcome": "若继续绕开，产品发布本周仍不会形成可验证进展",
          "deadline_hours": 24,
          "confidence": 0.88,
          "adopted_expected_result": "今天形成一个可核验的最小发布交付",
          "adopted_confidence": 0.82,
          "urgency": 0.78,
          "impact": 0.82
        }"""

    def second_opinion_route(self):
        return "anthropic_via_claude_code", "claude-fable-5"

    async def second_opinion(self, prompt, context):
        return '{"supported": true}'


class RejectingReviewGateway(FakeGateway):
    async def generate(self, route, prompt, context, user_id=None):
        return """{
          "action": "立即从官方入口核验登录记录",
          "first_step": "打开账号安全中心",
          "alternative": "若确认本人操作则标记已核验",
          "prediction_outcome": "若并非本人操作，风险会继续扩大",
          "deadline_hours": 2,
          "confidence": 0.90,
          "adopted_expected_result": "两小时内已从官方入口核验登录归属并完成必要处置",
          "adopted_confidence": 0.88
        }"""

    async def second_opinion(self, prompt, context):
        return '{"supported": false, "reason": "证据不足"}'


class RejectingDiscoveryGateway(DiscoveryGateway):
    async def second_opinion(self, prompt, context):
        return '{"supported": false, "reason": "原文不足以支持警告"}'


class MissingAdoptedRefinementGateway(FakeGateway):
    async def generate(self, route, prompt, context, user_id=None):
        return """{
          "action": "先回滚最近一次部署，再核对失败阶段",
          "first_step": "打开发布记录并保存错误码",
          "alternative": "切换到上一稳定版本",
          "prediction_outcome": "若不处理，发布会继续阻塞",
          "deadline_hours": 6,
          "confidence": 0.84
        }"""


class MissingAdoptedDiscoveryGateway(DiscoveryGateway):
    async def generate(self, route, prompt, context, user_id=None):
        return """{
          "intervene": true,
          "issue_subject": "customer complaint response",
          "category": "客户投诉",
          "requested_level": 2,
          "evidence_quote": "发布质量不符合约定，需要今天回复",
          "action": "先确认不符合约定的具体项，再准备可兑现的回复",
          "first_step": "打开投诉原文并列出对方指出的三个事实",
          "alternative": "证据不全时先确认收到并约定核验时间",
          "prediction_outcome": "若今天不处理，客户关系和发布验收会继续恶化",
          "deadline_hours": 12,
          "confidence": 0.88,
          "urgency": 0.86,
          "impact": 0.84
        }"""

    async def second_opinion(self, prompt, context):
        raise AssertionError("incomplete discovery reached second opinion")


class ValidWarningGateway:
    async def generate(self, route, prompt, context, user_id=None):
        return """{
          "action": "立即从官方入口核验登录记录",
          "first_step": "打开账号安全中心并查看最近登录",
          "alternative": "若确认本人操作则标记已核验",
          "prediction_outcome": "若并非本人操作，风险会继续扩大",
          "deadline_hours": 2,
          "confidence": 0.90,
          "adopted_expected_result": "两小时内已核验登录归属并完成必要处置",
          "adopted_confidence": 0.88
        }"""

    def second_opinion_route(self):
        return "anthropic_via_claude_code", "claude-fable-5"

    async def second_opinion(self, prompt, context):
        return '{"supported": true}'


class BlockingWarningGateway(ValidWarningGateway):
    def __init__(self):
        self.review_started = asyncio.Event()
        self.release_review = asyncio.Event()

    async def second_opinion(self, prompt, context):
        self.review_started.set()
        await self.release_review.wait()
        return '{"supported": true}'


class ControlledWarningGateway(ValidWarningGateway):
    def __init__(self, result: str):
        self.result = result
        self.review_started = asyncio.Event()
        self.release_review = asyncio.Event()

    async def second_opinion(self, prompt, context):
        self.review_started.set()
        await self.release_review.wait()
        if self.result == "error":
            raise RuntimeError("review unavailable")
        supported = self.result == "pass"
        return f'{{"supported": {str(supported).lower()}}}'


class ExplodingWarningGateway(ValidWarningGateway):
    async def second_opinion(self, prompt, context):
        raise RuntimeError("review unavailable")


class UnavailableGateway:
    def __init__(self, failure: ModelUnavailable):
        self.failure = failure

    async def generate(self, *_args, **_kwargs):
        raise self.failure


def make_advice(tmp_path):
    repo = Repository(tmp_path / "refiner.db")
    goal = repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="发布产品",
            quote="联系 owner@example.com，本周必须发布产品",
        )
    )
    repo.set_charter("u1", "work", AdviceLevel.L3, False)
    result = ProactiveService(repo).ingest(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={"title": "发布失败", "text": "请联系 owner@example.com，连接超时"},
            confidence=0.98,
        )
    )
    assert result is not None and result.advice is not None
    assert result.advice.goal_id == goal.id
    return repo, result


def make_warning(tmp_path):
    repo = Repository(tmp_path / "warning-refiner.db")
    goal = repo.insert_goal(
        Goal(
            user_id="u1",
            domain="security",
            title="保护账号安全",
            quote="任何可疑登录都必须立即核验",
        )
    )
    repo.set_charter("u1", "security", AdviceLevel.L3, False)
    result = ProactiveService(repo).ingest(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={"title": "安全警告", "text": "检测到可疑登录，若非本人请立即处理"},
            confidence=0.99,
        )
    )
    assert result is not None and result.advice is not None
    assert result.advice.goal_id == goal.id
    assert result.advice.requested_level == AdviceLevel.L3
    return repo, result


def test_proactive_refinement_updates_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol")
    repo, result = make_advice(tmp_path)
    refined = asyncio.run(ProactiveAdviceRefiner(repo, FakeGateway()).refine(result, "u1"))

    assert refined.advice is not None
    assert refined.advice.action == "先回滚最近一次部署，再核对失败阶段"
    assert refined.advice.adopted_expected_result == "回滚后发布恢复到上一稳定版本"
    assert refined.advice.adopted_confidence == 0.81
    assert "AI_REFINED" in refined.reason_codes
    stored = repo.get_advice("u1", refined.advice.id)
    assert stored is not None and stored.action == refined.advice.action
    repo.close()


def test_incomplete_l2_refinement_keeps_valid_original_adopted_prediction(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol")
    repo, result = make_advice(tmp_path)
    original = result.advice

    refined = asyncio.run(
        ProactiveAdviceRefiner(repo, MissingAdoptedRefinementGateway()).refine(
            result,
            "u1",
        )
    )

    assert refined.advice is not None
    assert refined.advice.action == original.action
    assert refined.advice.adopted_expected_result == original.adopted_expected_result
    assert refined.advice.adopted_confidence == original.adopted_confidence
    assert "AI_REFINEMENT_UNAVAILABLE" in refined.reason_codes
    stored = repo.get_advice("u1", original.id)
    assert stored is not None and stored.adopted_expected_result
    assert stored.adopted_confidence is not None
    repo.close()


def test_rejected_l3_review_removes_provisional_advice(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol")
    repo, result = make_warning(tmp_path)

    rejected = asyncio.run(
        ProactiveAdviceRefiner(repo, RejectingReviewGateway()).refine(result, "u1")
    )

    assert rejected.decision == "hold"
    assert rejected.advice is None
    assert "AI_REVIEW_REJECTED" in rejected.reason_codes
    assert repo.get_advice("u1", result.advice.id) is None
    repo.close()


def test_l3_is_invisible_while_review_blocks_then_atomically_promoted(tmp_path):
    repo, result = make_warning(tmp_path)
    gateway = BlockingWarningGateway()
    assert result.advice.status == AdviceStatus.PROVISIONAL

    async def run_review():
        task = asyncio.create_task(
            ProactiveAdviceRefiner(repo, gateway).refine(result, "u1")
        )
        await asyncio.wait_for(gateway.review_started.wait(), timeout=1)
        assert repo.list_advice("u1") == []
        assert repo.get_advice("u1", result.advice.id) is None
        row = repo._connection.execute(
            "SELECT status FROM advice WHERE id=?",
            (str(result.advice.id),),
        ).fetchone()
        assert row["status"] == AdviceStatus.PROVISIONAL.value
        gateway.release_review.set()
        return await task

    reviewed = asyncio.run(run_review())

    assert reviewed.decision == "publish"
    assert reviewed.advice is not None
    assert reviewed.advice.status == AdviceStatus.ACTIVE
    visible = repo.list_advice("u1")
    assert [item.id for item in visible] == [result.advice.id]
    assert repo.get_advice("u1", result.advice.id).status == AdviceStatus.ACTIVE
    repo.close()


@pytest.mark.parametrize("loser_result", ("pass", "reject", "error", "cancel"))
def test_concurrent_l3_review_cleanup_never_deletes_promoted_advice(
    tmp_path,
    loser_result,
):
    repo, result = make_warning(tmp_path)
    loser_gateway = ControlledWarningGateway(loser_result)

    async def run_race():
        loser_task = asyncio.create_task(
            ProactiveAdviceRefiner(repo, loser_gateway).refine(result, "u1")
        )
        await asyncio.wait_for(loser_gateway.review_started.wait(), timeout=1)
        winner = await ProactiveAdviceRefiner(repo, ValidWarningGateway()).refine(
            result,
            "u1",
        )
        assert winner.advice is not None
        assert winner.advice.status == AdviceStatus.ACTIVE
        if loser_result == "cancel":
            loser_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loser_task
            return winner, None
        loser_gateway.release_review.set()
        return winner, await loser_task

    winner, loser = asyncio.run(run_race())

    assert winner.decision == "publish"
    if loser is not None:
        assert loser.decision == "hold"
        assert loser.advice is None
    stored = repo.get_advice("u1", result.advice.id)
    assert stored is not None
    assert stored.status == AdviceStatus.ACTIVE
    assert stored.action == winner.advice.action
    assert repo.delete_advice("u1", stored.id) is False
    assert repo.get_advice("u1", stored.id).status == AdviceStatus.ACTIVE
    repo.close()


def test_l3_review_exception_deletes_provisional_advice(tmp_path):
    repo, result = make_warning(tmp_path)

    failed = asyncio.run(
        ProactiveAdviceRefiner(repo, ExplodingWarningGateway()).refine(result, "u1")
    )

    assert failed.decision == "hold"
    assert failed.advice is None
    assert "AI_REFINEMENT_UNAVAILABLE" in failed.reason_codes
    assert repo.list_advice("u1") == []
    assert repo._connection.execute(
        "SELECT 1 FROM advice WHERE id=?",
        (str(result.advice.id),),
    ).fetchone() is None
    repo.close()


def test_l3_refinement_can_propagate_model_failure_after_provisional_cleanup(tmp_path):
    repo, result = make_warning(tmp_path)
    failure = ModelUnavailable(
        "OpenAI authentication failed",
        category="auth",
        reason_code="openai_auth_failed",
        retryable=False,
        provider="openai",
        status_code=401,
    )

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(
            ProactiveAdviceRefiner(repo, UnavailableGateway(failure)).refine(
                result,
                "u1",
                raise_model_unavailable=True,
            )
        )

    assert captured.value is failure
    assert captured.value.reason_code == "openai_auth_failed"
    assert captured.value.retryable is False
    assert repo.list_advice("u1") == []
    assert repo._connection.execute(
        "SELECT 1 FROM advice WHERE id=?",
        (str(result.advice.id),),
    ).fetchone() is None
    repo.close()


def test_l3_review_cancellation_deletes_provisional_advice(tmp_path):
    repo, result = make_warning(tmp_path)
    gateway = BlockingWarningGateway()

    async def cancel_review():
        task = asyncio.create_task(
            ProactiveAdviceRefiner(repo, gateway).refine(result, "u1")
        )
        await asyncio.wait_for(gateway.review_started.wait(), timeout=1)
        assert repo.list_advice("u1") == []
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_review())

    assert repo.list_advice("u1") == []
    assert repo._connection.execute(
        "SELECT 1 FROM advice WHERE id=?",
        (str(result.advice.id),),
    ).fetchone() is None
    repo.close()


def test_repository_startup_keeps_recent_inflight_provisional_advice_invisible(tmp_path):
    repo, result = make_warning(tmp_path)
    path = repo.path
    row = repo._connection.execute(
        "SELECT status FROM advice WHERE id=?",
        (str(result.advice.id),),
    ).fetchone()
    assert row["status"] == AdviceStatus.PROVISIONAL.value
    repo.close()

    restarted = Repository(path)

    assert restarted.list_advice("u1") == []
    assert restarted._connection.execute(
        "SELECT 1 FROM advice WHERE id=?",
        (str(result.advice.id),),
    ).fetchone() is not None
    restarted.close()


def test_refinement_context_excludes_unrelated_same_source_content(tmp_path):
    repo, result = make_advice(tmp_path)
    repo.insert_event(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={"title": "私人周报", "text": "与发布故障无关的日常内容"},
        )
    )

    context = ProactiveAdviceRefiner(repo, FakeGateway())._minimal_context(result.advice, "u1")

    assert "私人周报" not in str(context)
    assert "发布失败" in str(context)
    repo.close()


def test_refinement_context_includes_redacted_feedback_for_the_same_goal(tmp_path):
    repo, result = make_advice(tmp_path)
    repo.record_feedback(
        "u1",
        result.advice.id,
        FeedbackCreate(
            kind="guidance",
            note="优先给 owner@example.com 可执行选项，少讲背景",
        ),
    )
    repo.record_feedback("u1", result.advice.id, FeedbackCreate(kind="useful"))

    context = ProactiveAdviceRefiner(repo, FakeGateway())._minimal_context(
        result.advice,
        "u1",
    )

    by_kind = {item["kind"]: item for item in context["recent_user_feedback"]}
    assert by_kind["guidance"]["note"] == "优先给 [email] 可执行选项，少讲背景"
    assert by_kind["useful"]["note"] is None
    assert "owner@example.com" not in str(context)
    repo.close()


def test_refinement_prompt_defines_feedback_semantics_and_cold_start_exploration():
    from app.advice_refiner import REFINEMENT_PROMPT

    assert "adopted/useful 是正向信号" in REFINEMENT_PROMPT
    assert "later 表示时机不合适" in REFINEMENT_PROMPT
    assert "guidance 是用户明确给出的方向" in REFINEMENT_PROMPT
    assert "acknowledged 仅表示已经知悉、不是偏好或效果信号" in REFINEMENT_PROMPT
    assert "这些反馈不是事实证据" in REFINEMENT_PROMPT
    assert "执行、风险、机会、时间、资源、沟通" in REFINEMENT_PROMPT


def test_cloud_redaction_removes_common_identifiers():
    value = redact_for_cloud(
        "联系 owner@example.com 或 +86 138 0013 8000，验证码 123456，详情 https://example.com/a?token=x"
    )

    assert "owner@example.com" not in value
    assert "138 0013 8000" not in value
    assert "123456" not in value
    assert "https://" not in value
    assert "[email]" in value and "[phone]" in value and "[number]" in value and "[url]" in value


def test_cloud_discovery_finds_unmodeled_goal_related_problem(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol")
    repo = Repository(tmp_path / "discoverer.db")
    goal = repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="发布产品",
            quote="联系 owner@example.com，本周必须发布产品",
            target={"keywords": ["发布"]},
        )
    )
    event = Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={
            "package": "com.example.crm",
            "title": "客户投诉",
            "text": "发布质量不符合约定，需要今天回复",
        },
        confidence=0.96,
    )
    repo.insert_event(event)

    candidate = asyncio.run(ProactiveIssueDiscoverer(repo, DiscoveryGateway()).discover(event, "u1"))

    assert candidate is not None
    assert candidate.goal_id == goal.id
    assert candidate.requested_level == AdviceLevel.L3
    assert candidate.first_step == "打开投诉原文并列出对方指出的三个事实"
    assert candidate.adopted_expected_result == "今天形成一份基于投诉事实的可兑现回复"
    assert candidate.adopted_confidence == 0.83
    assert candidate.alternative
    repo.close()


def test_discovery_without_adopted_result_prediction_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol")
    repo = Repository(tmp_path / "incomplete-discovery.db")
    repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="发布产品",
            quote="本周必须发布产品",
            target={"keywords": ["发布"]},
        )
    )
    event = Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={"title": "客户投诉", "text": "发布质量不符合约定，需要今天回复"},
        confidence=0.96,
    )

    candidate = asyncio.run(
        ProactiveIssueDiscoverer(repo, MissingAdoptedDiscoveryGateway()).discover(
            event,
            "u1",
        )
    )

    assert candidate is None
    assert repo.list_advice("u1") == []
    repo.close()


def test_rejected_discovered_warning_is_not_downgraded_into_a_notification(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol")
    repo = Repository(tmp_path / "rejected-discovery.db")
    repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="发布产品",
            quote="本周必须发布产品",
            target={"keywords": ["发布"]},
        )
    )
    event = Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={"title": "客户投诉", "text": "发布质量不符合约定，需要今天回复"},
        confidence=0.96,
    )

    candidate = asyncio.run(
        ProactiveIssueDiscoverer(repo, RejectingDiscoveryGateway()).discover(event, "u1")
    )

    assert candidate is None
    assert repo.list_advice("u1") == []
    repo.close()


def test_activity_review_reaches_model_without_error_keyword(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol")
    repo = Repository(tmp_path / "activity-discoverer.db")
    goal = repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="Ship My AI Twin alpha",
            quote="This week I will finish the My AI Twin alpha milestone",
            target={"keywords": ["mouchen"]},
        )
    )
    event = Event(
        user_id="u1",
        source="windows.proactive",
        type="ui.visible_text",
        facts={
            "context": "proactive_activity_review",
            "visible_text": (
                "APP | Code.exe | mouchen - Visual Studio Code | 3 min\n"
                "BROWSER | GitHub Actions | github.com"
            ),
            "activity_count": 6,
            "source_counts": {"windows.foreground": 5, "windows.browser_history": 1},
            "window_started_at": "2026-08-03T01:00:00Z",
            "window_ended_at": "2026-08-03T01:10:00Z",
        },
        confidence=0.92,
    )

    candidate = asyncio.run(
        ProactiveIssueDiscoverer(repo, ActivityDiscoveryGateway()).discover(event, "u1")
    )

    assert candidate is not None
    assert candidate.goal_id == goal.id
    assert candidate.requested_level == AdviceLevel.L2
    assert candidate.evidence[0].fact.endswith(
        "APP | Code.exe | mouchen - Visual Studio Code | 3 min"
    )
    repo.close()


@pytest.mark.parametrize(
    "context_kind",
    ["owner_self_report", "shared_image", "conversation_import"],
)
def test_owner_context_is_reviewed_without_problem_keyword(
    tmp_path,
    monkeypatch,
    context_kind,
):
    monkeypatch.setenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol")
    repo = Repository(tmp_path / "owner-context-discoverer.db")
    goal = repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="发布产品",
            quote="本周完成产品发布",
            target={"keywords": ["产品", "发布"]},
        )
    )
    event = Event(
        user_id="u1",
        source="android.self_report",
        type="ui.visible_text",
        facts={
            "context": context_kind,
            "visible_text": ["我发现这周总在绕开产品发布，转去整理不重要的资料"],
            "analysis_requested": True,
        },
        sensitivity=Sensitivity.SENSITIVE,
        confidence=1.0,
    )

    candidate = asyncio.run(
        ProactiveIssueDiscoverer(repo, OwnerContextDiscoveryGateway()).discover(event, "u1")
    )

    assert candidate is not None
    assert candidate.goal_id == goal.id
    assert candidate.action.startswith("先把产品发布拆成")
    repo.close()


def test_restricted_event_never_enters_cloud_discovery(tmp_path):
    class NeverGateway:
        async def generate(self, *_args, **_kwargs):
            raise AssertionError("restricted event reached a model")

    repo = Repository(tmp_path / "restricted.db")
    repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="发布产品",
            quote="本周必须发布产品",
            target={"keywords": ["发布"]},
        )
    )
    event = Event(
        user_id="u1",
        source="android.accessibility",
        type="ui.visible_text",
        facts={"text": "发布失败，需要立即处理"},
        sensitivity=Sensitivity.RESTRICTED,
    )

    candidate = asyncio.run(ProactiveIssueDiscoverer(repo, NeverGateway()).discover(event, "u1"))

    assert candidate is None
    repo.close()


def test_discovery_can_propagate_model_failure_with_retry_metadata(tmp_path):
    repo = Repository(tmp_path / "unavailable-discovery.db")
    repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="Ship release",
            quote="Ship the release this week",
            target={"keywords": ["release"]},
        )
    )
    event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "The release is blocked and needs action"},
    )
    failure = ModelUnavailable(
        "OpenAI is temporarily unavailable",
        category="transient",
        reason_code="openai_timeout",
        retryable=True,
        provider="openai",
    )

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(
            ProactiveIssueDiscoverer(repo, UnavailableGateway(failure)).discover_attempt(
                event,
                "u1",
                raise_model_unavailable=True,
            )
        )

    assert captured.value is failure
    assert captured.value.reason_code == "openai_timeout"
    assert captured.value.retryable is True
    repo.close()


def test_explicit_minimized_consent_allows_restricted_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol")
    repo = Repository(tmp_path / "restricted-minimized.db")
    goal = repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="发布产品",
            quote="本周必须发布产品",
            target={"keywords": ["发布"]},
        )
    )
    event = Event(
        user_id="u1",
        source="android.accessibility",
        type="ui.visible_text",
        facts={
            "package": "com.example.crm",
            "visible_text": ["客户投诉：发布质量不符合约定，需要今天回复 owner@example.com"],
        },
        sensitivity=Sensitivity.RESTRICTED,
        consent_scope="alpha.minimized_context",
        confidence=0.96,
    )

    candidate = asyncio.run(
        ProactiveIssueDiscoverer(repo, DiscoveryGateway()).discover(
            event,
            "u1",
            allow_restricted_minimized=True,
        )
    )

    assert candidate is not None
    assert candidate.goal_id == goal.id
    assert candidate.first_step == "打开投诉原文并列出对方指出的三个事实"
    repo.close()
