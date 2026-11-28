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
    assert "entry_state" not in s2
    assert "exit_state" not in s2
    assert "明确亚区间" in s2
    assert "【本集原稿参考】" in s2
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
    task_prompt = build_s9_stage2_task_prompt(
        segment=seg,
        slots=slots,
        style_context=style,
        source_text="暴雨夜，林默坐在二手显示器前敲代码，黑衣男人进店点单。",
    )
    assert "本段末镜必达收束状态" in task_prompt
    assert "entry_state" not in task_prompt
    assert "exit_state" not in task_prompt
    assert "【本集原稿参考" in task_prompt
    assert "暴雨夜，林默坐在二手显示器前敲代码" in task_prompt
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


def test_normalize_recovers_missing_subshot_assets_and_character_os_voice():
    from novelvideo.models import (
        CharacterIdentity,
        NovelCharacter,
        NovelEpisode,
        PropMenuItem,
        SceneMenuItem,
    )
    from novelvideo.s9_storyboard import normalize_s9_storyboard_plan

    c1 = NovelCharacter(name="林默")
    c1.identities = [CharacterIdentity(identity_id="林默_青年时期", character_name="林默", identity_name="青年时期")]
    c2 = NovelCharacter(name="黑衣男人")
    c2.identities = [CharacterIdentity(identity_id="黑衣男人_青年时期", character_name="黑衣男人", identity_name="青年时期")]

    ep = NovelEpisode(
        number=1,
        title="雨夜守夜人",
        identity_ids=["林默_青年时期", "黑衣男人_青年时期"],
        scene_menu=[SceneMenuItem(scene_id="老旧便利店_深夜", description="雨夜便利店收银台")],
        prop_menu=[
            PropMenuItem(prop_id="二手显示器"),
            PropMenuItem(prop_id="机械键盘"),
            PropMenuItem(prop_id="百元钞票"),
        ],
    )

    # Simulate LLM returning empty identity_ids / scene_id / prop_ids on shots
    raw = S9StoryboardPlan(
        segments=[
            S9SegmentPlan(
                segment_number=1,
                title="守夜抓包与来客",
                planned_duration_seconds=14,
                story_beat="林默敲机械键盘盯着二手显示器，黑衣男人入店拍下百元钞票。",
                shots=[
                    S9SubShot(
                        shot_number=1,
                        planned_duration_seconds=9.0,
                        start_seconds=0.0,
                        end_seconds=9.0,
                        audio_type="narration",
                        speaker="林默 OS",
                        spoken_text="白天是写字楼里的架构师，夜里是全网冷门接口的守夜人。",
                        content="林默在老旧便利店台后紧盯二手显示器，手指在机械键盘上快速敲击。",
                    ),
                    S9SubShot(
                        shot_number=2,
                        planned_duration_seconds=5.0,
                        start_seconds=9.0,
                        end_seconds=14.0,
                        audio_type="dialogue",
                        speaker="黑衣男人",
                        spoken_text="一包薄荷烟。",
                        content="黑衣男人走近收银台，将百元钞票拍在台面，随后视线转向显示器。",
                    ),
                ],
            )
        ]
    )

    plan, per_seg_slots = normalize_s9_storyboard_plan(
        raw,
        max_segment_seconds=30,
        characters=[c1, c2],
        episode=ep,
    )
    seg = plan.segments[0]
    assert "林默_青年时期" in seg.shots[0].identity_ids
    assert seg.shots[0].scene_id == "老旧便利店_深夜"
    assert set(seg.shots[0].prop_ids) == {"二手显示器", "机械键盘"}
    assert "黑衣男人_青年时期" in seg.shots[1].identity_ids
    assert "百元钞票" in seg.shots[1].prop_ids

    slots = per_seg_slots[0]
    slot_summary = [(s.media_type, s.asset_kind, s.asset_id) for s in slots]
    assert ("image", "identity", "林默_青年时期") in slot_summary
    assert ("image", "identity", "黑衣男人_青年时期") in slot_summary
    assert ("image", "scene", "老旧便利店_深夜") in slot_summary
    assert ("image", "prop", "二手显示器") in slot_summary
    # Character OS voice MUST be bound as an audio slot!
    assert ("audio", "voice", "林默_青年时期") in slot_summary
    assert ("audio", "voice", "黑衣男人_青年时期") in slot_summary


