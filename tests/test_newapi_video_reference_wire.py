"""Reference images must reach generic gateways as well as native adapters."""

from pathlib import Path

import pytest
from aiohttp import web

from novelvideo.generators import video_generator as video_module
from novelvideo.generators.video_generator import (
    NewApiVideoGenerator,
    ShotReference,
    VideoGenStatus,
)


@pytest.mark.parametrize("model", ["seedance-2.0", "seedance-2.0-fast"])
@pytest.mark.parametrize("gen_mode", [None, "all_reference"])
async def test_seedance_reference_images_reach_generic_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str, gen_mode: str | None
) -> None:
    """Catch model-specific metadata hiding images from an OpenAI relay channel."""
    requests: list[dict] = []

    async def submit(request: web.Request) -> web.Response:
        requests.append(await request.json())
        return web.json_response({"id": "task-reference-wire"})

    async def poll(_request: web.Request) -> web.Response:
        return web.json_response(
            {"status": "completed", "url": "https://media.example/result.mp4"}
        )

    app = web.Application()
    app.router.add_post("/video/generations", submit)
    app.router.add_get("/video/generations/task-reference-wire", poll)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    async def reserve(*_args, **_kwargs) -> str:
        return "reservation-reference-wire"

    async def settle(*_args, **_kwargs) -> None:
        pass

    async def download(_url: str, output_path: str) -> bytes:
        Path(output_path).write_bytes(b"video")
        return b"video"

    def upload(data: bytes, **_kwargs) -> str:
        return {
            b"storyboard": "https://media.example/storyboard.jpg",
            b"scene": "https://media.example/scene.jpg",
        }[data]

    monkeypatch.setattr(video_module, "upload_media_bytes", upload)
    monkeypatch.setattr(video_module, "_reserve_video_model_call", reserve)
    monkeypatch.setattr(video_module, "_confirm_video_model_call", settle)
    monkeypatch.setattr(video_module, "_refund_video_model_call", settle)
    monkeypatch.setattr(video_module, "get_video_result_delivery", lambda: None)
    generator = NewApiVideoGenerator(
        api_key="test-key",
        endpoint=f"http://127.0.0.1:{port}",
        model=model,
        resolution="720p",
        generate_audio=False,
    )
    monkeypatch.setattr(generator, "_download_video", download)
    storyboard = tmp_path / "storyboard.png"
    scene = tmp_path / "scene.png"
    storyboard.write_bytes(b"storyboard")
    scene.write_bytes(b"scene")

    try:
        result = await generator.generate(
            image_path=None,
            prompt="参考@图片1的构图和@图片2的场景。",
            output_path=str(tmp_path / "result.mp4"),
            duration=4,
            aspect_ratio="16:9",
            references=[
                ShotReference("image", str(storyboard), "图片1"),
                ShotReference("image", str(scene), "图片2"),
            ],
            seedance2_config={
                "mode": "multimodal_reference",
                "generate_audio": False,
                "generate_audio_user_set": True,
            },
            gen_mode=gen_mode,
            poll_interval=0,
            max_polls=1,
        )
    finally:
        await runner.cleanup()

    assert result.status == VideoGenStatus.DONE
    assert len(requests) == 1
    payload = requests[0]
    # Generic NewAPI channels inspect images, not metadata.reference_images.
    assert payload.get("images") == [
        "https://media.example/storyboard.jpg",
        "https://media.example/scene.jpg",
    ]
    # Native Seedance adapters still consume the model-specific media fields.
    assert payload["metadata"]["reference_images"] == [
        "https://media.example/storyboard.jpg",
        "https://media.example/scene.jpg",
    ]
    # A multimodal reference must not accidentally become a locked first frame.
    assert "image" not in payload
