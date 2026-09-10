from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from mouchen_desktop.collectors import (
    ActiveWindowCollector,
    BrowserHistoryCollector,
    ClipboardCollector,
    ChromiumHistoryProfile,
    FileActivityCollector,
    ForegroundWindow,
    VisibleTextCollector,
)
from mouchen_desktop.privacy import prepare_outbound, redact_text
from mouchen_desktop.settings import AppSettings


def test_active_window_emits_finished_session():
    windows = iter(
        [
            ForegroundWindow("code.exe", "Mouchen - Visual Studio Code", 777),
            ForegroundWindow("chrome.exe", "GitHub", 778),
        ]
    )
    collector = ActiveWindowCollector(reader=lambda: next(windows), minimum_session_seconds=5)

    assert collector.poll(now=100) == []
    signals = collector.poll(now=112)

    assert len(signals) == 1
    assert signals[0].type == "app.foreground_session"
    assert signals[0].facts["package"] == "code.exe"
    assert signals[0].facts["duration_ms"] == 12_000


def test_clipboard_uses_initial_value_as_baseline():
    values = iter(["old value", "new deadline error", "new deadline error"])
    collector = ClipboardCollector(reader=lambda: next(values))

    assert collector.poll() == []
    signals = collector.poll()
    assert len(signals) == 1
    assert signals[0].facts["visible_text"] == "new deadline error"
    assert collector.poll() == []


def test_file_collector_emits_only_after_baseline(tmp_path):
    watched = tmp_path / "work"
    watched.mkdir()
    document = watched / "status.md"
    document.write_text("all good", encoding="utf-8")
    collector = FileActivityCollector()

    assert collector.poll([str(watched)], [".md"], include_content=True) == []
    document.write_text("customer deadline is overdue", encoding="utf-8")
    signals = collector.poll([str(watched)], [".md"], include_content=True)

    assert len(signals) == 1
    assert signals[0].facts["file_name"] == "status.md"
    assert "overdue" in signals[0].facts["visible_text"]
    assert signals[0].facts["content_included"] is True


def test_browser_history_backfills_then_reads_only_new_visits(tmp_path):
    history = tmp_path / "History"
    connection = sqlite3.connect(history)
    connection.executescript(
        """
        CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER);
        CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);
        """
    )
    now = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)
    to_chromium = BrowserHistoryCollector._to_chromium_time
    connection.executemany(
        "INSERT INTO urls(id, url, title, visit_count) VALUES(?, ?, ?, ?)",
        [
            (1, "https://example.com/private/path?token=secret", "Project deadline warning", 2),
            (2, "chrome://settings/", "Settings", 1),
        ],
    )
    connection.executemany(
        "INSERT INTO visits(id, url, visit_time) VALUES(?, ?, ?)",
        [
            (10, 1, to_chromium(now - timedelta(hours=2))),
            (11, 2, to_chromium(now - timedelta(hours=1))),
        ],
    )
    connection.commit()
    connection.close()

    state: dict[str, int] = {}

    def save_state(value):
        state.clear()
        state.update(value)

    profile = ChromiumHistoryProfile("Chrome", "Default", history)
    collector = BrowserHistoryCollector(
        state_loader=lambda: state,
        state_saver=save_state,
        profile_provider=lambda: [profile],
        minimum_interval_seconds=0,
    )

    initial = collector.poll(now=100, now_utc=now)

    assert len(initial) == 1
    assert initial[0].facts["site_domain"] == "example.com"
    assert initial[0].facts["historical_backfill"] is True
    assert "private/path" not in str(initial[0].facts)
    assert collector.poll(now=101, now_utc=now) == []

    connection = sqlite3.connect(history)
    connection.execute(
        "INSERT INTO urls(id, url, title, visit_count) VALUES(?, ?, ?, ?)",
        (3, "https://status.example.net/incidents/42", "Service recovered", 1),
    )
    connection.execute(
        "INSERT INTO visits(id, url, visit_time) VALUES(?, ?, ?)",
        (12, 3, to_chromium(now + timedelta(minutes=1))),
    )
    connection.commit()
    connection.close()

    incremental = collector.poll(now=102, now_utc=now + timedelta(minutes=1))

    assert len(incremental) == 1
    assert incremental[0].facts["site_domain"] == "status.example.net"
    assert incremental[0].facts["historical_backfill"] is False


def test_remote_backend_receives_redacted_slice():
    event = {
        "local_id": "event-1",
        "source": "windows.clipboard",
        "type": "ui.visible_text",
        "occurred_at": "2026-08-02T10:00:00+00:00",
        "facts": {
            "visible_text": "owner@example.com https://example.com token ABCDEFGHIJKLMNOPQRSTUV",
            "full_path": r"C:\\Users\\Owner\\secret.txt",
        },
    }
    settings = AppSettings(backend_url="https://mouchen.example.com")

    outbound = prepare_outbound(event, settings)

    assert "owner@example.com" not in str(outbound)
    assert "https://" not in str(outbound["facts"])
    assert "full_path" not in outbound["facts"]
    assert outbound["consent_scope"] == "alpha.minimized_context"
    assert redact_text("owner@example.com") == "[email]"


def test_visible_text_collector_emits_contract_and_deduplicates():
    window = ForegroundWindow("wechat.exe", "微信", 777)
    collector = VisibleTextCollector(
        foreground_reader=lambda: window,
        text_reader=lambda: ["张三", "项目月底必须完成", "项目月底必须完成"],
        minimum_interval_seconds=1,
        duplicate_seconds=600,
    )

    signals = collector.poll(now=100)

    assert len(signals) == 1
    facts = signals[0].facts
    assert signals[0].source == "windows.visible_text"
    assert facts["content_kind"] == "chat"
    assert facts["speaker"] == "unknown"
    assert facts["visible_only"] is True
    assert facts["evidence_strength"] == "contextual"
    assert len(facts["session_key"]) == 32
    assert "张三" not in facts["session_key"]
    assert len(facts["content_hash"]) == 64
    assert collector.poll(now=102) == []


def test_visible_text_collector_filters_secrets_password_numbers_and_excluded_apps():
    window = ForegroundWindow("code.exe", "Editor", 778)
    token = "mch_at_" + "0123456789abcdef" * 2 + "." + "A" * 43
    collector = VisibleTextCollector(
        foreground_reader=lambda: window,
        text_reader=lambda: [
            "password=do-not-send",
            "card 4111 1111 1111 1111",
            f"登录令牌 {token}",
            "验证码：654321",
        ],
        minimum_interval_seconds=1,
    )

    text = collector.poll(now=100)[0].facts["visible_text"]

    assert "do-not-send" not in text
    assert "4111" not in text
    assert token not in text
    assert "654321" not in text
    assert "[secret]" in text
    assert "[payment-number]" in text
    assert collector.poll(excluded_apps=["code.exe"], now=102) == []


def test_remote_full_context_uses_authorized_scope_but_still_removes_secrets():
    event = {
        "local_id": "event-full",
        "source": "windows.visible_text",
        "type": "ui.visible_text",
        "occurred_at": "2026-08-12T08:00:00Z",
        "facts": {
            "visible_text": "owner@example.com password=hunter2 account discussion",
        },
    }
    settings = AppSettings(
        backend_url="https://mouchen.example.com",
        allow_remote_full_context=True,
    )

    outbound = prepare_outbound(event, settings)

    assert outbound["consent_scope"] == "alpha.raw_cloud"
    assert "owner@example.com" in outbound["facts"]["visible_text"]
    assert "hunter2" not in outbound["facts"]["visible_text"]