def test_compute_manifest_status_and_seedance2_assets_resolve_character_os_voice_and_images(tmp_path):
    from types import SimpleNamespace
    from novelvideo.s9_storyboard import compute_s9_manifest_with_live_status
    from novelvideo.seedance2_i2v.assets import build_seedance2_project_assets
    from novelvideo.seedance2_i2v.models import Seedance2I2VMode

    proj = tmp_path / "proj"
    id_img = proj / "assets" / "characters" / "林默" / "identities" / "青年时期.png"
    id_img.parent.mkdir(parents=True, exist_ok=True)
    id_img.write_bytes(b"png-id")

    scene_img = proj / "assets" / "scenes" / "老旧便利店" / "master.png"
    scene_img.parent.mkdir(parents=True, exist_ok=True)
    scene_img.write_bytes(b"png-scene")

    prop_img = proj / "assets" / "props" / "二手显示器" / "reference_3view.png"
    prop_img.parent.mkdir(parents=True, exist_ok=True)
    prop_img.write_bytes(b"png-prop")

    voice_file = proj / "assets" / "characters" / "林默" / "voices" / "voice_default.mp3"
    voice_file.parent.mkdir(parents=True, exist_ok=True)
    voice_file.write_bytes(b"mp3-voice")

    char = SimpleNamespace(
        name="林默",
        reference_audio_path="",
        identities=[
            SimpleNamespace(
                identity_id="林默_青年时期",
                character_name="林默",
                identity_name="青年时期",
                appearance_details="",
                costume="",
                face_prompt="",
                body_prompt="",
                turnaround_prompt="",
                turnaround_path="",
                portraits=[SimpleNamespace(view="front", image_path=str(id_img))],
                default_view="front",
                voice_sample_path=str(voice_file),
            )
        ],
    )
    ep = SimpleNamespace(
        number=1,
        scene_menu=[SimpleNamespace(scene_id="老旧便利店_深夜", variant_id="")],
        prop_menu=[SimpleNamespace(prop_id="二手显示器", reference_image_path=str(prop_img))],
    )

    hydrated = compute_s9_manifest_with_live_status(
        {
            "asset_manifest": [
                {"slot_label": "@图片1", "media_type": "image", "asset_kind": "identity", "name": "林默", "asset_id": "林默_青年时期"},
                {"slot_label": "@图片2", "media_type": "image", "asset_kind": "scene", "name": "老旧便利店_深夜", "asset_id": "老旧便利店_深夜"},
                {"slot_label": "@图片3", "media_type": "image", "asset_kind": "prop", "name": "二手显示器", "asset_id": "二手显示器"},
                {"slot_label": "@音频1", "media_type": "audio", "asset_kind": "voice", "name": "林默", "asset_id": "林默_青年时期"},
            ]
        },
        output_dir=proj,
        characters=[char],
        episode=ep,
    )
    assert [item["status_label"] for item in hydrated["asset_manifest"]] == [
        "已就绪",
        "已就绪",
        "已就绪",
        "已就绪",
    ]

    beat = {
        "beat_number": 1,
        "audio_type": "narration",
        "speaker": "",
        "detected_identities": ["林默_青年时期"],
        "detected_props": ["二手显示器"],
        "scene_ref": {"scene_id": "老旧便利店_深夜"},
        "s9_subshots": [
            {
                "shot_number": 1,
                "audio_type": "narration",
                "speaker": "林默 OS",
                "spoken_text": "夜里是全网冷门接口的守夜人。",
                "identity_ids": ["林默_青年时期"],
                "scene_id": "老旧便利店_深夜",
                "prop_ids": ["二手显示器"],
            }
        ],
    }
    assets = build_seedance2_project_assets(
        project_output=proj,
        episode=1,
        beat=beat,
        mode=Seedance2I2VMode.MULTIMODAL_REFERENCE,
        state_dir=proj,
        characters=[char],
    )
    selected_keys = [(a.media_type, a.key, a.exists, a.selected) for a in assets if a.selected]
    assert ("image", "identity:林默_青年时期", True, True) in selected_keys
    assert ("image", "scene:老旧便利店_深夜", True, True) in selected_keys
    assert ("image", "prop:二手显示器", True, True) in selected_keys
    assert any(
        mt == "audio" and key.startswith("voice:林默") and ex and sel
        for mt, key, ex, sel in selected_keys
    )

    from novelvideo.s9_storyboard import build_s9_slots_from_beat, rebind_s9_prompt_with_active_assets

    _subshots, _meta, slots = build_s9_slots_from_beat(beat)
    rebound, ordered, missing = rebind_s9_prompt_with_active_assets(
        "【参考职责】\n@图片1林默 @图片2场景 @图片3显示器 @音频1林默声线",
        slots=slots,
        assets=assets,
    )
    assert missing == []
    assert len(ordered) == 4
    assert "@图片1" in rebound and "@图片2" in rebound and "@图片3" in rebound and "@音频1" in rebound


