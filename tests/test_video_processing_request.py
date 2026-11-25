from pathlib import Path

import pytest
from pydantic import ValidationError


def test_video_enhance_target_fps_accepts_common_fractional_rates() -> None:
    from novelvideo.api.schemas import FreezoneVideoUpscaleRequest

    for fps in (23.976, 29.97, 59.94, 119.88):
        body = FreezoneVideoUpscaleRequest(source_url="/static/source.mp4", target_fps=fps)
        assert body.target_fps == fps
    for fps in (0, 120.001, 23.9765):
        with pytest.raises(ValidationError):
            FreezoneVideoUpscaleRequest(source_url="/static/source.mp4", target_fps=fps)


def test_video_enhance_accepts_slowdown_through_five_times() -> None:
    from novelvideo.api.schemas import FreezoneVideoUpscaleRequest

    for slowdown in ("auto", "2x", "3x", "4x", "5x"):
        body = FreezoneVideoUpscaleRequest(source_url="/static/source.mp4", slowdown=slowdown)
        assert body.slowdown == slowdown
    with pytest.raises(ValidationError):
        FreezoneVideoUpscaleRequest(source_url="/static/source.mp4", slowdown="6x")


def test_video_enhance_quote_carries_trusted_frame_rate_inputs() -> None:
    from novelvideo.api.routes.freezone import _video_enhance_billing
    from novelvideo.api.schemas import FreezoneVideoUpscaleRequest

    body = FreezoneVideoUpscaleRequest(
        source_url="/static/source.mp4", resolution="1080p", slowdown="2x",
    )
    billing = _video_enhance_billing(
        body, {"duration": 10.2, "fps": 23.976},
        {"catalog_id": "upscale-id"}, {"catalog_id": "frame-id"},
    )
    assert billing["feature_key"] == "freezone.video_enhance"
    assert billing["pricing_stages"][0]["duration_seconds"] == 11
    assert billing["pricing_stages"][1] == {
        "mode": "video_frame_rate", "catalog_id": "frame-id",
        "resolution": "1080p", "duration_seconds": 21,
        "source_fps": 23.976, "target_fps": 23.976, "slowdown": "2x",
        "smart_interpolation": True,
    }

    body.smart_interpolation = False
    billing = _video_enhance_billing(
        body, {"duration": 10.2, "fps": 23.976},
        {"catalog_id": "upscale-id"}, {"catalog_id": "frame-id"},
    )
    assert billing["pricing_stages"][1]["smart_interpolation"] is False

    body.target_fps = 59.94
    billing = _video_enhance_billing(
        body, {"duration": 10.2, "fps": 23.976},
        {"catalog_id": "upscale-id"}, {"catalog_id": "frame-id"},
    )
    assert billing["pricing_stages"][1]["target_fps"] == 59.94

    body.slowdown = "5x"
    billing = _video_enhance_billing(
        body, {"duration": 10.2, "fps": 23.976},
        {"catalog_id": "upscale-id"}, {"catalog_id": "frame-id"},
    )
    assert billing["pricing_stages"][1]["duration_seconds"] == 51
    assert billing["pricing_stages"][1]["slowdown"] == "5x"


def test_resolve_beat_video_references_returns_images_audio_and_missing(
    tmp_path: Path,
) -> None:
    from novelvideo.seedance2_i2v.assets import resolve_beat_video_references

    image_a = tmp_path / "a.png"
    image_b = tmp_path / "b.png"
    audio = tmp_path / "voice.wav"
    for path in (image_a, image_b, audio):
        path.write_bytes(b"media")

    references, declared, missing = resolve_beat_video_references(
        [str(image_a), str(image_b)], [str(audio)]
    )
    assert declared == 3
    assert missing == []
    assert [(item.media_type, item.reference_label) for item in references] == [
        ("image", "图片1"),
        ("image", "图片2"),
        ("audio", "音频1"),
    ]

    _references, declared, missing = resolve_beat_video_references(
        [str(tmp_path / "gone-a.png"), str(tmp_path / "gone-b.png")], []
    )
    assert declared == 2
    assert len(missing) == 2


@pytest.mark.asyncio
async def test_all_reference_mode_keeps_first_frame_and_reference_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novelvideo.generators.video_generator import NewApiVideoGenerator

    generator = NewApiVideoGenerator(
        api_key="test-key",
        endpoint="https://newapi.example/v1",
        model="ntt-720p",
        resolution="720p",
        generate_audio=False,
    )
    relayed: list[str] = []

    async def fake_relay(path: str, **_kwargs: object) -> str:
        relayed.append(path)
        return f"https://relay.example/{Path(path).name}"

    monkeypatch.setattr(generator, "_relay_frame_input", fake_relay)
    metadata: dict[str, object] = {}
    await generator._apply_huimeng_protocol_media_inputs(
        metadata,
        mode="all_reference",
        image_path="/tmp/first.png",
        last_frame_path=None,
        references=[
            type("Reference", (), {"type": "image", "path": "/tmp/ref.png", "role": "图片1"})()
        ],
        log=lambda _message: None,
    )
    payload: dict[str, object] = {"model": "ntt-720p", "metadata": metadata}
    NewApiVideoGenerator._canonicalize_video_payload(
        payload, metadata, promote_reference_media=True
    )
    assert payload["image"] == "https://relay.example/first.png"
    assert payload["images"] == ["https://relay.example/ref.png"]
    assert relayed == ["/tmp/first.png", "/tmp/ref.png"]


