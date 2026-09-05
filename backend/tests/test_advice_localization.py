from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.advice_localization import (
    AdviceLocalizationConsumer,
    AdviceLocalizationProcessor,
)
from app.domain.models import AdviceLevel, AdviceRecord, Event, Goal, utc_now
from app.localization import advice_display_source_hash
from app.model_gateway import ModelRoute, ModelUnavailable
from app.storage import AccountRetired, Repository


def _advice(
    repo: Repository,
    user_id: str,
    *,
    advice_id: UUID | None = None,
    delivery: str = "immediate",
) -> AdviceRecord:
    repo.ensure_user(user_id)
    goal = repo.insert_goal(
        Goal(
            user_id=user_id,
            domain="work",
            title="Owner supplied title",
            quote=f"GOAL-QUOTE-PRIVATE-{user_id}",
        )
    )
    now = utc_now()
    return repo.insert_advice(
        AdviceRecord(
            id=advice_id or uuid4(),
            user_id=user_id,
            domain="work",
            requested_level=AdviceLevel.L2,
            goal_id=goal.id,
            goal_quote=goal.quote,
            evidence=[
                {
                    "event_id": uuid4(),
                    "source": "test.localization",
                    "fact": f"EVIDENCE-PRIVATE-{user_id}",
                    "observed_at": now,
                    "confidence": 0.95,
                }
            ],
            action="检查发布阻塞条件并确定负责人",
            first_step="打开发布日志并记录第一项失败检查",
            alternative="如果证据不足，先向负责人确认失败检查",
            prediction={
                "outcome": "如果不处理，下次复核时发布仍会受阻",
                "deadline": now + timedelta(days=1),
                "confidence": 0.84,
            },
            adopted_expected_result="阻塞条件被记录并选定恢复路径",
            adopted_confidence=0.82,
            urgency=0.8,
            impact=0.8,
            novelty=0.8,
            relevance=0.9,
            context_fit=1.0,
            interruption_cost=0.1,
            dedupe_key=f"localization:{user_id}:{uuid4()}",
            issue_subject=f"release blocker {uuid4()}",
            effective_level=AdviceLevel.L2,
            proactive_score=0.85,
            delivery=delivery,
        )
    )


def _translation(prefix: str = "") -> dict[str, str]:
    marker = f"{prefix} " if prefix else ""
    return {
        "action": f"{marker}Check the release blocker and assign an owner",
        "first_step": f"{marker}Open the release log and record the first failed check",
        "alternative": f"{marker}Ask the owner to confirm the failed check if evidence is incomplete",
        "prediction_outcome": f"{marker}The release remains blocked at the next review without action",
        "adopted_expected_result": f"{marker}The blocker is recorded and a recovery path is selected",
    }


def _raw_payload_hash(repo: Repository, user_id: str, advice_id: UUID) -> str:
    row = repo._connection.execute(
        "SELECT payload_json FROM advice WHERE user_id=? AND id=?",
        (user_id, str(advice_id)),
    ).fetchone()
    assert row is not None
    return hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest()


def _ready_translation(
    repo: Repository,
    user_id: str,
    advice: AdviceRecord,
    translated: dict[str, str] | None = None,
) -> None:
    repo.set_account_locale(user_id, "en-US")
    assert repo.enqueue_advice_localizations(user_id, "en-US") == 1
    claim = repo.claim_next_advice_localization()
    assert claim is not None
    assert claim.user_id == user_id
    assert claim.advice_id == advice.id
    assert repo.complete_advice_localization(
        claim,
        translated or _translation(),
        provider="test-provider",
        model="test-model",
    )


