import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from by2kb.config import Config
from by2kb.errors import ConfigError
from by2kb.search.config import SearchConfig
from by2kb.search.models import Candidate
from by2kb.search.planning import fallback_plan, validate_plan, intent_score
from by2kb.search.providers import SearchProviderRegistry, BilibiliSearch
from by2kb.search.service import discover, format_recommendations
from by2kb.search.store import SearchStore
from by2kb.integrations.hermes import _plan_search


def candidate(n=0, title="Supabase 入门介绍", duration=180):
    return Candidate(provider="bilibili", url=f"https://www.bilibili.com/video/BV1MjjSzCEv{n}/",
                     title=title, duration_s=duration)


def config(tmp_path, **kw):
    return Config(home=tmp_path / "home", library_root=tmp_path / "kb", db_path=tmp_path / "db",
                  search=replace(SearchConfig(), providers=("bilibili",), retry_base_s=0, preview_mode="metadata_only", **kw))


class Sequence:
    name = "bilibili"
    def __init__(self, replies): self.replies, self.calls = replies, []
    async def search(self, query, limit, client):
        self.calls.append(query)
        value = self.replies[min(len(self.calls)-1, len(self.replies)-1)]
        if isinstance(value, Exception): raise value
        return value


def registry(*providers):
    result = SearchProviderRegistry()
    for p in providers: result.register(p)
    return result


@pytest.mark.parametrize("topic", ["我想查一下什么是supabase", "我想了解下supabase", "我想了解一下supabase", "什么是supabase？", "supabase是什么"])
def test_equivalent_requests_extract_subject_and_intent(topic):
    plan = fallback_plan(topic)
    assert plan["subject"] == "supabase"
    assert plan["intent"] == "introduction"
    assert plan["queries"][0] == "supabase"


def test_host_plan_rewrites_filler_preserves_constraints_and_safe_fallback():
    ctx = SimpleNamespace(llm=SimpleNamespace(complete=lambda **kw: SimpleNamespace(text=json.dumps({
        "subject": "Supabase", "intent": "introduction", "queries": ["Supabase", "Supabase 入门"]}))))
    plan = _plan_search(ctx, "能不能给我讲讲 Supabase 是什么", 1)
    assert plan["subject"] == "Supabase" and plan["origin"] == "host_model"
    assert _plan_search(SimpleNamespace(), "我想了解下supabase", 1)["origin"] == "rules"
    with pytest.raises(ConfigError): validate_plan({"subject":"Supabase", "intent":"deployment", "queries":["Supabase"]}, "Supabase 本地部署")
    with pytest.raises(ConfigError): validate_plan({"subject":"Postgres", "intent":"introduction", "queries":["Postgres"]}, "什么是Supabase")
    with pytest.raises(ConfigError): validate_plan({"subject":"数据库", "intent":"general", "queries":["http://example.com"]}, "数据库")


@pytest.mark.asyncio
async def test_empty_and_transient_retries_preserve_results(tmp_path):
    error = httpx.HTTPStatusError("secret signed URL", request=httpx.Request("GET", "https://example.com"), response=httpx.Response(412))
    p = Sequence([[], error, [candidate(n) for n in range(3)]])
    result = await discover("supabase", config(tmp_path), registry=registry(p))
    assert len(p.calls) == 3 and len(result["candidates"]) == 3
    assert result["warnings"] == []
    assert [a["outcome"] for a in result["sources"][0]["attempts"]] == ["empty", "rate_limited", "ok"]
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_failures_never_misreport_no_results(tmp_path):
    p = Sequence([httpx.ConnectError("private secret")])
    result = await discover("supabase", config(tmp_path), registry=registry(p))
    assert len(p.calls) == 3 and result["status"] == "search_failed"
    assert "未完成" in format_recommendations(result) and "没有找到" not in format_recommendations(result)
    p = Sequence([[]])
    result = await discover("supabase", config(tmp_path), registry=registry(p))
    assert len(p.calls) == 3 and result["status"] == "no_results"
    assert "没有找到匹配" in format_recommendations(result)


