from __future__ import annotations

from io import BytesIO
from pathlib import Path
import threading
import time
import wave

import pytest
from fastapi.testclient import TestClient

import app.paraformer_server as paraformer
from app.local_stt import _build_multipart_request


class FakeResult:
    text = ""


class FakeStream:
    def __init__(self) -> None:
        self.result = FakeResult()
        self.sample_rate: int | None = None
        self.samples = None

    def accept_waveform(self, sample_rate: int, samples) -> None:
        self.sample_rate = sample_rate
        self.samples = samples


class FakeRecognizer:
    def __init__(self, text: str = "广州市房地产中介协会分析") -> None:
        self.text = text
        self.streams: list[FakeStream] = []

    def create_stream(self) -> FakeStream:
        stream = FakeStream()
        self.streams.append(stream)
        return stream

    def decode_stream(self, stream: FakeStream) -> None:
        stream.result.text = self.text


def _wav_bytes(
    pcm: bytes,
    *,
    sample_rate: int = 16_000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return output.getvalue()


def _service(
    tmp_path: Path,
    recognizer: FakeRecognizer | None = None,
    *,
    lock_wait_seconds: float = 0.05,
    converter=None,
) -> paraformer.ParaformerService:
    model = tmp_path / "model.int8.onnx"
    tokens = tmp_path / "tokens.txt"
    model.write_bytes(b"model")
    tokens.write_text("tokens", encoding="utf-8")
    runtime = paraformer._Runtime(
        recognizer=recognizer or FakeRecognizer(),
        numpy=object(),
    )
    return paraformer.ParaformerService(
        paraformer.ParaformerConfig(
            model_path=model,
            tokens_path=tokens,
            threads=4,
            lock_wait_seconds=lock_wait_seconds,
        ),
        runtime_factory=lambda _config: runtime,
        sample_converter=converter or (lambda _pcm, _rate, _numpy: "samples"),
    )


def test_health_and_inference_use_fake_recognizer_without_native_dependencies(
    tmp_path: Path,
):
    recognizer = FakeRecognizer()
    conversions: list[tuple[bytes, int, object]] = []

    def convert(pcm: bytes, source_rate: int, numpy):
        conversions.append((pcm, source_rate, numpy))
        return "resampled-float32"

    service = _service(tmp_path, recognizer, converter=convert)
    paraformer.app.dependency_overrides[paraformer.get_paraformer_service] = lambda: service
    try:
        client = TestClient(paraformer.app)
        health = client.get("/health")
        body, content_type = _build_multipart_request(
            b"\x01\x00" * 8_000,
            8_000,
            "zh",
        )
        response = client.post(
            "/inference",
            headers={"Content-Type": content_type},
            content=body,
        )
    finally:
        paraformer.app.dependency_overrides.clear()

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert response.status_code == 200
    assert response.json() == {"text": "广州市房地产中介协会分析"}
    assert conversions == [(b"\x01\x00" * 8_000, 8_000, service._runtime.numpy)]
    assert recognizer.streams[0].sample_rate == 16_000
    assert recognizer.streams[0].samples == "resampled-float32"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "model.int8.onnx",
        "tokens.txt",
    ]


@pytest.mark.parametrize(
    "wav_data, expected_detail",
    [
        (_wav_bytes(b"\x00\x00" * 32, channels=2), "WAV must be mono PCM16"),
        (_wav_bytes(b"\x00" * 32, sample_width=1), "WAV must be mono PCM16"),
        (b"not-a-wav", "audio file is not a valid WAV"),
    ],
)
def test_inference_rejects_invalid_wav(
    tmp_path: Path,
    wav_data: bytes,
    expected_detail: str,
):
    service = _service(tmp_path)
    paraformer.app.dependency_overrides[paraformer.get_paraformer_service] = lambda: service
    try:
        response = TestClient(paraformer.app).post(
            "/inference",
            data={"language": "zh"},
            files={"file": ("audio.wav", wav_data, "audio/wav")},
        )
    finally:
        paraformer.app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["detail"] == expected_detail


def test_inference_rejects_oversized_stream_before_parsing(tmp_path: Path):
    service = _service(tmp_path)
    paraformer.app.dependency_overrides[paraformer.get_paraformer_service] = lambda: service

    def oversized_body():
        block = b"x" * (256 * 1_024)
        for _ in range(21):
            yield block

    try:
        response = TestClient(paraformer.app).post(
            "/inference",
            headers={"Content-Type": "multipart/form-data; boundary=test"},
            content=oversized_body(),
        )
    finally:
        paraformer.app.dependency_overrides.clear()

    assert response.status_code == 413
    assert response.json()["detail"] == "request is too large"


