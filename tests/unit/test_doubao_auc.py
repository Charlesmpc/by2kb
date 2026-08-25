from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from by2kb.errors import TerminalProviderError, TransientProviderError
from by2kb.providers.asr import AsrOptions
from by2kb.providers.asr_doubao_auc import (
    DoubaoAucAsrProvider,
    DoubaoAucConfig,
    _headers,
    _raise_provider_error,
)
from by2kb.providers.base import LocalAudio


def test_new_console_api_key_auth_does_not_require_legacy_credentials(monkeypatch):
    monkeypatch.setenv("VOLC_ACCESS_KEY_ID", "tos-ak")
    monkeypatch.setenv("VOLC_SECRET_ACCESS_KEY", "tos-sk")
    monkeypatch.setenv("TOS_BUCKET", "private-bucket")
    monkeypatch.setenv("DOUBAO_API_KEY", "new-api-key")
    monkeypatch.delenv("DOUBAO_APPID", raising=False)
    monkeypatch.delenv("DOUBAO_ACCESS_TOKEN", raising=False)

    config = DoubaoAucConfig.from_env()

    assert config.api_key == "new-api-key"
    assert config.app_id is None
    assert config.access_token is None


def test_new_console_api_key_headers_exclude_legacy_headers():
    config = DoubaoAucConfig(
        access_key="tos-ak",
        secret_key="tos-sk",
        bucket="private-bucket",
        api_key="new-api-key",
        app_id=None,
        access_token=None,
    )

    headers = _headers(config, "request-id", submit=True)

    assert headers["X-Api-Key"] == "new-api-key"
    assert headers["X-Api-Resource-Id"] == "volc.seedasr.auc"
    assert headers["X-Api-Sequence"] == "-1"
    assert "X-Api-App-Key" not in headers
    assert "X-Api-Access-Key" not in headers


@pytest.mark.asyncio
async def test_chunk_timeout_retries_only_failed_chunk_and_reuses_checkpoints(
    tmp_path, monkeypatch
):
    chunks = [tmp_path / "chunk-000.ogg", tmp_path / "chunk-001.ogg"]
    for chunk in chunks:
        chunk.write_bytes(b"audio")
    checkpoint_dir = tmp_path / "checkpoints"
    provider = DoubaoAucAsrProvider(
        DoubaoAucConfig("tos-ak", "tos-sk", "private-bucket")
    )
    calls: dict[str, int] = {}

    async def fake_transcribe(chunk, _format, _options):
        calls[chunk.name] = calls.get(chunk.name, 0) + 1
        if chunk.name == "chunk-000.ogg" and calls[chunk.name] == 1:
            raise TransientProviderError("simulated timeout", provider="doubao_auc")
        return f"text-{chunk.stem}"

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(provider, "_transcribe_one", fake_transcribe)
    monkeypatch.setattr(provider, "_retry_sleep", no_sleep)

    result = await provider._transcribe_chunks(
        chunks, AsrOptions(timeout_s=150), checkpoint_dir
    )

    assert result == "text-chunk-000text-chunk-001"
    assert calls == {"chunk-000.ogg": 2, "chunk-001.ogg": 1}
    assert (checkpoint_dir / "chunk-000.txt").read_text() == "text-chunk-000"
    assert (checkpoint_dir / "chunk-001.txt").read_text() == "text-chunk-001"

    async def must_not_transcribe(*_args):
        raise AssertionError("completed chunks must be loaded from checkpoints")

    monkeypatch.setattr(provider, "_transcribe_one", must_not_transcribe)
    resumed = await provider._transcribe_chunks(
        chunks, AsrOptions(timeout_s=150), checkpoint_dir
    )
    assert resumed == result


