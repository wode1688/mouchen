"""Global / per-goal advice-preference tests.

Covers: legacy-database migration, defaults, PUT idempotency, revision
conflicts, DELETE restoring inheritance, user isolation and cross-user 404,
validation (frequency / event types / direction length, null vs []), direction
merge order, default cloud redaction of directions, directions never entering
evidence, the three event-filter chains (deterministic / discovery / review),
preference changes hitting pending analysis jobs with zero model calls, global
and goal rolling quotas, per-goal cooldown, published_at accounting, promotion
gating, concurrency not exceeding quotas, manual_problem and emergency
boundaries, and preferences never bypassing the existing safety gates.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.analysis_queue import AnalysisQueueProcessor
from app.advice_refiner import ProactiveIssueDiscoverer, _preference_context
from app.domain.models import (
    AdviceCandidate,
    AdviceLevel,
    AdviceRecord,
    AdviceStatus,
    ContextSnapshot,
    Event,
    FeedbackCreate,
    Goal,
)
from app.domain.preferences import (
    EVENT_TYPE_IDS,
    FREQUENCY_POLICIES,
    default_global_preference,
    resolve_effective,
)
from app.review_engine import GoalReviewEngine
from app.service import ProactiveService
from app.storage import (
    PreferenceLimitExceeded,
    PreferenceRevisionConflict,
    Repository,
)


class ForbiddenGateway:
    """Any model call is a test failure; also counts attempted calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("preference gates must prevent every model call")

    async def second_opinion(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("preference gates must prevent every model call")

    def second_opinion_route(self):
        return "test", "test-model"


def _goal(repo: Repository, *, user_id: str = "u1", domain: str = "work", **target) -> Goal:
    return repo.insert_goal(
        Goal(
            user_id=user_id,
            domain=domain,
            title=f"{domain} goal {uuid4().hex[:6]}",
            quote=f"I will complete the {domain} goal",
            target=target or {"keywords": ["发布"]},
        )
    )


def _candidate(
    goal: Goal,
    *,
    level: AdviceLevel = AdviceLevel.L2,
    dedupe_key: str | None = None,
    event_id=None,
    urgency: float = 0.8,
    impact: float = 0.8,
) -> AdviceCandidate:
    now = datetime.now(timezone.utc)
    return AdviceCandidate(
        user_id=goal.user_id,
        domain=goal.domain,
        requested_level=level,
        goal_id=goal.id,
        goal_quote=goal.quote,
        evidence=[
            {
                "event_id": event_id or uuid4(),
                "source": "test",
                "fact": "A concrete problem was observed",
                "observed_at": now,
                "confidence": 0.99,
            }
        ],
        action="Resolve the observed problem",
        first_step="Open the failing item",
        alternative="Defer with a documented reason",
        prediction={
            "outcome": "The problem remains unresolved",
            "deadline": now + timedelta(days=1),
            "confidence": 0.8,
        },
        adopted_expected_result="The problem is resolved",
        adopted_confidence=0.8,
        urgency=urgency,
        impact=impact,
        novelty=0.8,
        relevance=0.9,
        dedupe_key=dedupe_key or f"pref:{uuid4()}",
    )


def _advice(repo: Repository, goal: Goal, **kwargs) -> AdviceRecord:
    candidate = _candidate(goal, **kwargs)
    record = AdviceRecord(
        **candidate.model_dump(),
        effective_level=candidate.requested_level,
        proactive_score=0.9,
        delivery="immediate",
    )
    return repo.insert_advice(record, enforce_publish_limits=False)


def _backdate_publishes(repo: Repository, *, hours: float) -> None:
    repo._connection.execute(
        "UPDATE advice SET published_at=? WHERE published_at IS NOT NULL",
        ((datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(),),
    )
    repo._connection.commit()


def _set_global(repo: Repository, user_id: str = "u1", **overrides):
    base = {
        "direction": "",
        "direction_mode": "replace",
        "frequency_mode": "active",
        "event_types": list(EVENT_TYPE_IDS),
    }
    base.update(overrides)
    current = repo.global_preference(user_id)
    return repo.upsert_advice_preference(
        user_id, goal_id=None, expected_revision=current.revision, **base
    )


# ---------------------------------------------------------------------------
# migration, defaults, CRUD
# ---------------------------------------------------------------------------


def test_legacy_database_migrates_columns_and_backfills_published_at(tmp_path: Path):
    path = tmp_path / "legacy.db"
    repo = Repository(path)
    goal = _goal(repo)
    active = _advice(repo, goal)
    provisional_candidate = _candidate(goal, level=AdviceLevel.L3)
    provisional = AdviceRecord(
        **provisional_candidate.model_dump(),
        effective_level=AdviceLevel.L3,
        proactive_score=0.9,
        delivery="immediate",
        status=AdviceStatus.PROVISIONAL,
    )
    repo.insert_advice(provisional, enforce_publish_limits=False)
    # Simulate the pre-preference schema.
    repo._connection.executescript(
        """
        DROP INDEX IF EXISTS idx_advice_user_published;
        DROP INDEX IF EXISTS idx_advice_user_goal_published;
        ALTER TABLE advice DROP COLUMN published_at;
        ALTER TABLE advice DROP COLUMN goal_id;
        DROP TABLE IF EXISTS advice_preferences;
        """
    )
    repo._connection.commit()
    repo.close()

    migrated = Repository(path)  # must not wipe anything
    columns = {
        row["name"]
        for row in migrated._connection.execute("PRAGMA table_info(advice)")
    }
    assert {"published_at", "goal_id"} <= columns
    rows = {
        row["status"]: row
        for row in migrated._connection.execute(
            "SELECT status, published_at, goal_id, created_at FROM advice"
        )
    }
    # Genuinely published advice counts from its creation moment...
    assert rows["active"]["published_at"] == rows["active"]["created_at"]
    # ...while provisional advice never gains a published_at.
    assert rows["provisional"]["published_at"] is None
    assert rows["active"]["goal_id"] == str(goal.id)
    assert migrated.get_advice("u1", active.id) is not None
    tables = {
        row["name"]
        for row in migrated._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "advice_preferences" in tables
    migrated.close()


def test_default_global_preference_is_active_all_events_empty_direction(tmp_path: Path):
    repo = Repository(tmp_path / "defaults.db")
    pref = repo.global_preference("u1")
    assert pref.frequency_mode == "active"
    assert pref.event_types == list(EVENT_TYPE_IDS)
    assert pref.direction == ""
    assert pref.revision == 0
    effective = repo.effective_preference("u1", None)
    assert effective.paused is False
    assert effective.directions == []
    repo.close()


def test_put_is_idempotent_and_revision_guards_stale_pages(tmp_path: Path):
    repo = Repository(tmp_path / "revision.db")
    first = _set_global(repo, direction="关注渠道")
    assert first.revision == 1

    # Identical replay with the already-consumed revision is accepted without
    # a bump (safe retry after a timeout)...
    replay = repo.upsert_advice_preference(
        "u1",
        goal_id=None,
        direction="关注渠道",
        direction_mode="replace",
        frequency_mode="active",
        event_types=list(EVENT_TYPE_IDS),
        expected_revision=0,
    )
    assert replay.revision == 1

    # ...but a stale page writing DIFFERENT content conflicts and receives the
    # current object to reload from.
    with pytest.raises(PreferenceRevisionConflict) as excinfo:
        repo.upsert_advice_preference(
            "u1",
            goal_id=None,
            direction="换个方向",
            direction_mode="replace",
            frequency_mode="quiet",
            event_types=list(EVENT_TYPE_IDS),
            expected_revision=0,
        )
    assert excinfo.value.current.revision == 1
    assert excinfo.value.current.direction == "关注渠道"

    # Same content at the current revision does not bump either.
    same = repo.upsert_advice_preference(
        "u1",
        goal_id=None,
        direction="关注渠道",
        direction_mode="replace",
        frequency_mode="active",
        event_types=list(EVENT_TYPE_IDS),
        expected_revision=1,
    )
    assert same.revision == 1
    repo.close()


def test_delete_goal_override_restores_global_immediately(tmp_path: Path):
    repo = Repository(tmp_path / "delete.db")
    goal = _goal(repo)
    _set_global(repo, direction="全局方向", frequency_mode="balanced")
    repo.upsert_advice_preference(
        "u1",
        goal_id=goal.id,
        direction="目标方向",
        direction_mode="replace",
        frequency_mode="quiet",
        event_types=[],
        expected_revision=0,
    )
    overridden = repo.effective_preference("u1", goal.id)
    assert overridden.frequency_mode == "quiet"
    assert overridden.event_types == []

    assert repo.delete_goal_preference("u1", goal.id) is True
    restored = repo.effective_preference("u1", goal.id)
    assert restored.frequency_mode == "balanced"
    assert restored.event_types == list(EVENT_TYPE_IDS)
    assert restored.directions == ["全局方向"]
    assert repo.delete_goal_preference("u1", goal.id) is False  # idempotent
    repo.close()


def test_inheritance_null_vs_empty_list_and_direction_merge_order(tmp_path: Path):
    repo = Repository(tmp_path / "inherit.db")
    goal = _goal(repo)
    _set_global(repo, direction="全局：盯风险", frequency_mode="balanced")

    inherit = resolve_effective(
        repo.global_preference("u1"),
        repo.goal_preference("u1", goal.id),
    )
    assert inherit.frequency_mode == "balanced"

    repo.upsert_advice_preference(
        "u1",
        goal_id=goal.id,
        direction="目标：盯机会",
        direction_mode="append",
        frequency_mode=None,  # inherit
        event_types=None,  # inherit
        expected_revision=0,
    )
    appended = repo.effective_preference("u1", goal.id)
    # Merge order: global first, goal appended; goal wins on conflict (prompt).
    assert appended.directions == ["全局：盯风险", "目标：盯机会"]
    assert appended.frequency_mode == "balanced"  # inherited (None)
    assert appended.event_types == list(EVENT_TYPE_IDS)  # inherited (None)

    current = repo.goal_preference("u1", goal.id)
    repo.upsert_advice_preference(
        "u1",
        goal_id=goal.id,
        direction="目标：盯机会",
        direction_mode="replace",
        frequency_mode=None,
        event_types=[],  # explicit empty list: block ALL automatic events
        expected_revision=current.revision,
    )
    replaced = repo.effective_preference("u1", goal.id)
    assert replaced.directions == ["目标：盯机会"]
    assert replaced.event_types == []  # [] must never collapse into a default

    current = repo.goal_preference("u1", goal.id)
    repo.upsert_advice_preference(
        "u1",
        goal_id=goal.id,
        direction="目标：盯机会",
        direction_mode="inherit",
        frequency_mode=None,
        event_types=[],
        expected_revision=current.revision,
    )
    inherited = repo.effective_preference("u1", goal.id)
    assert inherited.directions == ["全局：盯风险"]  # inherit: global only
    repo.close()


def test_preferences_are_isolated_per_user(tmp_path: Path):
    repo = Repository(tmp_path / "isolation.db")
    _set_global(repo, user_id="u1", direction="u1 的方向", frequency_mode="quiet")
    other = repo.global_preference("u2")
    assert other.direction == ""
    assert other.frequency_mode == "active"
    assert repo.list_goal_preferences("u2") == []
    assert repo.effective_preference("u2", None).frequency_mode == "active"
    repo.close()


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


def _api(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "prefs-api.db"))
    monkeypatch.delenv("MOUCHEN_API_TOKEN", raising=False)
    monkeypatch.delenv("MOUCHEN_API_TOKEN_FILE", raising=False)
    monkeypatch.delenv("MOUCHEN_SINGLE_USER_ID", raising=False)
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(tmp_path / "prefs-api.db")
    main.service = main.ProactiveService(main.repo)
    return main


def test_api_get_put_delete_flow_with_catalog_and_policies(tmp_path, monkeypatch):
    main = _api(tmp_path, monkeypatch)
    goal = _goal(main.repo)
    headers = {"X-User-Id": "u1"}
    with TestClient(main.app) as client:
        snapshot = client.get("/v1/advice-preferences", headers=headers)
        assert snapshot.status_code == 200
        payload = snapshot.json()
        assert payload["global"]["frequency_mode"] == "active"
        assert payload["global"]["revision"] == 0
        assert [item["id"] for item in payload["event_type_catalog"]] == list(
            EVENT_TYPE_IDS
        )
        assert payload["event_type_catalog"][0]["label"] == "通知"
        assert payload["frequency_policies"]["balanced"]["max_per_window"] == 3
        assert payload["frequency_policies"]["quiet"]["cooldown_minutes"] == 720
        assert payload["frequency_policies"]["paused"]["label"] == "暂停主动建言"
        goals = {item["goal_id"]: item for item in payload["goals"]}
        assert goals[str(goal.id)]["has_override"] is False
        assert goals[str(goal.id)]["effective"]["frequency_mode"] == "active"

        saved = client.put(
            "/v1/advice-preferences/global",
            headers=headers,
            json={
                "direction": "  多看渠道和现金流  ",
                "frequency_mode": "balanced",
                "event_types": ["notification.posted", "mail.received"],
                "revision": 0,
            },
        )
        assert saved.status_code == 200
        body = saved.json()["preference"]
        assert body["direction"] == "多看渠道和现金流"  # stripped
        assert body["revision"] == 1

        conflict = client.put(
            "/v1/advice-preferences/global",
            headers=headers,
            json={
                "direction": "另一台旧页面",
                "frequency_mode": "quiet",
                "event_types": ["mail.received"],
                "revision": 0,
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "preference_revision_conflict"
        assert conflict.json()["detail"]["current"]["revision"] == 1

        # A stale preference write must not disturb the independent central
        # attention route or its device registration transaction.
        attention = client.post(
            "/v1/advice-attention/claim",
            headers=headers,
            json={"device_id": "windows-preference-test", "platform": "windows"},
        )
        assert attention.status_code == 200
        assert attention.json() == {"status": "none"}

        goal_saved = client.put(
            f"/v1/advice-preferences/goals/{goal.id}",
            headers=headers,
            json={
                "direction": "这个目标盯交付",
                "direction_mode": "append",
                "frequency_mode": None,
                "event_types": [],
                "revision": 0,
            },
        )
        assert goal_saved.status_code == 200
        effective = goal_saved.json()["effective"]
        assert effective["directions"] == ["多看渠道和现金流", "这个目标盯交付"]
        assert effective["event_types"] == []  # explicit block-all survives JSON

        removed = client.delete(
            f"/v1/advice-preferences/goals/{goal.id}", headers=headers
        )
        assert removed.status_code == 200
        assert removed.json()["status"] == "deleted"
        assert removed.json()["effective"]["event_types"] == [
            "notification.posted",
            "mail.received",
        ]


def test_api_validation_and_cross_user_404(tmp_path, monkeypatch):
    main = _api(tmp_path, monkeypatch)
    goal = _goal(main.repo, user_id="u1")
    with TestClient(main.app) as client:
        bad_frequency = client.put(
            "/v1/advice-preferences/global",
            headers={"X-User-Id": "u1"},
            json={
                "frequency_mode": "hyperactive",
                "event_types": list(EVENT_TYPE_IDS),
                "revision": 0,
            },
        )
        assert bad_frequency.status_code == 422

        bad_event = client.put(
            "/v1/advice-preferences/global",
            headers={"X-User-Id": "u1"},
            json={
                "frequency_mode": "active",
                "event_types": ["totally.unknown"],
                "revision": 0,
            },
        )
        assert bad_event.status_code == 422

        too_long = client.put(
            "/v1/advice-preferences/global",
            headers={"X-User-Id": "u1"},
            json={
                "direction": "长" * 2001,
                "frequency_mode": "active",
                "event_types": list(EVENT_TYPE_IDS),
                "revision": 0,
            },
        )
        assert too_long.status_code == 422

        # The global scope must stay concrete: inherit placeholders are 422.
        null_frequency = client.put(
            "/v1/advice-preferences/global",
            headers={"X-User-Id": "u1"},
            json={"event_types": list(EVENT_TYPE_IDS), "revision": 0},
        )
        assert null_frequency.status_code == 422
        assert null_frequency.json()["detail"]["code"] == "global_frequency_required"

        cross_user = client.put(
            f"/v1/advice-preferences/goals/{goal.id}",
            headers={"X-User-Id": "u2"},
            json={"direction": "偷改", "revision": 0},
        )
        assert cross_user.status_code == 404
        assert cross_user.json()["detail"]["code"] == "goal_not_found"

        cross_delete = client.delete(
            f"/v1/advice-preferences/goals/{goal.id}", headers={"X-User-Id": "u2"}
        )
        assert cross_delete.status_code == 404

        assert main.repo.goal_preference("u1", goal.id) is None


# ---------------------------------------------------------------------------
# direction handling: redaction, never evidence
# ---------------------------------------------------------------------------


def test_direction_is_redacted_by_default_and_never_enters_evidence(tmp_path: Path):
    repo = Repository(tmp_path / "redaction.db")
    goal = _goal(repo)
    _set_global(repo, direction="优先关注 owner@example.com 的渠道进展")

    context = _preference_context(repo, "u1", goal.id, False)
    assert "owner@example.com" not in str(context)
    assert "[email]" in context["global_direction"]

    raw = _preference_context(repo, "u1", goal.id, True)
    assert "owner@example.com" in raw["global_direction"]

    # Publishing under a direction never plants the direction into evidence.
    service = ProactiveService(repo)
    evaluation = service.evaluate(_candidate(goal), ContextSnapshot())
    assert evaluation.decision == "publish"
    facts = " ".join(item.fact for item in evaluation.advice.evidence)
    assert "渠道进展" not in facts
    repo.close()


# ---------------------------------------------------------------------------
# event filtering: deterministic / discovery / review chains
# ---------------------------------------------------------------------------


def test_deterministic_chain_filters_disabled_event_types(tmp_path: Path):
    repo = Repository(tmp_path / "deterministic-filter.db")
    service = ProactiveService(repo)
    goal = _goal(repo, keywords=["发布"])
    _set_global(repo, event_types=["mail.received"])  # notifications disabled

    evaluation = service.ingest(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={"title": "发布失败", "text": "构建超时 urgent"},
            confidence=0.98,
        )
    )
    assert evaluation is None  # event recorded, no candidate
    assert len(repo.recent_events("u1", 10)) == 1
    assert repo.list_advice("u1") == []

    # The same event type allowed again publishes normally.
    _set_global(repo, event_types=list(EVENT_TYPE_IDS))
    evaluation = service.ingest(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={"title": "发布失败", "text": "第二次构建失败 error"},
            confidence=0.98,
        )
    )
    assert evaluation is not None
    assert goal.id == evaluation.advice.goal_id if evaluation.advice else True
    repo.close()


def test_discovery_chain_blocks_disabled_type_and_pause_with_zero_model_calls(
    tmp_path: Path,
):
    repo = Repository(tmp_path / "discovery-filter.db")
    goal = _goal(repo, keywords=["发布"])
    gateway = ForbiddenGateway()
    discoverer = ProactiveIssueDiscoverer(repo, gateway)
    event = Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={"title": "发布失败", "text": "构建超时风险"},
        confidence=0.98,
    )
    repo.insert_event(event)

    _set_global(repo, event_types=["mail.received"])
    attempt = asyncio.run(discoverer.discover_attempt(event, "u1"))
    assert attempt.disposition == "no_intervention"
    assert attempt.reason_code == "preference_event_type_disabled"

    _set_global(repo, event_types=list(EVENT_TYPE_IDS), frequency_mode="paused")
    attempt = asyncio.run(discoverer.discover_attempt(event, "u1"))
    assert attempt.disposition == "no_intervention"
    assert attempt.reason_code == "preference_paused"

    # A goal-scope pause blocks discovery for that goal too.
    _set_global(repo, frequency_mode="active")
    repo.upsert_advice_preference(
        "u1",
        goal_id=goal.id,
        direction="",
        direction_mode="inherit",
        frequency_mode="paused",
        event_types=None,
        expected_revision=0,
    )
    attempt = asyncio.run(discoverer.discover_attempt(event, "u1"))
    assert attempt.disposition == "no_intervention"
    assert attempt.reason_code == "preference_paused"
    assert gateway.calls == 0
    repo.close()


def test_review_chain_time_review_obeys_foreground_session_toggle(tmp_path: Path):
    repo = Repository(tmp_path / "review-filter.db")
    service = ProactiveService(repo)
    now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
    goal = repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="发布AI替身",
            quote="本周优先完成AI替身发布",
            target={"weekly_hours": 14, "packages": ["com.example.builder"]},
        )
    )
    for index, package in enumerate(("com.example.chat", "com.example.builder")):
        repo.insert_event(
            Event(
                user_id="u1",
                source="android.usage",
                type="app.foreground_session",
                occurred_at=now - timedelta(hours=index + 1),
                facts={
                    "package": package,
                    "app_label": package,
                    "duration_ms": 2 * 3_600_000,
                    "timezone_offset_minutes": 0,
                },
                evidence_ref=f"usage:{index}",
            )
        )
    engine = GoalReviewEngine(repo, service, ForbiddenGateway())

    # app.foreground_session disabled for this goal: no time review at all.
    repo.upsert_advice_preference(
        "u1",
        goal_id=goal.id,
        direction="",
        direction_mode="inherit",
        frequency_mode=None,
        event_types=[item for item in EVENT_TYPE_IDS if item != "app.foreground_session"],
        expected_revision=0,
    )
    blocked = asyncio.run(engine.tick("u1", now=now))
    assert blocked.reviewed_goals == 0
    assert blocked.evaluations == []
    assert repo.list_advice("u1") == []

    # Restoring inheritance re-enables the same deterministic time review.
    repo.delete_goal_preference("u1", goal.id)
    allowed = asyncio.run(engine.tick("u1", now=now))
    assert allowed.reviewed_goals == 1
    assert allowed.evaluations and allowed.evaluations[0].decision == "publish"
    repo.close()


