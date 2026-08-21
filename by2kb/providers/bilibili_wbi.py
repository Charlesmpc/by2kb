from __future__ import annotations

import hashlib
import time
import urllib.parse

import httpx

from by2kb.errors import TransientProviderError

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
KEY_TTL_S = 3600.0
NAV_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)

_STRIP_CHARS = str.maketrans("", "", "!'()*")


def key_from_image_url(url: str | None) -> str:
    name = str(url or "").rsplit("/", 1)[-1].split("?")[0]
    return name.rsplit(".", 1)[0] if "." in name else name


def get_mixin_key(img_key: str, sub_key: str) -> str:
    raw = f"{img_key or ''}{sub_key or ''}"
    if len(raw) < 64:
        raise ValueError("WBI key material too short to derive mixin_key")
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def build_query(params: dict) -> str:
    parts = []
    for key in sorted(params):
        value = str(params[key]).translate(_STRIP_CHARS)
        parts.append(
            f"{urllib.parse.quote(str(key), safe='')}={urllib.parse.quote(value, safe='')}"
        )
    return "&".join(parts)


def sign_params(
    params: dict, img_key: str, sub_key: str, now_seconds: int | None = None
) -> dict:
    mixin_key = get_mixin_key(img_key, sub_key)
    wts = int(now_seconds) if now_seconds and now_seconds > 0 else int(time.time())
    signed = {**params, "wts": wts}
    query = build_query(signed)
    return {**signed, "w_rid": hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()}


def signed_url(
    base_url: str,
    params: dict,
    img_key: str,
    sub_key: str,
    now_seconds: int | None = None,
) -> str:
    signed = sign_params(params, img_key, sub_key, now_seconds)
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{build_query(signed)}"


def parse_nav_response(data: dict) -> tuple[str, str]:
    wbi_img = (data.get("data") or {}).get("wbi_img") or {}
    img_key = key_from_image_url(wbi_img.get("img_url"))
    sub_key = key_from_image_url(wbi_img.get("sub_url"))
    if not img_key or not sub_key:
        raise ValueError("nav response did not include WBI keys")
    return img_key, sub_key


class WbiKeyCache:
    def __init__(self, client: httpx.AsyncClient, ttl_s: float = KEY_TTL_S):
        self._client = client
        self._ttl_s = ttl_s
        self._keys: tuple[str, str] | None = None
        self._fetched_at = 0.0

    async def get_keys(self, *, force: bool = False) -> tuple[str, str]:
        if (
            not force
            and self._keys is not None
            and time.monotonic() - self._fetched_at < self._ttl_s
        ):
            return self._keys
        try:
            response = await self._client.get(NAV_URL, headers=NAV_HEADERS)
        except httpx.HTTPError as exc:
            raise TransientProviderError(
                f"WBI key fetch failed: {exc}", provider="bilibili"
            ) from exc
        if response.status_code != 200:
            raise TransientProviderError(
                f"WBI key fetch failed: HTTP {response.status_code}", provider="bilibili"
            )
        self._keys = parse_nav_response(response.json())
        self._fetched_at = time.monotonic()
        return self._keys

    def clear(self) -> None:
        self._keys = None
        self._fetched_at = 0.0
