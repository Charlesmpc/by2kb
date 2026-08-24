# Changelog

## Unreleased

- Name durable Markdown artifacts `raw.<title>.md`, `short.<title>.md`, and
  `long.<title>.md`; safely retire fixed legacy names and unambiguous prior
  suffix-based names on republish.
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
