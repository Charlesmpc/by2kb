from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from by2kb.normalize import NormalizedTranscript

ASSESSMENT_VERSION = "1.0"
QualityStatus = Literal["pass", "warning", "fail"]


class QualityMetrics(BaseModel):
    effective_char_count: int
    segment_count: int
    duration_ms: int | None
    chars_per_minute: float | None
    repetition_ratio: float
    timed_coverage_ratio: float | None
    max_segment_gap_ms: int | None


class QualityThresholds(BaseModel):
    expected_min_chars: int
    hard_min_chars: int
    warning_min_chars_per_minute: float
    fail_repetition_ratio: float
    warning_repetition_ratio: float
    fail_timed_coverage_ratio: float
    warning_timed_coverage_ratio: float


class TranscriptQuality(BaseModel):
    assessment_version: str = ASSESSMENT_VERSION
    status: QualityStatus
    metrics: QualityMetrics
    thresholds: QualityThresholds
    reasons: list[str]


def assess_transcript(normalized: "NormalizedTranscript") -> TranscriptQuality:
    transcript = normalized.transcript
    duration_ms = normalized.source.duration_ms
    text = "".join(segment.text for segment in transcript.segments)
    effective = "".join(character.lower() for character in text if character.isalnum())
    effective_count = len(effective)
    duration_minutes = duration_ms / 60_000 if duration_ms and duration_ms > 0 else None
    kind_factor = {"human": 0.7, "auto_caption": 0.85, "asr": 1.0}[transcript.kind]
    expected_min = 20
    if duration_minutes is not None:
        expected_min = max(20, min(400, round(duration_minutes * 20 * kind_factor)))
    hard_min = max(3, round(expected_min * 0.2))
    warning_cpm = 12.0 * kind_factor
    thresholds = QualityThresholds(
        expected_min_chars=expected_min,
        hard_min_chars=hard_min,
        warning_min_chars_per_minute=warning_cpm,
        fail_repetition_ratio=0.85,
        warning_repetition_ratio=0.65,
        fail_timed_coverage_ratio=0.10,
        warning_timed_coverage_ratio=0.35,
    )

    repetition = _repetition_ratio(effective)
    coverage, max_gap = _timing_metrics(normalized)
    chars_per_minute = (
        round(effective_count / duration_minutes, 3)
        if duration_minutes is not None and duration_minutes > 0
        else None
    )
    metrics = QualityMetrics(
        effective_char_count=effective_count,
        segment_count=len(transcript.segments),
        duration_ms=duration_ms,
        chars_per_minute=chars_per_minute,
        repetition_ratio=round(repetition, 4),
        timed_coverage_ratio=round(coverage, 4) if coverage is not None else None,
        max_segment_gap_ms=max_gap,
    )

    failures: list[str] = []
    warnings: list[str] = []
    if effective_count == 0:
        failures.append("empty_transcript")
    elif effective_count < hard_min:
        failures.append("suspiciously_short")
    elif effective_count < expected_min:
        warnings.append("short_for_duration")

    if (
        chars_per_minute is not None
        and duration_minutes is not None
        and duration_minutes >= 1
        and chars_per_minute < warning_cpm
        and "suspiciously_short" not in failures
    ):
        warnings.append("low_characters_per_minute")

    if effective_count >= 40:
        if repetition >= thresholds.fail_repetition_ratio:
            failures.append("highly_repetitive")
        elif repetition >= thresholds.warning_repetition_ratio:
            warnings.append("repetitive")

    if coverage is not None:
        if coverage < thresholds.fail_timed_coverage_ratio:
            failures.append("very_low_timed_coverage")
        elif coverage < thresholds.warning_timed_coverage_ratio:
            warnings.append("low_timed_coverage")
        if (
            duration_ms
            and max_gap is not None
            and max_gap > max(30_000, duration_ms * 0.4)
        ):
            warnings.append("large_segment_gap")

    reasons = list(dict.fromkeys(failures + warnings))
    status: QualityStatus = "fail" if failures else "warning" if warnings else "pass"
    return TranscriptQuality(
        status=status,
        metrics=metrics,
        thresholds=thresholds,
        reasons=reasons,
    )


def quality_notice(quality: TranscriptQuality | None) -> str | None:
    if quality is None or quality.status == "pass":
        return None
    reasons = ", ".join(quality.reasons) or "unspecified"
    return (
        f"> **Transcript quality {quality.status}:** assessment "
        f"v{quality.assessment_version}; {reasons}. Treat summaries as incomplete."
    )


def _repetition_ratio(text: str, *, width: int = 4) -> float:
    if len(text) < width * 2:
        return 0.0
    grams = [text[index : index + width] for index in range(len(text) - width + 1)]
    return 1.0 - (len(set(grams)) / len(grams))


def _timing_metrics(
    normalized: "NormalizedTranscript",
) -> tuple[float | None, int | None]:
    duration_ms = normalized.source.duration_ms
    segments = normalized.transcript.segments
    if not duration_ms or duration_ms <= 0 or not segments:
        return None, None
    if (
        len(segments) == 1
        and segments[0].start_ms == 0
        and segments[0].duration_ms == duration_ms
    ):
        # Text-only providers are normalized as one full-duration synthetic segment.
        return None, None
    ranges = sorted(
        (
            max(0, segment.start_ms),
            min(duration_ms, segment.start_ms + max(0, segment.duration_ms)),
        )
        for segment in segments
        if segment.duration_ms > 0 and segment.start_ms < duration_ms
    )
    if not ranges:
        return 0.0, duration_ms
    covered = 0
    max_gap = max(0, ranges[0][0])
    start, end = ranges[0]
    for next_start, next_end in ranges[1:]:
        if next_start <= end:
            end = max(end, next_end)
            continue
        covered += max(0, end - start)
        max_gap = max(max_gap, next_start - end)
        start, end = next_start, next_end
    covered += max(0, end - start)
    max_gap = max(max_gap, duration_ms - end)
    return min(1.0, covered / duration_ms), max_gap
