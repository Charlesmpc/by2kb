# Bounded Bilibili acquisition: implementation and remaining work

## Scope

This change bounds media acquisition, not ASR, enrichment, publishing, or the
entire CLI process. It does not install browser dependencies, change pipx, start
browsers, export cookies, or prove live Bilibili access.

## Implemented

- Resolve short links once using HTTP, with at most five redirects and a ten-second
  deadline. Each next destination is checked before another request: only b23.tv
  redirects and recognized Bilibili video destinations are accepted. A full BVID
  and supported hostname are required. Browser preparation receives the canonical
  identity; fallback never repeats resolution or opens a browser to expand a link.
- On view HTTP 412, try pagelist for CID/duration. Missing title/author remain empty,
  aid is an internal zero sentinel, and `metadata_source=pagelist_after_view_412`
  marks partial metadata. Native preparation checkpoints `metadata.json` before
  requesting media, then tries native playback before the configured browser route.
  Transport failures and missing playback streams are recoverable, not proof that
  the video was deleted. There is at most one acquisition fallback.
- Discovery has a 120-second shared preparation budget, with native discovery
  capped at 30 seconds and browser discovery capped at 60 seconds (a smaller
  configured browser timeout is honored). These include browser connection,
  navigation, and JavaScript evaluation, not just the polling loop.
  Browser polling reads the same enclosing deadline (including the fallback
  budget), rather than restarting the timeout after navigation. Explicit login
  and verification gates are classified during polling, before expiry; available
  audio streams still take precedence over unrelated login text.
- Entering download switches to a 600-second deadline. CDN alternatives share one
  download deadline; the fallback chain retains the original deadline instead of
  resetting it on a second route. Discovery time is accounted separately from
  download/validation, although the download deadline continues to age during
  an intervening browser discovery attempt.
- Download connection/read idle limit is 30 seconds. The read limit measures
  incoming chunks, not the time required to accumulate a 256-KiB buffer. Only HTTP
  200, nonempty, size-bounded responses with matching Content-Length (if supplied)
  are accepted. Failed/aborted partial files are removed. Local
  `download-progress.json` records bytes, average throughput, and last progress
  timestamp; it does not contain signed URLs or cookies.
- Both native and browser audio receive a duration sanity check and a one-second
  audio decode sample within a 30-second validation budget. Rejected audio is
  removed. ffprobe has a 30-second deadline and is killed/reaped on cancellation.
  Its own timeout is a retryable provider error, including for local-file jobs;
  external task cancellation is propagated unchanged.
  Short/incomplete media is a transient acquisition failure, not an automatic
  login diagnosis. A generic QR-login button alone is not authentication evidence.
- Preparation polls cancellation every 250 ms, including while provider calls
  await network/browser responses. Task cancellation and download cleanup are
  propagated. Cleanup waiting is bounded to one second per helper invocation;
  it does not claim to forcibly terminate cancellation-hostile Python code.
- Persisted statuses distinguish fetching_metadata, browser_connecting,
  browser_loading, waiting_media, downloading_media, and validating_media.
  The task-control status API recognizes these states and no longer raises a
  KeyError when reading them. Progress numbers are stage estimates, not measured
  acquisition completion percentages.

## Deferred / limitations

- A job is still created only after source resolution. `resolving_source` is a
  recognized status but is not emitted before a job exists. Resolution failures
  consequently do not yet have their own durable job record. Supporting provisional
  jobs needs a deliberate identity/deduplication/retry migration. The ten-second
  resolver budget is separate from the 120-second preparation budget.
- Metadata checkpoints are diagnostic artifacts, not a resumable stage cache.
  Browser metadata is not merged with an earlier native checkpoint. Browser
  success records its own provenance, while earlier native attempt categories are
  retained; a full route-by-route timing/event journal remains unimplemented.
- Download progress stays in the work directory and is not yet exposed in the
  task-control JSON protocol. Writes are synchronous and not atomic snapshots.
- Browser page/response cleanup and the outer provider wait are bounded, but
  browser context/Playwright shutdown and cancellation-hostile tasks are not
  guaranteed to be killed. Process isolation, an external watchdog, and complete
  process-tree cleanup remain needed for hard termination guarantees. The ffmpeg
  decoder kills on cancellation but its final process wait has no separate bound.
- Decode validation samples one second; without trustworthy expected duration,
  it cannot prove that every frame of a long response is intact or that it is not
  a preview. Multipart/browser support and access restrictions are unchanged.
- Native playback still uses the existing WBI route and sends the zero aid sentinel
  for pagelist-only metadata alongside BVID/CID. No new unsigned playback route
  or live compatibility claim is introduced.

## Why a caller could still report 7200 seconds

`by2kb/integrations/hermes/__init__.py::_run_by2kb` passes `timeout=7200` to
`subprocess.run`. That is the hosting integration's whole-command watchdog, not
an acquisition timeout. It is intentionally unchanged: shortening it globally
could interrupt legitimate ASR/enrichment. Acquisition now has its own stage
limits; a later redesign can give the host an asynchronous job ID and bounded
wait/status calls without conflating acquisition with the entire workflow.

## Verification

The development `.venv` test suite is run locally with `.venv/bin/pytest -q`.
Regression coverage includes a hung browser evaluate, trickling and incomplete
CDN bodies, shared fallback download deadlines, cancellation during hung provider
work, ffprobe cancellation cleanup, metadata checkpointing, canonical-only
resolution, and persisted task-control acquisition states. Network calls and
browser objects in these tests are mocked; no browser or live video smoke test
was performed for this change. Historical smoke results in source-provider docs
are not evidence for this implementation.
