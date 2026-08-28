from __future__ import annotations

from by2kb.normalize import from_asr_result
from by2kb.providers.asr import AsrResult
from by2kb.providers.base import SourceIdentity
from by2kb.quality import assess_transcript
from by2kb.skills.model import find_skill
from by2kb.skills.runner import build_prompts
from by2kb.writers.updated import render_updated_md


def _normalized(*, duration_ms: int, text: str = "", segments=None, kind="asr"):
    identity = SourceIdentity(
        platform="test",
        video_id="quality",
        canonical_url="test://quality",
    )
    normalized = from_asr_result(
        identity,
        title="Quality fixture",
        author="by2kb",
        duration_ms=duration_ms,
        asr_result=AsrResult(
            provider="fixture",
            model="fixture",
            text=text,
            segments=segments or [],
        ),
        fetched_at="2026-08-28T00:00:00Z",
    )
    normalized.transcript.kind = kind
    return normalized


def test_empty_transcript_fails():
    quality = assess_transcript(_normalized(duration_ms=600_000))

    assert quality.status == "fail"
    assert "empty_transcript" in quality.reasons


def test_truncated_long_transcript_fails_duration_aware_threshold():
    quality = assess_transcript(
        _normalized(duration_ms=3_600_000, text="This is only a tiny fragment.")
    )

    assert quality.status == "fail"
    assert "suspiciously_short" in quality.reasons
    assert quality.thresholds.expected_min_chars == 400


def test_highly_repetitive_transcript_fails():
    quality = assess_transcript(
        _normalized(duration_ms=120_000, text="abcd" * 100)
    )

    assert quality.status == "fail"
    assert "highly_repetitive" in quality.reasons


def test_low_timed_coverage_fails():
    text = "".join(chr(0x4E00 + index) for index in range(300))
    quality = assess_transcript(
        _normalized(
            duration_ms=600_000,
            segments=[
                {"start": 0, "end": 10, "text": text[:150]},
                {"start": 590, "end": 600, "text": text[150:]},
            ],
        )
    )

    assert quality.status == "fail"
    assert quality.metrics.timed_coverage_ratio < 0.1
    assert "very_low_timed_coverage" in quality.reasons


def test_healthy_timestamped_transcript_passes():
    text = "".join(chr(0x4E00 + index) for index in range(300))
    quality = assess_transcript(
        _normalized(
            duration_ms=120_000,
            segments=[
                {"start": 0, "end": 60, "text": text[:150]},
                {"start": 60, "end": 120, "text": text[150:]},
            ],
        )
    )

    assert quality.status == "pass"
    assert quality.reasons == []
    assert quality.metrics.timed_coverage_ratio == 1.0


def test_borderline_warning_is_forced_into_outputs_and_prompt():
    text = "".join(chr(0x4E00 + index) for index in range(100))
    normalized = _normalized(duration_ms=600_000, text=text)
    normalized.transcript.quality = assess_transcript(normalized)
    skill = find_skill("short-video-abstract", [])

    _system, prompt = build_prompts(skill, normalized, "# raw")
    rendered = render_updated_md(
        normalized,
        body="# Generated notes",
        skill_name=skill.name,
        skill_version=skill.version,
        model="fixture",
        provider="fixture",
    )

    assert normalized.transcript.quality.status == "warning"
    assert '"status": "warning"' in prompt
    assert "Transcript quality warning" in rendered
    assert "transcript_quality: warning" in rendered


def test_thresholds_adjust_for_transcript_kind():
    asr = assess_transcript(
        _normalized(duration_ms=600_000, text="useful content", kind="asr")
    )
    human = assess_transcript(
        _normalized(duration_ms=600_000, text="useful content", kind="human")
    )

    assert human.thresholds.expected_min_chars < asr.thresholds.expected_min_chars
