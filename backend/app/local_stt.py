from __future__ import annotations

import asyncio
from array import array
import http.client
from io import BytesIO
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
import re
import secrets
import subprocess
from typing import Any, Callable
from urllib import parse as urllib_parse
import wave

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
import webrtcvad

from .stt_admission import (
    SttAdmissionRejected,
    SttAdmissionUnavailable,
    stt_admission,
)


router = APIRouter(prefix="/v1/audio", tags=["local-stt"])
_LOGGER = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_ALLOWED_SAMPLE_RATES = {8_000, 16_000, 32_000, 48_000}
_MAX_SECONDS = 45
_MAX_TRANSCRIPT_CHARACTERS = 12_000
_MAX_SERVER_RESPONSE_BYTES = 64 * 1_024
_MIN_GLOBAL_RMS = 100
_MIN_ACTIVE_FRAME_RMS = 200
_MIN_ACTIVE_FRAME_PEAK = 600
_LANGUAGE_RE = re.compile(r"^(?:auto|[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?)$")
_ENGINE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SRT_TIMING_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}$"
)


class LocalSttUnavailable(RuntimeError):
    pass


class LocalSttBusy(RuntimeError):
    pass


class LocalSttFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalSttConfig:
    enabled: bool
    ffmpeg_path: Path
    model_path: Path
    timeout_seconds: int = 15
    use_gpu: bool = False
    server_url: str | None = None
    server_engine: str = "local-stt-server"

    @classmethod
    def from_environment(cls) -> "LocalSttConfig":
        return cls(
            enabled=os.getenv("MOUCHEN_LOCAL_STT_ENABLED", "").strip().casefold()
            in _TRUE_VALUES,
            ffmpeg_path=Path(os.getenv("MOUCHEN_LOCAL_STT_FFMPEG", "ffmpeg").strip()),
            model_path=Path(os.getenv("MOUCHEN_LOCAL_STT_MODEL", "").strip()),
            timeout_seconds=_bounded_int(
                os.getenv("MOUCHEN_LOCAL_STT_TIMEOUT_SECONDS", "15"),
                default=15,
                minimum=5,
                maximum=120,
            ),
            use_gpu=os.getenv("MOUCHEN_LOCAL_STT_USE_GPU", "").strip().casefold()
            in _TRUE_VALUES,
            server_url=os.getenv("MOUCHEN_LOCAL_STT_SERVER_URL", "").strip() or None,
            server_engine=_validated_engine_name(
                os.getenv("MOUCHEN_LOCAL_STT_ENGINE", "local-stt-server")
            ),
        )


class TranscriptResponse(BaseModel):
    transcript: str
    language: str
    duration_ms: int
    engine: str = "local-ffmpeg-whisper.cpp"
    raw_audio_persisted: bool = False


Runner = Callable[..., subprocess.CompletedProcess[bytes]]
HttpConnectionFactory = Callable[..., Any]


