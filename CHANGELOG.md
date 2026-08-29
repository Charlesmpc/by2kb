# Changelog

## Unreleased

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
