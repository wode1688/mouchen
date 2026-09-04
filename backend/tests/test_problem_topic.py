import pytest

from app.domain.models import Event, Goal
from app.domain.problem_signals import detect_problem, deterministic_intervention_allowed


def _goal() -> Goal:
    return Goal(
        user_id="u1",
        domain="finance",
        title="Keep payments operational",
        quote="I will resolve payment failures promptly",
        target={"keywords": ["payment"]},
    )


def _payment_event(title: str, text: str, **facts) -> Event:
    return Event(
        user_id="u1",
        source="android.notification",
        type="notification.posted",
        facts={
            "package": "com.example.payments",
            "title": title,
            "text": text,
            **facts,
        },
    )


def test_rule_topic_merges_same_subject_when_date_amount_and_status_change():
    goal = _goal()
    first = detect_problem(
        _payment_event(
            "Merchant Alpha",
            "Payment failed today, amount $100, status 17",
        ),
        goal,
        0.9,
    )
    second = detect_problem(
        _payment_event(
            "Merchant Alpha",
            "Payment declined tomorrow, amount $250, status 42",
        ),
        goal,
        0.9,
    )

    assert first is not None and second is not None
    assert first.topic_key == second.topic_key
    assert first.issue_subject == "payment_failure:summary:merchant-alpha"


def test_rule_topic_separates_different_subjects_in_same_source_and_category():
    goal = _goal()
    alpha = detect_problem(
        _payment_event("Merchant Alpha", "Payment failed today for $100"),
        goal,
        0.9,
    )
    beta = detect_problem(
        _payment_event("Merchant Beta", "Payment failed today for $100"),
        goal,
        0.9,
    )

    assert alpha is not None and beta is not None
    assert alpha.topic_key != beta.topic_key


def test_rule_topic_prefers_stable_object_id_over_changing_summary():
    goal = _goal()
    first = detect_problem(
        _payment_event(
            "Payment alert",
            "Payment failed today for $100",
            order_id="ORDER-1001",
        ),
        goal,
        0.9,
    )
    same_order = detect_problem(
        _payment_event(
            "Settlement alert",
            "Payment declined tomorrow for $250",
            order_id="ORDER-1001",
        ),
        goal,
        0.9,
    )
    other_order = detect_problem(
        _payment_event(
            "Payment alert",
            "Payment failed today for $100",
            order_id="ORDER-1002",
        ),
        goal,
        0.9,
    )

    assert first is not None and same_order is not None and other_order is not None
    assert first.topic_key == same_order.topic_key
    assert first.topic_key != other_order.topic_key


def test_rule_topic_ignores_volatile_entities():
    goal = _goal()
    first = detect_problem(
        _payment_event(
            "Payment alert",
            "Payment failed today for $100",
            entities=[
                {"type": "merchant", "name": "Merchant Alpha"},
                {"type": "money", "value": "$100"},
            ],
        ),
        goal,
        0.9,
    )
    second = detect_problem(
        _payment_event(
            "Settlement alert",
            "Payment declined tomorrow for $250",
            entities=[
                {"type": "merchant", "name": "Merchant Alpha"},
                {"type": "money", "value": "$250"},
            ],
        ),
        goal,
        0.9,
    )

    assert first is not None and second is not None
    assert first.topic_key == second.topic_key


def test_rule_topic_falls_back_only_when_no_subject_survives():
    candidate = detect_problem(
        _payment_event("", "Payment failed today for $100, status 17"),
        _goal(),
        0.9,
    )

    assert candidate is not None
    assert candidate.issue_subject is None


@pytest.mark.parametrize(
    ("first_label", "same_label", "other_label"),
    [
        ("Order #ORD-1001", "Order ID ORD-1001", "Order #ORD-1002"),
        ("Invoice #INV-2001", "Invoice no. INV-2001", "Invoice #INV-2002"),
        ("Project ID PRJ-3001", "Project #PRJ-3001", "Project ID PRJ-3002"),
        ("Card ending in 4242", "Card last 4 digits 4242", "Card ending in 5252"),
    ],
)
def test_rule_topic_preserves_labeled_ids_in_free_text(
    first_label: str,
    same_label: str,
    other_label: str,
):
    goal = _goal()
    first = detect_problem(
        _payment_event(
            "Payment alert",
            f"{first_label} payment failed today for $100 after 1 retry",
        ),
        goal,
        0.9,
    )
    same = detect_problem(
        _payment_event(
            "Settlement alert",
            f"{same_label} payment declined tomorrow for $250 after 4 retries",
        ),
        goal,
        0.9,
    )
    other = detect_problem(
        _payment_event(
            "Payment alert",
            f"{other_label} payment failed tomorrow for $250 after 4 retries",
        ),
        goal,
        0.9,
    )

    assert first is not None and same is not None and other is not None
    assert first.topic_key == same.topic_key
    assert first.topic_key != other.topic_key


@pytest.mark.parametrize(
    "facts",
    [
        {
            "content_kind": "document",
            "speaker": "author",
            "evidence_strength": "contextual",
        },
        {
            "content_kind": "video",
            "speaker": "author",
            "evidence_strength": "contextual",
        },
        {
            "content_kind": "chat",
            "speaker": "counterparty",
            "message_direction": "inbound",
            "evidence_strength": "direct",
        },
        {
            "content_kind": "app_ui",
            "speaker": "system",
            "evidence_strength": "direct",
            "resolution_state": "resolved",
        },
    ],
)
def test_context_or_other_people_text_cannot_trigger_deterministic_advice(facts):
    event = _payment_event(
        "Example content",
        "Payment failed and the deployment deadline is today",
        **facts,
    )

    assert deterministic_intervention_allowed(event) is False
    assert detect_problem(event, _goal(), 0.9) is None


def test_direct_system_state_can_trigger_deterministic_advice():
    event = _payment_event(
        "Payment status",
        "Payment failed",
        content_kind="system_notice",
        speaker="system",
        evidence_strength="direct",
        resolution_state="unresolved",
    )

    assert deterministic_intervention_allowed(event) is True
    assert detect_problem(event, _goal(), 0.9) is not None


def test_direct_user_statement_can_trigger_deterministic_advice():
    event = Event(
        user_id="u1",
        source="windows.ime",
        type="ime.text_committed",
        facts={
            "text": "Payment failed",
            "content_kind": "user_input",
            "speaker": "user",
            "message_direction": "outbound",
            "evidence_strength": "direct",
            "resolution_state": "unresolved",
        },
    )

    assert deterministic_intervention_allowed(event) is True
    assert detect_problem(event, _goal(), 0.9) is not None
