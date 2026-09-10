from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.advice_refiner import ProactiveAdviceRefiner, ProactiveIssueDiscoverer
from app.analysis_queue import AnalysisQueueProcessor
from app.domain.models import Event, Goal
from app.domain.problem_signals import detect_problem
from app.localization import (
    DEFAULT_LOCALE,
    generated_values_match_locale,
    model_output_language_instruction,
    normalize_locale,
)
from app.model_gateway import ModelRoute
from app.service import ProactiveService
from app.storage import Repository


def _reset_main(main, path):
    try:
        main.repo.close()
    except Exception:
        pass
    main.repo = main.Repository(path)
    main.service = main.ProactiveService(main.repo)


def _register(client, username: str, device_id: str, *, locale: str | None = None):
    payload = {
        "username": username,
        "password": "correct horse battery staple",
        "device_id": device_id,
    }
    if locale is not None:
        payload["locale"] = locale
    return client.post("/v1/auth/register", json=payload)


def test_locale_normalization_is_strict_at_the_account_boundary():
    assert normalize_locale("zh") == "zh-CN"
    assert normalize_locale("zh-Hans") == "zh-CN"
    assert normalize_locale("EN") == "en-US"
    assert normalize_locale("en-US") == "en-US"
    assert normalize_locale(None, strict=False) == DEFAULT_LOCALE
    with pytest.raises(ValueError, match="unsupported locale"):
        normalize_locale("fr-FR")


def test_english_generated_output_rejects_every_han_character():
    assert generated_values_match_locale("en-US", "Review the release plan")
    assert not generated_values_match_locale("en-US", "Review AI替身 release plan")
    assert not generated_values_match_locale("en-US", "这是中文")


