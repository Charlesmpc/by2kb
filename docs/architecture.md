# Proposed architecture

> Design document. Components described here are not implemented yet.

## Components

```text
IM Adapter ──► Capture API ──► Queue ──► Transcript Worker
                                      │          │
                                      │          ├─ YouTube adapter
                                      │          ├─ Bilibili adapter
                                      │          └─ Audio/ASR adapter (phase 2)
                                      │
                                      └────► Job Store
                                                 │
Transcript Worker ──► Normalizer ──► Raw Writer ─┼─► Skill Runner
                                                 │       │
                                                 │       ▼
                                                 └─► Updated Writer
                                                          │
                                                          ▼
                                                  Knowledge Sink
                                                          │
                                                          ▼
                                                   IM Notification
```

## Boundaries

- **Input adapters** authenticate an IM event, extract URLs and user options, and submit jobs.
- **Source adapters** resolve canonical IDs, metadata, and native transcript tracks.
- **Media/ASR adapters** are optional phase-two fallbacks and must be independently enabled.
- **Normalizer** converts provider responses into one timestamped transcript schema.
- **Raw writer** renders deterministic Markdown and preserves provider JSON.
- **Skill runner** produces a new updated artifact; it never changes raw data.
- **Knowledge sinks** publish artifacts and return durable destination references.
- **Notifier** reports queue, completion, partial completion, and actionable failures.

## Suggested job states

```text
accepted
  → resolving
  → fetching_transcript
  → normalizing
  → raw_published
  → enriching
  → updated_published
  → completed
```

Terminal/exception states:

```text
needs_auth
no_native_transcript
needs_audio_fallback
rate_limited
failed_retryable
failed_terminal
cancelled
```

`raw_published` is an intentional checkpoint. If enrichment fails, the raw transcript is
still a useful successful result and can be updated later without refetching.

## Normalized transcript schema

```json
{
  "schema_version": 1,
  "source": {
    "platform": "youtube",
    "video_id": "...",
    "canonical_url": "https://...",
    "title": "...",
    "author": "...",
    "duration_ms": 123456
  },
  "transcript": {
    "provider": "...",
    "kind": "human|auto_caption|asr",
    "language": "en",
    "available_languages": ["en", "zh-CN"],
    "fetched_at": "RFC3339 timestamp",
    "segments": [
      {
        "start_ms": 0,
        "duration_ms": 3200,
        "text": "...",
        "confidence": null
      }
    ]
  }
}
```

## Skill execution contract

A skill receives:

- normalized transcript JSON;
- rendered raw Markdown;
- source metadata;
- user language and destination preferences;
- optional supporting files from the skill package.

It returns one or more named sections and provenance metadata. The system records the
skill identifier, content hash/version, model/provider, execution time, and warnings in
the updated document frontmatter.

## Idempotency

Recommended identities:

- source identity: `platform + canonical_video_id`;
- transcript identity: source identity + language + transcript provider/version;
- enrichment identity: transcript hash + ordered skill hashes + processing options;
- destination identity: enrichment identity + sink + destination path.

This permits independent refetch, re-enrichment, and republishing without silently
replacing evidence.

## Security

- Verify IM webhook signatures and map senders to configured users.
- Encrypt provider keys and authenticated platform sessions at rest.
- Do not print cookies, API keys, private video metadata, or transcript content in logs.
- Use a dedicated Bilibili account if authenticated subtitle access is needed.
- Restrict each knowledge sink token to its target folder/space where possible.
- Treat imported skills as executable instructions and require review or trust policy.
- Keep media retrieval disabled by default until provider and platform policies are defined.
