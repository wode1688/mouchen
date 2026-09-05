import importlib
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from app.domain.models import AdviceLevel, AdviceRecord, Goal, utc_now


class ApiFakeGateway:
    async def generate(self, route, prompt, context, user_id=None):
        assert route.model == "gpt-5.6-sol"
        assert user_id == "u1"
        if "候选发现器" in prompt:
            return """{
              "intervene": true,
              "issue_subject": "customer complaint response",
              "category": "客户投诉",
              "requested_level": 2,
              "evidence_quote": "发布质量不符合约定，需要今天回复",
              "action": "核对投诉事实并准备可兑现的回复",
              "first_step": "打开投诉原文并列出对方指出的问题",
              "alternative": "先确认收到并约定核验时间",
              "prediction_outcome": "若今天不处理，发布验收会继续受阻",
              "deadline_hours": 12,
              "confidence": 0.88,
              "adopted_expected_result": "今天形成一份基于投诉事实的可兑现回复",
              "adopted_confidence": 0.82,
              "urgency": 0.86,
              "impact": 0.84
            }"""
        return """{
          "action": "先保留错误记录，再切换到上一稳定版本",
          "first_step": "打开失败详情并保存错误码",
          "alternative": "改用人工发布通道",
          "prediction_outcome": "若不处理，发布任务会继续阻塞",
          "deadline_hours": 6,
          "confidence": 0.83,
          "adopted_expected_result": "发布恢复到上一稳定版本并保留错误记录",
          "adopted_confidence": 0.80
        }"""

    def second_opinion_route(self):
        return "anthropic_via_claude_code", "claude-fable-5"

    async def second_opinion(self, prompt, context):
        return '{"supported": true, "reason": "证据充分"}'


def _insert_feedback_advice(main, *, user_id: str = "u1") -> AdviceRecord:
    goal = main.repo.insert_goal(
        Goal(
            user_id=user_id,
            domain="work",
            title="Feedback API goal",
            quote="Use feedback to improve future recommendations",
        )
    )
    now = utc_now()
    return main.repo.insert_advice(
        AdviceRecord(
            user_id=user_id,
            domain=goal.domain,
            requested_level=AdviceLevel.L2,
            goal_id=goal.id,
            goal_quote=goal.quote,
            evidence=[
                {
                    "event_id": uuid4(),
                    "source": "test.api",
                    "fact": "A recommendation needs user feedback",
                    "observed_at": now,
                    "confidence": 0.95,
                }
            ],
            action="Review the recommendation",
            first_step="Open the recommendation",
            alternative="Defer it for later review",
            prediction={
                "outcome": "The recommendation remains unresolved",
                "deadline": now + timedelta(days=1),
                "confidence": 0.8,
            },
            adopted_expected_result="The recommendation is acted on",
            adopted_confidence=0.8,
            urgency=0.8,
            impact=0.8,
            novelty=0.8,
            relevance=0.9,
            context_fit=1.0,
            interruption_cost=0.1,
            dedupe_key=f"feedback-api:{uuid4()}",
            effective_level=AdviceLevel.L2,
            proactive_score=0.85,
            delivery="immediate",
        )
    )


def test_health_and_demo_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("MOUCHEN_MODEL_PROVIDER", "codex_cli")
    monkeypatch.setenv("MOUCHEN_DEMO_SEED_ENABLED", "true")
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "api.db")
    main.service = main.ProactiveService(main.repo)
    with TestClient(main.app) as client:
        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert health["model_provider"] == "codex_cli"
        assert health["runtime_contract"] == "proactive-analysis-v2"
        analysis = client.get(
            "/v1/analysis/status", headers={"X-User-Id": "u1"}
        ).json()
        assert analysis["waiting"] == 0
        assert analysis["last_status"] is None
        assert analysis["last_failure_reason"] is None
        seeded = client.post("/v1/demo/seed")
        assert seeded.status_code == 200, seeded.text
        advice = client.get("/v1/advice").json()
        assert len(advice) == 1
        assert advice[0]["goal_quote"]


