from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.domain.derivation import derive_for_detection
from app.domain.detectors import detect
from app.domain.gates import evaluate_context
from app.domain.models import AdviceLevel, ContextSnapshot, Event, Goal, Sensitivity
from app.domain.scoring import TrustSummary, proactive_score
from app.domain.trust import speaking_ceiling


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    goal = Goal(
        user_id="demo-user",
        domain="work",
        title="发布可运行的谋臣公开核心",
        quote="本周完成可运行、可验证的公开核心",
        target={"weekly_hours": 10},
    )
    raw_event = Event(
        user_id="demo-user",
        source="demo.synthetic_activity",
        type="app.foreground_session",
        facts={
            "domain": "work",
            "expected_hours": 6,
            "weekly_target_hours": 10,
            "period_total_duration_ms": 3_600_000,
            "week_elapsed_ratio": 0.6,
            "impact": 0.85,
        },
        confidence=0.95,
        sensitivity=Sensitivity.PUBLIC,
        consent_scope="demo.synthetic",
    )

    event = derive_for_detection(raw_event)
    candidate = detect(event, goal)
    if candidate is None:
        raise RuntimeError("The synthetic event should produce an advice candidate.")

    score = proactive_score(candidate)
    ceiling = speaking_ceiling(
        TrustSummary(),
        charter_level=AdviceLevel.L4,
        redline_authorized=False,
    )
    gate = evaluate_context(
        candidate,
        ContextSnapshot(),
        speaking_level=ceiling,
        duplicate_active=False,
    )

    result = {
        "data": "synthetic_only",
        "goal": goal.title,
        "normalized_event_type": event.type,
        "evidence": [item.fact for item in candidate.evidence],
        "advice": {
            "action": candidate.action,
            "first_step": candidate.first_step,
            "alternative": candidate.alternative,
            "prediction": candidate.prediction.outcome,
        },
        "evaluation": {
            "proactive_score": score,
            "requested_level": candidate.requested_level.label,
            "earned_speaking_ceiling": ceiling.label,
            "effective_level": min(candidate.requested_level, ceiling).label,
            "allowed": gate.allowed,
            "delivery": gate.delivery,
            "reason_codes": list(gate.reasons),
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
