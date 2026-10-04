import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from by2kb.cli import app
from by2kb.config import Config, load_config
from by2kb.errors import ConfigError
from by2kb.jobs.runner import IngestOutcome
from by2kb.search.config import SearchConfig
from by2kb.search.models import Candidate, canonical_video_url
from by2kb.search.providers import (
    BilibiliSearch, SearchProviderRegistry, YouTubeSearch, caption_text, normalized_caption,
)
from by2kb.search.service import discover, format_recommendations, parse_selection, select_and_ingest, scope_key
from by2kb.search.store import SearchStore
from by2kb.normalize import Segment


def settings(tmp_path, **search):
    return Config(home=tmp_path / "home", library_root=tmp_path / "kb", db_path=tmp_path / "home" / "jobs.db",
                  search=replace(SearchConfig(), **search))


def video(number=0, provider="youtube"):
    return Candidate(provider=provider, url=f"https://www.youtube.com/watch?v=video{number:06d}",
                     title=f"TiDB tutorial {number}", author="teacher", description="Create a vector index", duration_s=120)


def captions(candidate):
    return normalized_caption(candidate, [Segment(start_ms=0, duration_ms=120000,
        text="Create a TiDB vector index and query the nearest neighbours. First choose an embedding model suitable for the language of your documents. Store its output alongside the original text so that results remain explainable. A cosine distance measures direction while Euclidean distance measures separation. Filter by tenant before returning records to the caller. Evaluate recall against an exact baseline and inspect latency under realistic concurrency. Schema changes and model upgrades require careful migration because dimensions and distributions can differ.")],
                              language="en", kind="human", provider="yt_dlp")


class FakeProvider:
    name = "youtube"

    def __init__(self, preview=True):
        self.previews = []
        self.has_preview = preview
        self.queries = []

    async def search(self, topic, limit, client):
        self.queries.append(topic)
        return [video(n) for n in range(limit)]

    async def preview(self, candidate, languages, client, max_bytes):
        self.previews.append(candidate.url)
        return captions(candidate) if self.has_preview else None


def registry(*providers):
    result = SearchProviderRegistry()
    for provider in providers:
        result.register(provider)
    return result