def test_feedback_api_supports_later_guidance_and_terminal_conflict_rules(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "feedback-api.db"))
    monkeypatch.delenv("MOUCHEN_API_TOKEN", raising=False)
    monkeypatch.delenv("MOUCHEN_API_TOKEN_FILE", raising=False)
    monkeypatch.delenv("MOUCHEN_SINGLE_USER_ID", raising=False)
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "feedback-api.db")
    main.service = main.ProactiveService(main.repo)
    advice = _insert_feedback_advice(main)
    headers = {"X-User-Id": "u1"}
    endpoint = f"/v1/advice/{advice.id}/feedback"

    with TestClient(main.app) as client:
        empty_guidance = client.post(
            endpoint,
            headers=headers,
            json={"kind": "guidance", "note": "   "},
        )
        assert empty_guidance.status_code == 422, empty_guidance.text

        feedback_id = str(uuid4())
        later = client.post(
            endpoint,
            headers=headers,
            json={"feedback_id": feedback_id, "kind": "later"},
        )
        assert later.status_code == 200, later.text
        later_body = later.json()
        assert later_body["status"] == "recorded"
        assert later_body["advice"]["id"] == str(advice.id)
        assert later_body["advice"]["status"] == "active"
        assert later_body["advice"]["snoozed_until"] is not None
        later_retry = client.post(
            endpoint,
            headers=headers,
            json={"feedback_id": feedback_id, "kind": "later"},
        )
        assert later_retry.status_code == 200, later_retry.text
        assert main.repo._connection.execute(
            "SELECT COUNT(*) AS count FROM feedback WHERE feedback_id=?",
            (feedback_id,),
        ).fetchone()["count"] == 1

        adopted = client.post(endpoint, headers=headers, json={"kind": "adopted"})
        assert adopted.status_code == 200, adopted.text
        assert adopted.json()["advice"]["status"] == "adopted"
        assert adopted.json()["advice"]["snoozed_until"] is None

        adopted_retry = client.post(
            endpoint,
            headers=headers,
            json={"kind": "adopted"},
        )
        assert adopted_retry.status_code == 200, adopted_retry.text
        assert adopted_retry.json()["advice"]["status"] == "adopted"

        adopted_count = main.repo._connection.execute(
            "SELECT COUNT(*) AS count FROM feedback "
            "WHERE advice_id=? AND user_id=? AND kind='adopted'",
            (str(advice.id), "u1"),
        ).fetchone()["count"]
        assert adopted_count == 1

        conflict = client.post(
            endpoint,
            headers=headers,
            json={"kind": "dismissed"},
        )
        assert conflict.status_code == 409, conflict.text

        guidance = client.post(
            endpoint,
            headers=headers,
            json={
                "kind": "guidance",
                "note": "Prioritize release risks in future advice.",
            },
        )
        assert guidance.status_code == 200, guidance.text
        assert guidance.json()["status"] == "recorded"
        assert guidance.json()["advice"]["id"] == str(advice.id)
        assert guidance.json()["advice"]["status"] == "adopted"

        missing = client.post(
            f"/v1/advice/{uuid4()}/feedback",
            headers=headers,
            json={"kind": "later"},
        )
        assert missing.status_code == 404, missing.text

        acknowledged_advice = _insert_feedback_advice(main)
        acknowledged = client.post(
            f"/v1/advice/{acknowledged_advice.id}/feedback",
            headers=headers,
            json={"feedback_id": str(uuid4()), "kind": "acknowledged"},
        )
        assert acknowledged.status_code == 200, acknowledged.text
        assert acknowledged.json()["status"] == "recorded"
        assert acknowledged.json()["advice"]["status"] == "active"
        attention = main.repo._connection.execute(
            """SELECT state,resolved_reason FROM advice_attention
            WHERE advice_id=?""",
            (str(acknowledged_advice.id),),
        ).fetchone()
        assert tuple(attention) == ("resolved", "feedback:acknowledged")


