from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient


def _reset_main(main, path):
    try:
        main.repo.close()
    except Exception:
        pass
    main.repo = main.Repository(path)
    main.service = main.ProactiveService(main.repo)


def _register(client, username="Alice", device_id="windows-1", **extra):
    payload = {
        "username": username,
        "password": "correct horse battery staple",
        "device_id": device_id,
        **extra,
    }
    return client.post("/v1/auth/register", json=payload)


def _login(client, username="Alice", device_id="windows-1", password=None):
    return client.post(
        "/v1/auth/login",
        json={
            "username": username,
            "password": password or "correct horse battery staple",
            "device_id": device_id,
        },
    )


def test_registration_is_closed_by_default_and_bootstrap_code_is_one_time(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.delenv("MOUCHEN_REGISTRATION_MODE", raising=False)
    monkeypatch.delenv("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE", raising=False)
    import app.main as main

    _reset_main(main, tmp_path / "closed.db")
    with TestClient(main.app) as client:
        assert _register(client).status_code == 403

    _reset_main(main, tmp_path / "invite.db")
    monkeypatch.setenv("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE", "one-use-invite")
    with TestClient(main.app) as client:
        first = _register(client, registration_code="one-use-invite")
        assert first.status_code == 200, first.text
        second = _register(
            client,
            username="Bob",
            device_id="android-2",
            registration_code="one-use-invite",
        )
        assert second.status_code == 409
        assert second.json()["detail"] == "registration unavailable"


def test_account_tokens_are_hashed_device_scoped_and_server_derive_tenant(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    import app.main as main

    _reset_main(main, tmp_path / "accounts.db")
    with TestClient(main.app) as client:
        registered = _register(client, username="  Alice  ")
        assert registered.status_code == 200, registered.text
        payload = registered.json()
        access_token = payload["access_token"]
        user_id = payload["session"]["user_id"]
        assert payload["token_type"] == "bearer"
        assert payload["session"]["username"] == "Alice"
        assert payload["session"]["device_id"] == "windows-1"
        assert payload["session"]["user_id"] != "Alice"

        user_row = main.repo._connection.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)
        ).fetchone()
        assert user_row["password_hash"].startswith("$argon2id$")
        assert "correct horse" not in user_row["password_hash"]
        token_row = main.repo._connection.execute(
            "SELECT token_hash FROM auth_tokens WHERE user_id=?", (user_id,)
        ).fetchone()
        assert token_row["token_hash"] != access_token
        assert access_token not in str(dict(token_row))

        headers = {"Authorization": f"Bearer {access_token}"}
        session = client.get("/v1/session", headers=headers)
        assert session.status_code == 200
        assert session.json()["user_id"] == user_id
        assert client.get(
            "/v1/goals", headers={**headers, "X-User-Id": "victim-user"}
        ).status_code == 403
        assert client.post(
            "/v1/advice-attention/claim",
            headers=headers,
            json={"device_id": "another-device"},
        ).status_code == 403

        duplicate = _register(client, username="ＡＬＩＣＥ", device_id="ios-1")
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"] == "registration unavailable"