def test_localization_cache_is_tenant_isolated_and_never_mutates_source(tmp_path):
    repo = Repository(tmp_path / "tenant-localizations.db")
    shared_id = uuid4()
    alice = _advice(repo, "alice", advice_id=shared_id)
    bob = _advice(repo, "bob", advice_id=shared_id)
    alice_payload_hash = _raw_payload_hash(repo, "alice", shared_id)
    bob_payload_hash = _raw_payload_hash(repo, "bob", shared_id)

    for user_id in ("alice", "bob"):
        repo.set_account_locale(user_id, "en-US")
        assert repo.enqueue_advice_localizations(user_id, "en-US") == 1
    claims = [repo.claim_next_advice_localization(), repo.claim_next_advice_localization()]
    by_user = {claim.user_id: claim for claim in claims if claim is not None}
    assert set(by_user) == {"alice", "bob"}
    assert repo.complete_advice_localization(
        by_user["alice"], _translation("Alice"), provider="p", model="m"
    )
    assert repo.complete_advice_localization(
        by_user["bob"], _translation("Bob"), provider="p", model="m"
    )

    assert repo.advice_display_envelope("alice", alice, "en-US")["display"][
        "action"
    ].startswith("Alice ")
    assert repo.advice_display_envelope("bob", bob, "en-US")["display"][
        "action"
    ].startswith("Bob ")
    assert _raw_payload_hash(repo, "alice", shared_id) == alice_payload_hash
    assert _raw_payload_hash(repo, "bob", shared_id) == bob_payload_hash
    repo.close()


def test_source_hash_change_invalidates_ready_translation(tmp_path):
    repo = Repository(tmp_path / "source-hash.db")
    advice = _advice(repo, "u1")
    original_payload_hash = _raw_payload_hash(repo, "u1", advice.id)
    original_source_hash = advice_display_source_hash(advice)
    _ready_translation(repo, "u1", advice)
    assert _raw_payload_hash(repo, "u1", advice.id) == original_payload_hash

    changed = advice.model_copy(update={"action": "重新检查发布阻塞条件"})
    repo.update_advice(changed)
    assert advice_display_source_hash(changed) != original_source_hash
    assert repo.enqueue_advice_localizations("u1", "en-US") == 0
    row = repo._connection.execute(
        """SELECT source_hash,status,attempts,translated_json
        FROM advice_localizations WHERE user_id=? AND advice_id=? AND locale=?""",
        ("u1", str(advice.id), "en-US"),
    ).fetchone()
    assert tuple(row) == (advice_display_source_hash(changed), "pending", 0, None)
    assert repo.advice_display_envelope("u1", changed, "en-US") == {
        "display_locale": "en-US",
        "display_translation_status": "pending",
        "display": None,
    }
    repo.close()


def test_worker_sends_only_allowlisted_prose_and_uses_privacy_audit_and_budget(
    tmp_path, monkeypatch
):
    import app.advice_localization as localization_worker
    from app.model_gateway import ModelGateway

    class CapturingGateway:
        supports_pre_reserved_budget = True

        def __init__(self):
            self.before_privacy = None
            self.outbound = None
            self.reservations = []

        def prepare_cloud_payload(self, prompt, context, *, allow_raw=False):
            assert allow_raw is False
            self.before_privacy = (prompt, context)
            return ModelGateway.prepare_cloud_payload(
                prompt, context, allow_raw=allow_raw
            )

        def reserve_paid_call(self, provider, user_id, purpose):
            self.reservations.append((provider, user_id, purpose))
            return True

        async def generate(self, route, prompt, context, **kwargs):
            self.outbound = (prompt, context, kwargs)
            return json.dumps(_translation())

    monkeypatch.setattr(
        localization_worker,
        "choose_route",
        lambda *_args, **_kwargs: ModelRoute("openai", "translation-test", "routine"),
    )
    repo = Repository(tmp_path / "privacy-localization.db")
    advice = _advice(repo, "u1")
    repo.set_account_locale("u1", "en-US")
    repo.enqueue_advice_localizations("u1", "en-US")
    gateway = CapturingGateway()

    result = asyncio.run(
        AdviceLocalizationProcessor(repo, gateway, retry_base_seconds=0).drain()
    )

    assert result.ready == 1
    assert gateway.reservations == [("openai", "u1", "advice_translation")]
    before_prompt, before_context = gateway.before_privacy
    assert set(before_context) == {
        "response_locale",
        "action",
        "first_step",
        "alternative",
        "prediction_outcome",
        "adopted_expected_result",
    }
    serialized_before = json.dumps(
        {"prompt": before_prompt, "context": before_context}, ensure_ascii=False
    )
    serialized_outbound = json.dumps(gateway.outbound[:2], ensure_ascii=False)
    audit = repo._connection.execute(
        """SELECT redacted_context_json FROM cloud_slices
        WHERE user_id=? AND purpose='advice_translation'""",
        ("u1",),
    ).fetchone()["redacted_context_json"]
    for forbidden in (advice.goal_quote, advice.evidence[0].fact):
        assert forbidden not in serialized_before
        assert forbidden not in serialized_outbound
        assert forbidden not in audit
    repo.close()