@pytest.mark.asyncio
async def test_sqlite_store_get_beats_backfills_no_character_and_no_prop_from_s9_subshots(tmp_path):
    import json

    from novelvideo.models import NO_CHARACTER_MARKER, NO_PROP_MARKER, NovelVisualBeat
    from novelvideo.sqlite_store import SQLiteStore

    proj = tmp_path / "alice" / "backfill"
    proj.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore("alice/backfill", output_dir=str(proj), state_dir=str(proj))
    await store.initialize()
    try:
        beat = NovelVisualBeat(
            episode_number=1,
            beat_number=1,
            shot_number="shot_001",
            narration_segment="夜班守夜",
            visual_description="林默在收银台前看显示器。",
            detected_identities=[NO_CHARACTER_MARKER],
            detected_props=[NO_PROP_MARKER],
            scene_ref=None,
            s9_subshots_json=json.dumps(
                [
                    {
                        "shot_number": 1,
                        "identity_ids": ["林默_青年时期"],
                        "prop_ids": ["二手显示器"],
                        "scene_id": "老旧便利店_深夜",
                    }
                ],
                ensure_ascii=False,
            ),
            s9_segment_meta_json=json.dumps(
                {
                    "segment_asset_slots": [
                        {"slot": 1, "media_type": "image", "asset_kind": "identity", "name": "林默", "asset_id": "林默_青年时期"},
                        {"slot": 2, "media_type": "image", "asset_kind": "scene", "name": "老旧便利店", "asset_id": "老旧便利店_深夜"},
                        {"slot": 3, "media_type": "image", "asset_kind": "prop", "name": "显示器", "asset_id": "二手显示器"},
                    ]
                },
                ensure_ascii=False,
            ),
        )
        await store.add_visual_beats([beat])
        rows = await store.get_beats_as_dicts(1)
        assert len(rows) == 1
        row = rows[0]
        assert row["detected_identities"] == ["林默_青年时期"]
        assert row["detected_props"] == ["二手显示器"]
        assert (row.get("scene_ref") or {}).get("scene_id") == "老旧便利店_深夜"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_panel_service_generate_seedance2_prompt_forwards_source_text_and_syncs_beat_assets(
    tmp_path, monkeypatch
):
    from novelvideo import project_config as pc
    from novelvideo import s9_storyboard as s9_mod
    from novelvideo.models import NO_CHARACTER_MARKER, NO_PROP_MARKER, NovelEpisode
    from novelvideo.seedance2_i2v.panel_service import generate_seedance2_prompt_for_panel

    monkeypatch.setattr(pc, "OUTPUT_DIR", tmp_path / "state")
    project_dir = tmp_path / "output" / "alice" / "panel_sync"
    state_dir = tmp_path / "state" / "alice" / "panel_sync"
    project_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    captured_stage2: dict[str, object] = {}
    captured_update: dict[str, object] = {}

    class _FakeStore:
        def get_episode(self, ep_num: int):
            return NovelEpisode(
                number=ep_num,
                title="ep1",
                beat_source_text="暴雨夜，林默坐在老旧便利店收银台后敲键盘，黑衣男人进店买烟。",
            )

        async def update_beat_asset(self, **kwargs):
            captured_update.update(kwargs)
            return True

    async def _fake_stage2(**kwargs):
        captured_stage2.update(kwargs)
        return "【参考职责】\n@图片1绑定林默\n【动态时间轴】\n- 0.0秒—3.0秒：林默敲键盘"

    monkeypatch.setattr(s9_mod, "run_s9_stage2_segment_prompt", _fake_stage2)

    beat = {
        "beat_number": 1,
        "narration_segment": "林默守夜迎客。",
        "visual_description": "便利店收银台。",
        "detected_identities": [NO_CHARACTER_MARKER],
        "detected_props": [NO_PROP_MARKER],
        "scene_ref": None,
        "s9_subshots": [
            {
                "shot_number": 1,
                "planned_duration_seconds": 8.0,
                "start_seconds": 0.0,
                "end_seconds": 8.0,
                "shot_size": "中景",
                "camera_angle": "平视",
                "camera_movement": "微推",
                "content": "林默在台后敲机械键盘。",
                "identity_ids": ["林默_青年时期"],
                "scene_id": "老旧便利店_深夜",
                "prop_ids": ["机械键盘"],
            }
        ],
        "s9_segment_meta": {
            "segment_number": 1,
            "title": "雨夜守夜人",
            "planned_duration_seconds": 8,
            "story_beat": "林默守夜迎客",
            "segment_asset_slots": [
                {"slot": 1, "media_type": "image", "asset_kind": "identity", "name": "林默", "asset_id": "林默_青年时期"},
                {"slot": 2, "media_type": "image", "asset_kind": "scene", "name": "老旧便利店_深夜", "asset_id": "老旧便利店_深夜"},
                {"slot": 3, "media_type": "image", "asset_kind": "prop", "name": "机械键盘", "asset_id": "机械键盘"},
            ],
        },
    }

    await generate_seedance2_prompt_for_panel(
        store=_FakeStore(),
        state_dir=state_dir,
        episode=1,
        beat=beat,
        project_dir=project_dir,
    )

    assert "暴雨夜，林默坐在老旧便利店收银台后敲键盘" in str(captured_stage2.get("source_text") or "")
    assert captured_update.get("detected_identities") == ["林默_青年时期"]
    assert captured_update.get("detected_props") == ["机械键盘"]
    assert (captured_update.get("scene_ref") or {}).get("scene_id") == "老旧便利店_深夜"