def test_review_chain_paused_goal_is_skipped_entirely(tmp_path: Path):
    repo = Repository(tmp_path / "review-paused.db")
    service = ProactiveService(repo)
    now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
    repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="发布AI替身",
            quote="本周优先完成AI替身发布",
            target={"weekly_hours": 14, "packages": ["com.example.builder"]},
        )
    )
    repo.insert_event(
        Event(
            user_id="u1",
            source="android.usage",
            type="app.foreground_session",
            occurred_at=now - timedelta(hours=1),
            facts={
                "package": "com.example.chat",
                "duration_ms": 3_600_000,
                "timezone_offset_minutes": 0,
            },
            evidence_ref="usage:paused",
        )
    )
    _set_global(repo, frequency_mode="paused")
    result = asyncio.run(
        GoalReviewEngine(repo, service, ForbiddenGateway()).tick("u1", now=now)
    )
    assert result.reviewed_goals == 0
    assert result.evaluations == []
    repo.close()


# ---------------------------------------------------------------------------
# pending analysis jobs: fresh preference read, zero model calls
# ---------------------------------------------------------------------------


def test_pending_job_completes_on_pause_with_zero_model_calls(tmp_path: Path):
    repo = Repository(tmp_path / "pending-pause.db")
    service = ProactiveService(repo)
    _goal(repo, keywords=["发布"])
    event = Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={"title": "发布失败", "text": "构建失败 error"},
        confidence=0.98,
    )
    repo.insert_event(event)
    assert repo.enqueue_analysis_job(
        "u1", event.event_id, job_kind="discover", cloud_approved=True
    )
    # The preference changes AFTER the job was queued...
    _set_global(repo, frequency_mode="paused")
    gateway = ForbiddenGateway()
    processor = AnalysisQueueProcessor(repo, service, gateway)

    result = asyncio.run(processor.process_event("u1", event.event_id))

    assert result.attempted == 1
    assert result.no_intervention == 1
    assert gateway.calls == 0  # ...and is honoured before ANY model work
    job = repo._connection.execute(
        "SELECT status, last_reason FROM analysis_jobs WHERE event_id=?",
        (str(event.event_id),),
    ).fetchone()
    assert job["status"] == "no_intervention"
    assert job["last_reason"] == "preference_paused"
    repo.close()


