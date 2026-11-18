from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from novelvideo.s9_storyboard import (
    S9DurationAssessment,
    S9SegmentAssetSlot,
    S9SegmentPlan,
    S9StoryboardPlan,
    S9StyleContext,
    S9SubShot,
    audit_s9_plan_pacing,
    build_s9_stage1_system_prompt,
    build_s9_stage2_fallback_prompt,
    build_s9_stage2_system_prompt,
    build_s9_stage2_task_prompt,
    resolve_s9_style_context,
)
from novelvideo.skills.yd_storyboard_skill import (
    YD_STORYBOARD_SKILL_ID,
    load_yd_storyboard_skill,
)


def test_yd_storyboard_skill_loads_and_hot_reloads(tmp_path, monkeypatch):
    bundle = load_yd_storyboard_skill()
    assert bundle.skill_id == YD_STORYBOARD_SKILL_ID
    assert "YD分镜skills" in bundle.version
    assert "timing.md" in bundle.references
    assert "acting.md" in bundle.references
    assert "full-output.md" in bundle.references

    custom_root = tmp_path / "skills"
    shutil.copytree(bundle.root_dir, custom_root / YD_STORYBOARD_SKILL_ID)
    monkeypatch.setenv("REELFORGE_STORYBOARD_SKILLS_DIR", str(custom_root))

    skill_file = custom_root / YD_STORYBOARD_SKILL_ID / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8") + "\n<!-- hot-reload-marker -->\n",
        encoding="utf-8",
    )
    reloaded = load_yd_storyboard_skill()
    assert "hot-reload-marker" in reloaded.skill_md


def test_stage1_and_stage2_prompts_use_yd_skill_and_compact_pacing():
    s1 = build_s9_stage1_system_prompt(max_segment_seconds=30, spine_template="drama")
    assert "老唐" not in s1
    assert "YD分镜skills" in s1
    assert "上限，不是目标" in s1
    assert "废除固定镜数配额" in s1
    assert "exit_state" in s1
    assert "边做边说并行不相加" in s1

    s2 = build_s9_stage2_system_prompt(
        tail_anchor=False,
        include_first_frame=False,
        include_scene_layout=True,
    )
    assert "老唐" not in s2
    assert "【参考职责】" in s2
    assert "【全局光影画风】" in s2
    assert "【场景布局】" in s2
    assert "【注意事项】" in s2
    assert "【角色声音】" in s2
    assert "画面全域禁止生成任何硬字幕、文字条或水印" in s2


