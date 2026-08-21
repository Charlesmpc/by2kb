# Technical design — Milestone 1 (native transcript MVP)

> Status: **approved — all §7 design decisions signed off (2026-08-17);
> implementation not started.** Scope is Milestone 1 from the README roadmap.
> The product contract lives in `README.md`; component boundaries, job states, and
> the normalized transcript schema live in `docs/architecture.md`. This document makes
> Milestone 1 implementable: choices, module layout, and interface contracts.

## 1. Technology choices

### 1.1 Language and runtime — Python 3.12+ (confirmed)

The two prior-art repos are Chrome MV3 extensions written in plain browser
JavaScript. They are **behavioral references** (endpoints, WBI signing, error codes,
test vectors), not importable code. The decision therefore rests on server-side
assets and the later phases:

| Factor | Python | Node/TypeScript |
| --- | --- | --- |
| YouTube, no third party | `youtube-transcript-api` (mature, keyless) | no equally mature library |
| YouTube via Supadata | trivial HTTP | trivial HTTP |
| Bilibili flow | small port of `bili-api.js`/`wbi.js`; community Python WBI references exist (bilibili-API-collect) | near-verbatim reuse of the reference JS |
| Phase 2/2b (ASR, browser capture) | faster-whisper, Playwright Python — strongest ecosystem | Playwright first-class, ASR weaker |
| IM/bot SDKs (Telegram, Lark) | mature | mature |

Python is proposed because the keyless YouTube path and the Phase 2/2b ecosystem
dominate; the Bilibili port is small either way.

**Validated by spike (2026-08-17):** the entire Bilibili chain — nav/WBI keys,
`view`, WBI-signed `player/wbi/v2`, WBI-signed `playurl`, and CDN media download —
was exercised with Python 3.12 stdlib only (no third-party packages), unauthenticated,
against a real video (`BV1jmbD65EP2`) and extended to 22 more videos in a
follow-up probe (Appendix A.1). The YouTube keyless path was validated the same
day: `youtube-transcript-api` listed and fetched a full human-uploaded `zh`
transcript unauthenticated (Appendix B). **Decision: Python confirmed by the
owner (2026-08-17).**

Core dependencies (all intentionally boring):

- `httpx` — all platform/provider HTTP (async);
- `typer` — CLI surface;
- stdlib `sqlite3` — job store;
- `pydantic` — contract models (job, normalized transcript, provider responses);
- an OpenAI-compatible LLM client for the skill runner (see §3.5).

### 1.2 Storage

SQLite, one database file per deployment (`~/.by2kb/by2kb.db` local mode; a
configurable path in service mode). Tables for Milestone 1:

- `jobs` — id, platform, video_id, status, requested_by, destination, options JSON,
  created_at, updated_at, attempt count, last error category;
- `artifacts` — job id, kind (`source_json|transcript_json|raw_md|updated_md`),
  path, content hash, created_at;
- `idempotency` — unique index on `(platform, canonical_video_id, skills_hash,
  destination)` resolving to a job id (README idempotency rules).

No foreign keys with cascades; relationship enforcement in the application layer
(matching the convention the owner uses elsewhere).

### 1.3 Process model (Milestone 1)

Local mode only: `by2kb ingest` runs the pipeline synchronously in one process.
An `asyncio` task queue inside the process is the only concurrency (useful for
Supadata job polling and parallel subtitle-track probes). Service mode (`by2kb
serve`) is designed for in §3.3 but not implemented in Milestone 1.

## 2. Module layout

