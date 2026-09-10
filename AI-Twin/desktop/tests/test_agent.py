from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from mouchen_desktop.agent import DesktopAgent
from mouchen_desktop.backend import BackendError, BackendHTTPError
from mouchen_desktop.collectors import Signal
from mouchen_desktop.proactive import ActivityReviewBuffer
from mouchen_desktop.security import IdentityProtector
from mouchen_desktop.settings import AppSettings, SettingsRepository
from mouchen_desktop.store import EventStore


def test_manual_problem_enters_queue_and_requests_cloud_when_enabled(tmp_path):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings_repository.save(
        AppSettings(
            auth_mode="legacy",
            user_id="private-owner",
            proactive_cloud_enabled=True,
        )
    )
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    observed = []
    agent = DesktopAgent(settings_repository, store, on_event=observed.append)

    event_id = agent.submit_problem("Production deployment failed with timeout", "work")
    pending = store.pending()

    assert pending[0]["local_id"] == event_id
    assert pending[0]["facts"]["domain"] == "work"
    assert DesktopAgent._should_use_cloud(pending[0], agent.settings) is True
    assert observed[0]["local_id"] == event_id
    store.close()


def test_authentication_persists_identity_only_from_confirmed_session(tmp_path, monkeypatch):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings_repository.save(AppSettings())
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    agent = DesktopAgent(settings_repository, store)

    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.login",
        lambda self, username, password, device_id, device_name=None: {
            "access_token": "session-token",
            "session": {"user_id": "server-user", "username": "alice"},
        },
    )
    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.session",
        lambda self: {"user_id": "server-user", "username": "alice"},
    )

    confirmed = agent.authenticate("alice", "correct horse battery staple")

    assert confirmed["user_id"] == "server-user"
    assert agent.settings.session_user_id == "server-user"
    assert agent.settings.session_username == "alice"
    assert agent.settings.bearer_token == "session-token"
    assert agent.settings.has_current_collection_consent() is False
    assert agent.settings.collection_enabled is False
    assert agent.settings.visible_text_enabled is False
    assert agent.settings.browser_history_enabled is False
    assert agent.settings.clipboard_enabled is False
    assert agent.settings.proactive_cloud_enabled is False
    assert settings_repository.load().session_user_id == "server-user"
    store.close()


def test_explicit_collection_consent_enables_only_selected_sources(tmp_path):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings = AppSettings(backend_url="https://mouchen.example.com")
    settings.apply_session("session-token", {"user_id": "user-1", "username": "alice"})
    settings.restore_current_collection_consent()
    settings_repository.save(settings)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    store.enqueue(
        {
            "local_id": "pre-consent-event",
            "source": "windows.visible_text",
            "type": "ui.visible_text",
            "occurred_at": "2026-08-12T08:00:00Z",
            "facts": {"visible_text": "captured by an older build"},
        }
    )
    agent = DesktopAgent(settings_repository, store)

    agent.apply_collection_consent(
        visible_interface=True,
        browser_activity=False,
        clipboard_text=True,
        file_content=False,
        cloud_analysis=True,
    )

    saved = settings_repository.load()
    assert saved.has_current_collection_consent() is True
    assert saved.collection_enabled is True
    assert saved.active_window_enabled is True
    assert saved.visible_text_enabled is True
    assert saved.browser_history_enabled is False
    assert saved.clipboard_enabled is True
    assert saved.file_watch_enabled is False
    assert saved.file_content_enabled is False
    assert saved.proactive_cloud_enabled is True
    assert saved.allow_remote_full_context is False
    assert store.recent_events() == []
    store.close()


def test_unconsented_commercial_account_neither_syncs_nor_spends_retry(tmp_path):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings = AppSettings(backend_url="https://mouchen.example.com")
    settings.apply_session("session-token", {"user_id": "user-1", "username": "alice"})
    settings_repository.save(settings)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    store.enqueue(
        {
            "local_id": "event-before-consent",
            "source": "windows.visible_text",
            "type": "ui.visible_text",
            "occurred_at": "2026-08-12T08:00:00Z",
            "facts": {"visible_text": "must not leave this device"},
        }
    )
    agent = DesktopAgent(settings_repository, store)

    class MustNotUpload:
        def send_event(self, event, proactive_cloud_approved=False):
            raise AssertionError("unconsented event was uploaded")

    agent._sync_events_once(MustNotUpload(), agent.settings)

    saved = store.recent_events()[0]
    assert saved["synced"] is False
    assert saved["attempts"] == 0
    assert DesktopAgent._should_use_cloud(saved, agent.settings) is False
    store.close()


def test_explicit_all_off_choice_still_never_uploads(tmp_path):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings = AppSettings(backend_url="https://mouchen.example.com")
    settings.apply_session("session-token", {"user_id": "user-1", "username": "alice"})
    settings_repository.save(settings)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    agent = DesktopAgent(settings_repository, store)
    agent.apply_collection_consent(
        visible_interface=False,
        browser_activity=False,
        clipboard_text=False,
        file_content=False,
        cloud_analysis=False,
    )
    store.enqueue(
        {
            "local_id": "must-stay-local",
            "source": "windows.manual",
            "type": "ui.visible_text",
            "occurred_at": "2026-08-12T08:00:00Z",
            "facts": {"visible_text": "no uploads are enabled"},
        }
    )

    class MustNotUpload:
        def send_event(self, event, proactive_cloud_approved=False):
            raise AssertionError("all-off account uploaded an event")

    agent._sync_events_once(MustNotUpload(), agent.settings)

    assert store.recent_events()[0]["synced"] is False
    assert store.recent_events()[0]["attempts"] == 0
    store.close()


