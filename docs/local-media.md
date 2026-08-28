# Local audio and video

`by2kb ingest` accepts a Bilibili URL or a path to a local media file. Local files
run through the same ASR, normalization, raw publication, and optional enrichment
pipeline as remote media.

```bash
by2kb ingest ./meeting.mp3
by2kb ingest ./lecture.mp4 --enricher external_agent
```

Supported audio extensions are AAC, FLAC, M4A, MP3, OGA, OGG, Opus, WAV, and WMA.
Supported video extensions are AVI, M4V, MKV, MOV, MP4, MPEG/MPG, and WebM.

## Runtime requirement

`ffprobe` inspects duration for every local input. Video inputs additionally use
`ffmpeg` to extract the first audio stream as mono 16 kHz WAV. Install ffmpeg so both
executables are available on `PATH`. The `by2kb doctor` command in the setup phase
checks this dependency.

## Identity and privacy

The durable identity is the full SHA-256 digest of file contents. Submitting the
same bytes under a different filename therefore reuses the existing job. The source
artifact records the original filename, media kind, extension, byte size, digest,
and duration. It does not record the absolute source path.

Local source URLs in normalized artifacts use the non-resolving form
`local://sha256/<digest>`; they never expose a filesystem location.

## Agent attachments

An Agent host such as Hermes should download an attachment to a private, scoped
temporary directory, invoke `by2kb ingest <exact-path>`, and remove its temporary
copy after the command finishes. When external-agent enrichment is configured, the
normal `claim → complete/fail` protocol begins after the raw transcript is published.

Folder watching, a desktop upload UI, and permanent attachment storage are outside
this feature.
