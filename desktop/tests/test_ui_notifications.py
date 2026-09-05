from __future__ import annotations

import tkinter as tk
from datetime import datetime, timezone
from types import SimpleNamespace

from mouchen_desktop.i18n import set_locale
from mouchen_desktop.ui import (
    _ADVICE_RECONCILE_LIMIT,
    MouchenWindow,
    _NOTIFICATION_STATE_KEY,
    _analysis_state_text,
    _notification_delivery_key,
    _notification_timestamp,
)


class MemoryStore:
    def __init__(self, advice=None):
        self.states = {}
        self.advice = list(advice or [])
        self.requested_limits = []

    def save_state(self, key, value):
        self.states[key] = dict(value)

    def load_state(self, key, default=None):
        return dict(self.states.get(key, default or {}))

    def list_advice(self, limit=100):
        self.requested_limits.append(limit)
        return self.advice[:limit]

    def save_advice(self, value):
        advice_id = str(value["id"])
        for index, existing in enumerate(self.advice):
            if str(existing.get("id")) == advice_id:
                changed = existing != value
                self.advice[index] = dict(value)
                return changed
        self.advice.append(dict(value))
        return True


def make_window(store, tray, popup_result=True):
    window = MouchenWindow.__new__(MouchenWindow)

    class Agent:
        settings = SimpleNamespace(notifications_enabled=True)

        def __init__(self):
            self.attention_reports = []
            self.feedback_reports = []

        def feedback(self, advice_id, kind, note=None):
            self.feedback_reports.append((advice_id, kind, note))
            return {
                "status": "recorded",
                "advice": {"id": advice_id, "status": "active"},
            }

        def complete_attention(self, advice_id, claim_token):
            self.attention_reports.append(("complete", advice_id, claim_token, None))
            return {"status": "delivered"}

        def fail_attention(self, advice_id, claim_token, reason=None):
            self.attention_reports.append(("fail", advice_id, claim_token, reason))
            return {"status": "released"}

    window.agent = Agent()
    window.store = store
    window.tray = tray
    window._advice_popups = []
    window._visible_advice_popup_ids = set()
    window._advice_popups_by_id = {}
    window._notification_queue = []
    window._queued_advice_ids = set()
    window._attention_claims = {}
    window._notification_watermark = ("", "")
    window._notification_startup_watermark = ("", "")
    window._notification_next_dispatch_at = 0.0
    window._notification_initialized = False
    window._show_advice_popup = lambda title, message, advice_id, level: popup_result
    window._refresh_local_views = lambda: None
    window._run_task = lambda _name, function: function()
    return window


def claim(item, delivery_number=1, token=None):
    return {
        "status": "claimed",
        "claim_token": token or f"claim-{item['id']}-{delivery_number}",
        "lease_expires_at": "2026-08-05T09:00:00Z",
        "delivery_number": delivery_number,
        "advice": dict(item),
    }


def advice(advice_id="advice-1"):
    digits = "".join(character for character in advice_id if character.isdigit())
    sequence = int(digits or "1") % 60
    return {
        "id": advice_id,
        "created_at": f"2026-08-05T08:00:{sequence:02d}Z",
        "status": "active",
        "effective_level": 2,
        "domain": "work",
        "action": "检查失败原因",
        "first_step": "打开失败详情",
    }


def test_analysis_state_distinguishes_queue_failure_and_no_intervention():
    assert _analysis_state_text({"online": False}) == "后端离线 · 主动分析未运行"
    assert _analysis_state_text(
        {"online": True, "event_sync_deferred_for_quota": True}
    ) == "事件同步暂缓 · 云端存储空间已满，将自动重试"
    assert _analysis_state_text(
        {
            "online": True,
            "analysis_status": {
                "waiting": 3,
                "counts": {"running": 0, "retry": 0},
                "waiting_reasons": {"analysis_budget_deferred": 3},
                "last_status": "pending",
                "last_reason": "analysis_budget_deferred",
            },
        }
    ) == "分析排队中 · 3 项 · 等待预算窗口"
    assert _analysis_state_text(
        {
            "online": True,
            "analysis_status": {
                "waiting": 0,
                "counts": {"running": 0, "retry": 0},
                "last_status": "no_intervention",
                "last_reason": "codex_cli_not_logged_in",
                "last_failure_reason": "codex_cli_not_logged_in",
            },
        }
    ) == "最近分析失败 · Codex 未登录"
    assert _analysis_state_text(
        {
            "online": True,
            "analysis_status": {
                "waiting": 0,
                "counts": {"running": 0, "retry": 0},
                "last_status": "no_intervention",
                "last_reason": "evaluation_noop",
            },
        }
    ) == "主动守候中 · 最近一次未达到建言标准"