def test_account_locale_is_server_authoritative_and_tenant_isolated(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    monkeypatch.setenv("MOUCHEN_ALLOW_OPEN_REGISTRATION", "true")
    import app.main as main

    _reset_main(main, tmp_path / "locale-accounts.db")
    with TestClient(main.app) as client:
        alice = _register(client, "Alice", "windows-1", locale="en")
        bob = _register(client, "Bob", "android-1")
        assert alice.status_code == 200, alice.text
        assert bob.status_code == 200, bob.text
        assert alice.json()["session"]["locale"] == "en-US"
        assert bob.json()["session"]["locale"] == "zh-CN"

        alice_headers = {
            "Authorization": f"Bearer {alice.json()['access_token']}"
        }
        bob_headers = {"Authorization": f"Bearer {bob.json()['access_token']}"}
        assert client.get("/v1/session", headers=alice_headers).json()["locale"] == "en-US"
        assert client.get(
            "/v1/account/preferences", headers=alice_headers
        ).json()["locale"] == "en-US"

        catalog = client.get("/v1/advice-preferences", headers=alice_headers)
        assert catalog.status_code == 200, catalog.text
        labels = {
            item["id"]: item["label"] for item in catalog.json()["event_type_catalog"]
        }
        assert labels["ui.visible_text"] == "Visible screen text"
        assert catalog.json()["frequency_policies"]["balanced"]["label"] == "Balanced"

        changed = client.put(
            "/v1/account/preferences",
            headers=alice_headers,
            json={"locale": "zh-Hans"},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["locale"] == "zh-CN"
        assert client.get("/v1/session", headers=alice_headers).json()["locale"] == "zh-CN"
        assert client.get(
            "/v1/account/preferences", headers=bob_headers
        ).json()["locale"] == "zh-CN"

        invalid = client.put(
            "/v1/account/preferences",
            headers=alice_headers,
            json={"locale": "fr-FR"},
        )
        assert invalid.status_code == 422


def test_english_deterministic_counsel_preserves_original_evidence_quote():
    goal = Goal(
        user_id="u1",
        domain="finance",
        title="Keep payments operational",
        quote="Resolve payment failures promptly",
        target={"keywords": ["payment"]},
    )
    original = "Payment failed today for Merchant Alpha, amount $100"
    event = Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={
            "package": "com.example.payments",
            "title": "Merchant Alpha",
            "text": original,
        },
    )

    candidate = detect_problem(event, goal, 0.95, locale="en-US")

    assert candidate is not None
    assert candidate.action.startswith("Confirm the failed amount")
    assert candidate.first_step.startswith("Open the ")
    assert not any("\u4e00" <= character <= "\u9fff" for character in candidate.action)
    assert candidate.evidence[0].fact.startswith("Observed from android.notification:")
    assert original in candidate.evidence[0].fact
    assert "从 " not in candidate.evidence[0].fact


def test_model_language_rule_localizes_values_but_never_translates_evidence():
    english = model_output_language_instruction("en-US")
    chinese = model_output_language_instruction("zh-CN")
    assert "natural, concise English" in english
    assert "issue_subject is a non-user-facing canonical identity" in english
    assert "lower-case ASCII English" in english
    assert "exact verbatim substring" in english
    assert "所有面向用户的新生成内容均使用自然、简洁的中文" in chinese
    assert "issue_subject 不是面向用户的内容" in chinese
    assert "不得随 zh-CN/en-US 切换而变化" in chinese
    assert "evidence_quote 必须逐字保留" in chinese


def test_cloud_discovery_uses_account_locale_and_keeps_source_quote(tmp_path):
    class EnglishGateway:
        def __init__(self):
            self.prompt = ""
            self.context = {}

        async def generate(self, route, prompt, context, user_id=None):
            self.prompt = prompt
            self.context = context
            assert user_id == "u1"
            return """{
              "intervene": true,
              "category": "delivery risk",
              "issue_subject": "release blocking condition",
              "requested_level": 2,
              "evidence_quote": "The client says today's release is blocked",
              "action": "Confirm the blocking condition before changing the plan",
              "first_step": "Open the release log and record the first failed check",
              "alternative": "If the evidence is incomplete, ask for the exact failed check",
              "prediction_outcome": "The release remains blocked tomorrow without a verified cause",
              "deadline_hours": 24,
              "confidence": 0.88,
              "adopted_expected_result": "The blocking condition is recorded and one recovery path is chosen",
              "adopted_confidence": 0.82,
              "urgency": 0.75,
              "impact": 0.80
            }"""

        def second_opinion_route(self):
            return "template", "deterministic-v1"

    repo = Repository(tmp_path / "english-discovery.db")
    repo.ensure_user("u1")
    repo.set_account_locale("u1", "en-US")
    repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="Release My AI Twin",
            quote="Release My AI Twin this week",
            target={"keywords": ["release"]},
        )
    )
    event = Event(
        user_id="u1",
        source="shared.text",
        type="shared.text",
        facts={"text": "The client says today's release is blocked"},
        confidence=0.95,
    )
    repo.insert_event(event)
    gateway = EnglishGateway()

    candidate = asyncio.run(
        ProactiveIssueDiscoverer(repo, gateway).discover(event, "u1")
    )

    assert candidate is not None
    assert candidate.action.startswith("Confirm the blocking condition")
    assert candidate.evidence[0].fact.endswith(
        "The client says today's release is blocked"
    )
    assert "natural, concise English" in gateway.prompt
    assert "evidence_quote must remain an exact verbatim substring" in gateway.prompt
    assert gateway.context["response_locale"] == "en-US"
    repo.close()


