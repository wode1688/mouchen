from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient

import app.local_stt as local_stt_module
from app.local_stt import (
    LocalSttConfig,
    LocalSttService,
    LocalSttUnavailable,
    _parse_ffmpeg_srt,
)
from app.main import app


@pytest.fixture
def isolated_stt_api(monkeypatch, tmp_path: Path):
    """Run HTTP STT checks against a clean, explicitly bound legacy tenant."""

    import app.main as main

    previous_repo = main.repo
    previous_service = main.service
    database_path = tmp_path / "stt-api.db"
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(database_path))
    monkeypatch.setenv("MOUCHEN_SINGLE_USER_ID", "owner-1")
    monkeypatch.setenv("MOUCHEN_LEGACY_AUTH_ENABLED", "true")
    main.repo = main.Repository(database_path)
    main.service = main.ProactiveService(main.repo)
    try:
        yield TestClient(main.app)
    finally:
        main.repo.close()
        main.repo = previous_repo
        main.service = previous_service


def test_ffmpeg_whisper_receives_pcm_over_stdin_without_plain_audio_file(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setattr(local_stt_module, "_has_speech_frames", lambda *_: True)
    model = tmp_path / "model:small.bin"
    model.write_bytes(b"model")
    calls: list[tuple[list[str], dict]] = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="0\n00:00:00.000 --> 00:00:01.000\n 你好 \n\n".encode(),
            stderr=b"",
        )

    service = LocalSttService(
        LocalSttConfig(
            enabled=True,
            ffmpeg_path=Path("ffmpeg"),
            model_path=model,
            timeout_seconds=30,
        ),
        runner=runner,
    )
    pcm = (10_000).to_bytes(2, "little", signed=True) * 16_000
    result = asyncio.run(service.transcribe(pcm, sample_rate=16_000, language="zh"))

    assert result.transcript == "你好"
    assert result.duration_ms == 1_000
    assert result.raw_audio_persisted is False
    assert calls[0][1]["input"] == pcm
    assert "pipe:0" in calls[0][0]
    assert not any(str(item).lower().endswith(".wav") for item in calls[0][0])
    filter_spec = calls[0][0][calls[0][0].index("-af") + 1]
    assert "destination='-'" in filter_spec
    assert "format=srt" in filter_spec
    assert "model:small.bin" not in filter_spec
    assert "model\\:small.bin" in filter_spec
    assert calls[0][0][calls[0][0].index("-filter_threads") + 1] == "2"
    assert calls[0][1]["env"]["OMP_NUM_THREADS"] == "2"


def test_local_stt_requires_explicit_enable_and_model(tmp_path: Path):
    service = LocalSttService(
        LocalSttConfig(
            enabled=False,
            ffmpeg_path=Path("ffmpeg"),
            model_path=tmp_path / "missing.bin",
        )
    )
    with pytest.raises(LocalSttUnavailable):
        asyncio.run(service.transcribe(b"\x00\x00", sample_rate=16_000, language="zh"))


def test_parser_joins_srt_segments_without_interpreting_transcript_as_json():
    output = (
        b'not-srt\n\n0\n00:00:00.000 --> 00:00:00.500\nalpha "quoted"\n\n'
        b"0\n00:00:00.500 --> 00:00:01.000\n beta \\\\ path \n\n"
    )
    assert _parse_ffmpeg_srt(output) == 'alpha "quoted" beta \\\\ path'


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("1", 5), ("15", 15), ("999", 120), ("invalid", 15)],
)
def test_environment_bounds_resident_stt_timeout(monkeypatch, configured, expected):
    monkeypatch.setenv("MOUCHEN_LOCAL_STT_TIMEOUT_SECONDS", configured)

    assert LocalSttConfig.from_environment().timeout_seconds == expected


