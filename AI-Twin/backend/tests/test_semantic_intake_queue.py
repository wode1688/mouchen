import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from datetime import datetime, timedelta, timezone
import sqlite3
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.analysis_queue import (
    AnalysisQueueConsumer,
    AnalysisQueueProcessor,
    release_analysis_slot,
    try_acquire_analysis_slot,
)
from app.domain.models import Event, Goal, Sensitivity
from app.domain.problem_signals import event_text
from app.model_gateway import ModelGateway, ModelUnavailable
from app.review_engine import _event_search_text
from app.service import ProactiveService
from app.storage import AdviceConflict, Repository


class RecordingGateway:
    def __init__(
        self,
        *,
        evidence_quote: str = "发布质量有风险",
        intervene: bool = True,
        fail_times: int = 0,
        expected_domain: str | None = None,
        review_supported: bool = True,
        second_fail: bool = False,
    ) -> None:
        self.evidence_quote = evidence_quote
        self.intervene = intervene
        self.fail_times = fail_times
        self.expected_domain = expected_domain
        self.review_supported = review_supported
        self.second_fail = second_fail
        self.calls = 0
        self.second_calls = 0
        self.contexts = []

    async def generate(self, route, prompt, context, user_id=None):
        self.calls += 1
        self.contexts.append(context)
        if self.expected_domain is not None:
            assert context["domain"] == self.expected_domain
        if self.calls <= self.fail_times:
            raise RuntimeError("temporary provider failure")
        if not self.intervene:
            return '{"intervene": false}'
        return json.dumps(
            {
                "intervene": True,
                "category": f"semantic_{self.calls}",
                "issue_subject": "goal progress blockage",
                "requested_level": 2,
                "evidence_quote": self.evidence_quote,
                "action": "先核对现状，再锁定一个今天可交付的结果",
                "first_step": "打开当前清单并标出唯一阻塞项",
                "alternative": "若依据不足，先补齐事实再决定",
                "prediction_outcome": "若不处理，目标会继续缺少可验证进展",
                "deadline_hours": 24,
                "confidence": 0.88,
                "adopted_expected_result": "今天形成一个可核验的交付结果",
                "adopted_confidence": 0.82,
                "urgency": 0.78,
                "impact": 0.80,
            },
            ensure_ascii=False,
        )

    def second_opinion_route(self):
        return "anthropic_via_claude_code", "claude-fable-5"

    async def second_opinion(self, prompt, context):
        self.second_calls += 1
        if self.second_fail:
            raise RuntimeError("temporary review provider failure")
        return json.dumps({"supported": self.review_supported})


class MustNotRunGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, route, prompt, context, user_id=None):
        self.calls += 1
        raise AssertionError("ordinary ingestion must not await the model")


class UnavailableGateway:
    def __init__(self, *, reason_code: str, retryable: bool) -> None:
        self.reason_code = reason_code
        self.retryable = retryable
        self.calls = 0

    async def generate(self, route, prompt, context, user_id=None):
        self.calls += 1
        raise ModelUnavailable(
            "private provider detail",
            reason_code=self.reason_code,
            retryable=self.retryable,
        )


def _goal(repo: Repository, *, domain: str = "work", keyword: str = "发布") -> Goal:
    return repo.insert_goal(
        Goal(
            user_id="u1",
            domain=domain,
            title=f"推进{keyword}",
            quote=f"我本周要让{keyword}形成可验证进展",
            target={"keywords": [keyword]},
        )
    )


def _queue(repo: Repository, event: Event) -> None:
    assert repo.insert_event(event)
    assert repo.enqueue_analysis_job(
        event.user_id,
        event.event_id,
        cloud_approved=True,
    )


def test_analysis_status_exposes_only_queue_telemetry(tmp_path):
    repo = Repository(tmp_path / "analysis-status.db")
    event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "private event text"},
    )
    _queue(repo, event)

    pending = repo.analysis_status("u1")
    assert pending["waiting"] == 1
    assert pending["counts"]["pending"] == 1
    assert "private event text" not in json.dumps(pending)

    claim = repo.claim_analysis_job("u1", event.event_id)
    assert claim is not None
    assert repo.retry_analysis_job(
        "u1",
        event.event_id,
        "codex_cli_network_failure",
        attempt=claim.job.attempts,
    )
    retrying = repo.analysis_status("u1")
    assert retrying["counts"]["retry"] == 1
    assert retrying["waiting_reasons"] == {
        "codex_cli_network_failure": 1,
    }
    assert retrying["last_status"] == "retry"
    assert retrying["last_reason"] == "codex_cli_network_failure"
    assert retrying["last_failure_reason"] == "codex_cli_network_failure"

    second_claim = repo.claim_analysis_job("u1", event.event_id)
    assert second_claim is not None
    assert repo.complete_analysis_job(
        "u1",
        event.event_id,
        "published",
        "advice_published",
        attempt=second_claim.job.attempts,
    )
    completed = repo.analysis_status("u1")
    assert completed["last_status"] == "published"
    assert completed["last_failure_reason"] is None
    repo.close()


def test_sms_body_enters_semantic_queue_and_publishes_advice(tmp_path):
    repo = Repository(tmp_path / "sms.db")
    _goal(repo)
    event = Event(
        user_id="u1",
        source="android.sms",
        type="message.sms",
        facts={"body": "客户反馈发布质量有风险，今晚需要确定处理路径"},
        sensitivity=Sensitivity.SENSITIVE,
    )
    assert "发布质量有风险" in event_text(event)
    assert "发布质量有风险" in _event_search_text(event)
    _queue(repo, event)
    gateway = RecordingGateway()

    result = asyncio.run(
        AnalysisQueueProcessor(repo, ProactiveService(repo), gateway).process_event(
            "u1", event.event_id
        )
    )

    assert result.published == 1
    assert len(repo.list_advice("u1")) == 1
    assert repo.get_analysis_job("u1", event.event_id).status == "published"
    repo.close()


def test_neutral_sms_finishes_without_model_or_advice(tmp_path):
    repo = Repository(tmp_path / "neutral.db")
    _goal(repo)
    event = Event(
        user_id="u1",
        source="android.sms",
        type="message.sms",
        facts={"body": "下午三点见，收到请回复"},
    )
    _queue(repo, event)
    gateway = RecordingGateway()

    result = asyncio.run(
        AnalysisQueueProcessor(repo, ProactiveService(repo), gateway).process_event(
            "u1", event.event_id
        )
    )

    assert result.no_intervention == 1
    assert gateway.calls == 0
    assert repo.list_advice("u1") == []
    assert repo.get_analysis_job("u1", event.event_id).status == "no_intervention"
    assert repo.analysis_model_calls_used(
        "u1", since=datetime.now(timezone.utc) - timedelta(minutes=5)
    ) == 0
    repo.close()


def test_transient_model_failure_retries_then_publishes_exactly_once(tmp_path):
    repo = Repository(tmp_path / "retry.db")
    _goal(repo)
    event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "我在发布范围上来回比较，始终没有锁定下一步"},
    )
    _queue(repo, event)
    gateway = RecordingGateway(
        evidence_quote="发布范围上来回比较",
        fail_times=1,
    )
    processor = AnalysisQueueProcessor(
        repo,
        ProactiveService(repo),
        gateway,
        retry_base_seconds=0,
    )

    first = asyncio.run(processor.process_event("u1", event.event_id))
    assert first.retried == 1
    assert repo.get_analysis_job("u1", event.event_id).status == "retry"

    second = asyncio.run(processor.process_event("u1", event.event_id))
    third = asyncio.run(processor.process_event("u1", event.event_id))

    assert second.published == 1
    assert third.attempted == 0
    assert repo.get_analysis_job("u1", event.event_id).attempts == 2
    assert repo.get_analysis_job("u1", event.event_id).status == "published"
    assert len(repo.list_advice("u1")) == 1
    repo.close()


def test_nonretryable_model_failure_is_terminal_and_preserves_reason(tmp_path):
    repo = Repository(tmp_path / "terminal-model-failure.db")
    _goal(repo, keyword="release")
    event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "The release plan still has a material risk"},
    )
    _queue(repo, event)
    gateway = UnavailableGateway(
        reason_code="openai_auth_failed",
        retryable=False,
    )
    processor = AnalysisQueueProcessor(repo, ProactiveService(repo), gateway)

    first = asyncio.run(processor.process_event("u1", event.event_id))
    second = asyncio.run(processor.process_event("u1", event.event_id))

    assert first.failed == 1
    assert first.retried == 0
    assert second.attempted == 0
    assert gateway.calls == 1
    job = repo.get_analysis_job("u1", event.event_id)
    assert job is not None
    assert job.status == "no_intervention"
    assert job.last_reason == "openai_auth_failed"
    assert job.last_failure_reason == "openai_auth_failed"
    assert repo.analysis_model_calls_used(
        "u1", since=datetime.now(timezone.utc) - timedelta(minutes=5)
    ) == 1
    repo.close()


