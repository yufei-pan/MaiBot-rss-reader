"""出站 RSS URL 安全（SSRF 防护）测试。"""

import asyncio

import httpx
import pytest

from plugin import (
    FeedFetchPolicy,
    _assert_host_allowed,
    _is_private_address,
    validate_outbound_feed_url,
)

SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>T</title>
<item><title>x</title><link>https://example.com/1</link><guid>1</guid></item>
</channel></rss>"""


def test_is_private_address():
    assert _is_private_address("127.0.0.1") is True
    assert _is_private_address("10.1.2.3") is True
    assert _is_private_address("192.168.0.1") is True
    assert _is_private_address("169.254.1.1") is True
    assert _is_private_address("::1") is True
    assert _is_private_address("8.8.8.8") is False


def test_assert_host_allowed_blocks_loopback():
    async def run() -> None:
        with pytest.raises(ValueError, match="内网/保留地址"):
            await _assert_host_allowed("127.0.0.1", allow_private_networks=False)

    asyncio.run(run())


def test_assert_host_allowed_allows_when_configured():
    async def run() -> None:
        await _assert_host_allowed("127.0.0.1", allow_private_networks=True)

    asyncio.run(run())


def test_validate_outbound_feed_url_rejects_http_by_default():
    async def run() -> None:
        with pytest.raises(ValueError, match="仅允许 https"):
            await validate_outbound_feed_url(
                "http://example.com/feed.xml", policy=FeedFetchPolicy()
            )

    asyncio.run(run())


def test_validate_outbound_feed_url_allows_http_when_configured():
    async def run() -> None:
        url = await validate_outbound_feed_url(
            "http://example.com/feed.xml",
            policy=FeedFetchPolicy(allow_http=True),
        )
        assert url == "http://example.com/feed.xml"

    asyncio.run(run())


def test_validate_outbound_feed_url_rejects_private_ip():
    async def run() -> None:
        with pytest.raises(ValueError, match="内网/保留地址"):
            await validate_outbound_feed_url(
                "https://127.0.0.1/feed.xml", policy=FeedFetchPolicy()
            )

    asyncio.run(run())


def test_validate_outbound_feed_url_allows_private_when_configured():
    async def run() -> None:
        url = await validate_outbound_feed_url(
            "https://127.0.0.1/feed.xml",
            policy=FeedFetchPolicy(allow_private_networks=True),
        )
        assert url == "https://127.0.0.1/feed.xml"

    asyncio.run(run())


def test_validate_outbound_feed_url_rejects_embedded_credentials():
    async def run() -> None:
        with pytest.raises(ValueError, match="内嵌认证信息"):
            await validate_outbound_feed_url(
                "https://user:pass@example.com/feed.xml",
                policy=FeedFetchPolicy(allow_http=True),
            )

    asyncio.run(run())


def test_redirect_to_private_blocked():
    from plugin import _assert_scheme_allowed

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url.host) == "8.8.8.8":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/feed"})
        return httpx.Response(200, content=SAMPLE_RSS)

    policy = FeedFetchPolicy(allow_http=True)

    async def ssrf_request_hook(request: httpx.Request) -> None:
        _assert_scheme_allowed(str(request.url.scheme or ""), allow_http=policy.allow_http)
        await _assert_host_allowed(
            str(request.url.host or ""),
            allow_private_networks=policy.allow_private_networks,
        )

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=True,
            event_hooks={"request": [ssrf_request_hook]},
        ) as client:
            with pytest.raises(ValueError, match="内网/保留地址"):
                await client.get("https://8.8.8.8/feed")

    asyncio.run(run())
