from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.models import (
    AdviceCandidate,
    AdviceLevel,
    AdviceRecord,
    AdviceStatus,
    ContextSnapshot,
    FeedbackCreate,
    Goal,
    OutcomeCreate,
    OutcomeStatus,
)
from app.service import ProactiveService
from app.storage import Repository


def _goal(repo: Repository, *, user_id: str = "u1", domain: str = "work") -> Goal:
    return repo.insert_goal(
        Goal(
            user_id=user_id,
            domain=domain,
            title=f"{domain} goal",
            quote=f"I will complete the {domain} goal",
        )
    )


def _candidate(
    goal: Goal,
    *,
    level: AdviceLevel = AdviceLevel.L2,
    dedupe_key: str = "topic:a",
    deadline: datetime | None = None,
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
                "event_id": uuid4(),
                "source": "test",
                "fact": "A concrete problem was observed",
                "observed_at": now,
                "confidence": 0.99,
            }
        ],
        action="Resolve the observed problem",
        first_step="Open the source and verify the fact",
        alternative="Record the reason for deferring it",
        prediction={
            "outcome": "The problem will remain unresolved",
            "deadline": deadline or now + timedelta(days=1),
            "confidence": 0.85,
        },
        adopted_expected_result="The problem is resolved and the result is recorded",
        adopted_confidence=0.80,
        urgency=0.9,
        impact=0.9,
        novelty=0.9,
        relevance=1.0,
        context_fit=1.0,
        interruption_cost=0.1,
        dedupe_key=dedupe_key,
    )


def _advice(
    repo: Repository,
    goal: Goal,
    *,
    level: AdviceLevel,
    dedupe_key: str,
    deadline: datetime | None = None,
) -> AdviceRecord:
    candidate = _candidate(
        goal,
        level=level,
        dedupe_key=dedupe_key,
        deadline=deadline,
    )
    record = AdviceRecord(
        **candidate.model_dump(),
        effective_level=level,
        proactive_score=0.9,
        delivery="immediate",
    )
    # Fixture seeding builds ledger state directly; it is not the publish
    # pipeline, so the advice-preference frequency gate does not apply here.
    return repo.insert_advice(record, enforce_publish_limits=False)