def test_pending_job_completes_on_disabled_event_type_with_zero_model_calls(
    tmp_path: Path,
):
    repo = Repository(tmp_path / "pending-disabled.db")
    service = ProactiveService(repo)
    _goal(repo, keywords=["发布"])
    event = Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={"title": "发布失败", "text": "构建失败 error"},
        confidence=0.98,
    )
    repo.insert_event(event)
    assert repo.enqueue_analysis_job(
        "u1", event.event_id, job_kind="discover", cloud_approved=True
    )
    _set_global(repo, event_types=["mail.received"])
    gateway = ForbiddenGateway()
    result = asyncio.run(
        AnalysisQueueProcessor(repo, service, gateway).process_event(
            "u1", event.event_id
        )
    )
    assert result.no_intervention == 1
    assert gateway.calls == 0
    job = repo._connection.execute(
        "SELECT status, last_reason FROM analysis_jobs WHERE event_id=?",
        (str(event.event_id),),
    ).fetchone()
    assert job["last_reason"] == "preference_event_type_disabled"
    repo.close()


# ---------------------------------------------------------------------------
# frequency quotas: windows, cooldown, published_at, promotion, concurrency
# ---------------------------------------------------------------------------


def test_global_rolling_window_counts_all_published_statuses(tmp_path: Path):
    repo = Repository(tmp_path / "global-window.db")
    service = ProactiveService(repo)
    _set_global(repo, frequency_mode="balanced")  # 3 per 24h
    goals = [_goal(repo, domain=f"work{index}") for index in range(4)]
    for goal, status in zip(
        goals[:3], (AdviceStatus.ACTIVE, AdviceStatus.DISMISSED, AdviceStatus.WITHDRAWN)
    ):
        record = _advice(repo, goal)
        if status != AdviceStatus.ACTIVE:
            repo._connection.execute(
                "UPDATE advice SET status=? WHERE id=?", (status.value, str(record.id))
            )
            repo._connection.commit()
    _backdate_publishes(repo, hours=5)  # inside the 24h window, past cooldowns

    blocked = service.evaluate(_candidate(goals[3]), ContextSnapshot())
    assert blocked.decision == "hold"
    assert "PREFERENCE_GLOBAL_LIMIT" in blocked.reason_codes

    _backdate_publishes(repo, hours=25)  # rolls out of the window
    allowed = service.evaluate(_candidate(goals[3]), ContextSnapshot())
    assert allowed.decision == "publish"
    repo.close()


