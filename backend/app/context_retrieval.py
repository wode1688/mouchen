from __future__ import annotations

import re
from typing import Any

from .domain.models import Event, Goal, Sensitivity
from .domain.problem_signals import event_text
from .privacy import redact_text


MAX_QUESTION_GOALS = 3
MAX_QUESTION_EVENTS = 4
_CLIENT_STATE_KEYS = frozenset(
    {
        "active_advice_count",
        "pending_advice_count",
        "selected_domain",
        "app_version",
        "locale",
        "timezone_offset_minutes",
    }
)
_STRUCTURED_EVENT_FIELDS = (
    "domain",
    "title",
    "subject",
    "summary",
    "status",
    "error",
    "action",
    "app_label",
    "package",
    "actual_hours",
    "expected_hours",
    "weekly_target_hours",
    "reschedule_count",
    "duration_ms",
)
_GENERIC_TERMS = {
    "你好",
    "请问",
    "现在",
    "最近",
    "今天",
    "事情",
    "问题",
    "怎么",
    "如何",
    "什么",
    "为什么",
    "怎么办",
    "我的",
    "帮我",
    "一下",
    "目前",
}


def build_user_question_context(
    repository: Any,
    user_id: str,
    question: str,
    client_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Retrieve a small local-only relevance set before the shared cloud privacy gate."""

    terms = _meaningful_terms(question)
    current_goals = repository.current_goals(user_id)
    ranked_goals = sorted(
        enumerate(current_goals),
        key=lambda item: (_relevance(_goal_search_text(item[1]), terms), -item[0]),
        reverse=True,
    )
    matching_goals = [
        goal for _, goal in ranked_goals if _relevance(_goal_search_text(goal), terms) > 0
    ]
    selected_goals = (matching_goals or current_goals[:2])[:MAX_QUESTION_GOALS]

    event_candidates: list[tuple[float, int, Event, str]] = []
    if terms:
        for recency_index, event in enumerate(repository.recent_events(user_id, limit=120)):
            if event.sensitivity == Sensitivity.RESTRICTED:
                continue
            searchable = _event_search_text(event)
            if not searchable:
                continue
            score = _relevance(searchable, terms)
            if score <= 0:
                continue
            recency_bonus = max(0.0, 1.0 - recency_index / 120) * 0.25
            event_candidates.append((score + recency_bonus, recency_index, event, searchable))
    event_candidates.sort(key=lambda item: (-item[0], item[1]))

    return {
        "retrieval": "local-keyword-v1",
        "client_state": _minimal_client_state(client_context or {}),
        "goals": [_goal_slice(goal) for goal in selected_goals],
        "recent_relevant_events": [
            _event_slice(event, searchable, terms)
            for _, _, event, searchable in event_candidates[:MAX_QUESTION_EVENTS]
        ],
    }


def _goal_slice(goal: Goal) -> dict[str, Any]:
    return {
        "domain": redact_text(goal.domain, 80),
        "goal_title": redact_text(goal.title, 160),
        "goal_quote": redact_text(goal.quote, 500),
    }


def _event_slice(event: Event, searchable: str, terms: set[str]) -> dict[str, Any]:
    return {
        "event_source": redact_text(event.source, 100),
        "event_type": event.type,
        "occurred_at": event.occurred_at.isoformat(),
        "excerpt": _relevant_excerpt(searchable, terms),
    }


def _minimal_client_state(context: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in _CLIENT_STATE_KEYS:
        if key not in context:
            continue
        value = context[key]
        if isinstance(value, (bool, int, float)) or value is None:
            result[key] = value
        elif isinstance(value, str):
            result[key] = redact_text(value, 80)
    return result


def _goal_search_text(goal: Goal) -> str:
    keywords = goal.target.get("keywords", [])
    if isinstance(keywords, str):
        keywords = [keywords]
    keyword_text = " ".join(str(value) for value in keywords if isinstance(value, (str, int)))
    return f"{goal.domain} {goal.title} {goal.quote} {keyword_text}".casefold()


def _event_search_text(event: Event) -> str:
    direct = event_text(event)
    if direct:
        return direct
    values: list[str] = []
    for field in _STRUCTURED_EVENT_FIELDS:
        value = event.facts.get(field)
        if isinstance(value, (str, int, float, bool)):
            values.append(f"{field}={value}")
    return " ".join(values)


def _relevance(value: str, terms: set[str]) -> float:
    if not terms:
        return 0.0
    folded = value.casefold()
    return sum(1.0 + min(2, folded.count(term) - 1) * 0.2 for term in terms if term in folded)


def _relevant_excerpt(value: str, terms: set[str], limit: int = 360) -> str:
    folded = value.casefold()
    positions: list[tuple[int, str]] = []
    for term in terms:
        start = 0
        while len(positions) < 80:
            position = folded.find(term, start)
            if position < 0:
                break
            positions.append((position, term))
            start = position + max(1, len(term))
    if not positions or len(value) <= limit:
        return redact_text(value, limit)
    best_position, _ = max(
        positions,
        key=lambda item: (
            sum(
                1
                for term in terms
                if term in folded[max(0, item[0] - 40) : item[0] + limit]
            ),
            len(item[1]),
            -item[0],
        ),
    )
    start = best_position
    end = min(len(value), start + limit)
    prefix = "… " if start else ""
    suffix = " …" if end < len(value) else ""
    return redact_text(f"{prefix}{value[start:end]}{suffix}", limit)


def _meaningful_terms(value: str) -> set[str]:
    folded = value.casefold()
    terms = {word for word in re.findall(r"[a-z0-9_-]{3,}", folded)}
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", folded):
        if chunk not in _GENERIC_TERMS and len(chunk) <= 6:
            terms.add(chunk)
        terms.update(
            chunk[index : index + 2]
            for index in range(len(chunk) - 1)
            if chunk[index : index + 2] not in _GENERIC_TERMS
        )
    return {term for term in terms if term not in _GENERIC_TERMS}
