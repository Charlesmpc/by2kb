from __future__ import annotations

import pytest

from by2kb.errors import TerminalProviderError, TransientProviderError
from by2kb.providers.asr import AsrOptions
from by2kb.providers.asr_doubao_auc import (
    DoubaoAucAsrProvider,
    DoubaoAucConfig,
    _headers,
)


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
    monkeypatch.setattr("by2kb.providers.asr_doubao_auc.asyncio.sleep", no_sleep)

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
    monkeypatch.setattr("by2kb.providers.asr_doubao_auc.asyncio.sleep", no_sleep)

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
    monkeypatch.setattr("by2kb.providers.asr_doubao_auc.asyncio.sleep", no_sleep)

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
    monkeypatch.setattr("by2kb.providers.asr_doubao_auc.asyncio.sleep", no_sleep)

    with pytest.raises(TerminalProviderError, match="chunk 1 .*unsupported audio"):
        await provider._transcribe_chunks(
            chunks, AsrOptions(timeout_s=150), tmp_path / "checkpoints"
        )
