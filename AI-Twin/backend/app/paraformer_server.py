from __future__ import annotations

import asyncio
from dataclasses import dataclass
from email.message import Message
from email.parser import BytesHeaderParser
from email import policy
from io import BytesIO
import importlib
import os
from pathlib import Path
import threading
from typing import Annotated, Any, Callable
import wave

from fastapi import Depends, FastAPI, HTTPException, Request


app = FastAPI(title="My AI Twin Paraformer STT", docs_url=None, redoc_url=None)

_TARGET_SAMPLE_RATE = 16_000
_MIN_SAMPLE_RATE = 8_000
_MAX_SAMPLE_RATE = 48_000
_MAX_AUDIO_SECONDS = 45
_MAX_REQUEST_BYTES = 5 * 1_024 * 1_024
_MAX_MULTIPART_PARTS = 8
_MAX_MULTIPART_HEADER_BYTES = 2_048
_MAX_FIELD_BYTES = 128
_MAX_TRANSCRIPT_CHARACTERS = 12_000
_DEFAULT_LOCK_WAIT_SECONDS = 0.25
_ALLOWED_LANGUAGES = {"auto", "cmn", "zh", "zh-cn", "zh-hans"}


class ParaformerUnavailable(RuntimeError):
    pass


class ParaformerBusy(RuntimeError):
    pass


class ParaformerFailed(RuntimeError):
    pass


class InvalidAudio(ValueError):
    pass


@dataclass(frozen=True)
class ParaformerConfig:
    model_path: Path
    tokens_path: Path
    threads: int = 4
    lock_wait_seconds: float = _DEFAULT_LOCK_WAIT_SECONDS

    @classmethod
    def from_environment(cls) -> "ParaformerConfig":
        return cls(
            model_path=Path(os.getenv("MOUCHEN_PARAFORMER_MODEL", "").strip()),
            tokens_path=Path(os.getenv("MOUCHEN_PARAFORMER_TOKENS", "").strip()),
            threads=_bounded_int(
                os.getenv("MOUCHEN_PARAFORMER_THREADS", "4"),
                default=4,
                minimum=1,
                maximum=16,
            ),
            lock_wait_seconds=_bounded_float(
                os.getenv(
                    "MOUCHEN_PARAFORMER_QUEUE_WAIT_SECONDS",
                    str(_DEFAULT_LOCK_WAIT_SECONDS),
                ),
                default=_DEFAULT_LOCK_WAIT_SECONDS,
                minimum=0,
                maximum=2,
            ),
        )

    def validate(self) -> None:
        if not self.model_path.is_file() or not self.tokens_path.is_file():
            raise ParaformerUnavailable("Paraformer model assets are unavailable")
        if not 1 <= self.threads <= 16:
            raise ParaformerUnavailable("Paraformer thread configuration is invalid")
        if not 0 <= self.lock_wait_seconds <= 2:
            raise ParaformerUnavailable("Paraformer wait configuration is invalid")


@dataclass(frozen=True)
class _Runtime:
    recognizer: Any
    numpy: Any


RuntimeFactory = Callable[[ParaformerConfig], _Runtime]
SampleConverter = Callable[[bytes, int, Any], Any]


class ParaformerService:
    """One lazy-loaded recognizer with bounded, serialized inference."""

    def __init__(
        self,
        config: ParaformerConfig,
        *,
        runtime_factory: RuntimeFactory | None = None,
        sample_converter: SampleConverter | None = None,
    ) -> None:
        self.config = config
        self._runtime_factory = runtime_factory or _load_runtime
        self._sample_converter = sample_converter or _pcm16_to_float32
        self._runtime: _Runtime | None = None
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()

    def ensure_ready(self) -> None:
        if self._runtime is not None:
            return
        acquired = self._inference_lock.acquire(
            timeout=self.config.lock_wait_seconds,
        )
        if not acquired:
            raise ParaformerBusy("Paraformer is loading")
        try:
            self._get_runtime()
        finally:
            self._inference_lock.release()

    def transcribe_wav(self, wav_bytes: bytes, *, language: str) -> str:
        normalized_language = language.strip().casefold()
        if normalized_language not in _ALLOWED_LANGUAGES:
            raise InvalidAudio("only Mandarin recognition is supported")

        pcm, source_rate = _decode_pcm16_mono_wav(wav_bytes)
        acquired = self._inference_lock.acquire(
            timeout=self.config.lock_wait_seconds,
        )
        if not acquired:
            raise ParaformerBusy("Paraformer is processing another segment")
        try:
            runtime = self._get_runtime()
            samples = self._sample_converter(pcm, source_rate, runtime.numpy)
            stream = runtime.recognizer.create_stream()
            stream.accept_waveform(_TARGET_SAMPLE_RATE, samples)
            runtime.recognizer.decode_stream(stream)
            text = getattr(getattr(stream, "result", None), "text", None)
            if not isinstance(text, str) or "\x00" in text:
                raise ParaformerFailed("Paraformer returned an invalid transcript")
            return text.strip()[:_MAX_TRANSCRIPT_CHARACTERS]
        except ParaformerUnavailable:
            raise
        except ParaformerFailed:
            raise
        except Exception as exc:
            raise ParaformerFailed("Paraformer inference failed") from exc
        finally:
            self._inference_lock.release()

    def _get_runtime(self) -> _Runtime:
        if self._runtime is not None:
            return self._runtime
        with self._load_lock:
            if self._runtime is not None:
                return self._runtime
            self.config.validate()
            try:
                runtime = self._runtime_factory(self.config)
            except ParaformerUnavailable:
                raise
            except Exception as exc:
                raise ParaformerUnavailable("Paraformer runtime is unavailable") from exc
            if (
                not isinstance(runtime, _Runtime)
                or runtime.recognizer is None
                or runtime.numpy is None
            ):
                raise ParaformerUnavailable("Paraformer runtime is unavailable")
            self._runtime = runtime
            return runtime


