"""An ACP worker disappearing must not silently complete a partial turn."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from novelvideo.chat import hermes_sdk
from novelvideo.chat.hermes_operation import disposition_for
from novelvideo.chat.presentation_text import _completion_text_or_existing


def _thread(monkeypatch, updates, *, final_response=False):
    thread = hermes_sdk.HermesSdkThread(
        cli_path=Path("hermes"), cwd=Path("."), env={}, model=None,
        username="local", session_id="existing-session",
    )
    reader = asyncio.StreamReader()
    for update in updates:
        reader.feed_data((json.dumps({
            "method": "session/update", "params": {"update": update},
        }) + "\n").encode())
    if final_response:
        reader.feed_data(b'{"id":99,"result":{"stopReason":"end_turn"}}\n')
    reader.feed_eof()
    thread._proc = SimpleNamespace(stdout=reader, returncode=0)
    monkeypatch.setattr(thread, "_prepare", AsyncMock())
    monkeypatch.setattr(thread, "_send", AsyncMock(return_value=99))
    monkeypatch.setattr(thread, "close", AsyncMock())
    monkeypatch.setattr(hermes_sdk, "_issue_turn_capability", lambda **kwargs: None)
    return thread


@pytest.mark.parametrize("after_write", [False, True])
async def test_unexpected_eof_is_failed_and_retires_worker(monkeypatch, after_write):
    updates = [{
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "Let me arrange the canvas:"},
    }]
    if after_write:
        updates.extend([
            {"sessionUpdate": "tool_call", "toolCallId": "layout-1",
             "title": "freezone_emit_canvas_command", "status": "pending",
             "rawInput": {"commands": [{"type": "layout_nodes", "mode": "grid"}]}},
            {"sessionUpdate": "tool_call_update", "toolCallId": "layout-1",
             "status": "completed", "rawOutput": {
                 "ok": True, "applied": True, "canvas_apply_status": "applied",
                 "applied_count": 1,
             }},
        ])
    thread = _thread(monkeypatch, updates)
    events = [event async for event in thread.stream("Arrange the canvas")]
    terminal = events[-1]
    assert terminal.type == "complete"
    assert disposition_for(terminal) == "failed"
    assert terminal.text and "中断" in terminal.text
    assert any(event.type == "assistant_delta" for event in events)
    if after_write:
        assert any(event.type == "tool_updated" for event in events)
    thread.close.assert_awaited_once()
    # An unknown write outcome must never replay the user's request.
    thread._send.assert_awaited_once()


async def test_genuine_completion_keeps_worker_available(monkeypatch):
    thread = _thread(monkeypatch, [], final_response=True)
    events = [event async for event in thread.stream("Hello")]
    assert disposition_for(events[-1]) == "completed"
    thread.close.assert_not_awaited()


def test_failed_completion_preserves_partial_text_and_explains_interruption():
    partial = "Let me arrange the canvas:"
    notice = "连接意外中断，本轮后续处理未完成。"
    merged = _completion_text_or_existing(notice, partial, disposition="failed")
    assert merged == partial + "\n\n" + notice
    assert _completion_text_or_existing(notice, merged, disposition="failed") == merged
    assert _completion_text_or_existing("Final answer", partial) == "Final answer"


def test_timeout_completion_preserves_progress_and_explains_no_replay():
    partial = "我会重新整理这三个节点。"
    merged = _completion_text_or_existing(
        "(hermes timed out)", partial, disposition="timeout"
    )
    assert merged.startswith(partial)
    assert "等待模型响应超时" in merged
    assert "不会自动重试" in merged
    assert _completion_text_or_existing(
        "(hermes timed out)", merged, disposition="timeout"
    ) == merged

    empty = _completion_text_or_existing(
        "(hermes timed out)", "", disposition="timeout"
    )
    assert "等待模型响应超时" in empty
    assert "hermes timed out" not in empty


async def test_reconnect_warm_does_not_read_active_prompt_stream(monkeypatch):
    thread = _thread(monkeypatch, [])
    started = asyncio.Event()
    release = asyncio.Event()

    async def active_turn(_prompt, **_kwargs):
        started.set()
        await release.wait()
        yield hermes_sdk.ChatBackendEvent(type="complete", text="Done")

    monkeypatch.setattr(thread, "_stream_turn", active_turn)

    async def consume():
        return [event async for event in thread.stream("Arrange")]

    turn = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 2)
    warming = asyncio.create_task(thread.warm())
    try:
        await asyncio.sleep(0)
        thread._prepare.assert_not_awaited()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(turn, warming), 2)
    thread._prepare.assert_awaited_once()


@pytest.mark.parametrize("prior_tool_error", [False, True])
async def test_service_persists_interruption_without_replaying_write(
    monkeypatch, tmp_path, prior_tool_error
):
    from novelvideo.chat import service as chat_service
    from novelvideo.chat.store import ChatScope, chat_store

    monkeypatch.setenv("NOVELVIDEO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("NOVELVIDEO_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("DRAMACLAW_CHAT_BACKEND", "hermes")
    scope = ChatScope(kind="project", id="project-a", surface="freezone",
                      canvas_id="canvas-a", agent_id="main")
    partial = "布局执行成功，正在检查。"
    notice = "本轮对话连接意外中断，后续处理未完成。"
    calls = []

    class FakeThread:
        async def stream(self, _prompt, **_kwargs):
            calls.append("stream")
            yield hermes_sdk.ChatBackendEvent(type="assistant_delta", text=partial)
            if prior_tool_error:
                yield hermes_sdk.ChatBackendEvent(
                    type="tool_updated", name="freezone_emit_canvas_command",
                    raw={"error": "invalid layout mode"},
                )
            yield hermes_sdk.ChatBackendEvent(
                type="complete", disposition="failed", text=notice,
            )

    class FakePool:
        async def get_for_user(self, *_args, **_kwargs):
            return FakeThread()

        async def reset_for_user(self, *_args, **_kwargs):
            pytest.fail("A possibly applied canvas write must not be replayed")

    monkeypatch.setattr(chat_service, "is_hermes_backend_available", lambda: True)
    monkeypatch.setattr(chat_service, "_write_hermes_tool_mode", lambda *a, **k: None)
    monkeypatch.setattr("novelvideo.chat.hermes_pool.pool", FakePool())
    if prior_tool_error:
        monkeypatch.setattr(chat_service, "_extract_tool_chat_error",
                            lambda _raw: "Layout command rejected")
    events = []

    async def on_event(event):
        events.append(event)

    result = await chat_service.stream_assistant_reply(
        "admin", "project-a", "Arrange the canvas", on_event,
        surface="freezone", surface_context={"canvasId": "canvas-a"},
        store_scope=scope, turn_id="interrupted-turn",
    )
    assert partial in result["content"]
    assert notice in result["content"]
    assert calls == ["stream"]
    assert any(event["type"] == "assistant_message" for event in events)
    saved = chat_store.list_messages("admin", scope)
    assert any(notice in str(message.get("content", "")) for message in saved)
