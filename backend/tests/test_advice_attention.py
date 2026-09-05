from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.models import (
    AdviceLevel,
    AdviceRecord,
    AdviceStatus,
    FeedbackCreate,
    Goal,
)
from app.storage import AttentionConflict, Repository


def _goal(repo: Repository, *, user_id: str = "u1") -> Goal:
    return repo.insert_goal(
        Goal(
            user_id=user_id,
            domain="work",
            title="Ship safely",
            quote="Ship the release without repeating avoidable failures",
        )
    )


def _advice(
    repo: Repository,
    goal: Goal,
    *,
    level: AdviceLevel = AdviceLevel.L2,
    dedupe_key: str = "problem:deadline:aaaaaaaaaaaaaaaa",
    action: str = "Review the blocked release and choose one recovery path",
    created_at: datetime | None = None,
    prediction_deadline: datetime | None = None,
) -> AdviceRecord:
    now = created_at or datetime.now(timezone.utc)
    return repo.insert_advice(
        AdviceRecord(
            user_id=goal.user_id,
            domain=goal.domain,
            requested_level=level,
            goal_id=goal.id,
            goal_quote=goal.quote,
            evidence=[
                {
                    "event_id": uuid4(),
                    "source": "android.notification",
                    "fact": "The release remains blocked",
                    "observed_at": now,
                    "confidence": 0.95,
                }
            ],
            action=action,
            first_step="Open the failure detail",
            alternative="Roll back to the last known good release",
            prediction={
                "outcome": "The release remains blocked",
                "deadline": prediction_deadline or now + timedelta(days=3),
                "confidence": 0.85,
            },
            adopted_expected_result="The release has a verified recovery path",
            adopted_confidence=0.8,
            urgency=0.85,
            impact=0.85,
            novelty=0.8,
            relevance=0.95,
            context_fit=1.0,
            interruption_cost=0.1,
            dedupe_key=dedupe_key,
            effective_level=level,
            proactive_score=0.9,
            delivery="immediate",
            created_at=now,
        )
    )


