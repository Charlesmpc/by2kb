# Changelog

## Unreleased

- Fix missing source configuration defaulting to Bilibili only: Bilibili and YouTube
  now both have default routes, consistently across config loading and initialization
  (#45). YouTube remains captions-first with configured ASR for audio fallback.
- Include yt-dlp in base dependencies; keep existing YouTube extras compatible.
  Browser support remains opt-in and no installation runs during video processing.
- Apply defaults without rewriting existing configuration. Preserve explicit provider
  lists/environment overrides, disabled yt-dlp, custom ASR, cookies, browser fallback,
  and knowledge-base paths. Document adoption for old explicit Bilibili-only lists.
- Improve restricted-source and incomplete-installation errors; doctor explains
  disabled yt-dlp without demanding unused dependencies.

## 0.6.0 - 2026-09-17

- Prepare the native Hermes plugin for catalog admission: correct `provides_hooks`,
  synchronize its version, and declare its homepage, license and empty tool/env requirements.
- Bundle and register `by2kb:install-by2kb` alongside the runtime Skill. Explain
  independent CLI installation, local-first setup, Windows PATH, optional cloud/browser
  components, and preservation of existing configuration.
- Refuse to overwrite Hermes-managed Git/catalog installs from the copy installer,
  even with `--force`; preserve source pins and metadata. Enable copied plugins in
  the explicitly selected Hermes home, not an unrelated active profile.
- Add real, isolated Hermes installer validation and ownership regression tests.
- Refresh installation/upgrade docs and outdated README implementation status.
- Catalog submission is separate from release; the bare catalog name is not available
  until the upstream entry is accepted. See [0.6.0 release notes](docs/releases/0.6.0.md).

## 0.5.3 - 2026-09-13

- Default Agent CLI next/claim to small, file-backed control envelopes instead of
  dumping prompts into terminal output. Keep explicit --inline-prompts compatibility.
- Add checksum-verified, bounded Unicode prompt paging with enrichment read; Hermes
  loads and verifies request files directly while retaining operation IDs in code.
- Split oversized single transcript segments into lossless character ranges; preserve
  original transcript and timestamps. Pipeline 1.1 separates the updated cache identity.
- Reject excessive Agent prompts explicitly rather than truncating them; update the
  packaged Skill to read complete operations and never relabel stale responses.
- Includes the 0.5.2 synchronization fixes, which were merged but not published separately.
- See [0.5.3 release notes](docs/releases/0.5.3.md) for upgrade and compatibility details.

## 0.5.2 - 2026-09-13

- Make identical Agent operation submissions idempotent; reject stale IDs and
  conflicting content without overwriting saved responses or pending operations.
- Serialize external enrichment mutations across local processes on Windows and
  POSIX. Contending callers receive a retryable-by-caller busy diagnostic.
- Let Hermes resynchronize stale operations with bounded retries and freshly
  generated output; never attach an old answer to a new operation ID.
- Recheck task status before error notifications, preserve completed results on
  late failure reports, and retain resumable progress on host-side errors (#41).
- See [0.5.2 notes](docs/releases/0.5.2.md) for compatibility and validation limits.

## 0.5.1 - 2026-09-12

- Generate a concise, transcript-grounded title before enrichment when metadata is
  blank, a bare BVID, or an exact generic placeholder; preserve readable titles.
- Reuse the API or external Agent enrichment client, including staged next/submit
  operations. Validate JSON title responses and exact supporting evidence.
- Keep generated titles and provenance consistent in source/transcript JSON, raw
  and summary frontmatter, filenames, prompts and enrichment plans.
- Default absent/null/empty `title_source` to `original`. Raw-only remains free of
  LLM requirements. No archive migration runs on upgrade; explicit re-enrichment
  checks the selected job. See [0.5.1 notes](docs/releases/0.5.1.md).

## 0.5.0 - 2026-09-12

- Recover from Bilibili `view` HTTP 412 through `pagelist`: obtain CID/duration
  and continue native audio download and configured ASR without requiring a browser.
  Title/author may be unavailable; use the BVID title and preserve metadata provenance.
- Retain the opt-in browser Plan B introduced in 0.4.3. Basic installations do not
  require Playwright or Chromium; browser fallback requires separate dependencies,
  a compatible browser session and manual login/verification when the site requests it.
- Bound native/browser discovery, shared CDN download and validation stages; add
  cancellation polling, incomplete-file cleanup and persisted acquisition statuses.
- Detect explicit login/verification gates before the browser discovery deadline;
  a generic QR-login button or incomplete audio alone is not proof login is required.
- Map ffprobe's own timeout to a retryable error while preserving external cancellation.
- Resolve short URLs once with validated destinations and a 10-second budget.
  Browser fallback no longer retries failed short-link resolution; use a full BVID
  video URL if short-link resolution fails.
- See [0.5.0 release notes](docs/releases/0.5.0.md) for setup and validation limits.

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
