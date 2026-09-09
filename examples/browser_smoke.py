"""Opt-in live acquisition test (downloads ONE video's audio, no ASR/LLM).

Run in an environment with by2kb[browser] and ffmpeg installed. The CDP endpoint
must belong to a dedicated browser you have explicitly logged into. Never publish
the profile, media URLs or browser diagnostics. No existing config is modified.
"""
import argparse
import asyncio
import json
from pathlib import Path

import httpx

from by2kb.errors import By2kbError
from by2kb.providers.base import FetchOptions
from by2kb.providers.browser_source import BrowserConfig, BrowserSourceProvider
from by2kb.providers.source_bilibili import BilibiliSourceProvider
from by2kb.providers.source_fallback import FallbackSourceProvider


async def main(args):
    directory = Path(args.work_dir)
    directory.mkdir(parents=True, exist_ok=True)
    browser = BrowserSourceProvider(BrowserConfig.from_mapping({"cdp_url": args.cdp_url}, home=directory))
    provider = FallbackSourceProvider(BilibiliSourceProvider(), browser, args.url)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        identity = await provider.resolve(args.url, client)
        result = await provider.prepare(identity, client, directory, FetchOptions(),
            set_stage=lambda stage: print(stage, flush=True), cancel_check=lambda: None)
    print(json.dumps({"video_id": identity.video_id, "duration_s": result.duration_s,
        "audio_bytes": result.audio.size_bytes, "provenance": result.source_payload}, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--cdp-url", required=True)
    parser.add_argument("--work-dir", required=True)
    try:
        asyncio.run(main(parser.parse_args()))
    except By2kbError as error:
        print(json.dumps({"category": type(error).__name__, "message": str(error)}))
        raise SystemExit(error.exit_code)