def test_first_commercial_login_clears_legacy_cache_for_a_different_tenant(
    tmp_path, monkeypatch
):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings_repository.save(
        AppSettings(
            backend_url="https://mouchen.example.com",
            auth_mode="legacy",
            user_id="legacy-owner",
            bearer_token="legacy-token",
        )
    )
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    store.enqueue(
        {
            "local_id": "legacy-private-event",
            "source": "windows.visible_text",
            "type": "ui.visible_text",
            "occurred_at": "2026-08-12T08:00:00Z",
            "facts": {"visible_text": "legacy owner's private content"},
        }
    )
    agent = DesktopAgent(settings_repository, store)
    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.login",
        lambda self, username, password, device_id, device_name=None: {
            "access_token": "new-token",
            "session": {"user_id": "different-user", "username": "alice"},
        },
    )
    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.session",
        lambda self: {"user_id": "different-user", "username": "alice"},
    )

    agent.authenticate("alice", "correct horse battery staple")

    assert store.recent_events() == []
    assert agent.settings.session_user_id == "different-user"
    store.close()


def test_first_commercial_login_preserves_legacy_cache_for_same_tenant(
    tmp_path, monkeypatch
):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings_repository.save(
        AppSettings(
            backend_url="https://mouchen.example.com",
            auth_mode="legacy",
            user_id="legacy-owner",
            bearer_token="legacy-token",
        )
    )
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    store.enqueue(
        {
            "local_id": "same-owner-event",
            "source": "windows.visible_text",
            "type": "ui.visible_text",
            "occurred_at": "2026-08-12T08:00:00Z",
            "facts": {"visible_text": "same owner's content"},
        }
    )
    agent = DesktopAgent(settings_repository, store)
    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.login",
        lambda self, username, password, device_id, device_name=None: {
            "access_token": "owner-token",
            "session": {"user_id": "legacy-owner", "username": "owner"},
        },
    )
    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.session",
        lambda self: {"user_id": "legacy-owner", "username": "owner"},
    )

    agent.authenticate("owner", "correct horse battery staple")

    assert [event["local_id"] for event in store.recent_events()] == ["same-owner-event"]
    assert agent.settings.has_current_collection_consent() is True
    assert agent.settings.collection_enabled is True
    assert agent.settings.visible_text_enabled is True
    assert agent.settings.proactive_cloud_enabled is True
    store.close()


def test_legacy_owner_claim_rejects_other_tenant_without_clearing_cache(
    tmp_path, monkeypatch
):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings_repository.save(
        AppSettings(
            backend_url="https://mouchen.example.com",
            auth_mode="legacy",
            user_id="legacy-owner",
            bearer_token="legacy-token",
        )
    )
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    store.enqueue(
        {
            "local_id": "legacy-private-event",
            "source": "windows.visible_text",
            "type": "ui.visible_text",
            "occurred_at": "2026-08-12T08:00:00Z",
            "facts": {"visible_text": "legacy owner's private content"},
        }
    )
    agent = DesktopAgent(settings_repository, store)
    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.login",
        lambda self, username, password, device_id, device_name=None: {
            "access_token": "wrong-account-token",
            "session": {"user_id": "different-user", "username": "alice"},
        },
    )
    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.session",
        lambda self: {"user_id": "different-user", "username": "alice"},
    )
    revoked = []
    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.logout",
        lambda self: revoked.append(self.settings.session_user_id) or {},
    )

    with pytest.raises(BackendError, match="不属于这台电脑的原有数据"):
        agent.authenticate(
            "alice",
            "correct horse battery staple",
            expected_user_id="legacy-owner",
        )

    assert agent.settings.auth_mode == "legacy"
    assert agent.settings.user_id == "legacy-owner"
    assert agent.settings.bearer_token == "legacy-token"
    assert [event["local_id"] for event in store.recent_events()] == [
        "legacy-private-event"
    ]
    assert revoked == ["different-user"]
    store.close()


def test_activity_review_builds_factual_cloud_candidate():
    review = ActivityReviewBuffer()
    review.add(
        Signal(
            source="windows.foreground",
            type="app.foreground_session",
            facts={
                "package": "Code.exe",
                "window_title": "mouchen - Visual Studio Code",
                "duration_ms": 420_000,
            },
        ).to_local_event()
    )
    review.add(
        Signal(
            source="windows.browser_history",
            type="ui.visible_text",
            facts={"page_title": "GitHub Actions", "site_domain": "github.com"},
        ).to_local_event()
    )
    review.add(
        Signal(
            source="windows.clipboard",
            type="ui.visible_text",
            facts={"visible_text": "release checklist"},
        ).to_local_event()
    )

    signal = review.build_signal()

    assert signal is not None
    assert signal.facts["analysis_requested"] is True
    assert signal.facts["context"] == "proactive_activity_review"
    assert "mouchen - Visual Studio Code" in signal.facts["visible_text"]
    assert "GitHub Actions" in signal.facts["visible_text"]
    assert "release checklist" in signal.facts["visible_text"]
    assert len(review) == 0


def test_activity_review_ignores_historical_browser_backfill():
    review = ActivityReviewBuffer()
    review.add(
        Signal(
            source="windows.browser_history",
            type="ui.visible_text",
            facts={
                "page_title": "Old page",
                "site_domain": "example.com",
                "historical_backfill": True,
            },
        ).to_local_event()
    )

    assert review.build_signal(minimum_events=1) is None


def test_activity_review_includes_visible_content_with_provenance():
    review = ActivityReviewBuffer()
    review.add(
        Signal(
            source="windows.visible_text",
            type="ui.visible_text",
            facts={
                "visible_text": "counterparty says the deadline changed",
                "content_kind": "chat",
                "speaker": "unknown",
                "visible_only": True,
                "evidence_strength": "contextual",
                "session_key": "session-hash-only",
            },
            sensitivity="restricted",
        ).to_local_event()
    )

    signal = review.build_signal(minimum_events=1)

    assert signal is not None
    assert "VISIBLE | chat | unknown | session-hash-only" in signal.facts["visible_text"]
    assert "deadline changed" in signal.facts["visible_text"]


