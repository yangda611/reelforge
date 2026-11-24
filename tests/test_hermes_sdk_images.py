"""Hermes ACP image transport regression tests."""

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from novelvideo.chat import hermes_pool, hermes_sdk
from novelvideo.chat.turn_images import MAX_TURN_IMAGES


def _image_url(data=b"image-bytes"):
    return "data:" + "image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def _thread(monkeypatch):
    thread = hermes_sdk.HermesSdkThread(
        cli_path=Path("hermes"),
        cwd=Path("."),
        env={},
        model=None,
        username="user",
        session_id="session-a",
    )
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"id":99,"result":{}}\n')
    reader.feed_eof()
    thread._proc = SimpleNamespace(stdout=reader)
    monkeypatch.setattr(thread, "_prepare", AsyncMock())
    monkeypatch.setattr(thread, "_send", AsyncMock(return_value=99))
    monkeypatch.setattr(hermes_sdk, "_issue_turn_capability", lambda **kw: "capability")
    return thread


async def test_native_image_blocks_preserve_turn_metadata(monkeypatch):
    thread = _thread(monkeypatch)
    events = [
        event
        async for event in thread.stream(
            "describe",
            images=[_image_url()],
            current_project="project-a",
            gateway_api_key="turn-key",
        )
    ]
    method, params = thread._send.await_args.args
    assert method == "session/prompt"
    assert params["prompt"] == [
        {"type": "text", "text": "[CONTEXT: current_project=project-a]\n\ndescribe"},
        {
            "type": "image",
            "mimeType": "image/jpeg",
            "data": base64.b64encode(b"image-bytes").decode("ascii"),
        },
    ]
    assert params["_meta"] == {
        "dramaclaw.control_context_capability": "capability",
        "dramaclaw.gateway_api_key": "turn-key",
        "dramaclaw.gateway_api_key_required": True,
    }
    assert events[-1].type == "complete"


@pytest.mark.parametrize(
    "image",
    [
        "https://example.com/image.jpg",
        "data:text/plain;base64,eA==",
        "data:" + "image/jpeg;base64,",
        "data:" + "image/jpeg;base64,%%%",
        "data:" + "image/jpeg;base64,a",
        3,
    ],
)
async def test_invalid_image_rejected_before_worker_io(monkeypatch, image):
    thread = _thread(monkeypatch)
    with pytest.raises(ValueError, match="image"):
        _ = [event async for event in thread.stream("describe", images=[image])]
    thread._prepare.assert_not_awaited()
    thread._send.assert_not_awaited()


async def test_native_image_blocks_are_bounded(monkeypatch):
    thread = _thread(monkeypatch)
    _ = [
        event
        async for event in thread.stream(
            "describe",
            images=[_image_url()] * (MAX_TURN_IMAGES + 1),
        )
    ]
    params = thread._send.await_args.args[1]
    assert len(params["prompt"]) == MAX_TURN_IMAGES + 1


async def test_images_are_scoped_to_one_turn(monkeypatch):
    thread = _thread(monkeypatch)
    _ = [event async for event in thread.stream("describe", images=[_image_url()])]
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"id":99,"result":{}}\n')
    reader.feed_eof()
    thread._proc = SimpleNamespace(stdout=reader)
    _ = [event async for event in thread.stream("next turn")]
    params = thread._send.await_args.args[1]
    assert params["prompt"] == [{"type": "text", "text": "next turn"}]


async def test_pool_forwards_images_and_finishes_turn():
    seen = []

    class FakeThread:
        async def stream(self, prompt, **kwargs):
            seen.append(kwargs)
            if False:
                yield None

    owner = SimpleNamespace(_begin_turn=AsyncMock(), _finish_turn=AsyncMock())
    facade = hermes_pool._ManagedHermesThread(
        owner, SimpleNamespace(thread=FakeThread())
    )
    images = [_image_url()]
    _ = [event async for event in facade.stream("describe", images=images)]
    assert seen[0]["images"] == images
    owner._finish_turn.assert_awaited_once()