@pytest.mark.asyncio
async def test_filtered_empty_and_permanent_errors_not_blindly_retried(tmp_path):
    p = Sequence([[candidate(title="Unrelated cats")]])
    result = await discover("supabase", config(tmp_path), registry=registry(p))
    assert len(p.calls) == 1 and result["sources"][0]["outcome"] == "filtered_empty"
    error = httpx.HTTPStatusError("forbidden", request=httpx.Request("GET", "https://example.com"), response=httpx.Response(403))
    p = Sequence([error])
    result = await discover("supabase", config(tmp_path), registry=registry(p))
    assert len(p.calls) == 1 and result["status"] == "search_failed"


@pytest.mark.asyncio
async def test_expansion_prefers_introduction_and_keeps_partial_failure(tmp_path):
    deployment = candidate(0, "Supabase 本地部署教程", 4500)
    intro = candidate(1, "什么是 Supabase 入门介绍", 185)
    p = Sequence([[deployment], [intro], httpx.ConnectError("blocked")])
    result = await discover("我想了解下supabase", config(tmp_path), registry=registry(p))
    assert len(p.calls) == 3 and result["candidates"][0]["title"] == intro.title
    assert len(result["candidates"]) == 2
    assert result["sources"][0]["outcome"] == "partial"
    assert "暂时未能完成搜索" in format_recommendations(result)
    assert intent_score(intro, "introduction") > intent_score(deployment, "introduction")


@pytest.mark.asyncio
async def test_retry_after_outside_budget_stops_without_sleep(tmp_path):
    error = httpx.HTTPStatusError("limited", request=httpx.Request("GET", "https://example.com"), response=httpx.Response(429, headers={"Retry-After":"3600"}))
    p = Sequence([error]);start=time.monotonic()
    result = await discover("supabase", config(tmp_path), registry=registry(p), budget_s=.5)
    assert time.monotonic()-start < 1 and len(p.calls) == 1
    assert result["sources"][0]["attempts"][0]["http_status"] == 429


@pytest.mark.asyncio
async def test_budget_cancellation_and_pending_session_supersession(tmp_path):
    class Slow(Sequence):
        cancelled = False
        async def search(self, *args):
            try: await asyncio.sleep(5)
            finally: self.cancelled = True
    p=Slow([]);cfg=config(tmp_path);start=time.monotonic()
    result=await discover("supabase",cfg,registry=registry(p),budget_s=.5)
    assert time.monotonic()-start < .8 and p.cancelled and result["status"] == "search_failed"
    store=SearchStore(cfg.home);old=store.begin("supabase","local",3600);new=store.begin("supabase","local",3600)
    with pytest.raises(ConfigError): await discover("supabase",cfg,registry=registry(Sequence([[]])),session_id=old["session_id"])
    assert store.latest("local")["session_id"] == new["session_id"]


@pytest.mark.asyncio
async def test_invalid_payload_is_failure_not_empty(tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={"code":0,"data":{"oops":[]}}))) as client:
        result=await discover("supabase",config(tmp_path),client=client,registry=registry(BilibiliSearch()))
    assert result["status"] == "search_failed" and len(result["sources"][0]["attempts"]) == 1
    assert result["sources"][0]["outcome"] == "invalid_response"


@pytest.mark.parametrize("setting", [{"total_timeout_s":float("nan")},{"max_attempts":4},{"attempt_timeout_s":0},{"retry_base_s":float("inf")}])
def test_retry_config_validation_and_legacy_compatibility(setting):
    with pytest.raises(ConfigError): SearchConfig.from_mapping(setting)
    assert SearchConfig.from_mapping({"timeout_s":20,"preview":{"timeout_s":20}}).total_timeout_s == 28
