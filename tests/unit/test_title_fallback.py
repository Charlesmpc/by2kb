import json
from pathlib import Path

import pytest
from test_attempt_count import pipeline

from by2kb.normalize import SourceMeta


@pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
def test_legacy_title_source_defaults_original(value):
    data = dict(
        platform="bilibili",
        video_id="BV1xx411c7mD",
        canonical_url="test:",
        title="Readable title",
        author="",
    )
    assert SourceMeta(**data).title_source == "original"
    assert SourceMeta(**data, title_source=value).title_source == "original"


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["", "BV1xx411c7mD", "Untitled"])
async def test_api_generates_title_before_summaries_and_rewrites_artifacts(
    tmp_path, title
):
    from by2kb.config import Config
    from by2kb.enrichment import create_enrichment_request, ApiEnrichmentExecutor
    from by2kb.normalize import NormalizedTranscript, TranscriptMeta, Segment
    from by2kb.writers.raw import write_artifacts

    normalized = NormalizedTranscript(
        source=SourceMeta(
            platform="bilibili",
            video_id="BV1xx411c7mD",
            canonical_url="test:",
            title=title,
            author="",
        ),
        transcript=TranscriptMeta(
            provider="fixture",
            kind="human",
            fetched_at="2026-09-12",
            segments=[
                Segment(
                    start_ms=0,
                    duration_ms=60000,
                    text="Database indexes accelerate queries by avoiding full table scans. Indexes have storage and write costs.",
                )
            ],
        ),
    )
    artifacts = write_artifacts(
        tmp_path / "raw",
        source_payload={"source": normalized.source.model_dump(), "fixture": True},
        normalized=normalized,
    )
    original_bytes = artifacts["raw_md"].read_bytes()
    config = Config(
        home=tmp_path / "home",
        library_root=tmp_path / "library",
        db_path=tmp_path / "jobs.db",
    )
    request = create_enrichment_request(
        config,
        job_id="test",
        normalized=normalized,
        raw_path=artifacts["raw_md"],
        staging=tmp_path / "staging",
    )

    class Client:
        provider = model = runtime_version = "fixture"
        calls = []

        async def complete(self, system, user):
            self.calls.append((system, user))
            if len(self.calls) == 1:
                return json.dumps(
                    {
                        "title": "Database Index Tradeoffs",
                        "evidence": "Indexes have storage and write costs.",
                    }
                )
            assert "Database Index Tradeoffs" in user
            assert "title_source: generated" in user
            return "# Database Index Tradeoffs\n\nGrounded notes."

    client = Client()
    result = await ApiEnrichmentExecutor(client).submit(request)
    assert len(client.calls) == 3
    assert "title" in client.calls[0][0].lower()
    for kind in ("raw_md", "abstract_md", "updated_md"):
        path = result.artifacts[kind]
        assert "Database Index Tradeoffs" in path.name
        assert "title_source: generated" in path.read_text(encoding="utf-8")
        assert "# Database Index Tradeoffs" in path.read_text(encoding="utf-8")
    source = json.loads(result.artifacts["source_json"].read_text(encoding="utf-8"))
    assert source["source"]["title"] == "Database Index Tradeoffs"
    assert source["source"]["title_source"] == source["title_source"] == "generated"
    assert source["fixture"] is True
    assert (
        json.loads(result.artifacts["transcript_json"].read_text(encoding="utf-8"))["source"]["title"]
        == "Database Index Tradeoffs"
    )
    assert artifacts["raw_md"].read_bytes() == original_bytes