def test_failed_event_does_not_block_later_events(tmp_path):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings_repository.save(AppSettings(auth_mode="legacy", user_id="private-owner"))
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    agent = DesktopAgent(settings_repository, store)
    for event_id in ("bad-event", "good-event"):
        store.enqueue(
            {
                "local_id": event_id,
                "source": "windows.manual",
                "type": "ui.visible_text",
                "occurred_at": "2026-08-03T10:00:00+00:00",
                "facts": {"visible_text": event_id},
            }
        )

    class FakeClient:
        def send_event(self, event, proactive_cloud_approved=False):
            if event["facts"]["visible_text"] == "bad-event":
                raise BackendError("timed out")
            return {"evaluation": None}

    agent._sync_events_once(FakeClient(), agent.settings)

    by_id = {item["local_id"]: item for item in store.recent_events()}
    assert by_id["bad-event"]["synced"] is False
    assert by_id["bad-event"]["attempts"] == 1
    assert by_id["good-event"]["synced"] is True
    store.close()


def test_event_storage_quota_stops_batch_without_spending_retries_and_backs_off(
    tmp_path, monkeypatch
):
    clock = [100.0]
    monkeypatch.setattr("mouchen_desktop.agent.time.monotonic", lambda: clock[0])
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings_repository.save(AppSettings(auth_mode="legacy", user_id="private-owner"))
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    for event_id in ("quota-event-1", "quota-event-2"):
        store.enqueue(
            {
                "local_id": event_id,
                "source": "windows.manual",
                "type": "ui.visible_text",
                "occurred_at": "2026-08-14T08:00:00Z",
                "facts": {"visible_text": event_id},
            }
        )
    statuses = []
    recovery_send_statuses = []
    agent = DesktopAgent(settings_repository, store, on_status=statuses.append)

    class QuotaClient:
        storage_full = True

        def __init__(self):
            self.sent = []

        def send_event(self, event, proactive_cloud_approved=False):
            self.sent.append(event["facts"]["visible_text"])
            if self.storage_full:
                raise BackendHTTPError(507, "event storage quota exceeded")
            recovery_send_statuses.append(dict(statuses[-1]))
            return {"evaluation": None}

    client = QuotaClient()
    agent._sync_events_once(client, agent.settings)

    by_id = {item["local_id"]: item for item in store.recent_events()}
    assert client.sent == ["quota-event-1"]
    assert all(item["attempts"] == 0 for item in by_id.values())
    assert all(item["synced"] is False for item in by_id.values())
    assert statuses[-1]["event_sync_deferred_for_quota"] is True
    assert statuses[-1]["event_sync_retry_seconds"] == 60
    assert "event storage quota exceeded" in statuses[-1]["sync_error"]

    clock[0] += 30
    agent._sync_events_once(client, agent.settings)
    assert client.sent == ["quota-event-1"]
    assert statuses[-1]["event_sync_retry_seconds"] == 30

    clock[0] += 31
    client.storage_full = False
    agent._sync_events_once(client, agent.settings)
    by_id = {item["local_id"]: item for item in store.recent_events()}
    assert client.sent == ["quota-event-1", "quota-event-1", "quota-event-2"]
    assert recovery_send_statuses[0]["event_sync_deferred_for_quota"] is False
    assert recovery_send_statuses[0]["event_sync_retry_seconds"] == 0
    assert recovery_send_statuses[0]["sync_error"] == ""
    assert all(item["attempts"] == 0 for item in by_id.values())
    assert all(item["synced"] is True for item in by_id.values())
    assert statuses[-1]["event_sync_deferred_for_quota"] is False
    assert statuses[-1]["event_sync_retry_seconds"] == 0
    assert statuses[-1]["sync_error"] == ""
    store.close()


def test_account_switch_clears_quota_backoff_and_old_bearer_identity(tmp_path):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    alice = AppSettings(backend_url="https://mouchen.example.com")
    alice.apply_session("alice-token", {"user_id": "alice", "username": "alice"})
    alice.save_current_collection_consent()
    settings_repository.save(alice)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    store.enqueue(
        {
            "local_id": "alice-event",
            "source": "windows.manual",
            "type": "ui.visible_text",
            "occurred_at": "2026-08-14T08:00:00Z",
            "facts": {"visible_text": "alice-event"},
        }
    )
    statuses = []
    agent = DesktopAgent(settings_repository, store, on_status=statuses.append)

    class QuotaClient:
        def send_event(self, event, proactive_cloud_approved=False):
            raise BackendHTTPError(507, "event storage quota exceeded")

    agent._sync_events_once(QuotaClient(), agent.settings)
    assert agent._event_storage_quota_identity is not None
    assert agent._event_storage_quota_identity[-1] == "alice-token"
    assert statuses[-1]["event_sync_deferred_for_quota"] is True

    bob = agent.settings
    bob.apply_session("bob-token", {"user_id": "bob", "username": "bob"})
    bob.save_current_collection_consent()
    agent.update_settings(bob)

    assert agent._event_storage_quota_identity is None
    assert agent._event_storage_quota_retry_at == 0.0
    assert agent._event_storage_quota_error == ""
    assert statuses[-1]["event_sync_deferred_for_quota"] is False
    assert statuses[-1]["event_sync_retry_seconds"] == 0
    assert statuses[-1]["sync_error"] == ""
    assert agent._event_storage_quota_backoff(agent.settings) is None

    store.clear_account_data()
    store.enqueue(
        {
            "local_id": "bob-event",
            "source": "windows.manual",
            "type": "ui.visible_text",
            "occurred_at": "2026-08-14T08:01:00Z",
            "facts": {"visible_text": "bob-event"},
        }
    )

    class SuccessClient:
        def __init__(self):
            self.sent = []

        def send_event(self, event, proactive_cloud_approved=False):
            self.sent.append(event["facts"]["visible_text"])
            return {"evaluation": None}

    client = SuccessClient()
    agent._sync_events_once(client, agent.settings)
    assert client.sent == ["bob-event"]
    assert store.recent_events()[0]["synced"] is True
    store.close()


