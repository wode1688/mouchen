from __future__ import annotations

from dataclasses import dataclass

from .models import AdviceCandidate, AdviceLevel, ContextSnapshot


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    delivery: str
    reasons: tuple[str, ...]


def evaluate_context(
    candidate: AdviceCandidate,
    context: ContextSnapshot,
    speaking_level: AdviceLevel,
    duplicate_active: bool,
) -> GateResult:
    reasons: list[str] = []
    if not context.consent_active:
        return GateResult(False, "hold", ("CONSENT_REVOKED",))
    if context.user_requested_pause:
        return GateResult(False, "hold", ("SYSTEM_PAUSED",))
    if duplicate_active:
        return GateResult(False, "merge", ("ACTIVE_THREAD_EXISTS",))
    if candidate.requested_level > speaking_level:
        reasons.append("SPEAKING_RIGHT_DOWNGRADED")

    effective = min(candidate.requested_level, speaking_level)
    timing_blocked = context.sleeping or context.driving or context.in_meeting or context.quiet_hours
    # Emergency controls interruption timing, not the earned speaking level. A cold-start
    # domain may still be capped at L2, but a strongly evidenced L3 safety candidate must
    # not disappear into the brief queue during a meeting or quiet period.
    if timing_blocked and not (context.emergency and candidate.requested_level >= AdviceLevel.L3):
        return GateResult(True, "brief", tuple(reasons + ["CONTEXT_DEFERRED"]))
    if effective == AdviceLevel.L1:
        return GateResult(True, "brief", tuple(reasons))
    return GateResult(True, "immediate", tuple(reasons))