def test_advice_attention_api_claim_complete_fail_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "attention-api.db"))
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.delenv("MOUCHEN_API_TOKEN", raising=False)
    monkeypatch.delenv("MOUCHEN_API_TOKEN_FILE", raising=False)
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "attention-api.db")
    main.service = main.ProactiveService(main.repo)
    advice = _insert_feedback_advice(main)
    headers = {"X-User-Id": "u1"}

    with TestClient(main.app) as client:
        claimed = client.post(
            "/v1/advice-attention/claim",
            headers=headers,
            json={
                "device_id": "android-1",
                "platform": "android",
                "app_version": "private-alpha",
            },
        )
        assert claimed.status_code == 200, claimed.text
        claim = claimed.json()
        assert claim["status"] == "claimed"
        assert claim["delivery_number"] == 1
        assert claim["advice"]["id"] == str(advice.id)
        assert claim["claim_token"]
        assert claim["lease_expires_at"]

        failed = client.post(
            f"/v1/advice-attention/{advice.id}/fail",
            headers=headers,
            json={
                "device_id": "android-1",
                "claim_token": claim["claim_token"],
                "reason": "notification_permission_missing",
            },
        )
        assert failed.status_code == 200, failed.text
        assert failed.json() == {"status": "released", "delivery_count": 1}

        failed_retry = client.post(
            f"/v1/advice-attention/{advice.id}/fail",
            headers=headers,
            json={
                "device_id": "android-1",
                "claim_token": claim["claim_token"],
                "reason": "notification_permission_missing",
            },
        )
        assert failed_retry.status_code == 200, failed_retry.text
        assert failed_retry.json() == {"status": "released", "delivery_count": 1}

        not_due = client.post(
            "/v1/advice-attention/claim",
            headers=headers,
            json={"device_id": "windows-1", "platform": "windows"},
        )
        assert not_due.status_code == 200, not_due.text
        assert not_due.json() == {"status": "none"}

        # Move only the eligibility clock forward so the API contract can
        # exercise permit #2 without making this test sleep for one hour.
        main.repo._connection.execute(
            "UPDATE advice_attention SET next_eligible_at=? WHERE advice_id=?",
            ((utc_now() - timedelta(seconds=1)).isoformat(), str(advice.id)),
        )
        main.repo._connection.commit()
        reclaimed = client.post(
            "/v1/advice-attention/claim",
            headers=headers,
            json={"device_id": "windows-1", "platform": "windows"},
        ).json()
        assert reclaimed["status"] == "claimed"
        assert reclaimed["delivery_number"] == 2
        completed = client.post(
            f"/v1/advice-attention/{advice.id}/complete",
            headers=headers,
            json={
                "device_id": "windows-1",
                "claim_token": reclaimed["claim_token"],
            },
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "delivered"
        assert completed.json()["delivery_count"] == 2
        assert completed.json()["next_eligible_at"] is None

        retry = client.post(
            f"/v1/advice-attention/{advice.id}/complete",
            headers=headers,
            json={
                "device_id": "windows-1",
                "claim_token": reclaimed["claim_token"],
            },
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["delivery_count"] == 2

        none_due = client.post(
            "/v1/advice-attention/claim",
            headers=headers,
            json={"device_id": "ios-1", "platform": "ios"},
        )
        assert none_due.status_code == 200
        assert none_due.json() == {"status": "none"}


def test_ready_checks_the_active_database(tmp_path, monkeypatch):
    token_file = tmp_path / "ready-token"
    token_file.write_text("ready-secret\n", encoding="utf-8")
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "ready-api.db"))
    monkeypatch.setenv("MOUCHEN_REQUIRE_API_TOKEN", "true")
    monkeypatch.setenv("MOUCHEN_SINGLE_USER_ID", "ready-owner")
    monkeypatch.delenv("MOUCHEN_API_TOKEN", raising=False)
    monkeypatch.setenv("MOUCHEN_API_TOKEN_FILE", str(token_file))
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "ready-api.db")
    main.service = main.ProactiveService(main.repo)
    with TestClient(main.app) as client:
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ready",
            "database": "ready",
            "authentication": "ready",
        }

        main.repo.close()
        unavailable = client.get("/ready")
        assert unavailable.status_code == 503
        assert unavailable.json() == {
            "status": "unavailable",
            "database": "unavailable",
            "authentication": "ready",
        }


