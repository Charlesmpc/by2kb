"""Opt-in Bilibili acquisition through a dedicated, user-authenticated browser.

No CAPTCHA solving, credential export, or access-control bypass. Browser state and
media URLs stay private. A successfully loaded page is not a successful acquisition.
"""
from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx

from by2kb.errors import ConfigError, NeedsAuth, RateLimited, TerminalProviderError, TransientProviderError
from by2kb.providers.base import LocalAudio, PreparedSource
from by2kb.providers.acquisition import bounded_prepare, cleanup
from by2kb.providers.bilibili import resolve
from by2kb.providers.local_media import probe_duration


@dataclass(frozen=True)
class BrowserConfig:
    profile_dir: Path
    headless: bool = False
    timeout_s: float = 60
    cdp_url: str = ""
    executable_path: str = ""
    max_audio_bytes: int = 1024 * 1024 * 1024

    @classmethod
    def from_mapping(cls, values, *, home: Path):
        profile = values.get("profile", "bilibili")
        if not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile):
            raise ConfigError("browser profile must contain only letters, digits, '-' or '_'")
        headless = values.get("headless", False)
        if not isinstance(headless, bool):
            raise ConfigError("sources.browser.headless must be a boolean")
        platforms = values.get("platforms", ["bilibili"])
        if platforms != ["bilibili"]:
            raise ConfigError("browser currently supports platforms = ['bilibili'] only")
        try:
            timeout = float(values.get("timeout_s", 60))
            limit = int(values.get("max_audio_bytes", 1024 * 1024 * 1024))
        except (TypeError, ValueError) as exc:
            raise ConfigError("browser timeout/size limit must be numeric") from exc
        if not 5 <= timeout <= 300 or not 1024 <= limit <= 4 * 1024**3:
            raise ConfigError("browser timeout must be 5..300 seconds; audio limit 1 KiB..4 GiB")
        cdp = str(values.get("cdp_url", ""))
        if cdp:
            parsed = urlparse(cdp)
            if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ConfigError("browser cdp_url must be a local HTTP endpoint; use an SSH tunnel for remote access")
        return cls(home / "browser" / profile, headless, timeout, cdp,
                   str(values.get("executable_path", "")), limit)


@asynccontextmanager
async def browser_context(config: BrowserConfig):
    try:
        from playwright.async_api import async_playwright, Error
    except ImportError as exc:
        raise ConfigError("Browser dependency missing. Install by2kb[browser], then run 'by2kb browser install'.") from exc
    async with async_playwright() as pw:
        if config.cdp_url:
            try:
                browser = await pw.chromium.connect_over_cdp(config.cdp_url, timeout=config.timeout_s * 1000)
            except Error as exc:
                raise ConfigError("Cannot connect to browser cdp_url. Check the dedicated browser and local SSH tunnel.") from exc
            try:
                if not browser.contexts:
                    raise ConfigError("Connected browser has no persistent context")
                yield browser.contexts[0]
            finally:
                # Disconnect Playwright; do not close the user's browser/context.
                await browser.close()
        else:
            config.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                config.profile_dir.chmod(0o700)
            try:
                context = await pw.chromium.launch_persistent_context(
                    str(config.profile_dir), headless=config.headless,
                    executable_path=config.executable_path or None,
                    timeout=config.timeout_s * 1000,
                )
            except Error as exc:
                raise ConfigError("Cannot start browser. Run 'by2kb browser install'; check DISPLAY/VNC and close other browsers using this profile.") from exc
            try:
                yield context
            finally:
                await context.close()


def supported_url(source: str) -> bool:
    if re.fullmatch(r"BV[0-9A-Za-z]{10}", source):
        return True
    parsed = urlparse(source)
    return (parsed.scheme in {"http", "https"} and not parsed.username and not parsed.password
            and parsed.hostname in {"b23.tv", "www.bilibili.com", "bilibili.com", "m.bilibili.com"})


def audio_urls(playinfo: dict) -> list[str]:
    data = playinfo.get("data") or playinfo.get("result") or playinfo
    if not isinstance(data, dict):
        return []
    streams = (data.get("dash") or {}).get("audio") or []
    streams = sorted((item for item in streams if isinstance(item, dict)),
                     key=lambda item: int(item.get("bandwidth") or 0), reverse=True)
    urls = []
    for item in streams:
        for url in [item.get("baseUrl") or item.get("base_url"),
                    *(item.get("backupUrl") or item.get("backup_url") or [])]:
            if not isinstance(url, str):
                continue
            parsed = urlparse(url)
            host = parsed.hostname or ""
            if (parsed.scheme == "https" and not parsed.username and not parsed.password
                    and any(host == domain or host.endswith("." + domain)
                            for domain in ("bilivideo.com", "bilivideo.cn", "hdslb.com"))):
                urls.append(url)
    return list(dict.fromkeys(urls))


def missing_media_error(text: str, codes: list[int]):
    if any(code in {-404, 62002, 62004} for code in codes) or any(
        hint in text for hint in ("视频已失效", "视频不见了", "视频已被删除")
    ):
        return TerminalProviderError("Video is unavailable; no audio or transcript was acquired.", provider="browser")
    if any(hint in text for hint in ("登录后观看", "登录后继续", "请先登录", "安全验证", "完成验证")) or -403 in codes:
        return NeedsAuth("Browser requires login or human verification. Run 'by2kb browser login' (or use the configured VNC browser), then retry. No audio was acquired; transcription and summaries were not run.", provider="browser")
    if -352 in codes or "412" in text or "请求被拦截" in text:
        return RateLimited("Browser access was blocked. Stop repeated retries; try later or ingest a local audio/video file from a device that can access it.", provider="browser")
    return TransientProviderError("Browser did not expose a usable audio stream before timeout. Check playback in 'by2kb browser login', then retry, or ingest a local file. No transcript or summary was generated.", provider="browser")


