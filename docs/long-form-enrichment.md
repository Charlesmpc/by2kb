# Long-form enrichment

Long transcripts should not be copied wholesale into both final LLM calls. by2kb
uses `LongFormEnrichmentPipeline` to prepare a grounded, bounded context first.

## Planning

`TranscriptChunkPlanner` estimates tokens heuristically and normally groups complete
normalized transcript segments using token and duration budgets. Since pipeline 1.1
(by2kb 0.5.3), a segment exceeding the token budget is split into lossless character
ranges (`char_start`, `char_end`), retaining its original segment index and time range.
No finer timestamps are invented. The original normalized transcript is unchanged.
Very small budgets that cannot fit timestamp overhead can still mark pieces oversized.
Short transcripts retain the existing two-call fast path.

Defaults can be overridden in `config.toml`:

```toml
[enrichment.long_form]
threshold_tokens = 8000
chunk_token_budget = 4000
chunk_duration_s = 900
reduce_token_budget = 6000
reduce_group_size = 4
```

## Execution and cache

Each chunk produces grounded intermediate notes with its source time range. Notes are
recursively reduced until one bounded context remains; the existing short-abstract
and deep-study Skills then consume that context.

Private intermediate cache entries live under
`~/.by2kb/enrichment-cache/<prefix>/<key>.json`. Cache keys cover transcript/chunk
content, segment plan, runtime provider/model, both Skill names and versions, prompt
pipeline version, and reduction children. A retry therefore reuses successful chunks
and reductions without refetching or retranscribing media.

Pipeline 1.1 changes cache identity: older intermediate caches remain on disk but
are not reused under the new plan. Existing archives are not automatically rewritten.
The Agent path also guards complete prompts at 24,000 estimated tokens, including
custom instructions and reductions. An excessive model response can cause a later
reduction to hit this guard; the operation stops rather than silently dropping text.

## Provenance

Every enriched job publishes `enrichment-plan.json`. It records the pipeline and
schema versions, transcript and plan hashes, chunk ranges, hierarchy edges, cache
keys/hits, runtime identity, and Skill versions. It intentionally excludes
intermediate note bodies and hidden reasoning. Generated Markdown records the
pipeline version and plan hash in frontmatter.

External Agents execute the same plan through bounded `enrichment next` and
`enrichment submit` operations. Successful intermediate nodes enter the same cache,
so an Agent retry resumes at the first missing operation.
