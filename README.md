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

### Phase 2: audio fallback

When no usable transcript exists:

1. obtain the audio through a configurable media provider;
2. normalize it to an ASR-friendly format;
3. transcribe it through a configurable ASR provider;
4. store provenance, timing, model, and confidence metadata;
5. continue through the same raw/updated pipeline.

The download and ASR providers are intentionally undecided. Platform terms, account
security, regional restrictions, cost, and deployment environment must be evaluated
before enabling this path.

## Connectors

### IM inputs

Planned adapters:

- Telegram bot;
- Lark/Feishu bot;
- generic webhook;
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

## Non-goals for the first release

- A full video player or browser side panel.
- Real-time subtitle following while a video plays.
- Replacing the user's knowledge-base application.
- Treating generated summaries as a substitute for the source transcript.
- Circumventing access controls or platform restrictions.

## Prior art

The product direction was informed by:

- [youtube-digest](https://github.com/zarazhangrui/youtube-digest), a Chrome extension
  that retrieves YouTube transcripts through Supadata and presents learning tools in a
  side panel;
- [bilibili-digest](https://github.com/biuworks/bilibili-digest), a Bilibili adaptation
  that retrieves official subtitle tracks through Bilibili APIs.

`by2kb` is a new server-side, IM-driven ingestion project. No source code from those
projects is included at this stage. Their licenses and attribution requirements must be
reviewed before reusing code in future implementations.

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
