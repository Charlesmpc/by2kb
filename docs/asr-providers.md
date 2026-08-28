# ASR providers

`by2kb` keeps ASR behind a provider registry. The core package stays small; each
runtime is installed and configured only by deployments that use it.

## Local faster-whisper

Install the optional runtime into the same pipx environment:

```bash
pipx inject by2kb "faster-whisper>=1.2.1,<2"
by2kb models status
by2kb models install large-v3-turbo
```

Model downloads are explicit. Ingestion opens only a previously installed local
model and will return a remediation command instead of downloading weights silently.

Configure `~/.by2kb/config.toml`:

```toml
[asr]
provider = "faster_whisper"
model = "large-v3-turbo"
device = "auto"          # auto, cpu, or cuda
compute_type = "default" # e.g. int8 on CPU or float16 on CUDA
vad_filter = true
beam_size = 5
cpu_threads = 0
```

The same settings can be overridden with `BY2KB_WHISPER_MODEL`,
`BY2KB_WHISPER_DEVICE`, `BY2KB_WHISPER_COMPUTE_TYPE`,
`BY2KB_WHISPER_MODEL_DIR`, `BY2KB_WHISPER_VAD_FILTER`,
`BY2KB_WHISPER_BEAM_SIZE`, and `BY2KB_WHISPER_CPU_THREADS`.

`large-v3-turbo` is the speed-oriented default. Select `large-v3` when accuracy is
more important and the deployment has enough compute and memory. Both produce
timestamped segments in the normalized transcript.

## Cloud Doubao AUC

The existing `doubao_auc` provider stages audio temporarily in a private TOS bucket
and calls the hosted asynchronous ASR API. Install it with:

```bash
pipx inject by2kb "boto3>=1.34"
```

See [Doubao AUC with private TOS staging](reference/doubao-auc-tos-asr.md) for its
credential and lifecycle contract.

## Automatic selection

`provider = "auto"` tries ready providers in priority order. Local faster-whisper is
preferred when both its dependency and selected model are installed; otherwise the
registry can fall back to a fully configured Doubao provider. If no candidate is
ready, the error lists each rejected provider and its remediation.
