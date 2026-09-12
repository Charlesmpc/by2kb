import asyncio

async def test_native_missing_stream_is_recoverable(tmp_path):
    from by2kb.errors import TransientProviderError
    from by2kb.providers.base import FetchOptions
    from unittest.mock import AsyncMock
    keys = AsyncMock()
    keys.get_keys.return_value = ('a' * 32, 'b' * 32)
    info = bilibili.BilibiliVideoInfo(bvid='BV1Pyta66EDh', aid=0, cid=42, title='', author='', duration_s=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'code': 0, 'data': {}})
    )) as client:
        with pytest.raises(TransientProviderError):
            await bilibili.BilibiliMediaProvider(client, keys, tmp_path).fetch_audio(
                bilibili.resolve(info.bvid), FetchOptions(), info=info)

async def test_pagelist_transport_failure_is_recoverable():
    from by2kb.errors import TransientProviderError
    def handler(request):
        if request.url.path.endswith('/view'):
            return httpx.Response(412)
        raise httpx.ReadError('connection lost')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TransientProviderError):
            await bilibili.fetch_video_info(client, 'BV1Pyta66EDh')

async def test_cancel_check_interrupts_hung_provider(tmp_path):
    from by2kb.providers.acquisition import bounded_prepare
    from by2kb.errors import JobCancelled
    started = asyncio.Event()
    class Native:
        name = 'native'
        @bounded_prepare
        async def prepare(self, *, set_stage, cancel_check):
            started.set()
            await asyncio.Event().wait()
    def cancel():
        if started.is_set():
            raise JobCancelled('stop')
    with pytest.raises(JobCancelled):
        await asyncio.wait_for(Native().prepare(set_stage=lambda s: None, cancel_check=cancel), 0.7)

async def test_download_idle_tracks_bytes_not_256k_buffer(tmp_path):
    from by2kb.providers.acquisition import download
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(5):
                await asyncio.sleep(0.008)
                yield b'x'
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Slow())
    )) as client:
        await download(client, ['https://cdn.test/a'], tmp_path / 'audio', {}, idle_s=0.025, total_s=0.5)
    assert (tmp_path / 'audio').read_bytes() == b'xxxxx'

async def test_probe_cancellation_reaps_subprocess(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from by2kb.providers.local_media import probe_duration
    process = MagicMock()
    async def hung():
        await asyncio.Event().wait()
    process.communicate = hung
    process.wait = AsyncMock()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await probe_duration(tmp_path / 'audio')
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()

async def test_shared_download_budget_is_not_reset_by_fallback(tmp_path, monkeypatch):
    from by2kb.providers import acquisition
    from by2kb.providers.source_fallback import FallbackSourceProvider
    from by2kb.errors import TransientProviderError
    from unittest.mock import AsyncMock
    monkeypatch.setattr(acquisition, 'DOWNLOAD_S', 0.04, raising=False)
    native, browser = AsyncMock(), AsyncMock()
    native.name, browser.name = 'native', 'browser'
    async def first(*args, set_stage, **kwargs):
        set_stage('downloading_media')
        await asyncio.sleep(0.03)
        raise TransientProviderError('CDN unavailable')
    async def second(*args, set_stage, **kwargs):
        set_stage('browser_connecting')
        set_stage('downloading_media')
        await asyncio.sleep(0.03)
        from by2kb.providers.base import PreparedSource, LocalAudio
        return PreparedSource(title='test', author='', duration_s=None, source_payload={},
                              audio=LocalAudio(path=tmp_path / 'audio', format='mp4', size_bytes=5))
    native.prepare.side_effect = first
    browser.prepare.side_effect = second
    with pytest.raises(TransientProviderError, match='timed out'):
        await FallbackSourceProvider(native, browser, '').prepare(
            None, None, tmp_path, None, set_stage=lambda s: None, cancel_check=lambda: None)

async def test_download_rejects_short_content_length(tmp_path):
    from by2kb.providers.acquisition import download
    from by2kb.errors import TransientProviderError
    target = tmp_path / 'audio'
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b'ab', headers={'content-length': '100'})
    )) as client:
        with pytest.raises(TransientProviderError):
            await download(client, ['https://cdn.test/a'], target, {})
    assert not target.exists()
import httpx
import pytest
from by2kb.providers import bilibili
from by2kb.errors import UnsupportedUrl