def test_successful_advice_notification_records_delivery_checkpoint():
    class Tray:
        last_notification_error = None

        def notify(self, title, message, level):
            assert title == "AI替身 L2 · work"
            assert "第一步：打开失败详情" in message
            assert level == 2
            return True

    store = MemoryStore()
    window = make_window(store, Tray())

    assert window._deliver_advice_notification(advice()) is True

    latest = store.states[_NOTIFICATION_STATE_KEY]
    assert latest == store.states[_notification_delivery_key("advice-1")]
    assert latest["advice_id"] == "advice-1"
    assert latest["channel"] == "windows_tray+in_app_popup"
    assert latest["delivery_status"] == "awaiting_acknowledgement"
    assert latest["tray_status"] == "accepted"
    assert latest["popup_status"] == "visible"
    assert latest["attempt_count"] == 1


def test_english_system_notification_hides_han_generated_text_until_ready():
    original_action = "建议：保留这段服务端正文"
    original_first_step = "第一步：打开原始记录"
    item = advice("advice-11")
    item.update(action=original_action, first_step=original_first_step)

    class Tray:
        last_notification_error = None

        def notify(self, title, message, level):
            assert title == "My AI Twin L2 · work"
            assert message == "Translation pending\nFirst step: Translation pending"
            assert original_action not in message
            assert original_first_step not in message
            assert level == 2
            return True

    set_locale("en-US")
    try:
        assert make_window(MemoryStore(), Tray())._deliver_advice_notification(item)
    finally:
        set_locale("zh-CN")


def test_popup_still_delivers_when_windows_suppresses_notification():
    class Tray:
        last_notification_error = "Windows suppressed the notification"

        def notify(self, title, message, level):
            return False

    store = MemoryStore()
    window = make_window(store, Tray())

    assert window._deliver_advice_notification(advice("advice-2")) is True

    state = store.states[_NOTIFICATION_STATE_KEY]
    assert state["channel"] == "in_app_popup"
    assert state["delivery_status"] == "awaiting_acknowledgement"
    assert state["tray_status"] == "failed"
    assert "Windows suppressed" in state["last_error"]


def test_failed_notification_is_persisted_for_visible_diagnostics():
    class Tray:
        last_notification_error = "tray unavailable"

        def notify(self, title, message, level):
            return False

    store = MemoryStore()
    window = make_window(store, Tray(), popup_result=False)

    assert window._deliver_advice_notification(advice("advice-3")) is False

    state = store.states[_NOTIFICATION_STATE_KEY]
    assert state["delivery_status"] == "failed"
    assert state["channel"] == ""
    assert state["tray_status"] == "failed"
    assert state["popup_status"] == "failed"
    assert "tray unavailable" in state["last_error"]
    assert "弹窗创建失败" in state["last_error"]


def test_unacknowledged_advice_is_not_replayed_locally_after_restart():
    current = advice("advice-4")
    store = MemoryStore([current])
    store.save_state(
        _NOTIFICATION_STATE_KEY,
        {
            "advice_id": "advice-4",
            "delivery_status": "awaiting_acknowledgement",
            "channel": "in_app_popup",
        },
    )

    class Tray:
        last_notification_error = None

        def notify(self, title, message, level):
            return True

    window = make_window(store, Tray())
    delivered = []

    def deliver(item):
        delivered.append(item)
        return True

    window._deliver_advice_notification = deliver

    window._notify_latest_unseen_advice()
    assert delivered == []

    store.states[_NOTIFICATION_STATE_KEY]["acknowledged_at"] = "2026-08-05T08:00:00Z"
    window._notify_latest_unseen_advice()
    assert delivered == []


def test_startup_watermark_does_not_replay_unattempted_history():
    history = [advice("advice-1"), advice("advice-2")]
    store = MemoryStore(history)
    window = make_window(store, SimpleNamespace())
    startup_at = datetime(2026, 8, 5, 8, 5, tzinfo=timezone.utc)

    window._initialize_notification_queue(startup_at=startup_at)

    assert window._notification_queue == []
    assert window._notification_startup_watermark == (
        _notification_timestamp(startup_at),
        "",
    )
    assert store.requested_limits == [_ADVICE_RECONCILE_LIMIT]