def test_retryable_model_failure_history_survives_stale_retirement(tmp_path):
    repo = Repository(tmp_path / "retryable-model-failure.db")
    _goal(repo, keyword="release")
    occurred_at = datetime.now(timezone.utc)
    event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "The release plan still has a material risk"},
        occurred_at=occurred_at,
    )
    _queue(repo, event)
    gateway = UnavailableGateway(
        reason_code="openai_transport_unavailable",
        retryable=True,
    )

    result = asyncio.run(
        AnalysisQueueProcessor(
            repo,
            ProactiveService(repo),
            gateway,
            retry_base_seconds=0,
        ).process_event("u1", event.event_id)
    )

    assert result.retried == 1
    retrying = repo.get_analysis_job("u1", event.event_id)
    assert retrying is not None
    assert retrying.last_reason == "openai_transport_unavailable"
    assert retrying.last_failure_reason == "openai_transport_unavailable"
    assert repo.retire_stale_analysis_jobs(
        "u1",
        now=occurred_at + timedelta(hours=7),
    ) == 1
    retired = repo.get_analysis_job("u1", event.event_id)
    assert retired is not None
    assert retired.last_reason == "stale_discover_retired"
    assert retired.last_failure_reason == "openai_transport_unavailable"
    repo.close()


def test_stale_retirement_is_scoped_by_user_when_event_ids_collide(tmp_path):
    repo = Repository(tmp_path / "tenant-stale-retirement.db")
    now = datetime.now(timezone.utc)
    shared_event_id = uuid4()
    stale = Event(
        event_id=shared_event_id,
        user_id="tenant-a",
        source="android.notification",
        type="notification.posted",
        facts={"text": "Historical status for tenant A"},
        occurred_at=now - timedelta(hours=7),
    )
    fresh = Event(
        event_id=shared_event_id,
        user_id="tenant-b",
        source="android.notification",
        type="notification.posted",
        facts={"text": "Fresh status for tenant B"},
        occurred_at=now,
    )
    for event in (stale, fresh):
        assert repo.insert_event(event)
        assert repo.enqueue_analysis_job(
            event.user_id,
            event.event_id,
            cloud_approved=True,
        )

    assert repo.retire_stale_analysis_jobs("tenant-a", now=now) == 1
    assert repo.get_analysis_job("tenant-a", shared_event_id).status == "no_intervention"
    assert repo.get_analysis_job("tenant-b", shared_event_id).status == "pending"
    repo.close()


def test_global_queue_budget_survives_restart_without_charging_blocked_tenant(
    tmp_path,
):
    path = tmp_path / "global-queue-restart.db"
    now = datetime.now(timezone.utc)
    repo = Repository(path)
    first = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "How should I handle release path one?"},
        occurred_at=now,
    )
    second = Event(
        user_id="u2",
        source="android.thought",
        type="thought.note",
        facts={"text": "How should I handle release path two?"},
        occurred_at=now,
    )
    _queue(repo, first)
    _queue(repo, second)
    claim_time = now + timedelta(seconds=1)
    first_claim = repo.claim_analysis_job(
        "u1",
        first.event_id,
        now=claim_time,
        hourly_call_limit=20,
        daily_call_limit=20,
        global_hourly_call_limit=2,
        global_daily_call_limit=4,
    )
    assert first_claim is not None
    assert first_claim.reservation_id is not None
    repo.close()

    restarted = Repository(path)
    for seconds in (2, 3):
        assert restarted.claim_analysis_job(
            "u2",
            second.event_id,
            now=now + timedelta(seconds=seconds),
            hourly_call_limit=20,
            daily_call_limit=20,
            global_hourly_call_limit=2,
            global_daily_call_limit=4,
            max_attempts=1,
        ) is None
    blocked = restarted.get_analysis_job("u2", second.event_id)
    assert blocked is not None
    assert blocked.status == "pending"
    assert blocked.attempts == 0
    assert blocked.last_reason == "global_model_budget_deferred"
    assert blocked.next_attempt_at > now
    assert restarted.analysis_model_calls_used(
        "u2", since=now - timedelta(minutes=1)
    ) == 0
    assert restarted._connection.execute(
        "SELECT COUNT(*) FROM analysis_model_reservations WHERE user_id='u2'"
    ).fetchone()[0] == 0
    restarted.close()


def test_global_queue_budget_claim_is_atomic_across_connections(tmp_path):
    path = tmp_path / "global-queue-concurrent.db"
    now = datetime.now(timezone.utc)
    seed = Repository(path)
    events = []
    for user_id in ("u1", "u2"):
        event = Event(
            user_id=user_id,
            source="android.thought",
            type="thought.note",
            facts={"text": f"How should {user_id} handle this release?"},
            occurred_at=now,
        )
        _queue(seed, event)
        events.append(event)
    seed.close()
    repositories = [Repository(path), Repository(path)]

    def claim(index):
        return repositories[index].claim_analysis_job(
            f"u{index + 1}",
            events[index].event_id,
            now=now + timedelta(seconds=1),
            hourly_call_limit=20,
            daily_call_limit=20,
            global_hourly_call_limit=2,
            global_daily_call_limit=4,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, range(2)))
    assert sum(value is not None for value in claims) == 1
    blocked_index = 0 if claims[0] is None else 1
    blocked = repositories[blocked_index].get_analysis_job(
        f"u{blocked_index + 1}",
        events[blocked_index].event_id,
    )
    assert blocked is not None
    assert blocked.attempts == 0
    assert blocked.last_reason == "global_model_budget_deferred"
    for repository in repositories:
        repository.close()


def test_queue_global_reservation_is_not_charged_twice_by_real_gateway(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MOUCHEN_MODEL_PROVIDER", "openai")
    repo = Repository(tmp_path / "queue-no-double-charge.db")
    _goal(repo, keyword="release")
    event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "release decision needs one concrete next step"},
    )
    _queue(repo, event)
    gateway = ModelGateway(global_budget_reserver=repo.reserve_global_direct_model_call)
    provider_calls = 0

    async def fake_openai(route, prompt, context, user_id):
        nonlocal provider_calls
        provider_calls += 1
        return '{"intervene": false}'

    monkeypatch.setattr(gateway, "_openai", fake_openai)
    result = asyncio.run(
        AnalysisQueueProcessor(
            repo,
            ProactiveService(repo),
            gateway,
            hourly_model_call_limit=20,
            daily_model_call_limit=20,
            global_hourly_model_call_limit=2,
            global_daily_model_call_limit=4,
        ).process_event("u1", event.event_id)
    )
    assert result.no_intervention == 1
    assert provider_calls == 1
    assert repo._connection.execute(
        "SELECT COUNT(*) FROM global_model_usage"
    ).fetchone()[0] == 0
    assert repo.global_model_calls_used(
        since=datetime.now(timezone.utc) - timedelta(minutes=5)
    ) == 1
    repo.close()


def test_running_job_is_reclaimed_only_after_lease_expiry(tmp_path):
    path = tmp_path / "restart.db"
    repo = Repository(path)
    _goal(repo)
    event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "我在发布优先级之间反复比较，还没有决定下一步"},
    )
    _queue(repo, event)
    assert repo.claim_analysis_job("u1", event.event_id) is not None
    assert repo.get_analysis_job("u1", event.event_id).status == "running"
    repo.close()

    restarted = Repository(path)
    recovered = restarted.get_analysis_job("u1", event.event_id)
    assert recovered.status == "running"
    assert recovered.last_reason is None
    assert restarted.claim_analysis_job("u1", event.event_id) is None
    stale = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    restarted._connection.execute(
        "UPDATE analysis_jobs SET updated_at=? WHERE event_id=?",
        (stale, str(event.event_id)),
    )
    restarted._connection.commit()
    gateway = RecordingGateway(evidence_quote="发布优先级之间反复比较")
    result = asyncio.run(
        AnalysisQueueProcessor(
            restarted,
            ProactiveService(restarted),
            gateway,
            retry_base_seconds=0,
        ).process_event("u1", event.event_id)
    )

    assert result.published == 1
    assert len(restarted.list_advice("u1")) == 1
    restarted.close()


def test_old_analysis_lease_cannot_complete_a_newer_retry_attempt(tmp_path):
    repo = Repository(tmp_path / "lease.db")
    _goal(repo)
    event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "我还在比较发布方案"},
    )
    _queue(repo, event)
    first = repo.claim_analysis_job("u1", event.event_id)
    assert first is not None and first.job.attempts == 1
    assert repo.retry_analysis_job(
        "u1",
        event.event_id,
        "temporary_failure",
        attempt=first.job.attempts,
        delay_seconds=0,
    )
    second = repo.claim_analysis_job("u1", event.event_id)
    assert second is not None and second.job.attempts == 2

    assert not repo.complete_analysis_job(
        "u1",
        event.event_id,
        "no_intervention",
        "stale_worker",
        attempt=first.job.attempts,
    )
    assert repo.get_analysis_job("u1", event.event_id).status == "running"
    assert repo.complete_analysis_job(
        "u1",
        event.event_id,
        "no_intervention",
        "new_worker",
        attempt=second.job.attempts,
    )
    repo.close()