@pytest.mark.asyncio
async def test_live_agent_title_operation_validates_before_accept_and_finishes(
    tmp_path,
):
    from test_agent_provider import _external_job
    from by2kb.jobs.store import JobStore
    from by2kb.jobs.enrichment_service import (
        claim_external_enrichment,
        next_external_enrichment_operation,
        submit_external_enrichment_operation,
    )
    from by2kb.errors import ConfigError

    config, job_id = _external_job(tmp_path, long=False)
    store = JobStore(config.db_path)
    paths = {a["kind"]: Path(a["path"]) for a in store.artifacts(job_id)}
    store.close()
    data = json.loads(paths["transcript_json"].read_text(encoding="utf-8"))
    data["source"]["title"] = "BV1xx411c7mD"
    paths["transcript_json"].write_text(json.dumps(data))
    before = {k: p.read_bytes() for k, p in paths.items()}
    manifest = claim_external_enrichment(config, job_id)
    assert manifest["title_required"] is True
    assert manifest["outputs"] == {}  # no stale summary prompts
    kwargs = dict(provider="fixture", model="fixture")
    step = await next_external_enrichment_operation(config, job_id, **kwargs)
    assert "concise readable video title" in step["operation"]["system_prompt"]
    output = tmp_path / "output.json"
    output.write_text('{"title":"Invented", "evidence":"not in transcript"}')
    with pytest.raises(ConfigError, match="invalid generated title"):
        submit_external_enrichment_operation(
            config,
            job_id,
            operation_id=step["operation"]["id"],
            output_path=output,
            **kwargs,
        )
    evidence = data["transcript"]["segments"][0]["text"]
    output.write_text(
        json.dumps({"title": "Grounded Fixture Title", "evidence": evidence})
    )
    submit_external_enrichment_operation(
        config,
        job_id,
        operation_id=step["operation"]["id"],
        output_path=output,
        **kwargs,
    )
    for _ in range(3):
        step = await next_external_enrichment_operation(config, job_id, **kwargs)
        if step["status"] == "completed":
            break
        assert "Grounded Fixture Title" in step["operation"]["user_prompt"]
        output.write_text("# Grounded Fixture Title\n\nNotes")
        submit_external_enrichment_operation(
            config,
            job_id,
            operation_id=step["operation"]["id"],
            output_path=output,
            **kwargs,
        )
    assert step["status"] == "completed"
    for kind in ("raw_md", "abstract_md", "updated_md"):
        assert "Grounded Fixture Title" in Path(step["artifacts"][kind]).name
    assert {k: p.read_bytes() for k, p in paths.items()} == before


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        "{}",
        "[]",
        '{"title":"BV1xx411c7mD","evidence":"Indexes"}',
        '{"title":"A\\nB", "evidence":"Indexes"}',
        '{"title":"Made up", "evidence":"absent"}',
        '{"title":"Valid topic", "evidence":"Transcript evidence (bounded excerpt):"}',
    ],
)
def test_invalid_title_response_rejected(response):
    from by2kb.titles import parse_title_response
    from by2kb.errors import ConfigError

    with pytest.raises(ConfigError, match="invalid generated title"):
        parse_title_response(
            response, "Transcript evidence (bounded excerpt):\n\nIndexes cost storage."
        )


@pytest.mark.asyncio
async def test_empty_transcript_never_requests_generated_title(tmp_path):
    from test_summaries import normalized_transcript
    from by2kb.config import Config
    from by2kb.enrichment import ApiEnrichmentExecutor, create_enrichment_request
    from by2kb.writers.raw import write_artifacts

    normalized = normalized_transcript()
    normalized.source.title = ""
    normalized.transcript.segments = []
    artifacts = write_artifacts(
        tmp_path / "raw", source_payload={}, normalized=normalized
    )
    request = create_enrichment_request(
        Config(
            home=tmp_path / "home",
            library_root=tmp_path / "lib",
            db_path=tmp_path / "db",
        ),
        job_id="empty",
        normalized=normalized,
        raw_path=artifacts["raw_md"],
        staging=tmp_path / "staging",
    )

    class Client:
        provider = model = "fixture"

        async def complete(self, system, user):
            assert "Generate a concise readable video title" not in system
            return "# Insufficient transcript"

    result = await ApiEnrichmentExecutor(Client()).submit(request)
    assert "raw_md" not in result.artifacts
    assert normalized.source.title_source == "original"