def test_advice_pull_only_updates_display_and_never_triggers_notification():
    store = MemoryStore()
    window = make_window(store, SimpleNamespace())
    startup_at = datetime(2026, 8, 5, 8, 10, tzinfo=timezone.utc)
    window._initialize_notification_queue(startup_at=startup_at)
    delivered = []
    window._deliver_advice_notification = lambda item: delivered.append(item["id"]) or True

    historical = advice("advice-10")
    historical["created_at"] = "2026-08-05T16:00:00+08:00"
    historical["_mouchen_locally_new"] = True
    store.advice.append(historical)
    window._handle_new_advice(historical)

    assert delivered == []
    assert window._notification_queue == []

    published_after_startup = advice("advice-11")
    published_after_startup["created_at"] = "2026-08-05T08:10:01Z"
    published_after_startup["_mouchen_locally_new"] = True
    store.advice.append(published_after_startup)
    window._handle_new_advice(published_after_startup)

    assert delivered == []
    assert window._notification_queue == []


def test_translation_only_history_update_never_replays_an_old_notification():
    historical = advice("advice-translation")
    store = MemoryStore([historical])
    window = make_window(store, SimpleNamespace())
    window._initialize_notification_queue(
        startup_at=datetime(2026, 8, 5, 8, 10, tzinfo=timezone.utc)
    )
    delivered = []
    window._deliver_advice_notification = lambda item: delivered.append(item["id"]) or True

    translated = {
        **historical,
        "display_locale": "en-US",
        "display_translation_status": "ready",
        "display": {
            "action": "Review the failure",
            "first_step": "Open the failure details",
            "alternative": None,
            "prediction_outcome": "The issue remains unresolved",
            "adopted_expected_result": None,
        },
        "_mouchen_locally_new": False,
    }
    assert store.save_advice(translated) is True
    window._handle_new_advice(translated)
    window._process_notification_queue(force=True)

    assert delivered == []
    assert window._notification_queue == []
    assert window._attention_claims == {}


def test_only_a_server_claim_can_trigger_notification_and_is_completed():
    current = advice("advice-2")
    store = MemoryStore([current])
    window = make_window(store, SimpleNamespace())
    delivered = []
    window._deliver_advice_notification = lambda item: delivered.append(item["id"]) or True

    window._handle_new_advice({**current, "_mouchen_locally_new": True})
    assert delivered == []

    window._handle_attention_claim(claim(current, delivery_number=1, token="token-1"))

    assert delivered == ["advice-2"]
    assert window._notification_queue == []
    assert window.agent.attention_reports == [
        ("complete", "advice-2", "token-1", None)
    ]


def test_reclaimed_same_delivery_is_completed_without_a_second_popup():
    current = advice("advice-12")
    store = MemoryStore([current])
    store.save_state(
        _notification_delivery_key("advice-12"),
        {
            "advice_id": "advice-12",
            "delivery_status": "delivered_unconfirmed",
            "delivered_at": "2026-08-05T08:00:00Z",
            "delivery_number": 1,
        },
    )
    window = make_window(store, SimpleNamespace())
    delivered = []
    window._deliver_advice_notification = lambda item: delivered.append(item["id"]) or True

    window._handle_attention_claim(claim(current, delivery_number=1, token="new-token"))

    assert delivered == []
    assert window.agent.attention_reports == [
        ("complete", "advice-12", "new-token", None)
    ]


def test_second_server_delivery_is_allowed_once_even_if_first_popup_is_visible():
    current = advice("advice-13")
    store = MemoryStore([current])
    store.save_state(
        _notification_delivery_key("advice-13"),
        {
            "advice_id": "advice-13",
            "delivery_status": "awaiting_acknowledgement",
            "delivered_at": "2026-08-05T08:00:00Z",
            "delivery_number": 1,
        },
    )
    window = make_window(store, SimpleNamespace())
    window._visible_advice_popup_ids.add("advice-13")
    delivered = []
    window._deliver_advice_notification = lambda item: delivered.append(item["id"]) or True

    window._handle_attention_claim(claim(current, delivery_number=2, token="token-2"))

    assert delivered == ["advice-13"]
    assert window.agent.attention_reports == [
        ("complete", "advice-13", "token-2", None)
    ]