def test_audit_s9_plan_pacing_catches_overcrowded_multi_speaker_prop_shot():
    overcrowded = S9StoryboardPlan(
        duration_assessment=S9DurationAssessment(
            pace_tier="紧凑",
            estimated_total_seconds=18,
            segment_count=1,
        ),
        segments=[
            S9SegmentPlan(
                segment_number=1,
                title="守夜交易",
                planned_duration_seconds=18,
                story_beat="林默敲代码，黑衣人买烟买冰美式并找零盯屏。",
                exit_state="黑衣人侧身盯向显示器，林默手停键盘。",
                shots=[
                    S9SubShot(
                        shot_number=1,
                        planned_duration_seconds=10.0,
                        start_seconds=0.0,
                        end_seconds=10.0,
                        audio_type="narration",
                        speaker="林默 OS",
                        spoken_text="白天是写字楼里的架构师，夜里是全网冷门接口的守夜人。只要再抓十分钟，这批异常心跳包就能完整落盘。",
                        content="林默盯着绿码屏幕敲机械键盘。",
                    ),
                    S9SubShot(
                        shot_number=2,
                        planned_duration_seconds=8.0,
                        start_seconds=10.0,
                        end_seconds=18.0,
                        audio_type="dialogue",
                        speaker="黑衣男人_青年时期；林默_青年时期",
                        spoken_text="一包薄荷烟，再拿罐冰美式。|一共三十四，找您六十六。",
                        content="黑衣人进店拍下百元钞，林默转身拿烟拿冰美式、扫码、装袋并找零，男人接过零钱侧身盯屏。",
                        prop_ids=["百元钞票", "薄荷烟", "冰美式", "零钱"],
                    ),
                ],
            )
        ],
    )
    issues = audit_s9_plan_pacing(overcrowded, max_segment_seconds=30)
    assert any("大段1-分镜2" in msg and "多人轮流开口" in msg for msg in issues)


def test_normalize_uses_integer_seconds_and_preserves_merged_exit_state():
    from novelvideo.s9_storyboard import normalize_s9_storyboard_plan

    raw = S9StoryboardPlan(
        segments=[
            S9SegmentPlan(
                segment_number=1,
                title="前段",
                planned_duration_seconds=14,
                story_beat="前半段事件",
                exit_state="旧尾帧状态",
                shots=[
                    S9SubShot(
                        shot_number=1,
                        planned_duration_seconds=10.5,
                        content="镜一内容",
                    ),
                    S9SubShot(
                        shot_number=2,
                        planned_duration_seconds=3.5,
                        content="镜二内容",
                    ),
                ],
            ),
            S9SegmentPlan(
                segment_number=2,
                title="短尾",
                planned_duration_seconds=3,
                story_beat="收束事件",
                exit_state="合并后的最终尾帧状态",
                shots=[
                    S9SubShot(
                        shot_number=1,
                        planned_duration_seconds=3.0,
                        content="镜三内容",
                    ),
                ],
            ),
        ]
    )
    plan, _ = normalize_s9_storyboard_plan(raw, max_segment_seconds=30)
    assert len(plan.segments) == 1
    seg = plan.segments[0]
    assert seg.exit_state == "合并后的最终尾帧状态"
    for s in seg.shots:
        assert s.start_seconds == float(int(s.start_seconds))
        assert s.end_seconds == float(int(s.end_seconds))