def test_english_account_rejects_chinese_refinement_and_keeps_english_fallback(
    tmp_path,
):
    class ChineseRefinementGateway:
        async def generate(self, route, prompt, context, user_id=None):
            assert "You are My AI Twin's solution writer" in prompt
            assert "你是AI替身的方案生成器" not in prompt
            assert context["response_locale"] == "en-US"
            return """{
              "action": "先核对付款失败原因，再选择补款或更换通道",
              "first_step": "打开官方应用并查看失败详情",
              "alternative": "联系官方客服核对款项状态",
              "prediction_outcome": "如果不处理，服务可能继续中断",
              "deadline_hours": 12,
              "confidence": 0.84,
              "adopted_expected_result": "十二小时内确认付款状态并选定处理路径",
              "adopted_confidence": 0.82
            }"""

        def second_opinion_route(self):
            return "disabled", "none"

    repo = Repository(tmp_path / "english-refinement-guard.db")
    repo.ensure_user("u1")
    repo.set_account_locale("u1", "en-US")
    repo.insert_goal(
        Goal(
            user_id="u1",
            domain="finance",
            title="Keep payments operational",
            quote="Resolve payment failures promptly",
            target={"keywords": ["payment"]},
        )
    )
    result = ProactiveService(repo).ingest(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={
                "package": "com.example.payments",
                "title": "Payment failed",
                "text": "Payment failed today for Merchant Alpha",
                "content_kind": "system_notice",
                "speaker": "system",
                "evidence_strength": "direct",
                "resolution_state": "unresolved",
            },
            confidence=0.96,
        )
    )
    assert result is not None and result.advice is not None
    original = result.advice
    assert generated_values_match_locale("en-US", original.action)

    refined = asyncio.run(
        ProactiveAdviceRefiner(repo, ChineseRefinementGateway()).refine(
            result,
            "u1",
        )
    )

    assert refined.advice is not None
    assert refined.advice.action == original.action
    assert "MODEL_OUTPUT_LOCALE_MISMATCH" in refined.reason_codes
    assert repo.get_advice("u1", original.id).action == original.action
    repo.close()


def test_queued_discovery_reloads_latest_locale_without_rewriting_history(tmp_path):
    class EnglishDiscoveryGateway:
        def __init__(self):
            self.prompt = ""
            self.context = {}

        async def generate(self, route, prompt, context, user_id=None):
            self.prompt = prompt
            self.context = context
            return """{
              "intervene": true,
              "category": "release risk",
              "issue_subject": "release blocking condition",
              "requested_level": 2,
              "evidence_quote": "The release is blocked and needs action",
              "action": "Confirm the blocking condition before changing the release plan",
              "first_step": "Open the release log and record the first failed check",
              "alternative": "Ask for the exact failed check if the evidence is incomplete",
              "prediction_outcome": "The release remains blocked at the next review",
              "deadline_hours": 24,
              "confidence": 0.88,
              "adopted_expected_result": "The blocking condition is recorded and one recovery path is chosen",
              "adopted_confidence": 0.82,
              "urgency": 0.75,
              "impact": 0.80
            }"""

        def second_opinion_route(self):
            return "disabled", "none"

    repo = Repository(tmp_path / "queued-locale-boundary.db")
    repo.ensure_user("u1")
    repo.insert_goal(
        Goal(
            user_id="u1",
            domain="operations",
            title="保持部署稳定",
            quote="发现部署失败时及时处理",
            target={"keywords": ["部署"]},
        )
    )
    goal = repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="Release My AI Twin",
            quote="Release My AI Twin this week",
            target={"keywords": ["release", "发布"]},
        )
    )
    old_result = ProactiveService(repo).ingest(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={
                "domain": "operations",
                "title": "部署失败",
                "text": "部署失败，需要立即处理",
                "content_kind": "system_notice",
                "speaker": "system",
                "evidence_strength": "direct",
                "resolution_state": "unresolved",
            },
            confidence=0.96,
        )
    )
    assert old_result is not None and old_result.advice is not None
    old_id = old_result.advice.id
    old_payload = old_result.advice.model_dump_json()
    assert any("\u4e00" <= char <= "\u9fff" for char in old_result.advice.action)

    queued_event = Event(
        user_id="u1",
        source="windows.thought",
        type="thought.note",
        facts={
            "domain": "work",
            "text": "The release is blocked and needs action",
        },
        confidence=0.96,
    )
    assert repo.insert_event(queued_event)
    assert repo.enqueue_analysis_job(
        "u1",
        queued_event.event_id,
        job_kind="discover",
        cloud_approved=True,
    )
    repo.set_account_locale("u1", "en-US")

    gateway = EnglishDiscoveryGateway()
    processor = AnalysisQueueProcessor(
        repo,
        ProactiveService(repo),
        gateway,
        retry_base_seconds=0,
    )
    drained = asyncio.run(processor.process_event("u1", queued_event.event_id))

    assert drained.published == 1
    assert gateway.context["response_locale"] == "en-US"
    assert "You are My AI Twin's proactive reasoning" in gateway.prompt
    assert "reviewer." in gateway.prompt
    assert "你是AI替身" not in gateway.prompt
    assert repo.get_advice("u1", old_id).model_dump_json() == old_payload
    new_items = [item for item in repo.list_advice("u1") if item.id != old_id]
    assert len(new_items) == 1
    assert new_items[0].goal_id == goal.id
    assert new_items[0].action.startswith("Confirm the blocking condition")
    repo.close()


