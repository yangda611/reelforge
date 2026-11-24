"""Reference images and audio must stay separate on the NewAPI wire."""

import hashlib
import json
from pathlib import Path

import pytest
from aiohttp import connector, resolver, web

from novelvideo.generators import video_generator as video_module
from novelvideo.generators.video_generator import (
    NewApiVideoGenerator,
    ShotReference,
    VideoGenStatus,
)


@pytest.mark.parametrize(
    "model", ["seedance-2.0", "seedance-2.0-fast", "seedance-2.5", "YK-SD2.5-2"]
)
@pytest.mark.parametrize("gen_mode", [None, "all_reference"])
async def test_seedance_reference_media_reaches_gateway_in_separate_arrays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    model: str,
    gen_mode: str | None,
) -> None:
    requests: list[dict] = []
    task_logs: list[str] = []
    logs_at_submit: list[str] = []
    voice_url = "https://media.example/voice.mp3?signature=private-value"
    prompt = "参考@图片1的构图、@图片2的场景和@音频1的声线。"
    caplog.set_level("INFO", logger=video_module.__name__)

    async def submit(request: web.Request) -> web.Response:
        logs_at_submit.extend(task_logs)
        requests.append(await request.json())
        return web.json_response(
            {"id": "task-reference-wire"},
            headers={"x-request-id": "request-reference-wire"},
        )

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
            b"voice": voice_url,
        }[data]

    monkeypatch.setattr(video_module, "upload_media_bytes", upload)
    monkeypatch.setattr(video_module, "_reserve_video_model_call", reserve)
    monkeypatch.setattr(video_module, "_confirm_video_model_call", settle)
    monkeypatch.setattr(video_module, "_refund_video_model_call", settle)
    monkeypatch.setattr(video_module, "get_video_result_delivery", lambda: None)
    generator = NewApiVideoGenerator(
        api_key="private-api-key-value",
        endpoint=f"http://127.0.0.1:{port}",
        model=model,
        resolution="720p",
        generate_audio=True,
    )
    monkeypatch.setattr(generator, "_download_video", download)
    storyboard = tmp_path / "storyboard.png"
    scene = tmp_path / "scene.png"
    voice = tmp_path / "voice.mp3"
    storyboard.write_bytes(b"storyboard")
    scene.write_bytes(b"scene")
    voice.write_bytes(b"voice")

    try:
        result = await generator.generate(
            image_path=None,
            prompt=prompt,
            output_path=str(tmp_path / "result.mp4"),
            duration=4,
            aspect_ratio="16:9",
            references=[
                ShotReference("image", str(storyboard), "图片1"),
                ShotReference("audio", str(voice), "音频1"),
                ShotReference("image", str(scene), "图片2"),
            ],
            seedance2_config={
                "mode": "multimodal_reference",
                "generate_audio": True,
                "generate_audio_user_set": True,
            },
            gen_mode=gen_mode,
            on_log=lambda message: task_logs.append(str(message)),
            poll_interval=0,
            max_polls=1,
        )
    finally:
        await runner.cleanup()

    assert result.status == VideoGenStatus.DONE
    assert len(requests) == 1
    payload = requests[0]
    # Generic NewAPI channels and native adapters consume different fields.
    assert payload.get("images") == [
        "https://media.example/storyboard.jpg",
        "https://media.example/scene.jpg",
    ]
    assert payload.get("audios") == [voice_url]
    assert payload["metadata"]["reference_images"] == [
        "https://media.example/storyboard.jpg",
        "https://media.example/scene.jpg",
    ]
    assert payload["metadata"]["reference_audios"] == [voice_url]
    # A multimodal reference must not accidentally become a locked first frame.
    assert "image" not in payload

    def fingerprint(url: str, suffix: str) -> str:
        return f"{suffix}#{hashlib.sha256(url.encode()).hexdigest()[:12]}"

    def read_event(lines: list[str], event: str) -> dict:
        (line,) = [line for line in lines if line.startswith(f"{event} ")]
        return json.loads(line.split(" ", 1)[1])

    outbound = read_event(logs_at_submit, "video_submit_request")
    accepted = read_event(task_logs, "video_submit_accepted")
    assert outbound["model"] == payload["model"]
    assert outbound["gateway"] == "127.0.0.1"
    assert outbound["route"] == "/video/generations"
    assert outbound["media"] == {
        "images": [fingerprint(url, ".jpg") for url in payload["images"]],
        "audios": [fingerprint(voice_url, ".mp3")],
        "metadata": {
            "reference_images": [
                fingerprint(url, ".jpg") for url in payload["images"]
            ],
            "reference_audios": [fingerprint(voice_url, ".mp3")],
        },
    }
    assert accepted["trace_id"] == outbound["trace_id"]
    assert accepted["task_id"] == "task-reference-wire"
    assert accepted["request_id"] == "request-reference-wire"
    assert read_event(caplog.messages, "video_submit_request") == outbound
    assert read_event(caplog.messages, "video_submit_accepted") == accepted
    diagnostic_logs = "\n".join(
        line
        for line in task_logs + caplog.messages
        if line.startswith("video_submit_")
    )
    for private_value in ("private-value", "private-api-key-value", voice_url, prompt):
        assert private_value not in diagnostic_logs