def test_goal_override_local_quota_and_cooldown(tmp_path: Path):
    repo = Repository(tmp_path / "goal-quota.db")
    service = ProactiveService(repo)
    goal = _goal(repo)
    other = _goal(repo, domain="health")
    repo.upsert_advice_preference(
        "u1",
        goal_id=goal.id,
        direction="",
        direction_mode="inherit",
        frequency_mode="quiet",  # 1 per 24h, 12h cooldown, local to this goal
        event_types=None,
        expected_revision=0,
    )
    _advice(repo, goal)
    _backdate_publishes(repo, hours=5)  # beyond quiet's cooldown? no: 12h

    cooled = service.evaluate(_candidate(goal), ContextSnapshot())
    assert cooled.decision == "hold"
    assert "PREFERENCE_GOAL_COOLDOWN" in cooled.reason_codes

    _backdate_publishes(repo, hours=13)  # cooldown cleared, count still 1/1
    limited = service.evaluate(_candidate(goal), ContextSnapshot())
    assert limited.decision == "hold"
    assert "PREFERENCE_GOAL_LIMIT" in limited.reason_codes

    # The other goal only answers to the global budget.
    unaffected = service.evaluate(_candidate(other), ContextSnapshot())
    assert unaffected.decision == "publish"
    repo.close()


