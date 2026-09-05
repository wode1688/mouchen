#!/usr/bin/env python3
"""Evaluate the private STT endpoint with a real, user-supplied Mandarin corpus.

The report deliberately omits reference text and transcripts. It contains only
sample identifiers and aggregate quality/performance measurements.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Iterable, Sequence
import unicodedata
from urllib import error, parse, request
import wave


ALLOWED_SAMPLE_RATES = {8_000, 16_000, 32_000, 48_000}
MAX_AUDIO_SECONDS = 45.0
SPEECH_CATEGORIES = {"clean", "noisy", "entity"}
EXPECTED_ENGINE = "local-paraformer-sherpa-onnx"


@dataclass(frozen=True)
class CorpusSample:
    sample_id: str
    path: Path
    kind: str
    category: str
    reference: str


@dataclass(frozen=True)
class AudioData:
    pcm: bytes
    sample_rate: int
    duration_seconds: float


@dataclass(frozen=True)
class RunResult:
    sample_id: str
    kind: str
    category: str
    duration_seconds: float
    latency_seconds: float
    real_time_factor: float
    status: str
    error: str | None = None
    edit_distance: int | None = None
    reference_characters: int | None = None
    exact: bool | None = None
    transcript_characters: int | None = None


@dataclass(frozen=True)
class DockerState:
    memory_limit_bytes: int
    oom_killed: bool
    restart_count: int
    peak_memory_bytes: int | None


def normalize_text(value: str) -> str:
    """Normalize Mandarin CER text while ignoring whitespace and punctuation."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def edit_distance(reference: str, hypothesis: str) -> int:
    """Return Levenshtein distance using memory linear in the shorter input."""

    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference
    previous = list(range(len(hypothesis) + 1))
    for row, reference_character in enumerate(reference, start=1):
        current = [row]
        for column, hypothesis_character in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[column - 1] + 1,
                    previous[column] + 1,
                    previous[column - 1]
                    + (reference_character != hypothesis_character),
                )
            )
        previous = current
    return previous[-1]


def percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def load_manifest(manifest_path: Path) -> list[CorpusSample]:
    samples: list[CorpusSample] = []
    seen_ids: set[str] = set()
    manifest_directory = manifest_path.resolve().parent

    with manifest_path.open("r", encoding="utf-8") as manifest_file:
        for line_number, raw_line in enumerate(manifest_file, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"manifest line {line_number} is not valid JSON") from exc
            if not isinstance(item, dict):
                raise ValueError(f"manifest line {line_number} must be an object")

            raw_path = str(item.get("path", "")).strip()
            if not raw_path:
                raise ValueError(f"manifest line {line_number} has no path")
            audio_path = Path(raw_path)
            if not audio_path.is_absolute():
                audio_path = manifest_directory / audio_path
            audio_path = audio_path.resolve()
            if not audio_path.is_file():
                raise ValueError(f"manifest line {line_number} audio file is missing")

            sample_id = str(item.get("id", audio_path.stem)).strip()
            if not sample_id or sample_id in seen_ids:
                raise ValueError(f"manifest line {line_number} has a missing or duplicate id")
            seen_ids.add(sample_id)

            kind = str(item.get("kind", "speech")).strip().casefold()
            if kind not in {"speech", "silence"}:
                raise ValueError(f"manifest line {line_number} has an invalid kind")
            category = str(
                item.get("category", "silence" if kind == "silence" else "clean")
            ).strip().casefold()
            reference = str(item.get("reference", ""))

            if kind == "speech":
                if category not in SPEECH_CATEGORIES:
                    raise ValueError(f"manifest line {line_number} has an invalid category")
                if not normalize_text(reference):
                    raise ValueError(f"manifest line {line_number} has no usable reference")
            else:
                if category != "silence":
                    raise ValueError(f"manifest line {line_number} silence category must be silence")
                if normalize_text(reference):
                    raise ValueError(f"manifest line {line_number} silence must not have a reference")

            samples.append(
                CorpusSample(
                    sample_id=sample_id,
                    path=audio_path,
                    kind=kind,
                    category=category,
                    reference=reference,
                )
            )

    if not samples:
        raise ValueError("manifest has no real audio samples")
    return samples


