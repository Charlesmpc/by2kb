from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone


class JobStatus(str, enum.Enum):
    ACCEPTED = "accepted"
    RESOLVING = "resolving"
    FETCHING_TRANSCRIPT = "fetching_transcript"
    CAPTURING_MEDIA = "capturing_media"
    TRANSCRIBING = "transcribing"
    NORMALIZING = "normalizing"
    RAW_PUBLISHED = "raw_published"
    ENRICHMENT_PENDING = "enrichment_pending"
    ENRICHING = "enriching"
    UPDATED_PUBLISHED = "updated_published"
    COMPLETED = "completed"
    NEEDS_AUTH = "needs_auth"
    NO_NATIVE_TRANSCRIPT = "no_native_transcript"
    NEEDS_AUDIO_FALLBACK = "needs_audio_fallback"
    RATE_LIMITED = "rate_limited"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.NEEDS_AUTH,
        JobStatus.NO_NATIVE_TRANSCRIPT,
        JobStatus.RATE_LIMITED,
        JobStatus.FAILED_RETRYABLE,
        JobStatus.FAILED_TERMINAL,
        JobStatus.CANCELLED,
    }
)

STATUS_FOR_ERROR = {
    "NeedsAuth": JobStatus.NEEDS_AUTH,
    "NoNativeTranscript": JobStatus.NO_NATIVE_TRANSCRIPT,
    "RateLimited": JobStatus.RATE_LIMITED,
    "TransientProviderError": JobStatus.FAILED_RETRYABLE,
    "TerminalProviderError": JobStatus.FAILED_TERMINAL,
    "ConfigError": JobStatus.FAILED_TERMINAL,
    "UnsupportedUrl": JobStatus.FAILED_TERMINAL,
    "TranscriptQualityError": JobStatus.FAILED_TERMINAL,
    "JobCancelled": JobStatus.CANCELLED,
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Job:
    id: str
    platform: str
    video_id: str
    status: JobStatus = JobStatus.ACCEPTED
    requested_by: str | None = None
    destination: str | None = None
    options: dict = field(default_factory=dict)
    attempt_count: int = 0
    last_error_category: str | None = None
    error_message: str | None = None
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str | None = None
    cancel_requested: bool = False