```text
by2kb/
  cli.py                  # typer app: ingest / status / (serve stub)
  config.py               # env + config file loading, secrets stay in env
  errors.py               # error taxonomy (below)
  jobs/
    model.py              # Job, state machine transitions
    store.py              # SQLite persistence + idempotency
    runner.py             # orchestrates one job through the pipeline
  providers/
    base.py               # TranscriptProvider interface + provider registry
    youtube_supadata.py   # Supadata API (mode=native, job polling)
    youtube_native.py     # youtube-transcript-api
    bilibili.py           # deferred to Milestone 3 (§7.5): native subtitles dropped;
                          # Bilibili enters via the audio+ASR path
    bilibili_wbi.py       # WBI key fetch + signing (ported from bili-api/wbi.js;
                          # needed by the audio playurl too)
  normalize.py            # provider payloads -> normalized transcript schema
  writers/
    raw.py                # deterministic raw.md + source/transcript JSON
    updated.py            # updated.md with provenance frontmatter
  skills/
    model.py              # skill package parsing (SKILL.md frontmatter)
    runner.py             # applies selected skills via LLM client
    builtin/
      transcript-cleanup/SKILL.md
      summary/SKILL.md
  sinks/
    base.py               # KnowledgeSink interface
    filesystem.py         # local/Obsidian vault (Milestone 1 sink)
  notify/
    base.py               # Notifier interface
    console.py            # stdout/exit-code notifier for local mode
    webhook.py            # callback_url notifier (used by service/bot mode)
tests/
  unit/                   # wbi vectors, normalizer, writers, idempotency
  fixtures/               # recorded provider responses (VCR-style)
  e2e/                    # local-mode run with fixture providers
```

## 3. Interface contracts

### 3.1 Transcript provider

```python
class TranscriptProvider(Protocol):
    platform: str                      # "youtube" | "bilibili"

    def resolve(self, url: str) -> SourceIdentity: ...
        # canonical platform + video id + canonical url; raises UnsupportedUrl

    async def fetch(self, identity: SourceIdentity,
                    options: FetchOptions) -> NormalizedTranscript: ...
        # raises the error taxonomy below; never returns empty silently
```

Provider selection per platform is an ordered chain in config, e.g. YouTube:
`["youtube_native", "youtube_supadata"]` — first success wins; failures are
recorded per provider before falling through. Bilibili has one provider in
Milestone 1.

Error taxonomy (`errors.py`), mapped to job states from `architecture.md`:

| Error | Meaning | Job state | Retry |
| --- | --- | --- | --- |
| `NeedsAuth` | login-gated transcript / missing session credential (no M1 provider uses this after §7.5; kept for future platforms) | `needs_auth` | none (user action) |
| `NoNativeTranscript` | platform says no track exists | `no_native_transcript` | none (→ phase 2 later) |
| `RateLimited` | bilibili `-352`, Supadata 429 | `rate_limited` | exponential backoff, capped attempts |
| `TransientProviderError` | network, 5xx, Supadata job `failed` | `failed_retryable` | backoff |
| `TerminalProviderError` | 404 video, `-404/62002/62004`, invalid key | `failed_terminal` | none |

### 3.2 CLI surface

```text
by2kb ingest <url>
  [--skills name[,name...]]      # overrides default skill set
  [--dest <sink:target>]         # overrides default destination
  [--lang <pref[,pref...]>]      # preferred transcript languages
  [--allow-audio-fallback]       # reserved; rejected until phase 2
  [--refresh]                    # bypass idempotent reuse (full refetch)
  [--re-enrich]                  # duplicate with different skills: re-run
                                 # enrichment only, reuse stored transcript
  [--json]                       # machine-readable result
by2kb status <job-id> [--json]
```

Exit codes (stable, scriptable — this is what agent plugins and bots rely on):

| Code | Meaning |
| --- | --- |
| 0 | completed (raw published; updated published if skills ran) |
| 1 | terminal failure |
| 2 | retryable failure (caller may retry later) |
| 3 | needs auth (actionable by the user) |
| 4 | duplicate — existing artifacts returned, nothing refetched |

`--json` output includes: job id, state, artifact paths, transcript language,
provider used, warnings. This is the contract the hermes plugin parses.

### 3.3 HTTP job API (service mode — contract now, implementation later)

```text
POST /v1/jobs            # body = the README job contract + optional callback_url
GET  /v1/jobs/{id}       # state + artifact references
GET  /healthz
```

`callback_url`, when set, receives `POST {job_id, state, artifacts, error}` on
terminal states. The CLI in client mode is a thin wrapper over these two routes;
the IM bot adapter (Milestone 2) is another client. Keeping the contract fixed
now means local→service graduation changes no adapter code.

### 3.4 Artifact formats

`library/<platform>/<video-id>/` contains `source.json`, `transcript.json`
(the normalized schema from architecture.md), `raw.md`, `updated.md`.

`raw.md` frontmatter (deterministic — same input, byte-identical output):