class LocalSttService:
    """Transcribes through the resident local server or the legacy FFmpeg fallback."""

    def __init__(
        self,
        config: LocalSttConfig,
        *,
        runner: Runner = subprocess.run,
        connection_factory: HttpConnectionFactory = http.client.HTTPConnection,
    ) -> None:
        self.config = config
        self._runner = runner
        self._connection_factory = connection_factory
        self._lock = asyncio.Lock()

    def validate_configuration(self) -> None:
        if not self.config.enabled:
            raise LocalSttUnavailable("local STT is disabled")
        if not self.config.model_path.is_file():
            raise LocalSttUnavailable("local STT model is unavailable")
        if self.config.server_url is not None:
            _validate_server_url(self.config.server_url)
            return
        ffmpeg = self.config.ffmpeg_path
        if ffmpeg.is_absolute() and not ffmpeg.is_file():
            raise LocalSttUnavailable("local FFmpeg executable is unavailable")

    async def transcribe(
        self,
        pcm: bytes,
        *,
        sample_rate: int,
        language: str,
    ) -> TranscriptResponse:
        self.validate_configuration()
        _validate_audio(pcm, sample_rate)
        requested_language = language.strip().lower()
        if not _LANGUAGE_RE.fullmatch(requested_language):
            raise ValueError("unsupported language value")
        normalized_language = {
            "zh-cn": "zh",
            "zh-hans": "zh",
            "cmn": "zh",
        }.get(requested_language, requested_language)
        if not _has_speech_energy(pcm, sample_rate):
            return TranscriptResponse(
                transcript="",
                language=normalized_language,
                duration_ms=(len(pcm) * 1000) // (sample_rate * 2),
                engine=self._engine_name(),
            )
        if not _has_speech_frames(pcm, sample_rate):
            return TranscriptResponse(
                transcript="",
                language=normalized_language,
                duration_ms=(len(pcm) * 1000) // (sample_rate * 2),
                engine=self._engine_name(),
            )
        if self._lock.locked():
            raise LocalSttBusy("local STT is processing another segment")

        async with self._lock:
            transcript = await asyncio.to_thread(
                self._run_server if self.config.server_url else self._run_ffmpeg,
                pcm,
                sample_rate,
                normalized_language,
            )
        return TranscriptResponse(
            transcript=transcript,
            language=normalized_language,
            duration_ms=(len(pcm) * 1000) // (sample_rate * 2),
            engine=self._engine_name(),
        )

    def _engine_name(self) -> str:
        return (
            self.config.server_engine
            if self.config.server_url is not None
            else "local-ffmpeg-whisper.cpp"
        )

    def _run_server(self, pcm: bytes, sample_rate: int, language: str) -> str:
        server_url = self.config.server_url
        if server_url is None:
            raise LocalSttFailed("local STT server is not configured")
        _validate_server_url(server_url)
        body, content_type = _build_multipart_request(pcm, sample_rate, language)
        parsed = urllib_parse.urlsplit(server_url)
        connection = self._connection_factory(
            parsed.hostname,
            parsed.port,
            timeout=self.config.timeout_seconds,
        )
        try:
            connection.request(
                "POST",
                parsed.path,
                body=body,
                headers={
                    "Content-Type": content_type,
                    "Content-Length": str(len(body)),
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                raise LocalSttFailed("resident local STT server returned an error")
            response_body = response.read(_MAX_SERVER_RESPONSE_BYTES + 1)
        except LocalSttFailed:
            raise
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise LocalSttFailed("resident local STT server did not complete") from exc
        finally:
            connection.close()
        if len(response_body) > _MAX_SERVER_RESPONSE_BYTES:
            raise LocalSttFailed("resident local STT server response was too large")
        try:
            payload = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LocalSttFailed("resident local STT server returned invalid JSON") from exc
        transcript = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(transcript, str) or "\x00" in transcript:
            raise LocalSttFailed("resident local STT server returned no transcript")
        return transcript.strip()[:_MAX_TRANSCRIPT_CHARACTERS]

    def _run_ffmpeg(self, pcm: bytes, sample_rate: int, language: str) -> str:
        model = _escape_filter_value(self.config.model_path.resolve().as_posix())
        thread_count = _bounded_int(
            os.getenv("MOUCHEN_LOCAL_STT_THREADS", "2"),
            default=2,
            minimum=1,
            maximum=8,
        )
        filter_spec = (
            f"whisper=model='{model}':language={language}:queue=15:"
            f"use_gpu={'true' if self.config.use_gpu else 'false'}:"
            "destination='-':format=srt"
        )
        command = [
            str(self.config.ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-filter_threads",
            str(thread_count),
            "-f",
            "s16le",
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-af",
            filter_spec,
            "-f",
            "null",
            os.devnull,
        ]
        process_environment = os.environ.copy()
        process_environment["OMP_NUM_THREADS"] = str(thread_count)
        try:
            completed = self._runner(
                command,
                input=pcm,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.config.timeout_seconds,
                check=False,
                env=process_environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LocalSttFailed("local STT process did not complete") from exc
        if completed.returncode != 0:
            raise LocalSttFailed("local STT process returned an error")
        return _parse_ffmpeg_srt(completed.stdout)


_service: LocalSttService | None = None
_service_signature: tuple[str, ...] | None = None


def get_local_stt_service() -> LocalSttService:
    global _service, _service_signature
    config = LocalSttConfig.from_environment()
    signature = (
        str(config.enabled),
        str(config.ffmpeg_path),
        str(config.model_path),
        str(config.timeout_seconds),
        str(config.use_gpu),
        str(config.server_url),
        str(config.server_engine),
    )
    if _service is None or signature != _service_signature:
        _service = LocalSttService(config)
        _service_signature = signature
    return _service


def local_stt_runtime_ready(
    *,
    timeout_seconds: float = 1.0,
    connection_factory: HttpConnectionFactory = http.client.HTTPConnection,
) -> bool:
    """Readiness probe that cannot use proxies, redirects, or a public destination."""

    config = LocalSttConfig.from_environment()
    if not config.enabled:
        return True
    try:
        LocalSttService(config).validate_configuration()
        if config.server_url is None:
            return True
        parsed = urllib_parse.urlsplit(config.server_url)
        connection = connection_factory(parsed.hostname, parsed.port, timeout=timeout_seconds)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            response_body = response.read(1_025)
        finally:
            connection.close()
        if response.status != 200 or len(response_body) > 1_024:
            return False
        payload = json.loads(response_body.decode("utf-8"))
        return isinstance(payload, dict) and payload.get("status") == "ok"
    except (OSError, TimeoutError, ValueError, LocalSttUnavailable, http.client.HTTPException,
            UnicodeDecodeError, json.JSONDecodeError):
        return False


@router.post("/transcribe", response_model=TranscriptResponse)
async def transcribe_audio(
    request: Request,
    sample_rate: int = Query(16_000),
    language: str = Query("zh", min_length=2, max_length=24),
):
    """Transcribe one short PCM16 mono segment on this host only.

    The global API bearer middleware authenticates this endpoint. The raw body is
    forwarded in memory to the isolated resident Paraformer server and is never
    persisted by this service.
    """

    if sample_rate not in _ALLOWED_SAMPLE_RATES:
        raise HTTPException(422, "unsupported sample rate")
    principal = getattr(request.state, "auth_principal", None)
    user_id = str(getattr(principal, "user_id", "")).strip()
    if not user_id:
        raise HTTPException(401, "authentication required")
    content_length = request.headers.get("content-length", "").strip()
    max_bytes = sample_rate * 2 * _MAX_SECONDS
    if content_length.isdigit() and int(content_length) > max_bytes:
        raise HTTPException(413, "audio segment exceeds 45 seconds")

    try:
        lease = await asyncio.to_thread(stt_admission.acquire, user_id)
    except SttAdmissionRejected as exc:
        raise HTTPException(
            429,
            "speech transcription limit exceeded",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except SttAdmissionUnavailable as exc:
        raise HTTPException(
            503,
            "speech transcription admission is unavailable",
            headers={"Retry-After": "1"},
        ) from exc

    try:
        pcm_buffer = bytearray()
        try:
            body_read_timeout = _bounded_int(
                os.getenv("MOUCHEN_STT_BODY_READ_TIMEOUT_SECONDS", "30"),
                default=30,
                minimum=5,
                maximum=120,
            )
            try:
                async with asyncio.timeout(body_read_timeout):
                    async for chunk in request.stream():
                        if len(pcm_buffer) + len(chunk) > max_bytes:
                            raise HTTPException(413, "audio segment exceeds 45 seconds")
                        pcm_buffer.extend(chunk)
            except TimeoutError as exc:
                raise HTTPException(408, "audio upload timed out") from exc
            pcm = bytes(pcm_buffer)
        finally:
            # The immutable request chunks remain owned by Starlette, but do not
            # retain the aggregate mutable copy after producing the bounded PCM.
            pcm_buffer.clear()
        try:
            return await get_local_stt_service().transcribe(
                pcm,
                sample_rate=sample_rate,
                language=language,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except LocalSttUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except LocalSttBusy as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "1"}) from exc
        except LocalSttFailed as exc:
            raise HTTPException(502, str(exc)) from exc
    finally:
        try:
            # Shield the SQLite release so a disconnected client cannot retain
            # capacity until lease expiry. The raw audio never enters this call.
            await asyncio.shield(asyncio.to_thread(lease.release))
        except SttAdmissionUnavailable:
            # The expiring lease still recovers capacity after a database outage.
            _LOGGER.warning("failed to release an STT admission lease")


def _validate_audio(pcm: bytes, sample_rate: int) -> None:
    if sample_rate not in _ALLOWED_SAMPLE_RATES:
        raise ValueError("unsupported sample rate")
    if not pcm:
        raise ValueError("audio segment is empty")
    if len(pcm) % 2:
        raise ValueError("PCM16 segment must contain complete samples")
    if len(pcm) > sample_rate * 2 * _MAX_SECONDS:
        raise ValueError("audio segment exceeds 45 seconds")


def _has_speech_energy(pcm: bytes, sample_rate: int) -> bool:
    """Reject digital silence and very-low-energy noise before invoking STT."""
    samples = array("h")
    samples.frombytes(pcm)
    if os.sys.byteorder != "little":
        samples.byteswap()

    frame_size = max(1, sample_rate // 50)  # 20 ms
    active_frames = 0
    total_square = 0
    peak = 0
    frame_count = 0
    for start in range(0, len(samples), frame_size):
        frame = samples[start : start + frame_size]
        if not frame:
            continue
        frame_count += 1
        frame_square = 0
        frame_peak = 0
        for sample in frame:
            magnitude = abs(int(sample))
            frame_peak = max(frame_peak, magnitude)
            frame_square += magnitude * magnitude
        total_square += frame_square
        peak = max(peak, frame_peak)
        if (
            frame_peak >= _MIN_ACTIVE_FRAME_PEAK
            and frame_square >= len(frame) * (_MIN_ACTIVE_FRAME_RMS**2)
        ):
            active_frames += 1

    required_active_frames = min(5, max(1, (frame_count + 1) // 2))
    return (
        peak >= _MIN_ACTIVE_FRAME_PEAK
        and total_square >= len(samples) * (_MIN_GLOBAL_RMS**2)
        and active_frames >= required_active_frames
    )


def _has_speech_frames(pcm: bytes, sample_rate: int) -> bool:
    """Require a short run of speech-like WebRTC VAD frames before STT."""
    frame_bytes = (sample_rate * 20 // 1_000) * 2
    vad = webrtcvad.Vad(3)
    voiced_frames = 0
    consecutive_frames = 0
    longest_run = 0
    for start in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
        try:
            voiced = vad.is_speech(pcm[start : start + frame_bytes], sample_rate)
        except Exception:
            # VAD failures must fail closed; otherwise noise can become proactive advice.
            return False
        if voiced:
            voiced_frames += 1
            consecutive_frames += 1
            longest_run = max(longest_run, consecutive_frames)
        else:
            consecutive_frames = 0
    return voiced_frames >= 5 and longest_run >= 3


def _parse_ffmpeg_srt(output: bytes) -> str:
    parts: list[str] = []
    normalized = output.decode("utf-8", errors="replace").replace("\r\n", "\n")
    for raw_block in re.split(r"\n\s*\n", normalized):
        lines = raw_block.splitlines()
        timing_index = next(
            (
                index
                for index, line in enumerate(lines)
                if _SRT_TIMING_RE.fullmatch(line.strip())
            ),
            None,
        )
        if timing_index is None:
            continue
        text = "\n".join(lines[timing_index + 1 :]).strip()
        if text:
            parts.append(text)
    return " ".join(parts).strip()[:_MAX_TRANSCRIPT_CHARACTERS]


def _validate_server_url(value: str) -> None:
    """Allow only the unexposed Compose sidecar or an in-container loopback server."""

    try:
        parsed = urllib_parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise LocalSttUnavailable("local STT server URL is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"stt", "127.0.0.1", "localhost", "::1"}
        or port is None
        or parsed.path != "/inference"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise LocalSttUnavailable("local STT server URL is not an isolated endpoint")


def _build_multipart_request(pcm: bytes, sample_rate: int, language: str) -> tuple[bytes, str]:
    wav_buffer = BytesIO()
    with wave.open(wav_buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    wav_bytes = wav_buffer.getvalue()

    boundary = f"mouchen-{secrets.token_hex(16)}"
    boundary_bytes = boundary.encode("ascii")
    chunks: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        chunks.extend(
            [
                b"--" + boundary_bytes + b"\r\n",
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    add_field("language", language)
    add_field("response_format", "json")
    add_field("no_context", "true")
    chunks.extend(
        [
            b"--" + boundary_bytes + b"\r\n",
            b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n',
            b"Content-Type: audio/wav\r\n\r\n",
            wav_bytes,
            b"\r\n--" + boundary_bytes + b"--\r\n",
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _escape_filter_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _bounded_int(value: str, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _validated_engine_name(value: str) -> str:
    candidate = value.strip()
    return candidate if _ENGINE_NAME_RE.fullmatch(candidate) else "local-stt-server"