@pytest.mark.parametrize("base_path", ["", "/v1"])
@pytest.mark.parametrize("gen_mode", [None, "all_reference"])
async def test_zeroshell_yk_uses_native_video_route_with_separate_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_path: str,
    gen_mode: str | None,
) -> None:
    requests: list[tuple[str, dict]] = []
    polls: list[str] = []
    task_logs: list[str] = []
    picture_urls = [
        f"https://media.example/image/upload/picture-{index}.jpg"
        for index in range(1, 9)
    ]
    misplaced_audio = "https://media.example/video/upload/voice-1.MP3?signature=test"
    typed_audio = "https://media.example/video/upload/voice-2.mp3"
    video = "https://media.example/video/upload/clip.mp4"

    class LocalResolver(resolver.ThreadedResolver):
        async def resolve(self, host, port=0, family=0):
            assert host == "zeroshell.top"
            return await super().resolve("127.0.0.1", port, family)

    async def submit(request: web.Request) -> web.Response:
        requests.append((request.path, await request.json()))
        return web.json_response({"id": "task-zeroshell"})

    async def poll(request: web.Request) -> web.Response:
        polls.append(request.path)
        return web.json_response(
            {
                "status": "completed",
                "metadata": {"video_url": "https://media.example/result.mp4"},
            }
        )

    async def reserve(*_args, **_kwargs) -> str:
        return "reservation-zeroshell"

    async def settle(*_args, **_kwargs) -> None:
        pass

    async def download(_url: str, _output_path: str) -> bytes:
        return b"video"

    monkeypatch.setattr(connector, "DefaultResolver", LocalResolver)
    monkeypatch.setattr(video_module, "_reserve_video_model_call", reserve)
    monkeypatch.setattr(video_module, "_confirm_video_model_call", settle)
    monkeypatch.setattr(video_module, "_refund_video_model_call", settle)
    monkeypatch.setattr(video_module, "get_video_result_delivery", lambda: None)
    app = web.Application()
    for route in ("/v1/videos", f"{base_path}/video/generations"):
        app.router.add_post(route, submit)
        app.router.add_get(f"{route}/task-zeroshell", poll)

    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        generator = NewApiVideoGenerator(
            api_key="test-key",
            endpoint=f"http://zeroshell.top:{port}{base_path}",
            model="YK-SD2.5-2",
            resolution="720p",
        )
        monkeypatch.setattr(generator, "_download_video", download)
        result = await generator.generate(
            image_path=picture_urls[0],
            prompt="Use eight image references, two audio references and one video.",
            output_path=str(tmp_path / "result.mp4"),
            duration=17,
            gen_mode=gen_mode,
            references=[
                *(ShotReference("image", url, "") for url in picture_urls),
                ShotReference("image", misplaced_audio, ""),
                ShotReference("audio", typed_audio, ""),
                ShotReference("video", video, ""),
            ],
            on_log=lambda message: task_logs.append(str(message)),
            poll_interval=0,
            max_polls=1,
        )
    finally:
        await runner.cleanup()

    assert result.status == VideoGenStatus.DONE
    assert polls == ["/v1/videos/task-zeroshell"]
    assert requests == [
        (
            "/v1/videos",
            {
                "model": "YK-SD2.5-2",
                "prompt": "Use eight image references, two audio references and one video.",
                "seconds": "17",
                "images": picture_urls,
                "audios": [misplaced_audio, typed_audio],
                "videos": [video],
            },
        )
    ]
    (submit_log,) = [
        log for log in task_logs if log.startswith("video_submit_request ")
    ]
    log_payload = json.loads(submit_log.split(" ", 1)[1])
    assert log_payload["route"] == "/v1/videos"
    assert [entry.split("#")[0] for entry in log_payload["media"]["audios"]] == [
        ".mp3", ".mp3"
    ]


