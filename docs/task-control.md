# Agent task control

`by2kb` exposes a small, versioned CLI protocol so an Agent host can observe and
control an ingestion job without parsing logs or keeping a shell process alive.

```bash
by2kb status <job-id> --json
by2kb wait <job-id> --timeout 30 --json
by2kb cancel <job-id> --json
by2kb retry <job-id> --json
```

All four commands return the same schema. `schema_version` is currently `1`:

```json
{
  "schema_version": 1,
  "event": "snapshot",
  "job_id": "01J...",
  "state": "enriching",
  "stage": "enrichment",
  "progress": 0.85,
  "message": "enriching",
  "terminal": false,
  "retryable": false,
  "cancel_requested": false,
  "error": null,
  "artifacts": [],
  "updated_at": "2026-08-28T10:00:00+00:00"
}
```

Agents should branch on `schema_version`, `event`, `state`, `terminal`, and
`retryable`. Human-readable `message` text may change without a schema-version
change.

## Waiting

`wait` returns when the state or artifact set changes, the job reaches a terminal
state, or the timeout expires. Its event is respectively `state_changed`,
`terminal`, or `timeout`. A timeout is a successful observation and does not alter
the job. The timeout must be between 0 and 300 seconds, which keeps bot callbacks
bounded and lets the host choose its own scheduling policy.

## Cancellation

Queued work is cancelled immediately. Active work records a cancellation request,
which is honored at the next safe pipeline boundary and before each long-form LLM
operation. This is cooperative cancellation: an in-flight media download, ASR
request, or LLM call is allowed to finish before cleanup and transition to
`cancelled`.

Calling `cancel` on an already cancelled job is idempotent. Other terminal jobs
cannot be cancelled.

## Retry and checkpoint reuse

Only `needs_auth`, `rate_limited`, and `failed_retryable` jobs accept `retry`.
The original URL or local path is kept in the private jobs database so the Agent
does not need to remember it.

When raw Markdown and transcript JSON are still present and an enrichment task was
created, retry starts again at enrichment. Otherwise it refreshes acquisition and
ASR. Long-form intermediate caches and provider-specific ASR checkpoints remain
eligible for reuse. The source path used for retry is never added to public
artifacts beyond the existing privacy-safe provenance rules.

## Recommended Agent loop

1. Start `by2kb ingest` and retain the returned job ID.
2. Use bounded `wait` calls, or periodic `status`, to update the user.
3. When the state is `enrichment_pending`, execute the Agent enrichment
   `next`/`submit` loop.
4. Stop when `terminal` is true. Offer retry only when `retryable` is true.
5. Forward a user cancellation through `cancel` and continue observing until the
   task reports `cancelled`.

The Hermes reference plugin follows this contract before requesting each staged LLM
operation, so cancellation and terminal failures cannot accidentally trigger more
model work.
