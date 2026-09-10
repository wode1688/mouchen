from __future__ import annotations

import hashlib
import json
import queue
from types import SimpleNamespace

from mouchen_desktop.advice_display import (
    TRANSLATION_PENDING,
    TRANSLATION_UNAVAILABLE,
    project_advice,
)
from mouchen_desktop.i18n import set_locale
from mouchen_desktop.ui import MouchenWindow


def advice_payload(status: str = "ready") -> dict:
    return {
        "id": "advice-localized",
        "created_at": "2026-08-15T08:00:00Z",
        "status": "active",
        "effective_level": 2,
        "domain": "work",
        "goal_quote": "用户原话：完成发布",
        "evidence": [
            {
                "source": "windows.visible_text",
                "fact": "原始证据：构建仍然失败",
            }
        ],
        "action": "中文动作：核对失败原因",
        "first_step": "中文第一步：打开构建记录",
        "alternative": "中文替代路径：回滚",
        "prediction": {
            "outcome": "中文预测：发布继续阻塞",
            "deadline": "2026-08-16T08:00:00Z",
        },
        "adopted_expected_result": "中文采纳结果：恢复发布",
        "display_locale": "en-US",
        "display_translation_status": status,
        "display": {
            "action": "Review the failed build",
            "first_step": "Open the build record",
            "alternative": "Roll back to the stable release",
            "prediction_outcome": "The release remains blocked",
            "adopted_expected_result": "The release path is restored",
        },
    }


def payload_hash(value: dict) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_ready_projection_switches_both_directions_without_mutating_payload():
    advice = advice_payload()
    before = payload_hash(advice)

    english = project_advice(advice, "en-US")
    chinese = project_advice(advice, "zh-CN")

    assert english.uses_translation is True
    assert english.translation_status == "ready"
    assert english.action == "Review the failed build"
    assert english.first_step == "Open the build record"
    assert english.prediction_outcome == "The release remains blocked"
    assert english.adopted_expected_result == "The release path is restored"
    assert chinese.uses_translation is False
    assert chinese.action == "中文动作：核对失败原因"
    assert chinese.first_step == "中文第一步：打开构建记录"
    assert chinese.prediction_outcome == "中文预测：发布继续阻塞"
    assert payload_hash(advice) == before


def test_pending_and_invalid_ready_never_fall_back_to_original_action_or_first_step():
    pending = advice_payload("pending")
    before = payload_hash(pending)
    projected = project_advice(pending, "en-US")
    assert projected.action == TRANSLATION_PENDING
    assert projected.first_step == TRANSLATION_PENDING
    assert "中文动作" not in projected.action
    assert "中文第一步" not in projected.first_step

    invalid_ready = advice_payload("ready")
    invalid_ready["display"].pop("first_step")
    projected = project_advice(invalid_ready, "en-US")
    assert projected.translation_status == "unavailable"
    assert projected.action == TRANSLATION_UNAVAILABLE
    assert projected.first_step == TRANSLATION_UNAVAILABLE
    assert payload_hash(pending) == before

    han_ready = advice_payload("ready")
    han_ready["display"]["action"] = "中文错误翻译"
    projected = project_advice(han_ready, "en-US")
    assert projected.translation_status == "unavailable"
    assert projected.action == TRANSLATION_UNAVAILABLE


def test_english_original_is_allowed_but_han_original_waits_for_translation():
    english = advice_payload("original")
    english.update(
        action="Review the failure",
        first_step="Open the failure details",
        alternative="Use the verified rollback path",
        prediction={
            "outcome": "The release remains blocked",
            "deadline": "2026-08-16T08:00:00Z",
        },
        adopted_expected_result="The release path is restored",
    )
    projected = project_advice(english, "en-US")
    assert projected.translation_status == "original"
    assert projected.action == "Review the failure"

    han = advice_payload("original")
    han.pop("display_translation_status")
    projected = project_advice(han, "en-US")
    assert projected.translation_status == "pending"
    assert projected.action == TRANSLATION_PENDING
    assert projected.first_step == TRANSLATION_PENDING

    for status in ("pending", "unavailable"):
        chinese = project_advice(advice_payload(status), "zh-CN")
        assert chinese.translation_status == "original"
        assert chinese.action == "中文动作：核对失败原因"
        assert chinese.first_step == "中文第一步：打开构建记录"


def test_detail_uses_translation_but_keeps_original_quote_and_evidence():
    window = MouchenWindow.__new__(MouchenWindow)
    window._display_time = lambda value: f"display({value})"
    advice = advice_payload()
    before = payload_hash(advice)

    set_locale("en-US")
    try:
        rendered = "\n".join(window._advice_detail_lines(advice))
    finally:
        set_locale("zh-CN")

    assert "Original source: 用户原话：完成发布" in rendered
    assert "Evidence: 原始证据：构建仍然失败" in rendered
    assert "Advice: Review the failed build" in rendered
    assert "First step: Open the build record" in rendered
    assert "Prediction: The release remains blocked" in rendered
    assert "Expected result if adopted: The release path is restored" in rendered
    assert "中文动作：核对失败原因" not in rendered
    assert payload_hash(advice) == before