def test_failed_delivery_is_released_to_server_and_not_locally_retried():
    current = advice("advice-7")
    store = MemoryStore([current])

    class Tray:
        last_notification_error = "tray unavailable"

        def notify(self, title, message, level):
            return False

    window = make_window(store, Tray(), popup_result=False)
    window._handle_attention_claim(claim(current, token="token-failed"))

    assert window._notification_queue == []
    state_key = _notification_delivery_key("advice-7")
    assert store.states[state_key]["attempt_count"] == 1
    assert store.states[state_key]["next_retry_at"]
    assert window.agent.attention_reports[0][:3] == (
        "fail",
        "advice-7",
        "token-failed",
    )


def test_stale_local_queue_entry_without_server_claim_is_discarded():
    stale = advice("advice-1")
    store = MemoryStore([stale])
    window = make_window(store, SimpleNamespace())
    window._initialize_notification_queue()
    window._enqueue_advice_notification("advice-1")
    delivered = []
    window._deliver_advice_notification = lambda item: delivered.append(item["id"]) or True

    assert window._process_notification_queue(now=10.0) is False
    assert delivered == []
    assert window._notification_queue == []


def test_terminal_queue_entry_is_cleaned_without_consuming_the_dispatch_slot():
    terminal = advice("advice-1")
    terminal["status"] = "withdrawn"
    ready = advice("advice-2")
    store = MemoryStore([terminal, ready])
    window = make_window(store, SimpleNamespace())
    window._initialize_notification_queue(
        startup_at=datetime(2026, 8, 5, 8, 5, tzinfo=timezone.utc)
    )
    window._enqueue_advice_notification("advice-1")
    window._enqueue_advice_notification("advice-2")
    window._attention_claims["advice-2"] = claim(ready, token="token-ready")
    delivered = []
    window._deliver_advice_notification = lambda item: delivered.append(item["id"]) or True

    assert window._process_notification_queue(now=10.0) is True

    assert delivered == ["advice-2"]
    assert store.states[_notification_delivery_key("advice-1")]["delivery_status"] == "withdrawn"
    assert window.agent.attention_reports[0][:3] == (
        "complete",
        "advice-2",
        "token-ready",
    )


def test_notification_reconciliation_reads_at_most_500_advice_records():
    records = [advice(f"advice-{index:04d}") for index in range(501)]
    outside_window = records[-1]
    store = MemoryStore(records)
    store.save_state(
        _notification_delivery_key(outside_window["id"]),
        {
            "advice_id": outside_window["id"],
            "delivery_status": "failed",
            "attempted_at": "2026-08-05T08:00:00Z",
        },
    )
    window = make_window(store, SimpleNamespace())

    window._initialize_notification_queue(
        startup_at=datetime(2026, 8, 5, 8, 5, tzinfo=timezone.utc)
    )

    assert store.requested_limits == [_ADVICE_RECONCILE_LIMIT]
    assert outside_window["id"] not in window._queued_advice_ids


def test_terminal_status_closes_popup_removes_queue_and_persists_status():
    current = advice("advice-8")
    current["status"] = "withdrawn"
    store = MemoryStore([current])
    store.save_state(
        _notification_delivery_key("advice-8"),
        {
            "advice_id": "advice-8",
            "delivery_status": "awaiting_acknowledgement",
        },
    )

    class Popup:
        destroyed = False

        def winfo_exists(self):
            return not self.destroyed

        def destroy(self):
            self.destroyed = True

    popup = Popup()
    window = make_window(store, SimpleNamespace())
    window._advice_popups = [popup]
    window._advice_popups_by_id["advice-8"] = popup
    window._visible_advice_popup_ids.add("advice-8")
    window._notification_queue = ["advice-8"]
    window._queued_advice_ids.add("advice-8")

    window._finalize_advice_notification("advice-8", "withdrawn")

    assert popup.destroyed is True
    assert "advice-8" not in window._visible_advice_popup_ids
    assert "advice-8" not in window._queued_advice_ids
    assert store.states[_notification_delivery_key("advice-8")]["delivery_status"] == "withdrawn"


