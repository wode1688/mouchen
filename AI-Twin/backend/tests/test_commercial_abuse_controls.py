from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.abuse_controls import (
    AuthenticationHashAdmission,
    AuthenticatedRequestLimiter,
    LoginAttemptLimiter,
    RegistrationAttemptLimiter,
    RollingWindowLimiter,
    client_source_ip,
    commercial_proxy_configuration_ready,
)
from app.domain.models import Event
from app.model_gateway import ModelGateway, ModelRoute, ModelUnavailable
from app.storage import Repository


def _reset_main(main, path):
    try:
        main.repo.close()
    except Exception:
        pass
    main.repo = main.Repository(path)
    main.service = main.ProactiveService(main.repo)


def _account(client: TestClient, username: str = "abuse-test") -> dict:
    response = client.post(
        "/v1/auth/register",
        json={
            "username": username,
            "password": "correct horse battery staple",
            "device_id": "windows-test",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _headers(account: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {account['access_token']}"}


def _event(text: str = "ordinary observation") -> dict:
    return {
        "source": "test",
        "type": "thought.note",
        "facts": {"text": text},
    }


def test_commercial_proxy_configuration_requires_exact_accountable_peer(monkeypatch):
    monkeypatch.setenv("MOUCHEN_COMMERCIAL_MULTI_USER", "true")
    monkeypatch.setenv("MOUCHEN_TRUSTED_PROXY_HEADER", "x-forwarded-for")
    for value in ("", "172.21.0.0/24", "0.0.0.0/0", "invalid"):
        monkeypatch.setenv("MOUCHEN_TRUSTED_PROXY_CIDRS", value)
        assert commercial_proxy_configuration_ready() is False
    monkeypatch.setenv("MOUCHEN_TRUSTED_PROXY_CIDRS", "172.21.0.1/32")
    assert commercial_proxy_configuration_ready() is True


def test_general_request_event_facts_and_audio_have_independent_413_limits(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    monkeypatch.setenv("MOUCHEN_MAX_REQUEST_BYTES", "1024")
    import app.main as main

    _reset_main(main, tmp_path / "body-limits.db")
    with TestClient(main.app) as client:
        oversized_login = client.post(
            "/v1/auth/login",
            json={
                "username": "nobody",
                "password": "x" * 2000,
                "device_id": "test",
            },
        )
        assert oversized_login.status_code == 413

        account = _account(client)
        headers = _headers(account)
        monkeypatch.setenv("MOUCHEN_MAX_REQUEST_BYTES", str(32 * 1024))
        monkeypatch.setenv("MOUCHEN_MAX_EVENT_FACTS_BYTES", "1024")
        facts = client.post(
            "/v1/events",
            headers=headers,
            json=_event("x" * 1500),
        )
        assert facts.status_code == 413
        assert facts.json()["detail"] == "event facts are too large"

        monkeypatch.setenv("MOUCHEN_MAX_AUDIO_BYTES", "1024")
        audio = client.post(
            "/v1/audio/transcribe",
            headers=headers,
            content=b"\0" * 2048,
        )
        assert audio.status_code == 413


def test_event_ingest_rate_and_incremental_storage_quota_are_explicit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    monkeypatch.setenv("MOUCHEN_EVENT_INGESTS_PER_WINDOW", "2")
    monkeypatch.setenv("MOUCHEN_EVENT_RATE_WINDOW_SECONDS", "60")
    import app.main as main

    _reset_main(main, tmp_path / "event-limits.db")
    with TestClient(main.app) as client:
        account = _account(client, "event-rate-user")
        headers = _headers(account)
        assert client.post("/v1/events", headers=headers, json=_event("one")).status_code == 200
        assert client.post("/v1/events", headers=headers, json=_event("two")).status_code == 200
        limited = client.post("/v1/events", headers=headers, json=_event("three"))
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) >= 1

    monkeypatch.setenv("MOUCHEN_EVENT_INGESTS_PER_WINDOW", "100")
    monkeypatch.setenv("MOUCHEN_EVENT_STORAGE_BYTES_PER_USER", "1")
    _reset_main(main, tmp_path / "event-quota.db")
    with TestClient(main.app) as client:
        account = _account(client, "storage-user")
        full = client.post("/v1/events", headers=_headers(account), json=_event())
        assert full.status_code == 507
        assert full.json()["detail"] == "event storage quota exceeded"


def test_authenticated_request_limiter_enforces_user_and_global_windows(monkeypatch):
    monkeypatch.setenv("MOUCHEN_AUTHENTICATED_RATE_WINDOW_SECONDS", "60")
    monkeypatch.setenv("MOUCHEN_AUTHENTICATED_REQUESTS_PER_USER_WINDOW", "2")
    monkeypatch.setenv("MOUCHEN_AUTHENTICATED_REQUESTS_GLOBAL_WINDOW", "3")
    limiter = AuthenticatedRequestLimiter()

    assert limiter.reserve("u1").allowed
    assert limiter.reserve("u1").allowed
    user_blocked = limiter.reserve("u1")
    assert not user_blocked.allowed
    assert user_blocked.retry_after >= 1

    assert limiter.reserve("u2").allowed
    global_blocked = limiter.reserve("u3")
    assert not global_blocked.allowed
    assert global_blocked.retry_after >= 1


def test_authentication_hash_admission_bounds_expensive_parallel_work(monkeypatch):
    monkeypatch.setenv("MOUCHEN_AUTH_HASH_CONCURRENCY", "2")
    admission = AuthenticationHashAdmission()
    assert admission.acquire().allowed
    assert admission.acquire().allowed
    blocked = admission.acquire()
    assert not blocked.allowed
    assert blocked.retry_after == 1
    admission.release()
    assert admission.acquire().allowed
    admission.release()
    admission.release()


def test_authenticated_api_middleware_rejects_before_route_work(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    monkeypatch.setenv("MOUCHEN_AUTHENTICATED_RATE_WINDOW_SECONDS", "60")
    monkeypatch.setenv("MOUCHEN_AUTHENTICATED_REQUESTS_PER_USER_WINDOW", "2")
    monkeypatch.setenv("MOUCHEN_AUTHENTICATED_REQUESTS_GLOBAL_WINDOW", "100")
    import app.main as main

    _reset_main(main, tmp_path / "authenticated-api-rate.db")
    with TestClient(main.app) as client:
        account = _account(client, "api-rate-user")
        headers = _headers(account)
        assert client.get("/v1/session", headers=headers).status_code == 200
        assert client.get("/v1/session", headers=headers).status_code == 200
        blocked = client.get("/v1/session", headers=headers)
        assert blocked.status_code == 429
        assert blocked.json()["detail"] == "authenticated request rate exceeded"
        assert int(blocked.headers["retry-after"]) >= 1


def test_forwarded_ip_is_ignored_by_default_and_honored_only_from_trusted_peer(
    monkeypatch,
):
    request = SimpleNamespace(
        client=SimpleNamespace(host="198.51.100.20"),
        headers={"x-forwarded-for": "203.0.113.9"},
    )
    monkeypatch.delenv("MOUCHEN_TRUSTED_PROXY_CIDRS", raising=False)
    assert client_source_ip(request) == "198.51.100.20"

    trusted_request = SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.2"),
        headers={
            "forwarded": "for=192.0.2.200",
            "x-forwarded-for": "203.0.113.9, 10.0.0.1",
        },
    )
    monkeypatch.setenv("MOUCHEN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    monkeypatch.delenv("MOUCHEN_TRUSTED_PROXY_HEADER", raising=False)
    assert client_source_ip(trusted_request) == "203.0.113.9"

    monkeypatch.setenv("MOUCHEN_TRUSTED_PROXY_HEADER", "forwarded")
    assert client_source_ip(trusted_request) == "192.0.2.200"


def test_login_limiter_has_independent_account_and_source_windows(monkeypatch):
    monkeypatch.setenv("MOUCHEN_LOGIN_RATE_WINDOW_SECONDS", "60")
    monkeypatch.setenv("MOUCHEN_LOGIN_ACCOUNT_ATTEMPTS_PER_WINDOW", "2")
    monkeypatch.setenv("MOUCHEN_LOGIN_SOURCE_ATTEMPTS_PER_WINDOW", "3")

    account_limiter = LoginAttemptLimiter()
    assert account_limiter.reserve("victim", "198.51.100.1").allowed
    assert account_limiter.reserve("victim", "198.51.100.2").allowed
    assert not account_limiter.reserve("victim", "198.51.100.3").allowed

    source_limiter = LoginAttemptLimiter()
    assert source_limiter.reserve("one", "198.51.100.8").allowed
    assert source_limiter.reserve("two", "198.51.100.8").allowed
    assert source_limiter.reserve("three", "198.51.100.8").allowed
    assert not source_limiter.reserve("four", "198.51.100.8").allowed


def test_successful_logins_still_consume_the_account_window(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    monkeypatch.setenv("MOUCHEN_LOGIN_RATE_WINDOW_SECONDS", "60")
    monkeypatch.setenv("MOUCHEN_LOGIN_ACCOUNT_ATTEMPTS_PER_WINDOW", "2")
    monkeypatch.setenv("MOUCHEN_LOGIN_SOURCE_ATTEMPTS_PER_WINDOW", "100")
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    import app.main as main

    _reset_main(main, tmp_path / "successful-login-rate.db")
    monkeypatch.setattr(main, "login_attempts", LoginAttemptLimiter())
    with TestClient(main.app) as client:
        _account(client, "bounded-login-user")
        payload = {
            "username": "bounded-login-user",
            "password": "correct horse battery staple",
            "device_id": "windows-test",
        }
        assert client.post("/v1/auth/login", json=payload).status_code == 200
        assert client.post("/v1/auth/login", json=payload).status_code == 200
        blocked = client.post("/v1/auth/login", json=payload)
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) >= 1


def test_open_registration_requires_a_second_explicit_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    monkeypatch.setenv("MOUCHEN_ALLOW_OPEN_REGISTRATION", "false")
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    import app.main as main

    _reset_main(main, tmp_path / "open-registration-guard.db")
    with TestClient(main.app) as client:
        denied = client.post(
            "/v1/auth/register",
            json={
                "username": "must-not-register",
                "password": "correct horse battery staple",
                "device_id": "windows-test",
            },
        )
        assert denied.status_code == 503
        assert main.repo.credentialed_user_count() == 0


def test_rejected_random_accounts_do_not_grow_limiter_memory(monkeypatch):
    limiter = RollingWindowLimiter()
    assert limiter.reserve((("source:fixed", 1),), window_seconds=60, now=1).allowed
    for index in range(1_000):
        decision = limiter.reserve(
            ((f"account:{index}", 10), ("source:fixed", 1)),
            window_seconds=60,
            now=2,
        )
        assert not decision.allowed
    assert set(limiter._entries) == {"source:fixed"}


def test_limiter_sweeps_untouched_keys_and_recovers_capacity():
    limiter = RollingWindowLimiter(max_keys=2, sweep_seconds=1)
    assert limiter.reserve((("a", 1),), window_seconds=10, now=0).allowed
    assert limiter.reserve((("b", 1),), window_seconds=60, now=0).allowed
    blocked = limiter.reserve((("c", 1),), window_seconds=60, now=1)
    assert not blocked.allowed
    assert blocked.retry_after == 9
    assert set(limiter._entries) == {"a", "b"}

    assert limiter.reserve((("c", 1),), window_seconds=60, now=11).allowed
    assert set(limiter._entries) == {"b", "c"}


def test_limiter_capacity_rejection_is_atomic_and_never_evicts_live_keys():
    limiter = RollingWindowLimiter(max_keys=2, sweep_seconds=60)
    assert limiter.reserve((("victim", 3),), window_seconds=60, now=0).allowed
    assert limiter.reserve((("source", 3),), window_seconds=60, now=0).allowed

    blocked = limiter.reserve(
        (("victim", 3), ("random-account", 3)),
        window_seconds=60,
        now=1,
    )
    assert not blocked.allowed
    assert set(limiter._entries) == {"victim", "source"}
    assert len(limiter._entries["victim"]) == 1

    assert limiter.reserve((("victim", 3),), window_seconds=60, now=2).allowed
    assert len(limiter._entries["victim"]) == 2


def test_registration_rate_limit_precedes_password_hash_work(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_SOURCE_ATTEMPTS_PER_WINDOW", "2")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_RATE_WINDOW_SECONDS", "60")
    import app.main as main
    import app.storage as storage

    _reset_main(main, tmp_path / "registration-rate.db")
    monkeypatch.setattr(main, "registration_attempts", RegistrationAttemptLimiter())
    password_hashes = 0
    real_hash_password = storage.hash_password

    def counted_hash_password(password):
        nonlocal password_hashes
        password_hashes += 1
        return real_hash_password(password)

    monkeypatch.setattr(storage, "hash_password", counted_hash_password)
    with TestClient(main.app) as client:
        for index in range(2):
            response = client.post(
                "/v1/auth/register",
                json={
                    "username": f"registration-{index}",
                    "password": "correct horse battery staple",
                    "device_id": f"device-{index}",
                },
                headers={"X-Forwarded-For": f"203.0.113.{index + 1}"},
            )
            assert response.status_code == 200, response.text
        limited = client.post(
            "/v1/auth/register",
            json={
                "username": "registration-blocked",
                "password": "correct horse battery staple",
                "device_id": "device-blocked",
            },
            headers={"X-Forwarded-For": "203.0.113.200"},
        )
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert password_hashes == 2


def test_global_model_budget_survives_restart_and_does_not_charge_local_routes(
    tmp_path,
):
    path = tmp_path / "durable-global-budget.db"
    now = datetime.now(timezone.utc)
    repo = Repository(path)
    gateway = ModelGateway(
        global_budget_reserver=lambda **kwargs: repo.reserve_global_direct_model_call(
            **kwargs,
            now=now,
            hourly_call_limit=1,
            daily_call_limit=2,
        )
    )

    async def fake_openai(route, prompt, context, user_id):
        return "ok"

    gateway._openai = fake_openai
    route = ModelRoute("openai", "test-model", "routine")
    assert asyncio.run(gateway.generate(route, "prompt", {}, user_id="u1")) == "ok"
    assert repo.global_model_calls_used(since=now - timedelta(minutes=1)) == 1
    repo.close()

    restarted = Repository(path)
    restarted_gateway = ModelGateway(
        global_budget_reserver=lambda **kwargs: (
            restarted.reserve_global_direct_model_call(
                **kwargs,
                now=now + timedelta(seconds=1),
                hourly_call_limit=1,
                daily_call_limit=2,
            )
        )
    )
    restarted_gateway._openai = fake_openai
    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(restarted_gateway.generate(route, "prompt", {}, user_id="u2"))
    assert captured.value.reason_code == "global_model_budget_exhausted"
    assert captured.value.status_code == 429
    assert captured.value.retry_after_seconds >= 1

    assert asyncio.run(
        restarted_gateway.generate(
            ModelRoute("template", "deterministic-v1", "local"),
            "prompt",
            {},
        )
    )
    assert restarted.global_model_calls_used(since=now - timedelta(minutes=1)) == 1
    restarted.close()


def test_global_model_budget_allows_only_one_cross_connection_reservation(tmp_path):
    path = tmp_path / "concurrent-global-budget.db"
    repositories = [Repository(path) for _ in range(16)]
    now = datetime.now(timezone.utc)

    def reserve(index):
        return repositories[index].reserve_global_direct_model_call(
            f"u{index}",
            "openai",
            "concurrent-test",
            now=now,
            hourly_call_limit=1,
            daily_call_limit=1,
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        decisions = list(executor.map(reserve, range(len(repositories))))
    assert sum(decision.allowed for decision in decisions) == 1
    assert repositories[0].global_model_calls_used(
        since=now - timedelta(minutes=1)
    ) == 1
    for repository in repositories:
        repository.close()


def test_model_budget_http_429_includes_retry_after(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_GLOBAL_MODEL_CALLS_PER_HOUR", "1")
    monkeypatch.setenv("MOUCHEN_GLOBAL_MODEL_CALLS_PER_DAY", "1")
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_MODEL_PROVIDER", "openai")
    import app.main as main

    database_path = tmp_path / "model-budget-http.db"
    _reset_main(main, database_path)
    gateway = ModelGateway(global_budget_reserver=main._reserve_global_model_call)
    monkeypatch.setattr(main, "models", gateway)

    async def fake_openai(route, prompt, context, user_id):
        return "ok"

    monkeypatch.setattr(gateway, "_openai", fake_openai)
    with TestClient(main.app) as client:
        account = _account(client, "model-budget-user")
        body = {
            "level": "L2",
            "purpose": "budget-test",
            "prompt": "give one action",
            "redacted_context": {"event_text": "release decision"},
            "outbound_approved": True,
        }
        first = client.post(
            "/v1/model/analyze",
            headers=_headers(account),
            json=body,
        )
        second = client.post(
            "/v1/model/analyze",
            headers=_headers(account),
            json=body,
        )
    assert first.status_code == 200, first.text
    assert second.status_code == 429
    assert int(second.headers["retry-after"]) >= 1
    verified = Repository(database_path)
    assert verified._connection.execute(
        "SELECT COUNT(*) AS count FROM cloud_slices"
    ).fetchone()["count"] == 1
    verified.close()


def test_direct_model_endpoint_rate_limit_covers_local_template(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_REGISTRATION_MODE", "open")
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_DIRECT_MODEL_REQUESTS_PER_WINDOW", "1")
    monkeypatch.setenv("MOUCHEN_DIRECT_MODEL_RATE_WINDOW_SECONDS", "60")
    import app.main as main

    _reset_main(main, tmp_path / "direct-http-rate.db")
    with TestClient(main.app) as client:
        account = _account(client, "direct-template-user")
        body = {
            "level": "L1",
            "purpose": "local-template-test",
            "prompt": "give one local action",
        }
        first = client.post(
            "/v1/model/analyze",
            headers=_headers(account),
            json=body,
        )
        second = client.post(
            "/v1/model/analyze",
            headers=_headers(account),
            json=body,
        )
    assert first.status_code == 200, first.text
    assert second.status_code == 429
    assert int(second.headers["retry-after"]) >= 1


def test_direct_paid_calls_share_tenant_budget_and_do_not_block_other_user(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOUCHEN_ANALYSIS_MODEL_CALLS_PER_HOUR", "1")
    monkeypatch.setenv("MOUCHEN_ANALYSIS_MODEL_CALLS_PER_DAY", "10")
    repo = Repository(tmp_path / "direct-tenant-budget.db")
    now = datetime.now(timezone.utc)
    first = repo.reserve_global_direct_model_call(
        "u1",
        "openai",
        "direct-one",
        now=now,
        hourly_call_limit=10,
        daily_call_limit=20,
    )
    blocked = repo.reserve_global_direct_model_call(
        "u1",
        "openai",
        "direct-two",
        now=now + timedelta(seconds=1),
        hourly_call_limit=10,
        daily_call_limit=20,
    )
    other = repo.reserve_global_direct_model_call(
        "u2",
        "openai",
        "direct-other",
        now=now + timedelta(seconds=1),
        hourly_call_limit=10,
        daily_call_limit=20,
    )
    assert first.allowed
    assert not blocked.allowed
    assert blocked.reason_code == "user_model_budget_exhausted"
    assert other.allowed
    assert repo.analysis_model_calls_used("u1", since=now - timedelta(minutes=1)) == 1
    assert repo.analysis_model_calls_used("u2", since=now - timedelta(minutes=1)) == 1

    event = _queued_cloud_event(repo, "u1", "queued after direct call", occurred_at=now)
    assert repo.claim_analysis_job(
        "u1",
        event.event_id,
        now=now + timedelta(seconds=2),
        hourly_call_limit=2,
        daily_call_limit=10,
        global_hourly_call_limit=20,
        global_daily_call_limit=40,
    ) is None
    job = repo.get_analysis_job("u1", event.event_id)
    assert job is not None and job.attempts == 0
    assert job.last_reason == "analysis_budget_deferred"
    repo.close()


def test_queue_global_cap_below_worst_case_fails_closed_without_attempt(tmp_path):
    repo = Repository(tmp_path / "queue-hard-cap-one.db")
    now = datetime.now(timezone.utc)
    event = _queued_cloud_event(repo, "u1", "one-call global hard cap", occurred_at=now)
    assert repo.claim_analysis_job(
        "u1",
        event.event_id,
        now=now + timedelta(seconds=1),
        hourly_call_limit=20,
        daily_call_limit=20,
        global_hourly_call_limit=1,
        global_daily_call_limit=1,
    ) is None
    job = repo.get_analysis_job("u1", event.event_id)
    assert job is not None and job.attempts == 0
    assert job.last_reason == "global_model_budget_deferred"
    assert repo._connection.execute(
        "SELECT COUNT(*) AS count FROM analysis_model_reservations"
    ).fetchone()["count"] == 0
    repo.close()


def _queued_cloud_event(
    repo: Repository,
    user_id: str,
    text: str,
    *,
    occurred_at: datetime | None = None,
) -> Event:
    event = Event(
        user_id=user_id,
        source="commercial-abuse-test",
        type="thought.note",
        facts={"text": text, "user_question": True},
        occurred_at=occurred_at or datetime.now(timezone.utc),
    )
    assert repo.insert_event(event)
    assert repo.enqueue_analysis_job(
        user_id,
        event.event_id,
        cloud_approved=True,
    )
    return event


def test_direct_model_sliding_window_survives_restart_and_template_has_no_lease(
    tmp_path,
):
    path = tmp_path / "direct-window.db"
    now = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)
    repo = Repository(path)
    first = repo.admit_direct_model_request(
        "u1",
        "template",
        now=now,
        requests_per_window=2,
        rate_window_seconds=60,
    )
    second = repo.admit_direct_model_request(
        "u1",
        "template",
        now=now + timedelta(seconds=1),
        requests_per_window=2,
        rate_window_seconds=60,
    )
    assert first.allowed and first.lease_id is None
    assert second.allowed and second.lease_id is None
    assert repo._connection.execute(
        "SELECT COUNT(*) AS count FROM direct_model_leases"
    ).fetchone()["count"] == 0
    repo.close()

    restarted = Repository(path)
    blocked = restarted.admit_direct_model_request(
        "u1",
        "openai",
        now=now + timedelta(seconds=2),
        requests_per_window=2,
        rate_window_seconds=60,
    )
    assert not blocked.allowed
    assert blocked.reason_code == "direct_model_rate_limit_exhausted"
    assert blocked.retry_after == 58
    assert blocked.retry_at == now + timedelta(seconds=60)

    admitted = restarted.admit_direct_model_request(
        "u1",
        "openai",
        now=now + timedelta(seconds=61),
        requests_per_window=2,
        rate_window_seconds=60,
    )
    assert admitted.allowed
    assert admitted.lease_id is not None
    assert restarted.release_direct_model_lease(
        admitted.lease_id,
        now=now + timedelta(seconds=62),
    )
    assert not restarted.release_direct_model_lease(
        admitted.lease_id,
        now=now + timedelta(seconds=63),
    )
    restarted.close()


@pytest.mark.parametrize("provider", ["ollama", "openai", "codex_cli"])
def test_direct_model_executable_providers_take_concurrency_lease(tmp_path, provider):
    repo = Repository(tmp_path / f"direct-{provider}.db")
    decision = repo.admit_direct_model_request(
        "u1",
        provider,
        requests_per_window=20,
        user_concurrency_limit=2,
        global_concurrency_limit=4,
    )
    assert decision.allowed
    assert decision.lease_id is not None
    assert repo.release_direct_model_lease(decision.lease_id)
    repo.close()


def test_direct_model_user_and_global_concurrency_are_atomic_across_connections(
    tmp_path,
):
    path = tmp_path / "direct-concurrency.db"
    now = datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc)
    repositories = [Repository(path) for _ in range(12)]

    def admit(index):
        return repositories[index].admit_direct_model_request(
            f"u{index}",
            "ollama",
            now=now,
            requests_per_window=100,
            user_concurrency_limit=2,
            global_concurrency_limit=3,
            lease_seconds=120,
        )

    with ThreadPoolExecutor(max_workers=12) as executor:
        decisions = list(executor.map(admit, range(len(repositories))))
    assert sum(decision.allowed for decision in decisions) == 3
    assert all(
        decision.reason_code == "direct_model_global_concurrency_exhausted"
        for decision in decisions
        if not decision.allowed
    )
    assert all(decision.retry_after == 120 for decision in decisions if not decision.allowed)

    for decision in decisions:
        if decision.lease_id is not None:
            assert repositories[0].release_direct_model_lease(
                decision.lease_id,
                now=now + timedelta(seconds=1),
            )

    one = repositories[0].admit_direct_model_request(
        "same-user",
        "openai",
        now=now + timedelta(seconds=2),
        requests_per_window=100,
        user_concurrency_limit=1,
        global_concurrency_limit=10,
        lease_seconds=90,
    )
    assert one.allowed and one.lease_id is not None
    same_user_blocked = repositories[1].admit_direct_model_request(
        "same-user",
        "codex_cli",
        now=now + timedelta(seconds=3),
        requests_per_window=100,
        user_concurrency_limit=1,
        global_concurrency_limit=10,
        lease_seconds=90,
    )
    assert not same_user_blocked.allowed
    assert same_user_blocked.reason_code == "direct_model_user_concurrency_exhausted"
    assert same_user_blocked.retry_after == 89
    for repository in repositories:
        repository.close()


def test_direct_model_expired_lease_is_reclaimed_after_restart(tmp_path):
    path = tmp_path / "direct-expired.db"
    now = datetime(2026, 8, 12, 3, 0, tzinfo=timezone.utc)
    repo = Repository(path)
    abandoned = repo.admit_direct_model_request(
        "u1",
        "openai",
        now=now,
        requests_per_window=100,
        global_concurrency_limit=1,
        lease_seconds=10,
    )
    assert abandoned.allowed and abandoned.lease_id is not None
    repo.close()

    restarted = Repository(path)
    replacement = restarted.admit_direct_model_request(
        "u2",
        "ollama",
        now=now + timedelta(seconds=11),
        requests_per_window=100,
        global_concurrency_limit=1,
        lease_seconds=10,
    )
    assert replacement.allowed and replacement.lease_id is not None
    expired = restarted._connection.execute(
        "SELECT released_at FROM direct_model_leases WHERE id=?",
        (abandoned.lease_id,),
    ).fetchone()
    assert expired is not None and expired["released_at"] is not None
    restarted.close()


def test_global_budget_reclaims_expired_worker_from_another_tenant(tmp_path):
    repo = Repository(tmp_path / "cross-tenant-expired-worker.db")
    now = datetime.now(timezone.utc)
    first = _queued_cloud_event(repo, "u1", "first decision", occurred_at=now)
    second = _queued_cloud_event(repo, "u2", "second decision", occurred_at=now)
    claim_time = now + timedelta(seconds=1)
    abandoned = repo.claim_analysis_job(
        "u1",
        first.event_id,
        now=claim_time,
        hourly_call_limit=10,
        daily_call_limit=10,
        global_hourly_call_limit=2,
        global_daily_call_limit=4,
    )
    assert abandoned is not None and abandoned.reservation_id is not None

    reclaimed_at = claim_time + timedelta(minutes=11)
    replacement = repo.claim_analysis_job(
        "u2",
        second.event_id,
        now=reclaimed_at,
        hourly_call_limit=10,
        daily_call_limit=10,
        global_hourly_call_limit=2,
        global_daily_call_limit=4,
    )
    assert replacement is not None and replacement.reservation_id is not None
    abandoned_job = repo.get_analysis_job("u1", first.event_id)
    assert abandoned_job is not None
    assert abandoned_job.status == "retry"
    assert abandoned_job.last_reason == "worker_lease_expired"
    abandoned_reservation = repo._connection.execute(
        "SELECT actual_calls,settled_at FROM analysis_model_reservations WHERE id=?",
        (abandoned.reservation_id,),
    ).fetchone()
    assert abandoned_reservation["actual_calls"] == 0
    assert abandoned_reservation["settled_at"] is not None
    repo.close()


def test_settlement_wakes_only_own_tenant_and_one_global_highest_priority(tmp_path):
    repo = Repository(tmp_path / "scoped-budget-wake.db")
    now = datetime.now(timezone.utc)
    source = _queued_cloud_event(repo, "owner", "source", occurred_at=now)
    owner_waiting = _queued_cloud_event(repo, "owner", "owner waiting", occurred_at=now)
    other_tenant = _queued_cloud_event(repo, "other", "other waiting", occurred_at=now)
    global_high = _queued_cloud_event(repo, "high", "global high", occurred_at=now)
    global_low = _queued_cloud_event(repo, "low", "global low", occurred_at=now)
    claim_time = now + timedelta(seconds=1)
    claim = repo.claim_analysis_job(
        "owner",
        source.event_id,
        now=claim_time,
        hourly_call_limit=20,
        daily_call_limit=20,
        global_hourly_call_limit=20,
        global_daily_call_limit=20,
    )
    assert claim is not None and claim.reservation_id is not None

    future = claim_time + timedelta(hours=1)
    rows = (
        (owner_waiting, "analysis_budget_deferred", 40),
        (other_tenant, "analysis_budget_deferred", 90),
        (global_high, "global_model_budget_deferred", 90),
        (global_low, "global_model_budget_deferred", 10),
    )
    for event, reason, priority in rows:
        repo._connection.execute(
            """UPDATE analysis_jobs
            SET last_reason=?, next_attempt_at=?, priority=?
            WHERE user_id=? AND event_id=?""",
            (
                reason,
                future.isoformat(),
                priority,
                event.user_id,
                str(event.event_id),
            ),
        )
    repo._connection.commit()

    assert repo.settle_analysis_model_reservation(
        claim.reservation_id,
        "owner",
        source.event_id,
        attempt=claim.job.attempts,
        now=claim_time + timedelta(seconds=1),
    )
    wake_time = claim_time + timedelta(seconds=1)
    assert repo.get_analysis_job("owner", owner_waiting.event_id).next_attempt_at == wake_time
    assert repo.get_analysis_job("other", other_tenant.event_id).next_attempt_at == future
    assert repo.get_analysis_job("high", global_high.event_id).next_attempt_at == wake_time
    assert repo.get_analysis_job("low", global_low.event_id).next_attempt_at == future
    repo.close()