```yaml
---
schema_version: 1
platform: youtube
video_id: ...
canonical_url: https://...
title: ...
author: ...
duration_ms: 123456
language: en
transcript_provider: youtube_native
transcript_kind: auto_caption   # human | auto_caption | asr
fetched_at: 2026-08-17T12:00:00Z
---
```

Body: timestamped segments as `[M:SS] text`, linking back to the video at segment
granularity where the platform supports it.

`updated.md` frontmatter adds: `skills: [{name, version}]`, `model`, `provider`,
`processed_at`, `raw_ref: ./raw.md`, `confidence: high|medium|low` (native
transcript = high; phase-2 ASR will be lower).

### 3.5 Skill packages and runner

A skill is a directory: `SKILL.md` (frontmatter: `name`, `description`,
`version`, optional `model`, optional `sections`) plus optional `templates/` and
`references/`. Milestone 1 ships two built-ins: `transcript-cleanup` and
`summary`.

The runner calls models through a small `LlmClient` abstraction (owner
decision 2026-08-17): direct OpenAI-compatible calls, with the first shipped
implementation preset for the Volcengine Ark ecosystem (base URL + key +
model, defaulting to the Ark endpoint and a doubao-series model). Other
providers — including shelling out to an agent CLI, if users want their
agent's own context/skills in the loop — are later polymorphic
implementations behind the same interface, following the §3.6–§3.8 pattern.
The runner receives the normalized transcript + raw markdown + skill
instructions and returns named sections; the writer assembles `updated.md`
and records provenance.

Skills never mutate raw artifacts; the runner enforces this by construction
(reads raw, writes only updated).

### 3.6 ASR provider (Milestone 3 contract — pinned now, implemented later)

§7.5 routes Bilibili through audio+ASR, so the ASR boundary is pinned here with
the same "contract now, implementation later" convention as §3.3. The shape
follows the transcript/sink/notifier provider pattern:

```python
class AsrProvider(Protocol):
    name: str
    async def transcribe(self, audio: LocalAudio,
                         options: AsrOptions) -> AsrResult: ...

# LocalAudio: path, format, duration_s (optional), size_bytes
# AsrResult:  provider, model, language, text, segments[{start, end, text}],
#             provenance  # e.g. staging method; no operational secrets
```

Design rules:

- **The interface is "local audio in, normalized transcript out".** Staging,
  polling, chunking, and format conversion are implementation details of each
  provider, never part of the contract. Cloud object-storage staging exists
  because some ASR APIs consume a URL rather than a file — but that is a
  per-provider concern: `doubao_auc` stages in a private TOS bucket with a
  10-minute presigned URL (reference: `docs/reference/doubao-auc-tos-asr.md`,
  runnable `examples/doubao_auc_tos_asr.py`), `aws_transcribe` would use S3,
  `openai_whisper` uploads the file directly and needs no staging, and a
  `local_whisper` (faster-whisper) implementation never leaves the machine.
- **Configuration selects the implementation** (`[asr] provider = "doubao_auc"`
  in `~/.by2kb/config.toml`, optionally an ordered chain with fallthrough like
  the transcript providers in §3.1). Each implementation declares its own
  secrets as env vars — the TOS AK/SK + Doubao appid/token belong to
  `doubao_auc` alone, so an open-source user on Aliyun or AWS configures only
  their own provider. Adding a provider is one new class plus its config docs;
  the pipeline does not change.
- **Provenance flows through.** `AsrResult.provenance` records provider,
  model, and staging method so `raw.md` can mark `transcript_kind: asr` with
  lower confidence (§3.4), while operational details (object keys, presigned
  URLs) never reach durable artifacts.
- The reference adapter's known gaps before promotion: per-chunk timing is
  discarded (plain-text concatenation), no retries/jitter, dependencies
  unpinned — all recorded in the reference doc's production notes.

### 3.7 Knowledge sink (Milestone 1 filesystem; contract pinned for later sinks)

The KB landscape is crowded (Obsidian, Git, Lark Wiki/Docx, Notion, custom),
so the design inverts the problem the same way §3.6 does for ASR: the
**artifact set is the portable unit**, and a sink is only a delivery adapter.

```python
class KnowledgeSink(Protocol):
    name: str
    async def publish(self, artifacts: ArtifactSet,
                      options: SinkOptions) -> SinkReceipt: ...

# ArtifactSet: the four canonical files of one job (§3.4)
# SinkReceipt: per-artifact status + canonical link(s) for IM notifications
```