@pytest.mark.asyncio
async def test_terminal_chunk_error_is_not_retried(tmp_path, monkeypatch):
    chunk = tmp_path / "chunk-000.ogg"
    chunk.write_bytes(b"audio")
    provider = DoubaoAucAsrProvider(
        DoubaoAucConfig("tos-ak", "tos-sk", "private-bucket")
    )
    calls = 0

    async def terminal_failure(*_args):
        nonlocal calls
        calls += 1
        raise TerminalProviderError("bad audio", provider="doubao_auc")

    monkeypatch.setattr(provider, "_transcribe_one", terminal_failure)

    with pytest.raises(TerminalProviderError, match="chunk 0 .*bad audio"):
        await provider._transcribe_chunks(
            [chunk], AsrOptions(timeout_s=150), tmp_path / "checkpoints"
        )
    assert calls == 1


@pytest.mark.asyncio
async def test_retry_exhaustion_reports_chunk_and_attempt_count(tmp_path, monkeypatch):
    chunk = tmp_path / "chunk-000.ogg"
    chunk.write_bytes(b"audio")
    provider = DoubaoAucAsrProvider(
        DoubaoAucConfig("tos-ak", "tos-sk", "private-bucket")
    )
    calls = 0

    async def transient_failure(*_args):
        nonlocal calls
        calls += 1
        raise TransientProviderError("still processing", provider="doubao_auc")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(provider, "_transcribe_one", transient_failure)
    monkeypatch.setattr(provider, "_retry_sleep", no_sleep)

    with pytest.raises(
        TransientProviderError,
        match=r"chunk 0 failed after 3 attempts: still processing",
    ):
        await provider._transcribe_chunks(
            [chunk], AsrOptions(timeout_s=150), tmp_path / "checkpoints"
        )
    assert calls == 3


@pytest.mark.asyncio
async def test_successful_chunks_survive_sibling_retry_exhaustion(tmp_path, monkeypatch):
    chunks = [tmp_path / "chunk-000.ogg", tmp_path / "chunk-001.ogg"]
    for chunk in chunks:
        chunk.write_bytes(b"audio")
    checkpoint_dir = tmp_path / "checkpoints"
    provider = DoubaoAucAsrProvider(
        DoubaoAucConfig("tos-ak", "tos-sk", "private-bucket")
    )
    calls: dict[str, int] = {}

    async def one_chunk_fails(chunk, _format, _options):
        calls[chunk.name] = calls.get(chunk.name, 0) + 1
        if chunk.name == "chunk-000.ogg":
            raise TransientProviderError("still processing", provider="doubao_auc")
        return "saved-success"

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(provider, "_transcribe_one", one_chunk_fails)
    monkeypatch.setattr(provider, "_retry_sleep", no_sleep)

    with pytest.raises(TransientProviderError, match="chunk 0 failed after 3 attempts"):
        await provider._transcribe_chunks(
            chunks, AsrOptions(timeout_s=150), checkpoint_dir
        )

    assert calls == {"chunk-000.ogg": 3, "chunk-001.ogg": 1}
    assert not (checkpoint_dir / "chunk-000.txt").exists()
    assert (checkpoint_dir / "chunk-001.txt").read_text() == "saved-success"

    calls.clear()
    with pytest.raises(TransientProviderError):
        await provider._transcribe_chunks(
            chunks, AsrOptions(timeout_s=150), checkpoint_dir
        )
    assert calls == {"chunk-000.ogg": 3}


@pytest.mark.asyncio
async def test_terminal_error_takes_priority_over_lower_chunk_timeout(
    tmp_path, monkeypatch
):
    chunks = [tmp_path / "chunk-000.ogg", tmp_path / "chunk-001.ogg"]
    for chunk in chunks:
        chunk.write_bytes(b"audio")
    provider = DoubaoAucAsrProvider(
        DoubaoAucConfig("tos-ak", "tos-sk", "private-bucket")
    )

    async def mixed_failures(chunk, _format, _options):
        if chunk.name == "chunk-000.ogg":
            raise TransientProviderError("still processing", provider="doubao_auc")
        raise TerminalProviderError("unsupported audio", provider="doubao_auc")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(provider, "_transcribe_one", mixed_failures)
    monkeypatch.setattr(provider, "_retry_sleep", no_sleep)

    with pytest.raises(TerminalProviderError, match="chunk 1 .*unsupported audio"):
        await provider._transcribe_chunks(
            chunks, AsrOptions(timeout_s=150), tmp_path / "checkpoints"
        )


