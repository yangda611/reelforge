"""Gateway credentials authenticate video downloads without reaching other origins."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp import web

from novelvideo.generators.video_generator import NewApiVideoGenerator
from novelvideo.ports.video_delivery import VideoDeliveryError
from novelvideo.utils.media_download import _PublicResolver, download_to_path


@asynccontextmanager
async def _serve(app: web.Application) -> AsyncIterator[str]:
    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


async def test_same_origin_protected_video_download_uses_gateway_authorization(
    tmp_path: Path,
) -> None:
    authorization_headers: list[str | None] = []

    async def content(request: web.Request) -> web.Response:
        authorization_headers.append(request.headers.get("Authorization"))
        if request.headers.get("Authorization") != "Bearer test-key":
            raise web.HTTPUnauthorized()
        return web.Response(body=b"protected-video", content_type="video/mp4")

    gateway = web.Application()
    gateway.router.add_get("/v1/videos/task-1/content", content)
    output_path = tmp_path / "videos" / "result.mp4"

    async with _serve(gateway) as gateway_url:
        generator = NewApiVideoGenerator(
            api_key="test-key", endpoint=gateway_url + "/v1", model="video-model"
        )
        receipt = await generator._download_video(
            gateway_url + "/v1/videos/task-1/content", str(output_path)
        )

    assert authorization_headers == ["Bearer test-key"]
    assert output_path.read_bytes() == b"protected-video"
    assert receipt.size == len(b"protected-video")


async def test_video_download_to_other_port_does_not_send_gateway_authorization(
    tmp_path: Path,
) -> None:
    authorization_headers: list[str | None] = []

    async def content(request: web.Request) -> web.Response:
        authorization_headers.append(request.headers.get("Authorization"))
        return web.Response(body=b"external-video", content_type="video/mp4")

    external = web.Application()
    external.router.add_get("/video.mp4", content)
    output_path = tmp_path / "result.mp4"

    async with _serve(web.Application()) as gateway_url, _serve(external) as external_url:
        generator = NewApiVideoGenerator(
            api_key="test-key", endpoint=gateway_url + "/v1", model="video-model"
        )
        await generator._download_video(external_url + "/video.mp4", str(output_path))

    assert authorization_headers == [None]
    assert output_path.read_bytes() == b"external-video"


async def test_same_origin_video_redirect_does_not_leak_gateway_authorization(
    tmp_path: Path,
) -> None:
    gateway_authorization_headers: list[str | None] = []
    external_authorization_headers: list[str | None] = []

    async def external_content(request: web.Request) -> web.Response:
        external_authorization_headers.append(request.headers.get("Authorization"))
        return web.Response(body=b"redirected-video", content_type="video/mp4")

    external = web.Application()
    external.router.add_get("/video.mp4", external_content)
    output_path = tmp_path / "result.mp4"

    async with _serve(external) as external_url:

        async def redirect(request: web.Request) -> web.Response:
            gateway_authorization_headers.append(request.headers.get("Authorization"))
            if request.headers.get("Authorization") != "Bearer test-key":
                raise web.HTTPUnauthorized()
            raise web.HTTPFound(external_url + "/video.mp4")

        gateway = web.Application()
        gateway.router.add_get("/v1/videos/task-1/content", redirect)
        async with _serve(gateway) as gateway_url:
            generator = NewApiVideoGenerator(
                api_key="test-key", endpoint=gateway_url + "/v1", model="video-model"
            )
            await generator._download_video(
                gateway_url + "/v1/videos/task-1/content", str(output_path)
            )

    assert gateway_authorization_headers == ["Bearer test-key"]
    assert external_authorization_headers == [None]
    assert output_path.read_bytes() == b"redirected-video"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.2/video.mp4",
        "https://10.1.2.3/video.mp4",
        "http://169.254.169.254/video.mp4",
        "https://[::ffff:127.0.0.1]/video.mp4",
        "http://１２７.０.０.１/video.mp4",
        "http://①②⑦.⓪.⓪.①/video.mp4",
        "https://user:password@cdn.example/video.mp4",
    ],
)
async def test_public_only_download_rejects_private_url(tmp_path: Path, url: str) -> None:
    with pytest.raises(VideoDeliveryError, match="VIDEO_DOWNLOAD_URL_INVALID"):
        await download_to_path(
            url,
            str(tmp_path / "result.mp4"),
            public_only=True,
        )


async def test_public_resolver_rejects_private_dns_result(monkeypatch) -> None:
    class FakeLoop:
        async def getaddrinfo(self, host, port, *, type, family):
            return [
                (
                    2,
                    type,
                    6,
                    "",
                    ("10.10.1.2", port),
                )
            ]

    monkeypatch.setattr(
        "novelvideo.utils.media_download.asyncio.get_running_loop", lambda: FakeLoop()
    )
    with pytest.raises(VideoDeliveryError, match="VIDEO_DOWNLOAD_URL_INVALID"):
        await _PublicResolver().resolve("video.example", 443)


@pytest.mark.parametrize("redirect_host", ["127.0.0.1", "１２７.０.０.１"])
async def test_public_only_download_rejects_private_redirect(
    tmp_path: Path, monkeypatch, redirect_host: str
) -> None:
    content_requests = []

    async def content(request: web.Request) -> web.Response:
        content_requests.append(request.path)
        return web.Response(body=b"private-content")

    app = web.Application()
    app.router.add_get("/private", content)
    async with _serve(app) as local_url:
        async def resolve(self, host, port=0, family=0):
            return [
                {
                    "hostname": host,
                    "host": "127.0.0.1",
                    "port": port,
                    "family": 2,
                    "proto": 6,
                    "flags": 0,
                }
            ]

        monkeypatch.setattr(_PublicResolver, "resolve", resolve)

        async def redirect(request: web.Request) -> web.Response:
            target = local_url.replace("127.0.0.1", redirect_host) + "/private"
            return web.Response(status=302, headers={"Location": target})

        # Resolve a simulated public CDN to the test server; only the redirect
        # target should be rejected, before it can reach the private handler.
        redirect_app = web.Application()
        redirect_app.router.add_get("/video", redirect)
        async with _serve(redirect_app) as redirect_url:
            redirect_port = int(redirect_url.rsplit(":", 1)[1])
            with pytest.raises(VideoDeliveryError, match="VIDEO_DOWNLOAD_URL_INVALID"):
                await download_to_path(
                    f"http://cdn.example:{redirect_port}/video",
                    str(tmp_path / "result.mp4"),
                    public_only=True,
                )

    assert content_requests == []
    assert not (tmp_path / "result.mp4").exists()