def test_fallback_prompt_generates_sub_interval_timeline_and_splits_multispeaker_audio():
    style = resolve_s9_style_context(visual_style="realistic_modern", aspect_ratio="16:9")
    seg = S9SegmentPlan(
        segment_number=1,
        title="便利店交易",
        planned_duration_seconds=8,
        story_beat="黑衣人买烟后林默找零。",
        exit_state="男人握零钱盯向显示器",
        shots=[
            S9SubShot(
                shot_number=1,
                title="点单与找零",
                planned_duration_seconds=8,
                start_seconds=0,
                end_seconds=8,
                shot_scale_movement="中景",
                camera_angle="平视",
                camera_motion="固定机位",
                audio_type="dialogue",
                speaker="黑衣男人_青年时期；林默_青年时期",
                spoken_text="一包薄荷烟。|一共二十。",
                content="男人拍下百元钞点单。林默递出薄荷烟并找零。",
                sfx_cue="钞票拍台声、硬币轻响",
            )
        ],
    )
    slots = [
        S9SegmentAssetSlot(slot=1, media_type="audio", asset_kind="voice", name="林默", asset_id="林默_青年时期"),
        S9SegmentAssetSlot(slot=2, media_type="audio", asset_kind="voice", name="黑衣男人", asset_id="黑衣男人_青年时期"),
    ]
    prompt = build_s9_stage2_fallback_prompt(segment=seg, slots=slots, style_context=style)
    assert "承接前态后在触发点直接切入核心动作与可见部位变化" not in prompt
    assert "0秒—" in prompt
    assert "@" in prompt
    assert "@音频2音色" in prompt
    assert "@音频1音色" in prompt
    assert "|" not in prompt


def test_stage2_completeness_audit_and_task_prompt_for_incomplete_and_optimize():
    from novelvideo.s9_storyboard import _audit_s9_stage2_completeness

    style = resolve_s9_style_context(visual_style="realistic_modern", aspect_ratio="16:9")
    seg = S9SegmentPlan(
        segment_number=1,
        title="试探",
        planned_duration_seconds=23,
        story_beat="林默敲代码，黑衣男人点破十六进制心跳包。",
        exit_state="黑衣男人盯着屏幕，林默手停键盘。",
        shots=[
            S9SubShot(shot_number=1, planned_duration_seconds=4, start_seconds=0, end_seconds=4, content="镜1"),
            S9SubShot(shot_number=2, planned_duration_seconds=3, start_seconds=4, end_seconds=7, content="镜2"),
            S9SubShot(shot_number=3, planned_duration_seconds=10, start_seconds=7, end_seconds=17, content="镜3"),
            S9SubShot(shot_number=4, planned_duration_seconds=6, start_seconds=17, end_seconds=23, content="镜4"),
        ],
    )
    slots = [
        S9SegmentAssetSlot(slot=1, media_type="image", asset_kind="identity", name="林默", asset_id="林默_青年"),
    ]
    truncated = (
        "【全局光影画风】\n真人电影。\n"
        "【注意事项】\n无字幕。\n"
        "分镜1（局部0秒到4秒）：林默看屏幕。\n"
        "分镜2（局部4秒到7秒）：男人开口。\n"
        "分镜3（局部7秒到17秒）：8秒—11秒，他开口，说话时嘴角带着轻笑后的余意，语"
    )
    issues = _audit_s9_stage2_completeness(
        truncated, segment=seg, slots=slots, finish_reason="length"
    )
    assert any("token 上限" in i for i in issues)
    assert any("意外中断" in i for i in issues)
    assert any("【参考职责】" in i for i in issues)
    assert any("分镜4" in i for i in issues)
    assert any("23秒" in i for i in issues)

    repair_task = build_s9_stage2_task_prompt(
        segment=seg,
        slots=slots,
        style_context=style,
        current_prompt=truncated,
    )
    assert "当前待补全与优化的提示词草稿" in repair_task
    assert "完整补齐所有缺失栏目、缺失分镜" in repair_task

    complete_prompt = (
        "【参考职责】\n@图片1绑定林默。\n"
        "【全局光影画风】\n真人电影。\n"
        "【注意事项】\n无字幕。\n"
        "分镜1（0秒到4秒）：镜1。\n"
        "分镜2（4秒到7秒）：镜2。\n"
        "分镜3（7秒到17秒）：镜3。\n"
        "分镜4（17秒到23秒）：镜4，23秒落定。"
    )
    assert _audit_s9_stage2_completeness(complete_prompt, segment=seg, slots=slots) == []
    opt_task = build_s9_stage2_task_prompt(
        segment=seg,
        slots=slots,
        style_context=style,
        current_prompt=complete_prompt,
        user_guidance="镜头：强化压迫感推镜。",
    )
    assert "AI 优化指令" in opt_task
    assert "强化压迫感推镜" in opt_task


