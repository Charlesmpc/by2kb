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

The first configured provider declaring support is selected. This list is not an
error-fallback chain. Keeping `bilibili_native` first preserves its platform-specific signing,
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

## Browser fallback (0.4.3, opt-in)

Use a dedicated Chromium session when native Bilibili requests fail with HTTP 412,
authentication, rate-limit or transport errors. Browser support is currently limited
to single-part Bilibili videos (not playlists or multipart collections). It is not a browser extension or an LLM/agent callback.
It does not solve CAPTCHAs or bypass video access restrictions. Logged-in playback
does not guarantee that an audio download will succeed.

Install into the same environment as by2kb:

```bash
pipx inject by2kb "playwright>=1.58,<2"
by2kb browser install
```

For a new installation, add the `browser` extra to your other selected extras, e.g.
`pipx install "by2kb[asr-whisper,youtube,browser]"`. Linux may additionally need
Chromium system libraries, a GUI/VNC display and CJK fonts. VNC setup is not automated.
Keep VNC and browser debugging ports local; use SSH tunnels, never public ports.

Add these sections to your existing config; do not replace unrelated settings:

```toml
[sources.fallback]
provider = "browser"

[sources.browser]
platforms = ["bilibili"]
profile = "bilibili"
headless = false
timeout_s = 60
```

Run `by2kb browser login` on the same host/user as the worker. On Linux export the
correct `DISPLAY` (for example `:5` for a VNC desktop). Scan the QR code manually;
close the login tab or wait for its timeout before running ingestion. Then use the
normal `by2kb ingest URL --json` or `by2kb retry JOB_ID --json` command. A 24-hour
site session remains a 24-hour session: neither install nor upgrade extends it.

By default the profile lives at `<BY2KB_HOME>/browser/bilibili`, outside the package,
and upgrades leave it untouched. Treat this directory as credentials, do not commit
or share it. Use one task per profile at a time. Do not point at your personal daily
browser. `headless = true` is available but must be tested separately from headed mode.

Advanced: connect to an already-running **dedicated** Chromium using
`cdp_url = "http://127.0.0.1:9222"` inside `[sources.browser]`. Chromium must have
been started with a local remote-debugging port and a non-default user-data-dir.
In this mode that browser owns its profile and display; the `profile` and `headless`
launch settings do not apply. Only by2kb's newly opened tabs are closed after a job.
An optional `executable_path` selects an existing Chromium for managed-profile mode.

Fallback is attempted at most once, including short-link resolution failures. A
confirmed deleted/unavailable video, cancellation or configuration error does not
trigger fallback. Non-Bilibili sources keep their selected provider. Use
`providers = ["browser"]` under `[sources]` to explicitly test browser-only acquisition.

Title and author are best-effort: browser media acquisition can succeed without the
metadata API, using the page title/video ID and an empty author with a warning.
Native acquisition reuses its one metadata response; CID remains necessary for its
playback API. Browser acquisition reads playback data directly. If there is neither
audio nor a usable transcript, the job fails; it never publishes invented summaries.
Browser downloads require a complete HTTP response, a bounded size (default 1 GiB,
configurable `max_audio_bytes`), media inspection and a decodable audio sample.
Detected previews shorter than available full-video metadata require user action.

`needs_auth` / exit 3 means login or human verification is required: agents should
ask the user, not retry indefinitely. Transient/CDN failures use exit 2 and explain
the alternative of ingesting a local file. Failure JSON includes `reason_code`,
`requires_user_action` and privacy-safe provider `attempts`; successful fallback
records `fallback_attempts` in source provenance. Cookies, request headers, browser
page contents and signed media URLs are not included in published provenance.
`doctor` checks browser dependency/configuration/display but does not certify live
login validity or access to Bilibili.

Release smoke test (2026-09-09): on the Tokyo host, native acquisition returned a
rate-limit error and browser fallback retrieved 16,701,204 bytes of audio with a
1,455.15-second duration from `BV1Pyta66EDh`, using a manually authenticated headed
Chromium session over local CDP. Media inspection and sample decoding passed. No
ASR or LLM was invoked. This verifies that route, not all hosts, headless mode or
all Bilibili videos. `examples/browser_smoke.py` reproduces this opt-in test.