def test_shared_text_receives_same_goal_thought_and_active_advice_context(tmp_path):
    repo = Repository(tmp_path / "context.db")
    _goal(repo)
    thought = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "我准备推进产品发布，但交付范围还没有锁定"},
    )
    _queue(repo, thought)
    first_gateway = RecordingGateway(evidence_quote="交付范围还没有锁定")
    asyncio.run(
        AnalysisQueueProcessor(repo, ProactiveService(repo), first_gateway).process_event(
            "u1", thought.event_id
        )
    )
    assert len(repo.list_advice("u1")) == 1

    shared = Event(
        user_id="u1",
        source="android.share",
        type="shared.text",
        facts={"text": "我刚整理出发布清单，仍在比较两个方案"},
    )
    _queue(repo, shared)
    second_gateway = RecordingGateway(intervene=False)
    asyncio.run(
        AnalysisQueueProcessor(repo, ProactiveService(repo), second_gateway).process_event(
            "u1", shared.event_id
        )
    )

    context = second_gateway.contexts[0]
    assert "交付范围还没有锁定" in str(context["recent_owner_context"])
    assert context["current_active_advice"]
    assert "先核对现状" in context["current_active_advice"][0]["action"]
    repo.close()


def test_multi_goal_context_never_leaks_unrelated_thought(tmp_path):
    repo = Repository(tmp_path / "multi-goal.db")
    _goal(repo, domain="work", keyword="发布")
    health_goal = _goal(repo, domain="health", keyword="跑步")
    unrelated = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "产品发布清单还需要继续整理"},
    )
    assert repo.insert_event(unrelated)
    current = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "跑步训练安排让我一直在比较两个方案"},
    )
    _queue(repo, current)
    gateway = RecordingGateway(
        evidence_quote="跑步训练安排",
        intervene=False,
        expected_domain="health",
    )

    asyncio.run(
        AnalysisQueueProcessor(repo, ProactiveService(repo), gateway).process_event(
            "u1", current.event_id
        )
    )

    context = gateway.contexts[0]
    assert context["domain"] == health_goal.domain
    assert "产品发布清单" not in str(context["recent_owner_context"])
    repo.close()


def test_restricted_event_without_minimized_consent_is_rejected(tmp_path):
    repo = Repository(tmp_path / "restricted.db")
    _goal(repo)
    event = Event(
        user_id="u1",
        source="android.share",
        type="shared.text",
        facts={"text": "发布计划包含受限原文，需要分析"},
        sensitivity=Sensitivity.RESTRICTED,
        consent_scope="alpha.local",
    )
    _queue(repo, event)
    gateway = RecordingGateway(evidence_quote="需要分析")

    result = asyncio.run(
        AnalysisQueueProcessor(repo, ProactiveService(repo), gateway).process_event(
            "u1", event.event_id
        )
    )

    assert result.no_intervention == 1
    assert gateway.calls == 0
    assert repo.list_advice("u1") == []
    job = repo.get_analysis_job("u1", event.event_id)
    assert job.status == "no_intervention"
    assert job.last_reason == "restricted_not_authorized"
    repo.close()


def test_analysis_drain_endpoint_requires_approval_and_processes_pending_job(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "drain-api.db"))
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "drain-api.db")
    main.service = main.ProactiveService(main.repo)
    main.models = RecordingGateway(evidence_quote="发布路径上来回比较")
    headers = {"X-User-Id": "u1"}
    approved = {
        "X-User-Id": "u1",
        "X-Proactive-Cloud-Approved": "true",
    }
    with TestClient(main.app) as client:
        goal_response = client.post(
            "/v1/goals",
            headers=headers,
            json={
                "domain": "work",
                "title": "推进发布",
                "quote": "我本周要让发布形成可验证进展",
                "target": {"keywords": ["发布"]},
            },
        )
        assert goal_response.status_code == 200
        ingested = client.post(
            "/v1/events",
            headers=approved,
            json={
                "source": "android.thought",
                "type": "thought.note",
                "facts": {"text": "我在发布路径上来回比较，仍没有决定下一步"},
            },
        )
        assert ingested.status_code == 200
        assert ingested.json()["evaluation"] is None
        assert ingested.json()["queued"] is True
        event_id = ingested.json()["event"]["event_id"]

        denied = client.post("/v1/analysis/drain", headers=headers)
        assert denied.status_code == 409
        assert main.repo.get_analysis_job("u1", event_id).status == "pending"

        drained = client.post("/v1/analysis/drain", headers=approved)
        assert drained.status_code == 200, drained.text
        assert drained.json()["published"] == 1
        assert len(client.get("/v1/advice", headers=headers).json()) == 1


def test_stale_phone_backlog_is_stored_without_jobs_with_background_enabled(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "backlog-api.db"))
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "true")
    import app.main as main

    gateway = MustNotRunGateway()
    main.repo.close()
    main.repo = main.Repository(tmp_path / "backlog-api.db")
    main.service = main.ProactiveService(main.repo)
    main.models = gateway
    headers = {
        "X-User-Id": "u1",
        "X-Proactive-Cloud-Approved": "true",
    }
    with TestClient(main.app) as client:
        goal_response = client.post(
            "/v1/goals",
            headers=headers,
            json={
                "domain": "general",
                "title": "整理日常信息",
                "quote": "持续整理手机上的日常信息并发现真正需要处理的事情",
                "target": {},
            },
        )
        assert goal_response.status_code == 200
        occurred_at = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        for index in range(40):
            response = client.post(
                "/v1/events",
                headers=headers,
                json={
                    "source": "android.sms",
                    "type": "message.sms",
                    "occurred_at": occurred_at,
                    "facts": {"body": f"同步记录 {index}，今天一切正常"},
                    "evidence_ref": f"backlog-sms-{index}",
                },
            )
            assert response.status_code == 200
            assert response.json()["evaluation"] is None
            assert response.json()["queued"] is False

        assert main.repo._connection.execute(
            "SELECT COUNT(*) AS count FROM events"
        ).fetchone()["count"] == 40
        assert main.repo._connection.execute(
            "SELECT COUNT(*) AS count FROM analysis_jobs"
        ).fetchone()["count"] == 0
        assert gateway.calls == 0


def test_event_ingest_queue_contract_covers_discovery_l3_and_fail_closed_paths(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    import app.main as main

    main.repo = main.Repository(tmp_path / "queue-contract.db")
    main.service = main.ProactiveService(main.repo)
    main.models = RecordingGateway()
    approved = {
        "X-User-Id": "u1",
        "X-Proactive-Cloud-Approved": "true",
    }
    with TestClient(main.app) as client:
        goal = client.post(
            "/v1/goals",
            headers=approved,
            json={
                "domain": "security",
                "title": "Protect account release",
                "quote": "I will protect the account while preparing the release",
                "target": {"keywords": ["account", "release"]},
            },
        )
        assert goal.status_code == 200

        discovery = client.post(
            "/v1/events",
            headers=approved,
            json={
                "source": "android.thought",
                "type": "thought.note",
                "facts": {"text": "I am comparing the release paths and need a next step"},
            },
        )
        assert discovery.status_code == 200
        assert discovery.json()["evaluation"] is None
        assert discovery.json()["queued"] is True

        warning = client.post(
            "/v1/events",
            headers=approved,
            json={
                "source": "android.notification",
                "type": "notification.posted",
                "facts": {
                    "title": "Account warning",
                    "text": "Suspicious login detected",
                },
                "sensitivity": "sensitive",
            },
        )
        assert warning.status_code == 200
        assert warning.json()["queued"] is True
        warning_job = main.repo.get_analysis_job(
            "u1", warning.json()["event"]["event_id"]
        )
        assert warning_job is not None
        assert warning_job.job_kind == "refine_deterministic"

        unapproved = client.post(
            "/v1/events",
            headers={"X-User-Id": "u1"},
            json={
                "source": "android.thought",
                "type": "thought.note",
                "facts": {"text": "I am comparing another release path"},
            },
        )
        assert unapproved.status_code == 200
        assert unapproved.json()["queued"] is False

        stale_warning = client.post(
            "/v1/events",
            headers=approved,
            json={
                "source": "android.notification",
                "type": "notification.posted",
                "occurred_at": (
                    datetime.now(timezone.utc) - timedelta(days=30)
                ).isoformat(),
                "facts": {
                    "title": "Account warning",
                    "text": "Suspicious login detected",
                },
                "sensitivity": "sensitive",
            },
        )
        assert stale_warning.status_code == 200
        assert stale_warning.json()["evaluation"] is None
        assert stale_warning.json()["queued"] is False
        assert main.repo.get_analysis_job(
            "u1", stale_warning.json()["event"]["event_id"]
        ) is None


def _deterministic_security_job(repo: Repository):
    goal = _goal(repo, domain="security", keyword="account")
    event = Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={"title": "Account warning", "text": "Suspicious login detected"},
        sensitivity=Sensitivity.SENSITIVE,
    )
    service = ProactiveService(repo)
    evaluation = service.ingest(event)
    assert evaluation is not None and evaluation.advice is not None
    assert evaluation.advice.requested_level.value == 3
    service.discard_unreviewed(evaluation)
    assert repo.enqueue_analysis_job(
        "u1",
        event.event_id,
        job_kind="refine_deterministic",
        goal_id=goal.id,
        cloud_approved=True,
    )
    return service, event