def test_ready_fails_when_required_token_file_is_unreadable(tmp_path, monkeypatch):
    missing = tmp_path / "missing-ready-token"
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "missing-ready-token.db"))
    monkeypatch.setenv("MOUCHEN_REQUIRE_API_TOKEN", "true")
    monkeypatch.delenv("MOUCHEN_API_TOKEN", raising=False)
    monkeypatch.setenv("MOUCHEN_API_TOKEN_FILE", str(missing))
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "missing-ready-token.db")
    main.service = main.ProactiveService(main.repo)
    with TestClient(main.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "database": "ready",
        "authentication": "unavailable",
    }
    assert str(missing) not in response.text


def test_event_cloud_approval_refines_proactive_solution(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "cloud-api.db"))
    monkeypatch.setenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol")
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "cloud-api.db")
    main.service = main.ProactiveService(main.repo)
    main.models = ApiFakeGateway()
    headers = {
        "X-User-Id": "u1",
        "X-Proactive-Cloud-Approved": "true",
        "X-Semantic-Fast-Lane": "true",
    }
    with TestClient(main.app) as client:
        goal = client.post(
            "/v1/goals",
            headers=headers,
            json={
                "domain": "work",
                "title": "发布AI替身",
                "quote": "本周必须发布AI替身",
                "target": {"keywords": ["发布"]},
            },
        )
        assert goal.status_code == 200, goal.text
        charter = client.put(
            "/v1/charter/work",
            headers=headers,
            json={"max_level": "L3", "redline_authorized": False},
        )
        assert charter.status_code == 200, charter.text
        response = client.post(
            "/v1/events",
            headers=headers,
            json={
                "source": "android.notification",
                "type": "notification.posted",
                "facts": {"title": "发布失败", "text": "连接超时"},
                "confidence": 0.98,
            },
        )

        assert response.status_code == 200, response.text
        evaluation = response.json()["evaluation"]
        assert "AI_REFINED" in evaluation["reason_codes"]
        assert evaluation["advice"]["first_step"] == "打开失败详情并保存错误码"

        # Clear the per-goal preference cooldown so the second publish in this
        # flow is judged on discovery semantics, not on spacing.
        main.repo._connection.execute(
            "UPDATE advice SET published_at=? WHERE published_at IS NOT NULL",
            (
                (
                    datetime.now(timezone.utc) - timedelta(hours=2)
                ).isoformat(),
            ),
        )
        main.repo._connection.commit()

        discovered = client.post(
            "/v1/events",
            headers=headers,
            json={
                "source": "android.notification",
                "type": "notification.posted",
                "facts": {
                    "package": "com.example.crm",
                    "title": "客户投诉",
                    "text": "发布质量不符合约定，需要今天回复",
                },
                "confidence": 0.96,
            },
        )

        assert discovered.status_code == 200, discovered.text
        discovered_evaluation = discovered.json()["evaluation"]
        assert "AI_DISCOVERED" in discovered_evaluation["reason_codes"]
        assert discovered_evaluation["advice"]["first_step"] == "打开投诉原文并列出对方指出的问题"


def test_quiet_hours_context_defers_event_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "quiet-hours-api.db"))
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "quiet-hours-api.db")
    main.service = main.ProactiveService(main.repo)
    headers = {"X-User-Id": "u1"}
    with TestClient(main.app) as client:
        goal = client.post(
            "/v1/goals",
            headers=headers,
            json={
                "domain": "work",
                "title": "保持发布稳定",
                "quote": "发布故障必须及时处理",
                "target": {"keywords": ["发布", "故障"]},
            },
        )
        assert goal.status_code == 200, goal.text

        response = client.post(
            "/v1/events?quiet_hours=true",
            headers=headers,
            json={
                "source": "android.notification",
                "type": "notification.posted",
                "facts": {"title": "发布失败", "text": "部署连接超时"},
                "confidence": 0.99,
            },
        )

        assert response.status_code == 200, response.text
        evaluation = response.json()["evaluation"]
        assert evaluation["decision"] == "publish"
        assert evaluation["advice"]["delivery"] == "brief"
        assert "CONTEXT_DEFERRED" in evaluation["reason_codes"]


