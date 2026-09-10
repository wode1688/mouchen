from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import wave

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "deploy" / "scripts" / "evaluate_stt.py"
SPEC = importlib.util.spec_from_file_location("mouchen_evaluate_stt", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
evaluate_stt = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = evaluate_stt
SPEC.loader.exec_module(evaluate_stt)

ARCHIVE_NAME = "sherpa-onnx-paraformer-zh-2023-09-14.tar.bz2"
ARCHIVE_SHA256 = "9c49fd9c6fb63de8e18c1054cf3d100f804741b7e608e187923cd8ff09fa9f03"
ARCHIVE_SIZE = "234051698"
MODEL_NAME = "model.int8.onnx"
MODEL_SHA256 = "f36a0433bcf096bd6d6f11b80a3ac8bed110bdca632fe0d731df8d1a84475945"
MODEL_SIZE = "243371218"
TOKENS_NAME = "tokens.txt"
VOCAB_SHA256 = "59aba8873a2ed1e122c25fee421e25f283b63290efbde85c1f01a853d83cb6e6"


def _sample(sample_id: str, category: str):
    return evaluate_stt.CorpusSample(
        sample_id=sample_id,
        path=Path(f"{sample_id}.wav"),
        kind="silence" if category == "silence" else "speech",
        category=category,
        reference="" if category == "silence" else "普通话测试文本",
    )


def _result(sample_id: str, category: str, *, distance: int = 0, exact: bool = True):
    silence = category == "silence"
    return evaluate_stt.RunResult(
        sample_id=sample_id,
        kind="silence" if silence else "speech",
        category=category,
        duration_seconds=2.0,
        latency_seconds=0.5,
        real_time_factor=0.25,
        status="ok",
        edit_distance=None if silence else distance,
        reference_characters=None if silence else 10,
        exact=exact,
        transcript_characters=0 if silence else 10,
    )


def test_mandarin_normalization_and_character_error_distance():
    assert evaluate_stt.normalize_text("你好，World ２０２６！") == "你好world2026"
    assert evaluate_stt.edit_distance("你好世界", "你好世间") == 1
    assert evaluate_stt.edit_distance("", "你好") == 2


def test_paraformer_packaging_profile_is_pinned_and_consistent():
    repository = Path(__file__).resolve().parents[2]
    dockerfile = (repository / "backend" / "Dockerfile").read_text(encoding="utf-8")
    compose = (repository / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    environment = (repository / "deploy" / "env.example").read_text(encoding="utf-8")
    verifier = (repository / "deploy" / "scripts" / "verify.sh").read_text(
        encoding="utf-8"
    )

    for content in (dockerfile, compose, environment):
        assert ARCHIVE_NAME in content
        assert ARCHIVE_SHA256 in content
        assert ARCHIVE_SIZE in content
        assert MODEL_SHA256 in content
        assert MODEL_SIZE in content
        assert VOCAB_SHA256 in content
    assert MODEL_NAME in dockerfile
    assert TOKENS_NAME in dockerfile
    assert "sherpa-onnx==1.13.4" in (
        repository / "backend" / "requirements-stt.txt"
    ).read_text(encoding="utf-8")
    assert "app.paraformer_server:app" in compose
    assert "internal: true" in compose
    assert "whisper-server" not in dockerfile
    assert "cmake --build" not in dockerfile
    assert "MOUCHEN_STT_MEMORY_LIMIT:-1536m" in compose
    assert "MOUCHEN_LOCAL_STT_THREADS:-4" in compose
    assert "hashlib.file_digest" in verifier
    assert "model.stat().st_size != expected_model_size" in verifier


def test_manifest_requires_real_pcm_wav_and_references(tmp_path: Path):
    audio_path = tmp_path / "sample.wav"
    with wave.open(str(audio_path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        target.writeframes(b"\x00\x00" * 16_000)
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        '{"id":"clean-01","path":"sample.wav","kind":"speech",'
        '"category":"clean","reference":"你好，世界"}\n',
        encoding="utf-8",
    )

    samples = evaluate_stt.load_manifest(manifest)
    audio = evaluate_stt.read_pcm_wave(samples[0].path)

    assert samples[0].reference == "你好，世界"
    assert audio.sample_rate == 16_000
    assert audio.duration_seconds == 1.0
    assert len(audio.pcm) == 32_000


def test_manifest_rejects_missing_audio_instead_of_fabricating_results(tmp_path: Path):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        '{"id":"clean-01","path":"missing.wav","kind":"speech",'
        '"category":"clean","reference":"你好"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="audio file is missing"):
        evaluate_stt.load_manifest(manifest)


def test_quality_latency_and_memory_gates_pass_only_measured_results():
    samples = [
        *[_sample(f"clean-{index}", "clean") for index in range(5)],
        *[_sample(f"noisy-{index}", "noisy") for index in range(5)],
        *[_sample(f"entity-{index}", "entity") for index in range(5)],
        *[_sample(f"silence-{index}", "silence") for index in range(5)],
    ]
    results = [
        *[_result(f"clean-{index}", "clean") for index in range(5)],
        *[_result(f"noisy-{index}", "noisy", distance=1) for index in range(5)],
        *[_result(f"entity-{index}", "entity") for index in range(5)],
        *[_result(f"silence-{index}", "silence") for index in range(5)],
    ]

    summary = evaluate_stt.build_summary(
        samples,
        results,
        max_clean_cer=0.10,
        max_noisy_cer=0.18,
        min_entity_exact_rate=0.85,
        max_p95_latency_seconds=8.0,
        max_p95_rtf=1.0,
        min_clean_samples=5,
        min_noisy_samples=5,
        min_entity_samples=5,
        min_silence_samples=5,
        baseline_cer=0.20,
        min_relative_cer_improvement=0.20,
    )
    before = evaluate_stt.DockerState(1_610_612_736, False, 0, 400_000_000)
    after = evaluate_stt.DockerState(1_610_612_736, False, 0, 900_000_000)
    evaluate_stt.add_memory_gate(
        summary,
        before=before,
        after=after,
        require_memory_check=True,
        expected_limit_bytes=1_610_612_736,
        max_peak_bytes=1_342_177_280,
    )

    assert summary["passed"] is True
    assert summary["clean_cer"] == 0.0
    assert summary["noisy_cer"] == 0.1
    assert summary["silence_hallucinations"] == 0
    assert summary["memory"]["peak_bytes"] == 900_000_000


def test_silence_hallucination_and_oom_fail_release_gate():
    samples = [_sample("silence-0", "silence")]
    results = [_result("silence-0", "silence", exact=False)]
    summary = evaluate_stt.build_summary(
        samples,
        results,
        max_clean_cer=0.10,
        max_noisy_cer=0.18,
        min_entity_exact_rate=0.85,
        max_p95_latency_seconds=8.0,
        max_p95_rtf=1.0,
        min_clean_samples=0,
        min_noisy_samples=0,
        min_entity_samples=0,
        min_silence_samples=1,
        baseline_cer=None,
        min_relative_cer_improvement=0.20,
    )
    before = evaluate_stt.DockerState(1_610_612_736, False, 0, None)
    after = evaluate_stt.DockerState(1_610_612_736, True, 1, 1_500_000_000)
    evaluate_stt.add_memory_gate(
        summary,
        before=before,
        after=after,
        require_memory_check=True,
        expected_limit_bytes=1_610_612_736,
        max_peak_bytes=1_342_177_280,
    )

    assert summary["passed"] is False
    assert any("silence samples produced text" in failure for failure in summary["failures"])
    assert any("OOM" in failure for failure in summary["failures"])
    assert any("restarted" in failure for failure in summary["failures"])
