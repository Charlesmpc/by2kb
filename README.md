# by2kb

**Forward a video. Keep the knowledge.**

`by2kb` turns a video URL into three durable Markdown artifacts in your own knowledge
base: the evidence-preserving transcript, a short “is this worth reading?” abstract,
and long-form study notes.

The primary user is an **agent user**. Send a Bilibili URL or media attachment to an
agent such as Hermes; `by2kb` handles media retrieval and the selected local or cloud
ASR provider, while the agent uses its existing model authentication to produce both summaries.
Standalone users can run the same pipeline with their own OpenAI-compatible API key.

> **Current release: v0.2.1.** Bilibili ingestion, Doubao AUC ASR, local filesystem
> output, API enrichment, the external-agent protocol, guided initialization, and the
> Hermes plugin are implemented. YouTube, a resident service, native Telegram/Lark
> bots, and remote knowledge-base sinks remain planned.

## Start here

Requires Python 3.12+, `pipx`, and `ffmpeg`/`ffprobe` on PATH. Until the first PyPI
publication, install the versioned release artifact directly from GitHub:

```bash
pipx install "by2kb[asr-doubao] @ https://github.com/Charlesmpc/by2kb/releases/download/v0.2.1/by2kb-0.2.1-py3-none-any.whl"
by2kb init
```

`by2kb init` guides you through four decisions:

1. where the local knowledge base should live;
2. whether transcription uses local faster-whisper or cloud Doubao AUC;
3. local model settings, or the private TOS bucket and Doubao credentials;
4. whether summaries run in an agent (`agent`), through a standalone LLM API (`api`),
   or remain disabled.

After installing the selected optional ASR runtime/model, verify the deployment:

```bash
by2kb doctor
by2kb doctor --json
```

### Agent-first: Hermes

After selecting `agent` during initialization:

```bash
by2kb agent install hermes
hermes gateway restart
```

Now an authorized user can send a Bilibili or `b23.tv` URL to the Telegram Hermes bot.
The plugin acknowledges immediately, runs transcription in the background, calls the
Hermes host model through bounded staged operations, writes all three artifacts, and
replies with the short abstract and knowledge-base paths. No MCP server and no
separate by2kb LLM key are required.

### Standalone

Select `api` during initialization, provide an OpenAI-compatible endpoint, model and
API key, then run:

```bash
by2kb ingest "https://www.bilibili.com/video/<bvid>/"
by2kb ingest ./meeting.mp3
by2kb ingest ./lecture.mp4
```

See [Agent integration](docs/agent-integration.md) for the reusable staged
`ingest → next → submit` enrichment contract used by other agent hosts.
See [Agent task control](docs/task-control.md) for the versioned
`status`/`wait`/`cancel`/`retry` protocol and checkpoint-aware retries.
See [ASR providers](docs/asr-providers.md) for optional local faster-whisper setup,
explicit model installation, and the existing cloud Doubao path.
See [Source providers](docs/source-providers.md) for configurable provider priority,
optional yt-dlp installation, caption policy, audio fallback, and cookie handling.
See [Local media](docs/local-media.md) for supported formats, content-addressed
deduplication, ffmpeg requirements, and attachment handling.
See [Diagnostics](docs/doctor.md) for every read-only check and the Agent-facing JSON schema.
See [Transcript quality](docs/transcript-quality.md) for deterministic enrichment gates,
recorded metrics, and warning behavior.
See [Long-form enrichment](docs/long-form-enrichment.md) for segment-safe planning,
intermediate caching, recursive reduction, and provenance.
See [Agent enrichment provider](docs/agent-enrichment-provider.md) for the bounded
callback protocol and API-key versus subscription/OAuth authentication tradeoffs.

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
the lightest step first. And at every step it captures content, never the video
itself: the knowledge-base entry is a cheap, durable gist — transcript or ASR text
plus a link back — so the platform keeps bearing hosting and anti-scraping cost,
while a topic worth rewatching stays one click away from the original.

`by2kb` treats a video link as an asynchronous knowledge-ingestion job.

## Agent-first experience

