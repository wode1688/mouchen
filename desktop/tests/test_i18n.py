from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk

import pytest

from mouchen_desktop import backend
from mouchen_desktop.agent import DesktopAgent
from mouchen_desktop.backend import BackendClient
from mouchen_desktop.i18n import (
    get_locale,
    install_tk_localization,
    normalize_locale,
    retranslate,
    set_locale,
    set_raw_widget_text,
    set_ui_variable,
    translate_text,
)
from mouchen_desktop.settings import AppSettings, SettingsRepository
from mouchen_desktop.ui import MouchenWindow


class IdentityProtector:
    def protect(self, value: bytes) -> bytes:
        return value

    def unprotect(self, value: bytes) -> bytes:
        return value


def test_locale_normalization_and_interface_translation_leave_advice_untouched():
    assert normalize_locale("zh_CN") == "zh-CN"
    assert normalize_locale("en-GB") == "en-US"
    set_locale("en-US")
    assert translate_text("保存设置") == "Save settings"
    assert translate_text("第一步：打开项目") == "First step: 打开项目"
    assert translate_text("正在记录采纳...") == "Recording Adopt..."
    assert (
        translate_text("通知状态：应用内建言已显示，等待确认")
        == "Notification status: in-app advice shown; awaiting acknowledgement"
    )
    # Server-authored advice is not part of the interface catalogue.
    assert translate_text("先验证真实需求，再扩大投入") == "先验证真实需求，再扩大投入"
    set_locale("zh-CN")
    assert translate_text("Save settings") == "保存设置"


def test_locale_round_trips_without_changing_credentials_or_collection(tmp_path):
    path = tmp_path / "settings.json"
    repository = SettingsRepository(path, IdentityProtector())
    settings = AppSettings(
        locale="en-US",
        bearer_token="secret-token",
        session_user_id="account-1",
        session_username="alice",
        session_origin="http://127.0.0.1:8787",
        collection_enabled=True,
        clipboard_enabled=True,
    )

    repository.save(settings)
    loaded = repository.load()

    assert loaded.locale == "en-US"
    assert loaded.bearer_token == "secret-token"
    assert loaded.session_user_id == "account-1"
    assert loaded.collection_enabled is True
    assert loaded.clipboard_enabled is True


def test_session_locale_is_authoritative_when_present():
    settings = AppSettings(locale="en-US")
    settings.apply_session(
        "token",
        {"user_id": "account-1", "username": "alice", "locale": "zh-CN"},
    )
    assert settings.locale == "zh-CN"


def test_locale_is_not_part_of_worker_identity():
    chinese = AppSettings(locale="zh-CN")
    english = AppSettings(locale="en-US")
    assert DesktopAgent._settings_identity(chinese) == DesktopAgent._settings_identity(english)


def test_backend_locale_contract_sends_header_and_account_preference_body(monkeypatch):
    observed: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"locale":"en-US","updated_at":"2026-08-14T00:00:00Z"}'

    def fake_open(request, timeout):
        observed["url"] = request.full_url
        observed["method"] = request.get_method()
        observed["headers"] = dict(request.header_items())
        observed["body"] = json.loads(request.data.decode("utf-8"))
        observed["timeout"] = timeout
        return Response()

    monkeypatch.setattr(backend, "_open_no_redirect", fake_open)
    client = BackendClient(AppSettings(locale="en-US"))

    result = client.update_account_locale("en-US")

    assert observed["url"] == "http://127.0.0.1:8787/v1/account/preferences"
    assert observed["method"] == "PUT"
    assert observed["body"] == {"locale": "en-US"}
    assert observed["headers"]["Accept-language"] == "en-US"
    assert result["locale"] == "en-US"


def test_existing_widgets_retranslate_without_mutating_user_input():
    install_tk_localization()
    root = tk.Tk()
    root.withdraw()
    try:
        set_locale("zh-CN")
        label = ttk.Label(root, text="保存设置")
        notebook = ttk.Notebook(root)
        page = ttk.Frame(notebook)
        notebook.add(page, text="建言")
        tree = ttk.Treeview(root, columns=("status",), show="headings")
        tree.heading("status", text="状态")
        goal_title = tk.StringVar(root, value="目标")
        direction = tk.StringVar(root, value="保存设置")
        goal_entry = ttk.Entry(root, textvariable=goal_title)
        direction_entry = ttk.Entry(root, textvariable=direction)
        guidance = tk.Text(root)
        guidance.insert("1.0", "保存设置\n目标")

        set_locale("en-US")
        retranslate(root)
        status = tk.StringVar(root)
        set_ui_variable(status, "设置已保存")

        assert label.cget("text") == "Save settings"
        assert notebook.tab(page, "text") == "Advice"
        assert tree.heading("status", "text") == "Status"
        assert status.get() == "Settings saved"
        assert goal_title.get() == "目标"
        assert direction.get() == "保存设置"
        assert goal_entry.get() == "目标"
        assert direction_entry.get() == "保存设置"
        assert guidance.get("1.0", "end-1c") == "保存设置\n目标"

        set_locale("zh-CN")
        retranslate(root)
        assert label.cget("text") == "保存设置"
    finally:
        root.destroy()
        set_locale("zh-CN")