def test_attention_issues_exactly_two_global_permits_one_hour_apart(
    tmp_path: Path,
):
    repo = Repository(tmp_path / "two-deliveries.db")
    advice = _advice(repo, _goal(repo), level=AdviceLevel.L4)
    start = datetime(2026, 8, 9, 1, 0, tzinfo=timezone.utc)

    first = repo.claim_advice_attention("u1", "android", now=start)
    assert first is not None and first.delivery_number == 1
    completed = repo.complete_advice_attention(
        "u1",
        advice.id,
        "android",
        first.claim_token,
        now=start + timedelta(minutes=1),
    )
    assert completed["status"] == "delivered"
    assert completed["delivery_count"] == 1

    assert (
        repo.claim_advice_attention(
            "u1", "windows", now=start + timedelta(minutes=59, seconds=59)
        )
        is None
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

    assert (
        repo.claim_advice_attention(
            "u1", "ios", now=start + timedelta(hours=1, minutes=59, seconds=59)
        )
        is None
    )
    assert repo.get_advice("u1", advice.id).status == AdviceStatus.ACTIVE

    # The first poll at the exact one-hour timeout performs the automatic
    # terminal transition; it never returns a third delivery.
    assert (
        repo.claim_advice_attention(
            "u1", "ios", now=start + timedelta(hours=2)
        )
        is None
    )
    assert repo.get_advice("u1", advice.id).status == AdviceStatus.DISMISSED
    feedback = repo._connection.execute(
        """SELECT kind,origin,signal_weight FROM feedback
        WHERE advice_id=?""",
        (str(advice.id),),
    ).fetchall()
    assert [tuple(row) for row in feedback] == [("auto_irrelevant", "system", 0.25)]
    assert repo.error_count("u1", AdviceLevel.L4) == 0
    attention = repo._connection.execute(
        """SELECT state,delivery_count,resolved_reason FROM advice_attention
        WHERE advice_id=?""",
        (str(advice.id),),
    ).fetchone()
    assert tuple(attention) == ("resolved", 2, "ignored_timeout")
    repo.close()


def test_expired_or_failed_claim_never_refunds_its_delivery_number(tmp_path: Path):
    repo = Repository(tmp_path / "expired-permit.db")
    advice = _advice(repo, _goal(repo))
    start = datetime(2026, 8, 9, 5, 0, tzinfo=timezone.utc)

    first = repo.claim_advice_attention("u1", "android", now=start)
    assert first is not None and first.delivery_number == 1
    assert repo.sweep_advice_attention(
        "u1", now=start + timedelta(minutes=16)
    ) == 0
    attempt = repo._connection.execute(
        "SELECT state FROM advice_attention_attempts WHERE claim_token=?",
        (str(first.claim_token),),
    ).fetchone()
    assert attempt["state"] == "superseded"
    attention = repo._connection.execute(
        "SELECT state,delivery_count FROM advice_attention WHERE advice_id=?",
        (str(advice.id),),
    ).fetchone()
    assert tuple(attention) == ("waiting", 1)
    assert (
        repo.claim_advice_attention(
            "u1", "windows", now=start + timedelta(minutes=59, seconds=59)
        )
        is None
    )

    second = repo.claim_advice_attention(
        "u1", "windows", now=start + timedelta(hours=1)
    )
    assert second is not None and second.delivery_number == 2
    failed = repo.fail_advice_attention(
        "u1",
        advice.id,
        "windows",
        second.claim_token,
        reason="platform notification call failed",
        now=start + timedelta(hours=1, minutes=1),
    )
    assert failed == {"status": "released", "delivery_count": 2}
    assert (
        repo.claim_advice_attention(
            "u1", "ios", now=start + timedelta(hours=1, minutes=30)
        )
        is None
    )
    assert (
        repo.claim_advice_attention("u1", "ios", now=start + timedelta(hours=2))
        is None
    )
    assert repo.get_advice("u1", advice.id).status == AdviceStatus.ACTIVE
    feedback = repo._connection.execute(
        "SELECT kind FROM feedback WHERE advice_id=?",
        (str(advice.id),),
    ).fetchall()
    assert feedback == []
    attention = repo._connection.execute(
        """SELECT state,delivery_count,resolved_reason FROM advice_attention
        WHERE advice_id=?""",
        (str(advice.id),),
    ).fetchone()
    assert tuple(attention) == ("resolved", 2, "technical_delivery_exhausted")
    repo.close()


def test_completing_an_expired_claim_consumes_the_permit_and_supersedes_token(
    tmp_path: Path,
):
    repo = Repository(tmp_path / "late-complete.db")
    advice = _advice(repo, _goal(repo))
    start = datetime(2026, 8, 9, 6, 0, tzinfo=timezone.utc)
    claim = repo.claim_advice_attention("u1", "android", now=start)
    assert claim is not None

    with pytest.raises(AttentionConflict, match="expired"):
        repo.complete_advice_attention(
            "u1",
            advice.id,
            "android",
            claim.claim_token,
            now=start + timedelta(minutes=16),
        )

    attention = repo._connection.execute(
        "SELECT state,delivery_count FROM advice_attention WHERE advice_id=?",
        (str(advice.id),),
    ).fetchone()
    assert tuple(attention) == ("waiting", 1)
    attempt = repo._connection.execute(
        "SELECT state FROM advice_attention_attempts WHERE claim_token=?",
        (str(claim.claim_token),),
    ).fetchone()
    assert attempt["state"] == "superseded"
    assert (
        repo.claim_advice_attention(
            "u1", "windows", now=start + timedelta(minutes=16)
        )
        is None
    )
    repo.close()


def test_prediction_deadline_withdraws_before_claim_and_resolves_attention(
    tmp_path: Path,
):
    repo = Repository(tmp_path / "expired-deadline.db")
    start = datetime(2026, 8, 9, 7, 0, tzinfo=timezone.utc)
    advice = _advice(
        repo,
        _goal(repo),
        created_at=start - timedelta(hours=1),
        prediction_deadline=start,
    )

    assert repo.claim_advice_attention("u1", "android", now=start) is None
    assert repo.get_advice("u1", advice.id).status == AdviceStatus.WITHDRAWN
    attention = repo._connection.execute(
        """SELECT state,delivery_count,resolved_reason FROM advice_attention
        WHERE advice_id=?""",
        (str(advice.id),),
    ).fetchone()
    assert tuple(attention) == ("resolved", 0, "prediction_deadline_expired")
    repo.close()


def test_next_reminder_is_bounded_by_prediction_deadline(tmp_path: Path):
    repo = Repository(tmp_path / "deadline-bound.db")
    start = datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc)
    deadline = start + timedelta(minutes=30)
    advice = _advice(
        repo,
        _goal(repo),
        created_at=start,
        prediction_deadline=deadline,
    )

    claim = repo.claim_advice_attention("u1", "android", now=start)
    assert claim is not None and claim.delivery_number == 1
    attention = repo._connection.execute(
        "SELECT next_eligible_at FROM advice_attention WHERE advice_id=?",
        (str(advice.id),),
    ).fetchone()
    assert datetime.fromisoformat(attention["next_eligible_at"]) == deadline
    repo.complete_advice_attention(
        "u1", advice.id, "android", claim.claim_token, now=start + timedelta(minutes=1)
    )

    assert (
        repo.claim_advice_attention(
            "u1", "windows", now=deadline - timedelta(microseconds=1)
        )
        is None
    )
    assert repo.claim_advice_attention("u1", "windows", now=deadline) is None
    assert repo.get_advice("u1", advice.id).status == AdviceStatus.WITHDRAWN
    attention = repo._connection.execute(
        "SELECT state,delivery_count,resolved_reason FROM advice_attention WHERE advice_id=?",
        (str(advice.id),),
    ).fetchone()
    assert tuple(attention) == ("resolved", 1, "prediction_deadline_expired")
    repo.close()