@pytest.mark.parametrize(
    ("http_status", "provider_status"),
    [
        (408, None),
        (429, None),
        (503, "45000001"),
        (200, "45000131"),
        (200, "55000031"),
    ],
)
def test_retryable_http_and_provider_statuses_are_transient(
    http_status, provider_status
):
    headers = {"X-Api-Message": "busy"}
    if provider_status:
        headers["X-Api-Status-Code"] = provider_status
    response = httpx.Response(http_status, headers=headers)

    with pytest.raises(TransientProviderError):
        _raise_provider_error(response, operation="query")


def test_invalid_request_status_is_terminal():
    response = httpx.Response(
        400,
        headers={
            "X-Api-Status-Code": "45000001",
            "X-Api-Message": "invalid request",
        },
    )

    with pytest.raises(TerminalProviderError):
        _raise_provider_error(response, operation="submit")


@pytest.mark.asyncio
async def test_queued_query_continues_and_language_is_submitted(tmp_path, monkeypatch):
    audio = tmp_path / "audio.ogg"
    audio.write_bytes(b"audio")
    requests = []
    query_count = 0

    def handler(request: httpx.Request):
        nonlocal query_count
        requests.append(request)
        if request.url.path.endswith("/submit"):
            return httpx.Response(200, headers={"X-Api-Status-Code": "20000000"})
        query_count += 1
        if query_count == 1:
            return httpx.Response(200, headers={"X-Api-Status-Code": "20000002"})
        return httpx.Response(
            200,
            headers={"X-Api-Status-Code": "20000000"},
            json={"result": {"text": "完成"}},
        )

    class FakeS3:
        def put_object(self, **_kwargs):
            return None

        def generate_presigned_url(self, *_args, **_kwargs):
            return "https://example.test/audio"

        def delete_object(self, **_kwargs):
            return None

    async def no_sleep(_delay):
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DoubaoAucAsrProvider(
            DoubaoAucConfig("tos-ak", "tos-sk", "private-bucket", api_key="key"),
            client,
        )
        provider._s3 = FakeS3()
        monkeypatch.setattr("by2kb.providers.asr_doubao_auc.asyncio.sleep", no_sleep)
        text = await provider._transcribe_one(
            audio, "ogg", AsrOptions(timeout_s=10, language="zh-CN")
        )

    assert text == "完成"
    assert query_count == 2
    payload = json.loads(requests[0].content)
    assert payload["audio"]["language"] == "zh-CN"


@pytest.mark.asyncio
async def test_retry_backoff_releases_concurrency_permit(tmp_path, monkeypatch):
    chunks = [tmp_path / f"chunk-{index:03d}.ogg" for index in range(3)]
    for chunk in chunks:
        chunk.write_bytes(b"audio")
    provider = DoubaoAucAsrProvider(
        DoubaoAucConfig("tos-ak", "tos-sk", "private-bucket")
    )
    attempts: dict[str, int] = {}
    third_started = asyncio.Event()

    async def fake_transcribe(chunk, _format, _options):
        attempts[chunk.name] = attempts.get(chunk.name, 0) + 1
        if chunk.name == "chunk-002.ogg":
            third_started.set()
            return "third"
        if attempts[chunk.name] == 1:
            raise TransientProviderError("retry", provider="doubao_auc")
        return chunk.stem

    async def wait_for_third(_delay):
        await asyncio.wait_for(third_started.wait(), timeout=1)

    monkeypatch.setattr(provider, "_transcribe_one", fake_transcribe)
    monkeypatch.setattr(provider, "_retry_sleep", wait_for_third)

    result = await provider._transcribe_chunks(
        chunks, AsrOptions(), tmp_path / "checkpoints"
    )
    assert result == "chunk-000chunk-001third"


