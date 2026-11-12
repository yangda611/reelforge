"""流式故事脚本生成回归测试（Cloudflare 120 秒 524 根因）。

freezone「生成故事脚本」曾用 pydantic-ai 非流式 ``.run()`` 等完整结构化
输出才返回；一次生成常超过 Cloudflare 120 秒 Proxy Read Timeout，连接被
掐成 524（R5 实测 4/4 失败）。本测试用慢速流式假 agent 证明：

* 生成走 ``run_stream`` 而非 ``.run()``（假 agent 的 ``run()`` 直接抛错）；
* chunk 持续回流，相邻 chunk 间隔远小于 120 秒（无静默窗口）；
* 返回的仍是校验后的 ``FreezoneStoryScriptGenerateData`` 结构化输出。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from novelvideo.api.schemas import (
    FreezoneStoryScriptGenerateData,
    FreezoneStoryScriptRow,
)
from novelvideo.freezone.text_node import (
    generate_freezone_story_script,
    generate_freezone_story_script_with_vision,
)

# Cloudflare Proxy Read Timeout：超过这个间隔无数据，连接会被掐成 524。
CLOUDFLARE_READ_TIMEOUT_SECONDS = 120.0


def _script_output() -> FreezoneStoryScriptGenerateData:
    return FreezoneStoryScriptGenerateData(
        title="雨夜重逢",
        rows=[
            FreezoneStoryScriptRow(
                shot_no=1,
                duration=5,
                visual_description="老站台上两人相认，雨丝逆光。",
                character_1="阿铮",
                character_description_1="归乡的旅人。",
            )
        ],
    )


class SlowStreamAgent:
    """模拟一次缓慢的流式结构化生成。

    ``run()`` 直接抛错，证明被测代码走的是流式路径；``run_stream()``
    返回一个异步上下文管理器，``get_output()`` 在返回前按 chunk_gaps
    的间隔逐块等待，模拟 token 持续回流，并记录每次等待的时间戳。
    """

    def __init__(self, *, chunk_gaps: list[float], output: Any) -> None:
        self.chunk_gaps = chunk_gaps
        self.output = output
        self.chunk_timestamps: list[float] = []
        self.captured_messages: Any = None

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("非流式 .run() 不应再被故事脚本生成调用")

    @asynccontextmanager
    async def run_stream(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        result = self
        result.captured_messages = args[0] if args else kwargs.get("messages")

        class _StreamResult:
            async def get_output(self) -> Any:
                loop = asyncio.get_running_loop()
                for gap in result.chunk_gaps:
                    await asyncio.sleep(gap)
                    result.chunk_timestamps.append(loop.time())
                return result.output

        yield _StreamResult()


def _assert_no_silent_window(agent: SlowStreamAgent) -> None:
    assert len(agent.chunk_timestamps) >= 2
    gaps = [
        b - a
        for a, b in zip(agent.chunk_timestamps, agent.chunk_timestamps[1:])
    ]
    assert gaps, "流式生成应有持续输出"
    assert max(gaps) < CLOUDFLARE_READ_TIMEOUT_SECONDS, (
        f"chunk 间隔 {max(gaps):.1f}s 超过 Cloudflare 120s 窗口，会被掐成 524"
    )


@pytest.mark.asyncio
async def test_text_story_script_streams_instead_of_blocking_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = SlowStreamAgent(chunk_gaps=[0.4, 0.4, 0.4, 0.4], output=_script_output())
    monkeypatch.setattr(
        "novelvideo.freezone.text_node.get_freezone_story_script_agent",
        lambda model=None: agent,
    )

    result = await generate_freezone_story_script(
        source_text="一段雨夜重逢的短故事",
        character_refs=[{"name": "阿铮"}],
        model="gpt-6-astra",
    )

    assert isinstance(result, FreezoneStoryScriptGenerateData)
    assert result.title == "雨夜重逢"
    assert len(result.rows) == 1
    assert result.rows[0].character_1 == "阿铮"
    assert "雨丝" in result.rows[0].visual_description
    _assert_no_silent_window(agent)


@pytest.mark.asyncio
async def test_vision_story_script_streams_with_frames_attached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    frame = tmp_path / "frame_0.png"
    Image.new("RGB", (1600, 900), (24, 48, 96)).save(frame)
    agent = SlowStreamAgent(chunk_gaps=[0.3, 0.3, 0.3], output=_script_output())
    monkeypatch.setattr(
        "novelvideo.freezone.text_node.get_freezone_video_story_script_agent",
        lambda: agent,
    )

    result = await generate_freezone_story_script_with_vision(
        frame_paths=[str(frame)],
        source_text="一段雨夜重逢的短故事",
        prompt="生成视频脚本",
    )

    assert isinstance(result, FreezoneStoryScriptGenerateData)
    assert result.title == "雨夜重逢"
    assert len(result.rows) == 1
    assert result.rows[0].shot_no == 1
    # 关键帧必须真的作为附件送进模型，而不是只在提示词里被提一句。
    messages = agent.captured_messages
    assert isinstance(messages, list) and len(messages) == 2
    assert getattr(messages[1], "media_type", None) == "image/jpeg"
    _assert_no_silent_window(agent)
