---
name: video-to-knowledge
description: Use by2kb to turn a Bilibili/YouTube URL or a downloaded local audio/video attachment into a transcript, a short abstract, and long-form study notes.
---

# Video to knowledge

The installed Hermes plugin handles a bare Bilibili or YouTube URL automatically. For a media
attachment, first save the attachment to a private temporary path, then pass that
exact path as the source argument. The workflow runs the deterministic transcription
stage first, then uses the Hermes host model for
bounded, program-planned enrichment operations. Do not clone or inspect the by2kb repository.

For an explicit/manual request:

1. Check installation with `by2kb version` (0.5.3+). If missing or setup is incomplete,
   load the bundled `skill_view("by2kb:install-by2kb")`. The default installation is
   `pipx install by2kb[asr-whisper,youtube]` with
   `by2kb init --preset agent-local`.
2. Use cloud Doubao ASR only when the user explicitly selects it. Never ask the user
   to send TOS or ASR credentials through an IM conversation.
3. Run `by2kb ingest <URL_OR_LOCAL_PATH> --enricher external_agent --json`.
4. Run `by2kb enrichment next <JOB_ID> --provider <HOST_PROVIDER> --model
   <HOST_MODEL> --json`.
5. With by2kb 0.5.3+, `needs_input` returns a small operation ticket, not prompt text.
   Read both `system_prompt` and `user_prompt` using `by2kb enrichment read
   --request-file <REQUEST_FILE> --field <FIELD> --offset 0 --limit 1000 --json`.
   Continue using `next_offset` until `eof` for each field. Check every page's
   `operation_id` against the ticket. Follow the complete prompts, save the bounded
   response as UTF-8 (title operations request JSON, others Markdown), and run `by2kb enrichment submit
   <JOB_ID> --operation-id <ID> --output-file <PATH> --provider <HOST_PROVIDER>
   --model <HOST_MODEL> --json`.
6. Repeat steps 4–5 until `next` returns status `completed`.
7. Report the three knowledge-base paths: raw transcript, short abstract, and
   long-form study notes.

Never print an entire request file or transcript into terminal output. Do not chain
`claim` and `next`; staged callers do not need `claim`. The program plans chunks and
reductions; do not invent operation IDs or summarize unseen/truncated input. If a
page is truncated, reread that offset with a smaller limit before continuing.

Inspect `submit.status`, not just its exit code. On `operation_mismatch`, query
status and next, discard the old output, and generate for the new operation; stop
after three resynchronizations. Never merely replace an ID on an old answer.
On conflicting content or other errors, check job status and report the step issue
without marking the shared job failed. Completed jobs should be reported as success.
Never place model credentials in by2kb when using this path. Preserve checkpoints
and user configuration. Older CLI inline prompts are compatibility-only; upgrade
if its tool output is truncated.