class BrowserSourceProvider:
    name = "browser"

    def __init__(self, config: BrowserConfig):
        self.config = config

    def supports(self, source):
        return supported_url(source.strip())

    async def resolve(self, source, client):
        if not self.supports(source):
            from by2kb.errors import UnsupportedUrl
            raise UnsupportedUrl("Browser provider supports single Bilibili video URLs only")
        from by2kb.providers.source_bilibili import BilibiliSourceProvider
        return await BilibiliSourceProvider().resolve(source, client)

    async def _navigate(self, page, url):
        from playwright.async_api import Error
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=self.config.timeout_s * 1000)
        except Error as exc:
            raise TransientProviderError("Browser navigation failed. Check the browser connection and retry.", provider=self.name) from exc

    @bounded_prepare
    async def prepare(self, identity, client, work_dir, options, *, set_stage, cancel_check):
        if identity.platform != "bilibili":
            raise ConfigError("Browser fallback only supports Bilibili")
        async with browser_context(self.config) as context:
            page = await context.new_page()
            captures = []
            pending = set()

            async def capture(response):
                parsed = urlparse(response.url)
                if parsed.hostname == "api.bilibili.com" and parsed.path.endswith("/playurl"):
                    query = parse_qs(parsed.query)
                    if query.get("bvid", [identity.video_id])[0] != identity.video_id:
                        return
                    try:
                        value = await response.json()
                        if isinstance(value, dict):
                            captures.append(value)
                    except Exception:
                        pass

            def on_response(response):
                task = asyncio.create_task(capture(response))
                pending.add(task)
                task.add_done_callback(pending.discard)

            page.on("response", on_response)
            try:
                set_stage("browser_loading")
                await self._navigate(page, identity.canonical_url)
                set_stage("waiting_media")
                deadline = asyncio.get_running_loop().time() + self.config.timeout_s
                metadata = {}
                urls = []
                while asyncio.get_running_loop().time() < deadline:
                    cancel_check()
                    snapshot = await page.evaluate("""() => ({
                      play: window.__playinfo__ || {},
                      meta: (window.__INITIAL_STATE__ || {}).videoData || {},
                      title: document.querySelector('h1')?.textContent || ''
                    })""")
                    metadata = snapshot.get("meta") or {}
                    if len(metadata.get("pages") or []) > 1:
                        raise TerminalProviderError("Browser provider currently supports single-part videos only; select a local file for this multipart video.", provider=self.name)
                    if metadata.get("bvid") and metadata["bvid"] != identity.video_id:
                        raise TerminalProviderError("Browser loaded a different video; refusing to ingest it", provider=self.name)
                    urls = audio_urls(snapshot.get("play") or {})
                    for payload in reversed(captures):
                        urls.extend(audio_urls(payload))
                    if urls:
                        break
                    await asyncio.sleep(0.5)
                if not urls:
                    body = await page.locator("body").inner_text(timeout=5000)
                    raise missing_media_error(body, [p.get("code") for p in captures])
                title = str(metadata.get("title") or snapshot.get("title") or identity.video_id).strip()
                author = str((metadata.get("owner") or {}).get("name") or "")
                user_agent = await page.evaluate("navigator.userAgent")
                target = work_dir / "browser-audio.m4a"
                set_stage("downloading_media")
                await self._download(urls, target, user_agent, identity.canonical_url, cancel_check)
                set_stage("validating_media")
                try:
                    duration = await probe_duration(target)
                    expected_duration = float(metadata.get("duration") or 0)
                    if expected_duration and (duration is None or duration + 5 < expected_duration * 0.95):
                        raise TransientProviderError("Incomplete audio; acquisition failed. This alone is not evidence of a login requirement; partial audio will not be transcribed.", provider=self.name)
                    await validate_audio(target)
                except BaseException:
                    target.unlink(missing_ok=True)
                    raise
                warnings = []
                if not metadata:
                    warnings.append("metadata_unavailable: used page title or video ID; author may be empty")
                return PreparedSource(
                    title=title or identity.video_id, author=author, duration_s=duration,
                    audio=LocalAudio(path=target, format="mp4", duration_s=duration, size_bytes=target.stat().st_size),
                    source_payload={"source_provider": self.name, "route": "browser_audio", "warnings": warnings},
                )
            except (ConfigError, NeedsAuth, RateLimited, TerminalProviderError, TransientProviderError):
                raise
            except Exception as exc:
                from by2kb.errors import JobCancelled
                if isinstance(exc, JobCancelled):
                    raise
                raise TransientProviderError("Browser extraction failed. Check playback and retry; no usable media was published.", provider=self.name) from exc
            finally:
                page.remove_listener("response", on_response)
                tasks = list(pending)
                for task in tasks:
                    task.cancel()
                await cleanup(asyncio.gather(*tasks, return_exceptions=True))
                await cleanup(page.close())

    async def _download(self, urls, target, user_agent, referer, cancel_check):
        from by2kb.providers.acquisition import download
        # Signed CDN URLs are private; no browser cookies are exported.
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            await download(client, urls, target, {"User-Agent": user_agent, "Referer": referer},
                           max_bytes=self.config.max_audio_bytes, cancel_check=cancel_check)


async def validate_audio(path: Path):
    """Decode a bounded sample; reject HTML, video-only and damaged media."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-map", "0:a:0",
            "-t", "1", "-f", "null", "-", stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise ConfigError("ffmpeg is required to validate browser audio; run by2kb doctor.") from exc
    try:
        await asyncio.wait_for(process.wait(), timeout=30)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise TerminalProviderError("Browser download did not contain decodable audio; no transcription was started.", provider="browser")