def test_budget_failures_back_off_and_become_unavailable_at_attempt_limit(
    tmp_path, monkeypatch
):
    import app.advice_localization as localization_worker

    class BudgetGateway:
        supports_pre_reserved_budget = True

        @staticmethod
        def prepare_cloud_payload(prompt, context, *, allow_raw=False):
            return prompt, context

        @staticmethod
        def reserve_paid_call(provider, user_id, purpose):
            raise ModelUnavailable(
                "budget exhausted",
                category="rate_limit",
                reason_code="global_model_budget_exhausted",
                retryable=True,
                retry_after_seconds=60,
            )

        async def generate(self, *args, **kwargs):  # pragma: no cover - fail closed
            raise AssertionError("generation must not run without a budget reservation")

    monkeypatch.setattr(
        localization_worker,
        "choose_route",
        lambda *_args, **_kwargs: ModelRoute("openai", "translation-test", "routine"),
    )
    repo = Repository(tmp_path / "budget-localization.db")
    advice = _advice(repo, "u1")
    repo.set_account_locale("u1", "en-US")
    repo.enqueue_advice_localizations("u1", "en-US")
    processor = AdviceLocalizationProcessor(
        repo, BudgetGateway(), retry_base_seconds=0, max_attempts=2
    )

    first = asyncio.run(processor.drain())
    assert first.retried == 1
    row = repo._connection.execute(
        "SELECT status,attempts,next_attempt_at,last_error FROM advice_localizations"
    ).fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["last_error"] == "global_model_budget_exhausted"
    assert row["next_attempt_at"] > utc_now().isoformat()

    repo._connection.execute(
        "UPDATE advice_localizations SET next_attempt_at=?",
        ((utc_now() - timedelta(seconds=1)).isoformat(),),
    )
    repo._connection.commit()
    second = asyncio.run(processor.drain())
    assert second.unavailable == 1
    row = repo._connection.execute(
        "SELECT status,attempts,last_error FROM advice_localizations"
    ).fetchone()
    assert tuple(row) == ("unavailable", 2, "global_model_budget_exhausted")
    assert repo.advice_display_envelope("u1", advice, "en-US")[
        "display_translation_status"
    ] == "unavailable"
    repo.close()


