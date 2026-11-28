from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from novelvideo.cognee.store import CogneeStore
from novelvideo.models import (
    CharacterIdentity,
    NovelCharacter,
    NovelEpisode,
    PropMenuItem,
    SceneMenuItem,
)
from novelvideo.s9_storyboard import (
    S9AssetManifestItem,
    S9DurationAssessment,
    S9SegmentAssetSlot,
    S9SegmentMeta,
    S9SegmentPlan,
    S9StoryboardPlan,
    S9SubShot,
    build_s9_slots_from_beat,
    compute_segment_subshots_update,
    normalize_s9_storyboard_plan,
    rebind_s9_prompt_with_active_assets,
)
from novelvideo.seedance2_i2v.assets import build_seedance2_project_assets
from novelvideo.seedance2_i2v.models import Seedance2I2VMode, parse_seedance2_config
from novelvideo.seedance2_i2v.request import build_seedance2_huimeng_params
from novelvideo.video_duration import seedance2_media_limits_for_backend


def _make_store(tmp_path: Path) -> CogneeStore:
    project_dir = tmp_path / "alice" / "s9demo"
    project_dir.mkdir(parents=True, exist_ok=True)
    store = CogneeStore.__new__(CogneeStore)
    store.project_name = "alice/s9demo"
    store.dataset_name = "novelvideo_alice_s9demo"
    store._db = None
    store._characters = {}
    store._episodes = {}
    store._alias_index = {}
    store.project_dir = str(project_dir)
    store.state_dir = str(project_dir)
    store.db_path = str(project_dir / "data.db")
    return store


def test_normalize_s9_plan_caps_duration_and_merges_short_tail():
    episode = NovelEpisode(
        number=1,
        title="雨夜",
        scene_menu=[SceneMenuItem(scene_id="便利店夜", description="雨夜便利店")],
        prop_menu=[PropMenuItem(prop_id="银色U盘", description="金属U盘")],
    )
    char = NovelCharacter(name="林默")
    char.identities = [CharacterIdentity(identity_id="林默_青年", character_name="林默", identity_name="青年")]
    characters = [char]
    raw_plan = S9StoryboardPlan(
        duration_assessment=S9DurationAssessment(
            language="普通话",
            constraint_summary="单段上限15秒",
            estimated_total_seconds=18,
            segment_count=2,
        ),
        segments=[
            S9SegmentPlan(
                segment_number=1,
                title="收银台警觉",
                planned_duration_seconds=12,
                story_beat="林默敲代码突遇推门",
                shots=[
                    S9SubShot(
                        shot_number=1,
                        title="雨夜门外",
                        planned_duration_seconds=5,
                        start_seconds=0,
                        end_seconds=5,
                        content="大雨拍打便利店玻璃。",
                        identity_ids=["林默"],
                        scene_id="便利店夜",
                        prop_ids=["银色U盘"],
                    ),
                    S9SubShot(
                        shot_number=2,
                        title="敲击代码",
                        planned_duration_seconds=7,
                        start_seconds=5,
                        end_seconds=12,
                        audio_type="dialogue",
                        speaker="林默",
                        spoken_text="抓完这个异常包就收工。",
                        content="林默盯着绿码低声自语。",
                        identity_ids=["林默"],
                        scene_id="便利店夜",
                    ),
                ],
            ),
            # Short 3s trailing segment fits cleanly inside 15s (12 + 3 == 15)
            S9SegmentPlan(
                segment_number=2,
                title="门铃响起",
                planned_duration_seconds=3,
                story_beat="门铃叮咚响",
                shots=[
                    S9SubShot(
                        shot_number=1,
                        title="门铃反应",
                        planned_duration_seconds=3,
                        start_seconds=0,
                        end_seconds=3,
                        content="林默抬眼看向门外。",
                        identity_ids=["林默"],
                    )
                ],
            ),
        ],
    )

    plan, per_seg_slots = normalize_s9_storyboard_plan(
        raw_plan,
        max_segment_seconds=15,
        characters=characters,
        episode=episode,
    )

    assert len(plan.segments) == 1
    seg = plan.segments[0]
    assert seg.planned_duration_seconds == 15
    assert seg.shots[-1].end_seconds == pytest.approx(15.0)
    assert seg.shots[0].identity_ids == ["林默_青年"]
    slots = per_seg_slots[0]
    # Ordering must be identity -> scene -> prop -> voice
    assert [(s.media_type, s.slot, s.asset_kind) for s in slots] == [
        ("image", 1, "identity"),
        ("image", 2, "scene"),
        ("image", 3, "prop"),
        ("audio", 1, "voice"),
    ]


@pytest.mark.asyncio
async def test_s9_workflow_persists_segments_duration_and_stage2_prompts_drama_and_narrated(
    tmp_path, monkeypatch
):
    from novelvideo.workflows import s9_script_writing as wf_mod

    for spine, expected_audio, expected_speaker in [
        ("drama", "dialogue", "林默_青年"),
        ("narrated", "narration", "林默 OS"),
    ]:
        store = _make_store(tmp_path / spine)
        char = NovelCharacter(
            name="林默",
            reference_audio_path="",
        )
        char.identities = [
            CharacterIdentity(
                identity_id="林默_青年",
                character_name="林默",
                identity_name="青年",
            )
        ]
        await store.add_character(char)
        ep = NovelEpisode(
            number=1,
            title="暴雨便利店",
            beat_source_text="暴雨深夜，林默在便利店写代码。",
            identity_ids=["林默_青年"],
            scene_menu=[SceneMenuItem(scene_id="深夜便利店", description="收银台与玻璃门")],
            prop_menu=[PropMenuItem(prop_id="机械键盘", description="黑色机械键盘")],
        )
        await store.add_episode(ep)

        async def fake_stage1(**kwargs):
            return S9StoryboardPlan(
                duration_assessment=S9DurationAssessment(
                    language="普通话",
                    constraint_summary="单段上限30秒",
                    core_chain_seconds="约 22 秒",
                    estimated_total_seconds=22,
                    segment_count=1,
                    conclusion="满足约束",
                ),
                asset_manifest=[
                    S9AssetManifestItem(
                        slot_label="@图片1",
                        media_type="image",
                        asset_kind="identity",
                        name="林默_青年",
                        asset_id="林默_青年",
                    )
                ],
                segments=[
                    S9SegmentPlan(
                        segment_number=1,
                        title="深夜守夜与推门",
                        planned_duration_seconds=22,
                        story_beat="林默在收银台抓包时迎来不速之客",
                        shots=[
                            S9SubShot(
                                shot_number=1,
                                title="守夜独白",
                                planned_duration_seconds=10,
                                start_seconds=0,
                                end_seconds=10,
                                shot_scale_movement="中近景",
                                camera_angle="平视",
                                camera_motion="固定机位",
                                audio_type=expected_audio,
                                speaker=expected_speaker,
                                spoken_text="白天架构师，夜里守夜人。",
                                content="林默盯着屏幕敲键盘。",
                                identity_ids=["林默_青年"],
                                scene_id="深夜便利店",
                                prop_ids=["机械键盘"],
                            ),
                            S9SubShot(
                                shot_number=2,
                                title="门外身影",
                                planned_duration_seconds=12,
                                start_seconds=10,
                                end_seconds=22,
                                shot_scale_movement="中全景",
                                camera_angle="平视",
                                camera_motion="缓慢推近",
                                audio_type="silence",
                                speaker="",
                                spoken_text="",
                                content="自动门滑开，门外雨幕涌入冷风。",
                                identity_ids=["林默_青年"],
                                scene_id="深夜便利店",
                            ),
                        ],
                    )
                ],
            )

        async def fake_stage2(**kwargs):
            return (
                "【参考职责】@图片1约束林默_青年，@图片2约束深夜便利店，@图片3约束机械键盘，@音频1为林默声线。"
                "分镜1(0-10s)：中近景，林默敲键盘说话。分镜2(10-22s)：缓慢推近门外雨夜。"
            )

        from novelvideo import s9_storyboard as s9_mod

        monkeypatch.setattr(wf_mod, "run_s9_stage1_plan", fake_stage1)
        monkeypatch.setattr(s9_mod, "run_s9_stage2_segment_prompt", fake_stage2)

        try:
            wf = wf_mod.create_s9_script_writing_workflow(
                store,
                store,
                output_dir=str(Path(store.project_dir)),
                spine_template=spine,
            )
            script = await wf.run(
                episode_num=1,
                max_segment_seconds=30,
                tail_anchor=False,
                visual_style="chinese_period_drama",
                aspect_ratio="2:3",
            )

            assert len(script.beats) == 1
            beat_model = script.beats[0]
            assert beat_model.duration_seconds == 22.0
            assert beat_model.estimated_duration == 22.0

            stored_beats = await store.get_beats_as_dicts(1)
            assert len(stored_beats) == 1
            b = stored_beats[0]
            assert b["segment_title"] == "大段1 · 深夜守夜与推门"
            assert b["estimated_duration"] == 22.0
            assert b["duration_seconds"] == 22.0
            assert len(b["s9_subshots"]) == 2
            assert b["s9_segment_meta"]["planned_duration_seconds"] == 22
            assert b["audio_type"] == expected_audio

            cfg = parse_seedance2_config(b["seedance2_config_json"])
            assert cfg.duration == 22
            assert cfg.duration_user_set is False
            assert cfg.prompt_style == "s9_segment"
            assert cfg.prompt_source == "s9_script"
            assert cfg.s9_tail_anchor is False
            assert cfg.s9_include_first_frame is False
            assert "分镜1(0-10s)" in cfg.final_prompt

            script_payload = await store.get_script_as_dict(1)
            assert script_payload["s9_preview_meta"]["max_segment_seconds"] == 30
            assert script_payload["s9_preview_meta"]["asset_manifest"][0]["slot_label"] == "@图片1"
        finally:
            await store.close()


