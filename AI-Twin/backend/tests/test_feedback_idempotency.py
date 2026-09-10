from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest

from app.domain.models import (
    AdviceLevel,
    AdviceRecord,
    AdviceStatus,
    Evidence,
    FeedbackCreate,
    Goal,
    Prediction,
    utc_now,
)
from app.storage import Repository


def _active_advice(repo: Repository, *, delivery: str = "immediate") -> AdviceRecord:
    goal = repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="Ship alpha",
            quote="I will ship the alpha",
            target={"keywords": ["alpha"]},
        )
    )
    advice = AdviceRecord(
        user_id="u1",
        domain="work",
        requested_level=AdviceLevel.L2,
        goal_id=goal.id,
        goal_quote=goal.quote,
        evidence=[
            Evidence(
                event_id=uuid4(),
                source="test",
                fact="The alpha task is blocked",
                observed_at=utc_now(),
                confidence=0.9,
            )
        ],
        action="Inspect the blocker",
        first_step="Open the task",
        alternative="Reduce the scope",
        prediction=Prediction(
            outcome="The alpha remains blocked",
            deadline=utc_now() + timedelta(days=2),
            confidence=0.85,
        ),
        adopted_expected_result="The blocker has an owner",
        adopted_confidence=0.8,
        urgency=0.75,
        impact=0.8,
        novelty=0.8,
        relevance=0.9,
        dedupe_key=f"test:{uuid4()}",
        topic_key=f"topic:{uuid4()}",
        effective_level=AdviceLevel.L2,
        proactive_score=0.9,
        delivery=delivery,
        status=AdviceStatus.ACTIVE,
    )
    return repo.insert_advice(advice)


@pytest.mark.parametrize(
    ("kind", "note"),
    [
        ("useful", None),
        ("later", None),
        ("guidance", "Focus on the customer impact first"),
    ],
)
def test_semantically_identical_feedback_is_recorded_once(tmp_path, kind, note):
    repo = Repository(tmp_path / f"{kind}.db")
    advice = _active_advice(repo)

    repo.record_feedback(
        "u1",
        advice.id,
        FeedbackCreate(feedback_id=uuid4(), kind=kind, note=note),
    )
    repo.record_feedback(
        "u1",
        advice.id,
        FeedbackCreate(feedback_id=uuid4(), kind=kind, note=note),
    )

    count = repo._connection.execute(
        "SELECT COUNT(*) AS n FROM feedback WHERE advice_id=? AND origin='user'",
        (str(advice.id),),
    ).fetchone()["n"]
    assert count == 1
    repo.close()


def test_feedback_id_retry_is_idempotent_and_cannot_change_meaning(tmp_path):
    repo = Repository(tmp_path / "feedback-id.db")
    advice = _active_advice(repo)
    feedback_id = uuid4()
    request = FeedbackCreate(feedback_id=feedback_id, kind="useful")

    first = repo.record_feedback("u1", advice.id, request)
    second = repo.record_feedback("u1", advice.id, request)

    assert first.id == second.id
    assert repo._connection.execute(
        "SELECT COUNT(*) AS n FROM feedback WHERE feedback_id=?",
        (str(feedback_id),),
    ).fetchone()["n"] == 1
    with pytest.raises(ValueError, match="different feedback"):
        repo.record_feedback(
            "u1",
            advice.id,
            FeedbackCreate(feedback_id=feedback_id, kind="later"),
        )
    repo.close()


def test_cross_device_concurrent_feedback_is_atomically_deduplicated(tmp_path):
    path = tmp_path / "concurrent-feedback.db"
    setup = Repository(path)
    advice = _active_advice(setup)
    setup.close()
    first = Repository(path)
    second = Repository(path)

    def submit(repo: Repository) -> None:
        repo.record_feedback(
            "u1",
            advice.id,
            FeedbackCreate(feedback_id=uuid4(), kind="useful"),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, first), pool.submit(submit, second)]
        for future in futures:
            future.result(timeout=5)

    assert first._connection.execute(
        "SELECT COUNT(*) AS n FROM feedback WHERE advice_id=? AND kind='useful'",
        (str(advice.id),),
    ).fetchone()["n"] == 1
    first.close()
    second.close()


def test_different_guidance_notes_remain_distinct_learning_signals(tmp_path):
    repo = Repository(tmp_path / "guidance.db")
    advice = _active_advice(repo)

    repo.record_feedback(
        "u1",
        advice.id,
        FeedbackCreate(
            feedback_id=uuid4(),
            kind="guidance",
            note="Focus on customer impact first",
        ),
    )
    repo.record_feedback(
        "u1",
        advice.id,
        FeedbackCreate(
            feedback_id=uuid4(),
            kind="guidance",
            note="Focus on cash flow first",
        ),
    )

    assert repo._connection.execute(
        "SELECT COUNT(*) AS n FROM feedback WHERE advice_id=? AND kind='guidance'",
        (str(advice.id),),
    ).fetchone()["n"] == 2
    repo.close()


def test_brief_advice_never_enters_immediate_attention_claims(tmp_path):
    repo = Repository(tmp_path / "brief.db")
    advice = _active_advice(repo, delivery="brief")

    assert repo.claim_advice_attention("u1", "android-1") is None
    attention = repo._connection.execute(
        """SELECT state,delivery_count,resolved_reason FROM advice_attention
        WHERE advice_id=?""",
        (str(advice.id),),
    ).fetchone()
    assert dict(attention) == {
        "state": "resolved",
        "delivery_count": 0,
        "resolved_reason": "brief_no_push",
    }
    repo.close()


def test_acknowledged_stops_reminders_without_negative_learning_or_status_change(
    tmp_path,
):
    repo = Repository(tmp_path / "acknowledged.db")
    advice = _active_advice(repo)
    claim = repo.claim_advice_attention("u1", "android-1")
    assert claim is not None and claim.delivery_number == 1

    recorded = repo.record_feedback(
        "u1",
        advice.id,
        FeedbackCreate(feedback_id=uuid4(), kind="acknowledged"),
    )

    assert recorded.status == AdviceStatus.ACTIVE
    assert recorded.snoozed_until is None
    stored = repo.get_advice("u1", advice.id)
    assert stored is not None
    assert stored.status == AdviceStatus.ACTIVE
    assert stored.snoozed_until is None
    attention = repo._connection.execute(
        """SELECT state,delivery_count,resolved_reason FROM advice_attention
        WHERE advice_id=?""",
        (str(advice.id),),
    ).fetchone()
    assert dict(attention) == {
        "state": "resolved",
        "delivery_count": 1,
        "resolved_reason": "feedback:acknowledged",
    }
    feedback = repo._connection.execute(
        """SELECT kind,origin,signal_weight FROM feedback
        WHERE advice_id=?""",
        (str(advice.id),),
    ).fetchall()
    assert [tuple(row) for row in feedback] == [("acknowledged", "user", 1.0)]
    assert repo._connection.execute(
        "SELECT COUNT(*) AS n FROM error_ledger WHERE advice_id=?",
        (str(advice.id),),
    ).fetchone()["n"] == 0
    assert repo._connection.execute(
        "SELECT COUNT(*) AS n FROM relevance_ledger WHERE advice_id=?",
        (str(advice.id),),
    ).fetchone()["n"] == 0
    assert repo.claim_advice_attention("u1", "windows-1") is None
    repo.close()