def test_login_does_not_enumerate_accounts_and_tokens_expire_revoke_and_rotate(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    import app.main as main

    _reset_main(main, tmp_path / "sessions.db")
    with TestClient(main.app) as client:
        first = _register(client).json()
        first_token = first["access_token"]
        wrong = _login(client, password="this is the wrong password")
        absent = _login(client, username="Nobody", password="this is the wrong password")
        assert (wrong.status_code, wrong.json()) == (absent.status_code, absent.json())
        assert wrong.status_code == 401
        assert wrong.json() == {"detail": "invalid credentials"}

        android = _login(client, device_id="android-1")
        assert android.status_code == 200
        android_token = android.json()["access_token"]
        assert client.get(
            "/v1/session", headers={"Authorization": f"Bearer {first_token}"}
        ).status_code == 200
        rotated = _login(client, device_id="windows-1").json()["access_token"]
        assert rotated != first_token
        assert client.get(
            "/v1/session", headers={"Authorization": f"Bearer {first_token}"}
        ).status_code == 401
        assert client.get(
            "/v1/session", headers={"Authorization": f"Bearer {android_token}"}
        ).status_code == 200

        # Resolve through the token locator without putting the bearer in SQL/logs.
        from app.auth import token_locator

        token_id = token_locator(rotated)
        main.repo._connection.execute(
            "UPDATE auth_tokens SET expires_at=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), token_id),
        )
        main.repo._connection.commit()
        assert client.get(
            "/v1/session", headers={"Authorization": f"Bearer {rotated}"}
        ).status_code == 401

        logout = client.post(
            "/v1/auth/logout", headers={"Authorization": f"Bearer {android_token}"}
        )
        assert logout.status_code == 200
        assert client.get(
            "/v1/session", headers={"Authorization": f"Bearer {android_token}"}
        ).status_code == 401


def test_two_tenants_can_reuse_object_uuid_and_never_cross_read(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    import app.main as main

    _reset_main(main, tmp_path / "tenant-keys.db")
    shared_id = str(uuid4())
    with TestClient(main.app) as client:
        alice = _register(client, username="Alice", device_id="a").json()
        bob = _register(client, username="Bob", device_id="b").json()
        a_headers = {"Authorization": f"Bearer {alice['access_token']}"}
        b_headers = {"Authorization": f"Bearer {bob['access_token']}"}
        for headers, title in ((a_headers, "Alice goal"), (b_headers, "Bob goal")):
            response = client.put(
                f"/v1/goals/{shared_id}",
                headers=headers,
                json={"domain": "work", "title": title, "quote": title},
            )
            assert response.status_code == 200, response.text
        assert client.get("/v1/goals", headers=a_headers).json()[0]["title"] == "Alice goal"
        assert client.get("/v1/goals", headers=b_headers).json()[0]["title"] == "Bob goal"
        assert client.put(
            f"/v1/advice-preferences/goals/{uuid4()}",
            headers=a_headers,
            json={"frequency_mode": None, "event_types": None, "revision": 0},
        ).status_code == 404


def test_legacy_owner_claim_preserves_business_user_id_and_ambiguous_migration_blocks(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.delenv("MOUCHEN_REGISTRATION_MODE", raising=False)
    monkeypatch.setenv("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE", "claim-old-data")
    import app.main as main

    legacy_path = tmp_path / "legacy.db"
    legacy = main.Repository(legacy_path)
    legacy.ensure_user("owner-old-id")
    legacy.close()
    monkeypatch.setenv(
        "MOUCHEN_LEGACY_USERNAMES_JSON", json.dumps({"owner-old-id": "Owner"})
    )
    _reset_main(main, legacy_path)
    with TestClient(main.app) as client:
        claimed = _register(
            client,
            username="owner",
            registration_code="claim-old-data",
        )
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["session"]["user_id"] == "owner-old-id"

    monkeypatch.delenv("MOUCHEN_LEGACY_USERNAMES_JSON", raising=False)
    ambiguous_path = tmp_path / "ambiguous.db"
    ambiguous = main.Repository(ambiguous_path)
    ambiguous.ensure_user("tenant-a")
    ambiguous.ensure_user("tenant-b")
    ambiguous.close()
    _reset_main(main, ambiguous_path)
    assert main.repo.auth_migration_blocked is True
    with TestClient(main.app) as client:
        response = _register(client, registration_code="claim-old-data")
        assert response.status_code == 503
        assert "tenant-a" not in response.text


def test_bootstrap_typo_cannot_bypass_mapped_legacy_owner_or_consume_code(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "closed")
    monkeypatch.setenv("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE", "claim-old-data")
    import app.main as main

    path = tmp_path / "mapped-owner-typo.db"
    legacy = main.Repository(path)
    legacy.ensure_user("owner-old-id")
    legacy.insert_goal(
        main.Goal(
            id=uuid4(),
            user_id="owner-old-id",
            domain="work",
            version=1,
            quote="Protect the existing owner's data",
            title="Existing owner goal",
        )
    )
    legacy.close()
    monkeypatch.setenv(
        "MOUCHEN_LEGACY_USERNAMES_JSON", json.dumps({"owner-old-id": "Owner"})
    )
    _reset_main(main, path)

    with TestClient(main.app) as client:
        typo = _register(
            client,
            username="owner-typo",
            registration_code="claim-old-data",
        )
        assert typo.status_code == 409
        assert typo.json()["detail"] == "registration unavailable"
        assert main.repo._connection.execute(
            "SELECT COUNT(*) AS count FROM auth_registration_codes"
        ).fetchone()["count"] == 0
        assert main.repo._connection.execute(
            "SELECT COUNT(*) AS count FROM users"
        ).fetchone()["count"] == 1

        claimed = _register(
            client,
            username="owner",
            registration_code="claim-old-data",
        )
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["session"]["user_id"] == "owner-old-id"
        goals = client.get(
            "/v1/goals",
            headers={"Authorization": f"Bearer {claimed.json()['access_token']}"},
        )
        assert goals.status_code == 200
        assert [goal["title"] for goal in goals.json()] == ["Existing owner goal"]


def test_bootstrap_can_create_first_owner_in_empty_database(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "closed")
    monkeypatch.setenv("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE", "fresh-owner-code")
    monkeypatch.delenv("MOUCHEN_LEGACY_USERNAMES_JSON", raising=False)
    import app.main as main

    _reset_main(main, tmp_path / "fresh-install.db")
    with TestClient(main.app) as client:
        created = _register(
            client,
            username="first-owner",
            registration_code="fresh-owner-code",
        )
        assert created.status_code == 200, created.text
        created_id = created.json()["session"]["user_id"]
        row = main.repo._connection.execute(
            "SELECT username_normalized,password_hash FROM users WHERE id=?",
            (created_id,),
        ).fetchone()
        assert row["username_normalized"] == "first-owner"
        assert row["password_hash"].startswith("$argon2id$")


def test_single_unmapped_legacy_tenant_blocks_registration_until_explicitly_mapped(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE", "claim-old-data")
    monkeypatch.delenv("MOUCHEN_LEGACY_USERNAMES_JSON", raising=False)
    import app.main as main

    path = tmp_path / "single-unmapped.db"
    legacy = main.Repository(path)
    legacy.ensure_user("owner-old-id")
    legacy.close()

    _reset_main(main, path)
    assert main.repo.auth_migration_blocked is True
    with TestClient(main.app) as client:
        denied = _register(
            client,
            username="typo-owner",
            registration_code="claim-old-data",
        )
        assert denied.status_code == 503
        assert main.repo.credentialed_user_count() == 0


def test_ordinary_invite_cannot_claim_mapped_legacy_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "closed")
    monkeypatch.setenv("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE", "owner-only-bootstrap")
    import app.main as main

    path = tmp_path / "invite-cannot-claim-owner.db"
    legacy = main.Repository(path)
    legacy.ensure_user("owner-old-id")
    legacy.close()
    monkeypatch.setenv(
        "MOUCHEN_LEGACY_USERNAMES_JSON", json.dumps({"owner-old-id": "Owner"})
    )
    _reset_main(main, path)
    invite_code = "ordinary-invite-with-enough-randomness"
    import hashlib

    main.repo.create_registration_invite(
        invite_id="ordinary-invite",
        code_hash=hashlib.sha256(invite_code.encode()).hexdigest(),
        label="ordinary user",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    with TestClient(main.app) as client:
        denied = _register(
            client,
            username="owner",
            registration_code=invite_code,
        )
        assert denied.status_code == 409
        owner_row = main.repo._connection.execute(
            "SELECT password_hash FROM users WHERE id='owner-old-id'"
        ).fetchone()
        assert owner_row["password_hash"] is None
        invite_row = main.repo._connection.execute(
            "SELECT consumed_at FROM auth_invites WHERE id='ordinary-invite'"
        ).fetchone()
        assert invite_row["consumed_at"] is None

        claimed = _register(
            client,
            username="owner",
            registration_code="owner-only-bootstrap",
        )
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["session"]["user_id"] == "owner-old-id"


def test_legacy_static_token_requires_explicit_single_user_off_loopback(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_API_TOKEN", "legacy-secret")
    monkeypatch.delenv("MOUCHEN_SINGLE_USER_ID", raising=False)
    import app.main as main

    _reset_main(main, tmp_path / "legacy-token.db")
    auth = {"Authorization": "Bearer legacy-secret"}
    with TestClient(main.app, client=("192.0.2.10", 5000)) as client:
        assert client.get("/v1/goals", headers=auth).status_code == 401

    monkeypatch.setenv("MOUCHEN_SINGLE_USER_ID", "owner-old-id")
    _reset_main(main, tmp_path / "legacy-token-2.db")
    with TestClient(main.app, client=("192.0.2.10", 5000)) as client:
        assert client.get("/v1/goals", headers=auth).status_code == 403
        allowed = client.get(
            "/v1/goals", headers={**auth, "X-User-Id": "owner-old-id"}
        )
        assert allowed.status_code == 200


def test_legacy_static_token_is_rejected_when_commercial_switch_is_off(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_API_TOKEN", "legacy-secret")
    monkeypatch.setenv("MOUCHEN_SINGLE_USER_ID", "owner-old-id")
    monkeypatch.delenv("MOUCHEN_LEGACY_AUTH_ENABLED", raising=False)
    import app.main as main

    _reset_main(main, tmp_path / "legacy-disabled.db")
    headers = {
        "Authorization": "Bearer legacy-secret",
        "X-User-Id": "owner-old-id",
    }
    with TestClient(main.app, client=("192.0.2.10", 5000)) as client:
        response = client.get("/v1/goals", headers=headers)
        assert response.status_code == 401
        assert "legacy-secret" not in response.text


def test_ready_accepts_commercial_account_auth_without_legacy_token(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    monkeypatch.setenv("MOUCHEN_REQUIRE_API_TOKEN", "true")
    monkeypatch.delenv("MOUCHEN_LEGACY_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("MOUCHEN_API_TOKEN", raising=False)
    monkeypatch.delenv("MOUCHEN_API_TOKEN_FILE", raising=False)
    import app.main as main

    _reset_main(main, tmp_path / "commercial-ready.db")
    with TestClient(main.app) as client:
        assert client.get("/ready").status_code == 503
        assert _register(client).status_code == 200
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json()["authentication"] == "ready"


def test_fresh_closed_commercial_install_is_bootstrap_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "closed")
    monkeypatch.setenv("MOUCHEN_LEGACY_AUTH_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE", "fresh-owner-code")
    monkeypatch.delenv("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE_FILE", raising=False)
    import app.main as main

    _reset_main(main, tmp_path / "fresh-bootstrap-ready.db")
    with TestClient(main.app) as client:
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json()["authentication"] == "bootstrap-required"

        created = _register(
            client,
            username="fresh-owner",
            registration_code="fresh-owner-code",
        )
        assert created.status_code == 200, created.text
        assert client.get("/ready").json()["authentication"] == "ready"


def test_bootstrap_readiness_requires_empty_database(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "closed")
    monkeypatch.setenv("MOUCHEN_LEGACY_AUTH_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE", "fresh-owner-code")
    monkeypatch.delenv("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE_FILE", raising=False)
    import app.main as main

    _reset_main(main, tmp_path / "nonempty-bootstrap-not-ready.db")
    main.repo.ensure_user("orphaned-legacy-user")
    with TestClient(main.app) as client:
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["authentication"] == "unavailable"


def test_commercial_account_restart_ignores_stale_legacy_single_user_setting(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    monkeypatch.setenv("MOUCHEN_SINGLE_USER_ID", "old-private-alpha-owner")
    monkeypatch.setenv("MOUCHEN_LEGACY_AUTH_ENABLED", "false")
    import app.main as main

    path = tmp_path / "commercial-restart.db"
    _reset_main(main, path)
    with TestClient(main.app) as client:
        created = _register(client, username="commercial-owner")
        assert created.status_code == 200, created.text
        account_user_id = created.json()["session"]["user_id"]
        assert account_user_id != "old-private-alpha-owner"

    _reset_main(main, path)
    assert main.repo.auth_migration_blocked is False
    with TestClient(main.app) as client:
        logged_in = _login(client, username="commercial-owner")
        assert logged_in.status_code == 200, logged_in.text
        assert logged_in.json()["session"]["user_id"] == account_user_id