Design rules:

- **The filesystem layout is the reference layout** (§3.4):
  `library/<platform>/<video-id>/{source.json,transcript.json,raw.md,updated.md}`.
  The Milestone-1 `filesystem` sink writes exactly this, which also covers
  Obsidian (a vault is a folder of Markdown) and Git-backed libraries (commit
  the folder) with no dedicated adapter.
- **Sinks are projections, never the only copy.** Markdown + source JSON is the
  source of truth; a sink that cannot carry an artifact (e.g. a wiki that
  cannot store `source.json`) omits it — the local library remains the
  canonical record. A deployment configures additional sinks *alongside* the
  filesystem one, not instead of it.
- **Destinations address a sink, not a KB product**: `--dest <sink:target>`
  (e.g. `filesystem:~/vault/videos`, later `lark_wiki:<space>/<node>`,
  `notion:<database>`), mirroring the README job contract's `destination`.
- **Idempotent republish.** Re-publishing a completed job overwrites the same
  destination (same `(platform, video_id)` identity as the job store),
  matching the CLI duplicate semantics (exit code 4); `--refresh` republishes.
- **Degradation is explicit.** `SinkReceipt` reports which artifacts landed
  where, so notifications can say "raw + updated published to Lark Wiki; JSON
  kept in local library" instead of silently dropping anything.
- New sinks (Lark Wiki/Docx, Notion, generic webhook) are one class each in
  Milestone 2; they map the reference layout as best they can (raw.md → wiki
  page, updated.md → child page, JSON → attachment or omitted).

### 3.8 Media retrieval provider (Milestone 3 contract — pinned now, implemented later)

The audio that §3.6 consumes must first be fetched from the video platform —
the last unpinned provider boundary in the ingestion path. Note the division
of labor: *retrieving* audio from the platform is this layer (platform-specific,
always needed on the audio path); *staging* audio for an ASR API (§3.6, e.g.
TOS presigned URLs) stays inside the ASR provider because not every ASR
implementation needs it.

```python
class MediaProvider(Protocol):
    platform: str
    async def fetch_audio(self, identity: SourceIdentity,
                          options: FetchOptions) -> LocalAudio: ...
    # LocalAudio is exactly §3.6's input: path, format, duration_s, size_bytes
```

Design rules:

- **Platform-specific retrieval, provider-neutral output.** The Bilibili
  implementation is the spike-verified chain (Appendix A): WBI-signed
  `playurl` (`fnval=4048`) → highest-bandwidth DASH audio stream → direct CDN
  download with a bilibili.com `Referer` (range requests supported, so
  chunked/resumable downloads are available). A YouTube implementation is only
  needed for the rare no-transcript case.
- **Audio-only by preference.** DASH delivers audio separately, so the video
  track is never downloaded on Bilibili. Full-media download + ffmpeg audio
  extraction (README Phase 2 step 3) is the fallback for platforms that mux,
  and stays inside the provider.
- **No login, no browser.** The contract covers only what works
  unauthenticated (§7.5); Phase 2b browser capture is a separate opt-in
  adapter, not a MediaProvider implementation detail.
- Errors map into the §3.1 taxonomy (`RateLimited` for `-352`,
  `TerminalProviderError` for `-404`/`62002`/`62004`, etc.).

## 4. Configuration and secrets

Secrets are environment variables only; everything else lives in
`~/.by2kb/config.toml`.

| Env var | Purpose |
| --- | --- |
| `BY2KB_SUPADATA_KEY` | Supadata provider (optional) |
| `BY2KB_LLM_API_KEY` / `BY2KB_LLM_BASE_URL` / `BY2KB_LLM_MODEL` | skill runner (`LlmClient`; defaults to the Volcengine Ark preset, §3.5) |

Config file: default skills, default destination, language preferences, provider
order, retry policy, `library_root`. Secrets are never logged and never written
into artifacts.

## 5. Testing strategy

- **WBI signing**: pin the official test vectors carried by bilibili-digest's
  `tests/wbi.test.js` (imgKey/subKey → mixin key; params+wts → `w_rid`). These
  are the cheapest possible correctness proof for the riskiest port.