def read_pcm_wave(path: Path) -> AudioData:
    try:
        with wave.open(str(path), "rb") as source:
            if source.getnchannels() != 1:
                raise ValueError("WAV must be mono")
            if source.getsampwidth() != 2:
                raise ValueError("WAV must use signed 16-bit PCM")
            if source.getcomptype() != "NONE":
                raise ValueError("WAV must be uncompressed PCM")
            sample_rate = source.getframerate()
            if sample_rate not in ALLOWED_SAMPLE_RATES:
                raise ValueError("WAV sample rate must be 8, 16, 32, or 48 kHz")
            frame_count = source.getnframes()
            if frame_count <= 0:
                raise ValueError("WAV has no audio frames")
            duration_seconds = frame_count / sample_rate
            if duration_seconds > MAX_AUDIO_SECONDS:
                raise ValueError("WAV exceeds the endpoint's 45-second limit")
            pcm = source.readframes(frame_count)
    except (EOFError, wave.Error) as exc:
        raise ValueError("file is not a supported PCM WAV") from exc
    if len(pcm) != frame_count * 2:
        raise ValueError("WAV is truncated")
    return AudioData(pcm=pcm, sample_rate=sample_rate, duration_seconds=duration_seconds)


def transcribe(
    *,
    base_url: str,
    token: str,
    user_id: str,
    language: str,
    expected_engine: str,
    audio: AudioData,
    timeout_seconds: float,
) -> str:
    query = parse.urlencode({"sample_rate": audio.sample_rate, "language": language})
    endpoint = f"{base_url.rstrip('/')}/v1/audio/transcribe?{query}"
    api_request = request.Request(
        endpoint,
        data=audio.pcm,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
            "X-User-Id": user_id,
        },
    )
    try:
        with request.urlopen(api_request, timeout=timeout_seconds) as response:
            response_body = response.read()
    except error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except error.URLError as exc:
        raise RuntimeError("endpoint connection failed") from exc
    try:
        payload = json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("endpoint returned invalid JSON") from exc
    transcript = payload.get("transcript") if isinstance(payload, dict) else None
    engine = payload.get("engine") if isinstance(payload, dict) else None
    if not isinstance(transcript, str):
        raise RuntimeError("endpoint response has no transcript")
    if engine != expected_engine:
        raise RuntimeError("endpoint reported an unexpected STT engine")
    return transcript


def run_corpus(
    samples: Sequence[CorpusSample],
    *,
    base_url: str,
    token: str,
    user_id: str,
    language: str,
    expected_engine: str,
    timeout_seconds: float,
    repeat: int,
) -> list[RunResult]:
    results: list[RunResult] = []
    for _ in range(repeat):
        for sample in samples:
            try:
                audio = read_pcm_wave(sample.path)
            except ValueError as exc:
                results.append(
                    RunResult(
                        sample_id=sample.sample_id,
                        kind=sample.kind,
                        category=sample.category,
                        duration_seconds=0.0,
                        latency_seconds=0.0,
                        real_time_factor=0.0,
                        status="error",
                        error=str(exc),
                    )
                )
                continue

            started = time.monotonic()
            try:
                transcript = transcribe(
                    base_url=base_url,
                    token=token,
                    user_id=user_id,
                    language=language,
                    expected_engine=expected_engine,
                    audio=audio,
                    timeout_seconds=timeout_seconds,
                )
            except RuntimeError as exc:
                latency_seconds = time.monotonic() - started
                results.append(
                    RunResult(
                        sample_id=sample.sample_id,
                        kind=sample.kind,
                        category=sample.category,
                        duration_seconds=audio.duration_seconds,
                        latency_seconds=latency_seconds,
                        real_time_factor=latency_seconds / audio.duration_seconds,
                        status="error",
                        error=str(exc),
                    )
                )
                continue

            latency_seconds = time.monotonic() - started
            normalized_transcript = normalize_text(transcript)
            if sample.kind == "speech":
                normalized_reference = normalize_text(sample.reference)
                distance = edit_distance(normalized_reference, normalized_transcript)
                results.append(
                    RunResult(
                        sample_id=sample.sample_id,
                        kind=sample.kind,
                        category=sample.category,
                        duration_seconds=audio.duration_seconds,
                        latency_seconds=latency_seconds,
                        real_time_factor=latency_seconds / audio.duration_seconds,
                        status="ok",
                        edit_distance=distance,
                        reference_characters=len(normalized_reference),
                        exact=normalized_reference == normalized_transcript,
                        transcript_characters=len(normalized_transcript),
                    )
                )
            else:
                results.append(
                    RunResult(
                        sample_id=sample.sample_id,
                        kind=sample.kind,
                        category=sample.category,
                        duration_seconds=audio.duration_seconds,
                        latency_seconds=latency_seconds,
                        real_time_factor=latency_seconds / audio.duration_seconds,
                        status="ok",
                        exact=not normalized_transcript,
                        transcript_characters=len(normalized_transcript),
                    )
                )
    return results


