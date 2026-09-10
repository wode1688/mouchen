from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.abuse_controls import AccountStepUpLimiter, RollingWindowLimiter
from app.domain.models import Goal
from app.domain.models import Event
from app.storage import (
    DeviceQuotaExceeded,
    EventQuotaExceeded,
    GoalQuotaExceeded,
    Repository,
)


def test_limiter_total_hit_capacity_is_atomic_and_recovers_after_expiry():
    limiter = RollingWindowLimiter(max_keys=100, max_hits=3, sweep_seconds=60)

    assert limiter.reserve((("a", 10),), window_seconds=10, now=0).allowed
    assert limiter.reserve((("a", 10),), window_seconds=10, now=1).allowed
    assert limiter.reserve((("b", 10),), window_seconds=20, now=2).allowed
    assert limiter._total_hits == 3

    blocked = limiter.reserve(
        (("a", 10), ("new-key", 10)),
        window_seconds=20,
        now=3,
    )
    assert not blocked.allowed
    assert blocked.retry_after == 7
    assert limiter._total_hits == 3
    assert "new-key" not in limiter._entries

    assert limiter.reserve((("new-key", 10),), window_seconds=20, now=11).allowed
    assert limiter._total_hits == 2


def test_limiter_clear_and_reset_keep_total_hit_count_consistent():
    limiter = RollingWindowLimiter(max_keys=10, max_hits=10)
    assert limiter.reserve((("a", 5), ("b", 5)), window_seconds=60, now=0).allowed
    assert limiter.reserve((("a", 5),), window_seconds=60, now=1).allowed
    assert limiter._total_hits == 3
    limiter.clear("a")
    assert limiter._total_hits == 1
    limiter.reset()
    assert limiter._total_hits == 0
    assert limiter._entries == {}


def test_account_step_up_user_budget_cannot_be_bypassed_by_source_rotation(monkeypatch):
    monkeypatch.setenv("MOUCHEN_ACCOUNT_STEP_UP_ATTEMPTS_PER_WINDOW", "5")
    monkeypatch.setenv("MOUCHEN_ACCOUNT_STEP_UP_SOURCE_ATTEMPTS_PER_WINDOW", "100")
    limiter = AccountStepUpLimiter()

    for index in range(5):
        assert limiter.reserve("victim", f"198.51.100.{index}").allowed
    blocked = limiter.reserve("victim", "203.0.113.99")
    assert not blocked.allowed
    assert blocked.retry_after >= 1
    assert limiter.reserve("other-user", "203.0.113.99").allowed

def test_goal_target_and_per_user_count_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    monkeypatch.setenv("MOUCHEN_MAX_GOAL_TARGET_BYTES", "1024")
    monkeypatch.setenv("MOUCHEN_MAX_GOALS_PER_USER", "1")
    import app.main as main

    try:
        main.repo.close()
    except Exception:
        pass
    main.repo = main.Repository(tmp_path / "goal-limits.db")
    main.service = main.ProactiveService(main.repo)

    with TestClient(main.app) as client:
        account = client.post(
            "/v1/auth/register",
            json={
                "username": "goal-limit-user",
                "password": "correct horse battery staple",
                "device_id": "goal-limit-device",
            },
        )
        assert account.status_code == 200, account.text
        headers = {"Authorization": f"Bearer {account.json()['access_token']}"}
        oversized = client.post(
            "/v1/goals",
            headers=headers,
            json={
                "domain": "work",
                "title": "oversized",
                "quote": "test target quota",
                "target": {"blob": "x" * 1500},
            },
        )
        assert oversized.status_code == 413

        first = client.post(
            "/v1/goals",
            headers=headers,
            json={
                "domain": "work",
                "title": "first",
                "quote": "first accepted goal",
                "target": {"done": True},
            },
        )
        assert first.status_code == 200, first.text
        second = client.post(
            "/v1/goals",
            headers=headers,
            json={
                "domain": "work",
                "title": "second",
                "quote": "second blocked goal",
                "target": {},
            },
        )
        assert second.status_code == 507

        update = client.put(
            f"/v1/goals/{first.json()['id']}",
            headers=headers,
            json={
                "domain": "work",
                "title": "updated",
                "quote": "existing goal update remains allowed",
                "target": {"done": False},
            },
        )
        assert update.status_code == 200, update.text