def test_restart_recovers_expired_lease_and_startup_backfills_english_accounts(
    tmp_path, monkeypatch
):
    database = tmp_path / "restart-localization.db"
    repo = Repository(database)
    advice = _advice(repo, "u1")
    repo.set_account_locale("u1", "en-US")
    assert repo.enqueue_advice_localizations("u1", "en-US") == 1
    started = utc_now()
    first_claim = repo.claim_next_advice_localization(now=started)
    assert first_claim is not None and first_claim.attempts == 1
    repo.close()

    reopened = Repository(database)
    recovered = reopened.claim_next_advice_localization(
        now=started + timedelta(minutes=11)
    )
    assert recovered is not None
    assert recovered.user_id == "u1"
    assert recovered.advice_id == advice.id
    assert recovered.attempts == 2
    reopened.fail_advice_localization(
        recovered,
        "test_cleanup",
        provider="test",
        model="test",
        retryable=False,
    )
    reopened._connection.execute("DELETE FROM advice_localizations")
    reopened._connection.commit()

    consumer = AdviceLocalizationConsumer(reopened, object())

    async def idle_worker():
        await consumer._wake.wait()
        while not consumer._stopping:
            consumer._wake.clear()
            await consumer._wake.wait()

    monkeypatch.setattr(consumer, "_run", idle_worker)

    async def start_and_stop():
        consumer.start()
        row = reopened._connection.execute(
            "SELECT status,attempts FROM advice_localizations WHERE user_id=?",
            ("u1",),
        ).fetchone()
        assert tuple(row) == ("pending", 0)
        await consumer.stop()

    asyncio.run(start_and_stop())
    reopened.close()