def test_local_stt_bounds_filter_threads(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MOUCHEN_LOCAL_STT_THREADS", "99")
    monkeypatch.setattr(local_stt_module, "_has_speech_frames", lambda *_: True)
    model = tmp_path / "model.bin"
    model.write_bytes(b"model")
    captured: dict[str, object] = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, b"", b"")

    service = LocalSttService(LocalSttConfig(True, Path("ffmpeg"), model), runner=runner)
    speech_pcm = (10_000).to_bytes(2, "little", signed=True) * 1_600
    asyncio.run(service.transcribe(speech_pcm, sample_rate=16_000, language="zh"))

    command = captured["command"]
    assert isinstance(command, list)
    assert command[command.index("-filter_threads") + 1] == "8"
    assert captured["env"]["OMP_NUM_THREADS"] == "8"


def test_resident_whisper_server_receives_memory_only_wav(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(local_stt_module, "_has_speech_frames", lambda *_: True)
    model = tmp_path / "model.bin"
    model.write_bytes(b"model")
    captured: dict[str, object] = {}

    class Response:
        status = 200

        def read(self, limit):
            assert limit == 64 * 1_024 + 1
            return json.dumps({"text": " 你好，世界 "}, ensure_ascii=False).encode("utf-8")

    class Connection:
        def __init__(self, host, port, *, timeout):
            captured["host"] = host
            captured["port"] = port
            captured["timeout"] = timeout

        def request(self, method, path, *, body, headers):
            captured["method"] = method
            captured["path"] = path
            captured["headers"] = headers
            captured["body"] = body

        def getresponse(self):
            return Response()

        def close(self):
            captured["closed"] = True

    service = LocalSttService(
        LocalSttConfig(
            enabled=True,
            ffmpeg_path=Path("ffmpeg"),
            model_path=model,
            timeout_seconds=31,
            server_url="http://stt:8080/inference",
            server_engine="local-paraformer-sherpa-onnx",
        ),
        runner=lambda *_args, **_kwargs: pytest.fail("FFmpeg fallback must not run"),
        connection_factory=Connection,
    )
    pcm = (10_000).to_bytes(2, "little", signed=True) * 16_000
    result = asyncio.run(service.transcribe(pcm, sample_rate=16_000, language="zh-CN"))

    assert result.transcript == "你好，世界"
    assert result.language == "zh"
    assert result.engine == "local-paraformer-sherpa-onnx"
    assert captured["host"] == "stt"
    assert captured["port"] == 8080
    assert captured["method"] == "POST"
    assert captured["path"] == "/inference"
    assert captured["timeout"] == 31
    assert captured["closed"] is True
    body = captured["body"]
    assert isinstance(body, bytes)
    assert b'filename="audio.wav"' in body
    assert b"RIFF" in body
    assert pcm in body
    assert b'name="language"\r\n\r\nzh' in body
    assert b'name="no_context"\r\n\r\ntrue' in body
    assert b'name="response_format"\r\n\r\njson' in body


@pytest.mark.parametrize(
    "url",
    [
        "https://stt:8080/inference",
        "http://example.com:8080/inference",
        "http://stt:8080/load",
        "http://user:pass@stt:8080/inference",
        "http://stt:8080/inference?next=http://example.com",
    ],
)
def test_resident_whisper_server_url_fails_closed(url: str, tmp_path: Path):
    model = tmp_path / "model.bin"
    model.write_bytes(b"model")
    service = LocalSttService(
        LocalSttConfig(True, Path("ffmpeg"), model, server_url=url),
    )
    with pytest.raises(LocalSttUnavailable):
        service.validate_configuration()


def test_resident_whisper_server_failure_never_falls_back(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(local_stt_module, "_has_speech_frames", lambda *_: True)
    model = tmp_path / "model.bin"
    model.write_bytes(b"model")

    class FailedConnection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            raise OSError("unreachable")

        def close(self):
            pass

    service = LocalSttService(
        LocalSttConfig(
            True,
            Path("ffmpeg"),
            model,
            server_url="http://stt:8080/inference",
        ),
        runner=lambda *_args, **_kwargs: pytest.fail("unsafe model-loading fallback ran"),
        connection_factory=FailedConnection,
    )
    pcm = (10_000).to_bytes(2, "little", signed=True) * 1_600
    with pytest.raises(local_stt_module.LocalSttFailed):
        asyncio.run(service.transcribe(pcm, sample_rate=16_000, language="zh"))


def test_resident_whisper_server_does_not_follow_redirects(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(local_stt_module, "_has_speech_frames", lambda *_: True)
    model = tmp_path / "model.bin"
    model.write_bytes(b"model")
    requests = 0

    class RedirectResponse:
        status = 302

    class RedirectConnection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            nonlocal requests
            requests += 1

        def getresponse(self):
            return RedirectResponse()

        def close(self):
            pass

    service = LocalSttService(
        LocalSttConfig(
            True,
            Path("ffmpeg"),
            model,
            server_url="http://stt:8080/inference",
        ),
        connection_factory=RedirectConnection,
    )
    pcm = (10_000).to_bytes(2, "little", signed=True) * 1_600
    with pytest.raises(local_stt_module.LocalSttFailed):
        asyncio.run(service.transcribe(pcm, sample_rate=16_000, language="zh"))
    assert requests == 1


def test_readiness_probes_only_the_isolated_resident_server(monkeypatch, tmp_path: Path):
    model = tmp_path / "model.bin"
    model.write_bytes(b"model")
    monkeypatch.setenv("MOUCHEN_LOCAL_STT_ENABLED", "true")
    monkeypatch.setenv("MOUCHEN_LOCAL_STT_MODEL", str(model))
    monkeypatch.setenv("MOUCHEN_LOCAL_STT_SERVER_URL", "http://stt:8080/inference")
    captured: dict[str, object] = {}

    class HealthResponse:
        status = 200

        def read(self, limit):
            assert limit == 1_025
            return b'{"status":"ok"}'

    class HealthConnection:
        def __init__(self, host, port, *, timeout):
            captured.update(host=host, port=port, timeout=timeout)

        def request(self, method, path):
            captured.update(method=method, path=path)

        def getresponse(self):
            return HealthResponse()

        def close(self):
            captured["closed"] = True

    assert local_stt_module.local_stt_runtime_ready(
        timeout_seconds=0.25,
        connection_factory=HealthConnection,
    )
    assert captured == {
        "host": "stt",
        "port": 8080,
        "timeout": 0.25,
        "method": "GET",
        "path": "/health",
        "closed": True,
    }


def test_digital_silence_does_not_invoke_whisper(tmp_path: Path):
    model = tmp_path / "model.bin"
    model.write_bytes(b"model")

    def unexpected_runner(*args, **kwargs):
        raise AssertionError("Whisper must not run for digital silence")

    service = LocalSttService(
        LocalSttConfig(True, Path("ffmpeg"), model),
        runner=unexpected_runner,
    )
    result = asyncio.run(
        service.transcribe(b"\x00\x00" * 16_000, sample_rate=16_000, language="zh")
    )

    assert result.transcript == ""
    assert result.duration_ms == 1_000


def test_vad_requires_multiple_speech_frames(monkeypatch):
    class FakeVad:
        def __init__(self, mode):
            assert mode == 3
            self.calls = 0

        def is_speech(self, frame, sample_rate):
            assert len(frame) == 640
            assert sample_rate == 16_000
            self.calls += 1
            return self.calls in {2, 3, 4, 6, 7}

    monkeypatch.setattr(local_stt_module.webrtcvad, "Vad", FakeVad)
    pcm = (10_000).to_bytes(2, "little", signed=True) * (16_000 // 5)
    assert local_stt_module._has_speech_frames(pcm, 16_000)


def test_vad_rejects_isolated_noise_frames(monkeypatch):
    class FakeVad:
        def __init__(self, mode):
            pass

        def is_speech(self, frame, sample_rate):
            FakeVad.calls += 1
            return FakeVad.calls in {1, 3, 5, 7, 9}

    FakeVad.calls = 0
    monkeypatch.setattr(local_stt_module.webrtcvad, "Vad", FakeVad)
    pcm = (10_000).to_bytes(2, "little", signed=True) * (16_000 // 5)
    assert not local_stt_module._has_speech_frames(pcm, 16_000)


def test_audio_bounds_are_enforced(tmp_path: Path):
    model = tmp_path / "model.bin"
    model.write_bytes(b"model")
    service = LocalSttService(
        LocalSttConfig(True, Path("ffmpeg"), model),
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, b"", b""),
    )
    for pcm in (b"", b"\x00", b"\x00\x00" * (16_000 * 46)):
        with pytest.raises(ValueError):
            asyncio.run(service.transcribe(pcm, sample_rate=16_000, language="zh"))


def test_audio_endpoint_requires_bearer_and_explicit_local_engine(
    monkeypatch, isolated_stt_api
):
    monkeypatch.setenv("MOUCHEN_API_TOKEN", "test-stt-secret")
    monkeypatch.setenv("MOUCHEN_LOCAL_STT_ENABLED", "false")
    client = isolated_stt_api

    unauthenticated = client.post(
        "/v1/audio/transcribe?sample_rate=16000&language=zh",
        content=b"\x00\x00",
    )
    disabled = client.post(
        "/v1/audio/transcribe?sample_rate=16000&language=zh",
        headers={
            "Authorization": "Bearer test-stt-secret",
            "X-User-Id": "owner-1",
        },
        content=b"\x00\x00",
    )

    assert unauthenticated.status_code == 401
    assert disabled.status_code == 503


def test_audio_endpoint_returns_structured_local_transcript(
    monkeypatch, isolated_stt_api
):
    class FakeLocalService:
        async def transcribe(self, pcm: bytes, *, sample_rate: int, language: str):
            assert pcm == b"\x01\x00\x02\x00"
            assert sample_rate == 16_000
            assert language == "zh"
            return local_stt_module.TranscriptResponse(
                transcript="支付失败需要处理",
                language="zh",
                duration_ms=1,
            )

    monkeypatch.setenv("MOUCHEN_API_TOKEN", "test-stt-secret")
    monkeypatch.setattr(local_stt_module, "get_local_stt_service", lambda: FakeLocalService())
    response = isolated_stt_api.post(
        "/v1/audio/transcribe?sample_rate=16000&language=zh",
        headers={
            "Authorization": "Bearer test-stt-secret",
            "X-User-Id": "owner-1",
        },
        content=b"\x01\x00\x02\x00",
    )

    assert response.status_code == 200
    assert response.json() == {
        "transcript": "支付失败需要处理",
        "language": "zh",
        "duration_ms": 1,
        "engine": "local-ffmpeg-whisper.cpp",
        "raw_audio_persisted": False,
    }


def test_audio_endpoint_rejects_streamed_body_before_unbounded_buffering(
    monkeypatch, isolated_stt_api
):
    class MustNotRun:
        async def transcribe(self, *args, **kwargs):
            raise AssertionError("oversized audio must be rejected before transcription")

    monkeypatch.setenv("MOUCHEN_API_TOKEN", "test-stt-secret")
    monkeypatch.setattr(local_stt_module, "get_local_stt_service", lambda: MustNotRun())

    def chunks():
        block = b"\x00" * 64_000
        for _ in range(46):
            yield block

    response = isolated_stt_api.post(
        "/v1/audio/transcribe?sample_rate=16000&language=zh",
        headers={
            "Authorization": "Bearer test-stt-secret",
            "X-User-Id": "owner-1",
        },
        content=chunks(),
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "audio segment exceeds 45 seconds"
