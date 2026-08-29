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
- the durable `external_agent` executor, staged `next|submit` enrichment protocol,
  and versioned task controls;
- a bundled Hermes plugin with an authorized-user URL hook and host-owned LLM calls;
- a bundled Hermes Skill for natural-language/manual invocation;
- guided `by2kb init`, read-only `by2kb doctor`, and `by2kb agent install hermes`.

Future host adapters, including a Codex-specific plugin, can reuse the same CLI
protocol. MCP is optional and is not used by the Hermes implementation.

## Two deployment paths, one core pipeline

`by2kb` has one deterministic capture pipeline and a polymorphic enrichment boundary:

```text
video URL
   │
   ▼
resolve URL/path → audio → ASR → normalize → raw artifacts
                                      │
                                      ▼
                              EnrichmentRequest
                                │           │
                         executor=api   executor=external_agent
                                │           │
                         by2kb calls    persist pending work;
                         configured     an agent host executes
                         LLM API        bounded operations
                                │           │
                                └─────┬─────┘
                                      ▼
                       validate → publish → complete
```

Capture, artifact validation, publishing, hashing, and job state remain in `by2kb`.
Only the component that turns an `EnrichmentRequest` into two Markdown bodies is
polymorphic. Direct API and hosting-Agent modes now share the same long-form planner,
cache, reducer, final Skills, and artifact writer.

Before an enrichment request is created, by2kb records a deterministic transcript
quality assessment. Failed assessments preserve raw artifacts but cannot be claimed;
warnings are included in Agent prompts and forced into both generated outputs.

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

Recommended CLI contract:

```text
by2kb ingest <url> --enricher external_agent --json
by2kb enrichment next <job-id> \
  --provider <host> --model <model> --runtime-version <version> --json
by2kb enrichment submit <job-id> \
  --operation-id <operation-id> --output-file <response.md> \
  --provider <host> --model <model> --runtime-version <version> --json
by2kb enrichment fail <job-id> --retryable --message <message>
```

The host repeats `next` and `submit` until `next` returns `completed`. Each operation
contains a bounded prompt and input, while by2kb retains planning, caching, validation,
provenance, and publication. The earlier `claim`/`complete` commands remain available
for compatibility, but bypass staged long-form planning and are not recommended for
new adapters.

Use `by2kb status|wait|cancel|retry` for lifecycle control. These commands return one
versioned JSON envelope and are documented in [Agent task control](task-control.md).

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
4. Once transcription is ready, the plugin checks the task snapshot, requests one
   bounded operation with `enrichment next`, calls the host model, and returns its
   response with `enrichment submit`.
5. It repeats until by2kb validates, publishes, and completes both outputs, then
   reports the short abstract and artifact paths to the original conversation.

### Recommended Hermes strategy: host LLM calls

The plugin calls `ctx.llm.complete()` for each bounded operation supplied by by2kb.
This borrows the model and authentication already configured in Hermes, while keeping
execution deterministic and avoiding a nested agent tool loop. Chunking, recursive
reduction, and final abstract/study Skills remain controlled and cached by by2kb. No
`BY2KB_LLM_API_KEY` is needed.

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
contract is `ingest`, staged `next/submit`, and versioned task control, not a
host-specific callback.

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

Install the published package and configure the agent integration:

```bash
pipx install 'by2kb[asr-whisper,youtube]'
by2kb init --preset agent-local
by2kb models install
by2kb agent install hermes
```

This is the default, cloud-free path. Deployments that prefer hosted ASR can install
`by2kb[asr-doubao,youtube]` and use the interactive `by2kb init` flow to configure a
private TOS staging bucket and Doubao credentials.

For repository development, a local source installation can use:

```bash
pipx install '/absolute/path/to/by2kb[asr-whisper,youtube]'
```

`by2kb agent install hermes` copies the plugin into the active `HERMES_HOME`, enables it
through the Hermes CLI, and asks the user to restart the gateway.

The installed plugin directory is program-owned and may be replaced during upgrades.
A personalized Hermes runtime Skill belongs at
`$BY2KB_HOME/skills/video-to-knowledge/SKILL.md` (or the path named by
`BY2KB_HERMES_SKILL`); it takes precedence over the packaged copy and remains outside
the replacement boundary. See [Upgrading](upgrading.md) for the complete state contract.