@pytest.mark.asyncio
async def test_run_s9_stage2_uses_composer_route_max_tokens_and_continues_truncated_output(
    monkeypatch,
):
    from types import SimpleNamespace

    from novelvideo import config as cfg_mod
    from novelvideo import s9_storyboard as s9_mod

    monkeypatch.delenv("NEWAPI_TEXT_CONTEXT_TOKENS", raising=False)

    style = resolve_s9_style_context(visual_style="realistic_modern", aspect_ratio="16:9")
    seg = S9SegmentPlan(
        segment_number=1,
        title="试探",
        planned_duration_seconds=23,
        story_beat="林默敲代码，黑衣男人点破十六进制心跳包。",
        exit_state="黑衣男人盯着屏幕。",
        shots=[
            S9SubShot(shot_number=1, planned_duration_seconds=4, start_seconds=0, end_seconds=4, content="镜1"),
            S9SubShot(shot_number=2, planned_duration_seconds=3, start_seconds=4, end_seconds=7, content="镜2"),
            S9SubShot(shot_number=3, planned_duration_seconds=10, start_seconds=7, end_seconds=17, content="镜3"),
            S9SubShot(shot_number=4, planned_duration_seconds=6, start_seconds=17, end_seconds=23, content="镜4"),
        ],
    )
    slots = [
        S9SegmentAssetSlot(slot=1, media_type="image", asset_kind="identity", name="林默", asset_id="林默_青年"),
    ]

    captured_model_envs: list[str] = []
    captured_settings: list[dict] = []
    run_calls: list[dict] = []

    monkeypatch.setattr(
        cfg_mod,
        "get_newapi_text_pydantic_model",
        lambda model_env, **kwargs: captured_model_envs.append(model_env) or "dummy-model",
    )
    monkeypatch.setattr(
        cfg_mod,
        "get_newapi_structured_output_model_settings",
        lambda model_env=None: {"openai_reasoning_effort": "high"},
    )

    class _FakeAgent:
        def __init__(self, *args, **kwargs):
            captured_settings.append(dict(kwargs.get("model_settings") or {}))

        async def run(self, prompt, **kwargs):
            run_calls.append({"prompt": prompt, "has_history": "message_history" in kwargs})
            if len(run_calls) == 1:
                return SimpleNamespace(
                    output=(
                        "【参考职责】\n@图片1绑定林默。\n"
                        "【全局光影画风】\n真人电影。\n"
                        "【注意事项】\n无字幕。\n"
                        "分镜1（0秒到4秒）：林默看屏幕。\n"
                        "分镜2（4秒到7秒）：男人走近。\n"
                        "分镜3（7秒到17秒）：8秒—11秒，他开口，说话时嘴角带着轻笑后的余意，语"
                    ),
                    response=SimpleNamespace(finish_reason="length"),
                    all_messages=lambda: ["m1"],
                )
            return SimpleNamespace(
                output=(
                    "气里透着试探；11秒—17秒，男人指尖敲击桌面。\n"
                    "分镜4（17秒到23秒）：林默抬眼对视，17秒—23秒两人僵持落定。"
                ),
                response=SimpleNamespace(finish_reason="stop"),
                all_messages=lambda: ["m1", "m2"],
            )

    import pydantic_ai

    monkeypatch.setattr(pydantic_ai, "Agent", _FakeAgent)

    rendered = await s9_mod.run_s9_stage2_segment_prompt(
        segment=seg,
        slots=slots,
        style_context=style,
    )
    assert captured_model_envs == ["SEEDANCE2_PROMPT_COMPOSER_MODEL"]
    assert captured_settings[0]["max_tokens"] == 8192
    assert len(run_calls) == 2
    assert run_calls[1]["has_history"] is True
    assert "余意，语气里透着试探" in rendered
    assert "分镜4（17秒到23秒）" in rendered