def test_deterministic_l3_job_refines_and_passes_second_opinion_before_publish(tmp_path):
    repo = Repository(tmp_path / "deterministic-l3.db")
    service, event = _deterministic_security_job(repo)
    gateway = RecordingGateway()

    result = asyncio.run(
        AnalysisQueueProcessor(repo, service, gateway).process_event("u1", event.event_id)
    )

    assert result.published == 1
    assert gateway.calls == 1
    assert gateway.second_calls == 1
    assert repo.analysis_model_calls_used(
        "u1", since=datetime.now(timezone.utc) - timedelta(minutes=5)
    ) == 2
    advice = repo.list_advice("u1")
    assert len(advice) == 1
    assert advice[0].status.value == "active"
    assert repo.get_analysis_job("u1", event.event_id).job_kind == "refine_deterministic"
    repo.close()


def test_deterministic_l3_provider_failure_retries_without_visible_advice(tmp_path):
    repo = Repository(tmp_path / "deterministic-retry.db")
    service, event = _deterministic_security_job(repo)
    gateway = RecordingGateway(fail_times=1)

    result = asyncio.run(
        AnalysisQueueProcessor(
            repo,
            service,
            gateway,
            retry_base_seconds=0,
        ).process_event("u1", event.event_id)
    )

    assert result.retried == 1
    assert repo.get_analysis_job("u1", event.event_id).status == "retry"
    assert repo.list_advice("u1") == []
    repo.close()


def test_deterministic_l3_second_opinion_failure_retries_without_visible_advice(tmp_path):
    repo = Repository(tmp_path / "deterministic-review-retry.db")
    service, event = _deterministic_security_job(repo)
    gateway = RecordingGateway(second_fail=True)

    result = asyncio.run(
        AnalysisQueueProcessor(
            repo,
            service,
            gateway,
            retry_base_seconds=0,
        ).process_event("u1", event.event_id)
    )

    assert result.retried == 1
    assert gateway.calls == 1
    assert gateway.second_calls == 1
    assert repo.get_analysis_job("u1", event.event_id).status == "retry"
    assert repo.list_advice("u1") == []
    provisional = repo._connection.execute(
        "SELECT COUNT(*) AS count FROM advice WHERE status='provisional'"
    ).fetchone()
    assert provisional["count"] == 0
    repo.close()


def test_retryable_second_opinion_failure_stops_repeating_primary_at_attempt_limit(
    tmp_path,
):
    repo = Repository(tmp_path / "deterministic-review-attempt-limit.db")
    service, event = _deterministic_security_job(repo)

    class RetryableReviewGateway(RecordingGateway):
        async def second_opinion(self, prompt, context):
            self.second_calls += 1
            raise ModelUnavailable(
                "private review failure",
                category="transient",
                reason_code="claude_code_network_failure",
                retryable=True,
                provider="anthropic_via_claude_code",
            )

    gateway = RetryableReviewGateway()
    processor = AnalysisQueueProcessor(
        repo,
        service,
        gateway,
        retry_base_seconds=0,
        max_attempts=2,
    )

    first = asyncio.run(processor.process_event("u1", event.event_id))
    second = asyncio.run(processor.process_event("u1", event.event_id))
    exhausted = asyncio.run(processor.process_event("u1", event.event_id))

    assert first.retried == 1
    assert second.failed == 1
    assert second.retried == 0
    assert exhausted.attempted == 0
    assert gateway.calls == 2
    assert gateway.second_calls == 2
    assert repo.analysis_model_calls_used(
        "u1", since=datetime.now(timezone.utc) - timedelta(minutes=5)
    ) == 4
    job = repo.get_analysis_job("u1", event.event_id)
    assert job is not None
    assert job.attempts == 2
    assert job.status == "no_intervention"
    assert job.last_reason == "analysis_max_attempts_exhausted"
    assert job.last_failure_reason == "claude_code_network_failure"
    assert repo.list_advice("u1") == []
    repo.close()


def test_deterministic_l3_terminal_second_opinion_failure_does_not_repeat_primary(
    tmp_path,
):
    repo = Repository(tmp_path / "deterministic-terminal-review.db")
    service, event = _deterministic_security_job(repo)

    class TerminalReviewGateway(RecordingGateway):
        async def second_opinion(self, prompt, context):
            self.second_calls += 1
            raise ModelUnavailable(
                "private review failure",
                category="auth",
                reason_code="claude_code_not_logged_in",
                retryable=False,
                provider="anthropic_via_claude_code",
            )

    gateway = TerminalReviewGateway()
    processor = AnalysisQueueProcessor(repo, service, gateway, retry_base_seconds=0)

    first = asyncio.run(processor.process_event("u1", event.event_id))
    second = asyncio.run(processor.process_event("u1", event.event_id))

    assert first.failed == 1
    assert first.retried == 0
    assert second.attempted == 0
    assert gateway.calls == 1
    assert gateway.second_calls == 1
    assert repo.analysis_model_calls_used(
        "u1", since=datetime.now(timezone.utc) - timedelta(minutes=5)
    ) == 2
    job = repo.get_analysis_job("u1", event.event_id)
    assert job is not None
    assert job.status == "no_intervention"
    assert job.last_reason == "claude_code_not_logged_in"
    assert job.last_failure_reason == "claude_code_not_logged_in"
    assert repo.list_advice("u1") == []
    repo.close()


def test_deterministic_l3_review_rejection_is_terminal_and_never_leaks(tmp_path):
    repo = Repository(tmp_path / "deterministic-rejected.db")
    service, event = _deterministic_security_job(repo)
    gateway = RecordingGateway(review_supported=False)

    result = asyncio.run(
        AnalysisQueueProcessor(repo, service, gateway).process_event("u1", event.event_id)
    )

    assert result.no_intervention == 1
    assert gateway.second_calls == 1
    job = repo.get_analysis_job("u1", event.event_id)
    assert job.status == "no_intervention"
    assert job.last_reason == "deterministic_review_rejected"
    assert repo.list_advice("u1") == []
    provisional = repo._connection.execute(
        "SELECT COUNT(*) AS count FROM advice WHERE status='provisional'"
    ).fetchone()
    assert provisional["count"] == 0
    repo.close()


def test_concurrent_processors_claim_once_and_create_one_advice(tmp_path):
    repo = Repository(tmp_path / "concurrent-processor.db")
    _goal(repo)
    event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "I keep comparing the release plan and cannot decide the next step"},
    )
    _queue(repo, event)
    gateway = RecordingGateway(evidence_quote="comparing the release plan")
    processor = AnalysisQueueProcessor(repo, ProactiveService(repo), gateway)

    async def run_both():
        return await asyncio.gather(
            processor.process_event("u1", event.event_id),
            processor.process_event("u1", event.event_id),
        )

    results = asyncio.run(run_both())

    assert sum(item.attempted for item in results) == 1
    assert sum(item.published for item in results) == 1
    assert len(repo.list_advice("u1")) == 1
    repo.close()


def test_database_rejects_two_nonterminal_advice_rows_for_same_evidence(tmp_path):
    path = tmp_path / "evidence-unique.db"
    repo = Repository(path)
    _goal(repo)
    evaluation = ProactiveService(repo).ingest(
        Event(
            user_id="u1",
            source="detector",
            type="time.allocation",
            facts={"domain": "work", "actual_hours": 5, "expected_hours": 10},
        )
    )
    assert evaluation is not None and evaluation.advice is not None
    second_connection = Repository(path)
    duplicate = evaluation.advice.model_copy(
        update={"id": uuid4(), "dedupe_key": "different-model-output"}
    )

    with pytest.raises(AdviceConflict):
        # enforce_publish_limits=False: this exercises the DB-level evidence
        # uniqueness invariant, not the preference frequency gate (which would
        # otherwise trip first on the just-published advice's cooldown).
        second_connection.insert_advice(duplicate, enforce_publish_limits=False)

    assert len(repo.list_advice("u1")) == 1
    second_connection.close()
    repo.close()


