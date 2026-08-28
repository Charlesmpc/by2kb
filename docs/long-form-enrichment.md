# Long-form enrichment

Long transcripts should not be copied wholesale into both final LLM calls. by2kb
uses `LongFormEnrichmentPipeline` to prepare a grounded, bounded context first.

## Planning

`TranscriptChunkPlanner` estimates tokens conservatively, groups only complete
normalized transcript segments, and honors both token and duration budgets. A single
oversized segment is marked `oversized` but is never split inside the segment. Short
transcripts retain the existing two-call fast path.

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

## Provenance

Every enriched job publishes `enrichment-plan.json`. It records the pipeline and
schema versions, transcript and plan hashes, chunk ranges, hierarchy edges, cache
keys/hits, runtime identity, and Skill versions. It intentionally excludes
intermediate note bodies and hidden reasoning. Generated Markdown records the
pipeline version and plan hash in frontmatter.

External-Agent claims receive the same deterministic chunk plan and prompts. The
Agent execution adapter and staged submission lifecycle are layered on this contract
in the Agent-provider phase.