def test_inherited_goal_cooldown_uses_effective_policy(tmp_path: Path):
    repo = Repository(tmp_path / "inherited-cooldown.db")
    service = ProactiveService(repo)
    goal = _goal(repo)
    _advice(repo, goal)  # published just now; active default: 60min spacing

    blocked = service.evaluate(_candidate(goal), ContextSnapshot())
    assert blocked.decision == "hold"
    assert "PREFERENCE_GOAL_COOLDOWN" in blocked.reason_codes

    _backdate_publishes(repo, hours=2)
    allowed = service.evaluate(_candidate(goal), ContextSnapshot())
    assert allowed.decision == "publish"
    repo.close()


def test_provisional_does_not_count_and_promotion_runs_the_same_gate(tmp_path: Path):
    repo = Repository(tmp_path / "promotion.db")
    _set_global(repo, frequency_mode="quiet")  # 1 per day
    goal = _goal(repo)
    candidate = _candidate(goal, level=AdviceLevel.L3)
    provisional = AdviceRecord(
        **candidate.model_dump(),
        effective_level=AdviceLevel.L3,
        proactive_score=0.9,
        delivery="immediate",
        status=AdviceStatus.PROVISIONAL,
    )
    repo.insert_advice(provisional)  # gate does not apply to provisional
    row = repo._connection.execute(
        "SELECT published_at FROM advice WHERE id=?", (str(provisional.id),)
    ).fetchone()
    assert row["published_at"] is None

    # Exhaust the quota, then promotion must be blocked atomically...
    filler = _advice(repo, _goal(repo, domain="filler"))
    _backdate_publishes(repo, hours=5)
    assert filler is not None
    with pytest.raises(PreferenceLimitExceeded) as excinfo:
        repo.promote_advice(provisional)
    assert excinfo.value.reason_code == "PREFERENCE_GLOBAL_LIMIT"
    still = repo._connection.execute(
        "SELECT status, published_at FROM advice WHERE id=?", (str(provisional.id),)
    ).fetchone()
    # ...and never становится active first: nothing to withdraw afterwards.
    assert still["status"] == "provisional"
    assert still["published_at"] is None

    _backdate_publishes(repo, hours=25)
    promoted = repo.promote_advice(provisional)
    assert promoted.status == AdviceStatus.ACTIVE
    row = repo._connection.execute(
        "SELECT published_at FROM advice WHERE id=?", (str(provisional.id),)
    ).fetchone()
    assert row["published_at"] is not None  # counted from the promote moment
    repo.close()