def test_account_delete_cascades_localizations_and_tombstone_rejects_late_work(
    tmp_path,
):
    repo = Repository(tmp_path / "delete-localization.db")
    issued = repo.register_account(
        username="translation-owner",
        password="correct horse battery staple",
        device_id="windows-1",
        device_name="Windows",
        expires_at=utc_now() + timedelta(days=1),
        locale="en-US",
    )
    user_id = issued.principal.user_id
    advice = _advice(repo, user_id)
    assert repo.enqueue_advice_localizations(user_id, "en-US") == 1

    repo.delete_account(
        user_id=user_id, current_password="correct horse battery staple"
    )

    assert repo._connection.execute(
        "SELECT COUNT(*) FROM advice_localizations WHERE user_id=?", (user_id,)
    ).fetchone()[0] == 0
    with pytest.raises(AccountRetired):
        repo.ensure_user(user_id)
    with pytest.raises(sqlite3.IntegrityError, match="account is retired"):
        repo._connection.execute(
            """INSERT INTO advice_localizations(
              user_id,advice_id,locale,source_hash,status,attempts,next_attempt_at,
              created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                user_id,
                str(advice.id),
                "en-US",
                "0" * 64,
                "pending",
                0,
                utc_now().isoformat(),
                utc_now().isoformat(),
                utc_now().isoformat(),
            ),
        )
    repo._connection.rollback()
    repo.close()


def test_daily_localization_cap_defers_without_attempts_and_resumes_newest_first(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ADVICE_LOCALIZATION_CALLS_PER_USER_PER_DAY", "1")
    repo = Repository(tmp_path / "daily-localization-cap.db")
    older = _advice(repo, "u1")
    newer = _advice(repo, "u1")
    repo.set_account_locale("u1", "en-US")
    now = utc_now()
    assert repo.enqueue_advice_localizations("u1", "en-US", now=now) == 2
    decision = repo.reserve_global_direct_model_call(
        "u1",
        "openai",
        "advice_translation",
        now=now,
        hourly_call_limit=100,
        daily_call_limit=100,
    )
    assert decision.allowed

    assert repo.claim_next_advice_localization(
        now=now + timedelta(seconds=1)
    ) is None
    rows = repo._connection.execute(
        """SELECT attempts,next_attempt_at,last_error FROM advice_localizations
        WHERE user_id=? ORDER BY advice_id""",
        ("u1",),
    ).fetchall()
    assert len(rows) == 2
    assert all(row["attempts"] == 0 for row in rows)
    assert all(
        row["last_error"] == "localization_daily_budget_deferred" for row in rows
    )
    deferred_times = {row["next_attempt_at"] for row in rows}
    assert len(deferred_times) == 1
    assert repo.claim_next_advice_localization(
        now=now + timedelta(seconds=2)
    ) is None
    assert [
        row["attempts"]
        for row in repo._connection.execute(
            "SELECT attempts FROM advice_localizations WHERE user_id=?",
            ("u1",),
        ).fetchall()
    ] == [0, 0]

    resumed = repo.claim_next_advice_localization(
        now=now + timedelta(days=1, seconds=2)
    )
    assert resumed is not None
    assert resumed.advice_id == newer.id
    assert resumed.advice_id != older.id
    repo.close()


def test_due_live_analysis_prevents_historical_translation_claim(tmp_path):
    repo = Repository(tmp_path / "analysis-priority.db")
    _advice(repo, "u1")
    repo.set_account_locale("u1", "en-US")
    assert repo.enqueue_advice_localizations("u1", "en-US") == 1
    event = Event(
        user_id="u1",
        source="windows.thought",
        type="thought.note",
        facts={"text": "The release is blocked and requires a decision today."},
    )
    assert repo.insert_event(event)
    assert repo.enqueue_analysis_job(
        "u1",
        event.event_id,
        job_kind="discover",
        cloud_approved=True,
    )

    assert repo.claim_next_advice_localization() is None
    row = repo._connection.execute(
        "SELECT status,attempts FROM advice_localizations WHERE user_id=?",
        ("u1",),
    ).fetchone()
    assert tuple(row) == ("pending", 0)
    repo.close()


def test_api_list_attention_and_feedback_expose_ready_display_without_rewriting(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.delenv("MOUCHEN_API_TOKEN", raising=False)
    monkeypatch.delenv("MOUCHEN_API_TOKEN_FILE", raising=False)
    monkeypatch.delenv("MOUCHEN_SINGLE_USER_ID", raising=False)
    import app.main as main

    try:
        main.repo.close()
    except Exception:
        pass
    main.repo = main.Repository(tmp_path / "api-localization.db")
    main.service = main.ProactiveService(main.repo)
    advice = _advice(main.repo, "u1")
    original_payload_hash = _raw_payload_hash(main.repo, "u1", advice.id)
    _ready_translation(main.repo, "u1", advice)
    headers = {"X-User-Id": "u1"}
    locale_reads = 0
    original_user_locale = main.repo.user_locale

    def counted_user_locale(user_id):
        nonlocal locale_reads
        locale_reads += 1
        return original_user_locale(user_id)

    monkeypatch.setattr(main.repo, "user_locale", counted_user_locale)
    single_display_lookup = main.repo.advice_display_envelope
    monkeypatch.setattr(
        main.repo,
        "advice_display_envelope",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("list must use one batched display lookup")
        ),
    )

    with TestClient(main.app) as client:
        listed = client.get("/v1/advice", headers=headers)
        assert listed.status_code == 200, listed.text
        listed_advice = listed.json()[0]
        assert listed_advice["action"] == advice.action
        assert listed_advice["display_translation_status"] == "ready"
        assert listed_advice["display"] == _translation()
        assert locale_reads == 1
        monkeypatch.setattr(
            main.repo,
            "advice_display_envelope",
            single_display_lookup,
        )

        claimed = client.post(
            "/v1/advice-attention/claim",
            headers=headers,
            json={"device_id": "windows-1", "platform": "windows"},
        )
        assert claimed.status_code == 200, claimed.text
        claimed_advice = claimed.json()["advice"]
        assert claimed_advice["action"] == advice.action
        assert claimed_advice["display_translation_status"] == "ready"
        assert claimed_advice["display"] == _translation()

        feedback = client.post(
            f"/v1/advice/{advice.id}/feedback",
            headers=headers,
            json={"kind": "later"},
        )
        assert feedback.status_code == 200, feedback.text
        feedback_advice = feedback.json()["advice"]
        assert feedback_advice["action"] == advice.action
        assert feedback_advice["display_translation_status"] == "ready"
        assert feedback_advice["display"] == _translation()
        assert _raw_payload_hash(main.repo, "u1", advice.id) != original_payload_hash
        stored = main.repo.get_advice("u1", advice.id)
        assert stored.action == advice.action
        assert stored.goal_quote == advice.goal_quote
        assert stored.evidence == advice.evidence
