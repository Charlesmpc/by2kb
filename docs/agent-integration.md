# Agent-hosted enrichment and Hermes integration

## Status

This document records the implemented agent-hosted workflow and its extension boundary.

Implemented in `by2kb` today:

- the local CLI and Bilibili audio + ASR pipeline;
- API-key-authenticated OpenAI-compatible enrichment;
- independent short-abstract and deep-study outputs;
- `--re-enrich`, which regenerates both outputs from stored transcript artifacts by
  calling the configured LLM API directly;
- packaged default enrichment profiles;
- the durable `external_agent` executor and `claim|complete|fail` protocol;
- a bundled Hermes plugin with an authorized-user URL hook and host-owned LLM calls;
- a bundled Hermes Skill for natural-language/manual invocation;
- `by2kb init` and `by2kb agent install hermes`.

Future host adapters, including a Codex-specific plugin, can reuse the same CLI
protocol. MCP is optional and is not used by the Hermes implementation.

## Two deployment paths, one core pipeline

`by2kb` has one deterministic capture pipeline and a polymorphic enrichment boundary:

```text
video URL
   │
   ▼
resolve → audio → ASR → normalize → raw artifacts
                                      │
                                      ▼
                              EnrichmentRequest
                                │           │
                         executor=api   executor=external_agent
                                │           │
                         by2kb calls    persist pending work;
                         configured     an agent host claims it
                         LLM API        and submits the result
                                │           │
                                └─────┬─────┘
                                      ▼
                       validate → publish → complete
```

Capture, artifact validation, publishing, hashing, and job state remain in `by2kb`.
Only the component that turns an `EnrichmentRequest` into two Markdown bodies is
polymorphic.

Suggested configuration:

```toml
[enrichment]
executor = "api"  # api | external_agent | disabled
abstract_profile = "short-video-abstract"
study_profile = "default-video-digest"
```

- `api` is the native unattended deployment. `by2kb` uses
  `BY2KB_LLM_API_KEY`, `BY2KB_LLM_MODEL`, and `BY2KB_LLM_BASE_URL` and finishes the
  entire job itself.
- `external_agent` publishes raw artifacts and leaves durable pending enrichment work
  for an agent-host adapter. `by2kb` never calls the agent.
- `disabled` produces raw artifacts only.

## Core interface

The polymorphism belongs above `LlmClient`, because an external agent is asynchronous
and may finish in a later process or conversation:

```python
@dataclass(frozen=True)
class EnrichmentRequest:
    job_id: str
    raw_path: Path
    transcript_path: Path
    abstract_profile: str
    study_profile: str


@dataclass(frozen=True)
class CompletedEnrichment:
    abstract_path: Path
    study_path: Path


@dataclass(frozen=True)
class DeferredEnrichment:
    task_id: str


class EnrichmentExecutor(Protocol):
    async def submit(
        self, request: EnrichmentRequest
    ) -> CompletedEnrichment | DeferredEnrichment: ...
```

`ApiEnrichmentExecutor` completes inline. `ExternalAgentExecutor` persists the request
and returns `DeferredEnrichment`; it does not open Hermes, Codex, Telegram, or another
agent process.

Target CLI contract:

```text
by2kb ingest <url> --enricher external_agent --json
by2kb enrichment claim <job-id> --json
by2kb enrichment complete <job-id> \
  --abstract-file <short-output.md> \
  --study-file <long-output.md>
by2kb enrichment fail <job-id> --retryable --message <message>
```

`complete` is the single trusted publication path. It verifies that the job is waiting
for enrichment, validates both files, adds provenance, publishes them through the
configured sink, records hashes, and marks the job complete.

## CLI first; MCP optional

When the agent and `by2kb` run on the same host, subprocess + files are the smallest
useful interface. An agent-facing MCP server would duplicate capabilities already
available through the terminal and filesystem.

Use MCP later only if structured remote access becomes valuable—for example, a shared
`by2kb` service consumed by several agent products across machines. The core external
enrichment contract should remain transport-neutral so an HTTP or MCP adapter can be
added without changing the job pipeline.

## Hermes adapter