def _load_runtime(config: ParaformerConfig) -> _Runtime:
    """Import native dependencies only when readiness or inference needs them."""

    try:
        numpy = importlib.import_module("numpy")
        sherpa_onnx = importlib.import_module("sherpa_onnx")
    except ImportError as exc:
        raise ParaformerUnavailable("Paraformer runtime is unavailable") from exc

    try:
        recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=str(config.model_path),
            tokens=str(config.tokens_path),
            num_threads=config.threads,
            sample_rate=_TARGET_SAMPLE_RATE,
            feature_dim=80,
            decoding_method="greedy_search",
            debug=False,
        )
    except Exception as exc:
        raise ParaformerUnavailable("Paraformer model could not be loaded") from exc
    return _Runtime(recognizer=recognizer, numpy=numpy)


def _pcm16_to_float32(pcm: bytes, source_rate: int, numpy: Any) -> Any:
    source = numpy.frombuffer(pcm, dtype="<i2").astype(numpy.float32)
    source *= numpy.float32(1.0 / 32_768.0)
    if source_rate == _TARGET_SAMPLE_RATE:
        return numpy.ascontiguousarray(source, dtype=numpy.float32)

    target_count = max(
        1,
        int(round(int(source.size) * _TARGET_SAMPLE_RATE / source_rate)),
    )
    source_positions = numpy.arange(int(source.size), dtype=numpy.float64)
    target_positions = (
        numpy.arange(target_count, dtype=numpy.float64)
        * (source_rate / _TARGET_SAMPLE_RATE)
    )
    target_positions = numpy.minimum(target_positions, int(source.size) - 1)
    resampled = numpy.interp(target_positions, source_positions, source)
    return numpy.ascontiguousarray(resampled, dtype=numpy.float32)


_service: ParaformerService | None = None
_service_signature: tuple[str, ...] | None = None


def get_paraformer_service() -> ParaformerService:
    global _service, _service_signature
    config = ParaformerConfig.from_environment()
    signature = (
        str(config.model_path),
        str(config.tokens_path),
        str(config.threads),
        str(config.lock_wait_seconds),
    )
    if _service is None or signature != _service_signature:
        _service = ParaformerService(config)
        _service_signature = signature
    return _service


@app.get("/health")
async def health(
    service: Annotated[ParaformerService, Depends(get_paraformer_service)],
) -> dict[str, str]:
    try:
        await asyncio.to_thread(service.ensure_ready)
    except (ParaformerUnavailable, ParaformerBusy) as exc:
        raise HTTPException(503, "STT runtime unavailable") from exc
    return {"status": "ok"}


@app.post("/inference")
async def inference(
    request: Request,
    service: Annotated[ParaformerService, Depends(get_paraformer_service)],
) -> dict[str, str]:
    content_length = request.headers.get("content-length", "").strip()
    if content_length.isdigit() and int(content_length) > _MAX_REQUEST_BYTES:
        raise HTTPException(413, "request is too large")

    request_buffer = bytearray()
    try:
        async for chunk in request.stream():
            if len(request_buffer) + len(chunk) > _MAX_REQUEST_BYTES:
                raise HTTPException(413, "request is too large")
            request_buffer.extend(chunk)
        request_body = bytes(request_buffer)
    finally:
        request_buffer.clear()

    try:
        fields, wav_bytes = _parse_multipart(
            request.headers.get("content-type", ""),
            request_body,
        )
        language = fields.get("language", "zh")
        if fields.get("response_format", "json").casefold() != "json":
            raise InvalidAudio("response_format must be json")
        if fields.get("no_context", "true").casefold() not in {"1", "true", "yes"}:
            raise InvalidAudio("no_context must be true")
        text = await asyncio.to_thread(
            service.transcribe_wav,
            wav_bytes,
            language=language,
        )
    except InvalidAudio as exc:
        raise HTTPException(422, str(exc)) from exc
    except ParaformerUnavailable as exc:
        raise HTTPException(503, "STT runtime unavailable") from exc
    except ParaformerBusy as exc:
        raise HTTPException(
            429,
            "STT is busy",
            headers={"Retry-After": "1"},
        ) from exc
    except ParaformerFailed as exc:
        raise HTTPException(502, "STT inference failed") from exc
    finally:
        # Keep no aggregate request-body reference beyond this request.
        request_body = b""

    return {"text": text}


