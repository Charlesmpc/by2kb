# Changelog

## Unreleased

## 0.4.3 - 2026-09-09

- Add an opt-in Bilibili `browser` source and explicit `[sources.fallback]` route,
  including short-link failures, backed by a dedicated Chromium login session.
- Add `by2kb browser install` / `login`, optional local CDP attachment and browser
  diagnostics; browser profiles remain outside the installation and survive upgrades.
- Make browser title/author extraction best-effort, validate downloaded audio and
  reject detected incomplete previews instead of silently summarizing partial media.
- Explain native HTTP 412 and browser authentication/download failures, with bounded
  fallback, actionable errors and privacy-safe attempt diagnostics. This is not a
  guarantee against site risk control; login and human verification remain manual.
- Reuse native Bilibili metadata instead of fetching it twice per audio ingestion.

## 0.4.2 - 2026-09-08

- Add a video Referer to Bilibili metadata requests and client compatibility
  parameters before WBI playback signing (investigation of #36). This does not
  guarantee recovery from network-dependent HTTP 412 responses.

## 0.4.1 - 2026-09-07

- Fix #34: count job executions once per ingestion, retry, refresh, or re-enrichment
  instead of counting status transitions. Expose `attempt_count` in task-control
  JSON and document the preserved legacy counts from versions through 0.4.0.
- Use a version-independent installation Skill URL in the README.

## 0.4.0 - 2026-08-29

- Add a portable `install-by2kb` bootstrap Skill so an Agent can install, configure,
  diagnose, and connect the packaged Hermes integration without cloning the repository.
- Add the non-interactive `by2kb init --preset agent-local` path and make local
  faster-whisper the default ASR; Doubao with private TOS staging remains an explicit
  cloud-provider option.
- Define an upgrade-safe personalization boundary: configuration, credentials, job
  history, models, knowledge artifacts, and custom Skills remain user-owned, while a
  personalized Hermes runtime Skill overrides the replaceable packaged adapter copy.

## 0.3.0 - 2026-08-29

- Add a configurable source-provider registry, adapt native Bilibili ingestion to the
  shared contract, and add an optional yt-dlp transcript/audio provider.
- Complete the YouTube URL journey with caption-first ingestion, configured ASR
  fallback, stable identity/provenance, and Hermes interception.
- Add a pluggable ASR provider registry with deterministic explicit selection and
  ordered `auto` fallback.
- Add optional local `faster-whisper` transcription with timestamped segments,
  configurable runtime settings, and explicit model status/install commands.
- Accept local audio and video paths in `by2kb ingest`, with content-addressed
  identity, ffmpeg audio extraction, and privacy-safe filename provenance.
- Expand guided initialization for local/cloud ASR and add read-only `by2kb doctor`
  diagnostics with a stable Agent-readable JSON report.
- Add deterministic transcript-quality metrics and gates so unusable transcripts stop
  before enrichment while borderline outputs carry a visible warning.
- Add a cached long-form enrichment pipeline that plans segment-safe chunks,
  recursively reduces grounded notes, and records a public hierarchy trace.
- Add a staged Agent enrichment provider using bounded `next`/`submit` operations,
  allowing Hermes subscription-authenticated runtimes to execute the shared plan.
- Add a versioned Agent task-control protocol with stable status snapshots, bounded
  waits, cooperative cancellation, and checkpoint-aware retry.
- Persist the selected ASR provider, model, and runtime provenance in source and
  transcript artifacts.

## 0.2.1 - 2026-08-25

- Name durable Markdown artifacts `raw.<title>.md`, `short.<title>.md`, and
  `long.<title>.md`; safely retire fixed legacy names and unambiguous prior
  suffix-based names on republish.
- Fall back through Bilibili backup audio CDNs when the preferred CDN returns an
  HTTP or transport failure.
- Retry transient Doubao AUC failures per long-audio chunk and persist successful
  chunk transcripts so a resumed ingestion does not resubmit completed work.
- Classify queued, rate-limited, overloaded, and HTTP failure responses correctly;
  harden concurrent checkpoint writes and cache invalidation.

## 0.2.0 - 2026-08-23

- Add agent-first enrichment with durable `claim`, `complete`, and `fail` commands.
- Add short-abstract and long-form study-note execution through external agent hosts.
- Add the Hermes reference plugin, authorized URL interception, and host-owned LLM use.
- Add the bundled `video-to-knowledge` Hermes Skill and one-command plugin installer.
- Add guided `by2kb init` configuration for TOS, Doubao ASR, LLM mode, and local KB.
- Keep standalone API enrichment and transcript-only operation as supported executors.