def test_audit_s9_plan_pacing_catches_slow_s9_regression_and_accepts_compact_plan():
    # Mirrors the slow s9 output from the user's screenshot/video:
    # 4s empty door + 3s monitor + 16s typing for 32 chars + missing exit_state
    bloated = S9StoryboardPlan(
        duration_assessment=S9DurationAssessment(estimated_total_seconds=30, segment_count=2),
        segments=[
            S9SegmentPlan(
                segment_number=1,
                title="雨夜守夜人",
                planned_duration_seconds=30,
                story_beat="林默敲代码，黑衣男人进店买烟，最后男人视线转向屏幕。",
                exit_state="",  # Missing handoff!
                shots=[
                    S9SubShot(
                        shot_number=1,
                        planned_duration_seconds=4.0,
                        start_seconds=0.0,
                        end_seconds=4.0,
                        audio_type="silence",
                        content="空拍暴雨玻璃门",
                    ),
                    S9SubShot(
                        shot_number=2,
                        planned_duration_seconds=3.0,
                        start_seconds=4.0,
                        end_seconds=7.0,
                        audio_type="silence",
                        content="特写显示器代码",
                    ),
                    S9SubShot(
                        shot_number=3,
                        planned_duration_seconds=16.0,
                        start_seconds=7.0,
                        end_seconds=23.0,
                        audio_type="narration",
                        speaker="林默 OS",
                        spoken_text="白天是写字楼里的架构师，夜里是全网冷门接口的守夜人。只要再抓十分钟，这批异常心跳包就能完整落盘。",
                        content="林默敲机械键盘",
                    ),
                    S9SubShot(
                        shot_number=4,
                        planned_duration_seconds=7.0,
                        start_seconds=23.0,
                        end_seconds=30.0,
                        audio_type="dialogue",
                        speaker="黑衣男人",
                        spoken_text="一包薄荷烟，再拿罐冰美式。",
                        content="男人进店拍下百元钞票点单。",
                    ),
                ],
            ),
            S9SegmentPlan(
                segment_number=2,
                title="屏幕上的疑点",
                planned_duration_seconds=12,
                story_beat="男人质问公网节点",
                shots=[
                    S9SubShot(
                        shot_number=1,
                        planned_duration_seconds=6.0,
                        start_seconds=0.0,
                        end_seconds=6.0,
                        audio_type="silence",
                        content="无声干等6秒",
                    ),
                    S9SubShot(
                        shot_number=2,
                        planned_duration_seconds=6.0,
                        start_seconds=6.0,
                        end_seconds=12.0,
                        audio_type="dialogue",
                        speaker="黑衣男人",
                        spoken_text="这是什么。",
                        content="男人盯着屏幕问。",
                    ),
                ],
            ),
        ],
    )
    issues = audit_s9_plan_pacing(bloated, max_segment_seconds=30)
    assert any("无声铺垫镜" in msg for msg in issues)
    assert any("大段1-分镜3" in msg and "空尾" in msg for msg in issues)
    assert any("缺少 `exit_state`" in msg for msg in issues)
    assert any("大段2-分镜1" in msg and "无声镜头长达" in msg for msg in issues)

    # Now verify a tight YD-style compact plan passes with 0 issues
    compact = S9StoryboardPlan(
        duration_assessment=S9DurationAssessment(
            pace_tier="紧凑",
            estimated_total_seconds=22,
            segment_count=2,
        ),
        segments=[
            S9SegmentPlan(
                segment_number=1,
                title="守夜交易与盯屏",
                planned_duration_seconds=16,
                story_beat="林默敲代码守夜，黑衣男人进店买烟酒后视线转向屏幕。",
                entry_state="林默坐收银台后敲键盘，玻璃门外暴雨霓虹闪烁",
                exit_state="黑衣男人站在收银台右侧，头微侧越过林默左肩盯向绿色代码屏幕，右手压在百元钞上",
                scene_layout="收银台居画面中左，玻璃门在右后方，林默面朝左前方显示器，黑衣人立于收银台右侧。",
                shots=[
                    S9SubShot(
                        shot_number=1,
                        planned_duration_seconds=10.0,
                        start_seconds=0.0,
                        end_seconds=10.0,
                        audio_type="narration",
                        speaker="林默 OS",
                        spoken_text="白天是写字楼里的架构师，夜里是全网冷门接口的守夜人。只要再抓十分钟，这批异常心跳包就能完整落盘。",
                        content="中近景从显示器侧后方拍，背景玻璃门外暴雨霓虹闪烁；林默快敲机械键盘，嘴唇闭合随OS节奏推进。",
                    ),
                    S9SubShot(
                        shot_number=2,
                        planned_duration_seconds=3.5,
                        start_seconds=10.0,
                        end_seconds=13.5,
                        audio_type="dialogue",
                        speaker="黑衣男人",
                        spoken_text="一包薄荷烟，再拿罐冰美式。",
                        content="中景转近景：自动门滑开，黑衣人一步走到台前拍下百元钞，边开口点单边收回视线。",
                    ),
                    S9SubShot(
                        shot_number=3,
                        planned_duration_seconds=2.5,
                        start_seconds=13.5,
                        end_seconds=16.0,
                        audio_type="silence",
                        content="过肩近景：黑衣人未等林默转身取货，目光骤然越过林默肩头锁定显示器绿码并微眯双眼。",
                    ),
                ],
            ),
            S9SegmentPlan(
                segment_number=2,
                title="屏幕质问",
                planned_duration_seconds=8,
                story_beat="男人直接点出公网节点",
                entry_state="承接上段末黑衣人盯屏幕姿态",
                exit_state="林默手指僵在键盘上，抬眼对视黑衣人",
                shots=[
                    S9SubShot(
                        shot_number=1,
                        planned_duration_seconds=4.0,
                        start_seconds=0.0,
                        end_seconds=4.0,
                        audio_type="dialogue",
                        speaker="黑衣男人",
                        spoken_text="这台老机器跑得动这种节点？",
                        content="黑衣人指尖轻叩桌面开口试探。",
                    ),
                    S9SubShot(
                        shot_number=2,
                        planned_duration_seconds=4.0,
                        start_seconds=4.0,
                        end_seconds=8.0,
                        audio_type="dialogue",
                        speaker="林默",
                        spoken_text="只是普通监控后台。",
                        content="林默敲击停顿半拍后迅速切换收银界面，面无波澜回话。",
                    ),
                ],
            ),
        ],
    )
    assert audit_s9_plan_pacing(compact, max_segment_seconds=30) == []


