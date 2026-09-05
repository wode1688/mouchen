from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


def _reset_main(main, path):
    try:
        main.repo.close()
    except Exception:
        pass
    main.repo = main.Repository(path)
    main.service = main.ProactiveService(main.repo)


def _register(client, username: str, device_id: str, code: str):
    return client.post(
        "/v1/auth/register",
        json={
            "username": username,
            "password": "correct horse battery staple",
            "device_id": device_id,
            "registration_code": code,
        },
    )


def test_database_invites_are_hashed_expiring_revocable_and_one_time(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "closed")
    monkeypatch.delenv("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE", raising=False)
    monkeypatch.delenv("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE_FILE", raising=False)
    import app.main as main

    path = tmp_path / "invites.db"
    _reset_main(main, path)
    now = datetime.now(timezone.utc)
    active_code = "active-code-with-enough-randomness"
    revoked_code = "revoked-code-with-enough-randomness"
    expired_code = "expired-code-with-enough-randomness"
    for invite_id, label, code in (
        ("active-id", "Friend A", active_code),
        ("revoked-id", "Friend B", revoked_code),
        ("expired-id", "Friend C", expired_code),
    ):
        main.repo.create_registration_invite(
            invite_id=invite_id,
            code_hash=hashlib.sha256(code.encode()).hexdigest(),
            label=label,
            expires_at=now + timedelta(days=1),
        )
    assert main.repo.revoke_registration_invite("revoked-id") is True
    main.repo._connection.execute(
        "UPDATE auth_invites SET expires_at=? WHERE id='expired-id'",
        ((now - timedelta(seconds=1)).isoformat(),),
    )
    main.repo._connection.commit()

    with TestClient(main.app) as client:
        accepted = _register(client, "Alice", "windows-a", active_code)
        assert accepted.status_code == 200, accepted.text
        assert _register(client, "Bob", "android-b", active_code).status_code in {
            403,
            409,
        }
        assert _register(client, "Carol", "ios-c", revoked_code).status_code == 403
        assert _register(client, "Dave", "windows-d", expired_code).status_code == 403

    inspection = main.Repository(path)
    rows = inspection.list_registration_invites()
    assert {row["id"]: row["state"] for row in rows} == {
        "active-id": "consumed",
        "revoked-id": "revoked",
        "expired-id": "expired",
    }
    stored = inspection._connection.execute(
        "SELECT code_hash FROM auth_invites WHERE id='active-id'"
    ).fetchone()["code_hash"]
    assert stored == hashlib.sha256(active_code.encode()).hexdigest()
    assert active_code not in json.dumps(rows)
    inspection.close()


def test_invite_consumption_is_atomic_under_competing_registrations(tmp_path):
    from app.storage import Repository

    repo = Repository(tmp_path / "atomic.db")
    code = "single-use-concurrent-code"
    digest = hashlib.sha256(code.encode()).hexdigest()
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    repo.create_registration_invite(
        invite_id="race-id", code_hash=digest, label="Race", expires_at=expiry
    )

    def claim(index: int) -> bool:
        try:
            repo.register_account(
                username=f"Person{index}",
                password="correct horse battery staple",
                device_id=f"device-{index}",
                device_name=None,
                expires_at=expiry,
                registration_code_hash=digest,
                registration_code_kind="invite",
            )
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (1, 2)))
    assert results.count(True) == 1
    assert repo.credentialed_user_count() == 1
    repo.close()


def test_admin_cli_generates_multiple_codes_once_and_lists_only_metadata(
    tmp_path, monkeypatch, capsys
):
    from app import invite_admin
    from app.storage import Repository

    path = tmp_path / "cli.db"
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(path))
    assert invite_admin.run(
        ["create", "--label", "Beta cohort", "--count", "2", "--ttl-hours", "24"]
    ) == 0
    created_output = json.loads(capsys.readouterr().out)
    created = created_output["created"]
    assert len(created) == 2
    codes = [item["registration_code"] for item in created]
    assert len(set(codes)) == 2
    assert all(len(code) >= 40 for code in codes)

    repo = Repository(path)
    stored = repo._connection.execute(
        "SELECT id,code_hash,label FROM auth_invites ORDER BY label"
    ).fetchall()
    assert len(stored) == 2
    assert {row["code_hash"] for row in stored} == {
        hashlib.sha256(code.encode()).hexdigest() for code in codes
    }
    assert all(code not in json.dumps([dict(row) for row in stored]) for code in codes)
    repo.close()

    assert invite_admin.run(["list"]) == 0
    listing = capsys.readouterr().out
    assert all(code not in listing for code in codes)
    assert "registration_code" not in listing