@pytest.mark.asyncio
async def test_discovery_caps_preview_no_ingest_and_metadata_policy(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("search must never ingest or run ASR")
    monkeypatch.setattr("by2kb.jobs.runner.ingest_source", forbidden)
    provider = FakeProvider()
    config = settings(tmp_path, providers=("youtube",))
    result = await discover("我想了解一下 TiDB", config, registry=registry(provider))
    assert provider.queries == ["TiDB"]
    assert len(provider.previews) == len(result["candidates"]) == 3
    assert all(c["preview_status"] == "captions_ready" for c in result["candidates"])
    assert SearchStore(config.home).cache_path(result["session_id"], 1).is_file()
    provider.previews.clear()
    config.search = replace(config.search, preview_mode="metadata_only")
    result = await discover("TiDB", config, registry=registry(provider))
    assert not provider.previews
    assert all(c["preview_status"] == "metadata_only" for c in result["candidates"])
    assert "尚未评审完整视频" in format_recommendations(result)


@pytest.mark.asyncio
async def test_failed_provider_partial_results_disabled_source_not_called(tmp_path):
    class Broken(FakeProvider):
        name = "broken"
        async def search(self, *args):
            raise RuntimeError("private signed url must not leak")
    class Disabled(FakeProvider):
        name = "disabled"
        async def search(self, *args):
            raise AssertionError("disabled provider called")
    good = FakeProvider(preview=False)
    config = settings(tmp_path, providers=("broken", "youtube", "disabled"), options={"disabled": {"enabled": False}})
    result = await discover("TiDB", config, registry=registry(Broken(), good, Disabled()))
    assert len(result["candidates"]) == 3
    assert len(result["warnings"]) == 1
    assert "private signed" not in json.dumps(result)
    assert all(c["preview_status"] == "no_accessible_captions" for c in result["candidates"])


@pytest.mark.asyncio
async def test_timeout_cancellation_and_dedupe(tmp_path):
    class Duplicate(FakeProvider):
        async def search(self, *args):
            return [video(0), video(0), video(1)]
        async def preview(self, *args):
            await asyncio.sleep(5)
    config = settings(tmp_path, providers=("youtube",), preview_timeout_s=.01)
    result = await discover("TiDB", config, registry=registry(Duplicate()))
    assert len(result["candidates"]) == 2
    assert all(c["preview_status"] == "preview_failed" for c in result["candidates"])


@pytest.mark.asyncio
async def test_named_subject_filter_and_global_relevance_ranking(tmp_path):
    class Bili(FakeProvider):
        name = "bilibili"
        async def search(self, *args):
            return [video(0, provider=self.name).model_copy(update={"title": "TiDB basics", "description": "Database basics"}),
                    video(1, provider=self.name).model_copy(update={"title": "Vector illustration", "description": "Graphic design"})]
    class Youtube(FakeProvider):
        async def search(self, *args):
            return [video(2).model_copy(update={"title": "TiDB vector search tutorial", "description": "Nearest neighbour queries"})]
    config = settings(tmp_path, preview_mode="metadata_only")
    result = await discover("TiDB vector search tutorial", config, registry=registry(Bili(), Youtube()))
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["title"] == "TiDB vector search tutorial"
    assert not any(c["title"] == "Vector illustration" for c in result["candidates"])


@pytest.mark.asyncio
async def test_newer_search_wins_when_old_search_finishes_later(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    class Slow(FakeProvider):
        async def search(self, topic, limit, client):
            if topic == "TiDB old":
                entered.set()
                await release.wait()
            return [video()]
    config = settings(tmp_path, providers=("youtube",), preview_mode="metadata_only")
    source = registry(Slow())
    old = asyncio.create_task(discover("TiDB old", config, registry=source))
    await entered.wait()
    new = await discover("TiDB new", config, registry=source)
    release.set()
    assert (await old)["status"] == "superseded"
    assert SearchStore(config.home).latest("local")["session_id"] == new["session_id"]


@pytest.mark.asyncio
async def test_caption_cache_tampering_falls_back_only_after_selection(tmp_path, monkeypatch):
    config = settings(tmp_path, providers=("youtube",))
    session = await discover("TiDB", config, registry=registry(FakeProvider()))
    cache = SearchStore(config.home).cache_path(session["session_id"], 1)
    normalized = json.loads(cache.read_text(encoding="utf-8"))
    normalized["transcript"]["segments"][0]["text"] = "tampered"
    cache.write_text(json.dumps(normalized), encoding="utf-8")
    calls = []
    async def ingest(url, config, **kwargs):
        calls.append(kwargs)
        return IngestOutcome(exit_code=0, status="completed")
    monkeypatch.setattr("by2kb.jobs.runner.ingest_source", ingest)
    await select_and_ingest(session["session_id"], "1", config)
    assert calls[0]["cached_transcript"] is None


def test_scopes_expiry_supersession_and_cancel(tmp_path):
    store = SearchStore(tmp_path)
    scope = scope_key("telegram", "room", "alice")
    first = store.begin("topic", scope, 60)
    with pytest.raises(ConfigError):
        store.load(first["session_id"], scope_key("telegram", "room", "bob"))
    second = store.begin("topic2", scope, 60)
    with pytest.raises(ConfigError, match="superseded"):
        store.load(first["session_id"], scope)
    store.cancel(second["session_id"], scope)
    second["status"] = "awaiting_selection"
    store.finish(second)
    assert store.latest(scope)["status"] == "cancelled"
    expired = store.begin("old", scope, -1)
    with pytest.raises(ConfigError, match="expired"):
        store.load(expired["session_id"], scope)


@pytest.mark.asyncio
async def test_selection_reuses_caption_topic_and_repeat_does_not_ingest(tmp_path, monkeypatch):
    config = settings(tmp_path, providers=("youtube",))
    session = await discover("TiDB", config, registry=registry(FakeProvider()))
    calls = []
    async def ingest(url, config, **kwargs):
        calls.append(kwargs)
        return IngestOutcome(exit_code=0, job_id="job", status="enrichment_pending")
    monkeypatch.setattr("by2kb.jobs.runner.ingest_source", ingest)
    selected = await select_and_ingest(session["session_id"], "1,3", config)
    assert len(selected["results"]) == len(calls) == 2
    assert calls[0]["cached_transcript"].source.canonical_url == video(0).url
    assert calls[0]["learning_topic"] == "TiDB"
    await select_and_ingest(session["session_id"], "1,3", config)
    assert len(calls) == 2
    with pytest.raises(ConfigError, match="already has"):
        await select_and_ingest(session["session_id"], "2", config)


@pytest.mark.asyncio
async def test_cached_caption_end_to_end_bypasses_acquisition_and_asr(tmp_path, monkeypatch):
    from by2kb.jobs.runner import ingest_source
    from by2kb.providers.yt_dlp_source import YtDlpSourceProvider
    from by2kb.providers.asr_registry import build_default_asr_registry
    async def forbidden(*args, **kwargs):
        raise AssertionError("cached subtitles must skip acquisition")
    monkeypatch.setattr(YtDlpSourceProvider, "resolve", forbidden)
    monkeypatch.setattr(YtDlpSourceProvider, "prepare", forbidden)
    asr = build_default_asr_registry()
    monkeypatch.setattr(asr, "create", lambda *args: (_ for _ in ()).throw(AssertionError("ASR called")))
    config = settings(tmp_path)
    result = await ingest_source(video().url, config, cached_transcript=captions(video()),
                                 learning_topic="TiDB vector search", enricher="external_agent", asr_registry=asr)
    assert result.status == "enrichment_pending", result.message
    transcript = json.loads(open(result.artifacts["transcript_json"], encoding="utf-8").read())
    assert transcript["learning_topic"] == "TiDB vector search"
    source = json.loads(open(result.artifacts["source_json"], encoding="utf-8").read())
    assert source["provenance"]["reused_search_preview"] is True
    assert "audio" not in source["provenance"]


def test_config_and_cli_selection_protocol(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('[search]\nenabled = false\nproviders = ["youtube"]\n[search.preview]\nmode = "metadata_only"\n', encoding="utf-8")
    monkeypatch.setenv("BY2KB_HOME", str(home))
    config = load_config(home)
    assert not config.search.enabled and config.search.providers == ("youtube",)
    assert config.search.preview_mode == "metadata_only"
    cli = CliRunner()
    result = cli.invoke(app, ["search", "discover", "TiDB", "--json"])
    assert result.exit_code == 1 and json.loads(result.output)["status"] == "error"
    assert json.loads(cli.invoke(app, ["search", "latest", "--json"]).output) is None
    store = SearchStore(home)
    session = store.begin("TiDB", "local", 3600)
    session.update(status="awaiting_selection", candidates=[video().model_dump()])
    store.save(session)
    result = cli.invoke(app, ["search", "select", session["session_id"], "0", "--json"])
    assert result.exit_code == 1 and json.loads(result.output)["status"] == "error"
    assert json.loads(cli.invoke(app, ["search", "cancel", session["session_id"], "--json"]).output)["status"] == "cancelled"


@pytest.mark.parametrize("mapping", [{"enabled": "false"}, {"providers": "youtube"},
    {"providers": ["youtube", "youtube"]}, {"preview": {"mode": "audio"}},
    {"max_candidates": 10}, {"timeout_s": -1}, {"preview": {"max_bytes": 0}},
    {"youtube": {"enabled": "false"}}, {"session_ttl_s": 1}])
def test_invalid_search_settings(mapping):
    with pytest.raises(ConfigError):
        SearchConfig.from_mapping(mapping)


@pytest.mark.parametrize("url", ["http://127.0.0.1", "https://youtube.com.evil/watch?v=video000000",
                                "https://user:pass@youtube.com/watch?v=video000000", "https://youtube.com/playlist?list=x"])
def test_candidate_url_validation(url):
    with pytest.raises(ValueError):
        canonical_video_url(url)


@pytest.mark.asyncio
async def test_caption_size_and_redirect_boundaries():
    def handler(request):
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})
        return httpx.Response(200, content=b"x" * 2000)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="size"):
            await caption_text(client, "https://youtube.com/caption", 1000)
        with pytest.raises(ValueError, match="endpoint"):
            await caption_text(client, "https://youtube.com/redirect", 1000)