def test_canonicalize_video_payload_promotes_reference_media_to_top_level() -> None:
    from novelvideo.generators.video_generator import NewApiVideoGenerator

    metadata = {
        "resolution": "720p",
        "ratio": "16:9",
        "reference_images": ["https://relay.example/reference.png"],
        "reference_audios": ["https://relay.example/reference.wav"],
    }
    payload = {"model": "ntt-720p", "metadata": metadata}

    NewApiVideoGenerator._canonicalize_video_payload(
        payload, metadata, promote_reference_media=True
    )

    assert payload["images"] == ["https://relay.example/reference.png"]
    assert payload["audios"] == ["https://relay.example/reference.wav"]
    assert payload["metadata"]["reference_images"] == [
        "https://relay.example/reference.png"
    ]
    assert payload["metadata"]["reference_audios"] == [
        "https://relay.example/reference.wav"
    ]


def test_canonicalize_video_payload_keeps_first_frame_and_separate_references() -> None:
    from novelvideo.generators.video_generator import NewApiVideoGenerator

    metadata = {
        "resolution": "720p",
        "ratio": "16:9",
        "image_url": "https://relay.example/first.png",
        "reference_images": ["https://relay.example/reference.png"],
    }
    payload = {"model": "ntt-720p", "metadata": metadata}

    NewApiVideoGenerator._canonicalize_video_payload(
        payload, metadata, promote_reference_media=True
    )

    assert payload["image"] == "https://relay.example/first.png"
    assert payload["images"] == ["https://relay.example/reference.png"]


@pytest.mark.asyncio
async def test_newapi_video_processing_request_uses_canonical_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novelvideo.generators import video_generator as video_module
    from novelvideo.generators.video_generator import (
        NewApiVideoGenerator,
        ShotReference,
        VideoGenStatus,
    )

    captured: dict[str, object] = {}
    generator = NewApiVideoGenerator(
        api_key="test-key",
        endpoint="https://newapi.example/v1",
        model="video-frame-rate",
        resolution="4k",
        generate_audio=False,
    )

    async def fake_reserve(*_args, **_kwargs):
        return "reservation-1"

    async def fake_settle(*_args, **_kwargs):
        return None

    async def fake_post(url: str, payload: dict):
        captured["url"] = url
        captured["payload"] = payload
        return {"id": "task-1", "_newapi_request_id": "request-1"}

    async def fake_poll(_url: str):
        return {"status": "completed", "url": "https://example.com/output.mp4"}

    async def fake_download(_url: str, output_path: str):
        Path(output_path).write_bytes(b"video")
        return b"video"

    async def fake_relay(_path: str, **_kwargs):
        return "https://relay.example/source.mp4"

    monkeypatch.setattr(video_module, "_reserve_video_model_call", fake_reserve)
    monkeypatch.setattr(video_module, "_confirm_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "_refund_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "get_video_result_delivery", lambda: None)
    monkeypatch.setattr(generator, "_post_json", fake_post)
    monkeypatch.setattr(generator, "_get_json", fake_poll)
    monkeypatch.setattr(generator, "_download_video", fake_download)
    monkeypatch.setattr(generator, "_relay_media_input", fake_relay)

    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    result = await generator.generate(
        image_path=None,
        prompt="",
        output_path=str(tmp_path / "result.mp4"),
        duration=42,
        aspect_ratio="auto",
        poll_interval=0,
        max_polls=1,
        references=[ShotReference("video", str(source), "源视频")],
        gen_mode="video_frame_rate",
        processing_metadata={
            "target_fps": 60,
            "smart_interpolation": True,
            "resolution_tier": "4k",
        },
    )

    assert result.status == VideoGenStatus.DONE
    assert captured["url"] == "https://newapi.example/v1/video/generations"
    payload = captured["payload"]
    assert payload["model"] == "video-frame-rate"
    assert payload["prompt"] == ""
    assert payload["duration"] == 42
    metadata = payload["metadata"]
    assert metadata["target_fps"] == 60
    assert metadata["smart_interpolation"] is True
    assert metadata["resolution_tier"] == "4k"
    assert metadata["reference_videos"] == ["https://relay.example/source.mp4"]
    assert "omni_reference_task_type" not in metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["seedance2.5-mrt-720p", "seedance-2.0"])
