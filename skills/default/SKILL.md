---
name: default-video-digest
description: Clean a transcript and create a grounded video digest.
version: 0.1.0
---

# Default video digest

## Use when

Creating the updated Markdown artifact from a normalized video transcript.

## Rules

1. Preserve the raw transcript separately; never overwrite it.
2. Do not invent claims, speakers, chapters, or timestamps.
3. Clean punctuation and obvious transcription artifacts without changing meaning.
4. Every chapter and quoted key point must link to the nearest source timestamp.
5. Distinguish what the speaker said from your own synthesis.
6. If the transcript is incomplete or low quality, state that prominently.

## Output

Produce these sections:

1. `Summary` — a concise description of the video's purpose and conclusion.
2. `Chapters` — timestamped sections grounded in transcript boundaries.
3. `Key points` — important claims or lessons, each with a source timestamp.
4. `Questions` — unresolved questions, assumptions, or points worth checking.
5. `Transcript` — a readable, lightly cleaned transcript preserving timestamps.
