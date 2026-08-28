# Source providers

URL acquisition is selected independently from ASR, enrichment, and knowledge-base
storage. Configure a deterministic provider order in `config.toml`:

```toml
[sources]
providers = ["bilibili_native", "yt_dlp"]

[sources.yt_dlp]
enabled = true
subtitle_policy = "prefer"       # prefer | manual_only | disabled
playlist_policy = "reject"
cookie_file = ""
cookies_from_browser = ""
```

The default remains `bilibili_native` only, keeping a basic installation lightweight.
Enable `yt_dlp` explicitly when broader URL support is wanted:

```bash
pipx inject by2kb "yt-dlp>=2025.1.15"
by2kb doctor
```

For a new installation, the equivalent package extra is
`pipx install "by2kb[source-ytdlp]"` (or `by2kb[youtube]`).

## Selection and fallback

Providers are tried in configured order and only when they declare that they support
the input. Keeping `bilibili_native` first preserves its platform-specific signing,
error handling, and provenance. `yt_dlp` is the generic extension provider.

A provider returns exactly one of:

- a normalized caption transcript; or
- a local audio file for the configured ASR provider.

With `subtitle_policy = "prefer"`, yt-dlp selects a manual caption in
`preferred_languages`, then an automatic caption. If neither is usable, it downloads
the best available audio and the normal `[asr]` selection takes over. `manual_only`
does not select automatic captions; `disabled` always takes the audio path.

Playlists and multi-video sources are rejected. One submitted URL can never silently
create unbounded work.

## Authentication and privacy

Cookies are opt-in. Configure either `cookie_file` or `cookies_from_browser`, never
both. by2kb passes the selected option to yt-dlp but does not copy cookie contents into
the job database, logs, or knowledge artifacts. `doctor` checks a configured cookie
file for readability without printing its contents.

Public provenance records the source provider/version, extractor, selected caption
language and kind, or the fact that audio fallback was used. Temporary caption URLs,
media URLs, request headers, and cookies are not published.

## Opt-in live verification

Live extraction is intentionally not part of the unit suite. After enabling yt-dlp,
use one public, single-video URL that has captions and run:

```bash
by2kb ingest "https://www.youtube.com/watch?v=<video-id>" \
  --enricher disabled --json
```

Confirm that the result is terminal-success, the path is
`library/youtube/<video-id>/`, `source.json` reports `route: subtitle`, and
`transcript.json` contains timestamped segments. For the audio route, use a public
single video without captions and confirm `route: audio_fallback` plus the configured
ASR provider provenance. Keep both checks bounded to one URL; playlist and channel
URLs must fail before media download.

## Provider contract

The URL runner consumes a `SourceProviderRegistry`; it contains no platform-specific
prepare branch. A provider is responsible for capability detection, canonical source
identity, safe metadata, and either transcript or audio preparation. The rest of the
pipeline—quality assessment, optional ASR, enrichment, task control, and publishing—is
shared.