def test_concurrent_publishes_cannot_exceed_the_last_quota_slot(tmp_path: Path):
    path = tmp_path / "concurrent.db"
    repo = Repository(path)
    _set_global(repo, frequency_mode="balanced")  # 3 per 24h
    seed_goals = [_goal(repo, domain=f"seed{index}") for index in range(2)]
    for goal in seed_goals:
        _advice(repo, goal)  # 2 of 3 slots used
    _backdate_publishes(repo, hours=5)
    contenders = [_goal(repo, domain="raceA"), _goal(repo, domain="raceB")]
    repo.close()

    outcomes: list[str] = []
    lock = threading.Lock()

    def publish(goal: Goal) -> None:
        worker = Repository(path)
        try:
            worker.insert_advice(
                AdviceRecord(
                    **_candidate(goal).model_dump(),
                    effective_level=AdviceLevel.L2,
                    proactive_score=0.9,
                    delivery="immediate",
                )
            )
            outcome = "published"
        except PreferenceLimitExceeded as exc:
            outcome = exc.reason_code
        finally:
            worker.close()
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=publish, args=(goal,)) for goal in contenders]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["PREFERENCE_GLOBAL_LIMIT", "published"]
    check = Repository(path)
    count = check._connection.execute(
        "SELECT COUNT(*) AS count FROM advice WHERE published_at IS NOT NULL"
    ).fetchone()["count"]
    check.close()
    assert count == 3  # the quota was never exceeded


# ---------------------------------------------------------------------------
# paused / important_only / emergency / manual_problem boundaries
# ---------------------------------------------------------------------------


def test_paused_blocks_everything_including_emergencies(tmp_path: Path):
    repo = Repository(tmp_path / "paused.db")
    service = ProactiveService(repo)
    goal = _goal(repo)
    repo.set_charter("u1", "work", AdviceLevel.L4, False)
    _set_global(repo, frequency_mode="paused")

    ordinary = service.evaluate(_candidate(goal), ContextSnapshot())
    assert ordinary.decision == "hold"
    assert "PREFERENCE_PAUSED" in ordinary.reason_codes

    emergency = service.evaluate(
        _candidate(goal, level=AdviceLevel.L3, urgency=0.99, impact=0.95),
        ContextSnapshot(),
        l3_review_passed=True,
    )
    assert emergency.decision == "hold"
    assert "PREFERENCE_PAUSED" in emergency.reason_codes

    manual_event = Event(
        user_id="u1",
        source="windows.manual",
        type="ui.visible_text",
        facts={"visible_text": "A concrete problem", "context": "manual_problem"},
    )
    repo.insert_event(manual_event)
    manual_candidate = _candidate(goal, event_id=manual_event.event_id)
    manual = service.evaluate(manual_candidate, ContextSnapshot())
    assert manual.decision == "hold"
    assert "PREFERENCE_PAUSED" in manual.reason_codes
    with pytest.raises(PreferenceLimitExceeded) as excinfo:
        repo.insert_advice(
            AdviceRecord(
                **manual_candidate.model_dump(),
                effective_level=AdviceLevel.L2,
                proactive_score=0.9,
                delivery="immediate",
            )
        )
    assert excinfo.value.reason_code == "PREFERENCE_PAUSED"
    assert repo.list_advice("u1") == []
    repo.close()


