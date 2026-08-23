# Doubao AUC ASR through private Volcengine TOS staging

This document describes the Doubao AUC provider used by the application and the
standalone reference adapter in
[`examples/doubao_auc_tos_asr.py`](../../examples/doubao_auc_tos_asr.py).
Both follow the same private-TOS staging and asynchronous transcription contract.

## Why TOS staging is needed

Doubao's asynchronous file-transcription API (AUC) consumes an audio URL rather than a
local file. A deployment should not make its object bucket public just to satisfy that
contract. The adapter therefore uses a private Volcengine TOS bucket and creates a
short-lived, signed GET URL for one temporary object.

```text
local audio
    │
    ├─ validate size and format
    ├─ split long input into ordered 60-second Opus chunks
    ▼
private TOS object
    │
    ├─ 10-minute presigned GET URL
    ▼
Doubao AUC submit
    │
    ├─ poll by stable request ID
    ▼
transcript text
    │
    └─ delete temporary TOS object in finally
```

The object stays private, the URL expires after 600 seconds, and cleanup runs on both
success and failure. Long inputs use at most two concurrent AUC jobs and are reassembled
in source order.

## Provider behavior

### Input handling

- Accepted extensions: `.ogg`, `.oga`, `.opus`, `.mp3`, `.wav`, `.m4a`, `.mp4`, `.flac`.
- The reference adapter rejects empty files and files larger than 25 MiB.
- Ogg/Opus input explicitly sends `format=ogg` and `codec=opus`.
- `ffprobe` determines duration when available.
- Inputs longer than 75 seconds are converted into ordered 60-second Ogg/Opus chunks
  with ffmpeg (`libopus`, 32 kbit/s).
- Two chunks may be transcribed concurrently; output remains ordered by chunk index.
- Retryable provider and transport failures are retried per chunk up to three times
  with exponential backoff and jitter. Terminal failures are not retried.
- Successful chunk transcripts are written atomically under a source- and
  configuration-addressed `.asr-checkpoints/` directory beside the downloaded audio.
  A resumed ingestion loads those checkpoints instead of resubmitting completed chunks.

The 25 MiB guard applies to each submitted file, not the original long source before
chunking. Chunking is also a latency/reliability control: one slow long-file job cannot
hold the whole ingestion worker indefinitely.

### Private TOS staging

The adapter uses TOS's S3-compatible endpoint through `boto3` with:

- Signature V4;
- virtual-hosted addressing;
- a unique date-prefixed object key;
- the source's audio MIME type;
- a 600-second presigned `get_object` URL.

No bucket-level public-read policy is required. The presigned URL must be treated as a
secret while it is valid and must never be written to normal logs or durable job
metadata.

### Doubao AUC request

Endpoints used by the tested flow:

```text
POST https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit
POST https://openspeech.bytedance.com/api/v3/auc/bigmodel/query
```

Important request headers:

```text
X-Api-Key:          <new-console API key>
X-Api-Resource-Id:  volc.seedasr.auc   # configurable; verify for the account/product
X-Api-Request-Id:   <one UUID reused for submit and query>
X-Api-Sequence:     -1                 # submit only
```

Legacy-console credentials remain supported as a fallback through
`X-Api-App-Key` plus `X-Api-Access-Key`, but `DOUBAO_API_KEY` takes precedence.

The JSON request enables inverse text normalization and punctuation:

```json
{
  "user": {"uid": "by2kb"},
  "audio": {
    "format": "ogg",
    "codec": "opus",
    "url": "<short-lived presigned URL>"
  },
  "request": {
    "model_name": "bigmodel",
    "enable_itn": true,
    "enable_punc": true
  }
}
```

The adapter reads the business status from response headers, not only the HTTP status:

| `X-Api-Status-Code` | Meaning in this adapter | Action |
| --- | --- | --- |
| `20000000` | success | read `result.text` |
| `20000001` | processing | continue polling |
| `20000002` | queued | continue polling |
| `20000003` | silent/empty audio | return an empty transcript |
| `45000131` | submission-rate limit | retry the affected chunk |
| `550xxxx` | internal error / overload | retry the affected chunk |
| other `450xxxx` | invalid input/request | fail without retrying |

HTTP 408, 429, and 5xx responses are retryable even when the provider status header is
missing. Other non-2xx responses are terminal unless explicitly classified above.

Polling runs every 1.5 seconds until the configured deadline. Each HTTP request has its
own 30-second timeout. A by2kb provider should map transport errors, provider errors,
timeouts, and empty-audio results into distinct job error/status categories.

## Configuration

Install the reference script's dependencies:

```bash
python -m pip install boto3 requests
# ffmpeg/ffprobe are required for reliable duration detection and long-input chunking
```

Supply secrets through process environment variables or an optional private
`KEY=VALUE` file. Environment variables take precedence.

```text
VOLC_ACCESS_KEY_ID=...
VOLC_SECRET_ACCESS_KEY=...
TOS_REGION=ap-southeast-1
TOS_BUCKET=...
TOS_S3_ENDPOINT=tos-s3-ap-southeast-1.volces.com
DOUBAO_API_KEY=...
# Legacy-console fallback only:
DOUBAO_APPID=...
DOUBAO_ACCESS_TOKEN=...
DOUBAO_RESOURCE_ID=volc.seedasr.auc
```

Do not commit the environment file. The TOS AK/SK and Doubao credentials are separate
credential domains even when they belong to the same Volcengine account.

Run it as:

```bash
python examples/doubao_auc_tos_asr.py input.ogg \
  --env-file /run/secrets/by2kb-volc.env \
  --timeout 150 \
  --output transcript.txt
```

The transcript is always written to stdout; `--output` optionally writes the same text
to a file. Diagnostics go to stderr and the process exits non-zero on failure.

## Security and production notes

1. Use a dedicated private bucket or tightly scoped prefix and least-privilege TOS
   credentials (`PutObject`, `GetObject` for signing, and `DeleteObject`).
2. Never log credentials, request headers, or presigned URLs.
3. Keep a lifecycle rule on the temporary prefix as a second cleanup layer in case the
   process is killed before `finally` executes.
4. A cleanup failure is warned about without discarding a successfully produced
   transcript. Production code should also emit a metric and enqueue cleanup retry.
5. Verify `DOUBAO_RESOURCE_ID` against the currently purchased speech product. Endpoint
   names such as `bigmodel` do not prove that the configured resource ID is correct.
6. The reference concatenates chunk text. A production by2kb adapter should retain
   per-chunk timing/provenance and normalize it into the common timestamped transcript
   schema rather than returning only plain text.
7. Pin dependencies and add bounded retries with jitter for transient TOS and HTTP
   failures before promoting the adapter from reference code to a production provider.

## Suggested by2kb integration boundary

The future ASR provider should accept a normalized local audio artifact and return a
provider-neutral result, for example:

```json
{
  "provider": "doubao_auc",
  "model": "bigmodel",
  "language": null,
  "text": "...",
  "segments": [],
  "provenance": {
    "media_source": "audio_fallback",
    "staging": "private_tos_presigned_url"
  }
}
```

Temporary object keys and presigned URLs are operational details and should not appear
in the durable transcript artifact.
