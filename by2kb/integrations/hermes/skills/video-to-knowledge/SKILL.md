---
name: video-to-knowledge
description: Use by2kb to turn a Bilibili URL or a downloaded local audio/video attachment into a transcript, a short abstract, and long-form study notes.
---

# Video to knowledge

The installed Hermes plugin handles a bare Bilibili URL automatically. For a media
attachment, first save the attachment to a private temporary path, then pass that
exact path as the source argument. The workflow runs the deterministic transcription
stage first, then uses the Hermes host model for
two bounded enrichment calls. Do not clone or inspect the by2kb repository.

For an explicit/manual request:

1. Check installation with `by2kb version`. If missing, ask the user to install
   with `pipx install by2kb[asr-doubao]`.
2. If configuration is missing, run `by2kb init` and let the user provide TOS and
   Doubao ASR credentials. Select `agent` for enrichment.
3. Run `by2kb ingest <URL_OR_LOCAL_PATH> --enricher external_agent --json`.
4. Run `by2kb enrichment claim <JOB_ID> --json`.
5. Follow each returned system and user prompt exactly. Save the short response
   and study response as UTF-8 Markdown files.
6. Run `by2kb enrichment complete <JOB_ID> --abstract-file <PATH> --study-file
   <PATH> --provider <HOST_PROVIDER> --model <HOST_MODEL> --json`.
7. Report the three knowledge-base paths: raw transcript, short abstract, and
   long-form study notes.

Never place model credentials in by2kb when using this path. If enrichment fails,
run `by2kb enrichment fail <JOB_ID> --message <ERROR> --retryable` so it can be
resumed without retranscribing.
