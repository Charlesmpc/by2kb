# by2kb

**Forward a video. Keep the knowledge.**

`by2kb` turns interesting videos into durable Markdown for your own knowledge base.
Forward a YouTube or Bilibili link to an IM bot; a background service retrieves the
available transcript, preserves a raw version, optionally processes it with your
personal skills, and delivers both versions to the knowledge base you control.

> **Project status: planning / pre-alpha.** This repository currently defines the
> product, data contract, and roadmap. The ingestion service is not implemented yet.

The name can be read as **B/Y to KB** — Bilibili and YouTube to Knowledge Base — while
the architecture is intended to support more video sources over time.

## Why

Most video tools optimize for watching, summarizing, or taking notes inside a browser.
That is useful, but it is not the workflow this project wants:

- Interesting videos are often discovered in a mobile app.
- A browser extension should not be required to keep a desktop computer online.
- The transcript should become a portable artifact, not remain in extension storage.
- The original transcript and AI-edited output should be kept separately.
- Summarization should follow the user's own methods, prompts, and domain knowledge.
- Markdown and source JSON should remain the source of truth; a vector database is only
  an optional index.

There is also an obvious alternative that this project deliberately does not take as
the primary path: sending the video itself to a multimodal bot. In practice it hits
two walls:

- **Access.** Bots are usually locked out of the watch page — platforms gate playback
  behind logins and anti-scraping, and generally only allow metadata-level access. The
  same gating is exactly why subtitle and metadata APIs remain the most reliable entry
  point.
- **Cost.** Even when the content is reachable, feeding video into a model consumes
  orders of magnitude more tokens than working with text.

So `by2kb` works down a cost ladder, from lightest to heaviest:

1. **Native transcript** — if subtitles exist, the job is nearly free: metadata-level
   access is enough and the payload is small text.
2. **Audio stream + ASR** — when there are no subtitles, retrieve the audio track and
   transcribe it.
3. **Full media download** — when audio and video are not delivered separately,
   download the original media file (e.g. an mp4) and extract the audio.

Each step down costs more bandwidth, compute, and tokens, so the service always tries
the lightest step first.

`by2kb` treats a video link as an asynchronous knowledge-ingestion job.

## Intended experience

```text
YouTube / Bilibili mobile app
           │
           │ Share video URL
           ▼
 Telegram / Lark / another IM bot
           │
           │ Accepted: queued
           ▼
      by2kb service
           │
           ├─ resolve metadata
           ├─ fetch native transcript
           ├─ normalize timestamps
           ├─ save raw Markdown + source JSON
           ├─ run selected personal skills
           └─ save updated Markdown
           │
           ▼
 Obsidian / filesystem / Lark Wiki / Notion / custom KB
           │
           └─ IM notification with links and status
```

From the user's point of view:

1. Find an interesting video.
2. Tap **Share** and send it to the configured IM bot.
3. Receive an immediate acknowledgement and job status.
4. Receive two knowledge artifacts when processing finishes:
   - **Raw** — minimally normalized transcript with source timestamps.
   - **Updated** — the transcript processed by the selected skills, such as cleanup,
     chapters, summary, key arguments, tags, or domain-specific analysis.

## Output model

Every accepted video gets a stable content directory:

```text
library/
  youtube/<video-id>/
    source.json
    transcript.json
    raw.md
    updated.md
  bilibili/<bvid>/
    source.json
    transcript.json
    raw.md
    updated.md
```

### `raw.md`

The raw document should be deterministic and reproducible. It contains:

- source URL and canonical video ID;
- title, author/channel, duration, language, and capture time;
- transcript provider and whether the track is human- or machine-generated;
- timestamped transcript segments linking back to the original video;
- no invented facts or silent rewriting.

### `updated.md`

The updated document is generated from `raw.md` and records:

- skills and versions used;
- model/provider metadata where applicable;
- generated summary, chapters, key points, tags, and optional commentary;
- links back to the raw artifact and original video;
- processing timestamp, so it can be regenerated when skills improve.

Keeping both files prevents an AI rewrite from replacing the evidence.

## Personalized skills

A skill is a reusable Markdown-based processing instruction, with optional references,
templates, and scripts. A user can select a default skill set globally and override it
per message or destination.

Example:

```text
skills/
  transcript-cleanup/
    SKILL.md
  research-video/
    SKILL.md
    templates/report.md
  language-learning/
    SKILL.md
```

Possible skills include:

- punctuation and speech-disfluency cleanup;
- chapter detection with source timestamps;
- concise or detailed summaries;
- extraction of claims, evidence, questions, and action items;
- bilingual notes;
- terminology and entity indexing;
- a team- or domain-specific report format.