def test_unauthorized_job_waits_until_standing_approval_is_persisted(tmp_path):
    repo = Repository(tmp_path / "consumer-approval.db")
    _goal(repo)
    event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "I am comparing the release plan and need a concrete next step"},
    )
    assert repo.insert_event(event)
    assert repo.enqueue_analysis_job("u1", event.event_id)
    gateway = RecordingGateway(evidence_quote="comparing the release plan")

    async def scenario():
        consumer = AnalysisQueueConsumer(
            repo,
            ProactiveService(repo),
            gateway,
            poll_seconds=0.05,
            failure_backoff_seconds=0.05,
        )
        consumer.start()
        consumer.wake()
        await asyncio.sleep(0.15)
        assert repo.get_analysis_job("u1", event.event_id).status == "pending"
        assert gateway.calls == 0
        assert not repo.enqueue_analysis_job(
            "u1",
            event.event_id,
            cloud_approved=True,
        )
        consumer.wake()
        for _ in range(40):
            if repo.get_analysis_job("u1", event.event_id).status == "published":
                break
            await asyncio.sleep(0.025)
        await consumer.stop()

    asyncio.run(scenario())

    job = repo.get_analysis_job("u1", event.event_id)
    assert job.cloud_approved is True
    assert job.status == "published"
    assert len(repo.list_advice("u1")) == 1
    repo.close()


def test_drain_endpoint_is_nonblocking_single_flight_and_limit_is_ten(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "single-flight.db")
    main.service = main.ProactiveService(main.repo)
    main.models = RecordingGateway()
    assert try_acquire_analysis_slot()
    try:
        with TestClient(main.app) as client:
            busy = client.post(
                "/v1/analysis/drain",
                headers={
                    "X-User-Id": "u1",
                    "X-Proactive-Cloud-Approved": "true",
                },
            )
            assert busy.status_code == 429
            too_large = client.post(
                "/v1/analysis/drain?limit=11",
                headers={
                    "X-User-Id": "u1",
                    "X-Proactive-Cloud-Approved": "true",
                },
            )
            assert too_large.status_code == 422
    finally:
        release_analysis_slot()


def test_backfill_is_explicit_dry_run_newest_first_and_hard_bounded(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    import app.main as main

    main.repo = main.Repository(tmp_path / "backfill.db")
    main.service = main.ProactiveService(main.repo)
    main.models = RecordingGateway()
    for index in range(130):
        assert main.repo.insert_event(
            Event(
                user_id="u1",
                source="android.thought",
                type="thought.note",
                facts={"text": f"historical thought {index}"},
            )
        )
    selected = main.repo.analysis_backfill_candidates("u1", limit=500)
    assert len(selected) == 100
    assert selected[0].facts["text"] == "historical thought 129"
    assert main.repo._connection.execute(
        "SELECT COUNT(*) AS count FROM analysis_jobs"
    ).fetchone()["count"] == 0

    with TestClient(main.app) as client:
        dry = client.post("/v1/analysis/backfill?limit=2", headers={"X-User-Id": "u1"})
        assert dry.status_code == 200
        assert dry.json()["dry_run"] is True
        assert dry.json()["enqueued"] == 0
        denied = client.post(
            "/v1/analysis/backfill?dry_run=false&limit=2",
            headers={"X-User-Id": "u1"},
        )
        assert denied.status_code == 409
        queued = client.post(
            "/v1/analysis/backfill?dry_run=false&limit=2",
            headers={
                "X-User-Id": "u1",
                "X-Proactive-Cloud-Approved": "true",
            },
        )
        assert queued.status_code == 200
        assert queued.json()["enqueued"] == 2


def test_raw_cloud_requires_env_and_request_and_audit_remains_redacted(
    tmp_path,
    monkeypatch,
):
    import app.main as main

    event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "Contact owner@example.com about the release decision"},
    )
    monkeypatch.delenv("MOUCHEN_ALLOW_RAW_CLOUD", raising=False)
    assert not main._raw_cloud_approved(True, event)
    monkeypatch.setenv("MOUCHEN_ALLOW_RAW_CLOUD", "true")
    assert not main._raw_cloud_approved(False, event)
    assert main._raw_cloud_approved(True, event)

    repo = Repository(tmp_path / "raw-cloud.db")
    _goal(repo, keyword="release")
    assert repo.insert_event(event)
    assert repo.enqueue_analysis_job(
        "u1",
        event.event_id,
        cloud_approved=True,
        raw_cloud_approved=True,
    )
    gateway = RecordingGateway(intervene=False)
    asyncio.run(
        AnalysisQueueProcessor(repo, ProactiveService(repo), gateway).process_event(
            "u1", event.event_id
        )
    )

    assert "owner@example.com" in gateway.contexts[0]["event_text"]
    audit = repo._connection.execute(
        "SELECT redacted_context_json FROM cloud_slices ORDER BY id DESC LIMIT 1"
    ).fetchone()["redacted_context_json"]
    assert "owner@example.com" not in audit
    assert "[email]" in audit
    repo.close()


def test_restricted_raw_cloud_requires_explicit_raw_consent_scope(tmp_path):
    for scope, should_be_raw in (
        ("alpha.minimized_context", False),
        ("alpha.raw_cloud", True),
        ("owner_full_context", True),
    ):
        repo = Repository(tmp_path / f"restricted-{should_be_raw}.db")
        _goal(repo, keyword="release")
        event = Event(
            user_id="u1",
            source="android.thought",
            type="thought.note",
            facts={"text": "Contact owner@example.com about the release decision"},
            sensitivity=Sensitivity.RESTRICTED,
            consent_scope=scope,
        )
        assert repo.insert_event(event)
        assert repo.enqueue_analysis_job(
            "u1",
            event.event_id,
            cloud_approved=True,
            raw_cloud_approved=True,
        )
        gateway = RecordingGateway(intervene=False)
        asyncio.run(
            AnalysisQueueProcessor(repo, ProactiveService(repo), gateway).process_event(
                "u1", event.event_id
            )
        )
        text_value = gateway.contexts[0]["event_text"]
        assert ("owner@example.com" in text_value) is should_be_raw
        repo.close()


def test_owner_full_context_still_requires_server_and_request_raw_gates(
    tmp_path,
    monkeypatch,
):
    import app.main as main

    event = Event(
        user_id="u1",
        source="android.accessibility",
        type="ui.visible_text",
        facts={
            "visible_text": ["Contact owner@example.com about the release decision"],
            "context": "owner_self_report",
            "analysis_requested": True,
        },
        sensitivity=Sensitivity.RESTRICTED,
        consent_scope="owner_full_context",
    )
    monkeypatch.delenv("MOUCHEN_ALLOW_RAW_CLOUD", raising=False)
    assert not main._raw_cloud_approved(True, event)
    monkeypatch.setenv("MOUCHEN_ALLOW_RAW_CLOUD", "true")
    assert not main._raw_cloud_approved(False, event)
    assert main._raw_cloud_approved(True, event)

    repo = Repository(tmp_path / "owner-full-minimized.db")
    _goal(repo, keyword="release")
    assert repo.insert_event(event)
    assert repo.enqueue_analysis_job(
        "u1",
        event.event_id,
        cloud_approved=True,
        raw_cloud_approved=False,
    )
    gateway = RecordingGateway(intervene=False)
    asyncio.run(
        AnalysisQueueProcessor(repo, ProactiveService(repo), gateway).process_event(
            "u1", event.event_id
        )
    )
    assert gateway.calls == 1
    assert "owner@example.com" not in gateway.contexts[0]["event_text"]
    assert "[email]" in gateway.contexts[0]["event_text"]
    repo.close()


def test_model_call_budget_persists_across_restart_and_defers_without_loss(tmp_path):
    path = tmp_path / "persistent-budget.db"
    repo = Repository(path)
    _goal(repo, keyword="release")
    first = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "I am comparing release path one and need a decision"},
    )
    second = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "I am comparing release path two and need a decision"},
    )
    _queue(repo, first)
    _queue(repo, second)
    first_gateway = RecordingGateway(intervene=False)
    processor = AnalysisQueueProcessor(
        repo,
        ProactiveService(repo),
        first_gateway,
        hourly_model_call_limit=2,
        daily_model_call_limit=4,
    )

    first_result = asyncio.run(processor.process_event("u1", first.event_id))
    assert first_result.no_intervention == 1
    assert first_gateway.calls == 1
    assert repo.analysis_model_calls_used(
        "u1", since=datetime.now(timezone.utc) - timedelta(minutes=5)
    ) == 1
    repo.close()

    restarted = Repository(path)
    second_gateway = RecordingGateway(intervene=False)
    deferred = asyncio.run(
        AnalysisQueueProcessor(
            restarted,
            ProactiveService(restarted),
            second_gateway,
            hourly_model_call_limit=2,
            daily_model_call_limit=4,
        ).process_event("u1", second.event_id)
    )
    assert deferred.attempted == 0
    assert second_gateway.calls == 0
    job = restarted.get_analysis_job("u1", second.event_id)
    assert job is not None
    assert job.status == "pending"
    assert job.last_reason == "analysis_budget_deferred"
    assert job.next_attempt_at > datetime.now(timezone.utc)
    restarted.close()


