from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.account_export_admission import AccountExportAdmission, AccountExportBusy


def test_export_admission_is_cross_connection_single_flight_and_durable_cooldown(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ACCOUNT_EXPORT_COOLDOWN_SECONDS", "300")
    database = tmp_path / "export-admission.db"
    started = datetime(2026, 8, 12, tzinfo=timezone.utc)
    first_gate = AccountExportAdmission(database)
    second_gate = AccountExportAdmission(database)
    lease = first_gate.acquire("alice", now=started)

    with pytest.raises(AccountExportBusy) as global_block:
        second_gate.acquire("bob", now=started + timedelta(seconds=1))
    assert global_block.value.reason_code == "account_export_in_progress"
    assert lease.release()
    with pytest.raises(AccountExportBusy) as cooldown:
        AccountExportAdmission(database).acquire(
            "alice", now=started + timedelta(seconds=2)
        )
    assert cooldown.value.reason_code == "account_export_cooldown"
    bob = second_gate.acquire("bob", now=started + timedelta(seconds=2))
    assert bob.release()


def test_export_response_deadline_terminates_and_cleans_private_spool(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    import app.main as main
    from app.storage import PreparedAccountExport

    directory = tmp_path / "mouchen-account-export-deadline"
    directory.mkdir()
    path = directory / "account-export.json"
    path.write_bytes(b"{}")
    released = False

    class Lease:
        expires_at = datetime.now(timezone.utc) + timedelta(milliseconds=20)

        def release(self):
            nonlocal released
            released = True
            return True

    response = main._AccountExportResponse(
        PreparedAccountExport(path, directory, 2), Lease()
    )

    async def slow_send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            await asyncio.sleep(0.2)

    with pytest.raises(TimeoutError):
        asyncio.run(response({}, None, slow_send))
    assert released is True
    assert not path.exists() and not directory.exists()