def test_important_only_allows_l3_blocks_l2(tmp_path: Path):
    repo = Repository(tmp_path / "important.db")
    service = ProactiveService(repo)
    goal = _goal(repo)
    repo.set_charter("u1", "work", AdviceLevel.L4, False)
    _set_global(repo, frequency_mode="important_only")

    minor = service.evaluate(_candidate(goal), ContextSnapshot())
    assert minor.decision == "hold"
    assert "PREFERENCE_IMPORTANT_ONLY" in minor.reason_codes
    # The preference filters by the EFFECTIVE level: it can only reduce
    # output, never raise the trust-derived speaking ceiling to satisfy L3.
    assert minor.effective_level < AdviceLevel.L3

    # With an actually earned/authorized L3 effective level, publishing works
    # (validated at the atomic storage gate, the same one promotion uses).
    candidate = _candidate(goal, level=AdviceLevel.L3)
    record = AdviceRecord(
        **candidate.model_dump(),
        effective_level=AdviceLevel.L3,
        proactive_score=0.9,
        delivery="immediate",
    )
    published = repo.insert_advice(record)  # gate enforced; L3 passes
    assert published.status == AdviceStatus.ACTIVE

    # An L2 record hitting the same storage gate is rejected with the stable code.
    weaker = _candidate(goal, dedupe_key="important:l2")
    with pytest.raises(PreferenceLimitExceeded) as excinfo:
        repo.insert_advice(
            AdviceRecord(
                **weaker.model_dump(),
                effective_level=AdviceLevel.L2,
                proactive_score=0.9,
                delivery="immediate",
            )
        )
    assert excinfo.value.reason_code == "PREFERENCE_IMPORTANT_ONLY"
    repo.close()


def test_emergency_bypasses_numeric_quota_but_not_disabled_event_types(tmp_path: Path):
    repo = Repository(tmp_path / "emergency.db")
    service = ProactiveService(repo)
    goal = _goal(repo, keywords=["发布"])
    repo.set_charter("u1", "work", AdviceLevel.L4, False)
    _set_global(repo, frequency_mode="quiet")  # 1 per day
    _advice(repo, _goal(repo, domain="filler"))
    _backdate_publishes(repo, hours=5)  # quota exhausted

    ordinary = service.evaluate(_candidate(goal), ContextSnapshot())
    assert ordinary.decision == "hold"

    emergency = service.evaluate(
        _candidate(goal, level=AdviceLevel.L3, urgency=0.99, impact=0.95),
        ContextSnapshot(),
        l3_review_passed=True,
    )
    assert emergency.decision == "publish"  # numeric quota bypassed

    # But a disabled event type still blocks the pipeline that feeds it.
    _set_global(repo, frequency_mode="quiet", event_types=["mail.received"])
    evaluation = service.ingest(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={"title": "紧急 发布失败", "text": "urgent 构建失败"},
            confidence=0.99,
        )
    )
    assert evaluation is None
    repo.close()


def test_manual_problem_bypasses_frequency_and_event_filter(tmp_path: Path):
    repo = Repository(tmp_path / "manual.db")
    goal = _goal(repo, keywords=["发布"])
    _set_global(repo, frequency_mode="quiet", event_types=[])
    _advice(repo, _goal(repo, domain="filler"))
    _backdate_publishes(repo, hours=5)  # numeric quota remains exhausted
    manual_event = Event(
        user_id="u1",
        source="windows.manual",
        type="ui.visible_text",
        facts={
            "visible_text": "发布迁移一直失败怎么办",
            "context": "manual_problem",
            "analysis_requested": True,
        },
        sensitivity="sensitive",
    )
    repo.insert_event(manual_event)

    # Event filtering and numeric quota/cooldown stand aside; paused and all
    # non-frequency safety gates are tested separately above/below.
    import app.main as main_module

    original_repo = main_module.repo
    main_module.repo = repo
    try:
        assert main_module._automatic_analysis_preference_block(manual_event) is None
    finally:
        main_module.repo = original_repo

    candidate = _candidate(goal, event_id=manual_event.event_id)
    record = AdviceRecord(
        **candidate.model_dump(),
        effective_level=AdviceLevel.L2,
        proactive_score=0.9,
        delivery="immediate",
    )
    published = repo.insert_advice(record)  # gate active, manual bypasses it
    assert published.status == AdviceStatus.ACTIVE

    # The same submission WITHOUT the manual context is fully gated.
    ordinary = _candidate(goal)
    with pytest.raises(PreferenceLimitExceeded):
        repo.insert_advice(
            AdviceRecord(
                **ordinary.model_dump(),
                effective_level=AdviceLevel.L2,
                proactive_score=0.9,
                delivery="immediate",
            )
        )
    repo.close()