def test_pending_and_unavailable_detail_never_expose_original_generated_text():
    window = MouchenWindow.__new__(MouchenWindow)
    window._display_time = lambda value: f"display({value})"

    set_locale("en-US")
    try:
        pending = "\n".join(window._advice_detail_lines(advice_payload("pending")))
        unavailable = "\n".join(
            window._advice_detail_lines(advice_payload("unavailable"))
        )
    finally:
        set_locale("zh-CN")

    assert TRANSLATION_PENDING in pending
    assert "中文动作：核对失败原因" not in pending
    assert "中文第一步：打开构建记录" not in pending
    assert TRANSLATION_UNAVAILABLE in unavailable
    assert "中文动作：核对失败原因" not in unavailable
    assert "中文第一步：打开构建记录" not in unavailable


class FakeTree:
    def __init__(self):
        self.rows: dict[str, tuple] = {}
        self.selected: tuple[str, ...] = ()
        self.focused = ""

    def selection(self):
        return self.selected

    def focus(self, value=None):
        if value is not None:
            self.focused = value
        return self.focused

    def get_children(self):
        return tuple(self.rows)

    def delete(self, item):
        self.rows.pop(item, None)

    def insert(self, _parent, _index, iid, values):
        self.rows[iid] = tuple(values)

    def exists(self, item):
        return item in self.rows

    def selection_set(self, item):
        self.selected = (item,)

    def see(self, _item):
        return None


def test_advice_and_overview_rows_share_the_same_locale_projection():
    window = MouchenWindow.__new__(MouchenWindow)
    window.selected_advice_id = None
    window._display_time = lambda _value: "08-15 16:00"
    window._advice_list_status = lambda _value: "Pending"
    window.advice_tree = FakeTree()
    window.overview_advice = FakeTree()
    advice = advice_payload()

    set_locale("en-US")
    try:
        window._fill_advice_tree(window.advice_tree, [advice], detailed=True)
        window._fill_advice_tree(window.overview_advice, [advice], detailed=False)
        assert window.advice_tree.rows[advice["id"]][3] == "Review the failed build"
        assert window.overview_advice.rows[advice["id"]][2] == "Review the failed build"

        for status, expected in (
            ("pending", TRANSLATION_PENDING),
            ("unavailable", TRANSLATION_UNAVAILABLE),
        ):
            localized = advice_payload(status)
            window._fill_advice_tree(window.advice_tree, [localized], detailed=True)
            window._fill_advice_tree(
                window.overview_advice, [localized], detailed=False
            )
            assert window.advice_tree.rows[localized["id"]][3] == expected
            assert window.overview_advice.rows[localized["id"]][2] == expected

        set_locale("zh-CN")
        window._fill_advice_tree(window.advice_tree, [advice], detailed=True)
        window._fill_advice_tree(window.overview_advice, [advice], detailed=False)
        assert window.advice_tree.rows[advice["id"]][3] == "中文动作：核对失败原因"
        assert window.overview_advice.rows[advice["id"]][2] == "中文动作：核对失败原因"
    finally:
        set_locale("zh-CN")


class MemoryStateStore:
    def __init__(self):
        self.states = {}

    def load_state(self, key, default=None):
        return dict(self.states.get(key, default or {}))

    def save_state(self, key, value):
        self.states[key] = dict(value)


def notification_window(messages):
    window = MouchenWindow.__new__(MouchenWindow)
    window.agent = SimpleNamespace(settings=SimpleNamespace(notifications_enabled=True))
    window.store = MemoryStateStore()
    window._attention_claims = {}
    window._refresh_notification_status = lambda: None

    class Tray:
        last_notification_error = None

        def notify(self, title, message, level):
            messages.append(("tray", title, message, level))
            return True

    window.tray = Tray()
    window._show_advice_popup = (
        lambda title, message, advice_id, level: messages.append(
            ("popup", title, message, level)
        )
        is None
    )
    return window


def test_tray_and_popup_share_ready_and_pending_projections():
    messages = []
    set_locale("en-US")
    try:
        assert notification_window(messages)._deliver_advice_notification(advice_payload())
        assert messages[0][2] == "Review the failed build\nFirst step: Open the build record"
        assert messages[1][2] == messages[0][2]

        messages.clear()
        assert notification_window(messages)._deliver_advice_notification(
            advice_payload("pending")
        )
        assert messages[0][2] == f"{TRANSLATION_PENDING}\nFirst step: {TRANSLATION_PENDING}"
        assert "中文动作" not in messages[0][2]
        assert messages[1][2] == messages[0][2]

        messages.clear()
        assert notification_window(messages)._deliver_advice_notification(
            advice_payload("unavailable")
        )
        assert messages[0][2] == (
            f"{TRANSLATION_UNAVAILABLE}\n"
            f"First step: {TRANSLATION_UNAVAILABLE}"
        )
        assert "中文动作" not in messages[0][2]
        assert messages[1][2] == messages[0][2]
    finally:
        set_locale("zh-CN")


def test_queued_advice_and_attention_are_fenced_after_account_switch():
    window = MouchenWindow.__new__(MouchenWindow)
    window.ui_queue = queue.Queue()

    class Agent:
        context = (1, "account-a")

        def ui_session_context(self):
            return self.context

        def ui_session_context_is_current(self, context):
            return context == self.context

    window.agent = Agent()
    payload = advice_payload()
    window._queue_session_event("advice", payload)
    kind, queued = window.ui_queue.get_nowait()
    assert kind == "advice"
    assert window._current_session_event(queued) is payload

    window._queue_session_event("attention", {"status": "claimed"})
    _, stale = window.ui_queue.get_nowait()
    window.agent.context = (2, "account-b")
    assert window._current_session_event(stale) is None