def _age_recent_publishes(repo: Repository, *, hours: int = 2) -> None:
    """Backdate published_at so the next real publish clears the preference
    cooldown — used where a test intentionally publishes twice in a row."""

    repo._connection.execute(
        "UPDATE advice SET published_at=? WHERE published_at IS NOT NULL",
        ((datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(),),
    )
    repo._connection.commit()


def _record_errors(
    repo: Repository,
    goal: Goal,
    level: AdviceLevel,
    count: int,
    *,
    now: datetime,
) -> None:
    for index in range(count):
        advice = _advice(
            repo,
            goal,
            level=level,
            dedupe_key=f"error:{level.name}:{index}:{uuid4()}",
        )
        repo.record_feedback(
            goal.user_id,
            advice.id,
            FeedbackCreate(kind="fact_error"),
            now=now,
        )


def test_new_governance_tables_are_added_to_an_existing_database(tmp_path: Path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE users (id TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
    connection.execute("INSERT INTO users VALUES ('legacy-user', '2026-01-01T00:00:00+00:00')")
    connection.commit()
    connection.close()

    repo = Repository(path)
    table_names = {
        row["name"]
        for row in repo._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    assert {"error_ledger", "speaking_freezes", "topic_suppressions"} <= table_names
    assert repo._connection.execute(
        "SELECT id FROM users WHERE id='legacy-user'"
    ).fetchone()["id"] == "legacy-user"
    repo.close()


def test_existing_error_ledger_is_deduplicated_before_unique_index_migration(
    tmp_path: Path,
):
    path = tmp_path / "duplicate-error-ledger.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE error_ledger (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          advice_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          domain TEXT NOT NULL,
          effective_level INTEGER NOT NULL,
          error_kind TEXT NOT NULL,
          created_at TEXT NOT NULL
        )"""
    )
    connection.executemany(
        """INSERT INTO error_ledger(
          advice_id,user_id,domain,effective_level,error_kind,created_at
        ) VALUES(?,?,?,?,?,?)""",
        (
            ("same-advice", "u1", "work", 3, "fact_error", "2026-01-01T00:00:00+00:00"),
            ("same-advice", "u1", "work", 3, "timing_error", "2026-01-02T00:00:00+00:00"),
            ("other-advice", "u1", "work", 3, "irrelevant", "2026-01-03T00:00:00+00:00"),
        ),
    )
    connection.commit()
    connection.close()

    repo = Repository(path)
    rows = repo._connection.execute(
        "SELECT advice_id,error_kind FROM error_ledger ORDER BY id"
    ).fetchall()

    assert [(row["advice_id"], row["error_kind"]) for row in rows] == [
        ("same-advice", "fact_error"),
    ]
    with pytest.raises(sqlite3.IntegrityError):
        repo._connection.execute(
            """INSERT INTO error_ledger(
              advice_id,user_id,domain,effective_level,error_kind,created_at
            ) VALUES(?,?,?,?,?,?)""",
            ("same-advice", "u1", "work", 3, "prediction_error", "2026-01-04T00:00:00+00:00"),
        )
    repo._connection.rollback()
    repo.close()


def test_legacy_irrelevant_feedback_is_preserved_but_removed_from_budget_and_freeze(
    tmp_path: Path,
):
    path = tmp_path / "legacy-irrelevant.db"
    repo = Repository(path)
    goal = _goal(repo)
    advice = _advice(
        repo,
        goal,
        level=AdviceLevel.L4,
        dedupe_key="legacy:irrelevant",
    )
    now = datetime.now(timezone.utc)
    repo._connection.execute(
        """INSERT INTO feedback(
          advice_id,user_id,kind,note,origin,signal_weight,created_at
        ) VALUES(?,?,?,?,?,?,?)""",
        (str(advice.id), "u1", "irrelevant", "not for me", "user", 1.0, now.isoformat()),
    )
    repo._connection.execute(
        """INSERT INTO error_ledger(
          advice_id,user_id,domain,effective_level,error_kind,created_at
        ) VALUES(?,?,?,?,?,?)""",
        (str(advice.id), "u1", "work", 4, "irrelevant", now.isoformat()),
    )
    repo._connection.execute(
        """INSERT INTO speaking_freezes(
          user_id,level,error_count,allowed_errors,window_started_at,
          frozen_at,expires_at,reason
        ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            "u1",
            4,
            1,
            0,
            (now - timedelta(days=30)).isoformat(),
            now.isoformat(),
            (now + timedelta(days=7)).isoformat(),
            "ERROR_BUDGET_L4_EXCEEDED",
        ),
    )
    repo._connection.commit()
    repo.close()

    migrated = Repository(path)
    preserved = migrated._connection.execute(
        "SELECT kind,note FROM feedback WHERE advice_id=?",
        (str(advice.id),),
    ).fetchall()
    assert [tuple(row) for row in preserved] == [("irrelevant", "not for me")]
    assert migrated.error_count("u1", AdviceLevel.L4) == 0
    assert migrated.active_speaking_freeze("u1", AdviceLevel.L4, now=now) is None
    signal = migrated._connection.execute(
        """SELECT signal_kind,origin,weight FROM relevance_ledger
        WHERE advice_id=?""",
        (str(advice.id),),
    ).fetchone()
    assert tuple(signal) == ("irrelevant", "user", 1.0)
    migrated.close()


def test_service_holds_a_new_candidate_without_an_adopted_result_prediction(tmp_path: Path):
    repo = Repository(tmp_path / "missing-adopted-result.db")
    goal = _goal(repo)
    incomplete = _candidate(goal, dedupe_key="missing:adopted").model_copy(
        update={"adopted_expected_result": None, "adopted_confidence": None}
    )

    result = ProactiveService(repo).evaluate(incomplete, ContextSnapshot())

    assert result.decision == "hold"
    assert result.advice is None
    assert result.reason_codes == ["ADOPTED_RESULT_PREDICTION_REQUIRED"]
    assert repo.list_advice("u1") == []
    repo.close()


def test_repository_rejects_new_advice_without_an_adopted_result_prediction(tmp_path: Path):
    repo = Repository(tmp_path / "repository-invariant.db")
    goal = _goal(repo)
    candidate = _candidate(goal, dedupe_key="repository:missing")
    incomplete = AdviceRecord(
        **candidate.model_dump(),
        effective_level=AdviceLevel.L2,
        proactive_score=0.9,
        delivery="immediate",
    ).model_copy(update={"adopted_expected_result": None, "adopted_confidence": None})

    with pytest.raises(ValueError, match="adopted-result prediction"):
        repo.insert_advice(incomplete)

    assert repo.list_advice("u1") == []
    repo.close()


def test_legacy_advice_without_adopted_result_fields_remains_readable(tmp_path: Path):
    repo = Repository(tmp_path / "legacy-advice.db")
    goal = _goal(repo)
    candidate = _candidate(goal, dedupe_key="legacy:advice")
    legacy = AdviceRecord(
        **candidate.model_dump(),
        effective_level=AdviceLevel.L2,
        proactive_score=0.9,
        delivery="immediate",
    ).model_copy(update={"adopted_expected_result": None, "adopted_confidence": None})
    repo._connection.execute(
        """INSERT INTO advice(
          id,user_id,domain,level,dedupe_key,status,delivery,
          prediction_confidence,prediction_deadline,payload_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(legacy.id),
            legacy.user_id,
            legacy.domain,
            int(legacy.effective_level),
            legacy.dedupe_key,
            legacy.status.value,
            legacy.delivery,
            legacy.prediction.confidence,
            legacy.prediction.deadline.isoformat(),
            legacy.model_dump_json(),
            legacy.created_at.isoformat(),
        ),
    )
    repo._connection.commit()

    loaded = repo.get_advice("u1", legacy.id)

    assert loaded is not None
    assert loaded.adopted_expected_result is None
    assert loaded.adopted_confidence is None
    repo.close()


@pytest.mark.parametrize(
    ("level", "allowed_errors"),
    (
        (AdviceLevel.L2, 2),
        (AdviceLevel.L3, 1),
        (AdviceLevel.L4, 0),
    ),
)
def test_error_budget_freezes_only_after_the_level_boundary_is_exceeded(
    tmp_path: Path,
    level: AdviceLevel,
    allowed_errors: int,
):
    repo = Repository(tmp_path / f"{level.name}.db")
    goal = _goal(repo)
    now = datetime.now(timezone.utc)

    _record_errors(repo, goal, level, allowed_errors, now=now)

    assert repo.error_count("u1", level) == allowed_errors
    assert repo.active_speaking_freeze("u1", level, now=now) is None

    _record_errors(repo, goal, level, 1, now=now)

    freeze = repo.active_speaking_freeze("u1", level, now=now)
    assert freeze is not None
    assert freeze["error_count"] == allowed_errors + 1
    assert freeze["allowed_errors"] == allowed_errors
    assert freeze["reason"] == f"ERROR_BUDGET_{level.name}_EXCEEDED"
    assert datetime.fromisoformat(freeze["expires_at"]) == now + timedelta(days=7)
    repo.close()


@pytest.mark.parametrize(
    "kind",
    ("fact_error", "prediction_error", "timing_error"),
)
def test_each_declared_error_kind_enters_the_error_ledger(tmp_path: Path, kind: str):
    repo = Repository(tmp_path / f"{kind}.db")
    goal = _goal(repo)
    advice = _advice(repo, goal, level=AdviceLevel.L2, dedupe_key=f"kind:{kind}")

    repo.record_feedback("u1", advice.id, FeedbackCreate(kind=kind))

    assert repo.error_count("u1", AdviceLevel.L2) == 1
    stored = repo._connection.execute(
        "SELECT error_kind, effective_level FROM error_ledger"
    ).fetchone()
    assert stored["error_kind"] == kind
    assert stored["effective_level"] == int(AdviceLevel.L2)
    repo.close()


def test_irrelevant_is_a_relevance_error_not_a_hard_error_budget_event(tmp_path: Path):
    repo = Repository(tmp_path / "irrelevant-relevance.db")
    goal = _goal(repo)
    advice = _advice(
        repo,
        goal,
        level=AdviceLevel.L4,
        dedupe_key="kind:irrelevant-relevance",
    )

    dismissed = repo.record_feedback(
        "u1", advice.id, FeedbackCreate(kind="irrelevant")
    )

    assert dismissed.status == AdviceStatus.DISMISSED
    assert repo.error_count("u1", AdviceLevel.L4) == 0
    assert repo.active_speaking_freeze("u1", AdviceLevel.L4) is None
    row = repo._connection.execute(
        """SELECT signal_kind,origin,weight FROM relevance_ledger
        WHERE advice_id=?""",
        (str(advice.id),),
    ).fetchone()
    assert dict(row) == {
        "signal_kind": "irrelevant",
        "origin": "user",
        "weight": 1.0,
    }
    repo.close()


def test_error_feedback_dismisses_advice_releases_topic_and_blocks_late_adoption(
    tmp_path: Path,
):
    repo = Repository(tmp_path / "error-dismissal.db")
    goal = _goal(repo)
    service = ProactiveService(repo)
    advice = _advice(repo, goal, level=AdviceLevel.L2, dedupe_key="error:retry-topic")

    repo.record_feedback("u1", advice.id, FeedbackCreate(kind="fact_error"))

    dismissed = repo.get_advice("u1", advice.id)
    assert dismissed is not None and dismissed.status == AdviceStatus.DISMISSED
    assert repo.active_duplicate("u1", advice.dedupe_key) is False
    assert repo._connection.execute(
        "SELECT COUNT(*) AS count FROM advice WHERE status='active'"
    ).fetchone()["count"] == 0

    with pytest.raises(ValueError, match="already dismissed"):
        repo.record_feedback("u1", advice.id, FeedbackCreate(kind="adopted"))
    assert repo.get_advice("u1", advice.id).status == AdviceStatus.DISMISSED

    _age_recent_publishes(repo)  # clear the per-goal preference cooldown
    replacement = service.evaluate(
        _candidate(goal, level=AdviceLevel.L2, dedupe_key=advice.dedupe_key),
        ContextSnapshot(),
    )
    assert replacement.decision == "publish"
    assert replacement.advice is not None
    assert replacement.advice.status == AdviceStatus.ACTIVE
    repo.close()


def test_error_feedback_freeze_and_dismissal_commit_together_then_allow_regeneration(
    tmp_path: Path,
):
    repo = Repository(tmp_path / "error-freeze-dismissal.db")
    goal = _goal(repo)
    keys = [f"error:budget:{index}" for index in range(3)]
    advice_records = [
        _advice(repo, goal, level=AdviceLevel.L2, dedupe_key=key)
        for key in keys
    ]

    for advice in advice_records:
        repo.record_feedback("u1", advice.id, FeedbackCreate(kind="timing_error"))

    assert repo.error_count("u1", AdviceLevel.L2) == 3
    assert repo.active_speaking_freeze("u1", AdviceLevel.L2) is not None
    assert all(
        repo.get_advice("u1", advice.id).status == AdviceStatus.DISMISSED
        for advice in advice_records
    )
    assert all(not repo.active_duplicate("u1", key) for key in keys)
    assert repo._connection.execute(
        "SELECT COUNT(*) AS count FROM advice WHERE status='active'"
    ).fetchone()["count"] == 0

    _age_recent_publishes(repo)  # clear the per-goal preference cooldown
    replacement = ProactiveService(repo).evaluate(
        _candidate(goal, level=AdviceLevel.L2, dedupe_key=keys[-1]),
        ContextSnapshot(),
    )
    assert replacement.decision == "publish"
    assert replacement.effective_level == AdviceLevel.L1
    assert replacement.advice is not None and replacement.advice.status == AdviceStatus.ACTIVE
    assert "ERROR_BUDGET_L2_FROZEN" in replacement.reason_codes
    repo.close()


def test_error_feedback_rolls_back_ledger_freeze_and_feedback_if_dismissal_fails(
    tmp_path: Path,
):
    repo = Repository(tmp_path / "error-atomicity.db")
    goal = _goal(repo)
    advice = _advice(repo, goal, level=AdviceLevel.L4, dedupe_key="error:atomic")
    repo._connection.execute(
        """CREATE TRIGGER reject_error_dismissal
        BEFORE UPDATE OF status ON advice
        WHEN NEW.status='dismissed'
        BEGIN
          SELECT RAISE(ABORT, 'forced dismissal failure');
        END"""
    )
    repo._connection.commit()

    with pytest.raises(sqlite3.DatabaseError, match="forced dismissal failure"):
        repo.record_feedback("u1", advice.id, FeedbackCreate(kind="prediction_error"))

    assert repo.error_count("u1", AdviceLevel.L4) == 0
    assert repo.active_speaking_freeze("u1", AdviceLevel.L4) is None
    assert repo._connection.execute(
        "SELECT COUNT(*) AS count FROM feedback"
    ).fetchone()["count"] == 0
    assert repo.get_advice("u1", advice.id).status == AdviceStatus.ACTIVE
    repo.close()


def test_retried_or_changed_error_feedback_counts_an_advice_only_once(tmp_path: Path):
    repo = Repository(tmp_path / "idempotent-error.db")
    goal = _goal(repo)
    advice = _advice(repo, goal, level=AdviceLevel.L2, dedupe_key="idempotent:error")

    dismissed = repo.record_feedback("u1", advice.id, FeedbackCreate(kind="fact_error"))
    retried = repo.record_feedback("u1", advice.id, FeedbackCreate(kind="fact_error"))
    with pytest.raises(ValueError, match="already dismissed"):
        repo.record_feedback("u1", advice.id, FeedbackCreate(kind="timing_error"))

    assert dismissed.status == AdviceStatus.DISMISSED
    assert retried == dismissed
    assert repo.error_count("u1", AdviceLevel.L2) == 1
    assert repo.active_speaking_freeze("u1", AdviceLevel.L2) is None
    assert repo._connection.execute(
        "SELECT COUNT(*) AS count FROM feedback WHERE advice_id=?",
        (str(advice.id),),
    ).fetchone()["count"] == 1
    repo.close()


def test_guidance_can_be_added_to_historical_advice_without_changing_its_status(
    tmp_path: Path,
):
    repo = Repository(tmp_path / "historical-guidance.db")
    goal = _goal(repo)
    advice = _advice(
        repo,
        goal,
        level=AdviceLevel.L2,
        dedupe_key="guidance:historical",
    )
    repo.record_feedback("u1", advice.id, FeedbackCreate(kind="dismissed"))

    guided = repo.record_feedback(
        "u1",
        advice.id,
        FeedbackCreate(kind="guidance", note="Prioritize timing and resource cost."),
    )

    assert guided.status == AdviceStatus.DISMISSED
    stored = repo._connection.execute(
        "SELECT kind,note FROM feedback WHERE advice_id=? ORDER BY id",
        (str(advice.id),),
    ).fetchall()
    assert [(row["kind"], row["note"]) for row in stored] == [
        ("dismissed", None),
        ("guidance", "Prioritize timing and resource cost."),
    ]
    with pytest.raises(ValueError, match="already dismissed"):
        repo.record_feedback("u1", advice.id, FeedbackCreate(kind="useful"))
    repo.close()


def test_later_snoozes_for_four_hours_or_until_the_prediction_deadline(
    tmp_path: Path,
):
    repo = Repository(tmp_path / "later-deadline.db")
    goal = _goal(repo)
    now = datetime.now(timezone.utc)
    long_deadline = now + timedelta(hours=12)
    short_deadline = now + timedelta(hours=2)
    long_advice = _advice(
        repo,
        goal,
        level=AdviceLevel.L2,
        dedupe_key="later:four-hours",
        deadline=long_deadline,
    )
    short_advice = _advice(
        repo,
        goal,
        level=AdviceLevel.L2,
        dedupe_key="later:deadline",
        deadline=short_deadline,
    )

    long_result = repo.record_feedback(
        "u1", long_advice.id, FeedbackCreate(kind="later"), now=now
    )
    short_result = repo.record_feedback(
        "u1", short_advice.id, FeedbackCreate(kind="later"), now=now
    )

    assert long_result.status == AdviceStatus.ACTIVE
    assert long_result.snoozed_until == now + timedelta(hours=4)
    assert short_result.status == AdviceStatus.ACTIVE
    assert short_result.snoozed_until == short_deadline
    repo.close()


def test_later_does_not_extend_expired_advice(tmp_path: Path):
    repo = Repository(tmp_path / "later-expired.db")
    goal = _goal(repo)
    now = datetime.now(timezone.utc)
    advice = _advice(
        repo,
        goal,
        level=AdviceLevel.L2,
        dedupe_key="later:expired",
        deadline=now - timedelta(minutes=1),
    )

    result = repo.record_feedback(
        "u1", advice.id, FeedbackCreate(kind="later"), now=now
    )

    assert result.status == AdviceStatus.ACTIVE
    assert result.snoozed_until is None
    listed = {item.id: item for item in repo.list_advice("u1")}
    assert listed[advice.id].status == AdviceStatus.WITHDRAWN
    repo.close()


@pytest.mark.parametrize(
    ("kind", "expected_status"),
    (
        ("adopted", AdviceStatus.ADOPTED),
        ("fact_error", AdviceStatus.DISMISSED),
        ("dismissed", AdviceStatus.DISMISSED),
        ("stop_topic", AdviceStatus.DISMISSED),
    ),
)
def test_terminal_feedback_clears_an_existing_snooze(
    tmp_path: Path,
    kind: str,
    expected_status: AdviceStatus,
):
    repo = Repository(tmp_path / f"clear-snooze-{kind}.db")
    goal = _goal(repo)
    now = datetime.now(timezone.utc)
    advice = _advice(
        repo,
        goal,
        level=AdviceLevel.L2,
        dedupe_key=f"clear-snooze:{kind}",
        deadline=now + timedelta(days=1),
    )
    snoozed = repo.record_feedback(
        "u1", advice.id, FeedbackCreate(kind="later"), now=now
    )
    assert snoozed.snoozed_until == now + timedelta(hours=4)

    resolved = repo.record_feedback(
        "u1",
        advice.id,
        FeedbackCreate(kind=kind),
        now=now + timedelta(minutes=1),
    )

    assert resolved.status == expected_status
    assert resolved.snoozed_until is None
    assert repo.get_advice("u1", advice.id) == resolved
    repo.close()


def test_recent_feedback_is_filtered_sorted_limited_and_keeps_empty_notes(
    tmp_path: Path,
):
    repo = Repository(tmp_path / "recent-feedback.db")
    goal = _goal(repo)
    other_goal = _goal(repo, domain="health")
    other_user_goal = _goal(repo, user_id="u2")
    advice = _advice(
        repo,
        goal,
        level=AdviceLevel.L2,
        dedupe_key="recent:target",
    )
    other_goal_advice = _advice(
        repo,
        other_goal,
        level=AdviceLevel.L2,
        dedupe_key="recent:other-goal",
    )
    other_user_advice = _advice(
        repo,
        other_user_goal,
        level=AdviceLevel.L2,
        dedupe_key="recent:other-user",
    )
    now = datetime.now(timezone.utc)
    repo.record_feedback(
        "u1", advice.id, FeedbackCreate(kind="useful"), now=now
    )
    repo.record_feedback(
        "u1",
        advice.id,
        FeedbackCreate(kind="guidance", note="Look at resource constraints."),
        now=now + timedelta(minutes=1),
    )
    repo.record_feedback(
        "u1", advice.id, FeedbackCreate(kind="later"), now=now + timedelta(minutes=2)
    )
    repo.record_feedback(
        "u1",
        other_goal_advice.id,
        FeedbackCreate(kind="guidance", note="Different goal."),
        now=now + timedelta(minutes=3),
    )
    repo.record_feedback(
        "u2",
        other_user_advice.id,
        FeedbackCreate(kind="guidance", note="Different user."),
        now=now + timedelta(minutes=4),
    )

    recent = repo.recent_feedback_for_goal("u1", goal.id, limit=2)

    assert recent == [
        {
            "kind": "later",
            "note": None,
            "origin": "user",
            "signal_weight": 1.0,
            "created_at": (now + timedelta(minutes=2)).isoformat(),
        },
        {
            "kind": "guidance",
            "note": "Look at resource constraints.",
            "origin": "user",
            "signal_weight": 1.0,
            "created_at": (now + timedelta(minutes=1)).isoformat(),
        },
    ]
    assert repo.recent_feedback_for_goal("u1", goal.id, limit=0) == []
    for index in range(25):
        repo.record_feedback(
            "u1",
            advice.id,
            FeedbackCreate(kind="guidance", note=f"Direction {index}"),
            now=now + timedelta(minutes=10 + index),
        )
    bounded = repo.recent_feedback_for_goal("u1", goal.id, limit=100)
    assert len(bounded) == 20
    assert bounded[0]["note"] == "Direction 24"
    assert bounded[-1]["note"] == "Direction 5"
    repo.close()


@pytest.mark.parametrize("kind", ("adopted", "useful", "dismissed"))
def test_non_error_feedback_does_not_spend_error_budget(tmp_path: Path, kind: str):
    repo = Repository(tmp_path / f"safe-{kind}.db")
    goal = _goal(repo)
    advice = _advice(repo, goal, level=AdviceLevel.L4, dedupe_key=f"safe:{kind}")

    repo.record_feedback("u1", advice.id, FeedbackCreate(kind=kind))

    assert repo.error_count("u1", AdviceLevel.L4) == 0
    assert repo.active_speaking_freeze("u1", AdviceLevel.L4) is None
    repo.close()


def test_adopted_incorrect_outcomes_spend_l3_budget_once_per_advice(tmp_path: Path):
    repo = Repository(tmp_path / "adopted-outcome-l3.db")
    goal = _goal(repo)

    first = _advice(repo, goal, level=AdviceLevel.L3, dedupe_key="outcome:l3:first")
    repo.record_feedback("u1", first.id, FeedbackCreate(kind="adopted"))
    first_adopted = repo.get_advice("u1", first.id)
    outcome = OutcomeCreate(
        status=OutcomeStatus.INCORRECT,
        actual_result="The adopted result was not achieved",
    )

    assert repo.record_outcome("u1", first_adopted, outcome) is True
    assert repo.record_outcome("u1", first_adopted, outcome) is False
    assert repo.error_count("u1", AdviceLevel.L3) == 1
    assert repo.active_speaking_freeze("u1", AdviceLevel.L3) is None

    second = _advice(repo, goal, level=AdviceLevel.L3, dedupe_key="outcome:l3:second")
    repo.record_feedback("u1", second.id, FeedbackCreate(kind="adopted"))
    second_adopted = repo.get_advice("u1", second.id)
    assert repo.record_outcome("u1", second_adopted, outcome) is True

    assert repo.error_count("u1", AdviceLevel.L3) == 2
    assert repo.active_speaking_freeze("u1", AdviceLevel.L3) is not None
    kinds = {
        row["error_kind"]
        for row in repo._connection.execute(
            "SELECT error_kind FROM error_ledger WHERE user_id='u1'"
        ).fetchall()
    }
    assert kinds == {"prediction_error"}
    repo.close()


def test_outcome_failure_rolls_back_every_side_effect_and_retry_succeeds(
    tmp_path: Path,
):
    repo = Repository(tmp_path / "outcome-atomicity.db")
    goal = _goal(repo)
    advice = _advice(repo, goal, level=AdviceLevel.L4, dedupe_key="outcome:atomic")
    repo.record_feedback("u1", advice.id, FeedbackCreate(kind="adopted"))
    adopted = repo.get_advice("u1", advice.id)
    outcome = OutcomeCreate(
        status=OutcomeStatus.INCORRECT,
        actual_result="The expected adopted result was not achieved",
    )
    repo._connection.execute(
        """CREATE TRIGGER reject_outcome_verification
        BEFORE UPDATE OF status ON advice
        WHEN NEW.status='verified'
        BEGIN
          SELECT RAISE(ABORT, 'forced outcome verification failure');
        END"""
    )
    repo._connection.commit()

    with pytest.raises(sqlite3.DatabaseError, match="forced outcome verification failure"):
        repo.record_outcome("u1", adopted, outcome)

    assert repo._connection.in_transaction is False
    assert repo._connection.execute(
        "SELECT COUNT(*) AS count FROM outcomes WHERE advice_id=?",
        (str(advice.id),),
    ).fetchone()["count"] == 0
    assert repo.error_count("u1", AdviceLevel.L4) == 0
    assert repo.active_speaking_freeze("u1", AdviceLevel.L4) is None
    assert repo._connection.execute(
        "SELECT COUNT(*) AS count FROM trust_accounts WHERE user_id='u1'"
    ).fetchone()["count"] == 0
    assert repo.get_advice("u1", advice.id).status == AdviceStatus.ADOPTED

    repo._connection.execute("DROP TRIGGER reject_outcome_verification")
    repo._connection.commit()
    assert repo.record_outcome("u1", adopted, outcome) is True
    assert repo.record_outcome("u1", adopted, outcome) is False

    assert repo._connection.execute(
        "SELECT COUNT(*) AS count FROM outcomes WHERE advice_id=?",
        (str(advice.id),),
    ).fetchone()["count"] == 1
    assert repo.error_count("u1", AdviceLevel.L4) == 1
    assert repo.active_speaking_freeze("u1", AdviceLevel.L4) is not None
    assert repo.trust_summary("u1", "work", AdviceLevel.L4).judged == 1
    assert repo.get_advice("u1", advice.id).status == AdviceStatus.VERIFIED
    repo.close()


def test_correct_adopted_and_unadopted_risk_outcomes_do_not_spend_error_budget(
    tmp_path: Path,
):
    repo = Repository(tmp_path / "outcome-semantics.db")
    goal = _goal(repo)
    adopted = _advice(repo, goal, level=AdviceLevel.L4, dedupe_key="outcome:correct")
    repo.record_feedback("u1", adopted.id, FeedbackCreate(kind="adopted"))
    adopted = repo.get_advice("u1", adopted.id)
    unadopted = _advice(repo, goal, level=AdviceLevel.L4, dedupe_key="outcome:risk")

    repo.record_outcome(
        "u1",
        adopted,
        OutcomeCreate(status=OutcomeStatus.CORRECT, actual_result="Adopted result achieved"),
    )
    repo.record_outcome(
        "u1",
        unadopted,
        OutcomeCreate(status=OutcomeStatus.INCORRECT, actual_result="Untreated risk did not occur"),
    )

    assert repo.error_count("u1", AdviceLevel.L4) == 0
    assert repo.active_speaking_freeze("u1", AdviceLevel.L4) is None
    repo.close()


def test_android_style_outcome_api_is_idempotent_and_freezes_l4(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "outcome-api.db"
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(path))
    monkeypatch.delenv("MOUCHEN_API_TOKEN", raising=False)
    monkeypatch.delenv("MOUCHEN_API_BEARER_TOKEN", raising=False)
    import app.main as main

    main.repo.close()
    main.repo = main.Repository(path)
    main.service = main.ProactiveService(main.repo)
    goal = _goal(main.repo)
    advice = _advice(
        main.repo,
        goal,
        level=AdviceLevel.L4,
        dedupe_key="android:outcome:l4",
    )
    main.repo.record_feedback("u1", advice.id, FeedbackCreate(kind="adopted"))
    body = {
        "status": "incorrect",
        "actual_result": "The expected adopted result was not achieved",
    }

    with TestClient(main.app) as client:
        first = client.post(
            f"/v1/advice/{advice.id}/outcome",
            headers={"X-User-Id": "u1"},
            json=body,
        )
        retry = client.post(
            f"/v1/advice/{advice.id}/outcome",
            headers={"X-User-Id": "u1"},
            json=body,
        )
        assert first.status_code == 200
        assert retry.status_code == 200
        assert main.repo.error_count("u1", AdviceLevel.L4) == 1
        assert main.repo.active_speaking_freeze("u1", AdviceLevel.L4) is not None
        assert main.repo._connection.execute(
            "SELECT COUNT(*) AS count FROM outcomes WHERE advice_id=?",
            (str(advice.id),),
        ).fetchone()["count"] == 1


def test_budget_uses_a_rolling_window(tmp_path: Path):
    repo = Repository(tmp_path / "rolling.db")
    goal = _goal(repo)
    now = datetime.now(timezone.utc)
    _record_errors(repo, goal, AdviceLevel.L2, 2, now=now - timedelta(days=8))
    _record_errors(repo, goal, AdviceLevel.L2, 2, now=now)

    assert repo.error_count("u1", AdviceLevel.L2) == 4
    assert repo.active_speaking_freeze("u1", AdviceLevel.L2, now=now) is None

    _record_errors(repo, goal, AdviceLevel.L2, 1, now=now)
    assert repo.active_speaking_freeze("u1", AdviceLevel.L2, now=now) is not None
    repo.close()


def test_active_freeze_downgrades_other_domains_instead_of_silencing(tmp_path: Path):
    repo = Repository(tmp_path / "all-domain.db")
    work_goal = _goal(repo, domain="work")
    health_goal = _goal(repo, domain="health")
    now = datetime.now(timezone.utc)
    _record_errors(repo, work_goal, AdviceLevel.L2, 3, now=now)
    repo.set_charter("u1", "health", AdviceLevel.L4, True)

    result = ProactiveService(repo).evaluate(
        _candidate(health_goal, level=AdviceLevel.L2, dedupe_key="health:new"),
        ContextSnapshot(),
    )

    assert result.decision == "publish"
    assert result.advice is not None
    assert result.effective_level == AdviceLevel.L1
    assert result.advice.delivery == "brief"
    assert "ERROR_BUDGET_L2_FROZEN" in result.reason_codes
    assert "ERROR_BUDGET_DOWNGRADED" in result.reason_codes
    repo.close()


def test_expired_freeze_restores_the_level(tmp_path: Path):
    repo = Repository(tmp_path / "expired-freeze.db")
    goal = _goal(repo)
    now = datetime.now(timezone.utc)
    _record_errors(repo, goal, AdviceLevel.L2, 3, now=now)

    level, frozen = repo.available_speaking_level(
        "u1",
        AdviceLevel.L2,
        now=now + timedelta(days=7, seconds=1),
    )

    assert level == AdviceLevel.L2
    assert frozen == ()
    repo.close()


@pytest.mark.parametrize(
    ("proposed", "frozen_levels", "expected"),
    (
        (AdviceLevel.L3, (AdviceLevel.L2,), AdviceLevel.L1),
        (AdviceLevel.L4, (AdviceLevel.L3,), AdviceLevel.L2),
        (AdviceLevel.L4, (AdviceLevel.L4, AdviceLevel.L2), AdviceLevel.L1),
        (
            AdviceLevel.L4,
            (AdviceLevel.L4, AdviceLevel.L3, AdviceLevel.L2),
            AdviceLevel.L1,
        ),
    ),
)
def test_any_frozen_tier_below_a_candidate_is_a_ceiling_barrier(
    tmp_path: Path,
    proposed: AdviceLevel,
    frozen_levels: tuple[AdviceLevel, ...],
    expected: AdviceLevel,
):
    repo = Repository(tmp_path / f"barrier-{proposed.name}-{len(frozen_levels)}.db")
    goal = _goal(repo)
    now = datetime.now(timezone.utc)
    errors_to_freeze = {
        AdviceLevel.L2: 3,
        AdviceLevel.L3: 2,
        AdviceLevel.L4: 1,
    }
    for level in frozen_levels:
        _record_errors(repo, goal, level, errors_to_freeze[level], now=now)

    available, observed_frozen = repo.available_speaking_level(
        "u1",
        proposed,
        now=now,
    )

    assert available == expected
    assert observed_frozen == tuple(
        sorted(frozen_levels, key=int, reverse=True)
    )
    repo.close()


def test_service_cannot_publish_l3_across_an_l2_freeze(tmp_path: Path):
    repo = Repository(tmp_path / "service-barrier.db")
    goal = _goal(repo)
    now = datetime.now(timezone.utc)
    _record_errors(repo, goal, AdviceLevel.L2, 3, now=now)
    repo.set_charter("u1", "work", AdviceLevel.L4, True)
    repo._connection.execute(
        """INSERT INTO trust_accounts VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id,domain,level) DO UPDATE SET
          judged=excluded.judged,correct=excluded.correct,brier_sum=excluded.brier_sum,
          utility_sum=excluded.utility_sum,timing_sum=excluded.timing_sum,
          catastrophic_errors=excluded.catastrophic_errors,updated_at=excluded.updated_at""",
        ("u1", "work", 2, 100, 99, 5.0, 90.0, 90.0, 0, now.isoformat()),
    )
    repo._connection.commit()

    result = ProactiveService(repo).evaluate(
        _candidate(goal, level=AdviceLevel.L3, dedupe_key="barrier:l3"),
        ContextSnapshot(),
    )

    assert result.decision == "publish"
    assert result.effective_level == AdviceLevel.L1
    assert result.advice is not None and result.advice.effective_level == AdviceLevel.L1
    assert "ERROR_BUDGET_L2_FROZEN" in result.reason_codes
    repo.close()


def test_stop_topic_suppresses_only_the_exact_dedupe_key_for_thirty_days(tmp_path: Path):
    repo = Repository(tmp_path / "suppression.db")
    goal = _goal(repo)
    service = ProactiveService(repo)
    now = datetime.now(timezone.utc)
    original = service.evaluate(
        _candidate(goal, dedupe_key="topic:stop"),
        ContextSnapshot(),
    )
    assert original.advice is not None

    repo.record_feedback(
        "u1",
        original.advice.id,
        FeedbackCreate(kind="stop_topic"),
        now=now,
    )

    suppression = repo.active_topic_suppression("u1", "topic:stop", now=now)
    assert suppression is not None
    assert suppression["reason"] == "USER_STOP_TOPIC"
    assert datetime.fromisoformat(suppression["expires_at"]) >= now + timedelta(days=30)

    _age_recent_publishes(repo)  # clear the per-goal preference cooldown
    same_topic = service.evaluate(
        _candidate(goal, dedupe_key="topic:stop"),
        ContextSnapshot(),
    )
    other_topic = service.evaluate(
        _candidate(goal, dedupe_key="topic:other"),
        ContextSnapshot(),
    )

    assert same_topic.decision == "hold"
    assert same_topic.advice is None
    assert "TOPIC_SUPPRESSED_BY_USER" in same_topic.reason_codes
    assert other_topic.decision == "publish"
    assert other_topic.advice is not None
    assert repo.active_topic_suppression(
        "u1",
        "topic:stop",
        now=now + timedelta(days=30, seconds=1),
    ) is None
    repo.close()
