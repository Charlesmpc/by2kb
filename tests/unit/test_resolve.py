import httpx
import pytest

from by2kb.errors import TransientProviderError, UnsupportedUrl
from by2kb.jobs.runner import resolve_url


async def test_canonical_url_resolves_without_client():
    identity = await resolve_url(
        "https://www.bilibili.com/video/BV1jmbD65EP2/?share_source=copy_web", None
    )
    assert identity.platform == "bilibili"
    assert identity.video_id == "BV1jmbD65EP2"
    assert identity.canonical_url == "https://www.bilibili.com/video/BV1jmbD65EP2/"


async def test_bare_bvid_resolves():
    identity = await resolve_url("BV1jmbD65EP2", None)
    assert identity.video_id == "BV1jmbD65EP2"


async def test_short_link_expands_to_canonical_bvid(make_redirect_client):
    client = make_redirect_client(
        "https://www.bilibili.com/video/BV1DoLR62Eqh/?share_source=copy_web"
    )
    identity = await resolve_url("https://b23.tv/pjdcIJm", client)
    assert client.calls == 1
    assert identity.video_id == "BV1DoLR62Eqh"
    assert identity.canonical_url == "https://www.bilibili.com/video/BV1DoLR62Eqh/"


async def test_short_link_and_canonical_dedupe_to_same_identity(
    make_redirect_client,
):
    client = make_redirect_client("https://www.bilibili.com/video/BV1DoLR62Eqh/")
    via_short = await resolve_url("https://b23.tv/pjdcIJm", client)
    via_canonical = await resolve_url(
        "https://www.bilibili.com/video/BV1DoLR62Eqh/", None
    )
    assert (via_short.platform, via_short.video_id) == (
        via_canonical.platform,
        via_canonical.video_id,
    )


async def test_short_link_to_non_video_target_is_terminal(make_redirect_client):
    client = make_redirect_client("https://www.bilibili.com/read/cv12345")
    with pytest.raises(UnsupportedUrl):
        await resolve_url("https://b23.tv/abcdefg", client)


async def test_short_link_transport_failure_is_retryable(make_redirect_client):
    client = make_redirect_client(error=httpx.ConnectError("connection reset"))
    with pytest.raises(TransientProviderError):
        await resolve_url("https://b23.tv/abcdefg", client)


async def test_youtube_url_is_unsupported():
    with pytest.raises(UnsupportedUrl, match="YouTube"):
        await resolve_url("https://youtu.be/l38ceFOWOAE", None)


async def test_unknown_url_is_unsupported():
    with pytest.raises(UnsupportedUrl):
        await resolve_url("https://example.com/video/123", None)
