from __future__ import annotations

from pathlib import Path

import pytest

from by2kb.errors import TerminalProviderError
from by2kb.providers.asr import AsrOptions
from by2kb.providers.asr_doubao_auc import DoubaoAucConfig, _headers
from by2kb.providers.asr_doubao_auc import DoubaoAucAsrProvider
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
async def test_large_long_audio_is_chunked_before_single_upload_limit(tmp_path, monkeypatch):
    audio_path = tmp_path / "long.m4a"
    with audio_path.open("wb") as fh:
        fh.truncate(26 * 1024 * 1024)
    provider = DoubaoAucAsrProvider(
        DoubaoAucConfig("ak", "sk", "bucket", api_key="key")
    )
    called = False

    async def fake_chunked(path: Path, options: AsrOptions) -> str:
        nonlocal called
        called = True
        return "transcript"

    monkeypatch.setattr(provider, "_transcribe_chunked", fake_chunked)
    result = await provider.transcribe(
        LocalAudio(
            path=audio_path,
            format="mp4",
            duration_s=600,
            size_bytes=audio_path.stat().st_size,
        ),
        AsrOptions(timeout_s=900),
    )

    assert called is True
    assert result.text == "transcript"


@pytest.mark.asyncio
async def test_large_short_audio_still_enforces_single_upload_limit(tmp_path):
    audio_path = tmp_path / "short.m4a"
    with audio_path.open("wb") as fh:
        fh.truncate(26 * 1024 * 1024)
    provider = DoubaoAucAsrProvider(
        DoubaoAucConfig("ak", "sk", "bucket", api_key="key")
    )

    with pytest.raises(TerminalProviderError, match="exceeds 25 MiB"):
        await provider.transcribe(
            LocalAudio(
                path=audio_path,
                format="mp4",
                duration_s=60,
                size_bytes=audio_path.stat().st_size,
            ),
            AsrOptions(timeout_s=300),
        )


@pytest.mark.asyncio
async def test_chunk_timeout_allows_slow_doubao_queue(tmp_path, monkeypatch):
    source = tmp_path / "source.m4a"
    source.write_bytes(b"audio")
    observed_timeouts: list[float] = []
    provider = DoubaoAucAsrProvider(
        DoubaoAucConfig("ak", "sk", "bucket", api_key="key")
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_subprocess(*args, **kwargs):
        pattern = Path(args[-1])
        Path(str(pattern).replace("%03d", "000")).write_bytes(b"chunk")
        return FakeProcess()

    async def fake_one(path: Path, audio_format: str, options: AsrOptions) -> str:
        observed_timeouts.append(options.timeout_s)
        return "done"

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(provider, "_transcribe_one", fake_one)

    text = await provider._transcribe_chunked(source, AsrOptions(timeout_s=540))

    assert text == "done"
    assert observed_timeouts == [300.0]
