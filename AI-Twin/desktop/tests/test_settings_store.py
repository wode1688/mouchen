from __future__ import annotations

import base64
import json
import time

import pytest

from mouchen_desktop.security import IdentityProtector
from mouchen_desktop.settings import AppSettings, SettingsRepository
from mouchen_desktop.store import MAX_EVENT_ATTEMPTS, EventStore


def test_private_owner_defaults_enable_active_understanding():
    settings = AppSettings()

    assert settings.active_window_enabled is True
    assert settings.visible_text_enabled is True
    assert settings.browser_history_enabled is True
    assert settings.clipboard_enabled is True
    assert settings.proactive_cloud_enabled is True
    assert settings.proactive_review_minutes == 10


def test_new_commercial_account_is_fail_closed_until_explicit_consent():
    settings = AppSettings(backend_url="https://mouchen.example.com")
    settings.apply_session(
        "session-token",
        {"user_id": "commercial-user", "username": "alice"},
    )

    assert settings.has_current_collection_consent() is False
    assert settings.collection_permitted() is False
    assert settings.cloud_analysis_permitted() is False

    assert settings.restore_current_collection_consent() is False
    assert settings.collection_enabled is False
    assert settings.visible_text_enabled is False
    assert settings.browser_history_enabled is False
    assert settings.clipboard_enabled is False
    assert settings.file_content_enabled is False
    assert settings.proactive_cloud_enabled is False


def test_collection_consent_is_restored_per_server_account_without_inheritance():
    settings = AppSettings(backend_url="https://mouchen.example.com")
    settings.apply_session("alice-token", {"user_id": "alice-id", "username": "alice"})
    settings.collection_enabled = True
    settings.active_window_enabled = True
    settings.visible_text_enabled = True
    settings.browser_history_enabled = False
    settings.clipboard_enabled = True
    settings.file_watch_enabled = True
    settings.file_content_enabled = True
    settings.proactive_cloud_enabled = True
    settings.watched_folders = [r"C:\Alice\Notes"]
    settings.save_current_collection_consent()

    settings.apply_session("bob-token", {"user_id": "bob-id", "username": "bob"})
    assert settings.restore_current_collection_consent() is False
    assert settings.visible_text_enabled is False
    assert settings.clipboard_enabled is False
    assert settings.watched_folders == []
    settings.clipboard_enabled = True
    settings.collection_enabled = True
    settings.save_current_collection_consent()

    settings.apply_session("alice-new-token", {"user_id": "alice-id", "username": "alice"})
    assert settings.restore_current_collection_consent() is True
    assert settings.visible_text_enabled is True
    assert settings.clipboard_enabled is True
    assert settings.file_content_enabled is True
    assert settings.watched_folders == [r"C:\Alice\Notes"]


def test_legacy_private_owner_keeps_existing_collection_configuration():
    settings = AppSettings(
        auth_mode="legacy",
        user_id="legacy-owner",
        visible_text_enabled=True,
        browser_history_enabled=True,
        clipboard_enabled=True,
        proactive_cloud_enabled=True,
    )

    assert settings.has_current_collection_consent() is True
    assert settings.collection_permitted() is True
    assert settings.cloud_analysis_permitted() is True


def test_settings_round_trip_protects_token(tmp_path):
    path = tmp_path / "settings.json"
    repository = SettingsRepository(path, IdentityProtector())
    settings = AppSettings(
        backend_url="http://127.0.0.1:8787/",
        user_id="owner",
        bearer_token="secret-token",
        auth_mode="legacy",
        browser_history_enabled=True,
        clipboard_enabled=True,
        start_with_windows=True,
        watched_folders=[str(tmp_path)],
    )

    repository.save(settings)
    loaded = repository.load()

    assert loaded.backend_url == "http://127.0.0.1:8787"
    assert loaded.user_id == "owner"
    assert loaded.bearer_token == "secret-token"
    assert loaded.browser_history_enabled is True
    assert loaded.clipboard_enabled is True
    assert loaded.start_with_windows is True
    assert "secret-token" not in path.read_text(encoding="utf-8")


