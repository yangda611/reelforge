"""Single-shot video route: reference media must reach the generator.

Regression tests for the reported bug where a generic-backend single video
request arrived at the gateway as pure text-to-video: the route only wrote
``config["references"]`` for HappyHorse/Grok, so every other backend lost
the reference images/audio before the runner.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


class _FakeStore:
    def __init__(self, beats):
        self.beats = beats
        self.updated = []
        self.state_dir = "/state/alice/demo"

    async def get_beats_as_dicts(self, episode: int):
        assert episode == 3
        return self.beats

    async def update_beat_asset(self, **kwargs):
        self.updated.append(kwargs)
        return True


def _patch_generation_celery(monkeypatch, generation, tmp_path, store):
    ctx = SimpleNamespace(project_id="proj-1", state_dir=tmp_path / "state")

    async def fake_resolve_generation_project(
        project_arg, user, required_role="editor"
    ):
        return SimpleNamespace(
            ctx=ctx,
            username="alice",
            project_name="demo",
            project_dir=tmp_path,
            output_dir=str(tmp_path),
            state_dir=str(tmp_path / "state"),
            runtime_dir=str(tmp_path / "runtime"),
        )

    async def fake_make_sqlite_store_for_context(ctx_arg):
        assert ctx_arg is ctx
        return store

    monkeypatch.setattr(
        generation, "_resolve_generation_project", fake_resolve_generation_project
    )
    monkeypatch.setattr(
        generation, "make_sqlite_store_for_context", fake_make_sqlite_store_for_context
    )
    return ctx


def _fake_enqueue(calls):
    async def fake_enqueue_project_task(
        ctx, *, task_type, queue_kind, episode, payload, **extra
    ):
        calls.append({"task_type": task_type, "episode": episode, "payload": payload})
        return SimpleNamespace(
            task_state=SimpleNamespace(task_id="task-1"),
            backend="celery",
            queue="video",
        )

    return fake_enqueue_project_task


def _make_beat(tmp_path, *, image_paths, audio_paths):
    config = {
        "duration": 5,
        "final_prompt": "镜头从角色正面缓慢推近。",
        "reference_image_paths": [str(p) for p in image_paths],
        "reference_audio_paths": [str(p) for p in audio_paths],
    }
    return {
        "beat_number": 2,
        "video_mode": "first_frame",
        "video_prompt": "镜头从角色正面缓慢推近。",
        "seedance2_config_json": json.dumps(config),
    }


@pytest.mark.asyncio
async def test_single_video_generic_backend_forwards_references(
    monkeypatch, tmp_path
):
    from novelvideo.api.routes import generation
    from novelvideo.api.schemas import SingleVideoRequest

    image_a = tmp_path / "ref-a.png"
    image_b = tmp_path / "ref-b.png"
    audio = tmp_path / "voice.wav"
    for path in (image_a, image_b, audio):
        path.write_bytes(b"media")
    frame = tmp_path / "frames" / "ep003" / "beat_02.png"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"frame")

    calls = []
    store = _FakeStore([_make_beat(tmp_path, image_paths=[image_a, image_b], audio_paths=[audio])])

    async def fake_audio_duration(*_args, **_kwargs):
        return 0.0

    _patch_generation_celery(monkeypatch, generation, tmp_path, store)
    monkeypatch.setattr(
        generation,
        "get_task_backend",
        lambda: SimpleNamespace(enqueue_project_task=_fake_enqueue(calls)),
    )
    monkeypatch.setattr(generation, "_api_audio_duration_seconds", fake_audio_duration)

    response = await generation.generate_single_video(
        project="demo",
        episode_num=3,
        beat_num=2,
        body=SingleVideoRequest(video_backend="newapi_seedance-1.0-pro-fast"),
        user={"username": "alice"},
    )

    assert response["ok"] is True
    references = calls[0]["payload"]["config"]["references"]
    assert [(item["type"], item["path"]) for item in references] == [
        ("image", str(image_a)),
        ("image", str(image_b)),
        ("audio", str(audio)),
    ]


@pytest.mark.asyncio
async def test_single_video_generic_backend_errors_when_references_lost(
    monkeypatch, tmp_path
):
    from novelvideo.api.routes import generation
    from novelvideo.api.schemas import SingleVideoRequest

    frame = tmp_path / "frames" / "ep003" / "beat_02.png"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"frame")

    calls = []
    store = _FakeStore(
        [
            _make_beat(
                tmp_path,
                image_paths=[tmp_path / "gone-a.png", tmp_path / "gone-b.png"],
                audio_paths=[],
            )
        ]
    )

    async def fake_audio_duration(*_args, **_kwargs):
        return 0.0

    _patch_generation_celery(monkeypatch, generation, tmp_path, store)
    monkeypatch.setattr(
        generation,
        "get_task_backend",
        lambda: SimpleNamespace(enqueue_project_task=_fake_enqueue(calls)),
    )
    monkeypatch.setattr(generation, "_api_audio_duration_seconds", fake_audio_duration)

    response = await generation.generate_single_video(
        project="demo",
        episode_num=3,
        beat_num=2,
        body=SingleVideoRequest(video_backend="newapi_seedance-1.0-pro-fast"),
        user={"username": "alice"},
    )

    assert response["ok"] is False
    assert "参考素材" in response["error"]
    assert calls == []


@pytest.mark.asyncio
async def test_single_video_generic_backend_without_references_still_works(
    monkeypatch, tmp_path
):
    from novelvideo.api.routes import generation
    from novelvideo.api.schemas import SingleVideoRequest

    frame = tmp_path / "frames" / "ep003" / "beat_02.png"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"frame")

    calls = []
    store = _FakeStore([_make_beat(tmp_path, image_paths=[], audio_paths=[])])

    async def fake_audio_duration(*_args, **_kwargs):
        return 0.0

    _patch_generation_celery(monkeypatch, generation, tmp_path, store)
    monkeypatch.setattr(
        generation,
        "get_task_backend",
        lambda: SimpleNamespace(enqueue_project_task=_fake_enqueue(calls)),
    )
    monkeypatch.setattr(generation, "_api_audio_duration_seconds", fake_audio_duration)

    response = await generation.generate_single_video(
        project="demo",
        episode_num=3,
        beat_num=2,
        body=SingleVideoRequest(video_backend="newapi_seedance-1.0-pro-fast"),
        user={"username": "alice"},
    )

    assert response["ok"] is True
    assert calls[0]["payload"]["config"].get("references") in (None, [])