def test_stage2_prompt_and_fallback_include_scene_layout_and_exit_boundary():
    style = resolve_s9_style_context(visual_style="chinese_period_drama", aspect_ratio="9:16")
    assert "真人电影实拍" in style.s9_style_anchor

    seg = S9SegmentPlan(
        segment_number=1,
        title="守夜交易与盯屏",
        planned_duration_seconds=12,
        story_beat="林默守夜迎客，黑衣人买烟后视线锁定屏幕。",
        entry_state="林默坐在收银台后敲键盘",
        exit_state="黑衣人立于台前右方，视线锁定绿码显示器",
        scene_layout="收银台在画面左侧，玻璃门在右后方，摄影机保持在关系轴正面工作侧。",
        shots=[
            S9SubShot(
                shot_number=1,
                title="守夜抓包",
                planned_duration_seconds=8.0,
                start_seconds=0.0,
                end_seconds=8.0,
                shot_scale_movement="中近景",
                camera_angle="平视",
                camera_motion="固定机位",
                audio_type="narration",
                speaker="林默 OS",
                spoken_text="白天架构师，夜里守夜人。",
                content="林默在收银台后敲键盘盯屏幕。",
                identity_ids=["林默_青年"],
            ),
            S9SubShot(
                shot_number=2,
                title="点单盯屏",
                planned_duration_seconds=4.0,
                start_seconds=8.0,
                end_seconds=12.0,
                shot_scale_movement="双人中景",
                camera_angle="平视",
                camera_motion="微幅横摇",
                audio_type="dialogue",
                speaker="黑衣男人",
                spoken_text="一包薄荷烟。",
                content="黑衣人拍下百元钞点单后视线转向屏幕。",
                identity_ids=["林默_青年", "黑衣男人_青年"],
            ),
        ],
    )
    slots = [
        S9SegmentAssetSlot(slot=1, media_type="image", asset_kind="identity", name="林默", asset_id="林默_青年"),
        S9SegmentAssetSlot(slot=2, media_type="image", asset_kind="identity", name="黑衣男人", asset_id="黑衣男人_青年"),
        S9SegmentAssetSlot(slot=1, media_type="audio", asset_kind="voice", name="黑衣男人", asset_id="黑衣男人_青年"),
    ]
    task_prompt = build_s9_stage2_task_prompt(segment=seg, slots=slots, style_context=style)
    assert "本段末镜必达落点（exit_state" in task_prompt
    assert "【已规划场景空间蓝图】" in task_prompt

    fallback = build_s9_stage2_fallback_prompt(segment=seg, slots=slots, style_context=style)
    assert "【参考职责】" in fallback
    assert "【全局光影画风】" in fallback
    assert "【场景布局】" in fallback
    assert "【注意事项】" in fallback
    assert "本输入止于：黑衣人立于台前右方，视线锁定绿码显示器" in fallback
    assert "【角色声音】" in fallback


