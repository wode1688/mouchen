from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "correct horse battery staple changed"


def _reset_main(main, path):
    try:
        main.repo.close()
    except Exception:
        pass
    main.repo = main.Repository(path)
    main.service = main.ProactiveService(main.repo)


def _register(client: TestClient, username: str, device_id: str):
    response = client.post(
        "/v1/auth/register",
        json={
            "username": username,
            "password": PASSWORD,
            "device_id": device_id,
            "device_name": f"{username} device",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _login(client: TestClient, username: str, password: str, device_id: str):
    return client.post(
        "/v1/auth/login",
        json={"username": username, "password": password, "device_id": device_id},
    )


def _headers(account: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {account['access_token']}"}


def _assert_no_credential_fields(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.casefold()
            assert "password" not in normalized
            assert "token_hash" not in normalized
            assert normalized not in {"access_token", "refresh_token"}
            _assert_no_credential_fields(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_credential_fields(item)


def test_account_export_is_no_store_secret_free_and_tenant_isolated(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    import app.main as main

    _reset_main(main, tmp_path / "account-export.db")
    with TestClient(main.app) as client:
        alice = _register(client, "Alice", "alice-windows")
        bob = _register(client, "Bob", "bob-android")
        alice_goal = client.post(
            "/v1/goals",
            headers=_headers(alice),
            json={
                "domain": "work",
                "title": "Alice private plan",
                "quote": "Alice private plan",
            },
        )
        bob_goal = client.post(
            "/v1/goals",
            headers=_headers(bob),
            json={
                "domain": "work",
                "title": "Bob private plan",
                "quote": "Bob private plan",
            },
        )
        assert alice_goal.status_code == bob_goal.status_code == 200
        alice_id = alice["session"]["user_id"]
        main.repo.audit_cloud_slice(
            alice_id,
            "openai",
            "gpt-test",
            "export-test",
            {
                "important": "included audit context",
                "password_hash": "must-not-export",
                "access_token": alice["access_token"],
            },
        )

        stolen = client.post(
            "/v1/account/export",
            headers=_headers(alice),
            json={"current_password": "not the account password"},
        )
        assert stolen.status_code == 403
        response = client.post(
            "/v1/account/export",
            headers=_headers(alice),
            json={"current_password": PASSWORD},
        )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["content-disposition"].endswith(
            'filename="mouchen-account-export.json"'
        )
        payload = response.json()
        serialized = json.dumps(payload, ensure_ascii=False)
        assert payload["account"]["id"] == alice_id
        assert "Alice private plan" in serialized
        assert "included audit context" in serialized
        assert "Bob private plan" not in serialized
        assert bob["session"]["user_id"] not in serialized
        assert alice["access_token"] not in serialized
        token_hash = main.repo._connection.execute(
            "SELECT token_hash FROM auth_tokens WHERE user_id=?", (alice_id,)
        ).fetchone()["token_hash"]
        password_hash = main.repo._connection.execute(
            "SELECT password_hash FROM users WHERE id=?", (alice_id,)
        ).fetchone()["password_hash"]
        assert token_hash not in serialized
        assert password_hash not in serialized
        _assert_no_credential_fields(payload)


def test_password_change_requires_current_password_and_rotates_every_session(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    import app.main as main

    _reset_main(main, tmp_path / "password-change.db")
    with TestClient(main.app) as client:
        alice_windows = _register(client, "Alice", "alice-windows")
        alice_android = _login(client, "Alice", PASSWORD, "alice-android").json()
        bob = _register(client, "Bob", "bob-windows")

        rejected = client.post(
            "/v1/account/password",
            headers=_headers(alice_windows),
            json={
                "current_password": "wrong password for this account",
                "new_password": NEW_PASSWORD,
            },
        )
        assert rejected.status_code == 403
        assert client.get("/v1/session", headers=_headers(alice_windows)).status_code == 200
        assert client.get("/v1/session", headers=_headers(alice_android)).status_code == 200

        changed = client.post(
            "/v1/account/password",
            headers=_headers(alice_windows),
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        )
        assert changed.status_code == 200, changed.text
        fresh = changed.json()
        assert fresh["access_token"] not in {
            alice_windows["access_token"],
            alice_android["access_token"],
        }
        assert fresh["session"]["device_id"] == "alice-windows"
        assert client.get("/v1/session", headers=_headers(alice_windows)).status_code == 401
        assert client.get("/v1/session", headers=_headers(alice_android)).status_code == 401
        assert client.get("/v1/session", headers=_headers(fresh)).status_code == 200
        assert _login(client, "Alice", PASSWORD, "old-password-test").status_code == 401
        assert _login(client, "Alice", NEW_PASSWORD, "new-password-test").status_code == 200
        assert client.get("/v1/session", headers=_headers(bob)).status_code == 200


def test_account_delete_is_atomic_dynamic_and_does_not_touch_other_tenants(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    import app.main as main

    _reset_main(main, tmp_path / "account-delete.db")
    with TestClient(main.app) as client:
        alice = _register(client, "Alice", "alice-windows")
        bob = _register(client, "Bob", "bob-windows")
        alice_id = alice["session"]["user_id"]
        bob_id = bob["session"]["user_id"]

        for account, title in ((alice, "Alice retained data"), (bob, "Bob survives")):
            created = client.post(
                "/v1/goals",
                headers=_headers(account),
                json={"domain": "work", "title": title, "quote": title},
            )
            assert created.status_code == 200, created.text

        connection = main.repo._connection
        connection.executescript(
            """
            CREATE TABLE future_user_data(
              id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, value TEXT NOT NULL
            );
            CREATE TABLE future_target_data(
              id INTEGER PRIMARY KEY, target_user_id TEXT NOT NULL, value TEXT NOT NULL
            );
            CREATE TABLE future_consumed_data(
              id INTEGER PRIMARY KEY, consumed_by_user_id TEXT NOT NULL, value TEXT NOT NULL
            );
            """
        )
        for uid, value in ((alice_id, "alice"), (bob_id, "bob")):
            connection.execute(
                "INSERT INTO future_user_data(user_id,value) VALUES(?,?)", (uid, value)
            )
            connection.execute(
                "INSERT INTO future_target_data(target_user_id,value) VALUES(?,?)",
                (uid, value),
            )
            connection.execute(
                "INSERT INTO future_consumed_data(consumed_by_user_id,value) VALUES(?,?)",
                (uid, value),
            )
            connection.execute(
                """INSERT INTO global_model_usage(
                  user_id,provider,purpose,calls,used_at
                ) VALUES(?,?,?,1,datetime('now'))""",
                (uid, "openai", "account-delete-test"),
            )
        connection.execute(
            """INSERT INTO auth_registration_codes(
              code_hash,consumed_by_user_id,consumed_at
            ) VALUES(?,?,datetime('now'))""",
            (hashlib.sha256(b"alice-delete-code").hexdigest(), alice_id),
        )
        connection.execute(
            """INSERT INTO auth_invites(
              id,code_hash,label,created_at,expires_at,consumed_at,consumed_by_user_id
            ) VALUES(?,?,?,datetime('now'),datetime('now','+1 day'),datetime('now'),?)""",
            (
                "alice-consumed-invite",
                hashlib.sha256(b"alice-consumed-invite").hexdigest(),
                "used invite",
                alice_id,
            ),
        )
        connection.commit()

        wrong = client.request(
            "DELETE",
            "/v1/account",
            headers=_headers(alice),
            json={"current_password": "wrong password for this account"},
        )
        assert wrong.status_code == 403
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM users WHERE id=?", (alice_id,)
        ).fetchone()["count"] == 1

        deleted = client.request(
            "DELETE",
            "/v1/account",
            headers=_headers(alice),
            json={"current_password": PASSWORD},
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json() == {"status": "deleted"}

        for table, columns in main.repo._account_reference_columns_locked():
            predicate = main.repo._account_reference_predicate(columns)
            row = connection.execute(
                f'SELECT COUNT(*) AS count FROM "{table}" WHERE {predicate}',
                tuple(alice_id for _ in columns),
            ).fetchone()
            assert row["count"] == 0, table
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM users WHERE id=?", (alice_id,)
        ).fetchone()["count"] == 0
        assert client.get("/v1/session", headers=_headers(alice)).status_code == 401

        assert client.get("/v1/session", headers=_headers(bob)).status_code == 200
        assert client.get("/v1/goals", headers=_headers(bob)).json()[0]["title"] == "Bob survives"
        for table, column in (
            ("future_user_data", "user_id"),
            ("future_target_data", "target_user_id"),
            ("future_consumed_data", "consumed_by_user_id"),
            ("global_model_usage", "user_id"),
        ):
            assert connection.execute(
                f'SELECT COUNT(*) AS count FROM "{table}" WHERE "{column}"=?',
                (bob_id,),
            ).fetchone()["count"] == 1


def test_deleted_account_is_tombstoned_against_delayed_and_concurrent_writes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    import app.main as main
    from app.domain.models import Event, Goal
    from app.storage import AccountRetired, Repository

    path = tmp_path / "retired-write-guard.db"
    _reset_main(main, path)
    with TestClient(main.app) as client:
        alice = _register(client, "Alice", "alice-windows")
        user_id = alice["session"]["user_id"]
        delayed = Repository(path)
        deleted = client.request(
            "DELETE",
            "/v1/account",
            headers=_headers(alice),
            json={"current_password": PASSWORD},
        )
        assert deleted.status_code == 200, deleted.text

        goal = Goal(
            id=uuid4(), user_id=user_id, domain="work", quote="late", title="late"
        )
        event = Event(
            event_id=uuid4(),
            user_id=user_id,
            source="delayed-worker",
            type="thought.note",
            facts={"text": "late"},
        )
        for write in (
            lambda: delayed.ensure_user(user_id),
            lambda: delayed.insert_goal(goal),
            lambda: delayed.insert_event(event),
            lambda: delayed.enqueue_analysis_job(user_id, event.event_id),
        ):
            with pytest.raises(AccountRetired):
                write()
        with pytest.raises(sqlite3.IntegrityError, match="account is retired"):
            delayed._connection.execute(
                """INSERT INTO advice(
                  id,user_id,domain,level,dedupe_key,status,delivery,
                  prediction_confidence,prediction_deadline,payload_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid4()), user_id, "work", 1, "late-advice", "active", "feed",
                    0.5, datetime.now(timezone.utc).isoformat(), "{}",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        delayed._connection.rollback()
        delayed.close()

        fresh = Repository(path)
        assert fresh._connection.execute(
            "SELECT COUNT(*) AS count FROM users WHERE id=?", (user_id,)
        ).fetchone()["count"] == 0
        assert fresh._connection.execute(
            "SELECT COUNT(*) AS count FROM events WHERE user_id=?", (user_id,)
        ).fetchone()["count"] == 0
        assert fresh._connection.execute(
            "SELECT COUNT(*) AS count FROM advice WHERE user_id=?", (user_id,)
        ).fetchone()["count"] == 0
        fresh.close()


def test_bootstrap_and_invite_remain_consumed_after_account_deletion(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "closed")
    monkeypatch.setenv("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE", "owner-once-code")
    import app.main as main

    path = tmp_path / "retired-registration-codes.db"
    _reset_main(main, path)
    with TestClient(main.app) as client:
        owner_response = client.post(
            "/v1/auth/register",
            json={
                "username": "Owner",
                "password": PASSWORD,
                "device_id": "owner-windows",
                "registration_code": "owner-once-code",
            },
        )
        assert owner_response.status_code == 200, owner_response.text
        owner = owner_response.json()
        owner_id = owner["session"]["user_id"]
        assert client.request(
            "DELETE", "/v1/account", headers=_headers(owner),
            json={"current_password": PASSWORD},
        ).status_code == 200
        reused = client.post(
            "/v1/auth/register",
            json={
                "username": "SecondOwner", "password": PASSWORD,
                "device_id": "second", "registration_code": "owner-once-code",
            },
        )
        assert reused.status_code == 409
        bootstrap = main.repo._connection.execute(
            "SELECT consumed_by_user_id,consumed_at FROM auth_registration_codes"
        ).fetchone()
        assert bootstrap["consumed_at"]
        assert bootstrap["consumed_by_user_id"] != owner_id
        assert owner_id not in bootstrap["consumed_by_user_id"]

    invite_path = tmp_path / "retired-invite.db"
    _reset_main(main, invite_path)
    alice_code = "alice-invite-long-random-value"
    bob_code = "bob-invite-long-random-value"
    for invite_id, code in (("alice-invite", alice_code), ("bob-invite", bob_code)):
        main.repo.create_registration_invite(
            invite_id=invite_id,
            code_hash=hashlib.sha256(code.encode()).hexdigest(),
            label=invite_id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    with TestClient(main.app) as client:
        def invite_register(username, device, code):
            response = client.post(
                "/v1/auth/register",
                json={"username": username, "password": PASSWORD,
                      "device_id": device, "registration_code": code},
            )
            assert response.status_code == 200, response.text
            return response.json()

        alice = invite_register("Alice", "alice", alice_code)
        bob = invite_register("Bob", "bob", bob_code)
        alice_id = alice["session"]["user_id"]
        assert client.request(
            "DELETE", "/v1/account", headers=_headers(alice),
            json={"current_password": PASSWORD},
        ).status_code == 200
        invite = main.repo._connection.execute(
            """SELECT consumed_at,consumed_by_user_id,consumed_by_user_hash
            FROM auth_invites WHERE id='alice-invite'"""
        ).fetchone()
        assert invite["consumed_at"]
        assert invite["consumed_by_user_id"] is None
        assert invite["consumed_by_user_hash"] == hashlib.sha256(
            alice_id.encode()
        ).hexdigest()
        assert client.post(
            "/v1/auth/register",
            json={"username": "Eve", "password": PASSWORD,
                  "device_id": "eve", "registration_code": alice_code},
        ).status_code == 403
        assert client.get("/v1/session", headers=_headers(bob)).status_code == 200


def test_account_step_up_limit_precedes_argon_and_is_tenant_scoped(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    import app.main as main

    _reset_main(main, tmp_path / "step-up-limit.db")
    with TestClient(main.app) as client:
        alice = _register(client, "Alice", "alice")
        bob = _register(client, "Bob", "bob")
        calls = 0
        original = main.repo.verify_account_password

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(main.repo, "verify_account_password", counted)
        for _ in range(5):
            assert client.post(
                "/v1/account/export", headers=_headers(alice),
                json={"current_password": "wrong-password"},
            ).status_code == 403
        blocked = client.post(
            "/v1/account/export", headers=_headers(alice),
            json={"current_password": "wrong-password"},
        )
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) >= 1
        assert calls == 5
        other = client.post(
            "/v1/account/export", headers=_headers(bob),
            json={"current_password": PASSWORD},
        )
        assert other.status_code == 200, other.text


def test_export_spool_closes_snapshot_cleans_up_and_fails_bounded(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ACCOUNT_EXPORT_TEMP_DIR", str(tmp_path / "exports"))
    from app.auth import utc_now as auth_now
    from app.storage import AccountExportTooLarge, Repository

    path = tmp_path / "export-spool.db"
    repo = Repository(path)
    issued = repo.register_account(
        username="Alice", password=PASSWORD, device_id="alice", device_name=None,
        expires_at=auth_now() + timedelta(days=1),
    )
    uid = issued.principal.user_id
    repo._connection.execute(
        """INSERT INTO events(id,user_id,source,type,occurred_at,payload_json,created_at)
        VALUES(?,?,?,?,?,?,?)""",
        (str(uuid4()), uid, "test", "thought.note", auth_now().isoformat(),
         json.dumps({"blob": "x" * 20_000}), auth_now().isoformat()),
    )
    repo._connection.commit()

    prepared = repo.prepare_account_export_file(uid, page_size=1)
    assert prepared.path.is_file() and prepared.bytes_written > 20_000
    checkpoint = repo._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    assert checkpoint[0] == 0
    prepared.cleanup()
    assert not prepared.path.exists() and not prepared.directory.exists()

    with pytest.raises(AccountExportTooLarge):
        repo.prepare_account_export_file(uid, page_size=1, maximum_bytes=128)
    assert list((tmp_path / "exports").glob("mouchen-account-export-*")) == []
    repo.close()
