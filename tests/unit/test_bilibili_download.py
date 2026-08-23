from pathlib import Path

import httpx

from by2kb.providers.bilibili import BilibiliMediaProvider


class _FakeStream:
    def __init__(self, url: str):
        self.url = url
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_bytes(self, chunk_size: int):
        if self.url == "https://primary.example/audio.m4a":
            yield b"partial"
            raise httpx.RemoteProtocolError("peer closed incomplete body")
        yield b"complete-audio"


class _FakeClient:
    def __init__(self):
        self.requested: list[str] = []

    def stream(self, method: str, url: str, *, headers: dict[str, str]):
        self.requested.append(url)
        return _FakeStream(url)


async def test_audio_download_falls_back_to_backup_url(tmp_path: Path):
    client = _FakeClient()
    provider = BilibiliMediaProvider(client, keys=None, work_dir=tmp_path)
    target = tmp_path / "audio.m4a"

    await provider._download(
        [
            "https://primary.example/audio.m4a",
            "https://backup.example/audio.m4a",
        ],
        target,
        "https://www.bilibili.com/video/BV1P5gf6NEjA/",
    )

    assert client.requested == [
        "https://primary.example/audio.m4a",
        "https://backup.example/audio.m4a",
    ]
    assert target.read_bytes() == b"complete-audio"
