from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

from .models import AdviceCandidate


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def proactive_score(candidate: AdviceCandidate) -> float:
    """Score evidence quality and expected value, never message volume."""
    evidence_confidence = sum(e.confidence for e in candidate.evidence) / len(candidate.evidence)
    base = (
        0.30 * candidate.urgency
        + 0.30 * candidate.impact
        + 0.15 * candidate.novelty
        + 0.25 * candidate.relevance
    )
    timing = 0.70 + 0.30 * candidate.context_fit
    burden_factor = 1.0 - 0.10 * candidate.interruption_cost
    return round(clamp((evidence_confidence**1.2) * base * timing * burden_factor), 4)


def wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = successes / total
    denominator = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return clamp((centre - margin) / denominator)


@dataclass(frozen=True)
class TrustSummary:
    judged: int = 0
    correct: int = 0
    brier_sum: float = 0.0
    utility_sum: float = 0.0
    timing_sum: float = 0.0
    catastrophic_errors: int = 0

    @property
    def precision_lower_bound(self) -> float:
        return wilson_lower_bound(self.correct, self.judged)

    @property
    def brier(self) -> float:
        return self.brier_sum / self.judged if self.judged else 1.0

    @property
    def utility(self) -> float:
        return self.utility_sum / self.judged if self.judged else 0.5

    @property
    def timing(self) -> float:
        return self.timing_sum / self.judged if self.judged else 0.5

    @property
    def score(self) -> float:
        return round(
            clamp(
                0.45 * self.precision_lower_bound
                + 0.25 * (1 - self.brier)
                + 0.20 * self.utility
                + 0.10 * self.timing
                - min(1.0, 0.5 * self.catastrophic_errors)
            ),
            4,
        )