```text
Bilibili mobile app
           │
           │ Share video URL
           ▼
 Telegram Hermes bot
           │
           │ Accepted: queued
           ▼
 Hermes plugin + by2kb CLI
           │
           ├─ resolve metadata
           ├─ retrieve audio
           ├─ stage privately in TOS and run ASR
           ├─ normalize transcript
           ├─ save raw Markdown + source JSON
           ├─ create a short abstract
           └─ create long-form study notes
           │
           ▼
 Local filesystem / Obsidian vault
           │
           └─ IM notification with links and status
```

From the user's point of view:

1. Find an interesting video.
2. Tap **Share** and send it to the configured IM bot.
3. Receive an immediate acknowledgement and job status.
4. Receive three knowledge artifacts when processing finishes:
   - **Raw** — minimally normalized transcript with source timestamps.
   - **Abstract** — a one-minute interest check: what the video argues, what the reader
     will learn, and whether it is worth going deeper.
   - **Updated / study notes** — a long, structured learning guide with a knowledge
     map, guided walkthrough, claims and evidence, concepts, questions, and actions.

Agent hosts can expose progress and lifecycle controls without parsing logs:

```bash
by2kb status <job-id> --json
by2kb wait <job-id> --timeout 30 --json
by2kb cancel <job-id> --json
by2kb retry <job-id> --json
```

## Output model

Every accepted video gets a stable content directory. JSON artifacts use fixed
names; Markdown artifacts are named `raw.<Title>.md`, `short.<Title>.md`, and
`long.<Title>.md`. The parent directory already carries the stable video ID, while
the prefix makes reading depth obvious in ordinary file listings (naming contract:
`docs/tech-design-m1.md` §3.4):

```text
library/
  youtube/<video-id>/
    source.json
    transcript.json
    raw.<Title>.md
    short.<Title>.md
    long.<Title>.md
  bilibili/<bvid>/
    source.json
    transcript.json
    raw.<Title>.md
    short.<Title>.md
    long.<Title>.md
```

### `raw.<Title>.md`

The raw document should be deterministic and reproducible. It contains:

- source URL and canonical video ID;
- title, author/channel, duration, language, and capture time;
- transcript provider and whether the track is human- or machine-generated;
- timestamped transcript segments linking back to the original video;
- no invented facts or silent rewriting.

### `short.<Title>.md`

The abstract is deliberately short and decision-oriented. It contains a one-sentence
summary, two to four concrete learning outcomes, and a recommendation describing who
will benefit from reading the study notes or watching the source.

### `long.<Title>.md` (deep study notes)

The long document is generated from `raw.<Title>.md` and records:

- skills and versions used;
- model/provider metadata where applicable;
- core thesis, knowledge map, guided walkthrough, claims and evidence, concepts,
  questions to verify, and a study/action list;
- links back to the raw artifact and original video;
- processing timestamp, so it can be regenerated when skills improve.

Keeping generated files separate from `raw.<Title>.md` prevents an AI rewrite from replacing
the evidence.

### LLM access and authentication

Summary generation does require an LLM, but it does **not** require a browser login.
There are two execution paths:

- Agent users select `external_agent`. The Hermes reference plugin borrows the active
  Hermes provider/model through its host-owned LLM API; `by2kb` never receives that
  credential and no nested agent loop is created.
- Standalone deployments select `api`. `by2kb` calls an OpenAI-compatible endpoint
  with a server-side API key and never stores that key in an artifact. The default
  endpoint preset is Volcengine Ark. To use OpenAI, set:

```dotenv
BY2KB_LLM_API_KEY=<your API key>
BY2KB_LLM_MODEL=<an API model available to your project>
BY2KB_LLM_BASE_URL=https://api.openai.com/v1
```

