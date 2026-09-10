from datetime import timedelta
from uuid import uuid4

from app.domain.models import AdviceCandidate, AdviceLevel, Evidence, Prediction, utc_now
from app.domain.scoring import TrustSummary, proactive_score, wilson_lower_bound
from app.domain.trust import speaking_ceiling


def candidate(confidence: float = 0.95, urgency: float = 0.8) -> AdviceCandidate:
    now = utc_now()
    return AdviceCandidate(
        user_id="u1",
        domain="work",
        requested_level=AdviceLevel.L3,
        goal_id=uuid4(),
        goal_quote="本周完成发布",
        evidence=[Evidence(event_id=uuid4(), source="calendar", fact="只投入2小时", observed_at=now, confidence=confidence)],
        action="重新锁定开发时间",
        first_step="建立两小时日历草稿",
        alternative="调整目标版本并记录取舍",
        prediction=Prediction(outcome="里程碑继续延期", deadline=now + timedelta(days=7), confidence=0.8),
        urgency=urgency,
        impact=0.9,
        novelty=0.8,
        relevance=1.0,
        context_fit=1.0,
        interruption_cost=0.2,
        dedupe_key="work-drift",
    )


def test_score_rewards_evidence_and_value():
    assert proactive_score(candidate()) > proactive_score(candidate(confidence=0.5, urgency=0.3))
    assert 0 <= proactive_score(candidate()) <= 1


def test_wilson_lower_bound_requires_evidence_volume():
    assert wilson_lower_bound(8, 10) < wilson_lower_bound(80, 100)


def test_speaking_rights_are_earned():
    cold = TrustSummary()
    strong = TrustSummary(judged=100, correct=99, brier_sum=5, utility_sum=85, timing_sum=85)
    assert speaking_ceiling(cold, AdviceLevel.L4, True) == AdviceLevel.L2
    assert speaking_ceiling(strong, AdviceLevel.L4, True) == AdviceLevel.L4
