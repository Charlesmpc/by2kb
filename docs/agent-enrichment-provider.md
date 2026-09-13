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

`next` either returns `needs_input` with one operation ticket or `completed`
with artifact paths. Each operation includes `timeout_s` and `max_output_bytes`.

## File-backed CLI transport (0.5.3)

By default, CLI `next` returns schema 2 with an operation ID, absolute `request_file`,
SHA-256, byte count, estimated input tokens and prompt character counts. The complete
system/user prompts live in that content-addressed UTF-8 JSON file, not stdout.
The bundled Hermes adapter reads and verifies it in code before calling the host
model, retaining the operation ID in code rather than asking the model to copy it.

Manual Agents read one field at a time:

```bash
by2kb enrichment read --request-file REQUEST_FILE --field system_prompt --offset 0 --limit 1000 --json
by2kb enrichment read --request-file REQUEST_FILE --field user_prompt --offset 0 --limit 1000 --json
```

For each field, advance to `next_offset` until `eof`. Offsets count Unicode characters,
not bytes or tokens; pages contain at most 2,000 characters. Check `operation_id` on
every page. If the host still truncates a page, repeat that offset with a smaller
limit. Do not print the full request or submit a summary of only the first page.

CLI `claim` likewise returns a small file reference to the legacy full manifest;
staged callers should use only `next`, not `claim && next`. The paged `read` command
reads operation files from `next`, not legacy claim manifests. Existing custom
adapters can explicitly request `--inline-prompts` on `next` or `claim` for schema-1
compatibility, accepting its output-size risk. Python service APIs remain unchanged.
CLI and Agent must share filesystem access (or explicitly transfer and verify the
request file); the file path is not a remote download URL.

File transport does not expand model context. Program-planned transcript chunks and
reductions still apply. Agent callbacks reject prompts over 24,000 estimated tokens
without truncation; reduce configured budgets or oversized intermediate output when
this happens. The estimate is heuristic, not a model-specific tokenizer guarantee.
Request files have a 32 MiB safety limit; no transcript is shortened to meet it.
Saved request files may contain private transcript content and belong in private job
storage, not bug reports. Existing personalized Skills are not overwritten on upgrade;
apply this transport guidance to them explicitly.

## Submission contract

`submit` accepts only the currently pending operation ID, non-empty UTF-8, the same
runtime identity, and output within the advertised bound.

As of 0.5.2, callers must inspect the JSON `status` returned by `submit`, even
when the CLI exits successfully:

- `accepted`: new response saved.
- `already_accepted`: the same ID and exact content were saved earlier; no mutation.
- `completed`: an identical replay after completion; use the returned artifacts.
- `operation_mismatch`: no matching pending operation. Query status and `next`,
  then generate fresh content for that operation. Never relabel an old answer.
- `operation_content_conflict`: the ID already has a different saved answer.
  Stop and inspect the task; do not overwrite the accepted response.

Conflicts include `error.reason_code`, submitted/expected operation IDs and a
recovery hint. Diagnostic logs contain IDs, not prompts or response content.
Local process locks serialize enrichment mutations per job. A busy error means
another operation owns the lock; retry later, not in a tight loop. The lock is
released by the OS on process exit, and is not held while the host calls its LLM.
Use a local filesystem for job/session state; distributed locking is not provided.

The bundled Hermes adapter retries operation mismatches at most three times with
fresh `status`/`next` calls and fresh model output. Host-side errors no longer call
`enrichment fail` automatically. It rechecks status, reports already-completed
results as success, and otherwise leaves saved progress available for resumption.

The runtime identity, prompt content, selected Skills, transcript/plan hashes, and
pipeline version participate in operation/cache identity. Provider, model, runtime
version, plan hierarchy, and cache provenance are recorded; OAuth tokens and hidden
Agent state are not.

Legacy `enrichment claim` and `enrichment complete` remain available for adapters
that already generate the two final documents themselves. New adapters should use
the staged protocol so long transcripts benefit from chunking, cache reuse, partial
retry, and bounded operations.


## Missing titles (0.5.1)

The staged protocol may first request grounded title JSON before any summary or
chunk operation. Submit it through the same operation API. Do not assume the first
operation returns Markdown. Invalid title output is rejected before caching.
Legacy claim callers must follow `title_required` and switch to `enrichment next` for
these jobs; summary prompts are withheld until the title is resolved. See
[0.5.1](releases/0.5.1.md) for validation, abstention and raw-only semantics.
