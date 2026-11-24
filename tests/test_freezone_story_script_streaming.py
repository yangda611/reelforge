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
    _STORY_SCRIPT_BATCH_ROWS,
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


# --- A/C：输出上限导致截断后的兜底 -------------------------------------------


def _rows(count: int, start: int = 1) -> list[FreezoneStoryScriptRow]:
    return [
        FreezoneStoryScriptRow(
            shot_no=start + index,
            duration=4,
            visual_description=f"第 {start + index} 镜的画面。",
            character_1="阿铮",
        )
        for index in range(count)
    ]


class TruncatingAgent:
    """首次整表生成抛截断异常，之后按任务书要求的分批行数返回。

    ``IncompleteToolCall`` 正是 pydantic-ai 在结构化输出撞上 token 上限时
    抛出的异常（见 ``_agent_graph.check_incomplete_tool_call``），用它模拟
    「JSON 写到一半被截断」比伪造 ValidationError 更贴近真实。

    每批的行数从任务书的「最多 N 行」里读，而不是写死常量：这样断言同时
    验证了批量约束真的进了提示词，而不是被测代码背着自己另开一套。
    """

    def __init__(self, *, total_rows: int) -> None:
        self.total_rows = total_rows
        self.calls: list[Any] = []
        self.written = 0

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("故事脚本生成应始终走流式")

    @asynccontextmanager
    async def run_stream(self, *args: Any, **kwargs: Any):
        from pydantic_ai.exceptions import IncompleteToolCall

        prompt = args[0] if args else kwargs.get("messages")
        self.calls.append(prompt)
        state = self

        class _StreamResult:
            async def get_output(_self) -> Any:
                if len(state.calls) == 1:
                    # 第一次：整表生成，模拟撞上输出上限。
                    raise IncompleteToolCall(
                        "Model token limit (8192) exceeded while generating a tool call, "
                        "resulting in incomplete arguments."
                    )
                limit = _requested_rows(prompt)
                remaining = state.total_rows - state.written
                count = min(limit, remaining)
                start = state.written + 1
                state.written += count
                return FreezoneStoryScriptGenerateData(
                    title="分批标题",
                    rows=_rows(count, start=start),
                )

        yield _StreamResult()


def _requested_rows(prompt: Any) -> int:
    """从分批任务书里读出「最多 N 行」。"""
    import re

    match = re.search(r"最多 (\d+) 行", str(prompt))
    assert match, f"分批任务书必须写明行数上限：{prompt}"
    return int(match.group(1))


@pytest.mark.asyncio
async def test_truncated_story_script_falls_back_to_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整表被截断后必须自动分批，而不是把失败抛给用户。"""

    batch = _STORY_SCRIPT_BATCH_ROWS
    total = batch * 2 + 2  # 4+4+2：最后一批不满，循环应自行收尾。
    agent = TruncatingAgent(total_rows=total)
    monkeypatch.setattr(
        "novelvideo.freezone.text_node.get_freezone_story_script_agent",
        lambda model=None: agent,
    )

    result = await generate_freezone_story_script(source_text="一段十镜的短故事")

    assert isinstance(result, FreezoneStoryScriptGenerateData)
    assert len(result.rows) == total
    # shot_no 必须重排成连续的 1..total，不能因为分批各从 1 数就重复。
    assert [row.shot_no for row in result.rows] == list(range(1, total + 1))
    assert result.title == "分批标题"
    # 1 次整表尝试 + 3 次分批（4+4+2）。
    assert len(agent.calls) == 4
    # 第一批的任务书里必须写明起始镜号，否则模型会重新从头写。
    assert f"第 1 到第 {batch} 镜" in agent.calls[1]
    # 后续批次带上已写镜头的概要，避免剧情断裂。
    assert "前面已经写过的镜头概要" in agent.calls[2]


@pytest.mark.asyncio
async def test_batches_running_out_raises_instead_of_returning_half_a_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批次数用尽仍没写完必须报错：半张分镜表会被下游当成完整脚本用。"""

    batch = _STORY_SCRIPT_BATCH_ROWS
    monkeypatch.setenv("FREEZONE_STORY_SCRIPT_MAX_BATCHES", "2")
    # 模型永远写满每批，模拟「剧本比允许的批次还长」。
    agent = TruncatingAgent(total_rows=batch * 10)
    monkeypatch.setattr(
        "novelvideo.freezone.text_node.get_freezone_story_script_agent",
        lambda model=None: agent,
    )

    with pytest.raises(RuntimeError, match="still unfinished after 2 batches"):
        await generate_freezone_story_script(source_text="一段很长的短故事")


@pytest.mark.asyncio
async def test_retry_exhaustion_is_not_treated_as_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重试耗尽不是截断：分批只会让模型把同一个字段错误犯 N 次。"""

    from pydantic_ai.exceptions import UnexpectedModelBehavior

    class BadFieldsAgent:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("故事脚本生成应始终走流式")

        @asynccontextmanager
        async def run_stream(self, *args: Any, **kwargs: Any):
            self.calls += 1

            class _StreamResult:
                async def get_output(_self) -> Any:
                    raise UnexpectedModelBehavior(
                        "Exceeded maximum output retries (3)"
                    )

            yield _StreamResult()

    agent = BadFieldsAgent()
    monkeypatch.setattr(
        "novelvideo.freezone.text_node.get_freezone_story_script_agent",
        lambda model=None: agent,
    )

    with pytest.raises(UnexpectedModelBehavior):
        await generate_freezone_story_script(source_text="一段短故事")
    # 只尝试一次就放弃，绝不能因为误判成截断而再跑 8 批。
    assert agent.calls == 1


def test_story_script_settings_raise_output_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A：默认把输出预算抬到 32768，且可被环境变量覆盖。"""

    from novelvideo.freezone.text_node import _story_script_model_settings

    monkeypatch.delenv("NEWAPI_TEXT_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("FREEZONE_STORY_SCRIPT_OUTPUT_TOKENS", raising=False)
    assert _story_script_model_settings()["max_tokens"] == 32768

    monkeypatch.setenv("FREEZONE_STORY_SCRIPT_OUTPUT_TOKENS", "16384")
    assert _story_script_model_settings()["max_tokens"] == 16384


def test_story_script_settings_defer_to_context_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启用上下文预算的部署由它统一裁决，这里不能再塞 max_tokens。"""

    from novelvideo.freezone.text_node import _story_script_model_settings

    monkeypatch.delenv("FREEZONE_STORY_SCRIPT_OUTPUT_TOKENS", raising=False)
    monkeypatch.setenv("NEWAPI_TEXT_CONTEXT_TOKENS", "131072")
    assert "max_tokens" not in _story_script_model_settings()


def test_explicit_output_budget_wins_over_context_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式配置优先：否则开了上下文预算的部署会被固定回 8192、白白截断。"""

    from novelvideo.freezone.text_node import _story_script_model_settings

    monkeypatch.setenv("NEWAPI_TEXT_CONTEXT_TOKENS", "131072")
    monkeypatch.setenv("FREEZONE_STORY_SCRIPT_OUTPUT_TOKENS", "16384")
    # 显式值原样传出，超窗口由上下文预算抛 MODEL_CONTEXT_BUDGET_EXCEEDED 明示。
    assert _story_script_model_settings()["max_tokens"] == 16384
