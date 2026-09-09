from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from by2kb.errors import ConfigError, NeedsAuth, RateLimited, JobCancelled, TerminalProviderError, TransientProviderError
from by2kb.providers.base import PreparedSource, LocalAudio, FetchOptions
from by2kb.providers.bilibili import resolve
from by2kb.providers.browser_source import BrowserConfig, BrowserSourceProvider, audio_urls, supported_url, missing_media_error
from by2kb.providers.source_fallback import FallbackSourceProvider

BVID = "BV1Pyta66EDh"


def provider(name):
    obj = AsyncMock()
    obj.name = name
    return obj


def prepared(tmp_path):
    return PreparedSource(title=BVID, author="", duration_s=10, source_payload={"source_provider": "browser"},
        audio=LocalAudio(path=tmp_path / "audio.m4a", format="mp4", size_bytes=100))


async def run_prepare(chain, tmp_path, cancel=lambda: None):
    return await chain.prepare(resolve(BVID), None, tmp_path, FetchOptions(), set_stage=lambda _: None, cancel_check=cancel)


async def test_fallback_after_metadata_block(tmp_path):
    native, browser = provider("native"), provider("browser")
    native.prepare.side_effect = RateLimited("signed-secret-url")
    browser.prepare.return_value = prepared(tmp_path)
    result = await run_prepare(FallbackSourceProvider(native, browser, BVID), tmp_path)
    assert result.source_payload["fallback_attempts"] == [{"provider": "native", "category": "RateLimited"}]
    assert "secret" not in str(result.source_payload)
    browser.prepare.assert_awaited_once()


async def test_fallback_includes_short_link_resolution(tmp_path):
    native, browser = provider("native"), provider("browser")
    native.resolve.side_effect = TransientProviderError("412")
    browser.resolve.return_value = resolve(BVID)
    browser.prepare.return_value = prepared(tmp_path)
    chain = FallbackSourceProvider(native, browser, "https://b23.tv/abc")
    assert (await chain.resolve(chain.source, None)).video_id == BVID
    await run_prepare(chain, tmp_path)
    native.prepare.assert_not_awaited()


@pytest.mark.parametrize("error", [TerminalProviderError("deleted"), JobCancelled("stop"), ConfigError("invalid")])
async def test_no_fallback_for_terminal_cancel_or_config(tmp_path, error):
    native, browser = provider("native"), provider("browser")
    native.prepare.side_effect = error
    with pytest.raises(type(error)):
        await run_prepare(FallbackSourceProvider(native, browser, BVID), tmp_path)
    browser.prepare.assert_not_awaited()


async def test_both_fail_preserves_auth_and_attempts(tmp_path):
    native, browser = provider("native"), provider("browser")
    native.prepare.side_effect = RateLimited("412")
    browser.prepare.side_effect = NeedsAuth("log in")
    with pytest.raises(NeedsAuth) as caught:
        await run_prepare(FallbackSourceProvider(native, browser, BVID), tmp_path)
    assert caught.value.exit_code == 3
    assert len(caught.value.detail["attempts"]) == 2
    browser.prepare.assert_awaited_once()


async def test_success_does_not_launch_fallback(tmp_path):
    native, browser = provider("native"), provider("browser")
    native.prepare.return_value = prepared(tmp_path)
    await run_prepare(FallbackSourceProvider(native, browser, BVID), tmp_path)
    browser.prepare.assert_not_awaited()


@pytest.mark.parametrize("mapping", [{"profile": "../cookies"}, {"headless": "false"}, {"timeout_s": 0}, {"cdp_url": "http://example.com:9222"}, {"cdp_url": "http://user:pass@127.0.0.1:9222"}, {"platforms": ["youtube"]}])
def test_invalid_config(mapping, tmp_path):
    with pytest.raises(ConfigError):
        BrowserConfig.from_mapping(mapping, home=tmp_path)


def test_profile_outside_installation(tmp_path):
    config = BrowserConfig.from_mapping({}, home=tmp_path)
    assert config.profile_dir == tmp_path / "browser" / "bilibili"
    assert not config.headless


@pytest.mark.parametrize("url", ["https://evil.test/bilibili.com", "https://bilibili.com.evil.test", "file:///tmp/video", "http://localhost", "https://u:p@b23.tv/x"])
def test_browser_rejects_unrelated_urls(url):
    assert not supported_url(url)