def test_goal_review_endpoint_discovers_from_neutral_evidence_and_is_idempotent(tmp_path, monkeypatch):
    from datetime import timedelta

    from app.domain.models import Event, Goal, utc_now

    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "review-api.db"))
    monkeypatch.setenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol")
    import app.main as main

    class ReviewApiGateway(ApiFakeGateway):
        def __init__(self):
            self.calls = 0

        async def generate(self, route, prompt, context, user_id=None):
            self.calls += 1
            assert "proactive activity reviewer" in prompt
            assert "发布构建仍在排队" in context["event_text"]
            return """{
              "intervene": true,
              "issue_subject": "release build queue delay",
              "category": "milestone_drift",
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
              "impact": 0.84
            }"""

    gateway = ReviewApiGateway()
    main.repo.close()
    main.repo = main.Repository(tmp_path / "review-api.db")
    main.service = main.ProactiveService(main.repo)
    main.models = gateway
    main.repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="发布AI替身",
            quote="本周完成AI替身发布",
            target={"keywords": ["发布", "构建"]},
        )
    )
    now = utc_now()
    for index, text in enumerate(("发布构建仍在排队", "发布评审尚待确认"), start=1):
        main.repo.insert_event(
            Event(
                user_id="u1",
                source="android.notification",
                type="notification.posted",
                occurred_at=now - timedelta(minutes=index),
                facts={"package": "com.example.builder", "title": "发布动态", "text": text},
                confidence=0.95,
                evidence_ref=f"api-review-neutral:{index}",
            )
        )

    headers = {
        "X-User-Id": "u1",
        "X-Proactive-Cloud-Approved": "true",
    }
    with TestClient(main.app) as client:
        first = client.post("/v1/reviews/run", headers=headers)
        second = client.post("/v1/reviews/run", headers=headers)

        assert first.status_code == 200, first.text
        assert first.json()["evaluations"][0]["decision"] == "publish"
        assert "PERIODIC_GOAL_REVIEW" in first.json()["evaluations"][0]["reason_codes"]
        assert second.status_code == 200, second.text
        assert second.json()["evaluations"] == []
        assert second.json()["duplicate_windows"] == 1
        assert gateway.calls == 1


def test_restricted_minimized_event_requires_proactive_cloud_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "restricted-cloud-api.db"))
    monkeypatch.setenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol")
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "restricted-cloud-api.db")
    main.service = main.ProactiveService(main.repo)
    main.models = ApiFakeGateway()
    user_headers = {"X-User-Id": "u1"}
    approved_headers = {
        "X-User-Id": "u1",
        "X-Proactive-Cloud-Approved": "true",
        "X-Semantic-Fast-Lane": "true",
    }
    event = {
        "source": "android.accessibility",
        "type": "ui.visible_text",
        "facts": {
            "package": "com.example.crm",
                "visible_text": ["客户投诉：发布质量不符合约定，需要今天回复"],
        },
        "confidence": 0.96,
        "sensitivity": "restricted",
        "consent_scope": "alpha.minimized_context",
    }
    with TestClient(main.app) as client:
        client.post(
            "/v1/goals",
            headers=user_headers,
            json={
                "domain": "work",
                "title": "发布AI替身",
                "quote": "本周必须发布AI替身",
                "target": {"keywords": ["发布"]},
            },
        )

        not_approved = client.post("/v1/events", headers=user_headers, json=event)
        assert not_approved.status_code == 200
        assert not_approved.json()["evaluation"] is None

        approved = client.post("/v1/events", headers=approved_headers, json=event)
        assert approved.status_code == 200
        assert "AI_DISCOVERED" in approved.json()["evaluation"]["reason_codes"]


def test_configured_api_token_protects_v1_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "auth-api.db"))
    monkeypatch.setenv("MOUCHEN_API_TOKEN", "alpha-secret")
    monkeypatch.delenv("MOUCHEN_API_BEARER_TOKEN", raising=False)
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "auth-api.db")
    main.service = main.ProactiveService(main.repo)
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        unauthenticated = client.get("/v1/goals")
        assert unauthenticated.status_code == 401
        assert "alpha-secret" not in unauthenticated.text
        assert client.get(
            "/v1/goals",
            headers={"Authorization": "Bearer wrong"},
        ).status_code == 401
        assert client.get(
            "/v1/goals",
            headers={"Authorization": "Bearer alpha-secret"},
        ).status_code == 200