def test_any_explicit_feedback_cancels_the_cross_device_reminder(tmp_path: Path):
    repo = Repository(tmp_path / "feedback-cancels.db")
    advice = _advice(repo, _goal(repo))
    start = datetime(2026, 8, 9, 2, 0, tzinfo=timezone.utc)
    first = repo.claim_advice_attention("u1", "android", now=start)
    assert first is not None
    repo.complete_advice_attention(
        "u1", advice.id, "android", first.claim_token, now=start
    )

    repo.record_feedback(
        "u1",
        advice.id,
        FeedbackCreate(kind="useful"),
        now=start + timedelta(minutes=10),
    )

    assert (
        repo.claim_advice_attention(
            "u1", "windows", now=start + timedelta(hours=3)
        )
        is None
    )
    attention = repo._connection.execute(
        """SELECT state,delivery_count,resolved_reason FROM advice_attention
        WHERE advice_id=?""",
        (str(advice.id),),
    ).fetchone()
    assert tuple(attention) == ("resolved", 1, "feedback:useful")
    repo.close()


def test_later_defers_the_remaining_delivery_and_never_becomes_auto_irrelevant(
    tmp_path: Path,
):
    repo = Repository(tmp_path / "later-deferred.db")
    start = datetime(2026, 8, 9, 4, 0, tzinfo=timezone.utc)
    advice = _advice(repo, _goal(repo), created_at=start)
    first = repo.claim_advice_attention("u1", "android", now=start)
    assert first is not None
    repo.complete_advice_attention(
        "u1", advice.id, "android", first.claim_token, now=start
    )

    deferred = repo.record_feedback(
        "u1",
        advice.id,
        FeedbackCreate(kind="later"),
        now=start + timedelta(minutes=10),
    )
    assert deferred.snoozed_until == start + timedelta(hours=4, minutes=10)
    assert (
        repo.claim_advice_attention(
            "u1", "windows", now=start + timedelta(hours=4, minutes=9, seconds=59)
        )
        is None
    )
    second = repo.claim_advice_attention(
        "u1", "windows", now=start + timedelta(hours=4, minutes=10)
    )
    assert second is not None and second.delivery_number == 2
    result = repo.complete_advice_attention(
        "u1",
        advice.id,
        "windows",
        second.claim_token,
        now=start + timedelta(hours=4, minutes=11),
    )
    assert result == {
        "status": "delivered",
        "delivery_count": 2,
        "next_eligible_at": None,
    }
    assert repo.sweep_advice_attention(
        "u1", now=start + timedelta(hours=12)
    ) == 0
    attention = repo._connection.execute(
        """SELECT state,delivery_count,resolved_reason,handling_kind
        FROM advice_attention WHERE advice_id=?""",
        (str(advice.id),),
    ).fetchone()
    assert tuple(attention) == ("resolved", 2, "handled:deferred", "later")
    kinds = repo._connection.execute(
        "SELECT kind FROM feedback WHERE advice_id=? ORDER BY id",
        (str(advice.id),),
    ).fetchall()
    assert [row["kind"] for row in kinds] == ["later"]
    assert repo.get_advice("u1", advice.id).status == AdviceStatus.ACTIVE
    repo.close()


