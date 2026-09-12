"""Acquisition-only deadlines; never wrap ASR or enrichment in these budgets."""
from __future__ import annotations

import asyncio
import httpx
import json
from datetime import datetime, timezone
from functools import wraps
from by2kb.errors import TransientProviderError


DISCOVERY_S = 120
DOWNLOAD_S = 600


def bounded_prepare(function):
    @wraps(function)
    async def wrapped(self, *args, set_stage, **kwargs):
        loop = asyncio.get_running_loop()
        seconds = (DISCOVERY_S if self.name == 'fallback' else
                   min(self.config.timeout_s, 60) if self.name == 'browser' else 30)
        stage = 'browser_connecting' if self.name == 'browser' else 'fetching_metadata'
        remaining_discovery = seconds
        changed = loop.time()
        download_deadline = None
        timer = asyncio.timeout(seconds)

        def update(value):
            nonlocal stage, remaining_discovery, changed, download_deadline
            now = loop.time()
            if stage not in {'downloading_media', 'validating_media'}:
                remaining_discovery -= now - changed
            stage, changed = value, now
            if value == 'downloading_media':
                if download_deadline is None:
                    download_deadline = now + DOWNLOAD_S
                deadline = download_deadline
            elif value == 'validating_media':
                deadline = now + 30
            else:
                deadline = now + max(0, remaining_discovery)
            timer.reschedule(deadline)
            set_stage(value)
        try:
            async with timer:
                if self.name != "fallback":
                    update(stage)
                kwargs['cancel_check']()
                task = asyncio.create_task(function(self, *args, set_stage=update, **kwargs))
                try:
                    while not task.done():
                        await asyncio.wait({task}, timeout=0.25)
                        kwargs['cancel_check']()
                    return task.result()
                finally:
                    if not task.done():
                        task.cancel()
                        await cleanup(task)
        except TimeoutError as exc:
            raise TransientProviderError(f'{stage} timed out', provider=self.name) from exc
    return wrapped


async def download(client, urls, target, headers, *, total_s: float = 600, idle_s: float = 30,
                   max_bytes=1024**3, cancel_check=lambda: None):
    """One deadline across all CDN attempts; an idle deadline for each read."""
    target.parent.mkdir(parents=True, exist_ok=True)
    started = asyncio.get_running_loop().time()
    complete = False
    try:
        async with asyncio.timeout(total_s):
            for url in dict.fromkeys(urls):
                cancel_check()
                try:
                    async with asyncio.timeout(idle_s):
                        manager = client.stream('GET', url, headers=headers)
                        response = await manager.__aenter__()
                    try:
                        if response.status_code != 200:
                            continue
                        size = 0
                        chunks = response.aiter_bytes(None).__aiter__()
                        with target.open('wb') as output:
                            while True:
                                try:
                                    async with asyncio.timeout(idle_s):
                                        chunk = await anext(chunks)
                                except StopAsyncIteration:
                                    break
                                cancel_check()
                                size += len(chunk)
                                if size > max_bytes:
                                    from by2kb.errors import TerminalProviderError
                                    raise TerminalProviderError('audio exceeds size limit', provider='bilibili')
                                output.write(chunk)
                                elapsed = max(asyncio.get_running_loop().time() - started, 0.001)
                                (target.parent / 'download-progress.json').write_text(json.dumps({
                                    'bytes': size, 'bytes_per_second': size / elapsed,
                                    'last_progress': datetime.now(timezone.utc).isoformat()}))
                        expected = response.headers.get('content-length')
                        if size and (expected is None or expected.isdigit() and size == int(expected)):
                            complete = True
                            return
                    finally:
                        await cleanup(manager.__aexit__(None, None, None))
                except (httpx.HTTPError, TimeoutError):
                    continue
        raise TransientProviderError('all audio CDN downloads failed', provider='bilibili')
    except TimeoutError as exc:
        raise TransientProviderError('downloading_media timed out', provider='bilibili') from exc
    finally:
        if not complete:
            target.unlink(missing_ok=True)


async def cleanup(awaitable):
    """Bound cleanup waiting; do not claim to kill cancellation-hostile tasks."""
    task = asyncio.ensure_future(awaitable)
    done, pending = await asyncio.wait({task}, timeout=1)
    for item in pending:
        item.cancel()
        item.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    for item in done:
        if not item.cancelled():
            item.exception()