def test_processing_failure_before_gateway_call_releases_entire_reservation(tmp_path):
    repo = Repository(tmp_path / "zero-call-failure.db")
    _, event = _deterministic_security_job(repo)
    gateway = RecordingGateway()

    class ExplodingService:
        def candidate_for_event(self, *args, **kwargs):
            raise RuntimeError("local processing failure")

    result = asyncio.run(
        AnalysisQueueProcessor(
            repo,
            ExplodingService(),
            gateway,
            retry_base_seconds=0,
            hourly_model_call_limit=2,
            daily_model_call_limit=4,
        ).process_event("u1", event.event_id)
    )

    assert result.retried == 1
    assert gateway.calls == 0
    assert gateway.second_calls == 0
    assert repo.analysis_model_calls_used(
        "u1", since=datetime.now(timezone.utc) - timedelta(minutes=5)
    ) == 0
    reservation = repo._connection.execute(
        """SELECT reserved_calls,actual_calls,settled_at
        FROM analysis_model_reservations"""
    ).fetchone()
    assert reservation["reserved_calls"] == 2
    assert reservation["actual_calls"] == 0
    assert reservation["settled_at"] is not None
    repo.close()


def test_budget_deferred_job_survives_stale_cutoff_and_clears_reason_on_claim(tmp_path):
    repo = Repository(tmp_path / "deferred-stale.db")
    now = datetime.now(timezone.utc)
    event = Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={"text": "Routine background status snapshot"},
        occurred_at=now - timedelta(hours=7),
    )
    _queue(repo, event)
    repo._connection.execute(
        """UPDATE analysis_jobs
        SET last_reason='analysis_budget_deferred', next_attempt_at=?, updated_at=?
        WHERE user_id=? AND event_id=?""",
        (
            (now - timedelta(seconds=1)).isoformat(),
            now.isoformat(),
            "u1",
            str(event.event_id),
        ),
    )
    repo._connection.commit()

    assert repo.retire_stale_analysis_jobs("u1", now=now) == 0
    assert repo.get_analysis_job("u1", event.event_id).status == "pending"
    claim = repo.claim_analysis_job(
        "u1",
        event.event_id,
        now=now,
        hourly_call_limit=12,
        daily_call_limit=288,
    )
    assert claim is not None
    assert claim.job.status == "running"
    assert claim.job.last_reason is None
    assert repo.complete_analysis_job(
        "u1",
        event.event_id,
        "no_intervention",
        "test_complete",
        attempt=claim.job.attempts,
        now=now,
    )
    assert claim.reservation_id is not None
    assert repo.settle_analysis_model_reservation(
        claim.reservation_id,
        "u1",
        event.event_id,
        attempt=claim.job.attempts,
        now=now,
    )
    repo.close()


def test_legacy_model_usage_still_defers_new_worst_case_reservation(tmp_path):
    repo = Repository(tmp_path / "legacy-budget.db")
    now = datetime.now(timezone.utc)
    event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "How should I handle this release choice?"},
        occurred_at=now,
    )
    _queue(repo, event)
    repo._connection.execute(
        """INSERT INTO analysis_model_usage(user_id,calls,used_at)
        VALUES(?,?,?)""",
        ("u1", 2, now.isoformat()),
    )
    repo._connection.commit()
    claim_time = now + timedelta(seconds=1)

    assert repo.claim_analysis_job(
        "u1",
        event.event_id,
        now=claim_time,
        hourly_call_limit=2,
        daily_call_limit=4,
    ) is None
    job = repo.get_analysis_job("u1", event.event_id)
    assert job is not None
    assert job.status == "pending"
    assert job.last_reason == "analysis_budget_deferred"
    assert repo.analysis_model_calls_used(
        "u1", since=now - timedelta(minutes=1)
    ) == 2
    repo.close()


def test_expired_worker_releases_unused_reservation_before_next_claim(tmp_path):
    repo = Repository(tmp_path / "expired-reservation.db")
    now = datetime.now(timezone.utc)
    first = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "How should I handle release path one?"},
        occurred_at=now,
    )
    second = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "How should I handle release path two?"},
        occurred_at=now,
    )
    _queue(repo, first)
    _queue(repo, second)
    claim_time = now + timedelta(seconds=1)
    abandoned = repo.claim_analysis_job(
        "u1",
        first.event_id,
        now=claim_time,
        hourly_call_limit=2,
        daily_call_limit=4,
    )
    assert abandoned is not None
    assert abandoned.reservation_id is not None
    assert repo.analysis_model_calls_used("u1", since=now - timedelta(minutes=1)) == 2

    reclaimed_at = claim_time + timedelta(minutes=11)
    next_claim = repo.claim_analysis_job(
        "u1",
        second.event_id,
        now=reclaimed_at,
        hourly_call_limit=2,
        daily_call_limit=4,
    )
    assert next_claim is not None
    abandoned_reservation = repo._connection.execute(
        """SELECT actual_calls,settled_at FROM analysis_model_reservations
        WHERE id=?""",
        (abandoned.reservation_id,),
    ).fetchone()
    assert abandoned_reservation["actual_calls"] == 0
    assert abandoned_reservation["settled_at"] is not None
    assert next_claim.reservation_id is not None
    assert repo.settle_analysis_model_reservation(
        next_claim.reservation_id,
        "u1",
        second.event_id,
        attempt=next_claim.job.attempts,
        now=reclaimed_at,
    )
    repo.close()


def test_queue_prioritizes_urgent_then_latest_explicit_user_question(tmp_path):
    repo = Repository(tmp_path / "priority.db")
    now = datetime.now(timezone.utc)
    ordinary = Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={"text": "A routine status update"},
        occurred_at=now,
    )
    older_question = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "How should I handle this release choice?"},
        occurred_at=now - timedelta(minutes=2),
    )
    latest_question = Event(
        user_id="u1",
        source="android.speech",
        type="speech.transcript",
        facts={"transcript": "Can you help me decide the next step?"},
        occurred_at=now - timedelta(minutes=1),
    )
    urgent = Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={"text": "Urgent: suspicious login detected"},
        occurred_at=now - timedelta(minutes=3),
    )
    for event in (ordinary, older_question, latest_question, urgent):
        _queue(repo, event)

    assert repo.get_analysis_job("u1", ordinary.event_id).priority == 0
    assert repo.get_analysis_job("u1", older_question.event_id).priority == 80
    assert repo.get_analysis_job("u1", latest_question.event_id).priority == 80
    assert repo.get_analysis_job("u1", urgent.event_id).priority == 100

    claim_time = now + timedelta(seconds=1)
    first = repo.claim_analysis_job("u1", now=claim_time)
    assert first is not None and first.event.event_id == urgent.event_id
    assert repo.complete_analysis_job(
        "u1",
        urgent.event_id,
        "no_intervention",
        "test_complete",
        attempt=first.job.attempts,
        now=claim_time,
    )
    second = repo.claim_analysis_job("u1", now=claim_time)
    assert second is not None and second.event.event_id == latest_question.event_id
    repo.close()


def test_passive_speech_and_screen_samples_cannot_flood_high_priority_queue(tmp_path):
    repo = Repository(tmp_path / "passive-priority.db")
    now = datetime.now(timezone.utc)
    passive_events = []
    for index in range(20):
        speech = Event(
            user_id="u1",
            source="android.microphone.transcript",
            type="speech.transcript",
            facts={
                "transcript": f"Passive meeting fragment {index}",
                "context": "audio_transcript",
                "analysis_requested": True,
            },
            occurred_at=now + timedelta(milliseconds=index),
        )
        screen = Event(
            user_id="u1",
            source="android.screen_ocr",
            type="ui.visible_text",
            facts={
                "visible_text": f"Passive screen fragment {index}",
                "context": "screen_ocr",
                "analysis_requested": True,
            },
            occurred_at=now + timedelta(milliseconds=100 + index),
        )
        _queue(repo, speech)
        _queue(repo, screen)
        passive_events.extend((speech, screen))

    resolved_screen = Event(
        user_id="u1",
        source="android.screen_ocr",
        type="ui.visible_text",
        facts={
            "visible_text": "\u95ee\u9898\u5df2\u89e3\u51b3\uff0c\u7cfb\u7edf\u5efa\u8bae\u7a0d\u540e\u67e5\u770b",
            "context": "screen_ocr",
            "analysis_requested": True,
        },
        occurred_at=now + timedelta(milliseconds=200),
    )
    _queue(repo, resolved_screen)
    passive_events.append(resolved_screen)

    explicit_owner_entry = Event(
        user_id="u1",
        source="android.share",
        type="ui.visible_text",
        facts={
            "visible_text": "A user-selected image for review",
            "context": "shared_image",
            "analysis_requested": True,
        },
        occurred_at=now - timedelta(minutes=1),
    )
    _queue(repo, explicit_owner_entry)

    assert all(
        repo.get_analysis_job("u1", event.event_id).priority == 0
        for event in passive_events
    )
    assert repo.get_analysis_job("u1", explicit_owner_entry.event_id).priority == 80
    repo.close()