def test_other_507_event_error_keeps_normal_failure_handling(tmp_path):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings_repository.save(AppSettings(auth_mode="legacy", user_id="private-owner"))
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    for event_id in ("other-507", "later-event"):
        store.enqueue(
            {
                "local_id": event_id,
                "source": "windows.manual",
                "type": "ui.visible_text",
                "occurred_at": "2026-08-14T08:00:00Z",
                "facts": {"visible_text": event_id},
            }
        )
    statuses = []
    agent = DesktopAgent(settings_repository, store, on_status=statuses.append)

    class Other507Client:
        def __init__(self):
            self.sent = []

        def send_event(self, event, proactive_cloud_approved=False):
            event_id = event["facts"]["visible_text"]
            self.sent.append(event_id)
            if event_id == "other-507":
                raise BackendHTTPError(507, "goal storage quota exceeded")
            return {"evaluation": None}

    client = Other507Client()
    agent._sync_events_once(client, agent.settings)

    by_id = {item["local_id"]: item for item in store.recent_events()}
    assert client.sent == ["other-507", "later-event"]
    assert by_id["other-507"]["attempts"] == 1
    assert by_id["other-507"]["synced"] is False
    assert by_id["later-event"]["synced"] is True
    assert statuses[-1]["event_sync_deferred_for_quota"] is False
    assert "goal storage quota exceeded" in statuses[-1]["sync_error"]
    store.close()


def test_revoked_runtime_session_pauses_collection_without_spending_event_retry(
    tmp_path
):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings = AppSettings(backend_url="https://mouchen.example.com")
    settings.apply_session("revoked-token", {"user_id": "owner", "username": "owner"})
    settings.save_current_collection_consent()
    settings_repository.save(settings)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    store.enqueue(
        {
            "local_id": "pending-private-event",
            "source": "windows.visible_text",
            "type": "ui.visible_text",
            "occurred_at": "2026-08-12T08:00:00Z",
            "facts": {"visible_text": "private"},
        }
    )
    agent = DesktopAgent(settings_repository, store)

    class RevokedClient:
        def send_event(self, event, proactive_cloud_approved=False):
            raise BackendHTTPError(401, "authentication required")

    agent._sync_events_once(RevokedClient(), agent.settings)

    saved = store.recent_events()[0]
    assert saved["attempts"] == 0
    assert saved["synced"] is False
    assert agent.paused is True
    assert agent.settings.bearer_token == ""
    assert agent.settings.session_user_id == "owner"
    store.close()


def test_advice_poll_reconciles_full_backlog_and_emits_each_changed_id(tmp_path):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings_repository.save(AppSettings())
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    notified = []
    agent = DesktopAgent(settings_repository, store, on_advice=notified.append)

    class FakeClient:
        def __init__(self):
            self.items = [
                {"id": "advice-002", "created_at": "2026-08-03T10:01:00Z", "status": "active"},
                {"id": "advice-001", "created_at": "2026-08-03T10:00:00Z", "status": "active"},
            ]
            self.requested_limit = None

        def list_advice(self, limit=100):
            self.requested_limit = limit
            return [dict(item) for item in self.items]

    client = FakeClient()
    assert agent._poll_advice_once(client) == 2
    assert client.requested_limit == 500
    assert len(store.list_advice()) == 2
    assert [item["id"] for item in notified] == ["advice-001", "advice-002"]
    assert all(item["_mouchen_locally_new"] is True for item in notified)
    assert agent._poll_advice_once(client) == 0
    assert [item["id"] for item in notified] == ["advice-001", "advice-002"]

    client.items[0].update(
        display_locale="en-US",
        display_translation_status="ready",
        display={
            "action": "Review the failure",
            "first_step": "Open the failure details",
            "alternative": None,
            "prediction_outcome": "The release remains blocked",
            "adopted_expected_result": None,
        },
    )
    assert agent._poll_advice_once(client) == 1
    assert notified[-1]["id"] == "advice-002"
    assert notified[-1]["_mouchen_locally_new"] is False

    client.items[0]["status"] = "withdrawn"
    assert agent._poll_advice_once(client) == 1
    assert store.list_advice()[0]["status"] == "withdrawn"
    assert notified[-1]["status"] == "withdrawn"
    assert notified[-1]["_mouchen_locally_new"] is False
    store.close()


def test_stale_activity_review_syncs_without_triggering_model(tmp_path):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings_repository.save(
        AppSettings(
            auth_mode="legacy",
            user_id="private-owner",
            proactive_cloud_enabled=True,
        )
    )
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    agent = DesktopAgent(settings_repository, store)
    event = {
        "local_id": "stale-review",
        "source": "windows.proactive",
        "type": "ui.visible_text",
        "occurred_at": "2026-08-03T10:00:00+00:00",
        "facts": {
            "context": "proactive_activity_review",
            "analysis_requested": True,
            "visible_text": "old activity summary",
        },
    }

    assert DesktopAgent._should_use_cloud(
        event,
        agent.settings,
        now=datetime(2026, 8, 3, 10, 31, tzinfo=timezone.utc),
    ) is False
    assert DesktopAgent._should_use_cloud(
        event,
        agent.settings,
        now=datetime(2026, 8, 3, 10, 29, tzinfo=timezone.utc),
    ) is True
    store.close()


def test_attention_claim_is_separate_from_advice_pull_and_emitted_once(tmp_path):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings_repository.save(AppSettings())
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    displayed = []
    attentions = []
    agent = DesktopAgent(
        settings_repository,
        store,
        on_advice=displayed.append,
        on_attention=attentions.append,
    )
    current = {
        "id": "advice-claim-1",
        "created_at": "2026-08-09T08:00:00Z",
        "status": "active",
        "action": "review the failed job",
    }

    class FakeClient:
        def claim_advice_attention(self, device_id, platform="windows"):
            assert device_id == agent.device_id
            assert platform == "windows"
            return {
                "status": "claimed",
                "claim_token": "token-1",
                "lease_expires_at": "2026-08-09T08:05:00Z",
                "delivery_number": 1,
                "advice": dict(current),
            }

    result = agent._claim_attention_once(FakeClient())

    assert result["claim_token"] == "token-1"
    assert [item["id"] for item in displayed] == ["advice-claim-1"]
    assert attentions == [result]
    assert store.list_advice()[0]["id"] == "advice-claim-1"
    store.close()