def test_settings_language_switch_is_prominent_and_above_other_settings():
    root = tk.Tk()
    root.withdraw()
    try:
        window = MouchenWindow.__new__(MouchenWindow)
        window.settings_tab = ttk.Frame(root)
        window.agent = type("FakeAgent", (), {"settings": AppSettings()})()

        window._build_settings()

        panel_grid = window.language_panel.grid_info()
        backend_grid = window.settings_backend_entry.grid_info()
        assert window.language_panel.cget("text") == "语言 / Language"
        assert int(panel_grid["row"]) == 0
        assert int(panel_grid["columnspan"]) == 2
        assert int(backend_grid["row"]) == 1
        assert window.locale_box.master is window.language_panel
        assert tuple(window.locale_box.cget("values")) == ("简体中文", "English")
    finally:
        root.destroy()


def test_raw_popup_text_bypasses_tk_translation_hook_and_retranslation():
    install_tk_localization()
    root = tk.Tk()
    root.withdraw()
    try:
        set_locale("en-US")
        label = tk.Label(root)
        message = "建议：保留服务端原文\nFirst step: 第一步：保留服务端原文"
        set_raw_widget_text(label, message)

        assert label.cget("text") == message
        retranslate(root)
        assert label.cget("text") == message
    finally:
        root.destroy()
        set_locale("zh-CN")


def test_stale_locale_task_result_is_rejected_before_touching_ui():
    class FakeAgent:
        @staticmethod
        def ui_session_context_is_current(_context):
            return False

    window = MouchenWindow.__new__(MouchenWindow)
    window.agent = FakeAgent()
    window._account_locale_generation = 7
    stale = {
        "name": "account_locale_update",
        "value": {"locale": "en-US"},
        "context": {"generation": 7, "session_context": (1, "old-account")},
    }

    set_locale("zh-CN")
    window._handle_task(stale)

    assert get_locale() == "zh-CN"


@pytest.mark.parametrize(
    ("pending", "agent_locale"),
    [(True, "zh-CN"), (False, "en-US")],
)
def test_cached_account_locale_status_cannot_revert_english_ui(
    monkeypatch, pending, agent_locale
):
    class Sink:
        def __init__(self):
            self.value = None

        def configure(self, **options):
            self.value = options.get("text")

        def set(self, value):
            self.value = value

    class FakeAgent:
        settings = type("Settings", (), {"locale": agent_locale})()

        @staticmethod
        def ui_session_context_is_current(_context):
            return True

    class Tray:
        def refresh_locale(self):
            raise AssertionError("stale locale must not redraw the tray")

    window = MouchenWindow.__new__(MouchenWindow)
    window.agent = FakeAgent()
    window.root = object()
    window.tray = Tray()
    window.var_locale = Sink()
    window._account_locale_update_pending = pending
    window.connection_label = Sink()
    window.current_app = Sink()
    window.current_title = Sink()
    window.agent_state = Sink()
    window.metric_events = Sink()
    window.metric_pending = Sink()
    window.metric_advice = Sink()
    monkeypatch.setattr("mouchen_desktop.ui.retranslate", lambda _root: None)

    set_locale("en-US")
    try:
        window._apply_status(
            {
                "account_locale": "zh-CN",
                "account_locale_context": (1, "current-account"),
            }
        )
        assert get_locale() == "en-US"
    finally:
        set_locale("zh-CN")


def test_stale_language_preferences_response_is_ignored():
    class FakeAgent:
        settings = type("Settings", (), {"locale": "en-US"})()

        @staticmethod
        def ui_session_context_is_current(_context):
            return True

    class Controller:
        loaded = []

        def load_snapshot(self, payload, *, keep_unsaved):
            self.loaded.append((payload, keep_unsaved))

    window = MouchenWindow.__new__(MouchenWindow)
    window.agent = FakeAgent()
    window.preferences = Controller()
    window._sync_preferences_widgets = lambda: None

    window._handle_preferences_task(
        "advice_preferences_load",
        {
            "value": {"frequency_policies": {"active": {"label": "积极"}}},
            "context": {
                "refresh": True,
                "locale": "zh-CN",
                "session_context": (1, "current-account"),
            },
        },
    )

    assert window.preferences.loaded == []


def test_preference_event_labels_refresh_in_both_locale_directions():
    class Box:
        def __init__(self, text):
            self.text = text

        def configure(self, **options):
            if "text" in options:
                self.text = options["text"]

    window = MouchenWindow.__new__(MouchenWindow)
    window.pref_event_vars = {"focus": object(), "deadline": object()}
    window.pref_event_boxes = {
        "focus": Box("应用切换"),
        "deadline": Box("截止日期"),
    }
    window._rebuild_pref_event_boxes = lambda _options: pytest.fail(
        "stable event IDs should update labels without rebuilding widgets"
    )

    window._sync_pref_event_boxes(
        [("focus", "App switch"), ("deadline", "Deadline")]
    )
    assert window.pref_event_boxes["focus"].text == "App switch"
    assert window.pref_event_boxes["deadline"].text == "Deadline"

    window._sync_pref_event_boxes(
        [("focus", "应用切换"), ("deadline", "截止日期")]
    )
    assert window.pref_event_boxes["focus"].text == "应用切换"
    assert window.pref_event_boxes["deadline"].text == "截止日期"