def test_source_payload_provenance_is_preserved(tmp_path):
    from test_summaries import normalized_transcript
    from by2kb.writers.raw import write_artifacts

    normalized = normalized_transcript()
    paths = write_artifacts(
        tmp_path, source_payload={"title_source": "generated"}, normalized=normalized
    )
    assert json.loads(paths["source_json"].read_text(encoding="utf-8"))["title_source"] == "generated"
    assert (
        json.loads(paths["transcript_json"].read_text(encoding="utf-8"))["source"]["title_source"]
        == "generated"
    )
    assert "title_source: generated" in paths["raw_md"].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_legacy_complete_cannot_bypass_missing_title(tmp_path):
    from test_agent_provider import _external_job
    from by2kb.jobs.store import JobStore
    from by2kb.jobs.enrichment_service import complete_external_enrichment
    from by2kb.errors import ConfigError

    config, job_id = _external_job(tmp_path, long=False)
    store = JobStore(config.db_path)
    path = Path(
        next(
            a["path"] for a in store.artifacts(job_id) if a["kind"] == "transcript_json"
        )
    )
    store.close()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["source"]["title"] = ""
    path.write_text(json.dumps(data))
    output = tmp_path / "summary.md"
    output.write_text("# Summary")
    with pytest.raises(ConfigError, match="enrichment next"):
        await complete_external_enrichment(
            config,
            job_id,
            abstract_path=output,
            study_path=output,
            provider="fixture",
            model="fixture",
        )


@pytest.mark.asyncio
async def test_raw_only_duplicate_and_explicit_reenrich(pipeline):
    from by2kb.jobs.enrichment_service import next_external_enrichment_operation

    config, run = pipeline
    first = await run()
    path = Path(first.artifacts["transcript_json"])
    data = json.loads(path.read_text(encoding="utf-8"))
    data["source"]["title"] = "BV1xx411c7mD"
    data["source"].pop("title_source", None)
    path.write_text(json.dumps(data))
    before = {k: Path(p).read_bytes() for k, p in first.artifacts.items()}
    assert (await run()).status == "duplicate"
    assert {k: Path(p).read_bytes() for k, p in first.artifacts.items()} == before
    outcome = await run(re_enrich=True, enricher="external_agent")
    assert outcome.status == "enrichment_pending"
    step = await next_external_enrichment_operation(
        config, first.job_id, provider="fixture", model="fixture"
    )
    assert "concise readable video title" in step["operation"]["system_prompt"]
    assert {k: Path(p).read_bytes() for k, p in first.artifacts.items()} == before


@pytest.mark.asyncio
async def test_legacy_source_provenance_reaches_enrichment_request(tmp_path):
    from test_summaries import normalized_transcript
    from by2kb.config import Config
    from by2kb.enrichment import create_enrichment_request

    raw = tmp_path / "raw.md"
    raw.write_text("# Readable title")
    (tmp_path / "source.json").write_text(json.dumps({"title_source": "generated"}))
    request = create_enrichment_request(
        Config(
            home=tmp_path / "home",
            library_root=tmp_path / "lib",
            db_path=tmp_path / "db",
        ),
        job_id="legacy",
        normalized=normalized_transcript(),
        raw_path=raw,
        staging=tmp_path / "staging",
    )
    assert request.normalized.source.title_source == "generated"


@pytest.mark.parametrize(
    "title",
    [
        "An actual lecture title",
        "无标题的艺术",
        "Video editing basics",
        "BV1xx411c7mD explained",
        "测试视频",
    ],
)
def test_readable_titles_are_preserved(title):
    from by2kb.titles import needs_title

    assert not needs_title(title)


def test_insufficient_evidence_response_does_not_fabricate():
    from by2kb.titles import parse_title_response

    assert parse_title_response('{"title":null,"evidence":""}', "hello") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["invalid_title", "title_network", "summary"])