def test_account_consent_round_trip_remains_bound_to_authenticated_user(tmp_path):
    path = tmp_path / "settings.json"
    repository = SettingsRepository(path, IdentityProtector())
    settings = AppSettings(backend_url="https://mouchen.example.com")
    settings.apply_session("alice-token", {"user_id": "alice-id", "username": "alice"})
    settings.disable_unconsented_collection()
    settings.visible_text_enabled = True
    settings.active_window_enabled = True
    settings.collection_enabled = True
    settings.proactive_cloud_enabled = True
    settings.save_current_collection_consent()
    repository.save(settings)

    loaded = repository.load()

    assert loaded.has_current_collection_consent() is True
    assert loaded.current_consent()["visible_text_enabled"] is True
    loaded.apply_session("bob-token", {"user_id": "bob-id", "username": "bob"})
    assert loaded.has_current_collection_consent() is False


def test_remote_plain_http_backend_is_rejected_but_loopback_remains_available():
    with pytest.raises(ValueError, match="https"):
        AppSettings(backend_url="http://example.com:8788").validate()

    settings = AppSettings(backend_url="http://127.0.0.1:8788")
    settings.validate()
    assert settings.backend_url == "http://127.0.0.1:8788"

    with pytest.raises(ValueError, match="不能包含"):
        AppSettings(backend_url="https://user:pass@example.com/#fragment").validate()


def test_session_identity_round_trip_is_server_derived_and_token_protected(tmp_path):
    path = tmp_path / "settings.json"
    repository = SettingsRepository(path, IdentityProtector())
    settings = AppSettings()
    settings.apply_session(
        "session-secret",
        {"user_id": "server-user-42", "username": "alice"},
    )

    repository.save(settings)
    loaded = repository.load()

    assert loaded.auth_mode == "session"
    assert loaded.session_user_id == "server-user-42"
    assert loaded.session_username == "alice"
    assert loaded.bearer_token == "session-secret"
    assert loaded.session_origin == "http://127.0.0.1:8787"
    assert "session-secret" not in path.read_text(encoding="utf-8")


def test_pre_origin_session_file_requires_reauthentication_instead_of_rebinding_token(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "backend_url": "https://mouchen.example.com/service",
                "auth_mode": "session",
                "session_user_id": "server-user-42",
                "session_username": "alice",
                "bearer_token_protected": base64.b64encode(b"session-secret").decode("ascii"),
            }
        ),
        encoding="utf-8",
    )

    loaded = SettingsRepository(path, IdentityProtector()).load()

    assert loaded.session_origin == ""
    assert loaded.has_session_credentials() is False
    assert loaded.session_user_id == "server-user-42"


def test_expired_session_can_preserve_identity_until_same_account_reauthenticates():
    settings = AppSettings()
    settings.apply_session(
        "expired-token",
        {"user_id": "server-user-42", "username": "alice"},
    )

    settings.clear_session(preserve_identity=True)

    assert settings.bearer_token == ""
    assert settings.has_session_credentials() is False
    assert settings.session_user_id == "server-user-42"
    assert settings.session_username == "alice"


def test_legacy_settings_without_auth_mode_remain_usable(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "backend_url": "https://legacy.example.com",
                "user_id": "legacy-owner",
                "bearer_token_protected": base64.b64encode(b"legacy-token").decode("ascii"),
            }
        ),
        encoding="utf-8",
    )

    loaded = SettingsRepository(path, IdentityProtector()).load()

    assert loaded.auth_mode == "legacy"
    assert loaded.user_id == "legacy-owner"
    assert loaded.bearer_token == "legacy-token"


def test_logout_cache_clear_preserves_only_install_device_identity(tmp_path):
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    device_id = store.get_or_create_device_id()
    store.enqueue(
        {
            "local_id": "private-event",
            "source": "windows.visible_text",
            "type": "ui.visible_text",
            "occurred_at": "2026-08-12T08:00:00Z",
            "facts": {"visible_text": "private"},
        }
    )
    store.save_advice({"id": "a1", "status": "active"})
    store.save_state("browser_history", {"cursor": 3})

    store.clear_account_data()

    assert store.recent_events() == []
    assert store.list_advice() == []
    assert store.load_state("browser_history") == {}
    assert store.get_or_create_device_id() == device_id
    store.close()