@pytest.mark.parametrize(
    "name", ["advice_preferences_save", "advice_preferences_delete"]
)
def test_stale_preference_mutation_success_and_error_do_not_touch_new_controller(name):
    class FakeAgent:
        @staticmethod
        def ui_session_context_is_current(_context):
            return False

    class Controller:
        def apply_save_success(self, *_args, **_kwargs):
            raise AssertionError("stale success reached the new account controller")

        def apply_delete_success(self, *_args, **_kwargs):
            raise AssertionError("stale success reached the new account controller")

        def apply_save_failure(self, *_args, **_kwargs):
            raise AssertionError("stale error reached the new account controller")

    window = MouchenWindow.__new__(MouchenWindow)
    window.agent = FakeAgent()
    window.preferences = Controller()
    window._pref_busy = True
    old_context = {"scope_key": "global", "session_context": (1, "old-account")}

    window._handle_preferences_task(
        name,
        {"value": {"preference": {}}, "context": old_context},
    )
    window._handle_preferences_task_error(
        name,
        {"error": "old request failed", "context": old_context},
    )

    assert window._pref_busy is True


def test_latest_locale_error_uses_agent_server_confirmed_rollback(monkeypatch):
    class FakeAgent:
        settings = type("Settings", (), {"locale": "en-US"})()

        @staticmethod
        def ui_session_context_is_current(_context):
            return True

    class ValueSink:
        def __init__(self):
            self.value = None

        def set(self, value):
            self.value = value

    class ConfigureSink:
        def __init__(self):
            self.options = {}

        def configure(self, **options):
            self.options.update(options)

    class Tray:
        refreshed = False

        def refresh_locale(self):
            self.refreshed = True

    window = MouchenWindow.__new__(MouchenWindow)
    window.agent = FakeAgent()
    window._account_locale_generation = 2
    window._account_locale_update_pending = True
    window.root = object()
    window.tray = Tray()
    window.var_locale = ValueSink()
    window.settings_status = ConfigureSink()
    window.preferences = type("Preferences", (), {"loaded": False})()
    window._refresh_local_views = lambda: None
    window._load_advice_preferences = lambda **_options: None
    shown_errors = []
    monkeypatch.setattr("mouchen_desktop.ui.retranslate", lambda _root: None)
    monkeypatch.setattr(
        "mouchen_desktop.ui.messagebox.showerror",
        lambda title, error, **_options: shown_errors.append((title, error)),
    )

    set_locale("zh-CN")
    try:
        window._handle_task_error(
            {
                "name": "account_locale_update",
                "error": "latest locale update failed",
                "context": {
                    "generation": 2,
                    "session_context": (1, "alice"),
                    "previous_locale": "zh-CN",
                },
            }
        )

        assert get_locale() == "en-US"
        assert window.var_locale.value == "English"
        assert window.tray.refreshed is True
        assert window._account_locale_update_pending is False
        assert "latest locale update failed" in window.settings_status.options["text"]
        assert shown_errors == [("语言切换失败", "latest locale update failed")]
    finally:
        set_locale("zh-CN")


def test_english_advice_detail_hides_han_generated_text_until_translation_ready():
    window = MouchenWindow.__new__(MouchenWindow)
    window._display_time = lambda value: f"display({value})"
    advice = {
        "goal_quote": "保存设置是用户原话",
        "action": "建议：先验证真实需求",
        "first_step": "第一步：打开项目",
        "alternative": "无",
        "prediction": {
            "outcome": "预测：这段服务端正文必须保留",
            "deadline": "2026-08-15T01:02:03Z",
        },
    }

    set_locale("en-US")
    try:
        assert window._advice_detail_lines(advice) == [
            "Original source: 保存设置是用户原话",
            "",
            "Advice: Translation pending",
            "First step: Translation pending",
            "Alternative: Translation pending",
            "Prediction: Translation pending",
            "Verification time: display(2026-08-15T01:02:03Z)",
        ]

        without_alternative = dict(advice, alternative=None)
        assert window._advice_detail_lines(without_alternative)[4] == "Alternative: None"
    finally:
        set_locale("zh-CN")


def test_english_dynamic_advice_statuses_are_localized_before_tree_insert():
    window = MouchenWindow.__new__(MouchenWindow)
    window._display_time = lambda _value: "08-15 09:30"

    set_locale("en-US")
    try:
        assert window._status_label("active") == "Pending"
        assert window._status_label("adopted") == "Adopted"
        assert window._status_label("server-defined") == "server-defined"
        assert window._advice_list_status(
            {
                "status": "active",
                "snoozed_until": "2999-08-15T09:30:00Z",
            }
        ) == "Later: 08-15 09:30"
    finally:
        set_locale("zh-CN")