def test_pending_delivery_receipt_is_retried_before_a_new_claim(tmp_path):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings_repository.save(AppSettings())
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    agent = DesktopAgent(settings_repository, store)
    store.save_state(
        "advice_notification:advice-1",
        {
            "advice_id": "advice-1",
            "server_report_action": "complete",
            "server_claim_token": "claim-token",
            "server_report_status": "error",
        },
    )

    class FakeClient:
        def complete_advice_attention(self, advice_id, device_id, claim_token):
            assert (advice_id, device_id, claim_token) == (
                "advice-1",
                agent.device_id,
                "claim-token",
            )
            return {"status": "delivered"}

    assert agent._retry_attention_reports_once(FakeClient()) is True
    saved = store.load_state("advice_notification:advice-1")
    assert saved["server_report_status"] == "delivered"
    assert "server_claim_token" not in saved
    assert "server_report_action" not in saved
    store.close()


def test_late_event_response_cannot_write_after_account_change(tmp_path):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings = AppSettings(backend_url="https://mouchen.example.com")
    settings.apply_session("alice-token", {"user_id": "alice", "username": "alice"})
    settings.save_current_collection_consent()
    settings_repository.save(settings)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    store.enqueue(
        {
            "local_id": "alice-private-event",
            "source": "windows.visible_text",
            "type": "ui.visible_text",
            "occurred_at": "2026-08-12T08:00:00Z",
            "facts": {"visible_text": "Alice private content"},
        }
    )
    notified = []
    agent = DesktopAgent(settings_repository, store, on_advice=notified.append)
    entered = threading.Event()
    release = threading.Event()
    old_settings = agent.settings
    old_context = agent._session_context(old_settings)

    class DelayedClient:
        def send_event(self, event, proactive_cloud_approved=False):
            entered.set()
            assert release.wait(2)
            return {
                "evaluation": {
                    "decision": "publish",
                    "advice": {"id": "alice-advice", "status": "active"},
                }
            }

    worker = threading.Thread(
        target=lambda: agent._sync_events_once(
            DelayedClient(),
            old_settings,
            context=old_context,
            stop_event=threading.Event(),
        )
    )
    worker.start()
    assert entered.wait(1)

    bob = agent.settings
    bob.apply_session("bob-token", {"user_id": "bob", "username": "bob"})
    agent.update_settings(bob)
    release.set()
    worker.join(2)

    saved = store.recent_events()[0]
    assert saved["synced"] is False
    assert saved["attempts"] == 0
    assert store.list_advice() == []
    assert notified == []
    store.close()


def test_late_advice_pull_cannot_populate_new_account_cache(tmp_path):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings = AppSettings(backend_url="https://mouchen.example.com")
    settings.apply_session("alice-token", {"user_id": "alice", "username": "alice"})
    settings_repository.save(settings)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    notified = []
    agent = DesktopAgent(settings_repository, store, on_advice=notified.append)
    entered = threading.Event()
    release = threading.Event()
    old_settings = agent.settings
    old_context = agent._session_context(old_settings)

    class DelayedClient:
        def list_advice(self, limit=500):
            entered.set()
            assert release.wait(2)
            return [{"id": "alice-advice", "status": "active"}]

    worker = threading.Thread(
        target=lambda: agent._poll_advice_once(
            DelayedClient(),
            context=old_context,
            stop_event=threading.Event(),
        )
    )
    worker.start()
    assert entered.wait(1)

    bob = agent.settings
    bob.apply_session("bob-token", {"user_id": "bob", "username": "bob"})
    agent.update_settings(bob)
    release.set()
    worker.join(2)

    assert store.list_advice() == []
    assert notified == []
    store.close()


@pytest.mark.parametrize("request_fails", [False, True])
def test_late_manual_locale_put_cannot_overwrite_new_account(
    tmp_path, monkeypatch, request_fails
):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    alice = AppSettings(backend_url="https://mouchen.example.com", locale="zh-CN")
    alice.apply_session("alice-token", {"user_id": "alice", "username": "alice"})
    settings_repository.save(alice)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    agent = DesktopAgent(settings_repository, store)
    entered = threading.Event()
    release = threading.Event()

    def delayed_update(self, locale):
        assert self.settings.bearer_token == "alice-token"
        assert locale == "en-US"
        entered.set()
        assert release.wait(2)
        if request_fails:
            raise BackendError("old account request failed")
        return {"locale": "en-US"}

    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.update_account_locale",
        delayed_update,
    )
    results = []
    errors = []

    def update_locale():
        try:
            results.append(agent.update_account_locale("en-US"))
        except Exception as exc:  # pragma: no cover - assertion records leakage
            errors.append(exc)

    worker = threading.Thread(target=update_locale)
    worker.start()
    assert entered.wait(1)

    bob = agent.settings
    bob.apply_session("bob-token", {"user_id": "bob", "username": "bob"})
    bob.locale = "zh-CN"
    agent.update_settings(bob)
    release.set()
    worker.join(2)

    assert errors == []
    assert results == [{"_discarded": True}]
    assert agent.settings.session_user_id == "bob"
    assert agent.settings.bearer_token == "bob-token"
    assert agent.settings.locale == "zh-CN"
    persisted = settings_repository.load()
    assert persisted.session_user_id == "bob"
    assert persisted.bearer_token == "bob-token"
    assert persisted.locale == "zh-CN"
    store.close()