@pytest.mark.asyncio
async def test_concurrent_checkpoint_writers_use_unique_temp_files(tmp_path, monkeypatch):
    chunk = tmp_path / "chunk-000.ogg"
    chunk.write_bytes(b"audio")
    checkpoint_dir = tmp_path / "checkpoints"
    provider = DoubaoAucAsrProvider(
        DoubaoAucConfig("tos-ak", "tos-sk", "private-bucket")
    )
    both_started = asyncio.Event()
    calls = 0

    async def synchronized_success(*_args):
        nonlocal calls
        calls += 1
        if calls == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        return "same-text"

    monkeypatch.setattr(provider, "_transcribe_one", synchronized_success)
    first, second = await asyncio.gather(
        provider._transcribe_chunks([chunk], AsrOptions(), checkpoint_dir),
        provider._transcribe_chunks([chunk], AsrOptions(), checkpoint_dir),
    )

    assert first == second == "same-text"
    assert (checkpoint_dir / "chunk-000.txt").read_text() == "same-text"
    assert not list(checkpoint_dir.glob("*.tmp"))


def test_checkpoint_key_changes_with_source_and_request_schema(tmp_path, monkeypatch):
    source = tmp_path / "audio.m4a"
    source.write_bytes(b"first")
    provider = DoubaoAucAsrProvider(
        DoubaoAucConfig("tos-ak", "tos-sk", "private-bucket")
    )
    options = AsrOptions(language="zh-CN")
    original = provider._checkpoint_dir(source, options)

    source.write_bytes(b"second")
    changed_source = provider._checkpoint_dir(source, options)
    monkeypatch.setattr("by2kb.providers.asr_doubao_auc.ENABLE_PUNC", False)
    changed_settings = provider._checkpoint_dir(source, options)

    assert original != changed_source
    assert changed_source != changed_settings


@pytest.mark.asyncio
async def test_large_source_is_allowed_when_it_will_be_chunked(tmp_path, monkeypatch):
    source = tmp_path / "large.m4a"
    with source.open("wb") as handle:
        handle.truncate(26 * 1024 * 1024)
    provider = DoubaoAucAsrProvider(
        DoubaoAucConfig("tos-ak", "tos-sk", "private-bucket")
    )

    async def fake_chunked(_path, _options):
        return "chunked"

    monkeypatch.setattr(provider, "_transcribe_chunked", fake_chunked)
    result = await provider.transcribe(
        LocalAudio(
            path=source,
            format="mp4",
            duration_s=120,
            size_bytes=source.stat().st_size,
        ),
        AsrOptions(),
    )
    assert result.text == "chunked"


@pytest.mark.asyncio
async def test_individual_submission_still_enforces_size_limit(tmp_path):
    source = tmp_path / "large.ogg"
    with source.open("wb") as handle:
        handle.truncate(26 * 1024 * 1024)
    provider = DoubaoAucAsrProvider(
        DoubaoAucConfig("tos-ak", "tos-sk", "private-bucket")
    )

    with pytest.raises(TerminalProviderError, match="exceeds 25 MiB"):
        await provider._transcribe_one(source, "ogg", AsrOptions())


@pytest.mark.asyncio
async def test_checkpoint_symlink_is_rejected(tmp_path):
    chunk = tmp_path / "chunk-000.ogg"
    chunk.write_bytes(b"audio")
    target = tmp_path / "target"
    target.mkdir()
    checkpoint_dir = tmp_path / "checkpoints"
    try:
        checkpoint_dir.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is not available")
        raise
    provider = DoubaoAucAsrProvider(
        DoubaoAucConfig("tos-ak", "tos-sk", "private-bucket")
    )

    with pytest.raises(TerminalProviderError, match="unsafe ASR checkpoint symlink"):
        await provider._transcribe_chunks([chunk], AsrOptions(), checkpoint_dir)