@pytest.mark.parametrize(
    "model", ["seedance-2.0", "seedance-2.5", "seedance2.5-mref-720p"]
)
@pytest.mark.parametrize("gen_mode", [None, "all_reference"])
async def test_generic_newapi_models_reclassify_misplaced_audio_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    gen_mode: str | None,
) -> None:
    requests: list[dict] = []
    image_url = "https://media.example/image/upload/scene.jpg?token=img"
    misplaced_mp3 = "https://media.example/video/upload/voice-1.MP3?signature=sig1"
    misplaced_m4a = "https://media.example/video/upload/voice-2.m4a?signature=sig2"
    typed_audio = "https://media.example/video/upload/voice-3.mp3"

    async def submit(request: web.Request) -> web.Response:
        requests.append(await request.json())
        return web.json_response({"id": "task-misplaced-generic"})

    async def poll(_request: web.Request) -> web.Response:
        return web.json_response(
            {"status": "completed", "url": "https://media.example/result.mp4"}
        )

    app = web.Application()
    app.router.add_post("/video/generations", submit)
    app.router.add_get("/video/generations/task-misplaced-generic", poll)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    async def reserve(*_args, **_kwargs) -> str:
        return "reservation-misplaced"

    async def settle(*_args, **_kwargs) -> None:
        pass

    async def download(_url: str, output_path: str) -> bytes:
        Path(output_path).write_bytes(b"video")
        return b"video"

    monkeypatch.setattr(video_module, "_reserve_video_model_call", reserve)
    monkeypatch.setattr(video_module, "_confirm_video_model_call", settle)
    monkeypatch.setattr(video_module, "_refund_video_model_call", settle)
    monkeypatch.setattr(video_module, "get_video_result_delivery", lambda: None)

    generator = NewApiVideoGenerator(
        api_key="test-key",
        endpoint=f"http://127.0.0.1:{port}",
        model=model,
        resolution="720p",
        generate_audio=True,
    )
    monkeypatch.setattr(generator, "_download_video", download)
    try:
        result = await generator.generate(
            image_path=None,
            prompt="参考图片1、音频1、音频2和音频3生成视频。",
            output_path=str(tmp_path / "result.mp4"),
            duration=5,
            aspect_ratio="16:9",
            references=[
                ShotReference("image", image_url, "图片1"),
                ShotReference("image", misplaced_mp3, "图片2"),
                ShotReference("image", misplaced_m4a, "图片3"),
                ShotReference("audio", typed_audio, "音频1"),
            ],
            seedance2_config={
                "mode": "multimodal_reference",
                "generate_audio": True,
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
    assert payload.get("images") == [image_url]
    assert payload.get("audios") == [misplaced_mp3, misplaced_m4a, typed_audio]
    assert payload["metadata"]["reference_images"] == [image_url]
    assert payload["metadata"]["reference_audios"] == [
        misplaced_mp3,
        misplaced_m4a,
        typed_audio,
    ]


def test_separate_reference_paths_handles_signed_urls_and_audio_extensions(
    tmp_path: Path,
) -> None:
    from novelvideo.seedance2_i2v.assets import resolve_beat_video_references
    from novelvideo.seedance2_i2v.models import parse_seedance2_config

    signed_mp3 = "https://res.cloudinary.com/demo/video/upload/v1/voice.mp3?sig=1"
    signed_webp = "https://res.cloudinary.com/demo/image/upload/v1/frame.webp?sig=2"
    cfg = parse_seedance2_config(
        {
            "reference_image_paths": [
                signed_webp,
                signed_mp3,
                "assets/voice.m4a",
                "assets/voice.opus",
            ],
            "reference_audio_paths": ["assets/voice.mp3"],
        }
    )
    assert cfg.reference_image_paths == [signed_webp]
    assert cfg.reference_audio_paths == [
        "assets/voice.mp3",
        signed_mp3,
        "assets/voice.m4a",
        "assets/voice.opus",
    ]

    img = tmp_path / "scene.png"
    audio = tmp_path / "voice.m4a"
    img.write_bytes(b"png")
    audio.write_bytes(b"audio")
    resolved, declared, missing = resolve_beat_video_references(
        [str(audio), str(img)], []
    )
    assert declared == 2
    assert missing == []
    assert [(a.media_type, str(a.path)) for a in resolved] == [
        ("image", str(img)),
        ("audio", str(audio)),
    ]


def test_lingke_payload_separates_misplaced_audio_from_images() -> None:
    from novelvideo.generators.lingke_media import build_lingke_video_payload

    payload = build_lingke_video_payload(
        {
            "model": "seedance-2.5-guanfang",
            "prompt": "test",
            "duration": 5,
            "images": [
                "https://media.example/a.jpg",
                "https://media.example/voice.mp3?sig=1",
            ],
            "audios": ["https://media.example/b.png"],
        },
        mode="all_reference",
    )
    assert payload["params"]["image_url"] == [
        "https://media.example/a.jpg",
        "https://media.example/b.png",
    ]
    assert payload["params"]["audio_url"] == [
        "https://media.example/voice.mp3?sig=1"
    ]
