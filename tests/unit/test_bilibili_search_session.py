import httpx
import pytest

from by2kb.errors import RateLimited
from by2kb.search.providers import BilibiliSearch


ROW={"bvid":"BV1MjjSzCEvG","title":"Supabase 入门","duration":"8:10","play":2424}


@pytest.mark.asyncio
async def test_anonymous_session_cookie_sent_once_and_client_isolation():
    paths=[]
    def handler(r):
        paths.append(r.url.path)
        if r.url.host == "www.bilibili.com":
            return httpx.Response(200,headers={"Set-Cookie":"buvid3=anonymous-test; Domain=.bilibili.com; Path=/; Secure"})
        assert "buvid3=anonymous-test" in r.headers.get("cookie", "")
        return httpx.Response(200,json={"code":0,"data":{"result":[ROW]}})
    provider=BilibiliSearch()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert len(await provider.search("supabase",20,client))==1
        await provider.search("supabase 入门",20,client)
        assert paths.count("/")==1
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await provider.search("supabase",20,client)
        assert paths.count("/")==2


@pytest.mark.parametrize("business", [False,True])
@pytest.mark.asyncio
async def test_risk_control_switches_next_attempt_to_video_group(business):
    calls=[]
    def handler(r):
        calls.append(r.url.path)
        if r.url.host == "www.bilibili.com":return httpx.Response(200)
        if r.url.path.endswith("/type"):
            return httpx.Response(200,json={"code":-412}) if business else httpx.Response(412)
        assert "search_type" not in r.url.params
        return httpx.Response(200,json={"code":0,"data":{"result":[{"result_type":"media_bangumi","data":[{"title":"Ignore anime"}]},{"result_type":"video","data":[ROW]}]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        p=BilibiliSearch()
        with pytest.raises((httpx.HTTPStatusError,RateLimited)):await p.search("supabase",20,client)
        rows=await p.search("supabase",20,client)
    assert len(rows)==1 and rows[0].view_count==2424
    assert calls==["/","/x/web-interface/search/type","/x/web-interface/search/all/v2"]


@pytest.mark.asyncio
async def test_homepage_failure_does_not_suppress_api():
    def handler(r):
        if r.url.host == "www.bilibili.com":raise httpx.ReadTimeout("unavailable")
        return httpx.Response(200,json={"code":0,"data":{"result":[ROW]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert len(await BilibiliSearch().search("supabase",20,client))==1


@pytest.mark.asyncio
async def test_invalid_fallback_video_group_is_not_empty_result():
    def handler(r):
        if r.url.host == "www.bilibili.com":return httpx.Response(200)
        if r.url.path.endswith("/type"):return httpx.Response(412)
        return httpx.Response(200,json={"code":0,"data":{"result":[{"result_type":"video","data":{}}]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        p=BilibiliSearch()
        with pytest.raises(httpx.HTTPStatusError):await p.search("supabase",20,client)
        with pytest.raises(ValueError):await p.search("supabase",20,client)