- **Providers**: recorded HTTP fixtures (VCR-style) per platform, including the
  error envelopes (`-352`, `-404`, `need_login_subtitle`, Supadata 202/206/429);
  assert error-taxonomy mapping, not raw payloads.
- **Normalizer/writers**: golden-file tests — normalized schema in, raw.md bytes
  out; determinism asserted by re-render equality.
- **Idempotency**: re-ingest returns exit code 4 and identical artifact paths;
  `--refresh` refetches.
- **E2E (local mode)**: fixture providers wired through the real runner → real
  SQLite → real filesystem sink, asserting the full artifact directory.

## 6. Out of scope for this document

Service mode implementation, IM bot adapters, Lark/Notion sinks, skill registry
UX, phase 2 media/ASR, phase 2b browser capture, and the hermes plugin itself
(separate repo, per hermes policy) — all have their contracts pinned above where
they touch Milestone 1, and their design docs come with their milestones.

## 7. Design decisions (all closed)

1. **Closed (2026-08-17): Python 3.12+ confirmed** — §1.1.
2. **Closed (2026-08-17): skill-runner LLM** — direct OpenAI-compatible calls
   behind an `LlmClient` abstraction; first implementation is the Volcengine
   Ark preset, other providers later polymorphic implementations — §3.5.
3. **Closed (2026-08-17): `--re-enrich` confirmed** — a duplicate with
   different skills re-runs enrichment only (no refetch); pure duplicates stay
   idempotent (exit code 4), `--refresh` forces a full refetch — §3.2.
4. **Closed (implementation constraint): Windows-first encoding guard** —
   Windows dev / Linux deploy; the spike's console output mojibake'd UTF-8
   titles under the GBK Windows console while the data itself parsed fine.
   Rule: pathlib + explicit UTF-8 everywhere (files, subprocess, stdout
   reconfiguration).
5. Bilibili subtitle gating — **closed (2026-08-17): native subtitles dropped;
   audio+ASR is Bilibili's primary route (owner decision).** Multi-video probe
   (Appendix A.1): 23/23 videos returned 0 subtitle tracks unauthenticated —
   Bilibili gates AI subtitle tracks behind login, and unauthenticated human CC
   tracks were never observed. The logged-in `SESSDATA` route that would unlock
   them was evaluated and **rejected** for two independent reasons:
   (a) a session cookie cannot even be exercised in an agent/LLM-assisted
   workflow — credential-shaped strings trip safety filters the moment they
   enter the context, so the path is untestable as designed;
   (b) a forwarded-video bot should not hold a user's Bilibili login long-term
   — `SESSDATA` expires and storing it is a real account-safety liability.
   With no viable way to authenticate, Bilibili ingestion goes straight to the
   audio+ASR path (Phase 2), whose media side works unauthenticated. The
   burned-in-vs-AI distinction for any single video no longer matters — both
   are covered by ASR. Two honest caveats recorded with the decision:

   - **The audio+ASR route is not zero-cost.** A native transcript is nearly
     free text; ASR needs a model — self-hosted (e.g. faster-whisper: no money,
     but accuracy risk on colloquial/mixed audio) or hosted (accurate, but
     per-minute cost). Bilibili ingestion therefore lands on a cost-ladder rung
     that YouTube native transcripts usually skip; ASR budget and provider
     choice belong to Milestone 3.
   - **Reference-implementation reconciliation:** bilibili-digest retrieves
     subtitles fine, but only because it runs inside the user's own logged-in
     browser — its API calls use `credentials: "include"` against
     `api.bilibili.com` and inherit the user's `SESSDATA`. Its own error path
     (`background.js`) maps an empty track list to `NEED_LOGIN` or
     `NO_SUBTITLE`, and it has no burned-in fallback either. Same APIs,
     different authentication context — nothing there contradicts the 23/23
     result; it confirms login is the differentiator a server-side service
     cannot legitimately obtain.

   Roadmap updated accordingly: the Milestone-1 "Bilibili native-transcript
   adapter" item is dropped; Bilibili ingestion is tracked under Milestone 3.

## Appendix A — Bilibili spike results (2026-08-17)

Spike: Python 3.12 stdlib only, unauthenticated, target
`https://www.bilibili.com/video/BV1jmbD65EP2/` (166 s video).