def test_file_backed_api_token_protects_v1_routes(tmp_path, monkeypatch):
    token_file = tmp_path / "api-token"
    token_file.write_text("file-secret\n", encoding="utf-8")
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "file-auth-api.db"))
    monkeypatch.delenv("MOUCHEN_API_TOKEN", raising=False)
    monkeypatch.setenv("MOUCHEN_API_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("MOUCHEN_API_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("MOUCHEN_API_BEARER_TOKEN_FILE", raising=False)
    monkeypatch.delenv("MOUCHEN_SINGLE_USER_ID", raising=False)
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "file-auth-api.db")
    main.service = main.ProactiveService(main.repo)
    with TestClient(main.app) as client:
        assert client.get("/v1/goals").status_code == 401
        response = client.get(
            "/v1/goals",
            headers={"Authorization": "Bearer file-secret"},
        )
        assert response.status_code == 200
        assert "file-secret" not in response.text


def test_unavailable_api_token_file_fails_closed_without_path_leak(tmp_path, monkeypatch):
    missing = tmp_path / "private-token"
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "missing-file-auth-api.db"))
    monkeypatch.delenv("MOUCHEN_API_TOKEN", raising=False)
    monkeypatch.setenv("MOUCHEN_API_TOKEN_FILE", str(missing))
    monkeypatch.delenv("MOUCHEN_API_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("MOUCHEN_API_BEARER_TOKEN_FILE", raising=False)
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "missing-file-auth-api.db")
    main.service = main.ProactiveService(main.repo)
    with TestClient(main.app) as client:
        response = client.get("/v1/goals")

    assert response.status_code == 503
    assert str(missing) not in response.text


def test_single_user_binding_rejects_missing_or_different_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "single-user-api.db"))
    monkeypatch.setenv("MOUCHEN_API_TOKEN", "alpha-secret")
    monkeypatch.setenv("MOUCHEN_SINGLE_USER_ID", "owner-user")
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "single-user-api.db")
    main.service = main.ProactiveService(main.repo)
    authorization = {"Authorization": "Bearer alpha-secret"}
    with TestClient(main.app) as client:
        missing = client.get("/v1/goals", headers=authorization)
        assert missing.status_code == 403
        assert "owner-user" not in missing.text

        different = client.get(
            "/v1/goals",
            headers={**authorization, "X-User-Id": "other-user"},
        )
        assert different.status_code == 403
        assert "owner-user" not in different.text

        allowed = client.get(
            "/v1/goals",
            headers={**authorization, "X-User-Id": "owner-user"},
        )
        assert allowed.status_code == 200


def test_unconfigured_api_is_loopback_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "loopback-api.db"))
    monkeypatch.delenv("MOUCHEN_API_TOKEN", raising=False)
    monkeypatch.delenv("MOUCHEN_API_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("MOUCHEN_ALLOW_UNAUTHENTICATED_PRIVATE_ALPHA", raising=False)
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "loopback-api.db")
    main.service = main.ProactiveService(main.repo)
    with TestClient(main.app, client=("192.0.2.10", 5050)) as client:
        assert client.get("/health").status_code == 200
        response = client.get("/v1/goals")
        assert response.status_code == 401
        assert response.json() == {"detail": "authentication required"}


def test_model_failure_does_not_echo_sensitive_exception(tmp_path, monkeypatch):
    class LeakingGateway:
        async def generate(self, *_args, **_kwargs):
            raise RuntimeError("owner@example.com sk-test-12345678901234567890")

    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "safe-error-api.db"))
    monkeypatch.delenv("MOUCHEN_API_TOKEN", raising=False)
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "safe-error-api.db")
    main.service = main.ProactiveService(main.repo)
    main.models = LeakingGateway()
    with TestClient(main.app) as client:
        response = client.post(
            "/v1/model/analyze",
            json={
                "level": "L2",
                "purpose": "safe_error_test",
                "prompt": "给出下一步",
                "redacted_context": {"event_text": "发布失败"},
                "outbound_approved": True,
            },
        )

        assert response.status_code == 200
        assert response.json()["degraded"] is True
        assert "owner@example.com" not in response.text
        assert "sk-test" not in response.text


