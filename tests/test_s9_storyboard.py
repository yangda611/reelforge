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


