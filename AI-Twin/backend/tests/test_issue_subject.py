import pytest

from app.advice_refiner import _candidate_from_discovery
from app.domain.models import Event, Goal


def _payload(
    evidence_quote: str,
    *,
    category: str,
    issue_subject: str,
    action: str,
) -> dict:
    return {
        "intervene": True,
        "category": category,
        "issue_subject": issue_subject,
        "requested_level": 2,
        "evidence_quote": evidence_quote,
        "action": action,
        "first_step": "Open the relevant record and verify the latest state",
        "alternative": "Contact support with the saved evidence",
        "prediction_outcome": "The issue remains unresolved at the next review",
        "deadline_hours": 24,
        "confidence": 0.88,
        "adopted_expected_result": "The issue has a verified owner and next step",
        "adopted_confidence": 0.82,
        "urgency": 0.76,
        "impact": 0.79,
    }


def _goal() -> Goal:
    return Goal(
        user_id="u1",
        domain="finance",
        title="Resolve payment issues",
        quote="I will keep all payment accounts operational",
        target={"keywords": ["payment"]},
    )


def _event(text: str) -> Event:
    return Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={"package": "com.example.payments", "text": text},
    )


def test_model_topic_uses_issue_subject_not_event_text_or_category():
    goal = _goal()
    first_text = "Payment account payout failed today with status 17"
    second_text = "The latest settlement remains blocked and needs review"
    first = _candidate_from_discovery(
        _payload(
            first_text,
            category="account_error",
            issue_subject="merchant payout failure",
            action="Inspect the failed payout record",
        ),
        _event(first_text),
        goal,
        0.9,
        first_text,
    )
    second = _candidate_from_discovery(
        _payload(
            second_text,
            category="settlement_risk",
            issue_subject="merchant payout failure",
            action="Compare the blocked settlement with the payout ledger",
        ),
        _event(second_text),
        goal,
        0.9,
        second_text,
    )

    assert first is not None and second is not None
    assert first.topic_key == second.topic_key
    assert first.issue_subject == "merchant payout failure"


def test_model_topic_keeps_distinct_subjects_in_same_source_and_category():
    goal = _goal()
    text = "Two independent payment problems need review"
    event = _event(text)
    payout = _candidate_from_discovery(
        _payload(
            text,
            category="payment_risk",
            issue_subject="merchant payout failure",
            action="Inspect the payout ledger",
        ),
        event,
        goal,
        0.9,
        text,
    )
    refund = _candidate_from_discovery(
        _payload(
            text,
            category="payment_risk",
            issue_subject="customer refund rejection",
            action="Inspect the refund request",
        ),
        event,
        goal,
        0.9,
        text,
    )

    assert payout is not None and refund is not None
    assert payout.topic_key != refund.topic_key


def test_model_topic_is_identical_across_user_facing_locales():
    goal = _goal()
    text = "Merchant Alpha payout failed"
    event = _event(text)
    english = _candidate_from_discovery(
        _payload(
            text,
            category="payment risk",
            issue_subject="merchant payout failure",
            action="Inspect the payout ledger",
        ),
        event,
        goal,
        0.9,
        text,
        locale="en-US",
    )
    chinese_payload = _payload(
        text,
        category="付款风险",
        issue_subject="merchant payout failure",
        action="核对付款账本",
    )
    chinese_payload.update(
        first_step="打开付款记录并核对最新状态",
        alternative="携带原始证据联系支持团队",
        prediction_outcome="下次核验时问题仍未解决",
        adopted_expected_result="问题已有明确负责人和下一步",
    )
    chinese = _candidate_from_discovery(
        chinese_payload,
        event,
        goal,
        0.9,
        text,
        locale="zh-CN",
    )

    assert english is not None and chinese is not None
    assert english.topic_key == chinese.topic_key
    assert english.issue_subject == chinese.issue_subject == "merchant payout failure"
    assert english.action != chinese.action


@pytest.mark.parametrize(
    "issue_subject",
    ["商户付款失败", "Merchant payout failure", "merchant payout failure ✅"],
)
def test_model_rejects_localized_or_noncanonical_issue_subject(issue_subject: str):
    text = "Merchant Alpha payout failed"
    candidate = _candidate_from_discovery(
        _payload(
            text,
            category="payment_risk",
            issue_subject=issue_subject,
            action="Inspect the affected record",
        ),
        _event(text),
        _goal(),
        0.9,
        text,
    )

    assert candidate is None


@pytest.mark.parametrize(
    ("source_text", "rewritten_quote"),
    [
        ("Payment failed for Merchant Alpha", "payment failed for Merchant Alpha"),
        ("Die Straße ist gesperrt", "Die STRASSE ist gesperrt"),
    ],
)
def test_model_evidence_quote_must_be_an_exact_verbatim_substring(
    source_text: str,
    rewritten_quote: str,
):
    candidate = _candidate_from_discovery(
        _payload(
            rewritten_quote,
            category="payment_risk",
            issue_subject="merchant payout failure",
            action="Inspect the affected record",
        ),
        _event(source_text),
        _goal(),
        0.9,
        source_text,
    )

    assert candidate is None


def test_issue_subject_normalization_ignores_volatile_time_and_count_words():
    goal = _goal()
    text = "Payout failures are still present"
    event = _event(text)
    first = _candidate_from_discovery(
        _payload(
            text,
            category="account_error",
            issue_subject="urgent merchant payout failure today status 17",
            action="Inspect today's payout record",
        ),
        event,
        goal,
        0.9,
        text,
    )
    second = _candidate_from_discovery(
        _payload(
            text,
            category="settlement_risk",
            issue_subject="merchant payout failure currently status 42",
            action="Inspect the latest settlement record",
        ),
        event,
        goal,
        0.9,
        text,
    )

    assert first is not None and second is not None
    assert first.topic_key == second.topic_key


@pytest.mark.parametrize(
    ("first_label", "same_label", "other_label"),
    [
        ("order #ord-1001", "order id ord-1001", "order #ord-1002"),
        ("invoice #inv-2001", "invoice no. inv-2001", "invoice #inv-2002"),
        ("project id prj-3001", "project #prj-3001", "project id prj-3002"),
        ("card ending in 4242", "card last 4 digits 4242", "card ending in 5252"),
    ],
)
def test_model_topic_preserves_labeled_stable_ids_but_ignores_volatile_numbers(
    first_label: str,
    same_label: str,
    other_label: str,
):
    goal = _goal()
    text = "Payment failure needs review"
    event = _event(text)

    def candidate(subject: str):
        return _candidate_from_discovery(
            _payload(
                text,
                category="payment_risk",
                issue_subject=f"{subject} failure",
                action="Inspect the affected record",
            ),
            event,
            goal,
            0.9,
            text,
        )

    first = candidate(first_label)
    same = candidate(same_label)
    other = candidate(other_label)

    assert first is not None and same is not None and other is not None
    assert first.topic_key == same.topic_key
    assert first.topic_key != other.topic_key