def test_single_recognizer_returns_busy_after_short_wait(tmp_path: Path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingRecognizer(FakeRecognizer):
        def decode_stream(self, stream: FakeStream) -> None:
            entered.set()
            assert release.wait(timeout=2)
            super().decode_stream(stream)

    service = _service(
        tmp_path,
        BlockingRecognizer(),
        lock_wait_seconds=0.03,
    )
    wav_data = _wav_bytes(b"\x01\x00" * 160)
    first_errors: list[Exception] = []

    def first_request() -> None:
        try:
            service.transcribe_wav(wav_data, language="zh")
        except Exception as exc:  # pragma: no cover - asserted below
            first_errors.append(exc)

    worker = threading.Thread(target=first_request)
    worker.start()
    assert entered.wait(timeout=1)
    started = time.monotonic()
    with pytest.raises(paraformer.ParaformerBusy):
        service.transcribe_wav(wav_data, language="zh")
    elapsed = time.monotonic() - started
    release.set()
    worker.join(timeout=2)

    assert elapsed < 0.25
    assert not first_errors
    assert not worker.is_alive()


def test_runtime_errors_do_not_expose_internal_details(tmp_path: Path):
    model = tmp_path / "secret-model-name.onnx"
    tokens = tmp_path / "tokens.txt"
    model.write_bytes(b"model")
    tokens.write_text("tokens", encoding="utf-8")
    service = paraformer.ParaformerService(
        paraformer.ParaformerConfig(model, tokens),
        runtime_factory=lambda _config: (_ for _ in ()).throw(
            ImportError("sherpa_onnx missing at C:/private/path")
        ),
    )
    paraformer.app.dependency_overrides[paraformer.get_paraformer_service] = lambda: service
    try:
        response = TestClient(paraformer.app).get("/health")
    finally:
        paraformer.app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "STT runtime unavailable"}
    assert "private" not in response.text.casefold()


def test_conversion_errors_are_returned_as_safe_inference_failure(tmp_path: Path):
    def failed_conversion(_pcm, _sample_rate, _numpy):
        raise RuntimeError("private resampler detail")

    service = _service(tmp_path, converter=failed_conversion)
    paraformer.app.dependency_overrides[paraformer.get_paraformer_service] = lambda: service
    try:
        response = TestClient(paraformer.app).post(
            "/inference",
            data={"language": "zh"},
            files={
                "file": (
                    "audio.wav",
                    _wav_bytes(b"\x01\x00" * 160),
                    "audio/wav",
                )
            },
        )
    finally:
        paraformer.app.dependency_overrides.clear()

    assert response.status_code == 502
    assert response.json() == {"detail": "STT inference failed"}
    assert "private" not in response.text.casefold()


def test_inference_reports_missing_runtime_as_safe_unavailable(tmp_path: Path):
    model = tmp_path / "model.onnx"
    tokens = tmp_path / "tokens.txt"
    model.write_bytes(b"model")
    tokens.write_text("tokens", encoding="utf-8")
    service = paraformer.ParaformerService(
        paraformer.ParaformerConfig(model, tokens),
        runtime_factory=lambda _config: (_ for _ in ()).throw(
            ImportError("native library path is private")
        ),
    )
    paraformer.app.dependency_overrides[paraformer.get_paraformer_service] = lambda: service
    try:
        response = TestClient(paraformer.app).post(
            "/inference",
            data={"language": "zh"},
            files={
                "file": (
                    "audio.wav",
                    _wav_bytes(b"\x01\x00" * 160),
                    "audio/wav",
                )
            },
        )
    finally:
        paraformer.app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "STT runtime unavailable"}
    assert "private" not in response.text.casefold()


def test_environment_bounds_thread_count(monkeypatch):
    monkeypatch.setenv("MOUCHEN_PARAFORMER_MODEL", "/models/model.onnx")
    monkeypatch.setenv("MOUCHEN_PARAFORMER_TOKENS", "/models/tokens.txt")
    monkeypatch.setenv("MOUCHEN_PARAFORMER_THREADS", "999")
    monkeypatch.setenv("MOUCHEN_PARAFORMER_QUEUE_WAIT_SECONDS", "99")

    config = paraformer.ParaformerConfig.from_environment()

    assert config.model_path == Path("/models/model.onnx")
    assert config.tokens_path == Path("/models/tokens.txt")
    assert config.threads == 16
    assert config.lock_wait_seconds == 2
