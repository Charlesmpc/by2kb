import httpx
import pytest

from by2kb.errors import TransientProviderError
from by2kb.providers.bilibili import (
    BilibiliMediaProvider, fetch_video_info, resolve,
)
from by2kb.providers.bilibili_wbi import sign_params
from by2kb.providers.base import FetchOptions

BVID = "BV1Pyta66EDh"
KEYS = ("a" * 32, "b" * 32)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 412])
async def test_metadata_referer_and_preserved_failure(status):
    def handle(request):
        assert request.headers["referer"] == f"https://www.bilibili.com/video/{BVID}/"
        assert "cookie" not in request.headers
        return httpx.Response(status, json={"code": 0, "data": {"cid": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        if status == 412:
            from by2kb.errors import RateLimited
            with pytest.raises(RateLimited, match="HTTP 412"):
                await fetch_video_info(client, BVID)
        else:
            assert (await fetch_video_info(client, BVID)).cid == 1


@pytest.mark.asyncio
async def test_playback_fields_are_included_in_signature(tmp_path):
    class Keys:
        async def get_keys(self):
            return KEYS

    def handle(request):
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(200, json={"code": 0, "data": {
                "cid": 1, "aid": 2, "bvid": BVID, "duration": 10,
            }})
        if request.url.path == "/x/player/wbi/playurl":
            params = dict(request.url.params)
            signature = params.pop("w_rid")
            assert params["web_location"] == "1550101"
            for name in ("dm_img_list", "dm_img_str", "dm_cover_img_str", "dm_img_inter"):
                assert params[name]
            assert sign_params(params, *KEYS, now_seconds=int(params["wts"]))["w_rid"] == signature
            assert request.headers["referer"] == f"https://www.bilibili.com/video/{BVID}/"
            return httpx.Response(200, json={"code": 0, "data": {"dash": {"audio": [
                {"baseUrl": "https://media.test/audio", "bandwidth": 100},
            ]}}})
        assert request.url.host == "media.test"
        return httpx.Response(200, content=b"audio")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = BilibiliMediaProvider(client, Keys(), tmp_path)
        audio = await provider.fetch_audio(resolve(BVID), FetchOptions())
        assert audio.path.read_bytes() == b"audio"