| Step | Call | Result |
| --- | --- | --- |
| WBI keys | `x/web-interface/nav` | OK unauthenticated; daily keys parsed from `wbi_img` |
| Metadata | `x/web-interface/view?bvid=...` | OK — aid/cid/title/duration |
| Subtitles | `x/player/wbi/v2` (WBI-signed) | API OK, **0 tracks**, `need_login_subtitle: null` — needs SESSDATA to disambiguate (§7.5) |
| Media manifest | `x/player/wbi/playurl` (WBI-signed, `fnval=4048`) | OK unauthenticated — 6 video + 3 audio DASH streams; video capped at 480p without login |
| Audio download | CDN `Range: bytes=0-262143` + `Referer` | HTTP 206, valid MP4 (`ftypiso5`); full audio track ≈ 1.7 MB |
| Video download | same | HTTP 206, valid MP4 |

Implications:

- **Bilibili phase-2 audio fallback needs no browser and no third-party
  downloader**: one WBI-signed `playurl` call plus a direct CDN download. DASH
  delivers audio and video as separate tracks, so the audio track alone suffices
  for ASR — the muxed-mp4 case is not expected on Bilibili.
- CDN media URLs require a bilibili.com `Referer` header; range requests work,
  so resumable/chunked downloads are available.
- Unauthenticated video quality is capped (480p). Irrelevant for ASR (audio is
  independent of video quality); relevant only if frame sampling is ever wanted.
- WBI signing, error envelopes, and the whole flow match the bilibili-digest
  reference implementation; its official test vectors remain the correctness
  baseline for `bilibili_wbi.py`.

### A.1 Multi-video subtitle probe (same day)

The single-video 0-track result above was ambiguous — login-gated AI subtitles
vs subtitles burned into the frames. Two follow-up probes (same stdlib-only
setup, unauthenticated) narrowed it down:

| Probe | Targets | Result |
| --- | --- | --- |
| Known-subtitle videos | `BV1yhKY6QEfG` (786 s), `BV1DAgS6SEqa` (726 s) — owner: "these have subtitles" | `view` OK; `player/wbi/v2` API OK (`code=0`, not risk-blocked) but **0 tracks**, `view.subtitle.list` empty; audio DASH manifest + CDN download OK for both (HTTP 206, `ftypiso5`) |
| Control sweep | 20 videos from `x/web-interface/popular` | **0 tracks for all 20** |

Findings:

- **Unauthenticated native-transcript retrieval is effectively dead on
  Bilibili.** 23/23 videos returned 0 subtitle tracks. That is too universal to
  be all burned-in content, so Bilibili gates AI subtitle tracks behind login.
  The logged-in `SESSDATA` that would unlock them was evaluated and rejected
  (untestable in-context + account-safety liability), so without it the adapter
  can only report "no tracks visible" and fall through to the audio path — see
  §7.5 for the decision.
- **Owner clarification:** no CC toggle was observed on the two known-subtitle
  videos, which points to burned-in subtitles there (no track exists even for a
  logged-in session). Remaining disambiguation for those two specific videos,
  if it ever matters: devtools check for `aisubtitle.hdslb.com` requests, or a
  SESSDATA probe. See §7.5.
- **The audio fallback is unaffected by all of this:** every probed video
  yielded a separate DASH audio track downloadable unauthenticated, so Phase 2
  does not depend on the subtitle/login question at all.

## Appendix B — YouTube spike results (2026-08-17)

Spike: `youtube-transcript-api` 1.2.4 (keyless, talks to YouTube directly),
unauthenticated, target `https://youtu.be/l38ceFOWOAE`.

| Step | Result |
| --- | --- |
| List tracks | 1 track: `zh`, human-uploaded (`is_generated=False`) |
| Fetch transcript | OK — 1057 timestamped segments, correct text |

Findings:

- **YouTube native transcripts work unauthenticated and keyless.** A real
  human-uploaded track was listed and fetched in full through
  `youtube-transcript-api` — no login, no API key, no third-party service.
- This validates the §1.1 preference for the keyless YouTube path as the
  default. Supadata remains an optional fallback behind the same provider
  interface for when YouTube's player internals change and the library
  temporarily breaks.
- Unlike Bilibili, both human and auto-caption tracks are reachable without
  auth, so for YouTube the Phase-1 native-transcript path is the primary route
  and audio+ASR is only the no-track fallback — the exact inverse of Bilibili.