def weighted_cer(results: Iterable[RunResult]) -> float | None:
    distance = 0
    reference_characters = 0
    for result in results:
        if result.status != "ok" or result.kind != "speech":
            continue
        distance += result.edit_distance or 0
        reference_characters += result.reference_characters or 0
    if reference_characters == 0:
        return None
    return distance / reference_characters


def _format_metric(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def build_summary(
    samples: Sequence[CorpusSample],
    results: Sequence[RunResult],
    *,
    max_clean_cer: float,
    max_noisy_cer: float,
    min_entity_exact_rate: float,
    max_p95_latency_seconds: float,
    max_p95_rtf: float,
    min_clean_samples: int,
    min_noisy_samples: int,
    min_entity_samples: int,
    min_silence_samples: int,
    baseline_cer: float | None,
    min_relative_cer_improvement: float,
) -> dict[str, Any]:
    failures: list[str] = []
    counts = {
        category: len({sample.sample_id for sample in samples if sample.category == category})
        for category in ("clean", "noisy", "entity", "silence")
    }
    minimums = {
        "clean": min_clean_samples,
        "noisy": min_noisy_samples,
        "entity": min_entity_samples,
        "silence": min_silence_samples,
    }
    for category, minimum in minimums.items():
        if counts[category] < minimum:
            failures.append(f"{category} corpus has {counts[category]} samples; requires {minimum}")

    request_failures = sum(result.status != "ok" for result in results)
    if request_failures:
        failures.append(f"{request_failures} transcription requests failed")

    successful_speech = [
        result for result in results if result.status == "ok" and result.kind == "speech"
    ]
    category_cer = {
        category: weighted_cer(
            result for result in successful_speech if result.category == category
        )
        for category in ("clean", "noisy", "entity")
    }
    if category_cer["clean"] is None or category_cer["clean"] > max_clean_cer:
        failures.append("clean Mandarin CER exceeds the configured limit")
    if category_cer["noisy"] is None or category_cer["noisy"] > max_noisy_cer:
        failures.append("noisy Mandarin CER exceeds the configured limit")

    entity_results = [result for result in successful_speech if result.category == "entity"]
    entity_exact_rate = (
        sum(result.exact is True for result in entity_results) / len(entity_results)
        if entity_results
        else None
    )
    if entity_exact_rate is None or entity_exact_rate < min_entity_exact_rate:
        failures.append("name/number exact-match rate is below the configured limit")

    successful_silence = [
        result for result in results if result.status == "ok" and result.kind == "silence"
    ]
    silence_hallucinations = sum(result.exact is False for result in successful_silence)
    if silence_hallucinations:
        failures.append(f"{silence_hallucinations} silence samples produced text")

    latencies = [result.latency_seconds for result in successful_speech]
    real_time_factors = [result.real_time_factor for result in successful_speech]
    p95_latency = percentile(latencies, 0.95)
    p95_rtf = percentile(real_time_factors, 0.95)
    if p95_latency is None or p95_latency > max_p95_latency_seconds:
        failures.append("speech p95 latency exceeds the configured limit")
    if p95_rtf is None or p95_rtf > max_p95_rtf:
        failures.append("speech p95 real-time factor exceeds the configured limit")

    overall_cer = weighted_cer(successful_speech)
    relative_cer_improvement: float | None = None
    if baseline_cer is not None:
        if baseline_cer <= 0 or overall_cer is None:
            failures.append("baseline CER cannot be compared")
        else:
            relative_cer_improvement = (baseline_cer - overall_cer) / baseline_cer
            if relative_cer_improvement < min_relative_cer_improvement:
                failures.append("relative CER improvement is below the configured limit")

    return {
        "passed": not failures,
        "failures": failures,
        "unique_corpus_samples": counts,
        "request_failures": request_failures,
        "speech_runs": len(successful_speech),
        "silence_runs": len(successful_silence),
        "overall_cer": _format_metric(overall_cer),
        "clean_cer": _format_metric(category_cer["clean"]),
        "noisy_cer": _format_metric(category_cer["noisy"]),
        "entity_cer": _format_metric(category_cer["entity"]),
        "entity_exact_rate": _format_metric(entity_exact_rate),
        "silence_hallucinations": silence_hallucinations,
        "p95_latency_seconds": _format_metric(p95_latency),
        "p95_real_time_factor": _format_metric(p95_rtf),
        "baseline_cer": _format_metric(baseline_cer),
        "relative_cer_improvement": _format_metric(relative_cer_improvement),
    }


def inspect_docker(container: str) -> DockerState:
    try:
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.HostConfig.Memory}} {{.State.OOMKilled}} {{.RestartCount}}",
                container,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip().split()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("could not inspect the Docker container") from exc
    if len(inspected) != 3:
        raise RuntimeError("Docker returned an unexpected container state")
    try:
        memory_limit = int(inspected[0])
        oom_killed = inspected[1].casefold() == "true"
        restart_count = int(inspected[2])
    except ValueError as exc:
        raise RuntimeError("Docker returned an invalid container state") from exc

    peak_script = (
        "for p in /sys/fs/cgroup/memory.peak "
        "/sys/fs/cgroup/memory/memory.max_usage_in_bytes; do "
        "if [ -r \"$p\" ]; then cat \"$p\"; exit 0; fi; done; exit 1"
    )
    try:
        peak_output = subprocess.run(
            ["docker", "exec", container, "sh", "-c", peak_script],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
        peak_memory = int(peak_output)
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        peak_memory = None
    return DockerState(
        memory_limit_bytes=memory_limit,
        oom_killed=oom_killed,
        restart_count=restart_count,
        peak_memory_bytes=peak_memory,
    )


def add_memory_gate(
    summary: dict[str, Any],
    *,
    before: DockerState | None,
    after: DockerState | None,
    require_memory_check: bool,
    expected_limit_bytes: int,
    max_peak_bytes: int,
) -> None:
    failures = summary["failures"]
    if before is None or after is None:
        summary["memory"] = {"checked": False}
        if require_memory_check:
            failures.append("Docker memory/OOM check was required but not run")
        summary["passed"] = not failures
        return

    summary["memory"] = {
        "checked": True,
        "limit_bytes": after.memory_limit_bytes,
        "peak_bytes": after.peak_memory_bytes,
        "oom_killed": after.oom_killed,
        "restart_count_before": before.restart_count,
        "restart_count_after": after.restart_count,
    }
    if after.memory_limit_bytes != expected_limit_bytes:
        failures.append("container memory limit is not the expected 1536 MiB envelope")
    if after.oom_killed:
        failures.append("container reports an OOM kill")
    if after.restart_count != before.restart_count:
        failures.append("container restarted during STT evaluation")
    if after.peak_memory_bytes is None:
        if require_memory_check:
            failures.append("container peak memory counter is unavailable")
    elif after.peak_memory_bytes > max_peak_bytes:
        failures.append("container peak memory exceeds the configured limit")
    summary["passed"] = not failures


def read_baseline_cer(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = payload["summary"]["overall_cer"]
        return float(value)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("baseline report has no valid summary.overall_cer") from exc


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description="Gate Mandarin STT with real PCM WAV samples; no audio is bundled."
    )
    argument_parser.add_argument("--manifest", required=True, type=Path)
    argument_parser.add_argument("--base-url", default="http://127.0.0.1:8788")
    argument_parser.add_argument("--token-file", required=True, type=Path)
    argument_parser.add_argument("--user-id", default=os.getenv("MOUCHEN_SINGLE_USER_ID", ""))
    argument_parser.add_argument("--language", default="zh")
    argument_parser.add_argument("--expected-engine", default=EXPECTED_ENGINE)
    argument_parser.add_argument("--timeout-seconds", type=float, default=180.0)
    argument_parser.add_argument("--repeat", type=int, default=1)
    argument_parser.add_argument("--report", type=Path)
    argument_parser.add_argument("--baseline-report", type=Path)
    argument_parser.add_argument("--min-relative-cer-improvement", type=float, default=0.20)
    argument_parser.add_argument("--max-clean-cer", type=float, default=0.10)
    argument_parser.add_argument("--max-noisy-cer", type=float, default=0.18)
    argument_parser.add_argument("--min-entity-exact-rate", type=float, default=0.85)
    argument_parser.add_argument("--max-p95-latency-seconds", type=float, default=8.0)
    argument_parser.add_argument("--max-p95-rtf", type=float, default=1.0)
    argument_parser.add_argument("--min-clean-samples", type=int, default=5)
    argument_parser.add_argument("--min-noisy-samples", type=int, default=5)
    argument_parser.add_argument("--min-entity-samples", type=int, default=5)
    argument_parser.add_argument("--min-silence-samples", type=int, default=5)
    argument_parser.add_argument("--docker-container")
    argument_parser.add_argument("--require-memory-check", action="store_true")
    argument_parser.add_argument(
        "--expected-container-limit-bytes", type=int, default=1_610_612_736
    )
    argument_parser.add_argument(
        "--max-container-peak-bytes", type=int, default=1_342_177_280
    )
    return argument_parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if not arguments.user_id.strip():
        raise SystemExit("--user-id or MOUCHEN_SINGLE_USER_ID is required")
    if not arguments.expected_engine.strip():
        raise SystemExit("--expected-engine must not be empty")
    if arguments.repeat < 1 or arguments.repeat > 10:
        raise SystemExit("--repeat must be between 1 and 10")
    if arguments.require_memory_check and not arguments.docker_container:
        raise SystemExit("--require-memory-check requires --docker-container")
    rate_thresholds = (
        arguments.min_relative_cer_improvement,
        arguments.max_clean_cer,
        arguments.max_noisy_cer,
        arguments.min_entity_exact_rate,
    )
    if any(value < 0 or value > 1 for value in rate_thresholds):
        raise SystemExit("CER, improvement, and exact-rate thresholds must be between 0 and 1")
    if arguments.max_p95_latency_seconds <= 0 or arguments.max_p95_rtf <= 0:
        raise SystemExit("latency thresholds must be positive")
    corpus_minimums = (
        arguments.min_clean_samples,
        arguments.min_noisy_samples,
        arguments.min_entity_samples,
        arguments.min_silence_samples,
    )
    if any(value < 1 for value in corpus_minimums):
        raise SystemExit("each corpus category must require at least one unique sample")
    if (
        arguments.expected_container_limit_bytes <= 0
        or arguments.max_container_peak_bytes <= 0
        or arguments.max_container_peak_bytes >= arguments.expected_container_limit_bytes
    ):
        raise SystemExit("container memory thresholds are invalid")
    try:
        token = arguments.token_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit("could not read token file") from exc
    if not token:
        raise SystemExit("token file is empty")

    try:
        samples = load_manifest(arguments.manifest)
        baseline_cer = read_baseline_cer(arguments.baseline_report)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    try:
        before = inspect_docker(arguments.docker_container) if arguments.docker_container else None
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    results = run_corpus(
        samples,
        base_url=arguments.base_url,
        token=token,
        user_id=arguments.user_id.strip(),
        language=arguments.language,
        expected_engine=arguments.expected_engine.strip(),
        timeout_seconds=arguments.timeout_seconds,
        repeat=arguments.repeat,
    )
    try:
        after = inspect_docker(arguments.docker_container) if arguments.docker_container else None
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    summary = build_summary(
        samples,
        results,
        max_clean_cer=arguments.max_clean_cer,
        max_noisy_cer=arguments.max_noisy_cer,
        min_entity_exact_rate=arguments.min_entity_exact_rate,
        max_p95_latency_seconds=arguments.max_p95_latency_seconds,
        max_p95_rtf=arguments.max_p95_rtf,
        min_clean_samples=arguments.min_clean_samples,
        min_noisy_samples=arguments.min_noisy_samples,
        min_entity_samples=arguments.min_entity_samples,
        min_silence_samples=arguments.min_silence_samples,
        baseline_cer=baseline_cer,
        min_relative_cer_improvement=arguments.min_relative_cer_improvement,
    )
    add_memory_gate(
        summary,
        before=before,
        after=after,
        require_memory_check=arguments.require_memory_check,
        expected_limit_bytes=arguments.expected_container_limit_bytes,
        max_peak_bytes=arguments.max_container_peak_bytes,
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_sample_count": len(samples),
        "repeat": arguments.repeat,
        "summary": summary,
        "runs": [asdict(result) for result in results],
    }
    rendered_report = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(rendered_report + "\n", encoding="utf-8")
    print(rendered_report)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
