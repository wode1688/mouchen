from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

import app.local_stt as local_stt_module
from app.stt_admission import (
    SttAdmissionConfig,
    SttAdmissionController,
    SttAdmissionRejected,
    SttAdmissionUnavailable,
)


def _config(
    *,
    requests: int = 100,
    window: int = 300,
    user_concurrency: int = 1,
    global_concurrency: int = 2,
    lease: int = 60,
) -> SttAdmissionConfig:
    return SttAdmissionConfig(
        requests_per_window=requests,
        rate_window_seconds=window,
        user_concurrency=user_concurrency,
        global_concurrency=global_concurrency,
        lease_seconds=lease,
    )


def _hold_cross_process_lease(
    database_path: str,
    result_queue,
    release_event,
) -> None:
    controller = SttAdmissionController(
        database_path,
        config=_config(global_concurrency=1),
    )
    try:
        lease = controller.acquire("process-owner")
    except BaseException as exc:
        result_queue.put(("error", type(exc).__name__))
        return
    result_queue.put(("acquired", lease.lease_id))
    release_event.wait(10)
    lease.release()


class _TrackingRequest:
    def __init__(self, body: bytes, *, user_id: str | None = "tenant-a") -> None:
        principal = (
            SimpleNamespace(user_id=user_id) if user_id is not None else None
        )
        self.state = SimpleNamespace(auth_principal=principal)
        self.headers: dict[str, str] = {}
        self.body = body
        self.stream_read = False

    async def stream(self):
        self.stream_read = True
        yield self.body


def test_environment_defaults_are_bounded_and_commercial_safe(monkeypatch):
    names = (
        "MOUCHEN_STT_REQUESTS_PER_WINDOW",
        "MOUCHEN_STT_RATE_WINDOW_SECONDS",
        "MOUCHEN_STT_USER_CONCURRENCY",
        "MOUCHEN_STT_GLOBAL_CONCURRENCY",
        "MOUCHEN_STT_LEASE_SECONDS",
        "MOUCHEN_STT_BODY_READ_TIMEOUT_SECONDS",
        "MOUCHEN_LOCAL_STT_TIMEOUT_SECONDS",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    assert SttAdmissionConfig.from_environment() == SttAdmissionConfig(
        requests_per_window=10,
        rate_window_seconds=300,
        user_concurrency=1,
        global_concurrency=2,
        lease_seconds=60,
    )


def test_lease_cannot_expire_before_bounded_upload_and_inference(monkeypatch):
    monkeypatch.setenv("MOUCHEN_STT_LEASE_SECONDS", "60")
    monkeypatch.setenv("MOUCHEN_STT_BODY_READ_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("MOUCHEN_LOCAL_STT_TIMEOUT_SECONDS", "120")

    assert SttAdmissionConfig.from_environment().lease_seconds == 255


def test_rolling_window_survives_release_and_controller_restart(tmp_path: Path):
    database = tmp_path / "stt-rate.db"
    config = _config(requests=2)
    started = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)

    first = SttAdmissionController(database, config=config).acquire(
        "tenant-a", now=started
    )
    assert first.release(now=started)
    second = SttAdmissionController(database, config=config).acquire(
        "tenant-a", now=started + timedelta(seconds=1)
    )
    assert second.release(now=started + timedelta(seconds=1))

    restarted = SttAdmissionController(database, config=config)
    with pytest.raises(SttAdmissionRejected) as rejected:
        restarted.acquire("tenant-a", now=started + timedelta(seconds=2))

    assert rejected.value.reason_code == "stt_rate_limit_exhausted"
    assert rejected.value.retry_after == 298


def test_user_and_global_leases_are_atomic_across_connections(tmp_path: Path):
    database = tmp_path / "stt-concurrency.db"
    config = _config()
    started = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    first_controller = SttAdmissionController(database, config=config)
    second_controller = SttAdmissionController(database, config=config)
    third_controller = SttAdmissionController(database, config=config)

    first = first_controller.acquire("tenant-a", now=started)
    with pytest.raises(SttAdmissionRejected) as same_user:
        second_controller.acquire("tenant-a", now=started + timedelta(seconds=1))
    assert same_user.value.reason_code == "stt_user_concurrency_exhausted"
    assert same_user.value.retry_after == 59

    second = second_controller.acquire("tenant-b", now=started + timedelta(seconds=1))
    with pytest.raises(SttAdmissionRejected) as globally_full:
        third_controller.acquire("tenant-c", now=started + timedelta(seconds=2))
    assert globally_full.value.reason_code == "stt_global_concurrency_exhausted"
    assert globally_full.value.retry_after == 58

    assert not second_controller.release(first.lease_id, "tenant-b", now=started)
    assert first.release(now=started + timedelta(seconds=3))
    third = third_controller.acquire("tenant-c", now=started + timedelta(seconds=3))
    assert second.release(now=started + timedelta(seconds=4))
    assert third.release(now=started + timedelta(seconds=4))


def test_expired_lease_recovers_capacity_after_restart(tmp_path: Path):
    database = tmp_path / "stt-expiry.db"
    config = _config(global_concurrency=1)
    started = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)

    SttAdmissionController(database, config=config).acquire("tenant-a", now=started)
    restarted = SttAdmissionController(database, config=config)
    recovered = restarted.acquire(
        "tenant-b",
        now=started + timedelta(seconds=config.lease_seconds),
    )

    assert recovered.user_id == "tenant-b"
    assert recovered.release(now=started + timedelta(seconds=61))