OpenAI documents API-key authentication in its
[API reference](https://developers.openai.com/api/reference/overview) and its current
text-generation APIs in the
[text generation guide](https://developers.openai.com/api/docs/guides/text).
`by2kb` does not reuse a ChatGPT browser session. Other providers can be used when
they expose the compatible chat-completions endpoint.

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
- **Bilibili** — dropped 2026-08-17: unauthenticated subtitle tracks proved
  unreachable (23/23 video probe) and the authenticated route was rejected, so
  Bilibili enters at Phase 2 (audio + ASR) instead. See `docs/tech-design-m1.md`
  §7.5 and Appendix A.1.

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

   Server-side notes: AI subtitle tracks are usually empty for logged-out sessions —
   the extension works only because it inherits the user's own browser session
   (`credentials: "include"` on `api.bilibili.com`), an option a server-side service
   does not legitimately have. The `SESSDATA` route was evaluated and rejected (see
   `docs/tech-design-m1.md` §7.5), so `by2kb` does not pursue Bilibili native
   subtitles. Business code `-352` means Bilibili risk control blocked the request and
   should map to a retryable/backoff state (still relevant to the audio-download
   path).
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

Neither project downloads audio or video streams — they deliberately stay on the
subtitle/metadata surface. Media streams are the fallback for when no native
transcript exists (see Phase 2); how heavily they are gated differs per platform —
`docs/tech-design-m1.md` appendix A records what the Bilibili spike actually reached
unauthenticated.

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

Implemented:

- Hermes plugin with deterministic Bilibili URL interception and Telegram replies;
- Hermes Skill for natural-language/manual requests.

Planned:

- native Telegram and Lark/Feishu bots for users without an agent host;
- generic webhook;
- other agent-host adapters;
- native mobile share target, PWA, or a minimal browser capture extension.

An input adapter should submit a canonical job; it should not contain transcript logic.

### Knowledge-base outputs

Implemented:

- local/shared filesystem and Obsidian vault directory.

Planned:

- Git-backed Markdown repository;
- Lark/Feishu Wiki or Docx;
- Notion;
- generic webhook/API.

Markdown plus the original transcript JSON is the portable source of truth.

## CLI reference

`.env` is loaded from `$BY2KB_ENV_FILE`, `<BY2KB_HOME>/.env` (default
`~/.by2kb/.env`), or `./.env`. Artifacts land in
`<library>/bilibili/<bvid>/{source.json,transcript.json,raw.<Title>.md,short.<Title>.md,long.<Title>.md}`;
the abstract and study notes are produced by the configured API or external agent.
Exit codes:
0 completed, 1 terminal failure, 2 retryable, 3 needs auth, 4 duplicate.

Agent hosts use the durable external-enrichment protocol:

```bash
by2kb ingest "<video-url>" --enricher external_agent --json
by2kb enrichment claim <job-id> --json
by2kb enrichment complete <job-id> \
  --abstract-file <short-output.md> \
  --study-file <long-output.md> \
  --provider <provider> \
  --model <model> \
  --json
```

The agent is not called recursively. `by2kb` leaves durable pending work, the host
claims it, performs two bounded model calls, and submits the generated bodies through
the trusted publication path.

If a video was transcribed before LLM credentials were configured, generate or refresh
only its two summaries without downloading and transcribing the media again:

```bash
by2kb ingest "https://www.bilibili.com/video/<bvid>/" --re-enrich
```

Bilibili ingestion goes straight to audio+ASR (native subtitles are not
pursued — `docs/tech-design-m1.md` §7.5); ASR setup details live in
[`docs/reference/doubao-auc-tos-asr.md`](docs/reference/doubao-auc-tos-asr.md).

## Deployment and integration

The implemented interface is a **local CLI**. `by2kb ingest <url>` resolves the video,
retrieves audio, runs ASR, writes raw artifacts, executes or defers enrichment, and
publishes to the filesystem. Job state, enrichment leases and idempotency live in a
local SQLite store. It runs on a laptop, an agent server, or any host that can spawn a
process.

A resident service and remote client mode are planned for higher-volume deployments;
`BY2KB_SERVER_URL`, HTTP submission, queues and remote workers are not implemented in
v0.2.1.

### Who needs the service?

- **Users without an agent: eventually.** A standalone IM bot needs a resident webhook
  listener, and ingestion is asynchronous (transcript fetch, optional ASR, skill
  runs), so the queue, retry, and notification loops must live in a durable resident
  process — that is the service. The bot adapter is just another input adapter calling
  the service API. ("The bot spawns the CLI per message" works only where the bot
  framework can itself execute commands, and gives up queueing, retries, and
  concurrency control; it is not the canonical path.)
- **Users with an agent: no.** An agent host already provides the
  message surface and the notification channel, so `by2kb` does not need its own bot
  identity at all. A small per-agent adapter triggers the CLI. The Hermes reference
  integration uses this path today.

### Agent integration (plugin adapters)

For agent-first users, the adapter of record is a **plugin** on the agent side, not a
`by2kb` bot. The first target is [hermes-agent](https://github.com/NousResearch/hermes-agent):

- a plugin hooking Hermes' `pre_gateway_dispatch` message hook matches a video URL
  deterministically, verifies the sender through Hermes authorization, spawns
  `by2kb ingest` in a background thread, acknowledges through the agent's own IM
  adapter, and returns `skip` so the trigger does not enter a model turn;
- after transcription, the plugin calls Hermes' host-owned LLM twice using the
  packaged enrichment profiles, without a nested agent loop or separate LLM key;
- a companion skill covers the phrased case — "save this video to my KB" — where the
  model invokes the CLI through its terminal tool. MCP is not required when both
  programs share a host and filesystem.

Other agents follow the same shape: deterministic trigger where the host offers one,
and a skill calling the CLI otherwise. The agreed executor boundary, loop prevention,
Hermes user journey, target CLI, and implementation status are specified in
[`docs/agent-integration.md`](docs/agent-integration.md).

### Integration matrix

| Scenario | Trigger | Execution | Service needed |
| --- | --- | --- | --- |
| Agent-first (Hermes) | authorized plugin hook on a video URL | CLI subprocess, local mode | No |
| Agent-first, phrased | skill (model judgment) | CLI via the agent's terminal | No |
| No agent | direct CLI today; native IM bot later | local CLI / future service | Not today |
| Scripted / manual | cron, mobile shortcut, hand-typed command | local CLI | No |

The future service will preserve the same job and enrichment contracts when workloads
outgrow a single process.

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

### Milestone 1 — Local ingestion MVP

- [x] Local SQLite job persistence, status tracking, and deduplication.
- [ ] Remote Job API, queue, and worker service.
- [x] Telegram input through the Hermes plugin.
- [ ] Standalone Telegram input adapter for users without an agent.
- [ ] YouTube native-transcript adapter.
- [ ] Timestamp-preserving normalization.
- [x] Filesystem/Obsidian Markdown sink (sink contract pinned in
      `docs/tech-design-m1.md` §3.7).
- [x] Raw, short-abstract, and long-form study-note generation.
- [x] Packaged default abstract and deep-study skills.
- [x] Durable external-agent enrichment protocol and Hermes reference plugin.
- [x] Guided `by2kb init` for TOS, ASR, enrichment mode, and filesystem output.
- [x] Hermes IM acknowledgement and completion/failure notifications.

### Milestone 2 — Personalization and more destinations

- [ ] Skill registry, per-user defaults, and per-job overrides.
- [ ] Lark/Feishu input adapter.
- [ ] Lark Wiki/Docx and Notion sinks (projection adapters over the same
      artifact set, `docs/tech-design-m1.md` §3.7).
- [x] Regenerate both summary outputs without refetching the transcript (`--re-enrich`).
- [ ] Cost, latency, and provider usage reporting.

### Milestone 3 — Audio and ASR

- [x] Media retrieval provider interface (contract pinned in
      `docs/tech-design-m1.md` §3.8; Bilibili chain spike-verified, Appendix A).
- [x] Bilibili ingestion via this path — its primary route, since unauthenticated
      native subtitles were found unreachable and the login route was rejected
      (see `docs/tech-design-m1.md` §7.5). Note this makes Bilibili ingestion
      non-free: ASR costs either accuracy (self-hosted) or money (hosted).
- [x] Audio extraction and long-audio chunking with ffmpeg.
- [x] Hosted ASR provider interface and Doubao AUC implementation (contract pinned in
      `docs/tech-design-m1.md` §3.6; see
      `docs/reference/doubao-auc-tos-asr.md`).
- [ ] Additional hosted and self-hosted ASR providers.
- [ ] Segment-level alignment and confidence metadata.
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

The project is in an early public implementation stage. Discussions and issues about
the following are especially welcome:

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
