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
    assert "reference_images" not in payload["metadata"]
    assert "reference_audios" not in payload["metadata"]


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