@pytest.mark.asyncio
async def test_panel_service_s9_ai_optimize_forwards_current_prompt_and_template_keys(
    tmp_path, monkeypatch
):
    from novelvideo import project_config as pc
    from novelvideo import s9_storyboard as s9_mod
    from novelvideo.models import NovelEpisode
    from novelvideo.seedance2_i2v.panel_service import generate_seedance2_prompt_for_panel

    monkeypatch.setattr(pc, "OUTPUT_DIR", tmp_path / "state")
    project_dir = tmp_path / "output" / "alice" / "opt_s9"
    state_dir = tmp_path / "state" / "alice" / "opt_s9"
    project_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    captured_stage2: dict[str, object] = {}

    class _FakeStore:
        def get_episode(self, ep_num: int):
            return NovelEpisode(number=ep_num, title="ep1", beat_source_text="原稿正文")

        async def update_beat_asset(self, **kwargs):
            return True

    async def _fake_stage2(**kwargs):
        captured_stage2.update(kwargs)
        return "【参考职责】\n@图片1\n【全局光影画风】\n实拍。\n【注意事项】\n无字幕。\n分镜1（0秒到8秒）：完成。"

    monkeypatch.setattr(s9_mod, "run_s9_stage2_segment_prompt", _fake_stage2)

    beat = {
        "beat_number": 1,
        "narration_segment": "对白",
        "visual_description": "便利店",
        "s9_subshots": [
            {"shot_number": 1, "planned_duration_seconds": 8.0, "start_seconds": 0.0, "end_seconds": 8.0, "content": "镜头1"}
        ],
        "s9_segment_meta": {
            "segment_number": 1,
            "title": "段落1",
            "planned_duration_seconds": 8,
            "story_beat": "核心动作",
        },
    }

    await generate_seedance2_prompt_for_panel(
        store=_FakeStore(),
        state_dir=state_dir,
        episode=1,
        beat=beat,
        project_dir=project_dir,
        manual_prompt_reference="当前残缺提示词，语",
        prompt_guidance="增强紧张感",
        prompt_guidance_template_keys=["camera", "lighting"],
    )

    assert captured_stage2.get("current_prompt") == "当前残缺提示词，语"
    guidance = str(captured_stage2.get("user_guidance") or "")
    assert "增强紧张感" in guidance
    assert "镜头：" in guidance
    assert "光影：" in guidance


def test_seedance_2_5_prompt_norms_in_stage2_directives_and_fallback():
    s2 = build_s9_stage2_system_prompt(
        tail_anchor=False,
        include_first_frame=False,
        include_scene_layout=True,
    )
    # 1. Anti-bloat without rigid 150-280 word cap; bans camera gear & empty quality buzzwords.
    assert "150～280" not in s2
    assert "去重不压戏" in s2
    assert "三脚架" in s2 and "拍摄设备词" in s2
    assert "8K" in s2 and "稀释注意力" in s2
    # 2. Anti-subtitle rule: no repeated dialogue lines in 【角色声音】.
    assert "严禁在 `【角色声音】` 中摘录、复述" in s2
    # 3. Dual-anchor BGM synonym suppression + positive-only visual constraints.
    assert "无背景音乐、无BGM、无配乐、无器乐、无旋律、无合成器铺底音、无氛围铺底音" in s2
    assert "全程纯人声对白与现场物理拟音，无任何配乐或背景音乐" in s2
    assert "负向禁令仅用于禁字幕与无音乐/BGM" in s2
    assert "眼神自然贴合当前画风" in s2

    # Style context adapts across live_action, 3D animation, and 2D anime without leaking T2I negative ban lists.
    for style_id, expected_marker in (
        ("realistic", "写实摄影光学成像"),
        ("guoman_fantasy", "三维CG建模"),
        ("anime", "二维手绘/赛璐璐"),
    ):
        ctx = resolve_s9_style_context(visual_style=style_id, aspect_ratio="16:9")
        assert expected_marker in ctx.s9_style_anchor
        assert "器材词" in ctx.s9_style_anchor
        assert "规避项" not in ctx.s9_style_anchor
        assert "mutated hands" not in ctx.s9_style_anchor.lower()
        assert "plastic toy" not in ctx.s9_style_anchor.lower()

    # Fallback prompt mirrors the same Seedance 2.5 rules cleanly.
    style = resolve_s9_style_context(visual_style="realistic_modern", aspect_ratio="16:9")
    seg = S9SegmentPlan(
        segment_number=1,
        title="对峙",
        planned_duration_seconds=8,
        story_beat="两人隔门对峙",
        exit_state="艾莉森握紧门把直视门外",
        shots=[
            S9SubShot(
                shot_number=1,
                planned_duration_seconds=8,
                start_seconds=0,
                end_seconds=8,
                audio_type="dialogue",
                speaker="艾莉森",
                spoken_text="你没有搜查令。",
                content="艾莉森站在门内直视门外。",
            )
        ],
    )
    fallback = build_s9_stage2_fallback_prompt(segment=seg, slots=[], style_context=style)
    assert "无背景音乐、无BGM、无配乐、无器乐、无旋律、无合成器铺底音、无氛围铺底音" in fallback
    assert "全程纯人声对白与现场物理拟音，无任何配乐或背景音乐" in fallback
    assert "不要表情突变" not in fallback
    assert "人物五官、比例与身份全程稳定" in fallback


