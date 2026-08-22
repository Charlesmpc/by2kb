from __future__ import annotations

IMG_KEY = "7cd084941338484aae1ad9425b84077c"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"

NAV_PAYLOAD = {
    "code": -101,
    "message": "账号未登录",
    "data": {
        "wbi_img": {
            "img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.png",
            "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png",
        }
    },
}


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: dict | None = None,
        url: str = "",
    ):
        self.status_code = status_code
        self._payload = payload or {}
        self.url = url

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, payload: dict | None = None):
        self._payload = payload if payload is not None else NAV_PAYLOAD
        self.calls = 0

    async def get(self, url, **kwargs):
        self.calls += 1
        return FakeResponse(200, self._payload)


class FakeRedirectClient:
    def __init__(self, final_url: str = "", error: Exception | None = None):
        self.final_url = final_url
        self.error = error
        self.calls = 0

    async def get(self, url, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return FakeResponse(200, url=self.final_url)


import pytest


@pytest.fixture
def httpx_mock_client():
    return FakeClient()


@pytest.fixture
def make_redirect_client():
    def factory(final_url: str = "", error: Exception | None = None):
        return FakeRedirectClient(final_url=final_url, error=error)

    return factory
