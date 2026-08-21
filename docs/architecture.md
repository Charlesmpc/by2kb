# Proposed architecture

> Design document. Components described here are not implemented yet.

## Components

```text
IM Adapter ──► Capture API ──► Queue ──► Transcript Worker
                                      │          │
                                      │          ├─ YouTube adapter (native transcript)
                                      │          ├─ Bilibili adapter (native transcript)
                                      │          ├─ Browser capture adapter (phase 2b, opt-in)
                                      │          └─ Audio/ASR adapter (phase 2)
                                      │
                                      └────► Job Store
                                                 │
Transcript Worker ──► Normalizer ──► Raw Writer ─┼─► Skill Runner
                                                 │       │
                                                 │       ▼
                                                 └─► Updated Writer
                                                          │
                                                          ▼
                                                  Knowledge Sink
                                                          │
                                                          ▼
                                                   IM Notification
```

## Boundaries

- **Input adapters** authenticate an IM event, extract URLs and user options, and submit jobs.
- **Source adapters** resolve canonical IDs, metadata, and native transcript tracks.
- **Media/ASR adapters** are optional phase-two fallbacks and must be independently enabled.
- **Browser capture adapter** (phase 2b) is a special media provider: it drives a
  headless browser to the watch page, extracts the media source the page itself plays
  (player object or observed network requests), downloads the stream, and hands the
  audio to the ASR adapter. It is opt-in per deployment and records its provenance as
  `browser_capture + asr`.
- **Normalizer** converts provider responses into one timestamped transcript schema.
- **Raw writer** renders deterministic Markdown and preserves provider JSON.
- **Skill runner** produces a new updated artifact; it never changes raw data.
- **Knowledge sinks** publish artifacts and return durable destination references.
- **Notifier** reports queue, completion, partial completion, and actionable failures.

## Execution modes

The same codebase runs two ways; the components above collapse accordingly:

- **Local (CLI-only) mode** — one process per job. The Queue degenerates to the
  process itself, the Job Store is a local SQLite file (idempotency and dedup still
  enforced), and the Notifier is a callback to the caller — an agent adapter, a
  webhook, or an IM reply. No resident daemon, no deployment beyond the binary.
- **Service mode** — a resident process owns the Queue, Job Store, worker pool,
  retries, and notification loop, and serves the HTTP job API. The CLI and IM bot
  adapters are both just clients of this API.

Mode selection is by configuration (e.g. `BY2KB_SERVER_URL`): with it, the CLI is a
thin client submitting jobs; without it, the CLI executes the pipeline in-process.
Input adapters (IM bot, agent plugin, cron, manual) are mode-agnostic: they submit a
canonical job and receive a callback. A deployment can start local (one user, one
agent) and graduate to service mode (concurrency, retries, multiple senders) without
changing any adapter.

Agent-side adapters (first target: a hermes plugin on the `pre_gateway_dispatch`
message hook) sit outside this service entirely — they are the deterministic trigger
that turns a bare video URL in the agent's IM channel into a submitted job, and they
deliver results back through the agent's own channel, so `by2kb` needs no bot
identity of its own in agent-hosted deployments.

## Native transcript retrieval (phase 1 reference implementations)

The two prior-art projects (see README "Prior art") provide proven retrieval paths
that port directly to server-side adapters:

- **Bilibili** — three calls against official web APIs:
  1. `x/web-interface/view?bvid=...` → `aid`/`cid`/metadata (unsigned);
  2. `x/player/wbi/v2?aid=&cid=&bvid=&wts=&w_rid=` → subtitle track list. Requires WBI
     signing: daily-rotating `imgKey`/`subKey` from the `nav` API, a fixed
     permutation-table mixin key, sorted query with `!'()*` stripped, and
     `w_rid = md5(query + mixin_key)`. Cache keys ≤ 1 hour.
  3. `GET <subtitle_url>` on the hdslb CDN → full `{body: [{from, to, content}]}` JSON,
     fetched without cookies.
   Server-side: AI subtitle tracks are usually empty logged-out — the reference
   extension works because it inherits the user's own browser session, an option a
   server-side service does not legitimately have. The `SESSDATA` cookie route was
   evaluated and rejected (`tech-design-m1.md` §7.5), so `by2kb` does not pursue
   Bilibili native subtitles; this flow remains the reference for WBI signing and
   error mapping used by the audio path. Map business code `-352` (risk control) to
   `rate_limited` with backoff.