def test_preferences_cannot_bypass_existing_safety_gates(tmp_path: Path):
    """A permissive preference never raises levels, revives suppressed topics,
    or weakens the evidence bar."""

    repo = Repository(tmp_path / "safety.db")
    service = ProactiveService(repo)
    goal = _goal(repo)
    _set_global(
        repo,
        direction="尽量多提建议，任何小事都提醒我",
        frequency_mode="active",
    )

    from app.domain.models import FeedbackCreate

    suppressed = service.evaluate(
        _candidate(goal, dedupe_key="stop:me"), ContextSnapshot()
    )
    assert suppressed.decision == "publish"
    repo.record_feedback("u1", suppressed.advice.id, FeedbackCreate(kind="stop_topic"))
    _backdate_publishes(repo, hours=2)

    same_topic = service.evaluate(
        _candidate(goal, dedupe_key="stop:me"), ContextSnapshot()
    )
    assert same_topic.decision == "hold"
    assert "TOPIC_SUPPRESSED_BY_USER" in same_topic.reason_codes

    # The charter ceiling still caps levels regardless of preference.
    ambitious = service.evaluate(
        _candidate(goal, level=AdviceLevel.L3),
        ContextSnapshot(),
        l3_review_passed=True,
    )
    assert ambitious.effective_level <= AdviceLevel.L2  # default charter is L2
    repo.close()


# ---------------------------------------------------------------------------
# preference x central-attention integration
# ---------------------------------------------------------------------------


def test_publish_frequency_is_orthogonal_to_two_delivery_attention_cycle(
    tmp_path: Path,
):
    repo = Repository(tmp_path / "frequency-attention.db")
    _set_global(repo, frequency_mode="quiet")  # exactly one publish per day
    goal = _goal(repo)
    advice = _advice(repo, goal)
    start = datetime.now(timezone.utc)

    def published_count() -> int:
        return int(
            repo._connection.execute(
                "SELECT COUNT(*) AS count FROM advice WHERE published_at IS NOT NULL"
            ).fetchone()["count"]
        )

    assert published_count() == 1
    first = repo.claim_advice_attention("u1", "android", now=start)
    assert first is not None and first.delivery_number == 1
    repo.complete_advice_attention(
        "u1",
        advice.id,
        "android",
        first.claim_token,
        now=start + timedelta(minutes=1),
    )

    second = repo.claim_advice_attention(
        "u1", "windows", now=start + timedelta(hours=1)
    )
    assert second is not None and second.delivery_number == 2
    repo.complete_advice_attention(
        "u1",
        advice.id,
        "windows",
        second.claim_token,
        now=start + timedelta(hours=1, minutes=1),
    )

    # Both deliveries belong to one published advice. The quiet publish quota
    # remains one, and no device can obtain a third permit.
    assert published_count() == 1
    assert (
        repo.claim_advice_attention(
            "u1", "ios", now=start + timedelta(hours=1, minutes=2)
        )
        is None
    )
    repo.close()


def test_pause_blocks_due_emergency_reminder_without_spending_then_resume_continues(
    tmp_path: Path,
):
    repo = Repository(tmp_path / "pause-attention.db")
    goal = _goal(repo)
    candidate = _candidate(
        goal,
        level=AdviceLevel.L3,
        urgency=0.99,
        impact=0.95,
    )
    advice = repo.insert_advice(
        AdviceRecord(
            **candidate.model_dump(),
            effective_level=AdviceLevel.L3,
            proactive_score=0.95,
            delivery="immediate",
        )
    )
    start = datetime.now(timezone.utc)
    first = repo.claim_advice_attention("u1", "android", now=start)
    assert first is not None and first.delivery_number == 1
    repo.complete_advice_attention(
        "u1",
        advice.id,
        "android",
        first.claim_token,
        now=start + timedelta(minutes=1),
    )

    _set_global(repo, frequency_mode="paused")
    assert (
        repo.claim_advice_attention(
            "u1", "windows", now=start + timedelta(hours=1)
        )
        is None
    )
    paused = repo._connection.execute(
        "SELECT state,delivery_count FROM advice_attention WHERE advice_id=?",
        (str(advice.id),),
    ).fetchone()
    assert tuple(paused) == ("waiting", 1)  # pause consumed no permit

    _set_global(repo, frequency_mode="active")
    resumed = repo.claim_advice_attention(
        "u1", "windows", now=start + timedelta(hours=1, minutes=1)
    )
    assert resumed is not None and resumed.delivery_number == 2
    repo.close()


def test_guidance_from_android_ends_windows_reminder_and_keeps_advice_active(
    tmp_path: Path,
):
    repo = Repository(tmp_path / "guidance-cross-device.db")
    advice = _advice(repo, _goal(repo))
    start = datetime.now(timezone.utc)
    first = repo.claim_advice_attention("u1", "android", now=start)
    assert first is not None
    repo.complete_advice_attention(
        "u1",
        advice.id,
        "android",
        first.claim_token,
        now=start + timedelta(minutes=1),
    )

    updated = repo.record_feedback(
        "u1",
        advice.id,
        FeedbackCreate(kind="guidance", note="先给我成本最低的方向"),
        now=start + timedelta(minutes=10),
    )
    assert updated.status == AdviceStatus.ACTIVE
    assert (
        repo.claim_advice_attention(
            "u1", "windows", now=start + timedelta(hours=2)
        )
        is None
    )
    attention = repo._connection.execute(
        """SELECT state,delivery_count,resolved_reason,handling_kind
        FROM advice_attention WHERE advice_id=?""",
        (str(advice.id),),
    ).fetchone()
    assert tuple(attention) == ("resolved", 1, "feedback:guidance", None)
    feedback = repo.recent_feedback_for_goal("u1", advice.goal_id, limit=1)
    assert feedback[0]["kind"] == "guidance"
    assert feedback[0]["origin"] == "user"
    assert feedback[0]["signal_weight"] == 1.0
    repo.close()