def test_english_discovery_wrong_language_is_retryable_and_never_published(tmp_path):
    class ChineseDiscoveryGateway:
        async def generate(self, route, prompt, context, user_id=None):
            return """{
              "intervene": true,
              "category": "发布风险",
              "issue_subject": "release blocking condition",
              "requested_level": 2,
              "evidence_quote": "The release is blocked and needs action",
              "action": "先确认阻塞条件，再调整发布计划",
              "first_step": "打开发布日志并记录第一个失败检查",
              "alternative": "证据不足时询问具体失败检查",
              "prediction_outcome": "下次复核时发布仍然受阻",
              "deadline_hours": 24,
              "confidence": 0.88,
              "adopted_expected_result": "已记录阻塞条件并选定恢复路径",
              "adopted_confidence": 0.82,
              "urgency": 0.75,
              "impact": 0.80
            }"""

        def second_opinion_route(self):
            return "disabled", "none"

    repo = Repository(tmp_path / "english-discovery-retry.db")
    repo.ensure_user("u1")
    repo.set_account_locale("u1", "en-US")
    repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="Release My AI Twin",
            quote="Release My AI Twin this week",
            target={"keywords": ["release"]},
        )
    )
    event = Event(
        user_id="u1",
        source="windows.thought",
        type="thought.note",
        facts={"text": "The release is blocked and needs action"},
        confidence=0.96,
    )
    assert repo.insert_event(event)
    assert repo.enqueue_analysis_job("u1", event.event_id, cloud_approved=True)

    result = asyncio.run(
        AnalysisQueueProcessor(
            repo,
            ProactiveService(repo),
            ChineseDiscoveryGateway(),
            retry_base_seconds=0,
        ).process_event("u1", event.event_id)
    )

    assert result.retried == 1
    assert repo.list_advice("u1") == []
    job = repo.get_analysis_job("u1", event.event_id)
    assert job.status == "retry"
    assert job.last_reason == "model_output_locale_mismatch"
    repo.close()


def test_english_direct_model_output_fails_closed_on_chinese_response(
    tmp_path,
    monkeypatch,
):
    class ChineseDirectModel:
        supports_pre_reserved_budget = False

        async def generate(self, route, prompt, context, user_id=None):
            assert context["response_locale"] == "en-US"
            return "这是模型生成的中文回答，不能显示给英文账号。"

    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    monkeypatch.setenv("MOUCHEN_ALLOW_OPEN_REGISTRATION", "true")
    import app.main as main

    _reset_main(main, tmp_path / "direct-model-locale.db")
    monkeypatch.setattr(main, "models", ChineseDirectModel())
    monkeypatch.setattr(
        main,
        "choose_route",
        lambda *_args, **_kwargs: ModelRoute("ollama", "test-model", "routine"),
    )
    with TestClient(main.app) as client:
        registered = _register(
            client,
            "EnglishOwner",
            "windows-direct",
            locale="en-US",
        )
        headers = {
            "Authorization": f"Bearer {registered.json()['access_token']}"
        }
        response = client.post(
            "/v1/model/analyze",
            headers=headers,
            json={
                "level": 2,
                "purpose": "user_question",
                "prompt": "What should I do next?",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["degraded"] is True
    assert payload["provider"] == "template"
    assert payload["content"].startswith("The model returned content in the wrong language")
