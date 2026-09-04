from __future__ import annotations

from .models import AdviceLevel
from .scoring import TrustSummary


def speaking_ceiling(
    trust: TrustSummary,
    charter_level: AdviceLevel,
    redline_authorized: bool,
) -> AdviceLevel:
    if trust.catastrophic_errors:
        return min(charter_level, AdviceLevel.L2)
    if (
        redline_authorized
        and charter_level >= AdviceLevel.L4
        and trust.judged >= 40
        and trust.precision_lower_bound >= 0.90
        and trust.brier <= 0.15
    ):
        return AdviceLevel.L4
    if (
        charter_level >= AdviceLevel.L3
        and trust.judged >= 20
        and trust.precision_lower_bound >= 0.80
        and trust.brier <= 0.20
    ):
        return AdviceLevel.L3
    if charter_level >= AdviceLevel.L2:
        return AdviceLevel.L2
    return AdviceLevel.L1
