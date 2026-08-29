# Agent enrichment provider

by2kb supports two LLM ownership models behind one deterministic enrichment provider:

| Mode | Authentication and cost | Execution |
| --- | --- | --- |
| Direct API | User supplies an API key; usage is billed by that API provider. | by2kb calls the configured OpenAI-compatible endpoint. |
| Hosting Agent | The Agent keeps its own OAuth, subscription, or provider profile; by2kb receives no token. | The host executes bounded callback operations and submits only Markdown results. |

The Agent path is callback-based; by2kb never calls itself and does not require MCP.
Hermes is the reference adapter. Its host-owned model may use a subscription or OAuth
profile without copying that credential into by2kb configuration, logs, cache
metadata, or knowledge artifacts.

## Staged protocol

After ingestion returns `enrichment_pending`, an Agent repeats:

```bash
by2kb enrichment next JOB_ID \
  --provider hermes --model HOST_PROFILE --runtime-version VERSION --json

by2kb enrichment submit JOB_ID \
  --operation-id OPERATION_ID --output-file result.md \
  --provider hermes --model HOST_PROFILE --runtime-version VERSION --json
```

`next` either returns `needs_input` with one system/user prompt pair or `completed`
with artifact paths. Each operation includes `timeout_s` and `max_output_bytes`.
`submit` accepts only the currently pending operation ID, non-empty UTF-8, the same
runtime identity, and output within the advertised bound.

The runtime identity, prompt content, selected Skills, transcript/plan hashes, and
pipeline version participate in operation/cache identity. Provider, model, runtime
version, plan hierarchy, and cache provenance are recorded; OAuth tokens and hidden
Agent state are not.

Legacy `enrichment claim` and `enrichment complete` remain available for adapters
that already generate the two final documents themselves. New adapters should use
the staged protocol so long transcripts benefit from chunking, cache reuse, partial
retry, and bounded operations.
