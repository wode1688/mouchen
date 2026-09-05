from __future__ import annotations

import threading
import hashlib
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Iterable

from .collectors import Signal


class ActivityReviewBuffer:
    """Builds a bounded, factual activity window for proactive model review."""

    def __init__(self, max_events: int = 300) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=max(20, int(max_events)))
        self._lock = threading.RLock()
        self._window_started_at = datetime.now(timezone.utc)

    def add(self, event: dict[str, Any]) -> None:
        facts = event.get("facts") if isinstance(event, dict) else None
        if not isinstance(facts, dict):
            return
        if facts.get("historical_backfill") or facts.get("proactive_activity_review"):
            return
        if event.get("source") == "windows.manual":
            return
        if event.get("sensitivity") == "restricted" and event.get("source") not in {
            "windows.file_activity",
            "windows.visible_text",
        }:
            return
        with self._lock:
            self._events.append(event)

    def build_signal(self, minimum_events: int = 3) -> Signal | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            events = list(self._events)
            if len(events) < max(1, int(minimum_events)):
                return None
            started_at = self._window_started_at
            self._events.clear()
            self._window_started_at = now

        visible_text, source_counts = summarize_activity(events)
        if not visible_text:
            return None
        return Signal(
            source="windows.proactive",
            type="ui.visible_text",
            facts={
                "visible_text": visible_text,
                "context": "proactive_activity_review",
                "analysis_requested": True,
                "proactive_activity_review": True,
                "activity_count": len(events),
                "source_counts": source_counts,
                "window_started_at": started_at.isoformat(),
                "window_ended_at": now.isoformat(),
                "content_kind": "unknown",
                "speaker": "unknown",
                "message_direction": "unknown",
                "visible_only": False,
                "evidence_strength": "contextual",
                "session_key": hashlib.sha256(
                    f"windows-review-v1\0{started_at.isoformat()}".encode("utf-8")
                ).hexdigest()[:32],
                "content_hash": hashlib.sha256(visible_text.encode("utf-8")).hexdigest(),
                "resolution_state": "unknown",
            },
            sensitivity="personal",
            confidence=0.92,
            occurred_at=now.isoformat(),
        )

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


def summarize_activity(events: Iterable[dict[str, Any]], limit: int = 5_000) -> tuple[str, dict[str, int]]:
    source_counts: Counter[str] = Counter()
    foreground: defaultdict[tuple[str, str], int] = defaultdict(int)
    browser: list[str] = []
    clipboard: list[str] = []
    files: list[str] = []
    visible_content: list[str] = []

    for event in events:
        source = str(event.get("source", "unknown"))
        facts = event.get("facts") or {}
        if not isinstance(facts, dict):
            continue
        source_counts[source] += 1
        if source == "windows.foreground":
            app = _clean(facts.get("package"), 100)
            title = _clean(facts.get("window_title") or facts.get("app_label"), 280)
            if app or title:
                foreground[(app, title)] += _positive_int(facts.get("duration_ms"))
        elif source == "windows.browser_history":
            title = _clean(facts.get("page_title"), 280)
            domain = _clean(facts.get("site_domain"), 160)
            _append_unique(browser, " | ".join(value for value in (title, domain) if value), 16)
        elif source == "windows.clipboard":
            value = _clean(facts.get("visible_text") or facts.get("text"), 240)
            if value:
                _append_unique(clipboard, value, 6)
        elif source == "windows.file_activity":
            name = _clean(facts.get("file_name") or facts.get("relative_path"), 240)
            action = _clean(facts.get("change_type") or facts.get("action"), 80)
            value = " | ".join(item for item in (action, name) if item)
            if value:
                _append_unique(files, value, 12)
        elif source == "windows.visible_text":
            content = _clean(facts.get("visible_text"), 900)
            if content:
                kind = _clean(facts.get("content_kind"), 40) or "unknown"
                speaker = _clean(facts.get("speaker"), 40) or "unknown"
                session_key = _clean(facts.get("session_key"), 80)
                _append_unique(
                    visible_content,
                    " | ".join((kind, speaker, session_key, content)),
                    10,
                )

    lines: list[str] = []
    ranked_foreground = sorted(foreground.items(), key=lambda item: item[1], reverse=True)[:16]
    for (app, title), duration_ms in ranked_foreground:
        minutes = max(1, round(duration_ms / 60_000))
        lines.append(f"APP | {app} | {title} | {minutes} min")
    lines.extend(f"BROWSER | {value}" for value in browser)
    lines.extend(f"CLIPBOARD | {value}" for value in clipboard)
    lines.extend(f"FILE | {value}" for value in files)
    lines.extend(f"VISIBLE | {value}" for value in visible_content)
    return "\n".join(lines)[: max(500, int(limit))], dict(source_counts)


def _append_unique(values: list[str], value: str, maximum: int) -> None:
    if value and value not in values and len(values) < maximum:
        values.append(value)


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
