import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from by2kb.errors import NeedsAuth, RateLimited, TransientProviderError
from by2kb.providers.base import FetchOptions
from by2kb.providers.bilibili import resolve
from by2kb.providers.browser_source import BrowserConfig, BrowserSourceProvider


@pytest.mark.parametrize('text,error', [('登录后观看', NeedsAuth), ('完成验证', NeedsAuth), ('请求被拦截', RateLimited)])
async def test_media_gate_is_classified_before_deadline(tmp_path, monkeypatch, text, error):
    page = MagicMock()
    page.evaluate = AsyncMock(return_value={'play': {}, 'meta': {}, 'title': '', 'text': text})
    page.close = AsyncMock()
    context = MagicMock(new_page=AsyncMock(return_value=page))
    @asynccontextmanager
    async def session(config):
        yield context
    monkeypatch.setattr('by2kb.providers.browser_source.browser_context', session)
    browser = BrowserSourceProvider(BrowserConfig(tmp_path, timeout_s=5))
    browser._navigate = AsyncMock()
    with pytest.raises(error):
        await asyncio.wait_for(browser.prepare(resolve('BV1Pyta66EDh'), None, tmp_path, FetchOptions(),
                            set_stage=lambda stage: None, cancel_check=lambda: None), timeout=2)
    page.close.assert_awaited_once()


async def test_generic_login_button_is_not_auth_gate(tmp_path, monkeypatch):
    page = MagicMock()
    page.evaluate = AsyncMock(return_value={'play': {}, 'meta': {}, 'text': '扫码登录'})
    page.close = AsyncMock()
    context = MagicMock(new_page=AsyncMock(return_value=page))
    @asynccontextmanager
    async def session(config):
        yield context
    monkeypatch.setattr('by2kb.providers.browser_source.browser_context', session)
    browser = BrowserSourceProvider(BrowserConfig(tmp_path, timeout_s=0.1))
    browser._navigate = AsyncMock()
    with pytest.raises(TransientProviderError, match='waiting_media timed out'):
        await browser.prepare(resolve('BV1Pyta66EDh'), None, tmp_path, FetchOptions(),
                              set_stage=lambda stage: None, cancel_check=lambda: None)


async def test_probe_own_timeout_is_retryable(tmp_path, monkeypatch):
    from by2kb.providers.local_media import probe_duration
    process = MagicMock()
    process.communicate = AsyncMock(side_effect=TimeoutError())
    process.wait = AsyncMock()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    with pytest.raises(TransientProviderError, match='ffprobe.*timed out') as caught:
        await probe_duration(tmp_path / 'audio')
    assert caught.value.exit_code == 2
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


async def test_probe_external_cancellation_is_not_retryable_error(tmp_path, monkeypatch):
    from by2kb.providers.local_media import probe_duration
    process = MagicMock()
    process.communicate = AsyncMock(side_effect=asyncio.CancelledError())
    process.wait = AsyncMock()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    with pytest.raises(asyncio.CancelledError):
        await probe_duration(tmp_path / 'audio')
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


async def test_deadline_is_shared_and_context_is_restored():
    from by2kb.providers.acquisition import bounded_prepare, remaining_acquisition_s
    class Browser:
        name = 'browser'
        config = MagicMock(timeout_s=60)
        @bounded_prepare
        async def prepare(self, *, set_stage, cancel_check):
            first = remaining_acquisition_s()
            await asyncio.sleep(0.05)
            set_stage('waiting_media')
            second = remaining_acquisition_s()
            assert second < first
            return second
    class Outer:
        name = 'browser'
        config = MagicMock(timeout_s=5)
        @bounded_prepare
        async def prepare(self, *, set_stage, cancel_check):
            return await Browser().prepare(set_stage=set_stage, cancel_check=cancel_check)
    assert await Outer().prepare(set_stage=lambda stage: None, cancel_check=lambda: None) <= 5
    assert remaining_acquisition_s() == float('inf')