def test_cdn_allowlist_and_backup():
    payload = {"data": {"dash": {"audio": [{"bandwidth": 10, "baseUrl": "https://cdn.bilivideo.com/audio?token=private", "backupUrl": ["http://127.0.0.1/secret", "https://bilivideo.com.evil.test/x", "https://backup.bilivideo.cn/x"]}]}}}
    assert audio_urls(payload) == ["https://cdn.bilivideo.com/audio?token=private", "https://backup.bilivideo.cn/x"]


@pytest.mark.parametrize("text, cls", [("扫码登录", NeedsAuth), ("完成验证", NeedsAuth), ("412", RateLimited), ("视频已被删除", TerminalProviderError), ("", TransientProviderError)])
def test_human_readable_failures(text, cls):
    assert isinstance(missing_media_error(text, []), cls)


async def test_download_requires_full_response_and_uses_backup(tmp_path, monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(206 if len(seen) == 1 else 200, content=b"audio")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("by2kb.providers.browser_source.httpx.AsyncClient", lambda **kwargs: client)
    obj = BrowserSourceProvider(BrowserConfig.from_mapping({}, home=tmp_path))
    target = tmp_path / "audio.m4a"
    await obj._download(["https://a.bilivideo.com/x", "https://b.bilivideo.com/x"], target, "UA", "https://www.bilibili.com/", lambda: None)
    assert target.read_bytes() == b"audio"
    assert len(seen) == 2
    assert all("cookie" not in r.headers for r in seen)


@pytest.mark.parametrize("meta, expected_error", [({}, None), ({"duration": 100}, NeedsAuth), ({"bvid": "BV1jmbD65EP2"}, TerminalProviderError), ({"pages": [{}, {}]}, TerminalProviderError)])
async def test_browser_prepare_metadata_optional_and_identity_guard(tmp_path, monkeypatch, meta, expected_error):
    from unittest.mock import MagicMock
    page = MagicMock()
    page.close = AsyncMock()
    page.evaluate = AsyncMock(side_effect=[{
        "meta": meta, "title": "",
        "play": {"data": {"dash": {"audio": [{"baseUrl": "https://a.bilivideo.com/audio"}]}}},
    }, "UA"])
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    @asynccontextmanager
    async def fake_context(config):
        yield context
    monkeypatch.setattr("by2kb.providers.browser_source.browser_context", fake_context)
    monkeypatch.setattr("by2kb.providers.browser_source.probe_duration", AsyncMock(return_value=10))
    decoder = AsyncMock()
    monkeypatch.setattr("by2kb.providers.browser_source.validate_audio", decoder)
    obj = BrowserSourceProvider(BrowserConfig.from_mapping({}, home=tmp_path))
    obj._navigate = AsyncMock()
    async def download(urls, path, *args):
        path.write_bytes(b"audio")
    obj._download = download
    if expected_error:
        with pytest.raises(expected_error):
            await run_prepare(obj, tmp_path)
        decoder.assert_not_awaited()
    else:
        result = await run_prepare(obj, tmp_path)
        assert result.title == BVID
        assert result.author == ""
        assert result.source_payload["warnings"]
        decoder.assert_awaited_once()
    page.close.assert_awaited_once()


async def test_cancellation_before_fallback(tmp_path):
    native, browser = provider("native"), provider("browser")
    native.prepare.side_effect = RateLimited("412")
    def cancel():
        raise JobCancelled("stop")
    with pytest.raises(JobCancelled):
        await run_prepare(FallbackSourceProvider(native, browser, BVID), tmp_path, cancel)
    browser.prepare.assert_not_awaited()


def test_config_and_doctor_browser_fallback(tmp_path):
    from by2kb.config import load_config
    from by2kb.doctor import _source_checks
    (tmp_path / "config.toml").write_text('[sources.fallback]\nprovider = "browser"\n[sources.browser]\ncdp_url = "http://127.0.0.1:9225"\n')
    config = load_config(tmp_path)
    assert config.sources.options["fallback"]["provider"] == "browser"
    checks = _source_checks(config)
    assert any(c.id == "source_browser_dependency" for c in checks)


def test_failure_envelope_has_actions(tmp_path):
    from by2kb.jobs.runner import _failure
    from by2kb.jobs.store import JobStore
    store = JobStore(tmp_path / "jobs.db")
    try:
        error = NeedsAuth("Login then retry", detail={"attempts": [{"provider": "browser", "category": "NeedsAuth"}]})
        result = _failure(store, None, error).to_dict()
        assert result["exit_code"] == 3
        assert result["error"]["requires_user_action"] is True
        assert len(result["error"]["attempts"]) == 1
    finally:
        store.close()