def test_l4_review_failure_preserves_successful_primary_answer(tmp_path, monkeypatch):
    class ReviewFailureGateway:
        async def generate(self, route, _prompt, _context, user_id=None):
            assert route.second_opinion is True
            assert user_id == "u1"
            return "GPT 主回答：先核对发布错误码。"

        def second_opinion_route(self):
            return "anthropic_via_claude_code", "claude-fable-5"

        async def second_opinion(self, _prompt, _context):
            raise RuntimeError("Claude process terminated without output")

    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "review-failure-api.db"))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol")
    monkeypatch.delenv("MOUCHEN_API_TOKEN", raising=False)
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "review-failure-api.db")
    main.service = main.ProactiveService(main.repo)
    main.models = ReviewFailureGateway()
    with TestClient(main.app) as client:
        response = client.post(
            "/v1/model/analyze",
            headers={"X-User-Id": "u1"},
            json={
                "level": "L4",
                "purpose": "redline_review",
                "prompt": "给出下一步",
                "redacted_context": {"event_text": "发布失败"},
                "outbound_approved": True,
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["provider"] == "openai"
        assert body["model"] == "gpt-5.6-sol"
        assert body["content"] == "GPT 主回答：先核对发布错误码。"
        assert body["second_opinion"] is None
        assert body["degraded"] is False


def test_user_question_uses_sol_and_server_retrieved_minimal_context(tmp_path, monkeypatch):
    class QuestionGateway:
        def __init__(self):
            self.calls = []

        async def generate(self, route, prompt, context, user_id=None):
            self.calls.append((route, prompt, context, user_id))
            return "先核对发布失败的错误码。"

    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "question-api.db"))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol")
    monkeypatch.delenv("MOUCHEN_API_TOKEN", raising=False)
    import app.main as main

    gateway = QuestionGateway()
    main.repo.close()
    main.repo = main.Repository(tmp_path / "question-api.db")
    main.service = main.ProactiveService(main.repo)
    main.models = gateway
    headers = {"X-User-Id": "u1"}
    with TestClient(main.app) as client:
        client.post(
            "/v1/goals",
            headers=headers,
            json={
                "domain": "work",
                "title": "发布AI替身",
                "quote": "本周发布AI替身，联系 owner@example.com 核验",
                "target": {"keywords": ["发布", "AI替身"]},
            },
        )
        client.post(
            "/v1/events",
            headers=headers,
            json={
                "source": "android.notification",
                "type": "notification.posted",
                "facts": {
                    "title": "发布状态",
                    "text": ("LOCAL-ONLY-SENTINEL " * 40) + "AI替身发布失败，请保留错误码",
                },
            },
        )
        response = client.post(
            "/v1/model/analyze",
            headers=headers,
            json={
                "level": "L2",
                "purpose": "user_question",
                "prompt": "AI替身发布为什么失败？",
                "redacted_context": {
                    "active_advice_count": 1,
                    "mail_body": "客户端试图附带完整邮件",
                },
                "outbound_approved": True,
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["provider"] == "openai"
        assert response.json()["model"] == "gpt-5.6-sol"
        route, prompt, context, user_id = gateway.calls[0]
        assert route.mode == "pro"
        assert prompt.startswith("AI替身发布为什么失败？\n\n")
        assert "所有面向用户的新生成内容均使用自然、简洁的中文" in prompt
        assert "evidence_quote 必须逐字保留" in prompt
        assert user_id == "u1"
        outbound = str(context)
        assert context["client_state"] == {"active_advice_count": 1}
        assert "发布失败" in outbound
        assert "LOCAL-ONLY-SENTINEL" not in outbound
        assert "客户端试图附带完整邮件" not in outbound
        assert "owner@example.com" not in outbound
