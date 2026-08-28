# Transcript quality gate

`by2kb` assesses every normalized transcript before enrichment. The assessment is
deterministic and does not call an LLM.

Version `1.0` records:

- effective alphanumeric/CJK character count and duration-adjusted minimums;
- characters per minute;
- four-character repetition ratio;
- timed segment coverage and the largest uncovered gap when real segment timing is
  available;
- status (`pass`, `warning`, or `fail`) and machine-readable reason codes.

The complete assessment, including thresholds and metrics, is stored as
`transcript.quality` in `transcript.json` and as `transcript_quality` in `source.json`.
Thresholds scale with media duration and transcript kind (`human`, `auto_caption`, or
`asr`) rather than applying one absolute character count.

## Behavior

- `pass`: enrichment proceeds normally.
- `warning`: enrichment proceeds, but the assessment is included in the LLM/Agent
  prompt and a warning block is inserted by by2kb into the raw transcript, short
  abstract, and long study notes. This insertion does not depend on model compliance.
- `fail`: raw source/transcript/Markdown artifacts are published for inspection, the
  job becomes `failed_terminal`, and no enrichment task or LLM call is created.

Reason codes currently include `empty_transcript`, `suspiciously_short`,
`short_for_duration`, `low_characters_per_minute`, `highly_repetitive`, `repetitive`,
`very_low_timed_coverage`, `low_timed_coverage`, and `large_segment_gap`.

For a failed assessment, inspect the raw transcript and retry with another ASR
provider or corrected media. `--re-enrich` applies the gate to older stored
transcripts that predate the assessment field.
