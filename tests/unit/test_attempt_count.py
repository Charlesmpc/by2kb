import pytest

from by2kb.config import Config
from by2kb.errors import TerminalProviderError, TransientProviderError
from by2kb.jobs.model import Job, JobStatus
from by2kb.jobs.runner import _ingest
from by2kb.jobs.store import JobStore
from by2kb.jobs.task_control import cancel_task, retry_task, task_status, wait_for_task
from by2kb.normalize import from_asr_result
from by2kb.providers.asr import AsrResult
from by2kb.providers.base import PreparedSource, SourceIdentity


@pytest.fixture
def pipeline(tmp_path):
    config = Config(home=tmp_path, library_root=tmp_path / "library",
                    db_path=tmp_path / "jobs.db", enrichment_executor="disabled")
    identity = SourceIdentity(platform="bilibili", video_id="test-attempt",
                              canonical_url="https://example.test/video")

    async def resolve(_client):
        return identity

    async def prepare(_client, _identity, _work, _options, store, job):
        assert store.get_job(job.id).attempt_count >= 1
        store.update_status(job.id, JobStatus.FETCHING_TRANSCRIPT)
        transcript = from_asr_result(
            identity, title="Test", author="Author", duration_ms=10000,
            fetched_at="2026-09-07T00:00:00Z",
            asr_result=AsrResult(provider="test", model="test",
                                text="A useful explanation of how database transactions work."),
        )
        return PreparedSource(title="Test", author="Author", duration_s=10,
                              source_payload={}, transcript=transcript)

    async def run(**kwargs):
        return await _ingest(resolve, kwargs.pop("prepare", prepare), config,
                             source_reference=identity.canonical_url, **kwargs)

    return config, run


def test_transitions_and_cancellation_preserve_counts(tmp_path):
    config = Config(home=tmp_path, library_root=tmp_path, db_path=tmp_path / "db")
    store = JobStore(config.db_path)
    try:
        store.create_job(Job(id="job", platform="test", video_id="test"))
        assert store.get_job("job").attempt_count == 0
        for status in JobStatus:
            store.update_status("job", status)
            assert store.get_job("job").attempt_count == 0
        store.begin_attempt("job")
        store.update_status("job", JobStatus.ENRICHMENT_PENDING)
        assert cancel_task(config, "job")["attempt_count"] == 1
        assert wait_for_task(config, "job", timeout_s=0)["attempt_count"] == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_success_duplicate_refresh_and_reenrich(pipeline):
    config, run = pipeline
    first = await run()
    assert first.status == "completed"
    assert task_status(config, first.job_id)["attempt_count"] == 1
    assert (await run()).status == "duplicate"
    assert task_status(config, first.job_id)["attempt_count"] == 1
    assert (await run(refresh=True)).status == "completed"
    assert task_status(config, first.job_id)["attempt_count"] == 2
    assert (await run(re_enrich=True, enricher="external_agent")).status == "enrichment_pending"
    assert task_status(config, first.job_id)["attempt_count"] == 3
    await run(enricher="external_agent")  # Existing pending Agent work is reused.
    assert task_status(config, first.job_id)["attempt_count"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("error,state", [
    (TransientProviderError, "failed_retryable"),
    (TerminalProviderError, "failed_terminal"),
])
async def test_first_failure_and_retry(pipeline, monkeypatch, error, state):
    config, run = pipeline

    async def fail(*_args):
        raise error("provider failed")

    first = await run(prepare=fail)
    assert first.status == state
    assert task_status(config, first.job_id)["attempt_count"] == 1
    if state == "failed_retryable":
        async def ingest(_source, _config, **kwargs):
            return await run(**kwargs)

        monkeypatch.setattr("by2kb.jobs.task_control.ingest_source", ingest)
        retried = await retry_task(config, first.job_id)
        assert retried["state"] == "completed"
        assert retried["attempt_count"] == 2


@pytest.mark.asyncio
async def test_agent_failure_retry_reuses_transcript(pipeline, monkeypatch):
    config, run = pipeline
    first = await run(enricher="external_agent")
    store = JobStore(config.db_path)
    try:
        store.update_status(first.job_id, JobStatus.ENRICHING)
        store.update_status(first.job_id, JobStatus.FAILED_RETRYABLE)
        assert store.get_job(first.job_id).attempt_count == 1
    finally:
        store.close()

    async def ingest(_source, _config, **kwargs):
        assert kwargs["re_enrich"] is True
        return await run(**kwargs)

    monkeypatch.setattr("by2kb.jobs.task_control.ingest_source", ingest)
    retried = await retry_task(config, first.job_id)
    assert retried["reused_transcript"] is True
    assert retried["attempt_count"] == 2
    assert retried["state"] == "enrichment_pending"


def test_legacy_count_is_preserved_on_reopen(tmp_path):
    path = tmp_path / "db"
    store = JobStore(path)
    store.create_job(Job(id="old", platform="test", video_id="old", attempt_count=14))
    store.close()
    store = JobStore(path)
    try:
        assert store.get_job("old").attempt_count == 14
        store.begin_attempt("old")
        store.update_status("old", JobStatus.COMPLETED)
        assert store.get_job("old").attempt_count == 15
    finally:
        store.close()
