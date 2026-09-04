from __future__ import annotations

from typing import Any

from .models import Event


def derive_for_detection(event: Event) -> Event:
    """Normalize supported raw collector events without inventing missing evidence."""
    facts = dict(event.facts)

    if event.type in {"intel.external", "mail.received"}:
        if not _has_explicit_threat_evidence(facts):
            return event
        fallback = facts.get("title") or facts.get("subject") or "目标相关外部威胁"
        facts.setdefault("summary", fallback)
        facts.setdefault("independent_sources", 1)
        facts.setdefault("primary_source", event.type == "mail.received")
        return event.model_copy(update={"type": "threat.detected", "facts": facts})

    if event.type == "calendar.scheduled" and _positive_int(facts.get("reschedule_count")) >= 2:
        return event.model_copy(update={"type": "commitment.slipped", "facts": facts})

    if event.type == "app.foreground_session" and _has_period_allocation(facts):
        duration_ms = _positive_float(facts.get("period_total_duration_ms"))
        facts["actual_hours"] = duration_ms / 3_600_000
        return event.model_copy(update={"type": "time.allocation", "facts": facts})

    return event


def _has_explicit_threat_evidence(facts: dict[str, Any]) -> bool:
    evidence = facts.get("threat_evidence")
    if isinstance(evidence, str):
        return bool(evidence.strip())
    if isinstance(evidence, (list, tuple, dict)):
        return bool(evidence)
    return False


def _has_period_allocation(facts: dict[str, Any]) -> bool:
    return (
        bool(str(facts.get("domain", "")).strip())
        and _positive_float(facts.get("expected_hours")) > 0
        and _positive_float(facts.get("period_total_duration_ms")) > 0
    )


def _positive_float(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