def test_startup_reclassifies_preexisting_passive_requested_job_to_zero(tmp_path):
    path = tmp_path / "priority-reclassification.db"
    repo = Repository(path)
    passive = Event(
        user_id="u1",
        source="android.microphone.transcript",
        type="speech.transcript",
        facts={
            "transcript": "A neutral passive transcript",
            "context": "audio_transcript",
            "analysis_requested": True,
        },
    )
    _queue(repo, passive)
    repo._connection.execute(
        "UPDATE analysis_jobs SET priority=80 WHERE event_id=?",
        (str(passive.event_id),),
    )
    repo._connection.commit()
    repo.close()

    restarted = Repository(path)
    assert restarted.get_analysis_job("u1", passive.event_id).priority == 0
    restarted.close()


def test_stale_discover_jobs_retire_without_deleting_source_events(tmp_path):
    repo = Repository(tmp_path / "retirement.db")
    goal = _goal(repo)
    now = datetime.now(timezone.utc)
    stale_ordinary = Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={"text": "Routine historical status"},
        occurred_at=now - timedelta(hours=7),
    )
    retained_question = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "How should I resolve this issue?"},
        occurred_at=now - timedelta(hours=7),
    )
    stale_question = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "Can you help me resolve this old issue?"},
        occurred_at=now - timedelta(hours=25),
    )
    retained_review = Event(
        user_id="u1",
        source="detector",
        type="notification.posted",
        facts={"text": "Urgent warning awaiting mandatory review"},
        occurred_at=now - timedelta(days=2),
    )
    for event in (
        stale_ordinary,
        retained_question,
        stale_question,
        retained_review,
    ):
        assert repo.insert_event(event)
        assert repo.enqueue_analysis_job(
            "u1",
            event.event_id,
            job_kind=(
                "refine_deterministic"
                if event.event_id == retained_review.event_id
                else "discover"
            ),
            goal_id=(goal.id if event.event_id == retained_review.event_id else None),
            cloud_approved=True,
        )

    assert repo.retire_stale_analysis_jobs("u1", now=now) == 2
    for event in (stale_ordinary, stale_question):
        job = repo.get_analysis_job("u1", event.event_id)
        assert job is not None
        assert job.status == "no_intervention"
        assert job.last_reason == "stale_discover_retired"
        assert repo.get_event("u1", event.event_id) is not None
    assert repo.get_analysis_job("u1", retained_question.event_id).status == "pending"
    assert repo.get_analysis_job("u1", retained_review.event_id).status == "pending"
    repo.close()


def test_background_consumer_enforces_persistent_hourly_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_MODEL_CALLS_PER_HOUR", "2")
    monkeypatch.setenv("MOUCHEN_ANALYSIS_MODEL_CALLS_PER_DAY", "4")
    repo = Repository(tmp_path / "consumer-budget.db")
    _goal(repo, keyword="release")
    events = []
    for index in range(3):
        event = Event(
            user_id="u1",
            source="android.thought",
            type="thought.note",
            facts={"text": f"I am comparing release path {index} and need a decision"},
        )
        _queue(repo, event)
        events.append(event)
    gateway = RecordingGateway(intervene=False)

    async def scenario():
        consumer = AnalysisQueueConsumer(
            repo,
            ProactiveService(repo),
            gateway,
            poll_seconds=0.02,
            failure_backoff_seconds=0.02,
        )
        consumer.start()
        consumer.wake()
        for _ in range(100):
            deferred = repo._connection.execute(
                """SELECT COUNT(*) AS count FROM analysis_jobs
                WHERE last_reason='analysis_budget_deferred'"""
            ).fetchone()["count"]
            if gateway.calls == 1 and deferred >= 1:
                break
            await asyncio.sleep(0.02)
        await consumer.stop()

    asyncio.run(scenario())

    assert gateway.calls == 1
    assert repo.analysis_model_calls_used(
        "u1", since=datetime.now(timezone.utc) - timedelta(minutes=5)
    ) == 1
    jobs = [repo.get_analysis_job("u1", event.event_id) for event in events]
    assert sum(job.status == "no_intervention" for job in jobs if job is not None) == 1
    assert sum(job.status == "pending" for job in jobs if job is not None) == 2
    assert all(
        job.last_reason == "analysis_budget_deferred"
        for job in jobs
        if job is not None and job.status == "pending"
    )
    repo.close()


def test_background_consumer_rechecks_persisted_budget_deferral_on_start(tmp_path):
    repo = Repository(tmp_path / "consumer-budget-recheck.db")
    event = Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={"text": "Routine background status snapshot"},
    )
    _queue(repo, event)
    future = datetime.now(timezone.utc) + timedelta(hours=12)
    repo._connection.execute(
        """UPDATE analysis_jobs
        SET last_reason='analysis_budget_deferred', next_attempt_at=?
        WHERE user_id=? AND event_id=?""",
        (future.isoformat(), "u1", str(event.event_id)),
    )
    repo._connection.commit()
    gateway = RecordingGateway()

    async def scenario():
        consumer = AnalysisQueueConsumer(
            repo,
            ProactiveService(repo),
            gateway,
            poll_seconds=0.02,
            failure_backoff_seconds=0.02,
        )
        consumer.start()
        for _ in range(100):
            job = repo.get_analysis_job("u1", event.event_id)
            if job is not None and job.status == "no_intervention":
                break
            await asyncio.sleep(0.02)
        await consumer.stop()

    asyncio.run(scenario())

    job = repo.get_analysis_job("u1", event.event_id)
    assert job is not None
    assert job.status == "no_intervention"
    assert job.last_reason == "no_semantic_review_cue"
    assert gateway.calls == 0
    assert repo.analysis_model_calls_used(
        "u1", since=datetime.now(timezone.utc) - timedelta(minutes=5)
    ) == 0
    repo.close()