def test_slow_locale_put_is_serialized_before_latest_selection(tmp_path, monkeypatch):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    alice = AppSettings(backend_url="https://mouchen.example.com", locale="zh-CN")
    alice.apply_session("alice-token", {"user_id": "alice", "username": "alice"})
    settings_repository.save(alice)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    agent = DesktopAgent(settings_repository, store)
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    server_state = {"locale": "zh-CN"}
    completed_requests = []
    request_tokens = []

    def ordered_update(self, locale):
        request_tokens.append(self.settings.bearer_token)
        if locale == "en-US":
            first_entered.set()
            assert release_first.wait(2)
        else:
            assert locale == "zh-CN"
            second_entered.set()
        server_state["locale"] = locale
        completed_requests.append(locale)
        return {"locale": locale}

    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.update_account_locale",
        ordered_update,
    )
    results = {}
    errors = []

    def update_locale(locale):
        try:
            results[locale] = agent.update_account_locale(locale)
        except Exception as exc:  # pragma: no cover - assertion records leakage
            errors.append(exc)

    slow_first = threading.Thread(target=update_locale, args=("en-US",))
    slow_first.start()
    assert first_entered.wait(1)

    fast_latest = threading.Thread(target=update_locale, args=("zh-CN",))
    fast_latest.start()
    assert not second_entered.wait(0.1)

    release_first.set()
    slow_first.join(2)
    fast_latest.join(2)

    assert not slow_first.is_alive()
    assert not fast_latest.is_alive()
    assert errors == []
    assert completed_requests == ["en-US", "zh-CN"]
    assert server_state["locale"] == "zh-CN"
    assert results["en-US"] == {"_discarded": True}
    assert results["zh-CN"]["locale"] == "zh-CN"
    assert results["zh-CN"]["_discarded"] is False
    assert request_tokens == ["alice-token", "alice-token"]
    assert agent.settings.session_user_id == "alice"
    assert agent.settings.bearer_token == "alice-token"
    assert agent.settings.locale == "zh-CN"
    persisted = settings_repository.load()
    assert persisted.session_user_id == "alice"
    assert persisted.bearer_token == "alice-token"
    assert persisted.locale == "zh-CN"
    store.close()


def test_locale_click_sequence_wins_when_ui_workers_enter_agent_in_reverse_order(
    tmp_path, monkeypatch
):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    alice = AppSettings(backend_url="https://mouchen.example.com", locale="zh-CN")
    alice.apply_session("alice-token", {"user_id": "alice", "username": "alice"})
    settings_repository.save(alice)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    agent = DesktopAgent(settings_repository, store)
    session_context = agent.ui_session_context()
    old_worker_waiting = threading.Event()
    allow_old_worker_to_enter = threading.Event()
    server_writes = []
    results = {}

    def update_server(_client, locale):
        server_writes.append(locale)
        return {"locale": locale}

    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.update_account_locale",
        update_server,
    )

    def old_english_click():
        old_worker_waiting.set()
        assert allow_old_worker_to_enter.wait(2)
        results["old"] = agent.update_account_locale(
            "en-US",
            request_id=1,
            expected_session_context=session_context,
        )

    def latest_chinese_click():
        results["latest"] = agent.update_account_locale(
            "zh-CN",
            request_id=2,
            expected_session_context=session_context,
        )

    old_worker = threading.Thread(target=old_english_click)
    old_worker.start()
    assert old_worker_waiting.wait(1)
    latest_worker = threading.Thread(target=latest_chinese_click)
    latest_worker.start()
    latest_worker.join(2)
    assert not latest_worker.is_alive()

    allow_old_worker_to_enter.set()
    old_worker.join(2)

    assert not old_worker.is_alive()
    assert server_writes == ["zh-CN"]
    assert results["latest"]["locale"] == "zh-CN"
    assert results["latest"]["_discarded"] is False
    assert results["old"] == {"_discarded": True}
    assert agent.settings.locale == "zh-CN"
    assert settings_repository.load().locale == "zh-CN"
    store.close()


def test_successful_locale_put_replaces_cached_account_locale_status(
    tmp_path, monkeypatch
):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings = AppSettings(backend_url="https://mouchen.example.com", locale="zh-CN")
    settings.apply_session("alice-token", {"user_id": "alice", "username": "alice"})
    settings_repository.save(settings)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    statuses = []
    agent = DesktopAgent(settings_repository, store, on_status=statuses.append)
    context = agent._session_context()
    agent._publish_status(
        account_locale="zh-CN",
        account_locale_context=agent._ui_session_context(context),
    )

    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.update_account_locale",
        lambda _client, locale: {"locale": locale},
    )

    result = agent.update_account_locale("en-US")
    agent._publish_status(online=True)

    assert result["locale"] == "en-US"
    assert statuses[-1]["account_locale"] == "en-US"
    assert statuses[-1]["account_locale_context"] == agent.ui_session_context()
    store.close()


def test_latest_locale_failure_rolls_back_to_slow_confirmed_put(tmp_path, monkeypatch):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    alice = AppSettings(backend_url="https://mouchen.example.com", locale="zh-CN")
    alice.apply_session("alice-token", {"user_id": "alice", "username": "alice"})
    settings_repository.save(alice)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    statuses = []
    agent = DesktopAgent(settings_repository, store, on_status=statuses.append)
    initial_context = agent._session_context()
    agent._publish_status(
        account_locale="zh-CN",
        account_locale_context=agent._ui_session_context(initial_context),
    )
    first_entered = threading.Event()
    latest_entered = threading.Event()
    release_first = threading.Event()
    server_state = {"locale": "zh-CN"}
    request_tokens = []

    def first_succeeds_latest_fails(self, locale):
        request_tokens.append(self.settings.bearer_token)
        if locale == "en-US":
            first_entered.set()
            assert release_first.wait(2)
            server_state["locale"] = locale
            return {"locale": locale}
        assert locale == "zh-CN"
        latest_entered.set()
        raise BackendError("latest locale update failed")

    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.update_account_locale",
        first_succeeds_latest_fails,
    )
    results = {}
    errors = {}

    def update_locale(locale):
        try:
            results[locale] = agent.update_account_locale(locale)
        except Exception as exc:
            errors[locale] = exc

    slow_first = threading.Thread(target=update_locale, args=("en-US",))
    slow_first.start()
    assert first_entered.wait(1)

    failing_latest = threading.Thread(target=update_locale, args=("zh-CN",))
    failing_latest.start()
    assert not latest_entered.wait(0.1)

    release_first.set()
    slow_first.join(2)
    failing_latest.join(2)

    assert not slow_first.is_alive()
    assert not failing_latest.is_alive()
    assert results == {"en-US": {"_discarded": True}}
    assert isinstance(errors.get("zh-CN"), BackendError)
    assert str(errors["zh-CN"]) == "latest locale update failed"
    assert server_state["locale"] == "en-US"
    assert request_tokens == ["alice-token", "alice-token"]
    assert agent.settings.session_user_id == "alice"
    assert agent.settings.bearer_token == "alice-token"
    assert agent.settings.locale == "en-US"
    persisted = settings_repository.load()
    assert persisted.session_user_id == "alice"
    assert persisted.bearer_token == "alice-token"
    assert persisted.locale == "en-US"
    assert statuses[-1]["account_locale"] == "en-US"
    assert statuses[-1]["account_locale_context"] == agent.ui_session_context()
    store.close()