def test_s9_rebind_compacts_missing_assets_and_toggles_first_frame(tmp_path):
    project_out = tmp_path / "proj"
    # Create character identity image + prop image + frame image; leave scene image missing!
    id_img = project_out / "assets" / "characters" / "林默" / "identities" / "青年.png"
    id_img.parent.mkdir(parents=True, exist_ok=True)
    id_img.write_bytes(b"png-id")

    prop_img = project_out / "assets" / "props" / "银色U盘" / "reference_3view.png"
    prop_img.parent.mkdir(parents=True, exist_ok=True)
    prop_img.write_bytes(b"png-prop")

    frame_img = project_out / "frames" / "ep001" / "beat_01.png"
    frame_img.parent.mkdir(parents=True, exist_ok=True)
    frame_img.write_bytes(b"png-frame")

    voice_file = project_out / "assets" / "characters" / "林默" / "voices" / "voice_default.mp3"
    voice_file.parent.mkdir(parents=True, exist_ok=True)
    voice_file.write_bytes(b"mp3-voice")

    char = SimpleNamespace(
        name="林默",
        reference_audio_path="",
        identities=[
            SimpleNamespace(
                identity_id="林默_青年",
                character_name="林默",
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
    beat = {
        "beat_number": 1,
        "audio_type": "dialogue",
        "speaker": "林默_青年",
        "detected_identities": ["林默_青年"],
        "detected_props": ["银色U盘"],
        "scene_ref": {"scene_id": "缺失便利店"},
        "frame_path": str(frame_img),
        "seedance2_config_json": json.dumps(
            {"mode": "multimodal_reference", "s9_include_first_frame": False},
            ensure_ascii=False,
        ),
        "s9_segment_meta": {
            "segment_number": 1,
            "planned_duration_seconds": 12,
            "segment_asset_slots": [
                {"slot": 1, "media_type": "image", "asset_kind": "identity", "name": "林默_青年", "asset_id": "林默_青年"},
                {"slot": 2, "media_type": "image", "asset_kind": "scene", "name": "缺失便利店", "asset_id": "缺失便利店"},
                {"slot": 3, "media_type": "image", "asset_kind": "prop", "name": "银色U盘", "asset_id": "银色U盘"},
                {"slot": 1, "media_type": "audio", "asset_kind": "voice", "name": "林默_青年", "asset_id": "林默_青年"},
            ],
        },
        "s9_subshots": [
            {
                "shot_number": 1,
                "audio_type": "dialogue",
                "speaker": "林默_青年",
                "spoken_text": "看这个。",
                "identity_ids": ["林默_青年"],
                "scene_id": "缺失便利店",
                "prop_ids": ["银色U盘"],
            }
        ],
    }
    prop_menu = [{"prop_id": "银色U盘", "reference_image_path": str(prop_img)}]
    assets = build_seedance2_project_assets(
        project_output=project_out,
        episode=1,
        beat=beat,
        mode=Seedance2I2VMode.MULTIMODAL_REFERENCE,
        state_dir=project_out,
        characters=[char],
        prop_menu=prop_menu,
    )
    # By default s9_include_first_frame=False, so first_frame must NOT be attached
    assert all(a.key != "first_frame" for a in assets)

    _subshots, _meta, slots = build_s9_slots_from_beat(beat)
    raw_prompt = (
        "【参考职责】\n"
        "@图片1维持林默造型，@图片2约束缺失便利店，@图片3保持银色U盘，@音频1为林默声线。\n"
        "分镜1：@图片1拿起@图片3看向@图片2，以@音频1开口。"
    )
    rebound_no_ff, ordered_no_ff, missing = rebind_s9_prompt_with_active_assets(
        raw_prompt,
        slots=slots,
        assets=assets,
        include_first_frame=False,
    )
    assert len(missing) == 1
    assert missing[0]["name"] == "缺失便利店"
    # Identity=@图片1, Prop (@图片3) compacted -> @图片2, zero dangling @图片3!
    assert "@图片3" not in rebound_no_ff
    assert "@图片1拿起@图片2看向缺失便利店" in rebound_no_ff
    assert len([a for a in ordered_no_ff if a.media_type == "image"]) == 2

    # Now enable include_first_frame=True
    beat_with_ff = {
        **beat,
        "seedance2_config_json": json.dumps(
            {"mode": "multimodal_reference", "s9_include_first_frame": True},
            ensure_ascii=False,
        ),
    }
    assets_with_ff = build_seedance2_project_assets(
        project_output=project_out,
        episode=1,
        beat=beat_with_ff,
        mode=Seedance2I2VMode.MULTIMODAL_REFERENCE,
        state_dir=project_out,
        characters=[char],
        prop_menu=prop_menu,
    )
    assert any(a.key == "first_frame" and a.exists for a in assets_with_ff)
    rebound_with_ff, ordered_with_ff, _ = rebind_s9_prompt_with_active_assets(
        raw_prompt,
        slots=slots,
        assets=assets_with_ff,
        include_first_frame=True,
    )
    # first_frame=@图片1, identity shifted to @图片2, prop shifted to @图片3
    assert "@图片1 绑定开篇首帧渲染图" in rebound_with_ff
    assert "@图片2拿起@图片3看向缺失便利店" in rebound_with_ff
    assert ordered_with_ff[0].key == "first_frame"


def test_seedance2_5_limits_allow_30s_30_images_10_audios():
    limits = seedance2_media_limits_for_backend("newapi_seedance-2.5")
    assert int(limits["max_duration"]) == 30
    assert int(limits["max_images"]) == 30
    assert int(limits["max_audios"]) == 10

    cfg = {
        "mode": "multimodal_reference",
        "duration": 25,
        "resolution": "720p",
        "ratio": "9:16",
        "generate_audio": True,
        "human_review": True,
        "final_prompt": "@图片12手持物品以@音频5说话，完成25秒连续镜头。",
    }
    params = build_seedance2_huimeng_params(
        cfg,
        reference_images=[f"https://example.com/img_{i}.png" for i in range(1, 13)],
        reference_audios=[f"https://example.com/aud_{i}.mp3" for i in range(1, 6)],
        max_images=int(limits["max_images"]),
        max_videos=int(limits["max_videos"]),
        max_audios=int(limits["max_audios"]),
    )
    assert params["duration"] == 25
    assert len(params["reference_images"]) == 12
    assert len(params["reference_audios"]) == 5


def test_compute_segment_subshots_update_recalculates_duration_and_metadata():
    meta = S9SegmentMeta(
        segment_number=1,
        segment_title="便利店夜哨",
        planned_duration_seconds=15,
        max_segment_seconds=30,
    )
    subshots = [
        S9SubShot(
            shot_number=1,
            title="镜一",
            planned_duration_seconds=8,
            shot_scale_movement="全景",
            camera_angle="平视",
            camera_motion="固定机位",
            content="便利店外暴雨如注。",
            audio_type="silence",
            identity_ids=["林默_青年"],
            scene_id="便利店夜",
        ),
        S9SubShot(
            shot_number=2,
            title="镜二",
            planned_duration_seconds=11,
            shot_scale_movement="近景",
            camera_angle="平视",
            camera_motion="缓慢推近",
            content="黑衣人推门说话。",
            audio_type="dialogue",
            speaker="黑衣人",
            spoken_text="来一包薄荷烟。",
            identity_ids=["黑衣人_风衣"],
        ),
    ]
    patch = compute_segment_subshots_update(
        subshots=subshots,
        existing_meta=meta,
        segment_title="便利店夜哨（修订）",
    )

    assert patch["planned_duration_seconds"] == 19
    assert patch["duration_seconds"] == 19.0
    assert patch["estimated_duration"] == 19.0
    assert patch["segment_title"] == "便利店夜哨（修订）"
    assert patch["audio_type"] == "dialogue"
    assert patch["speaker"] == "黑衣人"
    assert patch["detected_identities"] == ["林默_青年", "黑衣人_风衣"]
    saved_shots = json.loads(patch["s9_subshots_json"])
    assert saved_shots[0]["start_seconds"] == 0.0
    assert saved_shots[1]["start_seconds"] == 8.0
    assert saved_shots[1]["end_seconds"] == 19.0


@pytest.mark.asyncio
async def test_literal_workflow_delegates_to_s9_when_generation_mode_is_s9(tmp_path, monkeypatch):
    from novelvideo.workflows import literal_script_writing as lsw_mod
    from novelvideo.workflows import s9_script_writing as s9_mod

    store = _make_store(tmp_path)
    called: dict[str, object] = {}

    async def fake_s9_run(self, **kwargs):
        called.update(kwargs)
        return SimpleNamespace(beats=[], episode_number=kwargs.get("episode_num"))

    monkeypatch.setattr(s9_mod.S9ScriptWritingWorkflow, "run", fake_s9_run)

    try:
        wf = lsw_mod.LiteralScriptWritingWorkflow(store, store, output_dir=str(tmp_path))
        await wf.run(
            episode_num=2,
            generation_mode="s9",
            max_segment_seconds=15,
            tail_anchor=True,
        )
    finally:
        await store.close()

    assert called["episode_num"] == 2
    assert called["max_segment_seconds"] == 15
    assert called["tail_anchor"] is True


def test_build_seedance2_video_panel_state_exposes_s9_missing_assets_and_renumbered_prompt(
    tmp_path,
):
    from novelvideo.seedance2_i2v.panel_service import build_seedance2_video_panel_state

    project_dir = tmp_path / "proj"
    id_img = project_dir / "assets" / "characters" / "林默" / "identities" / "青年.png"
    id_img.parent.mkdir(parents=True, exist_ok=True)
    id_img.write_bytes(b"png-id")

    prop_img = project_dir / "assets" / "props" / "银色U盘" / "reference_3view.png"
    prop_img.parent.mkdir(parents=True, exist_ok=True)
    prop_img.write_bytes(b"png-prop")

    char = SimpleNamespace(
        name="林默",
        reference_audio_path="",
        identities=[
            SimpleNamespace(
                identity_id="林默_青年",
                character_name="林默",
                appearance_details="",
                costume="",
                face_prompt="",
                body_prompt="",
                turnaround_prompt="",
                turnaround_path="",
                portraits=[SimpleNamespace(view="front", image_path=str(id_img))],
                default_view="front",
                voice_sample_path="",
            )
        ],
    )
    beat = {
        "beat_number": 1,
        "segment_title": "大段1 · 雨夜便利店",
        "audio_type": "dialogue",
        "speaker": "林默_青年",
        "detected_identities": ["林默_青年"],
        "detected_props": ["银色U盘"],
        "scene_ref": {"scene_id": "缺失便利店"},
        "seedance2_config_json": json.dumps(
            {
                "mode": "multimodal_reference",
                "duration": 22,
                "duration_user_set": False,
                "prompt_source": "s9_script",
                "prompt_style": "s9_segment",
                "final_prompt": "@图片1保持林默，@图片2约束缺失便利店，@图片3拿着银色U盘。",
                "s9_include_first_frame": False,
            },
            ensure_ascii=False,
        ),
        "s9_segment_meta": {
            "segment_number": 1,
            "segment_title": "大段1 · 雨夜便利店",
            "planned_duration_seconds": 22,
            "max_segment_seconds": 30,
            "segment_asset_slots": [
                {"slot": 1, "media_type": "image", "asset_kind": "identity", "name": "林默_青年", "asset_id": "林默_青年"},
                {"slot": 2, "media_type": "image", "asset_kind": "scene", "name": "缺失便利店", "asset_id": "缺失便利店"},
                {"slot": 3, "media_type": "image", "asset_kind": "prop", "name": "银色U盘", "asset_id": "银色U盘"},
            ],
        },
        "s9_subshots": [
            {
                "shot_number": 1,
                "identity_ids": ["林默_青年"],
                "scene_id": "缺失便利店",
                "prop_ids": ["银色U盘"],
                "audio_type": "silence",
            }
        ],
    }

    state = build_seedance2_video_panel_state(
        project_dir=project_dir,
        episode=1,
        beat=beat,
        state_dir=project_dir,
        characters=[char],
        prop_menu=[{"prop_id": "银色U盘", "reference_image_path": str(prop_img)}],
    )

    assert state.duration == 22
    assert len(state.s9_missing_assets) == 1
    assert state.s9_missing_assets[0]["name"] == "缺失便利店"
    assert "@图片1保持林默" in state.s9_effective_prompt
    assert "@图片2拿着银色U盘" in state.s9_effective_prompt
    assert "@图片3" not in state.s9_effective_prompt
    assert [a.reference_label for a in state.assets if a.selected and a.media_type == "image"] == [
        "图片1",
        "图片2",
    ]


@pytest.mark.asyncio
async def test_s9_stage1_streams_and_preserves_validation_retries(monkeypatch):
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    from novelvideo import config as cfg_mod
    from novelvideo import s9_storyboard as s9_mod

    stream_calls = 0

    def _forbidden_non_stream(*_args, **_kwargs):
        raise AssertionError("非流式 request() 不应再被 Stage 1 调用")

    valid_plan = S9StoryboardPlan(
        segments=[
            S9SegmentPlan(
                segment_number=1,
                title="雨夜试探",
                planned_duration_seconds=3,
                story_beat="林默敲下最后一行代码。",
                exit_state="林默抬头看向门口。",
                shots=[
                    S9SubShot(
                        shot_number=1,
                        planned_duration_seconds=3,
                        start_seconds=0,
                        end_seconds=3,
                        audio_type="dialogue",
                        speaker="林默",
                        spoken_text="来得刚好。",
                        content="林默敲击键盘后抬眼看向门口。",
                    )
                ],
            )
        ]
    )

    async def _stream_plan(_messages, _info):
        nonlocal stream_calls
        stream_calls += 1
        if stream_calls == 1:
            # First streamed attempt fails schema validation to prove retries=3 still works.
            yield {0: DeltaToolCall(name="final_result", json_args='{"segments": "bad"}')}
            return
        payload = valid_plan.model_dump_json(ensure_ascii=False)
        mid = len(payload) // 2
        yield {0: DeltaToolCall(name="final_result", json_args=payload[:mid])}
        yield {0: DeltaToolCall(json_args=payload[mid:])}

    streaming_model = FunctionModel(
        function=_forbidden_non_stream,
        stream_function=_stream_plan,
    )
    monkeypatch.setattr(
        cfg_mod,
        "get_newapi_text_pydantic_model",
        lambda *_args, **_kwargs: streaming_model,
    )
    monkeypatch.setattr(
        cfg_mod,
        "get_newapi_structured_output_model_settings",
        lambda *_args, **_kwargs: {},
    )

    plan = await s9_mod.run_s9_stage1_plan(
        source_text="雨夜，林默敲下最后一行代码，抬头说道：来得刚好。",
        episode_num=1,
        episode_title="ep1",
        style_context=s9_mod.resolve_s9_style_context(
            visual_style="chinese_period_drama",
            aspect_ratio="2:3",
        ),
        characters=[],
        episode=SimpleNamespace(number=1, title="ep1"),
        voice_available_names=set(),
        max_segment_seconds=30,
    )

    assert stream_calls == 2
    assert len(plan.segments) == 1
    assert plan.segments[0].title == "雨夜试探"


@pytest.mark.asyncio
async def test_s9_stage2_streams_prompt_instead_of_blocking_request(monkeypatch):
    from pydantic_ai.models.function import FunctionModel

    from novelvideo import config as cfg_mod
    from novelvideo import s9_storyboard as s9_mod

    stream_calls = 0

    def _forbidden_non_stream(*_args, **_kwargs):
        raise AssertionError("非流式 request() 不应再被 Stage 2 调用")

    async def _stream_prompt(_messages, _info):
        nonlocal stream_calls
        stream_calls += 1
        yield "【参考职责】\n@图片1绑定林默。\n"
        yield "【全局光影画风】\n真人电影。\n"
        yield "【注意事项】\n无字幕。\n"
        yield "分镜1（0秒到4秒）：0秒—4秒，林默看向门口。"

    streaming_model = FunctionModel(
        function=_forbidden_non_stream,
        stream_function=_stream_prompt,
    )
    monkeypatch.setattr(
        cfg_mod,
        "get_newapi_text_pydantic_model",
        lambda *_args, **_kwargs: streaming_model,
    )
    monkeypatch.setattr(
        cfg_mod,
        "get_newapi_structured_output_model_settings",
        lambda *_args, **_kwargs: {},
    )

    seg = S9SegmentPlan(
        segment_number=1,
        title="试探",
        planned_duration_seconds=4,
        story_beat="林默看向门口。",
        exit_state="门外人影停步。",
        shots=[
            S9SubShot(
                shot_number=1,
                planned_duration_seconds=4,
                start_seconds=0,
                end_seconds=4,
                content="林默看向门口。",
            )
        ],
    )
    slots = [
        S9SegmentAssetSlot(
            slot=1,
            media_type="image",
            asset_kind="identity",
            name="林默",
            asset_id="林默_青年",
        )
    ]
    rendered = await s9_mod.run_s9_stage2_segment_prompt(
        segment=seg,
        slots=slots,
        style_context=s9_mod.resolve_s9_style_context(
            visual_style="chinese_period_drama",
            aspect_ratio="2:3",
        ),
    )

    assert stream_calls == 1
    assert "【参考职责】" in rendered
    assert "分镜1（0秒到4秒）" in rendered


def test_s9_scene_dedup_and_reverse_master_inclusion(tmp_path):
    from novelvideo.s9_storyboard import resolve_s9_active_assets_and_prompt

    project_dir = tmp_path / "proj"
    base_scene_dir = project_dir / "assets" / "scenes" / "乔瑟夫家"
    variant_scene_dir = project_dir / "assets" / "scenes" / "乔瑟夫家_空枪套版"
    base_scene_dir.mkdir(parents=True, exist_ok=True)
    variant_scene_dir.mkdir(parents=True, exist_ok=True)
    (base_scene_dir / "master.png").write_bytes(b"base-master")
    (base_scene_dir / "reverse_master.png").write_bytes(b"base-reverse")
    (variant_scene_dir / "master.png").write_bytes(b"variant-master")

    prop_img = project_dir / "assets" / "props" / "公文包" / "reference_3view.png"
    prop_img.parent.mkdir(parents=True, exist_ok=True)
    prop_img.write_bytes(b"prop-briefcase")

    beat = {
        "beat_number": 1,
        "visual_description": "乔瑟夫起身走到玄关拉开入户门，门外艾莉森抱着公文包。",
        "scene_ref": {"scene_id": "乔瑟夫家", "variant_id": "空枪套版"},
        "detected_props": ["公文包"],
        "s9_subshots": [
            {
                "shot_number": 1,
                "scene_id": "乔瑟夫家",
                "scene_variant_id": "",
                "prop_ids": ["公文包"],
                "content": "乔瑟夫走到入户门前开门。",
            }
        ],
        "s9_segment_meta": {
            "segment_number": 1,
            "planned_duration_seconds": 15,
            "segment_asset_slots": [
                {"slot": 1, "media_type": "image", "asset_kind": "scene", "name": "乔瑟夫家", "asset_id": "乔瑟夫家", "variant_id": "空枪套版"},
                {"slot": 2, "media_type": "image", "asset_kind": "scene_reverse", "name": "乔瑟夫家背面", "asset_id": "乔瑟夫家", "variant_id": "空枪套版"},
                {"slot": 3, "media_type": "image", "asset_kind": "prop", "name": "公文包", "asset_id": "公文包"},
            ],
        },
    }

    assets = build_seedance2_project_assets(
        project_output=project_dir,
        episode=2,
        beat=beat,
        mode=Seedance2I2VMode.MULTIMODAL_REFERENCE,
        state_dir=project_dir,
        prop_menu=[{"prop_id": "公文包", "reference_image_path": str(prop_img)}],
    )
    image_assets = [a for a in assets if a.media_type == "image" and a.selected]
    # Base master must NOT be emitted as a duplicate alongside variant master; reverse_master IS included.
    assert [a.key for a in image_assets] == [
        "scene:乔瑟夫家_空枪套版",
        "scene_reverse:乔瑟夫家_空枪套版",
        "prop:公文包",
    ]
    assert image_assets[0].path == variant_scene_dir / "master.png"
    assert image_assets[1].path == base_scene_dir / "reverse_master.png"

    prompt, renumbered, missing = resolve_s9_active_assets_and_prompt(
        beat,
        assets=assets,
        prompt="@图片1为客厅正面，@图片2为入户门背面，@图片3为公文包。",
        include_first_frame=False,
    )
    assert missing == []
    assert [a.reference_label for a in renumbered] == ["图片1", "图片2", "图片3"]
    assert "@图片1为客厅正面，@图片2为入户门背面，@图片3为公文包。" == prompt


def test_rebind_duplicate_scene_slots_does_not_shift_prop_indices(tmp_path):
    scene_img = tmp_path / "scene.png"
    prop_img = tmp_path / "prop.png"
    scene_img.write_bytes(b"scene")
    prop_img.write_bytes(b"prop")

    assets = [
        SimpleNamespace(
            key="scene:客厅_变体",
            label="场景锚点 · 客厅_变体",
            media_type="image",
            path=scene_img,
            exists=True,
            selected=True,
            reference_label="图片1",
        ),
        SimpleNamespace(
            key="prop:公文包",
            label="全局道具 · 公文包",
            media_type="image",
            path=prop_img,
            exists=True,
            selected=True,
            reference_label="图片2",
        ),
    ]
    slots = [
        S9SegmentAssetSlot(slot=1, media_type="image", asset_kind="scene", name="客厅", asset_id="客厅"),
        S9SegmentAssetSlot(slot=2, media_type="image", asset_kind="scene", name="客厅(变体)", asset_id="客厅", variant_id="变体"),
        S9SegmentAssetSlot(slot=3, media_type="image", asset_kind="prop", name="公文包", asset_id="公文包"),
    ]
    rebound, ordered, _missing = rebind_s9_prompt_with_active_assets(
        "参考@图片1和@图片2，拿着@图片3（未绑定@图片9保留）。",
        slots=slots,
        assets=assets,
    )
    assert len(ordered) == 2
    assert "参考@图片1和@图片1，拿着@图片2（未绑定@图片9保留）。" == rebound


@pytest.mark.asyncio
async def test_s9_panel_prompt_regen_injects_consistency_and_syncs_segment_slots(
    tmp_path, monkeypatch
):
    from novelvideo import s9_storyboard as s9_mod
    from novelvideo.models import NovelScene
    from novelvideo.seedance2_i2v import panel_service

    project_dir = tmp_path / "proj"
    scene_dir = project_dir / "assets" / "scenes" / "乔瑟夫家_空枪套版"
    scene_dir.mkdir(parents=True, exist_ok=True)
    (scene_dir / "master.png").write_bytes(b"scene")
    prop_dir = project_dir / "assets" / "props" / "公文包"
    prop_dir.mkdir(parents=True, exist_ok=True)
    (prop_dir / "reference_3view.png").write_bytes(b"prop")

    captured: dict[str, object] = {}

    async def fake_stage2(**kwargs):
        captured.update(kwargs)
        return "【参考职责】\n@图片1场景，@图片2公文包。\n【全局光影画风】\n黄昏。\n【注意事项】\n无。\n分镜1（0秒到8秒）：桌面上放公文包。"

    monkeypatch.setattr(s9_mod, "run_s9_stage2_segment_prompt", fake_stage2)

    beat = {
        "beat_number": 2,
        "segment_title": "大段2 · 对峙",
        "audio_type": "silence",
        "scene_ref": {"scene_id": "乔瑟夫家", "variant_id": "空枪套版"},
        "detected_props": ["公文包"],
        "s9_subshots": [
            {
                "shot_number": 1,
                "planned_duration_seconds": 8,
                "start_seconds": 0,
                "end_seconds": 8,
                "scene_id": "乔瑟夫家",
                "scene_variant_id": "空枪套版",
                "prop_ids": ["公文包"],
                "content": "两人隔桌对坐。",
            }
        ],
        "s9_segment_meta_json": json.dumps(
            {
                "segment_number": 2,
                "planned_duration_seconds": 8,
                "segment_asset_slots": [
                    {"slot": 1, "media_type": "image", "asset_kind": "scene", "name": "乔瑟夫家", "asset_id": "乔瑟夫家"},
                    {"slot": 2, "media_type": "image", "asset_kind": "scene", "name": "多余旧槽", "asset_id": "乔瑟夫家"},
                    {"slot": 3, "media_type": "image", "asset_kind": "prop", "name": "公文包", "asset_id": "公文包"},
                ],
            },
            ensure_ascii=False,
        ),
        "seedance2_config_json": json.dumps(
            {"mode": "multimodal_reference", "prompt_source": "s9_script", "prompt_style": "s9_segment"},
            ensure_ascii=False,
        ),
    }

    updated_calls = []

    class _StubStore:
        async def list_scenes(self):
            return [
                NovelScene(
                    name="乔瑟夫家_空枪套版",
                    base_scene_id="乔瑟夫家",
                    variant_id="空枪套版",
                    environment_prompt="黄昏斜阳穿过右侧百叶窗",
                    notes="背面：reverse方向为入户门与玄关，大门在右侧",
                )
            ]

        def get_episode(self, _ep):
            return None

        async def update_beat_asset(self, **kwargs):
            updated_calls.append(kwargs)
            return True

    await panel_service.generate_seedance2_prompt_for_panel(
        store=_StubStore(),
        state_dir=project_dir,
        episode=2,
        beat=beat,
        project_dir=project_dir,
        prev_beat={"beat_number": 1},
        prop_menu=[{"prop_id": "公文包", "description": "深棕色硬质皮革公文包"}],
    )

    notes = str(captured.get("scene_context_notes") or "")
    assert "锁图光" in notes
    assert "黄昏斜阳穿过右侧百叶窗" in notes
    assert "深棕色硬质皮革公文包" in notes
    assert "关键道具可见性铁律" in notes

    assert updated_calls
    saved_meta = json.loads(updated_calls[-1]["s9_segment_meta_json"])
    assert [(s["slot"], s["asset_kind"]) for s in saved_meta["segment_asset_slots"]] == [
        (1, "scene"),
        (2, "prop"),
    ]


@pytest.mark.asyncio
async def test_s9_same_scene_segments_inherit_lighting_style_anchor(monkeypatch):
    from novelvideo import s9_storyboard as s9_mod

    anchors_by_seg: dict[int, str] = {}

    async def fake_stage2(**kwargs):
        seg = kwargs["segment"]
        anchors_by_seg[seg.segment_number] = str(kwargs.get("continuity_anchor") or "")
        return (
            "【参考职责】\n@图片1 场景。\n\n"
            "【全局光影画风】\n"
            "- 视觉风格：真人电影实拍。\n"
            "- 全局主光：低角度黄昏斜阳穿透右侧百叶窗。\n"
            "- 色彩与材质：暖金琥珀高光配冷青灰阴影。\n\n"
            "【注意事项】\n无字幕。\n\n"
            "分镜1（0秒到8秒）：镜头稳定推进。"
        )

    monkeypatch.setattr(s9_mod, "run_s9_stage2_segment_prompt", fake_stage2)

    seg1 = S9SegmentPlan(
        segment_number=1,
        title="入场",
        planned_duration_seconds=8,
        story_beat="推门入室",
        shots=[S9SubShot(shot_number=1, planned_duration_seconds=8, start_seconds=0, end_seconds=8, content="推门")],
    )
    seg2 = S9SegmentPlan(
        segment_number=2,
        title="对坐",
        planned_duration_seconds=8,
        story_beat="对坐交谈",
        shots=[S9SubShot(shot_number=1, planned_duration_seconds=8, start_seconds=0, end_seconds=8, content="对坐")],
    )
    slots_1 = [S9SegmentAssetSlot(slot=1, media_type="image", asset_kind="scene", name="乔瑟夫家", asset_id="乔瑟夫家", variant_id="空枪套版")]
    slots_2 = [S9SegmentAssetSlot(slot=1, media_type="image", asset_kind="scene", name="乔瑟夫家", asset_id="乔瑟夫家", variant_id="空枪套版")]

    outputs = await s9_mod.generate_all_s9_segment_prompts(
        plan=S9StoryboardPlan(segments=[seg1, seg2]),
        per_segment_slots=[slots_1, slots_2],
        style_context=s9_mod.resolve_s9_style_context(visual_style="chinese_period_drama", aspect_ratio="16:9"),
    )

    assert len(outputs) == 2
    assert anchors_by_seg[1] == ""
    assert "低角度黄昏斜阳穿透右侧百叶窗" in anchors_by_seg[2]
    assert "禁止改变" in anchors_by_seg[2]

    task_prompt = s9_mod.build_s9_stage2_task_prompt(
        segment=seg2,
        slots=slots_2,
        style_context=s9_mod.resolve_s9_style_context(visual_style="chinese_period_drama", aspect_ratio="16:9"),
        current_prompt="【参考职责】\n- ：绑定旧稿。\n【全局光影画风】\n吊灯深夜。\n【注意事项】\n无。\n分镜1（0秒到8秒）：旧稿。",
    )
    assert "强制纠偏旧稿冲突" in task_prompt


def test_s9_derived_scene_inherits_base_layout_and_avoids_duplicate_variant_suffix():
    from novelvideo.models import NovelScene
    from novelvideo.s9_storyboard import (
        _format_planned_assets_summary,
        build_s9_scene_prop_consistency_notes,
    )

    base_scene = NovelScene(
        name="艾莉森公寓客厅",
        environment_prompt="正面：开放式厨房吧台；左侧：两扇竖向窗；右侧：灰色布艺三人沙发和矮木茶几；背面：深色实木入户前门。",
    )
    messy_scene = NovelScene(
        name="艾莉森公寓客厅_狼藉版",
        base_scene_id="艾莉森公寓客厅",
        variant_id="狼藉版",
        variant_prompt="客厅一片混乱：茶几上堆满卷饼包装纸、空啤酒瓶，笔记本电脑亮着地下赌局页面。",
    )
    ep = SimpleNamespace(
        identity_ids=[],
        identity_default_map_json="",
        scene_menu=[
            SimpleNamespace(scene_id="艾莉森公寓客厅_狼藉版", variant_id="狼藉版", description="", notes=""),
        ],
        prop_menu=[],
    )

    summary = _format_planned_assets_summary(
        characters=[],
        episode=ep,
        voice_available_names=set(),
        scenes=[base_scene, messy_scene],
    )
    assert "艾莉森公寓客厅_狼藉版_狼藉版" not in summary
    assert "正面：开放式厨房吧台" in summary
    assert "背面：深色实木入户前门" in summary
    assert "客厅一片混乱" in summary

    slots = [
        S9SegmentAssetSlot(
            slot=1,
            media_type="image",
            asset_kind="scene",
            name="艾莉森公寓客厅_狼藉版",
            asset_id="艾莉森公寓客厅_狼藉版",
            variant_id="狼藉版",
        )
    ]
    notes = build_s9_scene_prop_consistency_notes(
        slots=slots,
        scenes=[base_scene, messy_scene],
    )
    assert "艾莉森公寓客厅_狼藉版_狼藉版" not in notes
    assert "正面：开放式厨房吧台" in notes
    assert "背面：深色实木入户前门" in notes
    assert "客厅一片混乱" in notes


def test_s9_normalize_skips_dialogue_only_character_and_recovers_global_prop_and_reverse_slot(tmp_path):
    from novelvideo.models import NovelProp

    rev_path = tmp_path / "assets" / "scenes" / "艾莉森公寓客厅_狼藉版" / "reverse_master.png"
    rev_path.parent.mkdir(parents=True, exist_ok=True)
    rev_path.write_bytes(b"PNG")

    chars = [
        SimpleNamespace(
            name="艾莉森",
            aliases=[],
            identities=[SimpleNamespace(identity_id="艾莉森_律师时期")],
            reference_audio_path="",
        ),
        SimpleNamespace(
            name="达里尔",
            aliases=[],
            identities=[SimpleNamespace(identity_id="达里尔_青年时期")],
            reference_audio_path="",
        ),
        SimpleNamespace(
            name="乔瑟夫",
            aliases=["叔叔"],
            identities=[SimpleNamespace(identity_id="乔瑟夫_值勤时期")],
            reference_audio_path="",
        ),
    ]
    ep = SimpleNamespace(
        identity_ids=["艾莉森_律师时期", "达里尔_青年时期", "乔瑟夫_值勤时期"],
        identity_default_map_json="",
        scene_menu=[
            SimpleNamespace(scene_id="艾莉森公寓客厅", variant_id=""),
            SimpleNamespace(scene_id="艾莉森公寓客厅_狼藉版", variant_id="狼藉版"),
        ],
        prop_menu=[SimpleNamespace(prop_id="啤酒瓶")],
    )
    global_props = [NovelProp(name="公文包", description="深棕色皮质公文包")]

    raw_plan = S9StoryboardPlan(
        segments=[
            S9SegmentPlan(
                segment_number=1,
                title="推门对峙",
                planned_duration_seconds=10,
                story_beat="艾莉森推门而入质问达里尔",
                shots=[
                    S9SubShot(
                        shot_number=1,
                        planned_duration_seconds=5,
                        start_seconds=0,
                        end_seconds=5,
                        content="大门由外向内推开，艾莉森右手紧扣公文包迈入门槛。",
                        identity_ids=["艾莉森_律师时期"],
                        scene_id="艾莉森公寓客厅",
                        prop_ids=[],
                        audio_type="silence",
                    ),
                    S9SubShot(
                        shot_number=2,
                        planned_duration_seconds=5,
                        start_seconds=5,
                        end_seconds=10,
                        content="艾莉森把公文包平放茶几，达里尔手握啤酒瓶冷笑。",
                        identity_ids=["艾莉森_律师时期", "达里尔_青年时期"],
                        scene_id="艾莉森公寓客厅_狼藉版",
                        scene_variant_id="狼藉版",
                        prop_ids=["啤酒瓶"],
                        audio_type="dialogue",
                        speaker="艾莉森_律师时期",
                        spoken_text="这些都没关系，但我叔叔的枪，不行。",
                    ),
                ],
            )
        ]
    )

    plan, per_seg_slots = normalize_s9_storyboard_plan(
        raw_plan,
        characters=chars,
        episode=ep,
        props=global_props,
        output_dir=tmp_path,
    )
    slots = per_seg_slots[0]
    slot_summary = [(s.asset_kind, s.asset_id, s.variant_id) for s in slots]

    # Dialogue mention of "叔叔" must NOT drag 乔瑟夫_值勤时期 into on-screen identity slots
    assert ("identity", "乔瑟夫_值勤时期", "") not in slot_summary
    # Base scene + messy variant in the same segment collapse to the messy variant only (no redundant variant_id)
    scene_slots = [s for s in slots if s.asset_kind == "scene"]
    assert len(scene_slots) == 1
    assert scene_slots[0].asset_id == "艾莉森公寓客厅_狼藉版"
    assert scene_slots[0].variant_id == ""
    # Reverse master is included in segment slots when output_dir has reverse_master.png & door cue matches
    assert any(s.asset_kind == "scene_reverse" and s.asset_id == "艾莉森公寓客厅_狼藉版" for s in slots)
    # Cross-episode global prop "公文包" mentioned in visual shot content is recovered automatically
    assert any(s.asset_kind == "prop" and s.asset_id == "公文包" for s in slots)
    assert plan.segments[0].shots[1].scene_variant_id == ""


def test_rebind_s9_slot_matching_exact_before_prefix_substring(tmp_path):
    clean_path = tmp_path / "clean.png"
    messy_path = tmp_path / "messy.png"
    car_path = tmp_path / "car.png"
    for p in (clean_path, messy_path, car_path):
        p.write_bytes(b"PNG")

    slots = [
        S9SegmentAssetSlot(slot=1, media_type="image", asset_kind="scene", name="艾莉森公寓客厅", asset_id="艾莉森公寓客厅"),
        S9SegmentAssetSlot(slot=2, media_type="image", asset_kind="scene", name="艾莉森公寓客厅_狼藉版", asset_id="艾莉森公寓客厅_狼藉版"),
        S9SegmentAssetSlot(slot=3, media_type="image", asset_kind="prop", name="警车", asset_id="警车"),
    ]
    assets = [
        SimpleNamespace(key="scene:艾莉森公寓客厅", category="scene", label="场景锚点 · 艾莉森公寓客厅", media_type="image", exists=True, selected=True, path=str(clean_path), url=None),
        SimpleNamespace(key="scene:艾莉森公寓客厅_狼藉版", category="scene", label="场景锚点 · 艾莉森公寓客厅_狼藉版", media_type="image", exists=True, selected=True, path=str(messy_path), url=None),
        SimpleNamespace(key="prop:警车", category="prop", label="全局道具 · 警车", media_type="image", exists=True, selected=True, path=str(car_path), url=None),
    ]

    rebound, ordered, missing = rebind_s9_prompt_with_active_assets(
        "镜头内 @图片1 与 @图片2 为客厅，@图片3 警车急刹。",
        slots=slots,
        assets=assets,
    )
    assert not missing
    assert [a.key for a in ordered] == ["scene:艾莉森公寓客厅", "scene:艾莉森公寓客厅_狼藉版", "prop:警车"]
    assert "@图片3 警车急刹" in rebound


@pytest.mark.asyncio
async def test_s9_lighting_inheritance_groups_base_and_variant_scenes(monkeypatch):
    from novelvideo import s9_storyboard as s9_mod

    anchors_by_seg: dict[int, str] = {}

    async def fake_stage2(**kwargs):
        seg = kwargs["segment"]
        anchors_by_seg[seg.segment_number] = str(kwargs.get("continuity_anchor") or "")
        return (
            "【参考职责】\n@图片1 场景。\n\n"
            "【全局光影画风】\n"
            "- 全局主光：严格锁定暗调冷蓝笔记本荧光。\n\n"
            "【注意事项】\n无字幕。\n\n"
            "分镜1（0秒到8秒）：镜头稳定。"
        )

    monkeypatch.setattr(s9_mod, "run_s9_stage2_segment_prompt", fake_stage2)

    seg1 = S9SegmentPlan(
        segment_number=1,
        title="段1",
        planned_duration_seconds=8,
        story_beat="客厅对峙",
        shots=[S9SubShot(shot_number=1, planned_duration_seconds=8, start_seconds=0, end_seconds=8, content="对峙")],
    )
    seg2 = S9SegmentPlan(
        segment_number=2,
        title="段2",
        planned_duration_seconds=8,
        story_beat="破门而入",
        shots=[S9SubShot(shot_number=1, planned_duration_seconds=8, start_seconds=0, end_seconds=8, content="破门")],
    )
    slots_1 = [S9SegmentAssetSlot(slot=1, media_type="image", asset_kind="scene", name="艾莉森公寓客厅_狼藉版", asset_id="艾莉森公寓客厅_狼藉版")]
    slots_2 = [S9SegmentAssetSlot(slot=1, media_type="image", asset_kind="scene", name="艾莉森公寓客厅", asset_id="艾莉森公寓客厅")]

    await s9_mod.generate_all_s9_segment_prompts(
        plan=S9StoryboardPlan(segments=[seg1, seg2]),
        per_segment_slots=[slots_1, slots_2],
        style_context=s9_mod.resolve_s9_style_context(visual_style="chinese_period_drama", aspect_ratio="16:9"),
    )
    assert anchors_by_seg[1] == ""
    assert "暗调冷蓝笔记本荧光" in anchors_by_seg[2]


def test_create_script_writing_workflow_forwards_explicit_output_dir():
    from novelvideo.workflows.script_writing import create_script_writing_workflow

    stub_store = SimpleNamespace(state_dir="/tmp/state/u/p")
    wf = create_script_writing_workflow(stub_store, output_dir="/tmp/output/u/p")
    assert wf.output_dir == "/tmp/output/u/p"


def test_s9_strips_hallucinated_scene_reverse_suffix_and_exposes_project_scenes(tmp_path):
    from novelvideo.models import NovelScene
    from novelvideo.s9_storyboard import _format_planned_assets_summary

    rev_path = tmp_path / "assets" / "scenes" / "艾莉森公寓客厅_狼藉版" / "reverse_master.png"
    rev_path.parent.mkdir(parents=True, exist_ok=True)
    rev_path.write_bytes(b"PNG")

    scenes = [
        NovelScene(name="艾莉森公寓客厅", environment_prompt="正面厨房，背面入户门"),
        NovelScene(
            name="艾莉森公寓客厅_狼藉版",
            base_scene_id="艾莉森公寓客厅",
            variant_id="狼藉版",
            variant_prompt="茶几狼藉",
        ),
        NovelScene(name="艾莉森公寓楼外街道", environment_prompt="多层公寓楼外马路"),
    ]
    ep = SimpleNamespace(
        identity_ids=[],
        identity_default_map_json="",
        scene_menu=[SimpleNamespace(scene_id="艾莉森公寓客厅_狼藉版", variant_id="狼藉版")],
        prop_menu=[],
    )

    summary = _format_planned_assets_summary(
        characters=[],
        episode=ep,
        voice_available_names=set(),
        scenes=scenes,
        output_dir=tmp_path,
    )
    assert "严禁自行拼接 _reverse 或 _scene_reverse" in summary
    assert "艾莉森公寓楼外街道" in summary
    assert "【项目已有场景库" in summary

    raw_plan = S9StoryboardPlan(
        segments=[
            S9SegmentPlan(
                segment_number=1,
                title="破门",
                planned_duration_seconds=10,
                story_beat="警车赶到后破门而入",
                shots=[
                    S9SubShot(
                        shot_number=1,
                        planned_duration_seconds=4,
                        start_seconds=0,
                        end_seconds=4,
                        content="警车在公寓楼外街道急刹停稳。",
                        scene_id="艾莉森公寓楼外街道",
                    ),
                    S9SubShot(
                        shot_number=2,
                        planned_duration_seconds=3,
                        start_seconds=4,
                        end_seconds=7,
                        content="大门被撞开，乔瑟夫冲进玄关。",
                        scene_id="艾莉森公寓客厅_scene_reverse",
                        scene_variant_id="reverse",
                    ),
                    S9SubShot(
                        shot_number=3,
                        planned_duration_seconds=3,
                        start_seconds=7,
                        end_seconds=10,
                        content="客厅茶几一片狼藉。",
                        scene_id="艾莉森公寓客厅_狼藉版",
                        scene_variant_id="狼藉版",
                    ),
                ],
            )
        ]
    )
    plan, per_seg_slots = normalize_s9_storyboard_plan(
        raw_plan,
        episode=ep,
        scenes=scenes,
        output_dir=tmp_path,
    )
    slots = per_seg_slots[0]
    scene_slots = [(s.asset_kind, s.asset_id, s.variant_id) for s in slots if s.asset_kind in {"scene", "scene_reverse"}]
    # Outdoor street kept + messy living room kept (front+reverse), NO hallucinated _scene_reverse or duplicate clean base!
    assert scene_slots == [
        ("scene", "艾莉森公寓楼外街道", ""),
        ("scene", "艾莉森公寓客厅_狼藉版", ""),
        ("scene_reverse", "艾莉森公寓客厅_狼藉版", ""),
    ]
    assert plan.segments[0].shots[1].scene_id == "艾莉森公寓客厅_狼藉版"
    assert plan.segments[0].shots[1].scene_variant_id == ""


def test_s9_sanitize_lighting_anchor_and_negated_reverse_and_image_before_audio(tmp_path):
    from novelvideo.s9_storyboard import (
        sanitize_s9_lighting_anchor,
        text_uses_reverse_scene_view,
    )

    sanitized = sanitize_s9_lighting_anchor(
        "【全局光影画风】\n全局主光：严格锁定@图片3、@图片4的夜间室内色温，主光沿用 @图片6。"
    )
    assert "@图片3" not in sanitized
    assert "@图片4" not in sanitized
    assert "@图片6" not in sanitized
    assert "本段场景参考图" in sanitized

    assert text_uses_reverse_scene_view("客厅入户门在参考图既有位置，本段不开启。") is False
    assert text_uses_reverse_scene_view("大门由外向内推开，艾莉森迈入门槛。") is True

    img1 = tmp_path / "img1.png"
    img2 = tmp_path / "img2.png"
    aud1 = tmp_path / "aud1.mp3"
    for p in (img1, img2, aud1):
        p.write_bytes(b"DATA")
    slots = [
        S9SegmentAssetSlot(slot=1, media_type="image", asset_kind="scene", name="客厅", asset_id="客厅"),
        S9SegmentAssetSlot(slot=1, media_type="audio", asset_kind="voice", name="艾莉森", asset_id="艾莉森"),
    ]
    assets = [
        SimpleNamespace(key="scene:客厅", category="scene", label="场景 · 客厅", media_type="image", exists=True, selected=True, path=str(img1), url=None),
        SimpleNamespace(key="audio:艾莉森", category="audio", label="艾莉森声线", media_type="audio", exists=True, selected=True, path=str(aud1), url=None),
        SimpleNamespace(key="prop:公文包", category="prop", label="道具 · 公文包", media_type="image", exists=True, selected=True, path=str(img2), url=None),
    ]
    _prompt, ordered, _missing = rebind_s9_prompt_with_active_assets("测试", slots=slots, assets=assets)
    # Extra trailing image must appear BEFORE audios, never after audio
    assert [a.media_type for a in ordered] == ["image", "image", "audio"]


def test_s9_source_slicer_zero_drop_and_boundary_context():
    from novelvideo.s9_storyboard import (
        S9Stage1SkeletonSegment,
        build_s9_context_slice_from_episode_text,
        slice_s9_source_text_for_segments,
    )

    lines = [
        "第1行：雨夜便利店霓虹闪烁。",
        "第2行：陈默擦拭柜台，神色冷峻。",
        "第3行：玻璃门铃突然作响。",
        "第4行：未经引用的过渡动作线。",
        "第5行：黑衣男子压低帽檐走入室内。",
        "第6行：两人目光在收银台前短暂交锋。",
        "第7行：对方丢下一枚带泥渍的铜钥匙。",
        "第8行：陈默缓缓伸手按住柜台上的钥匙。",
    ]
    full_text = "\n".join(lines)
    skeletons = [
        S9Stage1SkeletonSegment(
            segment_number=1,
            title="门铃骤响",
            source_excerpt="第1行：雨夜便利店霓虹闪烁。\n第3行：玻璃门铃突然作响。",
        ),
        S9Stage1SkeletonSegment(
            segment_number=2,
            title="钥匙试探",
            source_excerpt="第5行：黑衣男子压低帽檐走入室内。\n第8行：陈默缓缓伸手按住柜台上的钥匙。",
        ),
    ]
    excerpts, slices = slice_s9_source_text_for_segments(full_text, skeletons)

    assert len(excerpts) == 2
    assert len(slices) == 2
    # Every single non-empty source line—including unreferenced line 4—must be covered.
    covered = set()
    for ex in excerpts:
        for ln in ex.splitlines():
            covered.add(ln.strip())
    assert set(lines) == covered
    # Contextual slices must include neighbor boundary lines around split points.
    assert "第5行：" in slices[0]
    assert "第4行：" in slices[1]

    panel_slice = build_s9_context_slice_from_episode_text(full_text, excerpts[1])
    assert "第5行：" in panel_slice
    assert "第8行：" in panel_slice
    assert build_s9_context_slice_from_episode_text(full_text, "") == full_text


async def test_s9_stage1_two_pass_parallel_continuity_bible_and_selective_repair(monkeypatch):
    import asyncio
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    from novelvideo import config as cfg_mod
    from novelvideo.s9_storyboard import (
        S9DurationAssessment,
        S9EpisodeContinuityBible,
        S9SceneLayoutAnchor,
        S9SceneLightingAnchor,
        S9SegmentExpansionOutput,
        S9Stage1SkeletonPlan,
        S9Stage1SkeletonSegment,
        S9StyleContext,
        S9SubShot,
        S9VoiceProfileAnchor,
        run_s9_stage1_plan,
    )

    long_source = "\n".join(
        [
            "第1句：雨夜便利店门口寒风呼啸，玻璃门映出霓虹残影。",
            "第2句：林默站在收银台后方，手中紧握半杯冷咖啡。",
            "第3句：他抬头盯住门外停下的黑色轿车，神情骤然紧绷。",
            "第4句：车门推开，身穿雨衣的程野大步走向便利店正门。",
            "第5句：程野推开玻璃门，雨水顺着帽檐滴落到瓷砖地面。",
            "第6句：他径直走到收银台前，压低声音开口质问。",
            "第7句：林默冷笑一声，将一枚旧U盘推过冰凉的玻璃台面。",
            "第8句：程野瞳孔微缩，右手猛地按住U盘边缘。",
        ]
    )

    bible = S9EpisodeContinuityBible(
        scene_lighting_rules=[
            S9SceneLightingAnchor(
                scene_id="便利店",
                time_of_day="夜",
                visual_style_summary="写实冷峻霓虹夜戏",
                key_lighting="冷白荧光顶灯直射，门外霓虹青红漫射作侧逆光",
                color_and_material="潮湿瓷砖反光、深蓝雨衣防水面料",
            )
        ],
        scene_layout_rules=[
            S9SceneLayoutAnchor(
                scene_id="便利店",
                primary_view_direction="自收银台望向玻璃正门",
                landmarks="正门在右后方、收银台横置前景左侧、货架列于纵深右侧",
                camera_working_side="收银台外侧主轴，同场光线左前顶投右后侧逆",
            )
        ],
        prop_state_chain=["U盘：大段1末在林默手中准备；大段2推送到收银台面由程野按住"],
        voice_profiles=[
            S9VoiceProfileAnchor(speaker="林默", voice_design="低沉微哑青年男声，语速冷静克制"),
            S9VoiceProfileAnchor(speaker="程野", voice_design="清冽紧绷青年男声，尾音带压迫感"),
        ],
    )
    skeleton_plan = S9Stage1SkeletonPlan(
        duration_assessment=S9DurationAssessment(
            estimated_total_seconds=18,
            segment_count=2,
            max_segment_seconds=15,
            compliant=True,
            reasoning="两段共18秒",
        ),
        continuity_bible=bible,
        segments=[
            S9Stage1SkeletonSegment(
                segment_number=1,
                title="门外逼近",
                planned_duration_seconds=6,
                source_excerpt="\n".join(long_source.splitlines()[:4]),
                story_beat="林默持咖啡警惕门外，程野下车走至玻璃门外停步。",
                entry_state="林默站收银台后持半杯咖啡望向玻璃门",
                exit_state="程野停在玻璃门外手搭门把准备推门",
                opening_shot_direction="中景略仰拍林默凝视门外",
                closing_shot_direction="门外侧近景锁定程野湿透袖口按上门把",
                identity_ids=["林默", "程野"],
                scene_ids=["便利店"],
                prop_ids=["U盘"],
            ),
            S9Stage1SkeletonSegment(
                segment_number=2,
                title="台前交锋",
                planned_duration_seconds=9,
                source_excerpt="\n".join(long_source.splitlines()[4:]),
                story_beat="程野推门走近收银台，林默推出U盘被其按住。",
                entry_state="程野站在玻璃门外手搭门把",
                exit_state="程野右手死死按住收银台上的旧U盘",
                opening_shot_direction="门内低角度反拍玻璃门被猛然推开",
                closing_shot_direction="微距俯拍U盘与指节",
                identity_ids=["林默", "程野"],
                scene_ids=["便利店"],
                prop_ids=["U盘"],
            ),
        ],
    )
    # Segment 1 cleanly passes pacing; Segment 2 intentionally has >cap duration & unclosed tail
    # so only Segment 2 triggers local selective repair.
    seg1_out = S9SegmentExpansionOutput(
        planned_duration_seconds=6,
        story_beat="林默持咖啡警惕门外，程野走至玻璃门外握住门把。",
        entry_state="林默站收银台后望向玻璃门",
        exit_state="程野右手握住玻璃门把手停在门外",
        scene_layout="正门在右后、收银台在左前",
        shots=[
            S9SubShot(
                shot_number=1,
                planned_duration_seconds=3,
                start_seconds=0,
                end_seconds=3,
                audio_type="dialogue",
                speaker="林默",
                spoken_text="这么晚，终于还是来了。",
                content="林默握紧半杯冷咖啡抬头盯住门外车灯开口低语。",
                identity_ids=["林默"],
                scene_id="便利店",
            ),
            S9SubShot(
                shot_number=2,
                planned_duration_seconds=3,
                start_seconds=3,
                end_seconds=6,
                audio_type="silence",
                content="程野冒雨快步走到门外，湿透右手扣住门把。",
                identity_ids=["程野"],
                scene_id="便利店",
            ),
        ],
    )
    seg2_bad = S9SegmentExpansionOutput(
        planned_duration_seconds=15,
        story_beat="程野进店与林默交锋。",
        entry_state="程野握住门把",
        exit_state="",
        shots=[
            S9SubShot(
                shot_number=1,
                planned_duration_seconds=12,
                start_seconds=0,
                end_seconds=12,
                content="无声等待空气凝固。",
                audio_type="silence",
                identity_ids=["程野"],
                scene_id="便利店",
            ),
            S9SubShot(
                shot_number=2,
                planned_duration_seconds=10,
                start_seconds=12,
                end_seconds=22,
                content="程野质问并按住U盘。",
                audio_type="dialogue",
                speaker="程野",
                spoken_text="东西呢？",
                identity_ids=["林默", "程野"],
                scene_id="便利店",
                prop_ids=["U盘"],
            ),
        ],
    )
    seg2_repaired = S9SegmentExpansionOutput(
        planned_duration_seconds=9,
        story_beat="程野推门而入逼问，林默推出U盘被其指节按住。",
        entry_state="程野推开便利店玻璃门迈入门槛",
        exit_state="程野右手紧按收银台上的U盘",
        shots=[
            S9SubShot(
                shot_number=1,
                planned_duration_seconds=4,
                start_seconds=0,
                end_seconds=4,
                content="程野推门大步走到收银台前低声发问。",
                audio_type="dialogue",
                speaker="程野",
                spoken_text="东西呢？",
                identity_ids=["程野"],
                scene_id="便利店",
            ),
            S9SubShot(
                shot_number=2,
                planned_duration_seconds=5,
                start_seconds=4,
                end_seconds=9,
                content="林默将旧U盘滑出，程野指节一把按住边缘。",
                audio_type="silence",
                identity_ids=["林默", "程野"],
                scene_id="便利店",
                prop_ids=["U盘"],
            ),
        ],
    )

    recorded_prompts: list[tuple[str, str]] = []
    pass1b_active = 0
    pass1b_peak = 0

    async def _stream_handler(messages, info):
        nonlocal pass1b_active, pass1b_peak
        schema_str = json.dumps(info.output_tools[0].parameters_json_schema)
        user_txt = "\n".join(
            str(part.content)
            for msg in messages
            for part in getattr(msg, "parts", [])
            if getattr(part, "content", None)
        )
        if "asset_manifest" in schema_str:
            recorded_prompts.append(("pass1a", user_txt))
            payload = skeleton_plan.model_dump_json(ensure_ascii=False)
        elif "请局部修订" in user_txt:
            recorded_prompts.append(("repair", user_txt))
            payload = seg2_repaired.model_dump_json(ensure_ascii=False)
        elif "**第 1 大段" in user_txt:
            pass1b_active += 1
            pass1b_peak = max(pass1b_peak, pass1b_active)
            await asyncio.sleep(0.03)
            pass1b_active -= 1
            recorded_prompts.append(("pass1b_1", user_txt))
            payload = seg1_out.model_dump_json(ensure_ascii=False)
        else:
            pass1b_active += 1
            pass1b_peak = max(pass1b_peak, pass1b_active)
            await asyncio.sleep(0.03)
            pass1b_active -= 1
            recorded_prompts.append(("pass1b_2", user_txt))
            payload = seg2_bad.model_dump_json(ensure_ascii=False)
        yield {0: DeltaToolCall(name="final_result", json_args=payload)}

    model = FunctionModel(stream_function=_stream_handler)
    monkeypatch.setattr(cfg_mod, "get_newapi_text_pydantic_model", lambda *a, **kw: model)
    monkeypatch.setattr(cfg_mod, "get_newapi_structured_output_model_settings", lambda *a, **kw: {"max_tokens": 8192})

    ep = SimpleNamespace(
        identity_ids=["林默", "程野"],
        identity_default_map={},
        scene_ids=["便利店"],
        prop_menu=[],
        beat_source_text=long_source,
        raw_content=long_source,
    )
    chars = [SimpleNamespace(name="林默", identities=[]), SimpleNamespace(name="程野", identities=[])]
    scenes = [SimpleNamespace(id="便利店", name="便利店")]
    style = S9StyleContext(style_id="modern", style_label="写实", aspect_ratio="16:9", s9_style_anchor="写实风格")

    plan = await run_s9_stage1_plan(
        source_text=long_source,
        episode_num=1,
        episode_title="雨夜",
        style_context=style,
        characters=chars,
        episode=ep,
        voice_available_names=set(),
        scenes=scenes,
        max_segment_seconds=15,
    )

    kinds = [k for k, _ in recorded_prompts]
    assert "pass1a" in kinds
    assert "pass1b_1" in kinds and "pass1b_2" in kinds
    # Selective repair only triggers for segment 2, never unnecessarily touching segment 1.
    assert kinds.count("repair") == 1
    assert pass1b_peak == 2
    assert len(plan.segments) == 2
    assert plan.continuity_bible is not None
    assert plan.continuity_bible.scene_lighting_rules[0].scene_id == "便利店"
    assert plan.segments[0].source_excerpt != ""
    assert plan.segments[1].planned_duration_seconds == 9
    # Verify Pass 1B prompts received Continuity Bible and handoff contracts.
    p1b_txt = dict(recorded_prompts)["pass1b_2"]
    assert "冷白荧光顶灯直射" in p1b_txt
    assert "程野停在玻璃门外手搭门把准备推门" in p1b_txt


async def test_s9_stage2_single_wave_with_continuity_bible_shot_handoffs_and_callback(monkeypatch):
    import asyncio
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    from novelvideo import config as cfg_mod
    from novelvideo.s9_storyboard import (
        S9EpisodeContinuityBible,
        S9SceneLightingAnchor,
        S9SegmentAssetSlot,
        S9SegmentPlan,
        S9StoryboardPlan,
        S9StyleContext,
        S9SubShot,
        generate_all_s9_segment_prompts,
    )

    bible = S9EpisodeContinuityBible(
        scene_lighting_rules=[
            S9SceneLightingAnchor(
                scene_id="客厅",
                time_of_day="夜",
                visual_style_summary="现代悬疑",
                key_lighting="茶几暖黄台灯与玄关冷月光对撞",
                color_and_material="胡桃木与毛玻璃反光",
            )
        ],
        prop_state_chain=["公文包：大段1玄关持握→大段2落至茶几→大段3打开取出文件"],
    )

    def _seg(num: int, first_desc: str, last_desc: str) -> S9SegmentPlan:
        return S9SegmentPlan(
            segment_number=num,
            title=f"片段{num}",
            planned_duration_seconds=6,
            story_beat=f"故事推进{num}",
            shots=[
                S9SubShot(
                    shot_number=1,
                    planned_duration_seconds=3,
                    start_seconds=0,
                    end_seconds=3,
                    content=first_desc,
                    scene_id="客厅",
                ),
                S9SubShot(
                    shot_number=2,
                    planned_duration_seconds=3,
                    start_seconds=3,
                    end_seconds=6,
                    content=last_desc,
                    scene_id="客厅",
                ),
            ],
        )

    plan = S9StoryboardPlan(
        continuity_bible=bible,
        segments=[
            _seg(1, "玄关推门首镜", "走向茶几尾镜动作A"),
            _seg(2, "承接落座首镜B", "开启搭扣尾镜动作C"),
            _seg(3, "抽出一页密件D", "定格反应末镜"),
        ],
    )
    slots = [
        [S9SegmentAssetSlot(slot=1, media_type="image", asset_kind="scene", name="客厅", asset_id="客厅")]
        for _ in range(3)
    ]

    active_calls = 0
    peak_calls = 0
    captured_prompts: dict[int, str] = {}

    async def _stage2_stream(messages, _info):
        nonlocal active_calls, peak_calls
        user_txt = "\n".join(
            str(part.content)
            for msg in messages
            for part in getattr(msg, "parts", [])
            if getattr(part, "content", None)
        )
        for seg_idx in (1, 2, 3):
            if f"请为「大段{seg_idx}：" in user_txt:
                captured_prompts[seg_idx] = user_txt
        active_calls += 1
        peak_calls = max(peak_calls, active_calls)
        await asyncio.sleep(0.04)
        active_calls -= 1
        yield (
            "【参考职责】\n@图片1：客厅场景。\n\n"
            "【全局光影画风】\n现代悬疑，茶几暖黄台灯与玄关冷月光对撞。\n\n"
            "【场景布局】\n客厅横轴不变。\n\n"
            "【注意事项】\n无字幕，严禁画面崩坏。\n\n"
            "【分镜详情】\n分镜1 [0秒-3秒]｜中景｜平视｜固定：镜头推进。\n"
            "分镜2 [3秒-6秒]｜近景｜平视｜固定：动作收束。"
        )

    model = FunctionModel(stream_function=_stage2_stream)
    monkeypatch.setattr(cfg_mod, "get_newapi_text_pydantic_model", lambda *a, **kw: model)
    monkeypatch.setattr(cfg_mod, "get_newapi_structured_output_model_settings", lambda *a, **kw: {})

    callback_events: list[tuple[int, int]] = []

    async def _on_done(idx: int, prompt: str) -> None:
        callback_events.append((idx, plan.segments[idx].segment_number))
        assert prompt

    prompts = await generate_all_s9_segment_prompts(
        plan=plan,
        per_segment_slots=slots,
        style_context=S9StyleContext(),
        source_text="全集完整长原文不应被整段塞进分段切片。",
        source_slices_by_segment=["切片1原文", "切片2原文", "切片3原文"],
        on_segment_done=_on_done,
    )

    assert len(prompts) == 3
    # When continuity_bible is present, all same-scene segments dispatch in 1 parallel wave.
    assert peak_calls == 3
    assert sorted(callback_events) == [(0, 1), (1, 2), (2, 3)]
    # Middle segment prompt must receive Continuity Bible, shot-level handoffs, and scoped slice.
    seg2_prompt = captured_prompts[2]
    assert "茶几暖黄台灯与玄关冷月光对撞" in seg2_prompt
    assert "走向茶几尾镜动作A" in seg2_prompt
    assert "抽出一页密件D" in seg2_prompt
    assert "切片2原文" in seg2_prompt
    assert "全集完整长原文不应被整段塞进分段切片" not in seg2_prompt
    assert "entry_state" not in seg2_prompt
    assert "exit_state" not in seg2_prompt


async def test_s9_workflow_progressive_persist_stage1_then_per_segment_stage2(monkeypatch):
    from novelvideo.s9_storyboard import (
        S9DurationAssessment,
        S9EpisodeContinuityBible,
        S9EpisodePreviewMeta,
        S9SceneLightingAnchor,
        S9SegmentAssetSlot,
        S9SegmentMeta,
        S9SegmentPlan,
        S9StoryboardPlan,
        S9SubShot,
    )
    from novelvideo.seedance2_i2v.models import parse_seedance2_config
    from novelvideo.workflows import s9_script_writing as wf_mod

    ep = SimpleNamespace(
        title="第1集",
        beat_source_text="雨夜进门并交出U盘。",
        raw_content="雨夜进门并交出U盘。",
        identity_ids=["艾莉森"],
        scene_ids=["客厅"],
        prop_menu=[],
        visual_style="realistic_modern",
        aspect_ratio="16:9",
    )

    call_log: list[str] = []
    persisted_scripts = []
    beat_updates: list[tuple[int, int, str]] = []

    class _ProgressiveStore:
        def get_episode(self, episode_num: int):
            return ep

        def get_all_characters(self):
            return [SimpleNamespace(name="艾莉森", identities=[])]

        def get_all_scenes(self):
            return [SimpleNamespace(id="客厅", name="客厅")]

        def get_all_props(self):
            return []

        async def load_working_content(self, episode_num: int):
            return ep.beat_source_text

        async def persist_narration_script(self, script):
            call_log.append("persist_stage1")
            persisted_scripts.append(script)

        async def update_beat_asset(self, *, episode_number: int, beat_number: int, **updates):
            call_log.append(f"update_beat:{beat_number}")
            beat_updates.append((episode_number, beat_number, updates.get("seedance2_config_json", "")))
            return True

    raw_plan = S9StoryboardPlan(
        duration_assessment=S9DurationAssessment(estimated_total_seconds=12, segment_count=2),
        continuity_bible=S9EpisodeContinuityBible(
            scene_lighting_rules=[S9SceneLightingAnchor(scene_id="客厅", key_lighting="玄关暖光")]
        ),
        segments=[
            S9SegmentPlan(
                segment_number=1,
                title="进门",
                planned_duration_seconds=6,
                source_excerpt="雨夜进门",
                story_beat="艾莉森进门。",
                shots=[S9SubShot(shot_number=1, planned_duration_seconds=6, start_seconds=0, end_seconds=6, content="进门", scene_id="客厅")],
            ),
            S9SegmentPlan(
                segment_number=2,
                title="交接",
                planned_duration_seconds=6,
                source_excerpt="交出U盘",
                story_beat="交出U盘。",
                shots=[S9SubShot(shot_number=1, planned_duration_seconds=6, start_seconds=0, end_seconds=6, content="递出", scene_id="客厅")],
            ),
        ],
    )
    slots = [
        [S9SegmentAssetSlot(slot=1, media_type="image", asset_kind="scene", name="客厅", asset_id="客厅")],
        [S9SegmentAssetSlot(slot=1, media_type="image", asset_kind="scene", name="客厅", asset_id="客厅")],
    ]

    async def _fake_stage1(**_kwargs):
        call_log.append("stage1_done")
        return raw_plan

    async def _fake_stage2(**kwargs):
        assert call_log[0] == "stage1_done"
        # Stage 1 preview MUST already be saved in DB before Stage 2 starts!
        assert call_log[1] == "persist_stage1"
        on_done = kwargs["on_segment_done"]
        prompts = ["大段1提示词 @图片1", "大段2提示词 @图片1"]
        for i, _seg in enumerate(kwargs["plan"].segments):
            await on_done(i, prompts[i])
        return prompts

    monkeypatch.setattr(wf_mod, "run_s9_stage1_plan", _fake_stage1)
    monkeypatch.setattr(
        wf_mod,
        "normalize_s9_storyboard_plan",
        lambda plan, **_kw: (plan, slots),
    )
    monkeypatch.setattr(wf_mod, "generate_all_s9_segment_prompts", _fake_stage2)

    progress_events: list[float] = []
    store = _ProgressiveStore()
    workflow = wf_mod.S9ScriptWritingWorkflow(cognee_store=store)
    result = await workflow.run(
        episode_num=1,
        on_progress=lambda p, _msg: progress_events.append(p),
    )

    assert len(persisted_scripts) == 1
    assert parse_seedance2_config(persisted_scripts[0].beats[0].seedance2_config_json).final_prompt == ""
    meta_parsed = S9EpisodePreviewMeta.model_validate_json(persisted_scripts[0].s9_preview_meta_json)
    assert meta_parsed.continuity_bible is not None
    assert meta_parsed.continuity_bible.scene_lighting_rules[0].key_lighting == "玄关暖光"
    seg1_meta = S9SegmentMeta.model_validate_json(persisted_scripts[0].beats[0].s9_segment_meta_json)
    assert seg1_meta.source_excerpt == "雨夜进门"

    assert len(beat_updates) == 2
    assert "大段1提示词" in parse_seedance2_config(beat_updates[0][2]).final_prompt
    assert "大段2提示词" in parse_seedance2_config(beat_updates[1][2]).final_prompt
    assert 0.50 in progress_events
    assert progress_events[-1] == 1.0
    assert len(result.beats) == 2


async def test_s9_stage1_omits_8192_cap_and_recovers_from_token_limit_exhaustion(monkeypatch):
    from novelvideo import config as cfg_mod
    from novelvideo.s9_storyboard import (
        S9Stage1SkeletonSegment,
        _run_s9_agent_streaming,
        _s9_stage1_model_settings,
        slice_s9_source_text_for_segments,
    )

    monkeypatch.delenv("NEWAPI_TEXT_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("S9_STAGE1_MAX_TOKENS", raising=False)
    monkeypatch.setattr(
        cfg_mod,
        "get_newapi_structured_output_model_settings",
        lambda *_a, **_kw: {"openai_reasoning_effort": "high"},
    )
    stage1_settings = _s9_stage1_model_settings("LITERAL_BEAT_META_MODEL")
    assert "max_tokens" not in stage1_settings
    assert stage1_settings["openai_reasoning_effort"] == "high"

    # Concise first/...\nlast anchor in Pass 1A must still expand into 100% verbatim lines.
    many_lines = [f"第{i}行：剧情动作与对白推进内容。" for i in range(1, 21)]
    full_src = "\n".join(many_lines)
    skels = [
        S9Stage1SkeletonSegment(
            segment_number=1,
            title="前半",
            source_excerpt=f"{many_lines[0]}\n...\n{many_lines[11]}",
        ),
        S9Stage1SkeletonSegment(
            segment_number=2,
            title="后半",
            source_excerpt=f"{many_lines[12]}\n...\n{many_lines[19]}",
        ),
    ]
    excerpts, _slices = slice_s9_source_text_for_segments(full_src, skels)
    assert excerpts[0].splitlines() == many_lines[:12]
    assert excerpts[1].splitlines() == many_lines[12:]

    # If a provider hits completion token limit before responding, retry automatically adjusts effort/budget.
    seen_overrides: list[dict | None] = []

    class _TokenLimitAgent:
        def __init__(self):
            self.model_settings = {"openai_reasoning_effort": "high", "max_tokens": 8192}

        async def run(self, _prompt, *, event_stream_handler=None, model_settings=None):
            seen_overrides.append(model_settings)
            if len(seen_overrides) == 1:
                raise RuntimeError(
                    "Model token limit (8192) exceeded before any response was generated."
                )
            return SimpleNamespace(output="recovered-ok")

    res = await _run_s9_agent_streaming(_TokenLimitAgent(), "hello")
    assert res.output == "recovered-ok"
    assert len(seen_overrides) == 2
    assert seen_overrides[0] is None
    assert seen_overrides[1]["openai_reasoning_effort"] == "low"
    assert seen_overrides[1]["max_tokens"] >= 16384


def test_s9_strip_mention_backticks_and_collapse_duplicate_scene_name():
    from novelvideo.s9_storyboard import _normalize_scene_id_variant, _strip_code_fences

    cleaned = _strip_code_fences("```\n- `@图片1` 绑定角色，``@音频2`` 绑定声线。\n```")
    assert cleaned == "- @图片1 绑定角色，@音频2 绑定声线。"
    assert "`" not in cleaned

    assert _normalize_scene_id_variant(
        "艾莉森公寓客厅_狼藉版_艾莉森公寓客厅_狼藉版", "狼藉版"
    ) == ("艾莉森公寓客厅_狼藉版", "")
    assert _normalize_scene_id_variant(
        "艾莉森公寓客厅_狼藉版", "艾莉森公寓客厅_狼藉版"
    ) == ("艾莉森公寓客厅_狼藉版", "")


@pytest.mark.asyncio
async def test_s9_workflow_syncs_reused_project_props_into_episode_prop_menu(monkeypatch, tmp_path):
    from novelvideo.models import NovelProp, PropMenuItem
    from novelvideo.workflows import s9_script_writing as wf_mod

    patched_calls: list[tuple[int, dict]] = []

    class _StubStore:
        state_dir = str(tmp_path / "state")

        def get_episode(self, _num):
            return SimpleNamespace(
                episode_number=3,
                title="第3集",
                identity_ids=["艾莉森_律师时期"],
                identity_default_map_json="",
                scene_menu=[SimpleNamespace(scene_id="客厅", variant_id="")],
                prop_menu=[PropMenuItem(prop_id="啤酒瓶")],
            )

        def get_all_characters(self):
            return [
                SimpleNamespace(
                    name="艾莉森",
                    aliases=[],
                    identities=[SimpleNamespace(identity_id="艾莉森_律师时期")],
                    reference_audio_path="",
                )
            ]

        async def list_scenes(self):
            return []

        async def list_props(self):
            return [
                NovelProp(name="啤酒瓶", description="棕色玻璃瓶"),
                NovelProp(name="公文包", description="深棕色硬质皮革公文包"),
            ]

        async def persist_narration_script(self, _script):
            return None

        async def patch_episode(self, ep_num, **fields):
            patched_calls.append((ep_num, fields))

    raw_plan = S9StoryboardPlan(
        segments=[
            S9SegmentPlan(
                segment_number=1,
                title="推门",
                planned_duration_seconds=8,
                story_beat="艾莉森拎着公文包推门进入客厅",
                shots=[
                    S9SubShot(
                        shot_number=1,
                        planned_duration_seconds=8,
                        start_seconds=0,
                        end_seconds=8,
                        content="艾莉森左手紧拎公文包跨进门，桌上摆着啤酒瓶。",
                        identity_ids=["艾莉森_律师时期"],
                        scene_id="客厅",
                        prop_ids=["啤酒瓶"],
                        audio_type="silence",
                    )
                ],
            )
        ]
    )

    async def fake_stage1(**_kw):
        return raw_plan

    async def fake_stage2_all(**_kw):
        return ["【参考职责】\n- @图片1：艾莉森。\n\n【全局光影画风】\n实拍。\n\n【注意事项】\n无。\n\n分镜1（0秒到8秒）：推门。"]

    monkeypatch.setattr(wf_mod, "run_s9_stage1_plan", fake_stage1)
    monkeypatch.setattr(wf_mod, "generate_all_s9_segment_prompts", fake_stage2_all)

    wf = wf_mod.S9ScriptWritingWorkflow(
        cognee_store=_StubStore(),
        output_dir=str(tmp_path / "output"),
    )
    script = await wf.run(episode_num=3, source_text="艾莉森拎着公文包回家。")
    assert "公文包" in script.beats[0].s9_segment_meta_json
    assert len(patched_calls) == 1
    ep_num, fields = patched_calls[0]
    assert ep_num == 3
    synced_ids = [item.prop_id for item in fields["prop_menu"]]
    assert synced_ids == ["啤酒瓶", "公文包"]


@pytest.mark.asyncio
async def test_s9_streaming_falls_back_to_prompted_output_on_output_retries(monkeypatch):
    from pydantic_ai import Agent, PromptedOutput
    from pydantic_ai.exceptions import UnexpectedModelBehavior
    from pydantic_ai.models.test import TestModel

    from novelvideo.s9_storyboard import (
        S9DurationAssessment,
        S9Stage1SkeletonPlan,
        S9Stage1SkeletonSegment,
        _run_s9_agent_streaming,
    )

    seen_calls: list[dict] = []
    expected = S9Stage1SkeletonPlan(
        duration_assessment=S9DurationAssessment(estimated_total_seconds=12, segment_count=1),
        segments=[
            S9Stage1SkeletonSegment(
                segment_number=1,
                title="推门对峙",
                planned_duration_seconds=12,
                source_excerpt="艾莉森快步走向门口。",
                story_beat="门外响起急促敲门声，艾莉森开门对峙。",
            )
        ],
    )

    agent = Agent(
        TestModel(),
        output_type=S9Stage1SkeletonPlan,
        model_settings={"openai_reasoning_effort": "high"},
        retries=3,
    )

    async def fake_run(prompt, **kwargs):
        seen_calls.append(dict(kwargs))
        if len(seen_calls) == 1:
            raise UnexpectedModelBehavior("Exceeded maximum output retries (3)")
        return SimpleNamespace(output=expected)

    monkeypatch.setattr(agent, "run", fake_run)

    res = await _run_s9_agent_streaming(agent, "拆分本集")
    assert res.output == expected
    assert len(seen_calls) == 2
    assert "output_type" not in seen_calls[0]
    assert isinstance(seen_calls[1].get("output_type"), PromptedOutput)
    assert seen_calls[1]["model_settings"]["openai_reasoning_effort"] == "low"


def test_normalize_s9_stage2_prompt_structure_unifies_drifted_formats():
    from novelvideo.s9_storyboard import normalize_s9_stage2_prompt_structure

    # Mirrors the drifted Beat 1 vs Beat 2 screenshot formats (multi-paragraph layout,
    # `---` separator, newline after `- 空间与主体：`, multi-paragraph `- 动态时间轴：`, ASCII `()`).
    beat1_like = """**【参考职责】**
@图片1 是艾莉森形象参考。

【全局光影画风】
视觉风格：三维CG写实渲染。
全局主光：冷白自然日光。
色彩与材质：冷白基调。

【场景布局】
本场景为艾莉森公寓客厅。

人物站位与活动空间：艾莉森站在吧台后侧。

机位工作轴与视线关系：镜头固定在吧台同侧。

【注意事项】
画面全域禁止生成任何硬字幕。

【角色声音】
艾莉森 (@音频1)：冷静克制。

---

分镜1 (0秒到5秒): 敲门与开门，中景转全景，35mm平视。
- 空间与主体：
艾莉森站在吧台后侧转身。

- 动态时间轴：
0秒-2秒：门外响起急促敲门声。

2秒-5秒：艾莉森走近门前推开门。

- 音频与对白：
0秒—2秒，急促敲门声。
- 尾帧与转场：门开，硬切。"""

    normalized = normalize_s9_stage2_prompt_structure(beat1_like)
    assert "---" not in normalized
    assert "**【参考职责】**" not in normalized
    assert "【参考职责】" in normalized
    assert "- 视觉风格：三维CG写实渲染。" in normalized
    assert "- 全局主光：冷白自然日光。" in normalized
    assert "- 色彩与材质：冷白基调。" in normalized
    assert "本场景为艾莉森公寓客厅。人物站位与活动空间：艾莉森站在吧台后侧。机位工作轴与视线关系：镜头固定在吧台同侧。" in normalized
    assert "分镜1（0秒到5秒）：敲门与开门，中景转全景，35mm平视。" in normalized
    assert "- 空间与主体：艾莉森站在吧台后侧转身。" in normalized
    assert "- 动态时间轴：0秒-2秒：门外响起急促敲门声。2秒-5秒：艾莉森走近门前推开门。" in normalized
    assert "- 音频与对白：0秒—2秒，急促敲门声。" in normalized
    assert "- 尾帧与转场：门开，硬切。" in normalized


def test_stage2_model_settings_caps_inherited_high_reasoning_and_concurrency_is_bounded(monkeypatch):
    from novelvideo import config as cfg_mod
    from novelvideo.s9_storyboard import (
        _s9_stage2_model_settings,
        get_s9_llm_concurrency,
    )

    monkeypatch.delenv("S9_LLM_CONCURRENCY", raising=False)
    monkeypatch.delenv("S9_STAGE2_REASONING_EFFORT", raising=False)
    monkeypatch.setattr(
        cfg_mod,
        "get_newapi_structured_output_model_settings",
        lambda *_a, **_kw: {"openai_reasoning_effort": "high"},
    )
    assert get_s9_llm_concurrency() == 4
    s2 = _s9_stage2_model_settings("SEEDANCE2_PROMPT_COMPOSER_MODEL")
    assert s2["openai_reasoning_effort"] == "low"
    assert s2["max_tokens"] == 8192


@pytest.mark.asyncio
async def test_s9_streaming_emits_heartbeat_logs_with_chunk_count(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from novelvideo import s9_storyboard as s9_mod

    monkeypatch.setattr(s9_mod, "_S9_STREAM_HEARTBEAT_SECONDS", 0.03)
    logs: list[str] = []

    class _SlowStreamAgent:
        name = "YD Stage1A Skeleton Planner"

        async def run(self, prompt, *, event_stream_handler=None, **kwargs):
            async def _events():
                for i in range(4):
                    await asyncio.sleep(0.02)
                    yield i

            if event_stream_handler is not None:
                await event_stream_handler(None, _events())
            return SimpleNamespace(output="done")

    res = await s9_mod._run_s9_agent_streaming(
        _SlowStreamAgent(),
        "test",
        on_log=logs.append,
        stage_label="Stage 1A · 全局骨架规划",
    )
    assert res.output == "done"
    assert any("Stage 1A · 全局骨架规划" in line and "流式" in line for line in logs)









