---
name: video-to-knowledge
description: Use by2kb to turn a Bilibili/YouTube URL or a downloaded local audio/video attachment into a transcript, a short abstract, and long-form study notes.
---

# Video to knowledge

The installed Hermes plugin handles a bare Bilibili or YouTube URL automatically. For a media
attachment, first save the attachment to a private temporary path, then pass that
exact path as the source argument. The workflow runs the deterministic transcription
stage first, then uses the Hermes host model for
two bounded enrichment calls. Do not clone or inspect the by2kb repository.

For an explicit/manual request:

1. Check installation with `by2kb version`. If missing, use the packaged
   `install-by2kb` Skill. The default installation is
   `pipx install by2kb[asr-whisper,youtube]` with
   `by2kb init --preset agent-local`.
2. Use cloud Doubao ASR only when the user explicitly selects it. Never ask the user
   to send TOS or ASR credentials through an IM conversation.
3. Run `by2kb ingest <URL_OR_LOCAL_PATH> --enricher external_agent --json`.
4. Run `by2kb enrichment next <JOB_ID> --provider <HOST_PROVIDER> --model
   <HOST_MODEL> --json`.
5. When status is `needs_input`, follow the returned system and user prompt exactly,
   save the bounded response as UTF-8 Markdown, and run `by2kb enrichment submit
   <JOB_ID> --operation-id <ID> --output-file <PATH> --provider <HOST_PROVIDER>
   --model <HOST_MODEL> --json`.
6. Repeat steps 4–5 until `next` returns status `completed`.
7. Report the three knowledge-base paths: raw transcript, short abstract, and
   long-form study notes.

Never place model credentials in by2kb when using this path. If enrichment fails,
run `by2kb enrichment fail <JOB_ID> --message <ERROR> --retryable` so it can be
resumed without retranscribing.