async def test_short_redirect_chain_is_validated():
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={'location': '/two' if len(seen) == 1 else 'https://www.bilibili.com/video/BV1Pyta66EDh/'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await bilibili.expand_short_url(client, 'https://b23.tv/one')
    assert result == 'https://www.bilibili.com/video/BV1Pyta66EDh/'
    assert len(seen) == 2

async def test_view_412_uses_pagelist_without_invented_metadata():
    def handler(request):
        if request.url.path.endswith('/view'):
            return httpx.Response(412)
        assert request.url.path.endswith('/pagelist')
        return httpx.Response(200, json={'code': 0, 'data': [{'cid': 42, 'page': 1, 'duration': 70, 'part': 'not a title'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        info = await bilibili.fetch_video_info(client, 'BV1Pyta66EDh')
    assert (info.cid, info.title, info.author, info.aid) == (42, '', '', 0)
    assert info.metadata_source == 'pagelist_after_view_412'

async def test_browser_hung_evaluate_is_bounded(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock
    from by2kb.providers.browser_source import BrowserSourceProvider, BrowserConfig
    from by2kb.providers.base import FetchOptions
    from by2kb.errors import TransientProviderError
    page = MagicMock()
    page.close = AsyncMock()
    async def hung(*args):
        await asyncio.Event().wait()
    page.evaluate = hung
    context = MagicMock(new_page=AsyncMock(return_value=page))
    @asynccontextmanager
    async def fake_context(config):
        yield context
    monkeypatch.setattr('by2kb.providers.browser_source.browser_context', fake_context)
    provider = BrowserSourceProvider(BrowserConfig(tmp_path, timeout_s=0.02))
    provider._navigate = AsyncMock()
    stages = []
    with pytest.raises(TransientProviderError, match='timed out'):
        await asyncio.wait_for(provider.prepare(bilibili.resolve('BV1Pyta66EDh'), None, tmp_path,
            FetchOptions(), set_stage=stages.append, cancel_check=lambda: None), 0.5)
    assert stages == ['browser_connecting', 'browser_loading', 'waiting_media']
    page.close.assert_awaited_once()

async def test_download_trickle_has_shared_total_deadline(tmp_path):
    from by2kb.providers.acquisition import download
    from by2kb.errors import TransientProviderError
    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.005)
                yield b'x'
    def handler(request):
        return httpx.Response(200, stream=Trickle())
    target = tmp_path / 'audio'
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TransientProviderError, match='timed out'):
            await asyncio.wait_for(download(client, ['https://cdn.test/a', 'https://cdn.test/b'],
                target, {}, total_s=0.04, idle_s=0.02), 0.5)
    assert not target.exists()

async def test_native_prepare_checkpoints_metadata_and_separates_stages(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from by2kb.providers.source_bilibili import BilibiliSourceProvider
    from by2kb.providers.base import FetchOptions, LocalAudio
    info = bilibili.BilibiliVideoInfo(bvid='BV1Pyta66EDh', aid=0, cid=42, title='', author='', duration_s=10)
    monkeypatch.setattr(bilibili, 'fetch_video_info', AsyncMock(return_value=info))
    async def audio(self, identity, options, *, info):
        self.set_stage('downloading_media')
        self.set_stage('validating_media')
        return LocalAudio(path=tmp_path / 'audio', format='mp4', size_bytes=10)
    monkeypatch.setattr(bilibili.BilibiliMediaProvider, 'fetch_audio', audio)
    stages = []
    await BilibiliSourceProvider().prepare(bilibili.resolve(info.bvid), None, tmp_path, FetchOptions(),
        set_stage=stages.append, cancel_check=lambda: None)
    assert stages == ['fetching_metadata', 'downloading_media', 'validating_media']
    import json
    assert json.loads((tmp_path / 'metadata.json').read_text())['cid'] == 42

async def test_fallback_short_resolution_never_opens_browser(tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError('resolution must not open browser')
    monkeypatch.setattr('by2kb.providers.browser_source.browser_context', forbidden)
    from by2kb.providers.source_bilibili import BilibiliSourceProvider
    from by2kb.providers.browser_source import BrowserSourceProvider, BrowserConfig
    from by2kb.providers.source_fallback import FallbackSourceProvider
    def handler(request):
        return httpx.Response(302, headers={'location': 'https://www.bilibili.com/video/BV1Pyta66EDh/'})
    browser = BrowserSourceProvider(BrowserConfig(tmp_path))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        identity = await browser.resolve('https://b23.tv/one', client)
    assert identity.video_id == 'BV1Pyta66EDh'

async def test_shared_discovery_budget_bounds_failed_fallback(tmp_path, monkeypatch):
    from by2kb.providers import acquisition
    from by2kb.providers.source_fallback import FallbackSourceProvider
    from by2kb.providers.base import FetchOptions
    from by2kb.errors import TransientProviderError
    from unittest.mock import AsyncMock
    monkeypatch.setattr(acquisition, 'DISCOVERY_S', 0.03, raising=False)
    native, browser = AsyncMock(), AsyncMock()
    native.name, browser.name = 'native', 'browser'
    async def failed(*args, **kwargs):
        await asyncio.sleep(0.02)
        raise TransientProviderError('blocked')
    async def hung(*args, **kwargs):
        await asyncio.Event().wait()
    native.prepare.side_effect = failed
    browser.prepare.side_effect = hung
    with pytest.raises(TransientProviderError, match='timed out'):
        await asyncio.wait_for(FallbackSourceProvider(native, browser, '').prepare(
            bilibili.resolve('BV1Pyta66EDh'), None, tmp_path, FetchOptions(),
            set_stage=lambda s: None, cancel_check=lambda: None), 0.3)
