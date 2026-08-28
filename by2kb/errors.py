from __future__ import annotations

EXIT_COMPLETED = 0
EXIT_TERMINAL = 1
EXIT_RETRYABLE = 2
EXIT_NEEDS_AUTH = 3
EXIT_DUPLICATE = 4


class By2kbError(Exception):
    exit_code = EXIT_TERMINAL


class ConfigError(By2kbError):
    pass


class UnsupportedUrl(By2kbError):
    pass


class DuplicateJob(By2kbError):
    exit_code = EXIT_DUPLICATE

    def __init__(self, message: str, *, job_id: str | None = None):
        super().__init__(message)
        self.job_id = job_id


class ProviderError(By2kbError):
    def __init__(self, message: str, *, provider: str | None = None, detail: object = None):
        super().__init__(message)
        self.provider = provider
        self.detail = detail


class NeedsAuth(ProviderError):
    exit_code = EXIT_NEEDS_AUTH


class NoNativeTranscript(ProviderError):
    exit_code = EXIT_TERMINAL


class RateLimited(ProviderError):
    exit_code = EXIT_RETRYABLE


class TransientProviderError(ProviderError):
    exit_code = EXIT_RETRYABLE


class TerminalProviderError(ProviderError):
    exit_code = EXIT_TERMINAL


class TranscriptQualityError(By2kbError):
    """The raw transcript is preserved, but enrichment must not run."""


class JobCancelled(By2kbError):
    """Cooperative cancellation was observed between pipeline stages."""


def category_of(error: By2kbError) -> str:
    return type(error).__name__