@pytest.mark.parametrize("operation", ["save", "delete"])
def test_delayed_preference_worker_cannot_start_on_replacement_account(
    tmp_path, monkeypatch, operation
):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    alice = AppSettings(backend_url="https://mouchen.example.com")
    alice.apply_session("alice-token", {"user_id": "alice", "username": "alice"})
    settings_repository.save(alice)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    agent = DesktopAgent(settings_repository, store)
    alice_context = agent.ui_session_context()
    worker_waiting = threading.Event()
    allow_worker_to_enter = threading.Event()
    backend_calls = []
    results = []

    def unexpected_backend_call(*_args, **_kwargs):
        backend_calls.append(True)
        raise AssertionError("stale preference task reached the replacement account")

    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.put_global_advice_preference",
        unexpected_backend_call,
    )
    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.delete_goal_advice_preference",
        unexpected_backend_call,
    )

    def delayed_worker():
        worker_waiting.set()
        assert allow_worker_to_enter.wait(2)
        if operation == "save":
            result = agent.save_advice_preference(
                None,
                {"direction": "old account payload"},
                expected_session_context=alice_context,
            )
        else:
            result = agent.delete_goal_advice_preference(
                "old-goal",
                expected_session_context=alice_context,
            )
        results.append(result)

    worker = threading.Thread(target=delayed_worker)
    worker.start()
    assert worker_waiting.wait(1)

    bob = agent.settings
    bob.apply_session("bob-token", {"user_id": "bob", "username": "bob"})
    agent.update_settings(bob)
    allow_worker_to_enter.set()
    worker.join(2)

    assert not worker.is_alive()
    assert results == [{"_discarded": True}]
    assert backend_calls == []
    assert agent.settings.session_user_id == "bob"
    assert agent.settings.bearer_token == "bob-token"
    store.close()


@pytest.mark.parametrize("operation", ["save", "delete"])
def test_inflight_preference_response_is_discarded_after_account_switch(
    tmp_path, monkeypatch, operation
):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    alice = AppSettings(backend_url="https://mouchen.example.com")
    alice.apply_session("alice-token", {"user_id": "alice", "username": "alice"})
    settings_repository.save(alice)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    agent = DesktopAgent(settings_repository, store)
    alice_context = agent.ui_session_context()
    request_entered = threading.Event()
    release_request = threading.Event()
    request_tokens = []
    results = []

    def delayed_backend_call(client, *_args, **_kwargs):
        request_tokens.append(client.settings.bearer_token)
        request_entered.set()
        assert release_request.wait(2)
        return {"preference": {"direction": "old response"}}

    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.put_global_advice_preference",
        delayed_backend_call,
    )
    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.delete_goal_advice_preference",
        delayed_backend_call,
    )

    def mutate_preference():
        if operation == "save":
            result = agent.save_advice_preference(
                None,
                {"direction": "old account payload"},
                expected_session_context=alice_context,
            )
        else:
            result = agent.delete_goal_advice_preference(
                "old-goal",
                expected_session_context=alice_context,
            )
        results.append(result)

    worker = threading.Thread(target=mutate_preference)
    worker.start()
    assert request_entered.wait(1)

    bob = agent.settings
    bob.apply_session("bob-token", {"user_id": "bob", "username": "bob"})
    agent.update_settings(bob)
    release_request.set()
    worker.join(2)

    assert not worker.is_alive()
    assert request_tokens == ["alice-token"]
    assert results == [{"_discarded": True}]
    assert agent.settings.session_user_id == "bob"
    assert agent.settings.bearer_token == "bob-token"
    store.close()


def test_background_locale_get_waits_for_inflight_put_and_reads_new_value(
    tmp_path, monkeypatch
):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    alice = AppSettings(backend_url="https://mouchen.example.com", locale="zh-CN")
    alice.apply_session("alice-token", {"user_id": "alice", "username": "alice"})
    settings_repository.save(alice)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    agent = DesktopAgent(settings_repository, store)
    server_state = {"locale": "zh-CN"}
    put_entered = threading.Event()
    release_put = threading.Event()
    get_entered = threading.Event()
    release_get = threading.Event()

    def delayed_put(self, locale):
        assert self.settings.bearer_token == "alice-token"
        put_entered.set()
        assert release_put.wait(2)
        server_state["locale"] = locale
        return {"locale": locale}

    class DelayedGetClient:
        def account_preferences(self):
            get_entered.set()
            observed = server_state["locale"]
            assert release_get.wait(2)
            return {"locale": observed}

    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.update_account_locale",
        delayed_put,
    )
    initial_settings = agent.settings
    context = agent._session_context(initial_settings)
    results = {}
    errors = []

    def put_locale():
        try:
            results["put"] = agent.update_account_locale("en-US")
        except Exception as exc:  # pragma: no cover - assertion records leakage
            errors.append(exc)

    def refresh_locale():
        try:
            results["get"] = agent._refresh_account_locale_once(
                DelayedGetClient(),
                initial_settings,
                context=context,
                stop_event=threading.Event(),
            )
        except Exception as exc:  # pragma: no cover - assertion records leakage
            errors.append(exc)

    put_worker = threading.Thread(target=put_locale)
    put_worker.start()
    assert put_entered.wait(1)
    get_worker = threading.Thread(target=refresh_locale)
    get_worker.start()
    assert not get_entered.wait(0.1)

    release_put.set()
    assert get_entered.wait(1)
    release_get.set()
    put_worker.join(2)
    get_worker.join(2)

    assert not put_worker.is_alive()
    assert not get_worker.is_alive()
    assert errors == []
    assert server_state["locale"] == "en-US"
    assert results["put"]["locale"] == "en-US"
    assert results["get"].locale == "en-US"
    assert agent.settings.locale == "en-US"
    assert agent.settings.bearer_token == "alice-token"
    persisted = settings_repository.load()
    assert persisted.locale == "en-US"
    assert persisted.bearer_token == "alice-token"
    store.close()


