import pytest

from by2kb.providers import bilibili_wbi as wbi

IMG_KEY = "7cd084941338484aae1ad9425b84077c"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"
MIXIN_KEY = "ea1db124af3c7062474693fa704f4ff8"


def test_key_from_image_url():
    assert (
        wbi.key_from_image_url(
            "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png"
        )
        == IMG_KEY
    )
    assert (
        wbi.key_from_image_url(
            "//i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png?v=1"
        )
        == SUB_KEY
    )
    assert wbi.key_from_image_url("") == ""
    assert wbi.key_from_image_url(None) == ""


def test_mixin_key_matches_official_vector():
    assert len(wbi.MIXIN_KEY_ENC_TAB) == 64
    assert wbi.get_mixin_key(IMG_KEY, SUB_KEY) == MIXIN_KEY
    assert len(wbi.get_mixin_key(IMG_KEY, SUB_KEY)) == 32


def test_mixin_key_rejects_short_key_material():
    with pytest.raises(ValueError, match="mixin_key"):
        wbi.get_mixin_key("short", "key")


def test_build_query_uses_percent_encoding_uppercase_hex_and_percent20():
    assert (
        wbi.build_query({"foo": "one one four", "bar": "五一四", "baz": 1919810})
        == "bar=%E4%BA%94%E4%B8%80%E5%9B%9B&baz=1919810&foo=one%20one%20four"
    )


def test_build_query_strips_special_characters():
    assert wbi.build_query({"a": "he!l'l(o)*"}) == "a=hello"


def test_w_rid_matches_official_vector():
    signed = wbi.sign_params(
        {"foo": "114", "bar": "514", "zab": 1919810},
        IMG_KEY,
        SUB_KEY,
        now_seconds=1702204169,
    )
    assert signed["wts"] == 1702204169
    assert signed["w_rid"] == "8f6f2b5b3d485fe1886cec6a0be8c5d4"
    assert signed["foo"] == "114"
    assert signed["zab"] == 1919810


def test_signed_url_builds_full_address():
    url = wbi.signed_url(
        "https://api.bilibili.com/x/player/wbi/v2",
        {"aid": 1, "cid": 2, "bvid": "BV1xx411c7mD"},
        IMG_KEY,
        SUB_KEY,
        now_seconds=1702204169,
    )
    assert url.startswith("https://api.bilibili.com/x/player/wbi/v2?")
    expected = wbi.sign_params(
        {"aid": 1, "cid": 2, "bvid": "BV1xx411c7mD"},
        IMG_KEY,
        SUB_KEY,
        now_seconds=1702204169,
    )
    assert f"wts=1702204169" in url
    assert "cid=2" in url
    assert f"w_rid={expected['w_rid']}" in url


def test_parse_nav_response():
    keys = wbi.parse_nav_response(
        {
            "code": 0,
            "data": {
                "wbi_img": {
                    "img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.png",
                    "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png",
                }
            },
        }
    )
    assert keys == (IMG_KEY, SUB_KEY)


def test_parse_nav_response_missing_keys():
    with pytest.raises(ValueError, match="WBI keys"):
        wbi.parse_nav_response({"code": 0, "data": {}})


async def test_key_cache_reuses_within_ttl_and_forces_refresh(httpx_mock_client):
    cache = wbi.WbiKeyCache(httpx_mock_client)
    keys_first = await cache.get_keys()
    keys_second = await cache.get_keys()
    assert keys_first == keys_second == (IMG_KEY, SUB_KEY)
    assert httpx_mock_client.calls == 1

    await cache.get_keys(force=True)
    assert httpx_mock_client.calls == 2