def _parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], bytes]:
    try:
        header = Message()
        header["content-type"] = content_type
    except (TypeError, ValueError) as exc:
        raise InvalidAudio("multipart content type is invalid") from exc
    if header.get_content_type().casefold() != "multipart/form-data":
        raise InvalidAudio("multipart/form-data is required")
    boundary_value = header.get_boundary()
    if not boundary_value:
        raise InvalidAudio("multipart boundary is required")
    try:
        boundary = boundary_value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise InvalidAudio("multipart boundary is invalid") from exc
    if (
        not 1 <= len(boundary) <= 70
        or any(byte < 33 or byte > 126 for byte in boundary)
    ):
        raise InvalidAudio("multipart boundary is invalid")

    marker = b"--" + boundary
    sections = body.split(marker)
    if (
        len(sections) < 3
        or len(sections) > _MAX_MULTIPART_PARTS + 2
        or sections[0] != b""
        or sections[-1] not in {b"--", b"--\r\n"}
    ):
        raise InvalidAudio("multipart body is invalid")

    fields: dict[str, str] = {}
    wav_bytes: bytes | None = None
    allowed_names = {"file", "language", "response_format", "no_context"}
    for section in sections[1:-1]:
        if not section.startswith(b"\r\n") or not section.endswith(b"\r\n"):
            raise InvalidAudio("multipart body is invalid")
        part = section[2:-2]
        header_bytes, separator, value = part.partition(b"\r\n\r\n")
        if not separator or len(header_bytes) > _MAX_MULTIPART_HEADER_BYTES:
            raise InvalidAudio("multipart part is invalid")
        try:
            part_headers = BytesHeaderParser(policy=policy.default).parsebytes(
                header_bytes + b"\r\n\r\n"
            )
        except Exception as exc:
            raise InvalidAudio("multipart part is invalid") from exc
        if part_headers.defects:
            raise InvalidAudio("multipart part is invalid")
        if part_headers.get_content_disposition() != "form-data":
            raise InvalidAudio("multipart part is invalid")
        name = part_headers.get_param("name", header="content-disposition")
        if not isinstance(name, str) or name not in allowed_names:
            raise InvalidAudio("multipart field is unsupported")
        if name == "file":
            if wav_bytes is not None:
                raise InvalidAudio("multiple audio files are not supported")
            filename = part_headers.get_filename()
            media_type = part_headers.get_content_type().casefold()
            if not filename or media_type not in {"audio/wav", "audio/x-wav"}:
                raise InvalidAudio("a WAV audio file is required")
            wav_bytes = bytes(value)
            continue
        if name in fields:
            raise InvalidAudio("duplicate multipart field")
        if len(value) > _MAX_FIELD_BYTES:
            raise InvalidAudio("multipart field is too large")
        try:
            fields[name] = value.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise InvalidAudio("multipart field is invalid") from exc

    if wav_bytes is None:
        raise InvalidAudio("a WAV audio file is required")
    return fields, wav_bytes


def _decode_pcm16_mono_wav(wav_bytes: bytes) -> tuple[bytes, int]:
    if not wav_bytes:
        raise InvalidAudio("audio file is empty")
    try:
        with wave.open(BytesIO(wav_bytes), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            source_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            compression = wav_file.getcomptype()
            if channels != 1 or sample_width != 2 or compression != "NONE":
                raise InvalidAudio("WAV must be mono PCM16")
            if not _MIN_SAMPLE_RATE <= source_rate <= _MAX_SAMPLE_RATE:
                raise InvalidAudio("WAV sample rate is unsupported")
            if frame_count <= 0:
                raise InvalidAudio("audio file is empty")
            if frame_count > source_rate * _MAX_AUDIO_SECONDS:
                raise InvalidAudio("audio exceeds 45 seconds")
            pcm = wav_file.readframes(frame_count)
    except InvalidAudio:
        raise
    except Exception as exc:
        raise InvalidAudio("audio file is not a valid WAV") from exc
    if len(pcm) != frame_count * 2:
        raise InvalidAudio("WAV data is truncated")
    return pcm, source_rate


def _bounded_int(value: str, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _bounded_float(
    value: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed != parsed:  # NaN must not escape the bounds comparisons below.
        parsed = default
    return min(maximum, max(minimum, parsed))