@pytest.mark.asyncio
async def test_youtube_worker_requests_no_media_and_uses_caption_metadata(monkeypatch):
    calls = []
    async def metadata(source, *, flat=False):
        calls.append((source, flat))
        if flat:
            return [{"id": "video000000", "title": "TiDB", "view_count": 12500}]
        return {"view_count": 13000, "like_count": 123, "subtitles": {"en": [{"ext": "json3", "url": "https://youtube.com/captions"}]}}
    monkeypatch.setattr("by2kb.search.providers.youtube_metadata", metadata)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={
        "events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "TiDB"}]}]}))) as client:
        provider = YouTubeSearch()
        results = await provider.search("TiDB", 3, client)
        assert results[0].view_count == 12500 and results[0].like_count is None
        preview = await provider.preview(results[0], ["en"], client, 2000)
        assert results[0].view_count == 13000 and results[0].like_count == 123
        assert preview.transcript.segments[0].text == "TiDB"
    assert calls == [("ytsearch3:TiDB", True), (video().url, False)]


@pytest.mark.asyncio
async def test_bilibili_search_parses_duration_and_no_caption_does_not_get_audio():
    paths = []
    def handler(request):
        paths.append(request.url.path)
        if "search/type" in request.url.path:
            return httpx.Response(200, json={"code": 0, "data": {"result": [{"bvid": "BV1Pyta66EDh",
                "title": '<em class="keyword">TiDB</em> tutorial', "duration": "1:02:03", "play": "1.2万"}]}})
        if request.url.path.endswith("view"):
            return httpx.Response(200, json={"code": 0, "data": {"bvid": "BV1Pyta66EDh", "cid": 1, "aid": 2, "stat": {"view": 12005, "like": 321}}})
        return httpx.Response(200, json={"code": 0, "data": {"subtitle": {"subtitles": []}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = BilibiliSearch()
        results = await provider.search("TiDB", 3, client)
        assert results[0].duration_s == 3723 and results[0].title == "TiDB tutorial"
        assert results[0].view_count == 12000
        assert await provider.preview(results[0], ["zh"], client, 2000) is None
        assert results[0].view_count == 12005 and results[0].like_count == 321
    assert not any("playurl" in path for path in paths)


@pytest.mark.parametrize("value,expected", [(None,None),(0,0),("1,234",1234),("1.2万",12000),(-1,None),(True,None),("unknown",None),(1.5,None)])
def test_public_metric_normalization(value, expected):
    assert video().model_copy().model_validate({**video().model_dump(), "view_count": value}).view_count == expected


def test_result_cards_preserve_platforms_precision_and_missing_metrics():
    yt = video().model_dump()
    yt.update(duration_s=154, view_count=123456, like_count=0)
    bili = video(1).model_dump()
    bili.update(provider="bilibili",duration_s=3723)
    text = format_recommendations(dict(status="awaiting_selection",topic="我想要看一看comfyui教程",candidates=[yt,bili],warnings=["bilibili: 搜索未完成"]))
    assert "学习主题：comfyui教程" in text
    assert "1｜🟥 YouTube" in text and "2｜🟦 B站 · bilibili" in text
    assert "⏱ 2分34秒 · ▶ 播放 12.3万 · 👍 点赞 0" in text
    assert "⏱ 1小时02分03秒" in text
    assert "未提供" not in text
    assert "搜索未完成" not in text
    assert "预览未完成" not in text
    assert "优先查看第 1 项" not in text


@pytest.mark.parametrize("preview_status", ["metadata_only", "captions_ready", "no_accessible_captions", "preview_failed"])
def test_compact_cards_hide_preview_failures_and_keep_available_metrics(preview_status):
    candidate = video().model_dump()
    candidate.update(preview_status=preview_status, view_count=None, like_count=None)
    session=dict(status="awaiting_selection",topic="TiDB",candidates=[candidate],warnings=["youtube: 搜索未完成"])
    rendered = format_recommendations(session)
    assert "预览" not in rendered and "未提供" not in rendered
    assert "播放" not in rendered and "点赞" not in rendered
    assert "⏱ 2分00秒" in rendered
    assert "video000000" in rendered
    assert session["candidates"][0]["preview_status"] == preview_status


def test_description_links_do_not_clutter_cards():
    candidate=video().model_dump()
    candidate['description']='节点入门 https://example.org/promo?tracking=1 模型配置'
    result=format_recommendations(dict(status='awaiting_selection',topic='TiDB',candidates=[candidate],warnings=[]))
    assert 'example.org' not in result
    assert '节点入门 模型配置' in result
    assert candidate['url'] in result