def test_global_lease_is_visible_to_another_process(tmp_path: Path):
    database = tmp_path / "stt-process.db"
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    release_event = context.Event()
    process = context.Process(
        target=_hold_cross_process_lease,
        args=(str(database), result_queue, release_event),
    )
    process.start()
    try:
        status, _ = result_queue.get(timeout=15)
        assert status == "acquired"
        controller = SttAdmissionController(
            database,
            config=_config(global_concurrency=1),
        )
        with pytest.raises(SttAdmissionRejected) as rejected:
            controller.acquire("other-tenant")
        assert rejected.value.reason_code == "stt_global_concurrency_exhausted"
        assert 1 <= rejected.value.retry_after <= 60
    finally:
        release_event.set()
        process.join(timeout=15)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert process.exitcode == 0


def test_endpoint_rejects_before_reading_body_with_retry_after(monkeypatch):
    request = _TrackingRequest(b"must-not-be-read", user_id="authenticated-user")
    observed: dict[str, str] = {}

    class RejectingAdmission:
        def acquire(self, user_id: str):
            observed["user_id"] = user_id
            raise SttAdmissionRejected("stt_rate_limit_exhausted", 37)

    monkeypatch.setattr(local_stt_module, "stt_admission", RejectingAdmission())
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(
            local_stt_module.transcribe_audio(
                request,
                sample_rate=16_000,
                language="zh",
            )
        )

    assert observed == {"user_id": "authenticated-user"}
    assert request.stream_read is False
    assert rejected.value.status_code == 429
    assert rejected.value.headers == {"Retry-After": "37"}


def test_endpoint_fails_closed_before_body_when_admission_database_is_unavailable(
    monkeypatch,
):
    request = _TrackingRequest(b"must-not-be-read")

    class UnavailableAdmission:
        def acquire(self, user_id: str):
            raise SttAdmissionUnavailable("database unavailable")

    monkeypatch.setattr(local_stt_module, "stt_admission", UnavailableAdmission())
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(
            local_stt_module.transcribe_audio(
                request,
                sample_rate=16_000,
                language="zh",
            )
        )

    assert request.stream_read is False
    assert rejected.value.status_code == 503
    assert rejected.value.headers == {"Retry-After": "1"}


