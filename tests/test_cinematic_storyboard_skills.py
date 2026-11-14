import json
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_cinematic_skill_docs_and_hermes_references_complete() -> None:
    scale_doc = (
        REPO_ROOT / "docs" / "skills" / "ai-shot-scale-combinations" / "SKILL.md"
    ).read_text(encoding="utf-8")
    camera_doc = (
        REPO_ROOT / "docs" / "skills" / "ai-camera-motion" / "SKILL.md"
    ).read_text(encoding="utf-8")
    transition_doc = (
        REPO_ROOT / "docs" / "skills" / "ai-shot-transition" / "SKILL.md"
    ).read_text(encoding="utf-8")
    hermes_ref = (
        REPO_ROOT
        / ".hermes"
        / "skills"
        / "dramaclaw"
        / "references"
        / "cinematic-storyboard-skills.md"
    ).read_text(encoding="utf-8")
    episode_playbook = (
        REPO_ROOT / ".hermes" / "skills" / "dramaclaw" / "playbooks" / "episode.md"
    ).read_text(encoding="utf-8")
    director_skill = (
        REPO_ROOT / ".hermes" / "skills" / "sketch-storyboard-director" / "SKILL.md"
    ).read_text(encoding="utf-8")

    # 15 shot-scale combinations doc
    for scale in ("远景", "全景", "中景", "近景", "特写"):
        assert scale in scale_doc
    for idx in range(1, 16):
        assert f"**{idx:02d}**" in scale_doc
    assert "忌连续 3 拍" in scale_doc

    # Azhe camera motion doc
    for term in ("Dolly Zoom", "f/d", "机位与路径", "[LOCKED FIXED CAMERA]"):
        assert term in camera_doc
    for card_num in ("A01", "A11", "A26"):
        assert card_num in camera_doc

    # Shot transition + cross-episode doc
    for bridge in (
        "动作顺接",
        "相似构图/形状/色彩匹配",
        "视线引导",
        "前景遮挡",
        "急速甩镜",
        "J-Cut / L-Cut",
        "Episode N-1",
    ):
        assert bridge in transition_doc

    # Hermes references & playbooks wiring
    assert "cinematic-storyboard-skills.md" in (
        REPO_ROOT / ".hermes" / "skills" / "dramaclaw" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "$EP > 1" in hermes_ref
    assert "references/cinematic-storyboard-skills.md" in episode_playbook
    assert "Mandatory Cinematic Storyboard Skills" in director_skill


def test_prompts_actively_include_cinematic_skills_and_continuity() -> None:
    from novelvideo.agents.content_rewriter import REWRITE_PROMPT
    from novelvideo.agents.global_video_optimizer import (
        GLOBAL_VIDEO_OPTIMIZER_INSTRUCTIONS_EN,
    )
    from novelvideo.agents.keyframe_prompt_builder import (
        KEYFRAME_PROMPT_BUILDER_INSTRUCTIONS_EN,
    )
    from novelvideo.agents.video_prompt_builder import (
        VIDEO_PROMPT_BUILDER_INSTRUCTIONS_EN,
    )
    from novelvideo.generators.prompt_builder import PromptComponents
    from novelvideo.seedance2_i2v.prompt import SEEDANCE2_COMPOSER_SYSTEM_PROMPT
    from novelvideo.workflows.literal_script_writing import LITERAL_SCRIPT_PROMPT

    assert "15套景别组合" in LITERAL_SCRIPT_PROMPT
    assert "严禁连续 3 拍" in LITERAL_SCRIPT_PROMPT
    assert "6大无痕转场视觉锚点" in LITERAL_SCRIPT_PROMPT
    assert "跨集丝滑承接铁律" in LITERAL_SCRIPT_PROMPT

    assert "上一集" in REWRITE_PROMPT

    assert "Mandatory Storyboard Skills" in SEEDANCE2_COMPOSER_SYSTEM_PROMPT
    assert "Azhe 5-Variable Camera Choreography" in SEEDANCE2_COMPOSER_SYSTEM_PROMPT
    assert "cross_episode_transition" in SEEDANCE2_COMPOSER_SYSTEM_PROMPT

    for prompt_text in (
        GLOBAL_VIDEO_OPTIMIZER_INSTRUCTIONS_EN,
        VIDEO_PROMPT_BUILDER_INSTRUCTIONS_EN,
        KEYFRAME_PROMPT_BUILDER_INSTRUCTIONS_EN,
    ):
        assert "Azhe 5-Variable Camera Architecture" in prompt_text
        assert "Dolly Zoom" in prompt_text
        assert "15 Shot-Scale Progression" in prompt_text
        assert "Cross-Episode Continuity" in prompt_text

    beats = [
        {"beat_number": 1, "scene_ref": {"scene_id": "conv_store"}},
        {"beat_number": 2, "scene_ref": {"scene_id": "conv_store"}},
    ]
    render_continuity = PromptComponents.build_scene_continuity(beats, total_panels=2)
    sketch_continuity = PromptComponents.build_scene_continuity_for_sketch(
        beats, total_panels=2
    )
    assert "SHOT SCALE & TRANSITION DISCIPLINE" in render_continuity
    assert "SHOT SCALE & TRANSITION DISCIPLINE" in sketch_continuity


async def test_cross_episode_helpers_and_store_loading() -> None:
    from novelvideo.skills.cinematic_storyboard_skill import (
        extract_prev_episode_last_beat_for_video,
        format_prev_episode_script_context,
        load_prev_episode_last_beat_from_store,
        load_prev_episode_script_context_from_store,
    )

    prev_episode = SimpleNamespace(
        title="雨夜来客",
        cliffhanger="黑衣男人将染血芯片按在柜台桌面，冷声问她认不认识这东西。",
        content="暴雨敲打便利店玻璃。\n林默停下敲击键盘的手。\n黑衣男人按住她的手腕。",
    )
    prev_beats = [
        {
            "beat_number": 14,
            "scene_ref": {"scene_id": "便利店_夜"},
            "visual_description": "{{林默_店员}}近景惊愕抬头。",
            "audio_type": "silence",
        },
        {
            "beat_number": 15,
            "scene_ref": {"scene_id": "便利店_夜"},
            "visual_description": "特写桌面：[[染血芯片]]与一只苍白手掌压在上面。",
            "dialogue": "认得这个吗？",
            "audio_type": "dialogue",
            "seedance2_config_json": json.dumps(
                {"final_prompt": "镜头缓推至染血芯片与手指边缘，门外雨声淅沥。"}
            ),
        },
    ]

    formatted = format_prev_episode_script_context(
        prev_episode, prev_beats, target_episode_num=2
    )
    assert "第 1 集" in formatted
    assert "第 2 集" in formatted
    assert "雨夜来客" in formatted
    assert "染血芯片" in formatted
    assert "跨集丝滑过渡硬要求" in formatted

    annotated, prompt_tail = extract_prev_episode_last_beat_for_video(
        prev_episode, prev_beats, prev_episode_num=1
    )
    assert annotated is not None
    assert annotated["_from_previous_episode"] == 1
    assert "染血芯片" in annotated["_prev_episode_cliffhanger"]
    assert prompt_tail == "镜头缓推至染血芯片与手指边缘，门外雨声淅沥。"

    class FakeStore:
        def get_episode(self, num: int):
            return prev_episode if num == 1 else None

        async def get_beats_as_dicts(self, num: int):
            return list(prev_beats) if num == 1 else []

    ctx_text, ctx_last = await load_prev_episode_script_context_from_store(
        FakeStore(), episode_num=2
    )
    assert "第 1 集" in ctx_text
    assert ctx_last is not None and ctx_last["_from_previous_episode"] == 1

    last_beat, last_prompt = await load_prev_episode_last_beat_from_store(
        FakeStore(), episode_num=2
    )
    assert last_beat is not None and last_beat["beat_number"] == 15
    assert last_prompt == "镜头缓推至染血芯片与手指边缘，门外雨声淅沥。"

    # Episode 1 should safely return empty / None
    assert await load_prev_episode_script_context_from_store(FakeStore(), 1) == ("", None)
    assert await load_prev_episode_last_beat_from_store(FakeStore(), 1) == (None, None)


def test_seedance2_adjacent_beats_context_marks_cross_episode() -> None:
    from novelvideo.seedance2_i2v.prompt import _build_adjacent_beats_context

    context = _build_adjacent_beats_context(
        beat={"beat_number": 1, "scene_ref": {"scene_id": "便利店_夜"}},
        prev_beat={
            "beat_number": 15,
            "scene_ref": {"scene_id": "便利店_夜"},
            "visual_description": "手掌按住染血芯片。",
            "_from_previous_episode": 1,
            "_prev_episode_cliffhanger": "黑衣人逼问芯片来历。",
            "seedance2_config_json": json.dumps(
                {"final_prompt": "镜头紧逼手掌边缘与芯片细节。"}
            ),
        },
        next_beat={"beat_number": 2, "visual_description": "林默冷冷抽回手。"},
    )
    assert context["cross_episode_transition"] is True
    assert context["prev_beat"]["from_previous_episode"] == 1
    assert "黑衣人逼问" in context["prev_beat"]["prev_episode_cliffhanger"]
    assert "镜头紧逼手掌边缘" in context["prev_beat"]["prompt_ending"]


async def test_seedance2_panel_loads_prev_episode_last_beat_when_ep_gt_1(
    monkeypatch, tmp_path
) -> None:
    from novelvideo.seedance2_i2v import panel_service
    from novelvideo.seedance2_i2v.prompt import Seedance2PromptGeneration

    seen = {}

    class Store:
        def get_episode(self, num: int):
            if num == 1:
                return SimpleNamespace(title="第一集", cliffhanger="门口响起脚步声")
            return None

        async def get_beats_as_dicts(self, num: int):
            if num == 1:
                return [
                    {
                        "beat_number": 12,
                        "scene_ref": {"scene_id": "alley"},
                        "visual_description": "雨夜巷口有人推门而入。",
                    }
                ]
            return []

        async def update_beat_asset(self, **_kwargs):
            return None

    async def fake_generate(**kwargs):
        seen.update(kwargs)
        return Seedance2PromptGeneration(
            prompt="承接上集门口脚步声顺畅推镜。",
            used_ai=True,
            draft_prompt="承接上集门口脚步声顺畅推镜。",
        )

    monkeypatch.setattr(panel_service, "build_seedance2_project_assets", lambda **_k: [])
    monkeypatch.setattr(panel_service, "generate_seedance2_prompt", fake_generate)

    await panel_service.generate_seedance2_prompt_for_panel(
        store=Store(),
        state_dir=tmp_path / "state",
        episode=2,
        beat={"beat_number": 1, "visual_description": "男主回头看门口。"},
        prev_beat=None,
        next_beat=None,
        project_dir=tmp_path,
    )

    assert seen["prev_beat"] is not None
    assert seen["prev_beat"]["_from_previous_episode"] == 1
    assert seen["prev_beat"]["beat_number"] == 12


async def test_global_video_optimizer_includes_cross_episode_finale_bridge(
    tmp_path,
) -> None:
    from PIL import Image

    from novelvideo.agents.global_video_optimizer import GlobalVideoPromptOptimizer

    sketch_path = tmp_path / "beat_01.jpg"
    Image.new("RGB", (64, 64), color=(128, 128, 128)).save(sketch_path)

    captured_tasks: list[str] = []

    class FakeAgent:
        async def run(self, user_prompt):
            captured_tasks.append(user_prompt[0])
            return SimpleNamespace(
                output="Slow dolly in past the foreground doorframe as the clerk raises her eyes."
            )

    optimizer = GlobalVideoPromptOptimizer()
    optimizer._get_agent = lambda _lang: FakeAgent()  # type: ignore[method-assign]

    await optimizer.optimize_single_beat(
        beat={
            "beat_number": 1,
            "scene_ref": {"scene_id": "conv_store"},
            "visual_description": "{{林默}}抬眸直视门口来客。",
        },
        sketch_image_path=str(sketch_path),
        character_color_map={"林默": {"color": "cyan", "appearance": "a woman in a black hoodie"}},
        language="en",
        prev_beat={
            "beat_number": 18,
            "scene_ref": {"scene_id": "conv_store"},
            "visual_description": "门铃骤响，一只黑靴跨进便利店。",
            "video_prompt": "Camera dollies sideways along the glass door as the boot steps over the threshold.",
            "_from_previous_episode": 1,
            "_prev_episode_cliffhanger": "神秘人踏入雨夜便利店。",
        },
        prev_prompt=None,
        total_beats=10,
    )

    assert len(captured_tasks) == 1
    task_text = captured_tasks[0]
    assert "Previous Episode Ep1 Finale Beat" in task_text
    assert "Cross-Episode Seamless Bridge Required" in task_text
    assert "神秘人踏入雨夜便利店" in task_text
    assert "Camera dollies sideways along the glass door" in task_text


async def test_literal_script_workflow_injects_prev_episode_context_for_ep2(
    monkeypatch,
) -> None:
    from novelvideo.workflows.literal_script_writing import (
        LiteralBeatMetaOutput,
        LiteralScriptWritingWorkflow,
    )

    ep1 = SimpleNamespace(
        number=1,
        title="暴雨便利店",
        cliffhanger="黑衣男人抬眼盯住门外闪烁的霓虹灯。",
        content="暴雨夜。\n林默把纸币推到柜台上。",
    )
    ep2 = SimpleNamespace(
        number=2,
        title="暗巷追逐",
        identity_ids=[],
        identity_default_map={},
        scene_menu=[
            SimpleNamespace(
                scene_id="便利店", base_scene_id="", variant_id="", time_of_day=""
            )
        ],
        prop_menu=[SimpleNamespace(prop_id="纸币")],
    )

    class _CrossEpStore:
        output_dir = ""
        _props = {}

        def __init__(self) -> None:
            self.persisted = None

        async def load_graph_state(self):
            return None

        async def get_episode_from_graph(self, num: int):
            return ep1 if num == 1 else ep2

        def get_episode(self, num: int):
            return ep1 if num == 1 else ep2

        def get_all_characters(self):
            return []

        async def get_beats_as_dicts(self, num: int):
            if num == 1:
                return [
                    {
                        "beat_number": 16,
                        "scene_ref": {"scene_id": "便利店"},
                        "visual_description": "黑衣男人手指按在纸币边缘，视线转向左侧林默。",
                        "audio_type": "silence",
                    }
                ]
            return []

        async def persist_narration_script(self, script):
            self.persisted = script

    captured_prompts: list[str] = []

    class _FakeAgent:
        async def run(self, prompt):
            captured_prompts.append(prompt)
            return SimpleNamespace(
                output=LiteralBeatMetaOutput(
                    audio_type="silence",
                    speaker="",
                    speaker_kind="character",
                    visual_description="近景：林默沿着黑衣男人手指的方向缓缓抬头。",
                    scene_id="便利店",
                    continues_previous_shot=False,
                    estimated_duration_seconds=3.5,
                )
            )

    monkeypatch.setattr(
        LiteralScriptWritingWorkflow,
        "agent",
        property(lambda _workflow: _FakeAgent()),
    )

    store = _CrossEpStore()
    workflow = LiteralScriptWritingWorkflow(cognee_store=store, sqlite_store=store)
    script = await workflow.run(
        episode_num=2,
        source_text="第1场 便利店 夜 内\n林默顺着他的目光抬起头。",
    )

    assert len(script.beats) == 1
    assert len(captured_prompts) == 1
    first_prompt = captured_prompts[0]
    assert "上一集结尾镜头 (EP1 Beat 16)" in first_prompt
    assert "上一集（第 1 集）结尾状态与跨集丝滑承接锚点" in first_prompt
    assert "黑衣男人抬眼盯住门外闪烁的霓虹灯" in first_prompt


async def test_content_rewriter_appends_prev_episode_context(monkeypatch) -> None:
    from novelvideo.agents import content_rewriter

    captured: dict[str, str] = {}

    class _FakeRewriteAgent:
        def __init__(self, *_args, **kwargs) -> None:
            captured["system_prompt"] = str(kwargs.get("system_prompt") or "")

        async def run(self, task: str):
            captured["task"] = task
            return SimpleNamespace(
                output=content_rewriter.AdaptedContentOutput(
                    lines=["他刚一抬头，门外突然传来急促的刹车声。"]
                )
            )

    monkeypatch.setattr(content_rewriter, "Agent", _FakeRewriteAgent)
    monkeypatch.setattr(
        content_rewriter,
        "get_newapi_text_pydantic_model",
        lambda *_a, **_k: object(),
    )
    monkeypatch.setattr(
        content_rewriter,
        "get_newapi_structured_output_model_settings",
        lambda *_a, **_k: {},
    )

    result = await content_rewriter.rewrite_episode_content(
        "林默听到刹车声冲向门口。",
        episode_title="第二集",
        prev_episode_context="## 【上一集（第 1 集）结尾状态与跨集丝滑承接锚点 -> 第 2 集开篇必看】\n- 上一集结尾悬念/卡点: 黑衣人逼问未完",
    )

    assert "刹车声" in result
    assert "上一集（第 1 集）结尾状态" in captured["task"]
    assert "黑衣人逼问未完" in captured["task"]