Hermes is the first reference agent host. It should consume `by2kb` as an installed
application, not learn from a checkout placed in its working directory.

The Hermes plugin is packaged inside the `by2kb` wheel and installs as:

```text
~/.hermes/plugins/by2kb/
  plugin.yaml
  __init__.py
  skills/
    video-to-knowledge/
      SKILL.md
```

It registers no MCP server and does not need to add a permanent model tool. It uses
existing Hermes extension surfaces:

1. `pre_gateway_dispatch` deterministically recognizes a bare supported video URL
   before the normal agent turn.
2. It checks Hermes authorization, acknowledges receipt through the active platform
   adapter, starts a background CLI subprocess, and returns
   `{"action": "skip"}` so the URL does not also enter the model.
3. The subprocess invokes `by2kb ingest <url> --enricher external_agent --json` using an
   argument vector with `shell=False`.
4. Once transcription is ready, the plugin performs enrichment using one of the two
   host strategies below.
5. It calls `by2kb enrichment complete`, then reports the short abstract and artifact
   paths to the original Telegram conversation.

### Recommended Hermes strategy: host LLM calls

The plugin calls `ctx.llm.complete()` twice, supplying the packaged abstract and study
profiles plus the transcript. This borrows the model and authentication already
configured in Hermes, while keeping execution deterministic and avoiding a nested
agent tool loop. No `BY2KB_LLM_API_KEY` is needed.

This mode uses the Hermes model but not the full conversation memory or arbitrary agent
tools. It is the default because summary generation is a bounded transformation.

### Optional Hermes strategy: full agent turn

When personal memory, additional skills, web research, or other agent tools are
required, the plugin can register a namespaced skill and call `ctx.inject_message()`
for the retained gateway session. The injected message tells the agent to load the
plugin skill, read the prepared artifacts, create both summaries, and call
`by2kb enrichment complete`.

This requires the plugin's `allow_gateway_injection` permission and costs a complete
agent turn. It is more expressive but less deterministic than direct host LLM calls.

There is no URL loop: Hermes skips `pre_gateway_dispatch` for internal injected events,
and the plugin also records `(platform, video_id, job_id, phase)` for idempotency.

## Hermes user journey

```text
User sends a Bilibili URL to the Telegram Hermes bot
  → plugin replies “accepted; transcribing”
  → plugin starts the installed by2kb CLI in the background
  → by2kb creates source.json, transcript.json, and raw.<title>.md
  → plugin uses the Hermes host LLM (default) or injects a full agent task
  → short and long Markdown bodies are submitted to by2kb
  → by2kb validates, publishes, and completes the job
  → Telegram receives the short abstract and durable artifact paths
```

For phrased requests rather than a bare URL, the bundled Hermes skill can invoke the
same CLI workflow through the terminal. The plugin hook is what guarantees the bare-URL
path; the skill supplies judgment and instructions when natural language is involved.

## Codex and other hosts

Codex can use the same CLI protocol through a small plugin containing an agent skill;
MCP is still optional. Unlike a messaging gateway plugin, a Codex plugin should not be
assumed to own a deterministic pre-dispatch hook for every user message. The shared
contract is `ingest/claim/complete`, not a host-specific callback.

Every new host adapter remains thin:

```text
by2kb core
  ├─ ApiEnrichmentExecutor
  └─ external enrichment protocol
       ├─ Hermes plugin
       ├─ Codex plugin/skill
       └─ future agent hosts
```

## Packaging and installation target

The `by2kb` Python package owns the CLI and the version-matched Hermes adapter. Users
install the application independently of any agent checkout, then copy and enable the
adapter with one command.

Target installation experience:

```bash
pipx install 'by2kb[asr-doubao]'
by2kb init
by2kb agent install hermes
```

Until packages are published, a local source installation can use:

```bash
pipx install '/absolute/path/to/by2kb[asr-doubao]'
```

Before the first package-index release, use the local source path in the `pipx install`
command. `by2kb agent install hermes` is implemented now; it copies the plugin into the
active `HERMES_HOME`, enables it through the Hermes CLI, and asks the user to restart
the gateway.