def test_old_analysis_schema_migrates_without_requeueing_running_job(tmp_path):
    path = tmp_path / "old-schema.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE events(
          id TEXT PRIMARY KEY,user_id TEXT NOT NULL,source TEXT NOT NULL,type TEXT NOT NULL,
          occurred_at TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE TABLE advice(
          id TEXT PRIMARY KEY,user_id TEXT NOT NULL,domain TEXT NOT NULL,level INTEGER NOT NULL,
          dedupe_key TEXT NOT NULL,status TEXT NOT NULL,delivery TEXT NOT NULL,
          prediction_confidence REAL NOT NULL,prediction_deadline TEXT NOT NULL,
          payload_json TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE TABLE analysis_jobs(
          event_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,status TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,next_attempt_at TEXT NOT NULL,last_reason TEXT,
          created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT
        );
        """
    )
    now = datetime.now(timezone.utc)
    queued_event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "How should I handle this migration issue?"},
        occurred_at=now,
        created_at=now,
    )
    connection.execute(
        """INSERT INTO events(id,user_id,source,type,occurred_at,payload_json,created_at)
        VALUES(?,?,?,?,?,?,?)""",
        (
            str(queued_event.event_id),
            queued_event.user_id,
            queued_event.source,
            queued_event.type,
            queued_event.occurred_at.isoformat(),
            queued_event.model_dump_json(),
            queued_event.created_at.isoformat(),
        ),
    )
    connection.execute(
        """INSERT INTO analysis_jobs(
          event_id,user_id,status,attempts,next_attempt_at,last_reason,
          created_at,updated_at,completed_at
        ) VALUES(?,?,'pending',0,?,NULL,?,?,NULL)""",
        (str(queued_event.event_id), "u1", now.isoformat(), now.isoformat(), now.isoformat()),
    )
    connection.commit()
    connection.close()

    repo = Repository(path)
    analysis_columns = {
        row["name"] for row in repo._connection.execute("PRAGMA table_info(analysis_jobs)")
    }
    advice_columns = {
        row["name"] for row in repo._connection.execute("PRAGMA table_info(advice)")
    }
    reservation_columns = {
        row["name"]
        for row in repo._connection.execute(
            "PRAGMA table_info(analysis_model_reservations)"
        )
    }
    assert {
        "job_kind",
        "goal_id",
        "cloud_approved",
        "raw_cloud_approved",
        "priority",
        "last_failure_reason",
    }.issubset(analysis_columns)
    assert repo.get_analysis_job("u1", queued_event.event_id).priority == 80
    assert "evidence_ref" in advice_columns
    assert {
        "id",
        "user_id",
        "event_id",
        "attempt",
        "reserved_calls",
        "actual_calls",
        "created_at",
        "settled_at",
    } == reservation_columns
    repo.close()


def test_retryable_model_failure_stops_at_default_fifth_attempt(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("MOUCHEN_ANALYSIS_MAX_ATTEMPTS", raising=False)
    repo = Repository(tmp_path / "default-attempt-limit.db")
    _goal(repo, keyword="release")
    event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "The release decision is blocked by a material risk"},
    )
    _queue(repo, event)
    gateway = UnavailableGateway(
        reason_code="openai_transport_unavailable",
        retryable=True,
    )
    processor = AnalysisQueueProcessor(
        repo,
        ProactiveService(repo),
        gateway,
        retry_base_seconds=0,
    )

    results = [
        asyncio.run(processor.process_event("u1", event.event_id))
        for _ in range(5)
    ]
    exhausted = asyncio.run(processor.process_event("u1", event.event_id))

    assert [result.retried for result in results] == [1, 1, 1, 1, 0]
    assert [result.failed for result in results] == [0, 0, 0, 0, 1]
    assert exhausted.attempted == 0
    assert gateway.calls == 5
    job = repo.get_analysis_job("u1", event.event_id)
    assert job is not None
    assert job.attempts == 5
    assert job.status == "no_intervention"
    assert job.last_reason == "analysis_max_attempts_exhausted"
    assert job.last_failure_reason == "openai_transport_unavailable"
    repo.close()


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("3", 3),
        ("invalid", 5),
        ("0", 1),
        ("999", 20),
    ],
)
def test_analysis_max_attempts_environment_is_validated(
    tmp_path,
    monkeypatch,
    configured,
    expected,
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_MAX_ATTEMPTS", configured)
    repo = Repository(tmp_path / f"attempt-limit-{configured}.db")

    processor = AnalysisQueueProcessor(
        repo,
        ProactiveService(repo),
        RecordingGateway(),
    )

    assert processor.max_attempts == expected
    repo.close()


@pytest.mark.parametrize(("explicit", "expected"), [(7, 7), (0, 1), (99, 20)])
def test_explicit_analysis_max_attempts_overrides_environment(
    tmp_path,
    monkeypatch,
    explicit,
    expected,
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_MAX_ATTEMPTS", "3")
    repo = Repository(tmp_path / f"explicit-attempt-limit-{explicit}.db")

    processor = AnalysisQueueProcessor(
        repo,
        ProactiveService(repo),
        RecordingGateway(),
        max_attempts=explicit,
    )

    assert processor.max_attempts == expected
    repo.close()


def test_deterministic_refinement_failure_stops_at_attempt_limit(tmp_path):
    repo = Repository(tmp_path / "deterministic-attempt-limit.db")
    service, event = _deterministic_security_job(repo)
    gateway = RecordingGateway(fail_times=10)
    processor = AnalysisQueueProcessor(
        repo,
        service,
        gateway,
        retry_base_seconds=0,
        max_attempts=2,
    )

    first = asyncio.run(processor.process_event("u1", event.event_id))
    second = asyncio.run(processor.process_event("u1", event.event_id))
    exhausted = asyncio.run(processor.process_event("u1", event.event_id))

    assert first.retried == 1
    assert second.failed == 1
    assert second.retried == 0
    assert exhausted.attempted == 0
    assert gateway.calls == 2
    assert repo.list_advice("u1") == []
    job = repo.get_analysis_job("u1", event.event_id)
    assert job is not None
    assert job.status == "no_intervention"
    assert job.last_reason == "analysis_max_attempts_exhausted"
    assert job.last_failure_reason == "deterministic_refinement_unavailable"
    repo.close()


def test_processing_exception_at_attempt_limit_is_reported_failed(tmp_path):
    repo = Repository(tmp_path / "processing-attempt-limit.db")
    _, event = _deterministic_security_job(repo)

    class ExplodingService:
        def candidate_for_event(self, *args, **kwargs):
            raise RuntimeError("local processing failure")

    result = asyncio.run(
        AnalysisQueueProcessor(
            repo,
            ExplodingService(),
            RecordingGateway(),
            retry_base_seconds=0,
            max_attempts=1,
        ).process_event("u1", event.event_id)
    )

    assert result.failed == 1
    assert result.retried == 0
    job = repo.get_analysis_job("u1", event.event_id)
    assert job is not None
    assert job.status == "no_intervention"
    assert job.last_reason == "analysis_max_attempts_exhausted"
    assert job.last_failure_reason == "analysis_processing_unavailable"
    repo.close()


def test_cancelled_analysis_retries_then_stops_at_attempt_limit(tmp_path):
    repo = Repository(tmp_path / "cancelled-attempt-limit.db")
    _goal(repo, keyword="release")
    event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "How should I resolve the blocked release decision?"},
    )
    _queue(repo, event)

    class CancelledGateway:
        async def generate(self, *args, **kwargs):
            raise asyncio.CancelledError

    processor = AnalysisQueueProcessor(
        repo,
        ProactiveService(repo),
        CancelledGateway(),
        retry_base_seconds=0,
        max_attempts=2,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(processor.process_event("u1", event.event_id))
    retrying = repo.get_analysis_job("u1", event.event_id)
    assert retrying is not None
    assert retrying.status == "retry"
    assert retrying.last_failure_reason == "analysis_cancelled"

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(processor.process_event("u1", event.event_id))
    terminal = repo.get_analysis_job("u1", event.event_id)
    assert terminal is not None
    assert terminal.status == "no_intervention"
    assert terminal.last_reason == "analysis_max_attempts_exhausted"
    assert terminal.last_failure_reason == "analysis_cancelled"
    assert asyncio.run(
        processor.process_event("u1", event.event_id)
    ).attempted == 0
    repo.close()


def test_expired_worker_retries_below_attempt_limit_and_releases_reservation(tmp_path):
    repo = Repository(tmp_path / "expired-worker-below-limit.db")
    _goal(repo, keyword="release")
    event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "How should I resolve release path one?"},
    )
    _queue(repo, event)
    claimed_at = datetime.now(timezone.utc)
    abandoned = repo.claim_analysis_job(
        "u1",
        event.event_id,
        now=claimed_at,
        hourly_call_limit=2,
        daily_call_limit=4,
        max_attempts=2,
    )
    assert abandoned is not None
    assert abandoned.reservation_id is not None

    reclaimed_at = claimed_at + timedelta(minutes=11)
    reclaimed = repo.claim_analysis_job(
        "u1",
        event.event_id,
        now=reclaimed_at,
        hourly_call_limit=2,
        daily_call_limit=4,
        max_attempts=2,
    )

    assert reclaimed is not None
    assert reclaimed.job.attempts == 2
    assert reclaimed.job.last_failure_reason == "worker_lease_expired"
    abandoned_reservation = repo._connection.execute(
        "SELECT actual_calls,settled_at FROM analysis_model_reservations WHERE id=?",
        (abandoned.reservation_id,),
    ).fetchone()
    assert abandoned_reservation["actual_calls"] == 0
    assert abandoned_reservation["settled_at"] is not None
    repo.close()


def test_expired_worker_at_attempt_limit_is_terminal_and_releases_reservation(tmp_path):
    repo = Repository(tmp_path / "expired-worker-at-limit.db")
    _goal(repo, keyword="release")
    event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "How should I resolve release path two?"},
    )
    _queue(repo, event)
    claimed_at = datetime.now(timezone.utc)
    abandoned = repo.claim_analysis_job(
        "u1",
        event.event_id,
        now=claimed_at,
        hourly_call_limit=2,
        daily_call_limit=4,
        max_attempts=1,
    )
    assert abandoned is not None
    assert abandoned.reservation_id is not None

    reclaimed = repo.claim_analysis_job(
        "u1",
        event.event_id,
        now=claimed_at + timedelta(minutes=11),
        hourly_call_limit=2,
        daily_call_limit=4,
        max_attempts=1,
    )

    assert reclaimed is None
    job = repo.get_analysis_job("u1", event.event_id)
    assert job is not None
    assert job.status == "no_intervention"
    assert job.last_reason == "analysis_max_attempts_exhausted"
    assert job.last_failure_reason == "worker_lease_expired"
    reservation = repo._connection.execute(
        "SELECT actual_calls,settled_at FROM analysis_model_reservations WHERE id=?",
        (abandoned.reservation_id,),
    ).fetchone()
    assert reservation["actual_calls"] == 0
    assert reservation["settled_at"] is not None
    repo.close()


def test_attempt_guard_preserves_existing_failure_reason(tmp_path):
    repo = Repository(tmp_path / "attempt-guard-history.db")
    _goal(repo, keyword="release")
    event = Event(
        user_id="u1",
        source="android.thought",
        type="thought.note",
        facts={"text": "How should I resolve the release risk?"},
    )
    _queue(repo, event)
    claim = repo.claim_analysis_job("u1", event.event_id)
    assert claim is not None
    assert repo.retry_analysis_job(
        "u1",
        event.event_id,
        "original_provider_failure",
        attempt=claim.job.attempts,
        delay_seconds=0,
    )

    assert repo.claim_analysis_job(
        "u1",
        event.event_id,
        max_attempts=1,
    ) is None
    job = repo.get_analysis_job("u1", event.event_id)
    assert job is not None
    assert job.status == "no_intervention"
    assert job.last_reason == "analysis_max_attempts_exhausted"
    assert job.last_failure_reason == "original_provider_failure"
    repo.close()