def test_goal_count_quota_is_atomic_across_repository_connections(tmp_path):
    database_path = tmp_path / "goal-race.db"
    first = Repository(database_path)
    second = Repository(database_path)
    barrier = __import__("threading").Barrier(2)

    def insert(repo: Repository, title: str) -> str:
        barrier.wait()
        try:
            repo.insert_goal(
                Goal(
                    user_id="shared-user",
                    domain="work",
                    title=title,
                    quote=f"{title} quote",
                ),
                max_goals=1,
            )
            return "inserted"
        except GoalQuotaExceeded:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda item: insert(*item),
                ((first, "first"), (second, "second")),
            )
        )

    assert sorted(results) == ["blocked", "inserted"]
    assert len(first.list_goals("shared-user")) == 1
    first.close()
    second.close()


def test_event_storage_quota_is_durable_and_covers_internal_inserts(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "event-quota.db"
    probe = Event(
        user_id="quota-user",
        source="review.synthetic",
        type="goal.review_window",
        facts={"text": "first internal review"},
        evidence_ref="review:first",
    )
    first_size = len(probe.model_dump_json().encode("utf-8"))
    monkeypatch.setenv("MOUCHEN_EVENT_STORAGE_BYTES_PER_USER", str(first_size + 8))
    first = Repository(database_path)
    assert first.insert_event(probe)

    # A new Repository simulates another worker/restart. The durable counter,
    # not a process cache or HTTP-only guard, must reject the next review row.
    second = Repository(database_path)
    with __import__("pytest").raises(EventQuotaExceeded):
        second.insert_event(
            Event(
                user_id="quota-user",
                source="review.synthetic",
                type="goal.review_window",
                facts={"text": "second internal review"},
                evidence_ref="review:second",
            )
        )
    with second._lock:
        usage = second._connection.execute(
            "SELECT event_bytes FROM tenant_storage_usage WHERE user_id=?",
            ("quota-user",),
        ).fetchone()
    assert int(usage["event_bytes"]) == first_size
    assert len(second.recent_events("quota-user")) == 1
    first.close()
    second.close()


def test_event_storage_reconcile_preserves_an_unchanged_counter_row(tmp_path):
    database_path = tmp_path / "event-counter-stable.db"
    first = Repository(database_path)
    assert first.insert_event(
        Event(
            user_id="stable-user",
            source="windows.accessibility",
            type="ui.visible_text",
            facts={"text": "stable counter evidence"},
            evidence_ref="stable:first",
        )
    )
    with first._lock:
        before = tuple(
            first._connection.execute(
                """SELECT user_id,event_bytes,updated_at
                FROM tenant_storage_usage WHERE user_id=?""",
                ("stable-user",),
            ).fetchone()
        )
    first.close()

    reopened = Repository(database_path)
    with reopened._lock:
        after = tuple(
            reopened._connection.execute(
                """SELECT user_id,event_bytes,updated_at
                FROM tenant_storage_usage WHERE user_id=?""",
                ("stable-user",),
            ).fetchone()
        )
    reopened.close()

    assert after == before


def test_event_storage_reconcile_repairs_corruption_and_removes_orphans(tmp_path):
    database_path = tmp_path / "event-counter-repair.db"
    repository = Repository(database_path)
    assert repository.insert_event(
        Event(
            user_id="repair-user",
            source="review.synthetic",
            type="goal.review_window",
            facts={"text": "repair this durable counter"},
            evidence_ref="repair:first",
        )
    )
    repository.close()

    with sqlite3.connect(database_path) as connection:
        expected_bytes = int(
            connection.execute(
                """SELECT SUM(length(CAST(payload_json AS BLOB)))
                FROM events WHERE user_id=?""",
                ("repair-user",),
            ).fetchone()[0]
        )
        connection.execute(
            """UPDATE tenant_storage_usage
            SET event_bytes=0,updated_at='2000-01-01T00:00:00+00:00'
            WHERE user_id='repair-user'"""
        )
        connection.execute(
            """INSERT INTO tenant_storage_usage(user_id,event_bytes,updated_at)
            VALUES('orphan-user',123,'2000-01-01T00:00:00+00:00')"""
        )
        connection.commit()

    repaired = Repository(database_path)
    with repaired._lock:
        repaired_row = repaired._connection.execute(
            """SELECT event_bytes,updated_at FROM tenant_storage_usage
            WHERE user_id='repair-user'"""
        ).fetchone()
        orphan = repaired._connection.execute(
            "SELECT 1 FROM tenant_storage_usage WHERE user_id='orphan-user'"
        ).fetchone()
    repaired.close()

    assert int(repaired_row["event_bytes"]) == expected_bytes
    assert repaired_row["updated_at"] != "2000-01-01T00:00:00+00:00"
    assert orphan is None


def test_demo_seed_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    monkeypatch.delenv("MOUCHEN_DEMO_SEED_ENABLED", raising=False)
    import app.main as main

    try:
        main.repo.close()
    except Exception:
        pass
    main.repo = main.Repository(tmp_path / "demo-disabled.db")
    main.service = main.ProactiveService(main.repo)

    with TestClient(main.app) as client:
        account = client.post(
            "/v1/auth/register",
            json={
                "username": "demo-disabled-user",
                "password": "correct horse battery staple",
                "device_id": "demo-disabled-device",
            },
        )
        headers = {"Authorization": f"Bearer {account.json()['access_token']}"}
        response = client.post("/v1/demo/seed", headers=headers)
        assert response.status_code == 404
        assert client.get("/v1/goals", headers=headers).json() == []


def test_charter_and_guidance_quotas_are_enforced_by_api(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    monkeypatch.setenv("MOUCHEN_DEMO_SEED_ENABLED", "true")
    monkeypatch.setenv("MOUCHEN_MAX_CHARTERS_PER_USER", "1")
    monkeypatch.setenv("MOUCHEN_MAX_GUIDANCE_PER_ADVICE", "1")
    import app.main as main

    try:
        main.repo.close()
    except Exception:
        pass
    main.repo = main.Repository(tmp_path / "persistent-quotas.db")
    main.service = main.ProactiveService(main.repo)

    with TestClient(main.app) as client:
        account = client.post(
            "/v1/auth/register",
            json={
                "username": "persistent-quota-user",
                "password": "correct horse battery staple",
                "device_id": "persistent-quota-device",
            },
        )
        headers = {"Authorization": f"Bearer {account.json()['access_token']}"}
        seeded = client.post("/v1/demo/seed", headers=headers)
        assert seeded.status_code == 200, seeded.text

        charter_update = client.put(
            "/v1/charter/work",
            headers=headers,
            json={"max_level": "L2", "redline_authorized": False},
        )
        assert charter_update.status_code == 200
        extra_charter = client.put(
            "/v1/charter/random-domain",
            headers=headers,
            json={"max_level": "L2", "redline_authorized": False},
        )
        assert extra_charter.status_code == 507

        advice_id = seeded.json()["evaluation"]["advice"]["id"]
        first_guidance = client.post(
            f"/v1/advice/{advice_id}/feedback",
            headers=headers,
            json={"kind": "guidance", "note": "focus on business outcomes"},
        )
        assert first_guidance.status_code == 200, first_guidance.text
        second_guidance = client.post(
            f"/v1/advice/{advice_id}/feedback",
            headers=headers,
            json={"kind": "guidance", "note": "a different unbounded note"},
        )
        assert second_guidance.status_code == 507


def test_auth_failure_rows_are_pruned_and_hard_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_AUTH_FAILURE_MAX_ROWS", "2")
    monkeypatch.setenv("MOUCHEN_AUTH_FAILURE_RETENTION_HOURS", "1")
    repo = Repository(tmp_path / "auth-failure-cap.db")
    now = datetime.now(timezone.utc)
    with repo._lock:
        repo._connection.execute(
            "INSERT INTO auth_login_failures VALUES(?,?,?,?,?)",
            (
                "expired",
                1,
                (now - timedelta(hours=3)).isoformat(),
                None,
                (now - timedelta(hours=3)).isoformat(),
            ),
        )
        repo._record_login_failure_locked("one", now)
        repo._record_login_failure_locked("two", now)
        repo._record_login_failure_locked("three", now)
        repo._connection.commit()
        rows = repo._connection.execute(
            "SELECT identity_hash FROM auth_login_failures ORDER BY identity_hash"
        ).fetchall()
    assert [row["identity_hash"] for row in rows] == ["one", "two"]
    repo.close()


def test_active_device_tokens_are_bounded_per_user(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_AUTH_ACTIVE_DEVICES_PER_USER", "2")
    repo = Repository(tmp_path / "device-cap.db")
    expiry = datetime.now(timezone.utc) + timedelta(days=30)
    issued = repo.register_account(
        username="device-cap-user",
        password="correct horse battery staple",
        device_id="device-1",
        device_name=None,
        expires_at=expiry,
    )
    for device_id in ("device-2", "device-3"):
        login = repo.login_account(
            username="device-cap-user",
            password="correct horse battery staple",
            device_id=device_id,
            device_name=None,
            client_key=device_id,
            expires_at=expiry,
        )
        assert login is not None
    with repo._lock:
        active = repo._connection.execute(
            "SELECT COUNT(*) AS count FROM auth_tokens WHERE user_id=? AND revoked_at IS NULL",
            (issued.principal.user_id,),
        ).fetchone()
    assert int(active["count"]) == 2
    repo.close()


def test_revoked_token_history_and_attention_devices_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_AUTH_ACTIVE_DEVICES_PER_USER", "2")
    monkeypatch.setenv("MOUCHEN_AUTH_TOKEN_HISTORY_PER_USER", "3")
    monkeypatch.setenv("MOUCHEN_MAX_DEVICES_PER_USER", "2")
    repo = Repository(tmp_path / "identity-history-cap.db")
    expiry = datetime.now(timezone.utc) + timedelta(days=30)
    issued = repo.register_account(
        username="identity-history-user",
        password="correct horse battery staple",
        device_id="rotated-device",
        device_name=None,
        expires_at=expiry,
    )
    for _ in range(8):
        assert repo.login_account(
            username="identity-history-user",
            password="correct horse battery staple",
            device_id="rotated-device",
            device_name=None,
            client_key="same-source",
            expires_at=expiry,
        ) is not None
    with repo._lock:
        history = repo._connection.execute(
            "SELECT COUNT(*) AS count FROM auth_tokens WHERE user_id=?",
            (issued.principal.user_id,),
        ).fetchone()
    assert int(history["count"]) <= 4  # three revoked plus one live token

    assert repo.claim_advice_attention(issued.principal.user_id, "device-one") is None
    assert repo.claim_advice_attention(issued.principal.user_id, "device-two") is None
    with __import__("pytest").raises(DeviceQuotaExceeded):
        repo.claim_advice_attention(issued.principal.user_id, "device-three")
    with repo._lock:
        devices = repo._connection.execute(
            "SELECT COUNT(*) AS count FROM devices WHERE user_id=?",
            (issued.principal.user_id,),
        ).fetchone()
    assert int(devices["count"]) == 2
    repo.close()


def test_cloud_audit_retention_and_per_user_bytes_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_CLOUD_AUDIT_BYTES_PER_USER", "4096")
    monkeypatch.setenv("MOUCHEN_CLOUD_AUDIT_RETENTION_DAYS", "1")
    repo = Repository(tmp_path / "cloud-audit-cap.db")
    repo.audit_cloud_slice("u1", "openai", "model", "old", {"text": "old"})
    with repo._lock:
        repo._connection.execute(
            "UPDATE cloud_slices SET created_at=? WHERE user_id='u1'",
            ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),),
        )
        repo._connection.commit()
    for index in range(4):
        repo.audit_cloud_slice(
            "u1",
            "openai",
            "model",
            f"new-{index}",
            {"text": f"{index}-" + ("x" * 1800)},
        )
    with repo._lock:
        rows = repo._connection.execute(
            "SELECT purpose,length(CAST(redacted_context_json AS BLOB)) AS bytes FROM cloud_slices WHERE user_id='u1' ORDER BY id"
        ).fetchall()
    assert rows
    assert all(row["purpose"] != "old" for row in rows)
    assert rows[-1]["purpose"] == "new-3"
    assert sum(int(row["bytes"]) for row in rows) <= 4096
    repo.close()
