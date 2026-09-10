from __future__ import annotations

import json
import os

import pytest

from mouchen_desktop.local_mode import runtime_environment, setup
from mouchen_desktop.security import IdentityProtector
from mouchen_desktop.settings import SettingsRepository
from mouchen_desktop.store import EventStore


def test_bootstrap_is_isolated_authenticated_and_does_not_grant_collection(tmp_path):
    profile = tmp_path / "local-profile"
    protector = IdentityProtector()
    path = setup(profile, device_id="synthetic-desktop", protector=protector)
    config = json.loads(path.read_text(encoding="utf-8"))
    assert "bearer_token" not in config and "password" not in config
    settings = SettingsRepository(profile / "desktop" / "settings.json", protector=protector).load()
    assert settings.auth_mode == "session" and settings.has_session_credentials()
    assert not settings.has_current_collection_consent()
    assert not settings.collection_enabled and not settings.proactive_cloud_enabled
    assert settings.backend_url == "http://127.0.0.1:8788"
    store = EventStore(profile / "desktop" / "mouchen-desktop.db", protector=protector)
    try:
        assert store.get_or_create_device_id() == "synthetic-desktop"
    finally:
        store.close()
    from app.storage import Repository
    repository = Repository(config["database_path"])
    try:
        assert repository.authenticate_access_token(settings.bearer_token).user_id == config["session_user_id"]
        assert repository.total_user_count() == 1
    finally:
        repository.close()
    before = path.read_bytes()
    setup(profile, device_id="synthetic-desktop", protector=protector)
    assert path.read_bytes() == before


def test_runtime_does_not_inherit_other_deployment_configuration(monkeypatch):
    monkeypatch.setenv("MOUCHEN_API_TOKEN", "synthetic-old")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    monkeypatch.setenv("MOUCHEN_MODEL_PROVIDER", "openai")
    config = {"database_path": "synthetic.db", "desktop_data_dir": "synthetic-desktop",
              "backend_url": "http://127.0.0.1:8788", "desktop_device_id": "synthetic-desktop"}
    env = runtime_environment(config)
    assert "MOUCHEN_API_TOKEN" not in env
    assert env["MOUCHEN_MODEL_PROVIDER"] == "rules"
    assert env["MOUCHEN_LEGACY_AUTH_ENABLED"] == "false"
    assert env["MOUCHEN_REGISTRATION_MODE"] == "closed"
    assert env["MOUCHEN_ANALYSIS_BACKGROUND_ENABLED"] == "false"


@pytest.mark.skipif(os.name != "nt", reason="Windows profile mutexes")
def test_profile_mutexes_isolate_apps_and_still_prevent_duplicate_profiles(tmp_path, monkeypatch):
    from mouchen_desktop.single_instance import SingleInstance
    monkeypatch.setenv("MOUCHEN_DESKTOP_DATA_DIR", str(tmp_path / "one"))
    one = SingleInstance()
    duplicate = SingleInstance()
    monkeypatch.setenv("MOUCHEN_DESKTOP_DATA_DIR", str(tmp_path / "two"))
    two = SingleInstance()
    try:
        assert not one.already_running
        assert duplicate.already_running
        assert not two.already_running
    finally:
        one.close()
        duplicate.close()
        two.close()