async def test_api_failure_keeps_published_inputs_unchanged(tmp_path, failure):
    from test_agent_provider import _external_job
    from by2kb.enrichment import ApiEnrichmentExecutor, create_enrichment_request
    from by2kb.errors import ConfigError
    from by2kb.jobs.store import JobStore
    from by2kb.normalize import NormalizedTranscript

    config, job_id = _external_job(tmp_path, long=False)
    store = JobStore(config.db_path)
    paths = {a["kind"]: Path(a["path"]) for a in store.artifacts(job_id)}
    store.close()
    normalized = NormalizedTranscript.model_validate_json(paths["transcript_json"].read_text(encoding="utf-8"))
    normalized.source.title = ""
    before = {kind: path.read_bytes() for kind, path in paths.items()}
    request = create_enrichment_request(
        config, job_id=job_id, normalized=normalized, raw_path=paths["raw_md"],
        staging=tmp_path / "failure-staging",
    )

    class Client:
        provider = model = runtime_version = "fixture"
        calls = 0

        async def complete(self, system, user):
            self.calls += 1
            if self.calls == 1:
                if failure == "invalid_title":
                    return "not JSON"
                if failure == "title_network":
                    raise ConfigError("model unavailable")
                return json.dumps({
                    "title": "Grounded Fixture Title",
                    "evidence": normalized.transcript.segments[0].text,
                })
            raise ConfigError("summary unavailable")

    client = Client()
    with pytest.raises(ConfigError):
        await ApiEnrichmentExecutor(client).submit(request)
    assert client.calls == (2 if failure == "summary" else 1)
    assert {kind: path.read_bytes() for kind, path in paths.items()} == before
    assert normalized.source.title == ""
    assert normalized.source.title_source == "original"


@pytest.mark.asyncio
async def test_completed_reenrich_retires_stale_names_and_preserves_provenance(pipeline, tmp_path):
    from by2kb.jobs.enrichment_service import (
        next_external_enrichment_operation, submit_external_enrichment_operation,
    )

    config, run = pipeline
    first = await run()
    transcript = Path(first.artifacts["transcript_json"])
    data = json.loads(transcript.read_text(encoding="utf-8"))
    data["source"]["title"] = "BV1xx411c7mD"
    transcript.write_text(json.dumps(data))
    old_raw = Path(first.artifacts["raw_md"])
    await run(re_enrich=True, enricher="external_agent")
    output = tmp_path / "operation-output.txt"
    kwargs = dict(provider="fixture", model="fixture")
    for _ in range(4):
        step = await next_external_enrichment_operation(config, first.job_id, **kwargs)
        if step["status"] == "completed":
            break
        operation = step["operation"]
        if "concise readable video title" in operation["system_prompt"]:
            content = json.dumps({
                "title": "Database Transaction Basics",
                "evidence": data["transcript"]["segments"][0]["text"],
            })
        else:
            content = "# Database Transaction Basics\n\nGrounded notes."
        output.write_text(content)
        submit_external_enrichment_operation(
            config, first.job_id, operation_id=operation["id"], output_path=output, **kwargs,
        )
    assert step["status"] == "completed"
    assert not old_raw.exists()
    published = step["artifacts"]
    directory = Path(published["raw_md"]).parent
    assert sorted(p.name for p in directory.glob("*.md")) == sorted(
        Path(published[kind]).name for kind in ("raw_md", "abstract_md", "updated_md")
    )
    for kind in ("source_json", "transcript_json"):
        source = json.loads(Path(published[kind]).read_text(encoding="utf-8"))["source"]
        assert source["title"] == "Database Transaction Basics"
        assert source["title_source"] == "generated"
        assert "original_title" not in source
    await run(re_enrich=True, enricher="external_agent")
    step = await next_external_enrichment_operation(config, first.job_id, **kwargs)
    if step["status"] != "completed":
        assert "concise readable video title" not in step["operation"]["system_prompt"]
    assert json.loads(transcript.read_text(encoding="utf-8"))["source"]["title_source"] == "generated"