@pytest.mark.asyncio
async def test_yd_workflow_persists_entry_exit_and_layout_metadata(tmp_path, monkeypatch):
    from novelvideo.cognee.store import CogneeStore
    from novelvideo.models import CharacterIdentity, NovelCharacter, NovelEpisode
    from novelvideo.workflows import s9_script_writing as wf_mod
    from novelvideo import s9_storyboard as s9_mod

    project_dir = tmp_path / "alice" / "yddemo"
    project_dir.mkdir(parents=True, exist_ok=True)
    store = CogneeStore.__new__(CogneeStore)
    store.project_name = "alice/yddemo"
    store.dataset_name = "novelvideo_alice_yddemo"
    store._db = None
    store._characters = {}
    store._episodes = {}
    store._alias_index = {}
    store.project_dir = str(project_dir)
    store.state_dir = str(project_dir)
    store.db_path = str(project_dir / "data.db")

    char = NovelCharacter(name="林默", reference_audio_path="")
    char.identities = [
        CharacterIdentity(identity_id="林默_青年", character_name="林默", identity_name="青年")
    ]
    await store.add_character(char)
    await store.add_episode(
        NovelEpisode(
            number=1,
            title="夜班",
            beat_source_text="林默敲键盘守夜，黑衣人进店点单后盯向屏幕。",
            identity_ids=["林默_青年"],
        )
    )

    async def fake_stage1(**kwargs):
        return S9StoryboardPlan(
            duration_assessment=S9DurationAssessment(
                pace_tier="紧凑",
                estimated_total_seconds=14,
                segment_count=1,
            ),
            segments=[
                S9SegmentPlan(
                    segment_number=1,
                    title="守夜迎客",
                    planned_duration_seconds=14,
                    story_beat="林默敲键盘时迎客点单，黑衣人盯向屏幕。",
                    entry_state="林默坐收银台后敲键盘",
                    exit_state="黑衣人立于台前盯向屏幕",
                    scene_layout="收银台居左，玻璃门在右后方。",
                    shots=[
                        S9SubShot(
                            shot_number=1,
                            planned_duration_seconds=9.0,
                            start_seconds=0.0,
                            end_seconds=9.0,
                            audio_type="narration",
                            speaker="林默 OS",
                            spoken_text="夜里是全网冷门接口的守夜人。",
                            content="林默边敲键盘边听门响。",
                            identity_ids=["林默_青年"],
                        ),
                        S9SubShot(
                            shot_number=2,
                            planned_duration_seconds=5.0,
                            start_seconds=9.0,
                            end_seconds=14.0,
                            audio_type="dialogue",
                            speaker="林默",
                            spoken_text="买什么？",
                            content="黑衣人进店后视线锁定屏幕。",
                            identity_ids=["林默_青年"],
                        ),
                    ],
                )
            ],
        )

    async def fake_stage2(**kwargs):
        return "【参考职责】\n纯文字测试。\n分镜1（0秒到9秒）：中近景，林默敲键盘。"

    monkeypatch.setattr(wf_mod, "run_s9_stage1_plan", fake_stage1)
    monkeypatch.setattr(s9_mod, "run_s9_stage2_segment_prompt", fake_stage2)

    try:
        wf = wf_mod.create_s9_script_writing_workflow(
            store,
            store,
            output_dir=str(project_dir),
            spine_template="drama",
        )
        await wf.run(episode_num=1, max_segment_seconds=30)
        beats = await store.get_beats_as_dicts(1)
        assert len(beats) == 1
        meta = beats[0]["s9_segment_meta"]
        assert meta["entry_state"] == "林默坐收银台后敲键盘"
        assert meta["exit_state"] == "黑衣人立于台前盯向屏幕"
        assert meta["scene_layout"] == "收银台居左，玻璃门在右后方。"
        assert "YD 分镜分段完成" in wf.last_review_summary
    finally:
        await store.close()