def test_popup_confirmation_closes_and_acknowledges_advice_outside_reconcile_window():
    store = MemoryStore()
    initial = {
        "advice_id": "advice-older-than-500",
        "delivery_status": "awaiting_acknowledgement",
        "channel": "in_app_popup",
        "popup_status": "visible",
    }
    store.save_state(_NOTIFICATION_STATE_KEY, initial)
    store.save_state(_notification_delivery_key(initial["advice_id"]), initial)

    class Popup:
        destroyed = False

        def winfo_exists(self):
            return not self.destroyed

        def destroy(self):
            self.destroyed = True

    popup = Popup()
    window = make_window(store, SimpleNamespace())
    window._advice_popups = [popup]
    window._advice_popups_by_id[initial["advice_id"]] = popup
    window._visible_advice_popup_ids.add(initial["advice_id"])

    assert window._close_advice_popup(initial["advice_id"], popup, "opened") is True

    state = store.states[_notification_delivery_key(initial["advice_id"])]
    assert popup.destroyed is True
    assert state["delivery_status"] == "acknowledged"
    assert state["popup_status"] == "closed"
    assert initial["advice_id"] not in window._visible_advice_popup_ids


def test_popup_knows_button_records_acknowledged_feedback_on_server():
    current = advice("advice-acknowledged")
    store = MemoryStore([current])
    initial = {
        "advice_id": current["id"],
        "delivery_status": "awaiting_acknowledgement",
        "channel": "in_app_popup",
        "popup_status": "visible",
    }
    store.save_state(_NOTIFICATION_STATE_KEY, initial)
    store.save_state(_notification_delivery_key(current["id"]), initial)

    class Popup:
        destroyed = False

        def winfo_exists(self):
            return not self.destroyed

        def destroy(self):
            self.destroyed = True

    popup = Popup()
    window = make_window(store, SimpleNamespace())
    window._advice_popups = [popup]
    window._advice_popups_by_id[current["id"]] = popup
    window._visible_advice_popup_ids.add(current["id"])

    assert window._close_advice_popup(current["id"], popup, "acknowledged") is True

    assert popup.destroyed is True
    assert window.agent.feedback_reports == [
        (current["id"], "acknowledged", None)
    ]
    state = store.states[_notification_delivery_key(current["id"])]
    assert state["delivery_status"] == "acknowledged"
    assert state["acknowledgement"] == "acknowledged"


def test_popup_creation_failure_destroys_partial_window_and_tracking(monkeypatch):
    class BrokenPopup:
        destroyed = False

        def winfo_exists(self):
            return not self.destroyed

        def destroy(self):
            self.destroyed = True

        def title(self, _value):
            return None

        def configure(self, **_values):
            raise tk.TclError("popup setup failed")

    popup = BrokenPopup()
    monkeypatch.setattr(tk, "Toplevel", lambda _root: popup)
    window = make_window(MemoryStore(), SimpleNamespace())
    window.root = object()
    window._show_advice_popup = MouchenWindow._show_advice_popup.__get__(
        window, MouchenWindow
    )

    assert window._show_advice_popup("AI替身", "建言", "advice-broken", 2) is False

    assert popup.destroyed is True
    assert window._advice_popups == []
    assert "advice-broken" not in window._visible_advice_popup_ids
    assert "advice-broken" not in window._advice_popups_by_id


def test_popup_acknowledgement_updates_latest_and_per_advice_state():
    store = MemoryStore()
    initial = {
        "advice_id": "advice-5",
        "delivery_status": "awaiting_acknowledgement",
        "channel": "in_app_popup",
        "popup_status": "visible",
    }
    store.save_state(_NOTIFICATION_STATE_KEY, initial)
    store.save_state(_notification_delivery_key("advice-5"), initial)
    window = make_window(store, SimpleNamespace())

    window._acknowledge_advice_notification("advice-5", "opened")

    latest = store.states[_NOTIFICATION_STATE_KEY]
    per_advice = store.states[_notification_delivery_key("advice-5")]
    assert latest == per_advice
    assert latest["delivery_status"] == "acknowledged"
    assert latest["acknowledgement"] == "opened"
    assert latest["acknowledged_at"]
    assert latest["popup_status"] == "closed"
    assert latest["closed_at"]


def test_partial_channel_failure_remains_visible_in_advice_tab():
    store = MemoryStore()
    store.save_state(
        _NOTIFICATION_STATE_KEY,
        {
            "advice_id": "advice-6",
            "delivery_status": "awaiting_acknowledgement",
            "delivered_at": "2026-08-05T08:00:00Z",
            "last_error": "tray unavailable",
        },
    )

    class Label:
        configured = {}

        def configure(self, **values):
            self.configured = values

    window = make_window(store, SimpleNamespace())
    window.notification_status = Label()

    window._refresh_notification_status()

    assert "部分渠道失败：tray unavailable" in window.notification_status.configured["text"]
    assert window.notification_status.configured["style"] == "NotificationError.TLabel"