- **YouTube** — two interchangeable providers behind one interface:
  - **Supadata** (`GET api.supadata.ai/v1/transcript?url=...&text=false&mode=native`,
    API key; HTTP 202 → poll the job id): simplest, no anti-bot exposure, paid.
  - **youtube-transcript-api** (direct, keyless): self-hosted but breaks when YouTube
    changes player internals; needs its own retry/error taxonomy.
  Neither path ever touches media streams.

## Suggested job states

```text
accepted
  → resolving
  → fetching_transcript
  → normalizing
  → raw_published
  → enriching
  → updated_published
  → completed
```

Intermediate states for the phase-2 fallback path:

```text
  → capturing_media        (browser capture or media download in progress)
  → transcribing           (ASR in progress)
```

Terminal/exception states:

```text
needs_auth
no_native_transcript
needs_audio_fallback
browser_capture_unavailable
rate_limited
failed_retryable
failed_terminal
cancelled
```

`raw_published` is an intentional checkpoint. If enrichment fails, the raw transcript is
still a useful successful result and can be updated later without refetching.

## Normalized transcript schema

```json
{
  "schema_version": 1,
  "source": {
    "platform": "youtube",
    "video_id": "...",
    "canonical_url": "https://...",
    "title": "...",
    "author": "...",
    "duration_ms": 123456
  },
  "transcript": {
    "provider": "...",
    "kind": "human|auto_caption|asr",
    "language": "en",
    "available_languages": ["en", "zh-CN"],
    "fetched_at": "RFC3339 timestamp",
    "segments": [
      {
        "start_ms": 0,
        "duration_ms": 3200,
        "text": "...",
        "confidence": null
      }
    ]
  }
}
```

## Skill execution contract

A skill receives:

- normalized transcript JSON;
- rendered raw Markdown;
- source metadata;
- user language and destination preferences;
- optional supporting files from the skill package.

It returns one or more named sections and provenance metadata. The system records the
skill identifier, content hash/version, model/provider, execution time, and warnings in
the updated document frontmatter.

## Idempotency

Recommended identities:

- source identity: `platform + canonical_video_id`;
- transcript identity: source identity + language + transcript provider/version;
- enrichment identity: transcript hash + ordered skill hashes + processing options;
- destination identity: enrichment identity + sink + destination path.

This permits independent refetch, re-enrichment, and republishing without silently
replacing evidence.

## Security

- Verify IM webhook signatures and map senders to configured users.
- Encrypt provider keys and authenticated platform sessions at rest.
- Do not print cookies, API keys, private video metadata, or transcript content in logs.
- Use a dedicated Bilibili account if authenticated subtitle access is needed.
- Restrict each knowledge sink token to its target folder/space where possible.
- Treat imported skills as executable instructions and require review or trust policy.
- Keep media retrieval disabled by default until provider and platform policies are defined.
- Browser capture (phase 2b) additional constraints:
  - disabled by default; enable per deployment and per platform only after reviewing
    platform terms and the account-safety cost of a flagged session;
  - run the headless browser with a dedicated, least-privilege profile; never reuse the
    operator's primary account session;
  - never persist captured media beyond the ASR step unless the user explicitly opts in;
  - record `browser_capture + asr` provenance and lower confidence in the raw artifact
    so downstream consumers can distinguish it from native transcripts.