def test_normalize_orders_character_slots_by_first_appearance_for_seedance_2_5():
    from novelvideo.models import CharacterIdentity, NovelCharacter, NovelEpisode
    from novelvideo.s9_storyboard import normalize_s9_storyboard_plan

    c1 = NovelCharacter(name="艾莉森")
    c1.identities = [CharacterIdentity(identity_id="艾莉森_青年", character_name="艾莉森", identity_name="青年")]
    c2 = NovelCharacter(name="乔瑟夫")
    c2.identities = [CharacterIdentity(identity_id="乔瑟夫_中年", character_name="乔瑟夫", identity_name="中年")]
    ep = NovelEpisode(number=1, title="敲门", identity_ids=["艾莉森_青年", "乔瑟夫_中年"])

    # In Shot 1, 乔瑟夫 appears BEFORE 艾莉森 in the visual description, even though DB order
    # and raw shot.identity_ids list 艾莉森 first.
    raw = S9StoryboardPlan(
        segments=[
            S9SegmentPlan(
                segment_number=1,
                title="门外敲门",
                planned_duration_seconds=8,
                story_beat="乔瑟夫敲门，艾莉森走近门后应答。",
                shots=[
                    S9SubShot(
                        shot_number=1,
                        planned_duration_seconds=8,
                        start_seconds=0,
                        end_seconds=8,
                        content="乔瑟夫在门外大力拍门，门内的艾莉森放下咖啡杯快步走向门前。",
                        identity_ids=["艾莉森_青年", "乔瑟夫_中年"],
                    )
                ],
            )
        ]
    )
    _plan, per_seg_slots = normalize_s9_storyboard_plan(
        raw, max_segment_seconds=30, characters=[c1, c2], episode=ep
    )
    identity_slots = [s for s in per_seg_slots[0] if s.asset_kind == "identity"]
    assert [s.asset_id for s in identity_slots] == ["乔瑟夫_中年", "艾莉森_青年"]
    assert [s.slot for s in identity_slots] == [1, 2]

    # Same first-appearance order when identity_ids is omitted and inferred from prose.
    raw_inferred = S9StoryboardPlan(
        segments=[
            S9SegmentPlan(
                segment_number=1,
                title="门外敲门",
                planned_duration_seconds=8,
                story_beat="乔瑟夫敲门，艾莉森走近门后应答。",
                shots=[
                    S9SubShot(
                        shot_number=1,
                        planned_duration_seconds=8,
                        start_seconds=0,
                        end_seconds=8,
                        content="乔瑟夫在门外大力拍门，门内的艾莉森放下咖啡杯快步走向门前。",
                        identity_ids=[],
                    )
                ],
            )
        ]
    )
    _plan2, per_seg_slots2 = normalize_s9_storyboard_plan(
        raw_inferred, max_segment_seconds=30, characters=[c1, c2], episode=ep
    )
    identity_slots2 = [s for s in per_seg_slots2[0] if s.asset_kind == "identity"]
    assert [s.asset_id for s in identity_slots2] == ["乔瑟夫_中年", "艾莉森_青年"]
