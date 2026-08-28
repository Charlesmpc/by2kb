# Initialization and diagnostics

`by2kb init` interactively selects local faster-whisper or cloud Doubao ASR, the
knowledge-library destination, and Agent/API/raw-only enrichment. It writes
`config.toml` plus a local `.env`; secret prompts use hidden input and secrets are
never copied into diagnostic output or knowledge artifacts.

`by2kb doctor` is the read-only counterpart:

```bash
by2kb doctor
by2kb doctor --provider faster_whisper
by2kb doctor --json
```

It does not create directories, update configuration, or download model weights. It
checks ffmpeg/ffprobe execution, library/home/database permissions, the selected ASR
dependency and configuration, local model presence or private TOS access, and the
configured enrichment path. External-Agent mode also checks that the by2kb command
is callable and the complete Hermes plugin contract is installed.

## JSON schema

The schema is versioned for Agent hosts. Version 1 has this shape:

```json
{
  "schema_version": 1,
  "ok": false,
  "provider": "faster_whisper",
  "enrichment_executor": "external_agent",
  "checks": [
    {
      "id": "asr_model",
      "ok": false,
      "message": "faster-whisper model large-v3-turbo is missing",
      "remediation": "Run: by2kb models install large-v3-turbo"
    }
  ]
}
```

Every check contains exactly `id`, `ok`, `message`, and nullable `remediation`.
Overall `ok` is true only when every check passes. Human output and JSON mode both
exit with status 1 when any required check fails.

Doubao diagnostics make a read-only `HeadBucket` request with short timeouts. They do
not print provider exception bodies, credential values, or signed requests.