Skills transform the updated artifact only. They must not mutate or overwrite the raw
transcript.

## Transcript providers

Provider adapters hide platform-specific behavior behind a common contract.

### Phase 1: native transcripts

- **YouTube** — retrieve an existing human or auto-caption track through a configurable
  transcript provider. Supadata is one possible provider, not a hard dependency.
- **Bilibili** — retrieve available official subtitle tracks using Bilibili metadata,
  WBI-signed player APIs, and an optional authenticated session when a track requires
  login.

The service should prefer native transcripts because they are faster, cheaper, and
usually preserve better timestamps.

#### Known-good retrieval paths (from the prior-art projects)

Both inspiration projects are Chrome extensions, but their retrieval logic is plain
HTTP and ports to a server-side service directly:

- **Bilibili** ([bilibili-digest](https://github.com/biuworks/bilibili-digest),
  `lib/bili-api.js` + `lib/wbi.js`) — a three-step flow against official web APIs:
  1. `GET https://api.bilibili.com/x/web-interface/view?bvid=...` → `aid` / `cid` /
     title / parts (unsigned);
  2. `GET https://api.bilibili.com/x/player/wbi/v2?aid=&cid=&bvid=&wts=&w_rid=` →
     subtitle track list; this call requires **WBI signing** (daily-rotating keys from
     the `nav` API, a fixed permutation-table mixin key, and an MD5 `w_rid`);
  3. `GET <track.subtitle_url>` (hdslb CDN) → full transcript JSON
     `{body: [{from, to, content}]}` in one response, fetched **without** cookies.

  Server-side notes: AI subtitle tracks are usually empty for logged-out sessions, so
  the service needs its own logged-in `SESSDATA` cookie (dedicated account recommended)
  plus browser-like headers; business code `-352` means Bilibili risk control blocked
  the request and should map to a retryable/backoff state.
- **YouTube** ([youtube-digest](https://github.com/zarazhangrui/youtube-digest)) —
  delegates entirely to the third-party **Supadata** API
  (`GET https://api.supadata.ai/v1/transcript?url=...&text=false&mode=native` with an
  API key; HTTP 202 returns a job id to poll). This is the simplest path and keeps the
  service clear of YouTube's anti-bot surface, at the cost of a paid dependency.
  A self-hosted alternative is the
  [`youtube-transcript-api`](https://github.com/jdepoix/youtube-transcript-api) Python
  library (used by the
  [youtube-content skill](https://github.com/NousResearch/hermes-agent/tree/main/skills/media/youtube-content)
  in hermes-agent), which talks to YouTube directly; it needs no key but breaks
  occasionally when YouTube changes its player internals, so it should sit behind the
  same provider interface with its own error taxonomy.

Neither project downloads audio or video streams — and that is deliberate: media
endpoints are where platforms concentrate their anti-scraping defenses (see Phase 2).

### Phase 2: audio fallback

When no usable transcript exists:

1. obtain the audio through a configurable media provider (where a platform delivers
   audio and video muxed rather than separately, the provider downloads the media file
   itself — e.g. the original mp4 — and extracts the audio track);
2. normalize it to an ASR-friendly format;
3. transcribe it through a configurable ASR provider;
4. store provenance, timing, model, and confidence metadata;
5. continue through the same raw/updated pipeline.

The download and ASR providers are intentionally undecided. Platform terms, account
security, regional restrictions, cost, and deployment environment must be evaluated
before enabling this path.

A proven Doubao AUC reference flow is included for provider design: local audio is
staged in a private Volcengine TOS bucket, exposed through a 10-minute presigned URL,
submitted and polled asynchronously, and deleted in a `finally` block. Long audio is
split into ordered Opus chunks with bounded concurrency. See
[`docs/reference/doubao-auc-tos-asr.md`](docs/reference/doubao-auc-tos-asr.md) and the
runnable [`examples/doubao_auc_tos_asr.py`](examples/doubao_auc_tos_asr.py).

#### Phase 2b: browser-based source capture

A browser session can reach what plain HTTP cannot: the watch page itself already
negotiates a playable stream. When a video has no native transcript, the service can:

1. open the watch URL in a **headless browser** (Playwright/CDP or a managed browser
   service) using the user's own authenticated profile where needed;
2. extract the media source the page actually plays — from the player object in the
   page (e.g. the player response / `playurl` data) or by observing network requests —
   and download it (typically a segmented DASH/HLS stream assembled with ffmpeg);
3. feed the audio into the same ASR pipeline as Phase 2.

This is the same idea as consumer "download the page source" tools — e.g. Xunlei's
new Chrome extension for grabbing page media sources, and hermes-agent's browser
automation skills (stealth browsing via `scrapling`, managed-browser `browser` tools,
and Whisper-based ASR skills) — recast as a headless, server-side adapter.

This path is **off by default** and must stay opt-in per deployment:

- it is the most fragile (player internals change often) and the most
  policy-sensitive route — it exists precisely because platforms gate media streams;
- it must respect platform terms, copyright, and the user's own account safety
  (a flagged or banned session is a real cost);
- provenance must record that the transcript came from browser capture + ASR, with
  lower confidence than native tracks, so downstream skills and readers can tell the
  difference.

## Connectors

### IM inputs

Planned adapters:

- Telegram bot;
- Lark/Feishu bot;
- generic webhook;
- agent-host adapters — forward the video to your own agent bot (e.g. hermes), which
  triggers `by2kb` via a plugin; see [Deployment and integration](#deployment-and-integration);
- later: native mobile share target, PWA, or a minimal browser capture extension.

An input adapter should submit a canonical job; it should not contain transcript logic.

### Knowledge-base outputs

Planned sinks:

- local/shared filesystem and Obsidian vault;
- Git-backed Markdown repository;
- Lark/Feishu Wiki or Docx;
- Notion;
- generic webhook/API.

Markdown plus the original transcript JSON is the portable source of truth.

## Deployment and integration

The primary interface to `by2kb` is a **CLI**; a long-running **service** is an
optional upgrade, not a prerequisite. The same binary runs in two execution modes:

- **Local mode (no service).** `by2kb ingest <url>` runs the whole pipeline —
  resolve, fetch, normalize, raw, skills, updated, sink, notify — in one process,
  then exits. Job state and idempotency live in a local SQLite store. This is the
  zero-infrastructure mode: it runs on a laptop, on an agent's server, or anywhere a
  process can spawn.
- **Client mode (service deployed).** When `BY2KB_SERVER_URL` is configured, the CLI
  becomes a thin client: `ingest` submits a job to the service and returns a job id;
  `status` queries it. The service owns the queue, workers, retries, job store, and
  notification loop, and exposes the HTTP job API that bots and other callers use.

### Who needs the service?

- **Users without an agent: yes.** A standalone IM bot needs a resident webhook
  listener, and ingestion is asynchronous (transcript fetch, optional ASR, skill
  runs), so the queue, retry, and notification loops must live in a durable resident
  process — that is the service. The bot adapter is just another input adapter calling
  the service API. ("The bot spawns the CLI per message" works only where the bot
  framework can itself execute commands, and gives up queueing, retries, and
  concurrency control; it is not the canonical path.)
- **Users with an agent: not necessarily.** An agent host already provides the
  message surface and the notification channel, so `by2kb` does not need its own bot
  identity at all. A small per-agent adapter triggers the CLI; when volume grows
  (concurrent jobs, retries, multiple senders), point the CLI at a deployed service
  and nothing else changes.

### Agent integration (plugin adapters)

For agent-first users, the adapter of record is a **plugin** on the agent side, not a
`by2kb` bot. The first target is [hermes-agent](https://github.com/NousResearch/hermes-agent):

- a plugin hooking hermes' `pre_gateway_dispatch` message hook matches a bare video
  URL deterministically — before auth and before the LLM — spawns `by2kb ingest` in a
  background thread, acknowledges through the agent's own IM adapter, and returns
  `skip` so the message never reaches the model (zero tokens for the trigger path);
- a companion skill (and later an MCP server) covers the phrased case — "save this
  video to my KB" — where the model invokes the CLI through its terminal tool.

Other agents (Claude Code hooks, Codex MCP, ...) follow the same shape: deterministic
trigger where the agent offers one, skill/MCP otherwise, CLI always as the engine.

### Integration matrix

| Scenario | Trigger | Execution | Service needed |
| --- | --- | --- | --- |
| Agent-first (hermes) | plugin hook on a bare video URL (deterministic, pre-LLM) | CLI subprocess, local mode | No (optional upgrade) |
| Agent-first, phrased | skill / MCP tool (model judgment) | CLI via the agent's terminal | No |
| No agent | IM bot adapter (Telegram/Lark webhook) | service queue + workers | Yes |
| Scripted / manual | cron, mobile shortcut, hand-typed command | CLI, either mode | No |

The service is best understood as the CLI's execution substrate: the CLI is the access
method; the service is where jobs run once they outgrow a single process.

## Proposed job contract

```json
{
  "source_url": "https://www.youtube.com/watch?v=...",
  "requested_by": "im:user-id",
  "destination": "obsidian:videos/ai",
  "skills": ["transcript-cleanup", "research-video"],
  "options": {
    "preferred_languages": ["zh-CN", "en"],
    "allow_audio_fallback": false
  }
}
```

A job is idempotent by canonical platform and video ID. Re-sending the same video should
return the existing artifacts unless the user requests a refresh or a different skill
set.

## Architecture principles

- **Mobile-first capture:** sharing a URL is the primary interface.
- **Asynchronous by default:** IM acknowledgement is immediate; processing continues in
  the background.
- **Provider-neutral:** transcript, media, ASR, LLM, IM, and knowledge sinks are adapters.
- **Evidence preservation:** raw and updated outputs are separate and linked.
- **User-owned storage:** portable files are the primary artifacts.
- **Idempotent and retryable:** duplicate messages and transient provider failures are
  expected.
- **Observable:** every job records state, provider, timing, error category, and cost when
  available.
- **Credential-aware:** platform cookies and API keys are secrets; use least-privilege
  accounts and encrypted storage.
- **No browser dependency:** a browser extension may improve capture or authentication,
  but the processing service must run headlessly.

See [`docs/architecture.md`](docs/architecture.md) for the proposed components and state
machine.

## Roadmap

### Milestone 0 — Product contract

- [x] Define the capture-to-knowledge workflow.
- [x] Separate raw and updated artifacts.
- [x] Define personalized skills as a first-class concept.
- [x] Keep audio retrieval and ASR behind future provider interfaces.

### Milestone 1 — Native transcript MVP

- [ ] Job API, queue, persistence, retries, and deduplication.
- [ ] Telegram input adapter.
- [ ] YouTube native-transcript adapter.
- [ ] Bilibili native-transcript adapter.
- [ ] Timestamp-preserving normalization.
- [ ] Filesystem/Obsidian Markdown sink.
- [ ] Raw and updated document generation.
- [ ] One default cleanup-and-summary skill.
- [ ] IM completion/failure notifications.

### Milestone 2 — Personalization and more destinations

- [ ] Skill registry, per-user defaults, and per-job overrides.
- [ ] Lark/Feishu input adapter.
- [ ] Lark Wiki/Docx and Notion sinks.
- [ ] Regenerate updated output without refetching the transcript.
- [ ] Cost, latency, and provider usage reporting.

### Milestone 3 — Audio and ASR fallback

- [ ] Media retrieval provider interface.
- [ ] Audio normalization worker.
- [ ] Hosted and self-hosted ASR provider interfaces.
- [ ] Long-video chunking, alignment, and confidence metadata.
- [ ] Policy and deployment controls per platform/provider.
- [ ] Phase 2b: headless-browser source capture adapter (opt-in), extracting the
      playable media source from the watch page when no native transcript exists,
      with `browser_capture + asr` provenance.

## Non-goals for the first release

- A full video player or browser side panel.
- Real-time subtitle following while a video plays.
- Replacing the user's knowledge-base application.
- Treating generated summaries as a substitute for the source transcript.
- Circumventing access controls or platform restrictions.

## Prior art

`by2kb` was directly inspired by these two projects — they proved the core insight
that a video's native transcript can be retrieved programmatically and turned into a
learning artifact, and `by2kb` exists to move that workflow from a browser extension
into a headless, IM-driven, server-side service:

- [youtube-digest](https://github.com/zarazhangrui/youtube-digest) (MIT, © Zara Zhang) —
  a Chrome extension that retrieves YouTube transcripts through Supadata and presents
  learning tools (summaries, chapters, translation, notes) in a side panel. It inspired
  the raw/updated split, the skill-style prompt templates, and the Supadata provider
  path.
- [bilibili-digest](https://github.com/biuworks/bilibili-digest) (MIT) — a Bilibili
  adaptation of the same architecture that retrieves official subtitle tracks directly
  through Bilibili's web APIs (view → WBI-signed player API → subtitle CDN JSON). It
  inspired the Bilibili adapter design, including WBI signing, login-gated AI subtitle
  handling, and risk-control (`-352`) error mapping.

`by2kb` is a new server-side, IM-driven ingestion project. No source code from those
projects is included at this stage; when their retrieval logic is ported, their MIT
licenses and attribution requirements (copyright notices, including bilibili-digest's
"portions © Zara Zhang" credit) must be preserved.

## Contributing

The project is at the design stage. Discussions and issues about the following are
especially welcome:

- transcript providers and platform reliability;
- the portable skill format;
- knowledge-base destination contracts;
- safe Bilibili authenticated-session handling;
- audio/ASR provider evaluation;
- schemas for timestamped, multilingual transcripts.

Please do not include API keys, cookies, private transcripts, or copyrighted media in
issues or test fixtures.

## License

MIT — see [LICENSE](LICENSE).