async def test_newapi_video_keeps_reference_audios_in_both_top_level_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    from novelvideo.generators import video_generator as video_module
    from novelvideo.generators.video_generator import (
        NewApiVideoGenerator,
        ShotReference,
        VideoGenStatus,
    )

    captured: dict[str, object] = {}
    generator = NewApiVideoGenerator(
        api_key="test-key",
        endpoint="https://newapi.example/v1",
        model=model,
        resolution="720p",
        generate_audio=True,
    )

    async def fake_reserve(*_args, **_kwargs):
        return "reservation-1"

    async def fake_settle(*_args, **_kwargs):
        return None

    async def fake_post(_url: str, payload: dict):
        captured["payload"] = payload
        return {"id": "task-audio-1"}

    async def fake_poll(_url: str):
        return {"status": "completed", "url": "https://example.com/output.mp4"}

    async def fake_download(_url: str, output_path: str):
        Path(output_path).write_bytes(b"video")
        return b"video"

    def fake_upload(data: bytes, *, ext: str = "png", **_kwargs):
        return f"https://relay.example/{len(data)}.{ext}"

    monkeypatch.setattr(video_module, "_reserve_video_model_call", fake_reserve)
    monkeypatch.setattr(video_module, "_confirm_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "_refund_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "get_video_result_delivery", lambda: None)
    monkeypatch.setattr(video_module, "upload_media_bytes", fake_upload)
    monkeypatch.setattr(generator, "_post_json", fake_post)
    monkeypatch.setattr(generator, "_get_json", fake_poll)
    monkeypatch.setattr(generator, "_download_video", fake_download)

    img = tmp_path / "ref.png"
    audio = tmp_path / "voice.wav"
    img.write_bytes(b"png")
    audio.write_bytes(b"wav-audio")

    result = await generator.generate(
        image_path=str(img),
        prompt="图片1人物使用音频1的声音说话。",
        output_path=str(tmp_path / "out.mp4"),
        duration=5,
        aspect_ratio="9:16",
        poll_interval=0,
        max_polls=1,
        gen_mode="all_reference",
        references=[
            ShotReference("image", str(img), "图片1"),
            ShotReference("audio", str(audio), "音频1"),
        ],
    )

    assert result.status == VideoGenStatus.DONE
    payload = captured["payload"]
    assert payload["images"] == ["https://relay.example/3.png"]
    assert payload["audios"] == ["https://relay.example/9.wav"]
    assert payload["metadata"]["reference_images"] == ["https://relay.example/3.png"]
    assert payload["metadata"]["reference_audios"] == ["https://relay.example/9.wav"]


@pytest.mark.asyncio
async def test_newapi_video_polling_auto_reconnects_on_transient_network_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aiohttp

    from novelvideo.generators import video_generator as video_module
    from novelvideo.generators.video_generator import (
        NewApiVideoError,
        NewApiVideoGenerator,
        VideoGenStatus,
    )

    generator = NewApiVideoGenerator(
        api_key="test-key",
        endpoint="https://newapi.example/v1",
        model="seedance2.5-mrt-720p",
    )
    submit_calls = 0
    poll_calls = 0
    logs: list[str] = []

    async def fake_reserve(*_args, **_kwargs):
        return "reservation-1"

    async def fake_settle(*_args, **_kwargs):
        return None

    async def fake_post(_url: str, _payload: dict):
        nonlocal submit_calls
        submit_calls += 1
        return {"id": "task-reconnect-1"}

    async def fake_poll(_url: str):
        nonlocal poll_calls
        poll_calls += 1
        if poll_calls == 1:
            return {"status": "in_progress"}
        if poll_calls == 2:
            from aiohttp.client_reqrep import ConnectionKey

            conn_key = ConnectionKey(
                "zeroshell.top", 443, True, True, None, None, None
            )
            raise aiohttp.ClientConnectorError(
                connection_key=conn_key, os_error=OSError("None")
            )
        if poll_calls == 3:
            raise TimeoutError("poll timed out")
        if poll_calls == 4:
            raise NewApiVideoError("bad gateway", status_code=502)
        if poll_calls == 5:
            raise RuntimeError("DramaClawAPI task query returned invalid JSON: <html>502</html>")
        return {"status": "completed", "url": "https://example.com/out.mp4"}

    async def fake_download(_url: str, output_path: str):
        Path(output_path).write_bytes(b"video")
        return b"video"

    monkeypatch.setattr(video_module, "_reserve_video_model_call", fake_reserve)
    monkeypatch.setattr(video_module, "_confirm_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "_refund_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "get_video_result_delivery", lambda: None)
    monkeypatch.setattr(generator, "_post_json", fake_post)
    monkeypatch.setattr(generator, "_get_json", fake_poll)
    monkeypatch.setattr(generator, "_download_video", fake_download)

    result = await generator.generate(
        image_path=None,
        prompt="测试断网重连",
        output_path=str(tmp_path / "out.mp4"),
        poll_interval=0,
        max_polls=2,
        on_log=logs.append,
    )

    assert result.status == VideoGenStatus.DONE
    assert submit_calls == 1
    assert poll_calls == 6
    assert any("自动续连重试" in msg for msg in logs)
    assert any("网络连接已恢复" in msg for msg in logs)