def test_endpoint_always_releases_lease_after_failure(monkeypatch):
    request = _TrackingRequest(b"\x01\x00\x02\x00")
    released = False

    class Lease:
        def release(self):
            nonlocal released
            released = True
            return True

    class Admission:
        def acquire(self, user_id: str):
            assert user_id == "tenant-a"
            return Lease()

    class FailedService:
        async def transcribe(self, pcm: bytes, *, sample_rate: int, language: str):
            raise local_stt_module.LocalSttFailed("inference failed")

    monkeypatch.setattr(local_stt_module, "stt_admission", Admission())
    monkeypatch.setattr(local_stt_module, "get_local_stt_service", lambda: FailedService())

    with pytest.raises(HTTPException) as failed:
        asyncio.run(
            local_stt_module.transcribe_audio(
                request,
                sample_rate=16_000,
                language="zh",
            )
        )

    assert failed.value.status_code == 502
    assert released is True


def test_endpoint_never_persists_audio_in_admission_database(
    monkeypatch,
    tmp_path: Path,
):
    database = tmp_path / "stt-no-audio.db"
    controller = SttAdmissionController(database, config=_config())
    pcm = b"MOUCHEN-RAW-AUDIO-MARKER-" * 4
    request = _TrackingRequest(pcm)

    class SuccessfulService:
        async def transcribe(self, value: bytes, *, sample_rate: int, language: str):
            assert value == pcm
            return local_stt_module.TranscriptResponse(
                transcript="ok",
                language=language,
                duration_ms=1,
            )

    monkeypatch.setattr(local_stt_module, "stt_admission", controller)
    monkeypatch.setattr(
        local_stt_module,
        "get_local_stt_service",
        lambda: SuccessfulService(),
    )

    result = asyncio.run(
        local_stt_module.transcribe_audio(
            request,
            sample_rate=16_000,
            language="zh",
        )
    )

    assert result.transcript == "ok"
    assert pcm not in database.read_bytes()


def test_deployment_exposes_every_durable_stt_admission_setting():
    repository = Path(__file__).resolve().parents[2]
    backend_environment = (repository / "backend" / ".env.example").read_text(
        encoding="utf-8"
    )
    deployment_environment = (repository / "deploy" / "env.example").read_text(
        encoding="utf-8"
    )
    compose = (repository / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    expected = {
        "MOUCHEN_STT_RATE_WINDOW_SECONDS": "300",
        "MOUCHEN_STT_REQUESTS_PER_WINDOW": "10",
        "MOUCHEN_STT_USER_CONCURRENCY": "1",
        "MOUCHEN_STT_GLOBAL_CONCURRENCY": "2",
        "MOUCHEN_STT_LEASE_SECONDS": "60",
        "MOUCHEN_STT_BODY_READ_TIMEOUT_SECONDS": "30",
    }

    for name, default in expected.items():
        assert f"{name}={default}" in backend_environment
        assert f"{name}={default}" in deployment_environment
        assert f"${{{name}:-{default}}}" in compose


def test_repository_restart_keeps_stt_usable_and_retired_account_is_rejected(tmp_path):
    from app.storage import Repository, _user_id_hash

    database = tmp_path / "stt-repository-restart.db"
    repository = Repository(database)
    repository.ensure_user("active-tenant")
    controller = SttAdmissionController(database, config=_config())
    first = controller.acquire("active-tenant")
    assert first.release()
    repository.close()

    restarted = Repository(database)
    second = controller.acquire("active-tenant")
    assert second.release()
    restarted._connection.execute(
        "INSERT INTO retired_user_ids(user_id_hash,retired_at) VALUES(?,?)",
        (_user_id_hash("retired-tenant"), datetime.now(timezone.utc).isoformat()),
    )
    restarted._connection.commit()
    restarted.close()

    with pytest.raises(SttAdmissionRejected) as rejected:
        SttAdmissionController(database, config=_config()).acquire("retired-tenant")
    assert rejected.value.reason_code == "account_retired"
