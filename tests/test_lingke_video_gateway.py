"""Tests for Lingke video generation (/v1/media/generate + /v1/skills/task-status)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from novelvideo import model_gateway_settings
from novelvideo.generators import video_generator as video_module
from novelvideo.generators.lingke_media import (
    build_lingke_video_payload,
    lingke_task_outcome,
)
from novelvideo.generators.video_generator import (
    NewApiVideoGenerator,
    ShotReference,
    VideoGenStatus,
)


async def test_lingke_hailuo_cankaosheng_uses_media_generate_and_polls_task_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit_requests: list[tuple[str, dict, str]] = []
    poll_requests: list[tuple[str, str]] = []
    poll_count = 0

    async def submit(request: web.Request) -> web.Response:
        submit_requests.append(
            (
                request.path_qs,
                await request.json(),
                request.headers.get("Authorization", ""),
            )
        )
        return web.json_response(
            {
                "code": 200,
                "msg": "任务创建成功",
                "data": {"task_id": 123456},
            }
        )

    async def poll(request: web.Request) -> web.Response:
        nonlocal poll_count
        poll_count += 1
        poll_requests.append(
            (request.path_qs, request.headers.get("Authorization", ""))
        )
        if poll_count == 1:
            return web.json_response(
                {
                    "task_id": 123456,
                    "state": "running",
                    "status": "处理中",
                    "status_group": "进行中",
                    "is_final": False,
                    "progress": "45%",
                    "result_url": "",
                    "result_type": "",
                    "error": "",
                }
            )
        return web.json_response(
            {
                "task_id": 123456,
                "state": "success",
                "status": "已完成",
                "status_group": "已完成",
                "is_final": True,
                "progress": "100%",
                "result_url": "https://cdn.example.com/hailuo.mp4",
                "result_type": "video",
                "error": "",
            }
        )

    app = web.Application()
    app.router.add_post("/v1/media/generate", submit)
    app.router.add_get("/v1/skills/task-status", poll)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    async def reserve(*_args, **_kwargs) -> str:
        return "reservation-lingke-video"

    confirmed: list[dict] = []
    refunded: list[dict] = []

    async def confirm(*_args, **kwargs) -> None:
        confirmed.append(kwargs)

    async def refund(*_args, **kwargs) -> None:
        refunded.append(kwargs)

    downloaded: list[str] = []

    async def download(url: str, output_path: str, **_kwargs) -> bytes:
        downloaded.append(url)
        Path(output_path).write_bytes(b"lingke-video")
        return b"lingke-video"

    def upload(data: bytes, **_kwargs) -> str:
        return {
            b"ref-a": "https://relay.example/ref-a.jpg",
            b"ref-b": "https://relay.example/ref-b.jpg",
        }[data]

    monkeypatch.setattr(video_module, "upload_media_bytes", upload)
    monkeypatch.setattr(video_module, "_reserve_video_model_call", reserve)
    monkeypatch.setattr(video_module, "_confirm_video_model_call", confirm)
    monkeypatch.setattr(video_module, "_refund_video_model_call", refund)
    monkeypatch.setattr(video_module, "get_video_result_delivery", lambda: None)
    monkeypatch.setattr(
        model_gateway_settings,
        "resolve_direct_media_gateway_target",
        lambda *_args: SimpleNamespace(
            api_key="lingke-secret-key",
            base_url=f"http://127.0.0.1:{port}/v1",
            upstream_model="hailuo-h3-cankaosheng",
            provider="lingke",
        ),
    )

    ref_a = tmp_path / "ref-a.png"
    ref_b = tmp_path / "ref-b.png"
    ref_a.write_bytes(b"ref-a")
    ref_b.write_bytes(b"ref-b")

    generator = NewApiVideoGenerator(
        model="hailuo-h3-cankaosheng",
        resolution="768p",
        generate_audio=True,
    )
    monkeypatch.setattr(generator, "_download_video", download)

    try:
        result = await generator.generate(
            image_path=str(ref_a),
            prompt="参考图中的角色在雪地中奔跑，电影级运镜",
            output_path=str(tmp_path / "output.mp4"),
            aspect_ratio="adaptive",
            duration=5.0,
            references=[
                ShotReference("image", str(ref_a), "参考图1"),
                ShotReference("image", str(ref_b), "参考图2"),
            ],
            gen_mode="imageReference",
            poll_interval=0,
            max_polls=4,
        )
    finally:
        await runner.cleanup()

    assert result.status == VideoGenStatus.DONE
    assert result.task_id == "123456"
    assert downloaded == ["https://cdn.example.com/hailuo.mp4"]
    assert len(confirmed) == 1
    assert not refunded
    assert len(submit_requests) == 1
    assert submit_requests[0][0] == "/v1/media/generate"
    assert submit_requests[0][2] == "Bearer lingke-secret-key"
    assert submit_requests[0][1] == {
        "model": "hailuo-h3-cankaosheng",
        "prompt": "参考图中的角色在雪地中奔跑，电影级运镜",
        "params": {
            "duration": "5",
            "resolution": "768P",
            "aspect_ratio": "adaptive",
            "images": [
                "https://relay.example/ref-a.jpg",
                "https://relay.example/ref-b.jpg",
            ],
        },
    }
    assert poll_requests == [
        ("/v1/skills/task-status?task_id=123456", "Bearer lingke-secret-key"),
        ("/v1/skills/task-status?task_id=123456", "Bearer lingke-secret-key"),
    ]


async def test_lingke_submit_business_error_refunds_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def submit(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "code": 500,
                "msg": "任务创建失败",
                "data": {"失败数量": 1, "失败原因": ["余额不足"]},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/media/generate", submit)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    refunded: list[dict] = []

    async def reserve(*_args, **_kwargs) -> str:
        return "reservation-lingke-fail"

    async def refund(*_args, **kwargs) -> None:
        refunded.append(kwargs)

    monkeypatch.setattr(video_module, "_reserve_video_model_call", reserve)
    monkeypatch.setattr(video_module, "_refund_video_model_call", refund)
    monkeypatch.setattr(video_module, "get_video_result_delivery", lambda: None)
    monkeypatch.setattr(
        model_gateway_settings,
        "resolve_direct_media_gateway_target",
        lambda *_args: SimpleNamespace(
            api_key="lingke-secret-key",
            base_url=f"http://127.0.0.1:{port}",
            upstream_model="hailuo-h3",
            provider="lingke",
        ),
    )

    generator = NewApiVideoGenerator(model="hailuo-h3", resolution="1080p")
    try:
        result = await generator.generate(
            image_path=None,
            prompt="snow mountain",
            output_path=str(tmp_path / "out.mp4"),
            aspect_ratio="16:9",
            duration=5.0,
            gen_mode="textToVideo",
            poll_interval=0,
            max_polls=2,
        )
    finally:
        await runner.cleanup()

    assert result.status == VideoGenStatus.FAILED
    assert "余额不足" in (result.error or "")
    assert len(refunded) == 1


async def test_lingke_failed_task_refunds_and_returns_provider_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def submit(_request: web.Request) -> web.Response:
        return web.json_response({"code": 200, "data": {"task_id": "lk-99"}})

    async def poll(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "task_id": "lk-99",
                "state": "failed",
                "status": "失败",
                "is_final": True,
                "error": "sensitive content detected",
            }
        )

    app = web.Application()
    app.router.add_post("/v1/media/generate", submit)
    app.router.add_get("/v1/skills/task-status", poll)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    refunded: list[dict] = []

    async def reserve(*_args, **_kwargs) -> str:
        return "reservation-lk-99"

    async def refund(*_args, **kwargs) -> None:
        refunded.append(kwargs)

    monkeypatch.setattr(video_module, "_reserve_video_model_call", reserve)
    monkeypatch.setattr(video_module, "_refund_video_model_call", refund)
    monkeypatch.setattr(video_module, "get_video_result_delivery", lambda: None)
    monkeypatch.setattr(
        model_gateway_settings,
        "resolve_direct_media_gateway_target",
        lambda *_args: SimpleNamespace(
            api_key="lingke-secret-key",
            base_url=f"http://127.0.0.1:{port}/v1",
            upstream_model="hailuo-h3",
            provider="lingke",
        ),
    )

    generator = NewApiVideoGenerator(model="hailuo-h3", resolution="768p")
    try:
        result = await generator.generate(
            image_path=None,
            prompt="test prompt",
            output_path=str(tmp_path / "out.mp4"),
            aspect_ratio="16:9",
            duration=5.0,
            gen_mode="textToVideo",
            poll_interval=0,
            max_polls=2,
        )
    finally:
        await runner.cleanup()

    assert result.status == VideoGenStatus.FAILED
    assert result.error == "sensitive content detected"
    assert len(refunded) == 1


def test_build_lingke_video_payload_covers_model_families_and_custom_params() -> None:
    dual_keyframes = build_lingke_video_payload(
        {
            "model": "minimax-h3",
            "prompt": "camera dolly",
            "duration": 6,
            "image": "https://cdn.example/first.png",
            "metadata": {
                "resolution": "1080p",
                "ratio": "auto",
                "last_frame_image": "https://cdn.example/last.png",
            },
        },
        mode="firstLastFrame",
    )
    assert dual_keyframes == {
        "model": "minimax-h3",
        "prompt": "camera dolly",
        "params": {
            "duration": "6",
            "resolution": "1080P",
            "aspect_ratio": "adaptive",
            "mode": "shouweizhen",
            "images": [
                "https://cdn.example/first.png",
                "https://cdn.example/last.png",
            ],
        },
    }

    dual_reference = build_lingke_video_payload(
        {
            "model": "seedance-2.5-guanfang",
            "prompt": "edit subject",
            "duration": "auto",
            "images": ["https://cdn.example/ref.png"],
            "audios": ["https://cdn.example/ref.mp3"],
            "metadata": {
                "resolution": "720p",
                "ratio": "16:9",
                "reference_videos": ["https://cdn.example/clip.mp4"],
            },
        },
        mode="videoEdit",
    )
    assert dual_reference == {
        "model": "seedance-2.5-guanfang",
        "prompt": "edit subject",
        "params": {
            "duration": "auto",
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "mode": "cankaosheng",
            "_quan_neng_mode": "edit",
            "image_url": ["https://cdn.example/ref.png"],
            "video_url": ["https://cdn.example/clip.mp4"],
            "audio_url": ["https://cdn.example/ref.mp3"],
        },
    }

    wan_ref = build_lingke_video_payload(
        {
            "model": "wan3.0-video-cankaosheng",
            "prompt": "present slides",
            "duration": 5,
            "images": ["https://cdn.example/slide.png"],
            "metadata": {
                "resolution": "1080p",
                "ratio": "auto",
                "reference_file": "https://cdn.example/deck.pdf",
            },
        },
        mode="allReference",
    )
    assert wan_ref == {
        "model": "wan3.0-video-cankaosheng",
        "prompt": "present slides",
        "params": {
            "duration": "5",
            "resolution": "1080P",
            "ratio": "adaptive",
            "reference_urls": ["https://cdn.example/slide.png"],
            "file_url": "https://cdn.example/deck.pdf",
        },
    }

    vidu_custom = build_lingke_video_payload(
        {
            "model": "viduq3",
            "prompt": "dog on beach",
            "duration": 4,
            "model_variant": "turbo",
            "notify_url": "https://hook.example/done",
            "metadata": {
                "resolution": "720p",
                "ratio": "auto",
                "off_peak": True,
            },
        },
        request_schema={
            "endpoint": "video/generations",
            "parameters": [
                {
                    "key": "model_variant",
                    "requestPath": "model_variant",
                    "control": "select",
                    "options": ["turbo", "pro"],
                },
                {
                    "key": "off_peak",
                    "requestPath": "metadata.off_peak",
                    "control": "switch",
                },
                {
                    "key": "notify_url",
                    "requestPath": "notify_url",
                    "control": "text",
                },
            ],
        },
    )
    assert vidu_custom == {
        "model": "viduq3",
        "prompt": "dog on beach",
        "notify_url": "https://hook.example/done",
        "params": {
            "duration": "4",
            "resolution": "720p",
            "model_variant": "turbo",
            "off_peak": True,
        },
    }


def test_lingke_task_outcome_handles_localized_status_and_is_final() -> None:
    assert lingke_task_outcome({"state": "running", "status": "处理中", "is_final": False}) == (
        "pending",
        "running",
    )
    assert lingke_task_outcome({"state": "success", "status": "已完成", "is_final": True}) == (
        "completed",
        "",
    )
    assert lingke_task_outcome(
        {
            "state": "failed",
            "status": "失败",
            "is_final": True,
            "error": "quota exceeded",
        }
    ) == ("failed", "quota exceeded")