def test_attention_claim_is_atomic_across_repository_processes(tmp_path: Path):
    path = tmp_path / "atomic-claim.db"
    first_repo = Repository(path)
    advice = _advice(first_repo, _goal(first_repo))
    second_repo = Repository(path)
    start = datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc)

    def claim(repo: Repository, device_id: str):
        return repo.claim_advice_attention("u1", device_id, now=start)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda item: claim(*item),
                ((first_repo, "android"), (second_repo, "windows")),
            )
        )

    claims = [result for result in results if result is not None]
    assert len(claims) == 1
    assert claims[0].advice.id == advice.id
    first_repo.close()
    second_repo.close()


def test_startup_migrates_semantic_active_duplicates_and_keeps_history(tmp_path: Path):
    path = tmp_path / "legacy-duplicates.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE advice (
          id TEXT PRIMARY KEY,user_id TEXT NOT NULL,domain TEXT NOT NULL,
          level INTEGER NOT NULL,dedupe_key TEXT NOT NULL,status TEXT NOT NULL,
          delivery TEXT NOT NULL,prediction_confidence REAL NOT NULL,
          prediction_deadline TEXT NOT NULL,payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        )"""
    )
    goal_id = uuid4()
    started = datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc)
    records: list[AdviceRecord] = []
    for index, digest in enumerate(("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb")):
        record = AdviceRecord(
            user_id="u1",
            domain="work",
            requested_level=AdviceLevel.L2,
            goal_id=goal_id,
            goal_quote="Ship safely",
            evidence=[
                {
                    "event_id": uuid4(),
                    "source": "android.notification",
                    "fact": f"Release failure #{index}",
                    "observed_at": started + timedelta(minutes=index),
                    "confidence": 0.9,
                }
            ],
            action="Review the blocked release and choose one recovery path",
            first_step="Open the failure detail",
            alternative="Roll back",
            prediction={
                "outcome": "The release remains blocked",
                "deadline": started + timedelta(days=3),
                "confidence": 0.8,
            },
            adopted_expected_result="A recovery path is verified",
            adopted_confidence=0.8,
            urgency=0.8,
            impact=0.8,
            novelty=0.8,
            relevance=0.9,
            dedupe_key=f"problem:deadline:{digest}",
            effective_level=AdviceLevel.L2,
            proactive_score=0.85,
            delivery="immediate",
            created_at=started + timedelta(minutes=index),
        )
        records.append(record)
        connection.execute(
            """INSERT INTO advice(
              id,user_id,domain,level,dedupe_key,status,delivery,
              prediction_confidence,prediction_deadline,payload_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(record.id),
                record.user_id,
                record.domain,
                int(record.effective_level),
                record.dedupe_key,
                record.status.value,
                record.delivery,
                record.prediction.confidence,
                record.prediction.deadline.isoformat(),
                record.model_dump_json(),
                record.created_at.isoformat(),
            ),
        )
    connection.commit()
    connection.close()

    repo = Repository(path)
    tables = {
        row["name"]
        for row in repo._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"advice_preferences", "devices", "advice_attention"} <= tables
    advice_columns = {
        row["name"]
        for row in repo._connection.execute("PRAGMA table_info(advice)")
    }
    assert {"topic_key", "goal_id", "published_at"} <= advice_columns
    rows = repo._connection.execute(
        """SELECT id,status,topic_key,payload_json FROM advice
        WHERE user_id='u1' ORDER BY created_at"""
    ).fetchall()
    assert [row["id"] for row in rows] == [str(item.id) for item in records]
    assert [row["status"] for row in rows] == ["active", "withdrawn"]
    assert rows[0]["topic_key"] == rows[1]["topic_key"]
    assert AdviceRecord.model_validate_json(rows[1]["payload_json"]).status == AdviceStatus.WITHDRAWN
    attention = repo._connection.execute(
        """SELECT state,delivery_count,resolved_reason FROM advice_attention
        WHERE advice_id=?""",
        (str(records[0].id),),
    ).fetchone()
    assert tuple(attention) == ("resolved", 0, "legacy_no_replay")
    assert repo.claim_advice_attention("u1", "android") is None
    repo.close()