@pytest.mark.asyncio
async def test_newapi_video_polling_fails_fast_on_non_retryable_401(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novelvideo.generators import video_generator as video_module
    from novelvideo.generators.video_generator import (
        NewApiVideoError,
        NewApiVideoGenerator,
        VideoGenStatus,
    )

    generator = NewApiVideoGenerator(
        api_key="bad-key",
        endpoint="https://newapi.example/v1",
        model="seedance2.5-mrt-720p",
    )
    poll_calls = 0

    async def fake_reserve(*_args, **_kwargs):
        return "reservation-1"

    async def fake_settle(*_args, **_kwargs):
        return None

    async def fake_post(_url: str, _payload: dict):
        return {"id": "task-auth-fail"}

    async def fake_poll(_url: str):
        nonlocal poll_calls
        poll_calls += 1
        raise NewApiVideoError("unauthorized", status_code=401)

    monkeypatch.setattr(video_module, "_reserve_video_model_call", fake_reserve)
    monkeypatch.setattr(video_module, "_confirm_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "_refund_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "get_video_result_delivery", lambda: None)
    monkeypatch.setattr(generator, "_post_json", fake_post)
    monkeypatch.setattr(generator, "_get_json", fake_poll)

    result = await generator.generate(
        image_path=None,
        prompt="测试鉴权失败不重试",
        output_path=str(tmp_path / "out.mp4"),
        poll_interval=0,
        max_polls=5,
    )

    assert result.status == VideoGenStatus.FAILED
    assert poll_calls == 1


@pytest.mark.asyncio
async def test_newapi_video_cross_run_resumes_interrupted_task_without_resubmit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    import novelvideo.video_request_usage as video_usage
    from novelvideo.generators import video_generator as video_module
    from novelvideo.generators.video_generator import (
        NewApiVideoGenerator,
        ShotReference,
        VideoGenStatus,
    )

    output_root = tmp_path / "output"
    state_root = tmp_path / "state"
    project_output = output_root / "alice" / "demo"
    project_output.mkdir(parents=True)
    monkeypatch.setattr(video_usage, "OUTPUT_DIR", output_root)
    monkeypatch.setattr(video_usage, "STATE_DIR", state_root)

    img = tmp_path / "ref.png"
    audio = tmp_path / "voice.wav"
    img.write_bytes(b"img")
    audio.write_bytes(b"wav")

    generator = NewApiVideoGenerator(
        api_key="test-key",
        endpoint="https://newapi.example/v1",
        model="seedance2.5-mrt-720p",
    )
    submit_calls: list[dict] = []
    relay_calls: list[str] = []
    phase = {"step": 1}

    async def fake_reserve(*_args, **_kwargs):
        return "reservation-1"

    async def fake_settle(*_args, **_kwargs):
        return None

    def fake_upload(data: bytes, *, ext: str = "png", **_kwargs):
        relay_calls.append(f"{len(data)}.{ext}")
        return f"https://relay.example/{len(data)}.{ext}"

    async def fake_post(_url: str, payload: dict):
        submit_calls.append(payload)
        return {"id": f"task-{len(submit_calls)}"}

    async def fake_poll(url: str):
        if phase["step"] == 1:
            raise TimeoutError("persistent disconnect")
        if phase["step"] == 2:
            assert url.endswith("/video/generations/task-1")
            return {"status": "completed", "url": "https://example.com/resumed.mp4"}
        if phase["step"] == 3:
            assert url.endswith("/video/generations/task-2")
            return {"status": "completed", "url": "https://example.com/new.mp4"}
        if phase["step"] == 4:
            raise TimeoutError("interrupted again")
        assert url.endswith("/video/generations/task-4")
        return {"status": "completed", "url": "https://example.com/changed.mp4"}

    async def fake_download(url: str, output_path: str):
        dest = Path(output_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(url.encode())
        return url.encode()

    monkeypatch.setattr(video_module, "NEWAPI_POLL_MAX_CONSECUTIVE_ERRORS", 1)
    monkeypatch.setattr(video_module, "_reserve_video_model_call", fake_reserve)
    monkeypatch.setattr(video_module, "_confirm_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "_refund_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "get_video_result_delivery", lambda: None)
    monkeypatch.setattr(video_module, "upload_media_bytes", fake_upload)
    monkeypatch.setattr(generator, "_post_json", fake_post)
    monkeypatch.setattr(generator, "_get_json", fake_poll)
    monkeypatch.setattr(generator, "_download_video", fake_download)

    common_kwargs = dict(
        image_path=str(img),
        prompt="镜头缓慢推近。",
        output_path=str(project_output / "videos" / "beat_01.mp4"),
        duration=5,
        aspect_ratio="9:16",
        poll_interval=0,
        max_polls=2,
        gen_mode="all_reference",
        references=[
            ShotReference("image", str(img), "图片1"),
            ShotReference("audio", str(audio), "音频1"),
        ],
        project_output_dir=str(project_output),
        episode=1,
        beat_num=1,
        task_type="single_video",
    )

    first = await generator.generate(**common_kwargs)
    assert first.status == VideoGenStatus.FAILED
    assert first.task_id == "task-1"
    assert len(submit_calls) == 1
    assert len(relay_calls) == 3

    # Retry with the exact same inputs: must resume task-1 without re-uploading or re-submitting.
    phase["step"] = 2
    resumed_logs: list[str] = []
    second = await generator.generate(**common_kwargs, on_log=resumed_logs.append)
    assert second.status == VideoGenStatus.DONE
    assert second.task_id == "task-1"
    assert Path(common_kwargs["output_path"]).read_bytes() == b"https://example.com/resumed.mp4"
    assert len(submit_calls) == 1
    assert len(relay_calls) == 3
    assert any("自动续连" in msg for msg in resumed_logs)

    # Once completed, another run (even with same prompt) submits a fresh task.
    phase["step"] = 3
    third = await generator.generate(**common_kwargs)
    assert third.status == VideoGenStatus.DONE
    assert third.task_id == "task-2"
    assert len(submit_calls) == 2

    # Interrupt a fresh task, then change prompt/inputs: must NOT reuse interrupted task-3.
    phase["step"] = 4
    interrupted_again = await generator.generate(**common_kwargs)
    assert interrupted_again.status == VideoGenStatus.FAILED
    assert interrupted_again.task_id == "task-3"

    phase["step"] = 5
    changed = await generator.generate(
        **{**common_kwargs, "prompt": "镜头改为远景俯拍。"}
    )
    assert changed.status == VideoGenStatus.DONE
    assert changed.task_id == "task-4"
    assert len(submit_calls) == 4

    db_path = video_usage.get_video_request_usage_db_path(project_output)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT request_id, status, resumable FROM video_request_usage ORDER BY accepted_at"
        ).fetchall()
        assert rows == [
            ("task-1", "completed", 0),
            ("task-2", "completed", 0),
            ("task-3", "interrupted", 1),
            ("task-4", "completed", 0),
        ]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_newapi_video_resumed_task_falls_back_when_remote_task_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novelvideo.video_request_usage as video_usage
    from novelvideo.generators import video_generator as video_module
    from novelvideo.generators.video_generator import NewApiVideoGenerator, VideoGenStatus

    output_root = tmp_path / "output"
    state_root = tmp_path / "state"
    project_output = output_root / "alice" / "demo"
    project_output.mkdir(parents=True)
    monkeypatch.setattr(video_usage, "OUTPUT_DIR", output_root)
    monkeypatch.setattr(video_usage, "STATE_DIR", state_root)

    generator = NewApiVideoGenerator(
        api_key="test-key",
        endpoint="https://newapi.example/v1",
        model="seedance2.5-mrt-720p",
    )
    submit_count = 0
    phase = {"step": 1}

    async def fake_reserve(*_args, **_kwargs):
        return "reservation-1"

    async def fake_settle(*_args, **_kwargs):
        return None

    async def fake_post(_url: str, _payload: dict):
        nonlocal submit_count
        submit_count += 1
        return {"id": f"task-{submit_count}"}

    async def fake_poll(url: str):
        if phase["step"] == 1:
            raise TimeoutError("network down")
        if url.endswith("/task-1"):
            return {"status": "failed", "error": "upstream expired"}
        return {"status": "completed", "url": "https://example.com/new.mp4"}

    async def fake_download(_url: str, output_path: str):
        Path(output_path).write_bytes(b"new-video")
        return b"new-video"

    monkeypatch.setattr(video_module, "NEWAPI_POLL_MAX_CONSECUTIVE_ERRORS", 0)
    monkeypatch.setattr(video_module, "_reserve_video_model_call", fake_reserve)
    monkeypatch.setattr(video_module, "_confirm_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "_refund_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "get_video_result_delivery", lambda: None)
    monkeypatch.setattr(generator, "_post_json", fake_post)
    monkeypatch.setattr(generator, "_get_json", fake_poll)
    monkeypatch.setattr(generator, "_download_video", fake_download)

    kwargs = dict(
        image_path=None,
        prompt="雨夜街角",
        output_path=str(project_output / "out.mp4"),
        poll_interval=0,
        max_polls=2,
        project_output_dir=str(project_output),
    )

    first = await generator.generate(**kwargs)
    assert first.status == VideoGenStatus.FAILED

    phase["step"] = 2
    second = await generator.generate(**kwargs)
    assert second.status == VideoGenStatus.DONE
    assert second.task_id == "task-2"
    assert submit_count == 2


def test_video_request_usage_migrates_v1_schema_to_v2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    import novelvideo.video_request_usage as video_usage

    output_root = tmp_path / "output"
    state_root = tmp_path / "state"
    project_output = output_root / "alice" / "demo"
    project_output.mkdir(parents=True)
    monkeypatch.setattr(video_usage, "OUTPUT_DIR", output_root)
    monkeypatch.setattr(video_usage, "STATE_DIR", state_root)

    db_path = video_usage.get_video_request_usage_db_path(project_output)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE novelvideo_schema_components (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL
            );
            INSERT INTO novelvideo_schema_components(component, version)
            VALUES ('video_request_usage', 1);
            CREATE TABLE video_request_usage (
                request_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                model_name TEXT,
                episode INTEGER,
                beat_num INTEGER,
                task_type TEXT,
                duration_seconds REAL,
                status TEXT NOT NULL DEFAULT 'accepted',
                cost_estimate REAL,
                accepted_at TEXT NOT NULL,
                completed_at TEXT,
                downloaded_at TEXT,
                updated_at TEXT NOT NULL,
                error_message TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    video_usage.record_video_request(
        project_output_dir=project_output,
        request_id="task-legacy-1",
        provider="newapi",
        model_name="seedance2.5-mrt-720p",
        episode=1,
        beat_num=1,
        task_type="single_video",
        duration_seconds=17.0,
        request_digest="digest-1",
        poll_url="https://example.com/v1/video/generations/task-legacy-1",
        resumable=True,
    )
    found = video_usage.find_resumable_video_request(
        project_output_dir=project_output,
        request_digest="digest-1",
        provider="newapi",
        model_name="seedance2.5-mrt-720p",
    )
    assert found is not None
    assert found["request_id"] == "task-legacy-1"
    assert found["poll_url"] == "https://example.com/v1/video/generations/task-legacy-1"


@pytest.mark.asyncio
async def test_newapi_video_falls_back_to_gateway_content_when_result_url_returns_401(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novelvideo.generators import video_generator as video_module
    from novelvideo.generators.video_generator import NewApiVideoGenerator, VideoGenStatus
    from novelvideo.ports.video_delivery import VideoDeliveryError, VideoDeliveryReceipt

    generator = NewApiVideoGenerator(
        api_key="test-key",
        endpoint="https://zeroshell.top/v1",
        model="YK-SD2.5-2",
    )
    downloaded_urls: list[str] = []

    async def fake_reserve(*_args, **_kwargs):
        return "reservation-1"

    async def fake_settle(*_args, **_kwargs):
        return None

    async def fake_post(_url: str, _payload: dict):
        return {"id": "task-fallback-1"}

    async def fake_poll(_url: str):
        return {
            "status": "completed",
            "metadata": {"url": "https://cdn.example/expired.mp4?expires=1"},
        }

    async def fake_download(url: str, output_path: str):
        downloaded_urls.append(url)
        if url.startswith("https://cdn.example/"):
            raise VideoDeliveryError("VIDEO_DOWNLOAD_HTTP_401", retryable=False)
        Path(output_path).write_bytes(b"fallback-video")
        return VideoDeliveryReceipt(method="download", size=14, sha256="abc")

    monkeypatch.setattr(video_module, "_reserve_video_model_call", fake_reserve)
    monkeypatch.setattr(video_module, "_confirm_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "_refund_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "get_video_result_delivery", lambda: None)
    monkeypatch.setattr(generator, "_post_json", fake_post)
    monkeypatch.setattr(generator, "_get_json", fake_poll)
    monkeypatch.setattr(generator, "_download_video", fake_download)

    output_path = tmp_path / "out.mp4"
    result = await generator.generate(
        image_path=None,
        prompt="测试回退下载",
        output_path=str(output_path),
        poll_interval=0,
        max_polls=5,
    )

    assert result.status == VideoGenStatus.DONE
    assert result.video_url == "https://zeroshell.top/v1/videos/task-fallback-1/content"
    assert downloaded_urls == [
        "https://cdn.example/expired.mp4?expires=1",
        "https://zeroshell.top/v1/videos/task-fallback-1/content",
    ]
    assert output_path.read_bytes() == b"fallback-video"


@pytest.mark.asyncio
async def test_newapi_video_401_without_content_marks_delivery_failed_and_does_not_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    import novelvideo.video_request_usage as video_usage
    from novelvideo.generators import video_generator as video_module
    from novelvideo.generators.video_generator import NewApiVideoGenerator, VideoGenStatus
    from novelvideo.ports.video_delivery import VideoDeliveryError, VideoDeliveryReceipt

    output_root = tmp_path / "output"
    state_root = tmp_path / "state"
    project_output = output_root / "alice" / "demo"
    project_output.mkdir(parents=True)
    monkeypatch.setattr(video_usage, "OUTPUT_DIR", output_root)
    monkeypatch.setattr(video_usage, "STATE_DIR", state_root)

    generator = NewApiVideoGenerator(
        api_key="test-key",
        endpoint="https://newapi.example/v1",
        model="seedance2.5-mrt-720p",
    )
    submit_ids: list[str] = []
    download_calls: list[str] = []
    phase = {"step": 1}

    async def fake_reserve(*_args, **_kwargs):
        return "reservation-1"

    async def fake_settle(*_args, **_kwargs):
        return None

    async def fake_post(_url: str, _payload: dict):
        tid = f"task-{len(submit_ids) + 1}"
        submit_ids.append(tid)
        return {"id": tid}

    async def fake_poll(_url: str):
        if phase["step"] == 1:
            return {"status": "completed", "url": "https://cdn.example/expired.mp4"}
        return {"status": "completed", "url": "https://cdn.example/fresh.mp4"}

    async def fake_download(url: str, output_path: str):
        download_calls.append(url)
        if phase["step"] == 1:
            if url.endswith("/content"):
                raise VideoDeliveryError("VIDEO_DOWNLOAD_HTTP_404", retryable=False)
            raise VideoDeliveryError("VIDEO_DOWNLOAD_HTTP_401", retryable=False)
        Path(output_path).write_bytes(b"fresh-video")
        return VideoDeliveryReceipt(method="download", size=11, sha256="def")

    monkeypatch.setattr(video_module, "_reserve_video_model_call", fake_reserve)
    monkeypatch.setattr(video_module, "_confirm_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "_refund_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "get_video_result_delivery", lambda: None)
    monkeypatch.setattr(generator, "_post_json", fake_post)
    monkeypatch.setattr(generator, "_get_json", fake_poll)
    monkeypatch.setattr(generator, "_download_video", fake_download)

    kwargs = dict(
        image_path=None,
        prompt="测试401不再续连",
        output_path=str(project_output / "out.mp4"),
        poll_interval=0,
        max_polls=10,
        project_output_dir=str(project_output),
    )

    first = await generator.generate(**kwargs)
    assert first.status == VideoGenStatus.FAILED
    assert first.error == "VIDEO_DOWNLOAD_HTTP_401"
    # Should fail immediately without retrying 10 times; fallback 404 preserves 401.
    assert download_calls == [
        "https://cdn.example/expired.mp4",
        "https://newapi.example/v1/video/generations/task-1/content",
    ]

    # Next run must submit a fresh task rather than resuming the delivery_failed task.
    phase["step"] = 2
    second = await generator.generate(**kwargs)
    assert second.status == VideoGenStatus.DONE
    assert second.task_id == "task-2"
    assert submit_ids == ["task-1", "task-2"]

    db_path = video_usage.get_video_request_usage_db_path(project_output)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT request_id, status, resumable FROM video_request_usage ORDER BY accepted_at"
        ).fetchall()
        assert rows == [("task-1", "delivery_failed", 0), ("task-2", "completed", 0)]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_newapi_video_502_exhausting_retries_marks_delivery_failed_and_stops_resuming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    import novelvideo.video_request_usage as video_usage
    from novelvideo.generators import video_generator as video_module
    from novelvideo.generators.video_generator import NewApiVideoGenerator, VideoGenStatus
    from novelvideo.ports.video_delivery import VideoDeliveryError, VideoDeliveryReceipt

    output_root = tmp_path / "output"
    state_root = tmp_path / "state"
    project_output = output_root / "alice" / "demo"
    project_output.mkdir(parents=True)
    monkeypatch.setattr(video_usage, "OUTPUT_DIR", output_root)
    monkeypatch.setattr(video_usage, "STATE_DIR", state_root)

    generator = NewApiVideoGenerator(
        api_key="test-key",
        endpoint="https://zeroshell.top/v1",
        model="YK-SD2.5-2",
    )
    submit_ids: list[str] = []
    download_calls: list[str] = []
    phase = {"step": 1}

    async def fake_reserve(*_args, **_kwargs):
        return "reservation-1"

    async def fake_settle(*_args, **_kwargs):
        return None

    async def fake_post(_url: str, _payload: dict):
        tid = f"task-{len(submit_ids) + 1}"
        submit_ids.append(tid)
        return {"id": tid}

    async def fake_poll(_url: str):
        return {"status": "completed", "metadata": {"url": "https://cdn.example/v.mp4"}}

    async def fake_download(url: str, output_path: str):
        download_calls.append(url)
        if phase["step"] == 1:
            if url.startswith("https://cdn.example/"):
                raise VideoDeliveryError("VIDEO_DOWNLOAD_HTTP_401", retryable=False)
            raise VideoDeliveryError("VIDEO_DOWNLOAD_HTTP_502", retryable=True)
        Path(output_path).write_bytes(b"new-video")
        return VideoDeliveryReceipt(method="download", size=9, sha256="ghi")

    monkeypatch.setattr(video_module, "NEWAPI_DOWNLOAD_MAX_RETRIES", 2)
    monkeypatch.setattr(video_module, "_reserve_video_model_call", fake_reserve)
    monkeypatch.setattr(video_module, "_confirm_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "_refund_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "get_video_result_delivery", lambda: None)
    monkeypatch.setattr(generator, "_post_json", fake_post)
    monkeypatch.setattr(generator, "_get_json", fake_poll)
    monkeypatch.setattr(generator, "_download_video", fake_download)

    kwargs = dict(
        image_path=None,
        prompt="测试502耗尽重试不再死循环",
        output_path=str(project_output / "out.mp4"),
        poll_interval=0,
        max_polls=20,
        project_output_dir=str(project_output),
    )

    first = await generator.generate(**kwargs)
    assert first.status == VideoGenStatus.FAILED
    assert first.error == "VIDEO_DOWNLOAD_HTTP_502"
    # 1 initial + 2 retries = 3 primary+fallback pairs (6 download calls total)
    assert len(download_calls) == 6

    phase["step"] = 2
    second = await generator.generate(**kwargs)
    assert second.status == VideoGenStatus.DONE
    assert second.task_id == "task-2"
    assert submit_ids == ["task-1", "task-2"]

    db_path = video_usage.get_video_request_usage_db_path(project_output)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT request_id, status, resumable FROM video_request_usage ORDER BY accepted_at"
        ).fetchall()
        assert rows == [("task-1", "delivery_failed", 0), ("task-2", "completed", 0)]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_newapi_video_cancelled_marks_non_resumable_and_submits_fresh_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import sqlite3

    import novelvideo.video_request_usage as video_usage
    from novelvideo.generators import video_generator as video_module
    from novelvideo.generators.video_generator import NewApiVideoGenerator, VideoGenStatus
    from novelvideo.ports.video_delivery import VideoDeliveryReceipt

    output_root = tmp_path / "output"
    state_root = tmp_path / "state"
    project_output = output_root / "alice" / "demo"
    project_output.mkdir(parents=True)
    monkeypatch.setattr(video_usage, "OUTPUT_DIR", output_root)
    monkeypatch.setattr(video_usage, "STATE_DIR", state_root)

    generator = NewApiVideoGenerator(
        api_key="test-key",
        endpoint="https://zeroshell.top/v1",
        model="YK-SD2.5-2",
    )
    submit_ids: list[str] = []
    phase = {"step": 1}

    async def fake_reserve(*_args, **_kwargs):
        return "reservation-1"

    async def fake_settle(*_args, **_kwargs):
        return None

    async def fake_post(_url: str, _payload: dict):
        tid = f"task-{len(submit_ids) + 1}"
        submit_ids.append(tid)
        return {"id": tid}

    async def fake_poll(_url: str):
        return {"status": "completed", "url": "https://cdn.example/v.mp4"}

    async def fake_download(_url: str, output_path: str):
        if phase["step"] == 1:
            raise asyncio.CancelledError()
        Path(output_path).write_bytes(b"fresh-after-cancel")
        return VideoDeliveryReceipt(method="download", size=18, sha256="jkl")

    monkeypatch.setattr(video_module, "_reserve_video_model_call", fake_reserve)
    monkeypatch.setattr(video_module, "_confirm_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "_refund_video_model_call", fake_settle)
    monkeypatch.setattr(video_module, "get_video_result_delivery", lambda: None)
    monkeypatch.setattr(generator, "_post_json", fake_post)
    monkeypatch.setattr(generator, "_get_json", fake_poll)
    monkeypatch.setattr(generator, "_download_video", fake_download)

    kwargs = dict(
        image_path=None,
        prompt="测试终止任务后不再续连",
        output_path=str(project_output / "out.mp4"),
        poll_interval=0,
        max_polls=5,
        project_output_dir=str(project_output),
    )

    with pytest.raises(asyncio.CancelledError):
        await generator.generate(**kwargs)

    phase["step"] = 2
    second = await generator.generate(**kwargs)
    assert second.status == VideoGenStatus.DONE
    assert second.task_id == "task-2"
    assert submit_ids == ["task-1", "task-2"]

    db_path = video_usage.get_video_request_usage_db_path(project_output)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT request_id, status, resumable FROM video_request_usage ORDER BY accepted_at"
        ).fetchall()
        assert rows == [("task-1", "cancelled", 0), ("task-2", "completed", 0)]
    finally:
        conn.close()