def test_inflight_background_locale_get_finishes_before_waiting_put(
    tmp_path, monkeypatch
):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    alice = AppSettings(backend_url="https://mouchen.example.com", locale="zh-CN")
    alice.apply_session("alice-token", {"user_id": "alice", "username": "alice"})
    settings_repository.save(alice)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    agent = DesktopAgent(settings_repository, store)
    server_state = {"locale": "zh-CN"}
    get_entered = threading.Event()
    release_get = threading.Event()
    put_entered = threading.Event()

    class DelayedGetClient:
        def account_preferences(self):
            get_entered.set()
            observed = server_state["locale"]
            assert release_get.wait(2)
            return {"locale": observed}

    def update_server(self, locale):
        assert self.settings.bearer_token == "alice-token"
        put_entered.set()
        server_state["locale"] = locale
        return {"locale": locale}

    monkeypatch.setattr(
        "mouchen_desktop.backend.BackendClient.update_account_locale",
        update_server,
    )
    initial_settings = agent.settings
    context = agent._session_context(initial_settings)
    results = {}
    errors = []

    def refresh_locale():
        try:
            results["get"] = agent._refresh_account_locale_once(
                DelayedGetClient(),
                initial_settings,
                context=context,
                stop_event=threading.Event(),
            )
        except Exception as exc:  # pragma: no cover - assertion records leakage
            errors.append(exc)

    def put_locale():
        try:
            results["put"] = agent.update_account_locale("en-US")
        except Exception as exc:  # pragma: no cover - assertion records leakage
            errors.append(exc)

    get_worker = threading.Thread(target=refresh_locale)
    get_worker.start()
    assert get_entered.wait(1)
    put_worker = threading.Thread(target=put_locale)
    put_worker.start()
    assert not put_entered.wait(0.1)

    release_get.set()
    assert put_entered.wait(1)
    get_worker.join(2)
    put_worker.join(2)

    assert not get_worker.is_alive()
    assert not put_worker.is_alive()
    assert errors == []
    assert results["get"] is None
    assert results["put"]["locale"] == "en-US"
    assert server_state["locale"] == "en-US"
    assert agent.settings.locale == "en-US"
    assert agent.settings.bearer_token == "alice-token"
    persisted = settings_repository.load()
    assert persisted.locale == "en-US"
    assert persisted.bearer_token == "alice-token"
    store.close()


@pytest.mark.parametrize("request_fails", [False, True])
def test_late_background_locale_get_cannot_overwrite_new_account(
    tmp_path, request_fails
):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    alice = AppSettings(backend_url="https://mouchen.example.com", locale="zh-CN")
    alice.apply_session("alice-token", {"user_id": "alice", "username": "alice"})
    settings_repository.save(alice)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    statuses = []
    agent = DesktopAgent(settings_repository, store, on_status=statuses.append)
    entered = threading.Event()
    release = threading.Event()
    old_settings = agent.settings
    old_context = agent._session_context(old_settings)

    class DelayedClient:
        def account_preferences(self):
            entered.set()
            assert release.wait(2)
            if request_fails:
                raise BackendError("old account refresh failed")
            return {"locale": "en-US"}

    results = []
    errors = []

    def refresh_locale():
        try:
            results.append(
                agent._refresh_account_locale_once(
                    DelayedClient(),
                    old_settings,
                    context=old_context,
                    stop_event=threading.Event(),
                )
            )
        except Exception as exc:  # pragma: no cover - assertion records leakage
            errors.append(exc)

    worker = threading.Thread(target=refresh_locale)
    worker.start()
    assert entered.wait(1)

    bob = agent.settings
    bob.apply_session("bob-token", {"user_id": "bob", "username": "bob"})
    bob.locale = "zh-CN"
    agent.update_settings(bob)
    statuses.clear()
    release.set()
    worker.join(2)

    assert errors == []
    assert results == [None]
    assert agent.settings.session_user_id == "bob"
    assert agent.settings.bearer_token == "bob-token"
    assert agent.settings.locale == "zh-CN"
    persisted = settings_repository.load()
    assert persisted.session_user_id == "bob"
    assert persisted.bearer_token == "bob-token"
    assert persisted.locale == "zh-CN"
    assert not any("account_locale" in status for status in statuses)
    store.close()


def test_account_switch_fails_closed_while_old_worker_is_alive(tmp_path):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings_repository.save(AppSettings())
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    agent = DesktopAgent(settings_repository, store)
    release = threading.Event()
    old_worker = threading.Thread(target=lambda: release.wait(2), name="delayed-old-worker")
    old_worker.start()
    agent._threads = [old_worker]

    with pytest.raises(RuntimeError, match="account switch was cancelled"):
        agent.stop(timeout=0.01)
    with pytest.raises(RuntimeError, match="still stopping"):
        agent.start()

    release.set()
    old_worker.join(2)
    agent.stop(timeout=0.1)
    store.close()


def test_logged_in_agent_rejects_even_same_origin_service_path_change(tmp_path):
    settings_repository = SettingsRepository(tmp_path / "settings.json", IdentityProtector())
    settings = AppSettings(backend_url="https://mouchen.example.com/api-v1")
    settings.apply_session("alice-token", {"user_id": "alice", "username": "alice"})
    settings_repository.save(settings)
    store = EventStore(tmp_path / "desktop.db", IdentityProtector())
    agent = DesktopAgent(settings_repository, store)

    changed = agent.settings
    changed.backend_url = "https://mouchen.example.com/api-v2"

    with pytest.raises(ValueError, match="Sign out"):
        agent.update_settings(changed)
    assert agent.settings.backend_url.endswith("/api-v1")
    store.close()