def test_event_store_keeps_retry_queue_and_deduplicates_advice(tmp_path):
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    event = {
        "local_id": "event-1",
        "source": "windows.manual",
        "type": "ui.visible_text",
        "occurred_at": "2026-08-02T10:00:00+00:00",
        "facts": {"visible_text": "deployment failed"},
    }
    store.enqueue(event)

    assert store.pending() == [event]
    assert store.stats()["pending"] == 1
    store.mark_failed("event-1", "offline")
    assert store.recent_events()[0]["last_error"] == "offline"
    store.mark_synced("event-1")
    assert store.stats()["pending"] == 0

    advice = {
        "id": "advice-1",
        "created_at": "2026-08-02T10:01:00+00:00",
        "status": "active",
        "action": "rollback",
    }
    assert store.save_advice(advice) is True
    assert store.save_advice(advice) is False
    store.update_advice_status("advice-1", "adopted")
    assert store.list_advice()[0]["status"] == "adopted"

    backend_update = {
        **advice,
        "status": "withdrawn",
        "action": "use the replacement plan",
    }
    assert store.save_advice(backend_update) is True
    assert store.save_advice(backend_update) is False
    saved_advice = store.list_advice()[0]
    assert saved_advice["status"] == "withdrawn"
    assert saved_advice["action"] == "use the replacement plan"

    store.save_state("browser_history", {"profile": 123})
    assert store.load_state("browser_history") == {"profile": 123}
    store.close()


def test_recent_events_uses_created_index_at_real_owner_scale(tmp_path):
    """The desktop refresh must not sort the owner's whole encrypted history."""
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    filler = "x" * 4_000

    def rows():
        for index in range(32_230):
            event = {
                "local_id": f"event-{index:05d}",
                "source": "windows.visible_text",
                "type": "ui.visible_text",
                "occurred_at": f"2026-08-14T00:{index // 60 % 60:02d}:{index % 60:02d}Z",
                "facts": {"visible_text": filler},
            }
            yield (
                event["local_id"],
                f"2026-08-14T{index // 3_600:02d}:{index // 60 % 60:02d}:{index % 60:02d}+00:00",
                store._encode(event),
            )

    with store._lock, store._connection:
        store._connection.executemany(
            """INSERT INTO events(id, created_at, payload, synced, attempts, last_error)
               VALUES(?, ?, ?, 1, 0, NULL)""",
            rows(),
        )

    plan = [
        str(row[3])
        for row in store._connection.execute(
            """EXPLAIN QUERY PLAN
               SELECT synced, attempts, last_error, payload
               FROM events ORDER BY created_at DESC LIMIT 120"""
        )
    ]
    started = time.perf_counter()
    recent = store.recent_events(limit=120)
    elapsed = time.perf_counter() - started

    assert any("idx_desktop_events_created" in detail for detail in plan)
    assert not any("TEMP B-TREE" in detail for detail in plan)
    assert len(recent) == 120
    assert recent[0]["local_id"] == "event-32229"
    assert elapsed < 0.5
    store.close()


def test_event_store_quarantines_repeated_failure_without_deleting_it(tmp_path):
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    event = {
        "local_id": "poison-event",
        "source": "windows.manual",
        "type": "ui.visible_text",
        "occurred_at": "2026-08-03T10:00:00+00:00",
        "facts": {"visible_text": "model request"},
    }
    store.enqueue(event)

    for _ in range(MAX_EVENT_ATTEMPTS):
        store.mark_failed("poison-event", "timed out")

    assert store.pending() == []
    assert store.stats()["pending"] == 0
    assert store.stats()["quarantined"] == 1
    saved = store.recent_events()[0]
    assert saved["attempts"] == MAX_EVENT_ATTEMPTS
    assert saved["quarantined"] is True
    assert saved["last_error"] == "timed out"
    store.close()


def test_unchanged_advice_is_not_reported_as_updated_with_rotating_ciphertext(tmp_path):
    class RotatingProtector:
        def __init__(self):
            self.counter = 0

        def protect(self, value: bytes) -> bytes:
            self.counter = (self.counter + 1) % 256
            return bytes([self.counter]) + value

        def unprotect(self, value: bytes) -> bytes:
            return value[1:]

    store = EventStore(tmp_path / "desktop.db", RotatingProtector())
    advice = {
        "id": "stable-advice",
        "created_at": "2026-08-05T08:00:00+00:00",
        "status": "active",
        "action": "review the result",
    }

    assert store.save_advice(advice) is True
    assert store.save_advice(dict(advice)) is False
    assert store.list_advice() == [advice]
    store.close()


def test_device_identity_is_stable_across_store_restarts(tmp_path):
    path = tmp_path / "desktop.db"
    first_store = EventStore(path, IdentityProtector())
    first = first_store.get_or_create_device_id("windows")
    first_store.close()

    second_store = EventStore(path, IdentityProtector())
    second = second_store.get_or_create_device_id("windows")

    assert first == second
    assert first.startswith("windows-")
    second_store.close()
