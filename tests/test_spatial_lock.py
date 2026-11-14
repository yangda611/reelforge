from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from PIL import Image

from novelvideo.generators.nanobanana_grid import (
    GridGenerationResult,
    regenerate_selected_beats,
)
from novelvideo.generators.spatial_lock import (
    build_character_envelope_mask,
    composite_locked_background,
    composite_locked_sketch,
    extract_sketch_character_mask,
)
from novelvideo.models import (
    NovelVisualBeat,
    SceneRef,
    beat_lock_camera_beat,
    beat_lock_spatial_mode,
    beats_locked_fixed_camera,
)
from novelvideo.seedance2_i2v.models import Seedance2I2VMode
from novelvideo.seedance2_i2v.prompt import (
    build_seedance2_prompt_composer_task,
    build_seedance2_prompt_draft,
)


def _make_sketch(
    path: Path,
    *,
    width: int = 240,
    height: int = 240,
    char_box: tuple[int, int, int, int] = (90, 40, 150, 210),
    char_rgb: tuple[int, int, int] = (235, 45, 210),
) -> Path:
    arr = np.full((height, width, 3), 245, dtype=np.uint8)
    # Monochrome convenience store shelves on left and right margins
    arr[20:220, 10:55] = 120
    arr[30:36, 10:230] = 90
    arr[110:114, 15:225] = 100
    x0, y0, x1, y1 = char_box
    arr[y0:y1, x0:x1] = np.array(char_rgb, dtype=np.uint8)
    Image.fromarray(arr, mode="RGB").save(path)
    return path


def _make_solid_frame(path: Path, rgb: tuple[int, int, int], *, size=(240, 240)) -> Path:
    arr = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    arr[:, :] = np.array(rgb, dtype=np.uint8)
    # Put distinctive background pattern on top-left corner outside character bbox
    arr[0:30, 0:30] = np.array([40, 80, 120], dtype=np.uint8)
    Image.fromarray(arr, mode="RGB").save(path)
    return path


def test_scene_ref_fixed_camera_helpers():
    b1 = {"beat_number": 1, "scene_ref": {"scene_id": "convenience_store"}}
    b2 = {
        "beat_number": 2,
        "scene_ref": {
            "scene_id": "convenience_store",
            "lock_camera_beat": 1,
            "lock_spatial_mode": "upper_body",
        },
    }
    b3 = NovelVisualBeat(
        episode_number=1,
        beat_number=3,
        narration_segment="",
        visual_description="",
        scene_ref=SceneRef(scene_id="street"),
    )

    assert beat_lock_camera_beat(b1) is None
    assert beat_lock_spatial_mode(b1) == ""
    assert beat_lock_camera_beat(b2) == 1
    assert beat_lock_spatial_mode(b2) == "upper_body"

    assert beats_locked_fixed_camera(b1, next_beat=b2) is True
    assert beats_locked_fixed_camera(b2, prev_beat=b1) is True
    assert beats_locked_fixed_camera(b1, next_beat=b3) is False


def test_extract_mask_ignores_monochrome_lines_and_keeps_colored_character(tmp_path: Path):
    sketch = _make_sketch(tmp_path / "sketch.png", char_box=(100, 50, 140, 190))
    mask = extract_sketch_character_mask(
        sketch,
        sketch_colors={"hero": "#EB2DD2 MAGENTA"},
    )
    arr = np.asarray(mask, dtype=np.uint8)

    # Background shelf area on left edge must be unmasked (0)
    assert arr[20:210, 10:50].max() == 0
    # Interior of colored character must be masked (255)
    assert arr[90:150, 110:130].min() == 255


def test_build_envelope_upper_body_locks_lower_legs(tmp_path: Path):
    anchor_sk = _make_sketch(tmp_path / "b1_sk.png", char_box=(100, 40, 140, 200))
    target_sk = _make_sketch(tmp_path / "b2_sk.png", char_box=(108, 40, 148, 200))

    full_mask = build_character_envelope_mask(
        anchor_sk,
        target_sk,
        target_size=(240, 240),
        lock_spatial_mode="full_body",
        dilate_ratio=0.02,
        min_dilate_px=4,
        feather_ratio=0.01,
        min_feather_px=2,
    )
    upper_mask = build_character_envelope_mask(
        anchor_sk,
        target_sk,
        target_size=(240, 240),
        lock_spatial_mode="upper_body",
        dilate_ratio=0.02,
        min_dilate_px=4,
        feather_ratio=0.01,
        min_feather_px=2,
    )
    assert full_mask is not None and upper_mask is not None
    full_arr = np.asarray(full_mask, dtype=np.uint8)
    upper_arr = np.asarray(upper_mask, dtype=np.uint8)

    # Chest/head region active in both
    assert full_arr[70, 120] > 200
    assert upper_arr[70, 120] > 200
    # Feet/ankles row (y=190) editable in full_body, locked (~0) in upper_body
    assert full_arr[190, 120] > 200
    assert upper_arr[190, 120] == 0


def test_composite_locked_background_and_sketch_preserve_anchor_outside_mask(tmp_path: Path):
    anchor_frame = _make_solid_frame(tmp_path / "anchor.png", (100, 100, 100))
    gen_frame = tmp_path / "gen.png"
    gen_arr = np.full((240, 240, 3), 210, dtype=np.uint8)
    # Mark background corner with drifting color that should be overwritten by anchor
    gen_arr[0:30, 0:30] = np.array([250, 20, 20], dtype=np.uint8)
    # Distinct character color in center
    gen_arr[80:160, 80:160] = np.array([30, 180, 240], dtype=np.uint8)
    Image.fromarray(gen_arr, mode="RGB").save(gen_frame)

    mask_arr = np.zeros((240, 240), dtype=np.uint8)
    mask_arr[70:170, 70:170] = 255
    mask = Image.fromarray(mask_arr, mode="L")

    out_path = composite_locked_background(
        anchor_frame,
        gen_frame,
        mask,
        output_path=tmp_path / "out_render.png",
        match_color=False,
    )
    out_arr = np.asarray(Image.open(out_path).convert("RGB"))
    anchor_arr = np.asarray(Image.open(anchor_frame).convert("RGB"))

    # Background pixels outside mask equal anchor 100%
    assert np.array_equal(out_arr[0:40, 0:40], anchor_arr[0:40, 0:40])
    # Character pixels inside mask preserve generated foreground
    assert np.array_equal(out_arr[85:155, 85:155], gen_arr[85:155, 85:155])

    sketch_out = composite_locked_sketch(
        anchor_frame,
        gen_frame,
        mask,
        output_path=tmp_path / "out_sketch.png",
    )
    sketch_arr = np.asarray(Image.open(sketch_out).convert("RGB"))
    assert np.array_equal(sketch_arr[0:40, 0:40], anchor_arr[0:40, 0:40])


@pytest.mark.asyncio
async def test_regenerate_selected_beats_splits_to_1x1_and_chains_anchor(tmp_path: Path):
    calls: list[dict] = []

    class FakeGenerator:
        async def generate_grid(self, **kwargs):
            calls.append(kwargs)
            out_path = Path(kwargs["output_path"])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            _make_solid_frame(out_path, (90, 90, 90))
            return GridGenerationResult(
                success=True,
                grid_image_path=str(out_path),
                beat_count=len(kwargs["beats"]),
                grid_rows=kwargs["rows"],
                grid_cols=kwargs["cols"],
            )

    beats = [
        {"beat_number": 1, "scene_ref": {"scene_id": "shop"}},
        {
            "beat_number": 2,
            "scene_ref": {
                "scene_id": "shop",
                "lock_camera_beat": 1,
                "lock_spatial_mode": "full_body",
            },
        },
    ]
    locked_anchors = {
        2: {
            "anchor_beat_number": 1,
            "anchor_image_path": "",  # Produced dynamically by beat 1 in this batch
            "spatial_mode": "full_body",
            "anchor_sketch_path": "",
            "target_sketch_path": "",
            "apply_paste_back": True,
        }
    }

    with patch(
        "novelvideo.generators.nanobanana_grid.create_grid_generator",
        return_value=FakeGenerator(),
    ):
        results = await regenerate_selected_beats(
            selected_beats=beats,
            mode_key="2x2_16-9",
            character_map={},
            style="anime",
            output_dir=str(tmp_path),
            is_sketch=False,
            locked_anchors=locked_anchors,
        )

    assert len(results) == 2
    assert len(calls) == 2
    assert calls[0]["mode_key"] == "1x1_16-9"
    assert calls[0]["rows"] == 1 and calls[0]["cols"] == 1
    assert calls[0].get("locked_anchor_image_path") is None

    # Beat 2 must receive Beat 1's newly generated image path as its locked anchor
    assert calls[1]["mode_key"] == "1x1_16-9"
    assert calls[1]["locked_anchor_beat_number"] == 1
    assert calls[1]["locked_spatial_mode"] == "full_body"
    assert calls[1]["locked_anchor_image_path"] == calls[0]["output_path"]


@pytest.mark.asyncio
async def test_video_prompt_builders_respect_locked_fixed_camera(tmp_path: Path):
    from novelvideo.agents.global_video_optimizer import GlobalVideoPromptOptimizer
    from novelvideo.agents.keyframe_prompt_builder import KeyframePromptBuilder

    sketch = _make_sketch(tmp_path / "b2.png")
    fake_agent = MagicMock()
    captured_prompts: list[str] = []

    async def fake_run(user_prompt):
        captured_prompts.append(
            user_prompt[0] if isinstance(user_prompt, list) else str(user_prompt)
        )
        return SimpleNamespace(output="The girl turns her head smoothly while rain falls.")

    fake_agent.run = AsyncMock(side_effect=fake_run)
    optimizer = GlobalVideoPromptOptimizer()
    optimizer._agents["zh"] = fake_agent

    b1 = {
        "beat_number": 1,
        "visual_description": "店员站在柜台后",
        "scene_ref": {"scene_id": "shop"},
    }
    b2 = {
        "beat_number": 2,
        "visual_description": "店员抬手拿起玻璃杯",
        "narration_segment": "她轻声开口",
        "scene_ref": {
            "scene_id": "shop",
            "lock_camera_beat": 1,
            "lock_spatial_mode": "full_body",
        },
    }
    await optimizer.optimize_single_beat(
        beat=b2,
        sketch_image_path=str(sketch),
        character_color_map={},
        language="zh",
        prev_beat=b1,
        total_beats=3,
    )
    assert "[LOCKED FIXED CAMERA]" in captured_prompts[-1]
    assert "ZERO push-in, pan, tilt, or zoom" in captured_prompts[-1]

    # KeyframePromptBuilder locked_camera support + fallback
    kf_builder = KeyframePromptBuilder()
    kf_fallback = kf_builder._fallback_build("zh", locked_camera=True)
    assert "固定" in kf_fallback


def test_seedance2_prompt_reflects_fixed_camera_lock():
    b1 = {
        "beat_number": 1,
        "visual_description": "收银台前的便利店店员",
        "scene_ref": {"scene_id": "store"},
    }
    b2 = {
        "beat_number": 2,
        "visual_description": "店员伸手递过纸袋",
        "scene_ref": {
            "scene_id": "store",
            "lock_camera_beat": 1,
            "lock_spatial_mode": "upper_body",
        },
    }

    draft_zh = build_seedance2_prompt_draft(
        mode=Seedance2I2VMode.FIRST_FRAME,
        beat=b2,
        assets=None,
        text_overlay=None,
        language="zh",
        locked_camera=True,
    )
    assert "固定三脚架机位" in draft_zh

    task_zh = build_seedance2_prompt_composer_task(
        mode=Seedance2I2VMode.FIRST_LAST_FRAME,
        beat=b2,
        assets=None,
        text_overlay=None,
        prompt_guidance="",
        draft_prompt=draft_zh,
        prev_beat=b1,
        language="zh",
    )
    assert "locked_with_prev" in task_zh
    assert "固定机位锁定" in task_zh

    # Unlocked beat keeps draft concise without fixed-camera lock clause
    unlocked_draft = build_seedance2_prompt_draft(
        mode=Seedance2I2VMode.FIRST_FRAME,
        beat=b1,
        assets=None,
        text_overlay=None,
        language="zh",
        locked_camera=False,
    )
    assert "固定三脚架机位" not in unlocked_draft


def test_normalize_fixed_camera_plan_enforces_scene_and_order():
    from novelvideo.agents.fixed_camera_planner import (
        BeatCameraLockDecision,
        normalize_fixed_camera_plan,
    )

    beats = [
        {"beat_number": 1, "scene_ref": {"scene_id": "alley"}},
        {
            "beat_number": 2,
            "scene_ref": {
                "scene_id": "store",
                "variant_id": "night",
                "render_anchor_id": "selected_background",
                "lock_camera_beat": 1,
                "lock_spatial_mode": "full_body",
            },
        },
        {"beat_number": 3, "scene_ref": {"scene_id": "store"}},
        {"beat_number": 4, "scene_ref": {"scene_id": "store"}},
    ]
    decisions = [
        BeatCameraLockDecision(beat_number=1, lock_camera_beat=2, lock_spatial_mode="full_body"),
        BeatCameraLockDecision(beat_number=2, lock_camera_beat=1, lock_spatial_mode="full_body"),
        BeatCameraLockDecision(beat_number=3, lock_camera_beat=2, lock_spatial_mode="upper_body"),
        BeatCameraLockDecision(beat_number=4, lock_camera_beat=99, lock_spatial_mode="full_body"),
    ]

    refs, normalized = normalize_fixed_camera_plan(beats, decisions)
    # Beat 1 forward lock rejected; Beat 2 cross-scene lock rejected and stale lock cleared
    assert refs[1].get("lock_camera_beat") is None
    assert refs[1].get("lock_spatial_mode") == ""
    assert refs[2]["scene_id"] == "store"
    assert refs[2]["variant_id"] == "night"
    assert refs[2]["render_anchor_id"] == "selected_background"
    assert refs[2].get("lock_camera_beat") is None
    assert refs[2].get("lock_spatial_mode") == ""
    # Beat 3 same-scene lock onto Beat 2 kept with upper_body
    assert refs[3]["scene_id"] == "store"
    assert refs[3]["lock_camera_beat"] == 2
    assert refs[3]["lock_spatial_mode"] == "upper_body"
    # Beat 4 invalid anchor rejected
    assert refs[4].get("lock_camera_beat") is None
    assert [d.lock_camera_beat for d in normalized] == [None, None, 2, None]


@pytest.mark.asyncio
async def test_apply_fixed_camera_plan_persists_only_changed_beats(monkeypatch):
    from novelvideo.agents import fixed_camera_planner
    from novelvideo.agents.fixed_camera_planner import BeatCameraLockDecision

    stored_beats = [
        {"beat_number": 1, "scene_ref": {"scene_id": "store"}},
        {"beat_number": 2, "scene_ref": {"scene_id": "store"}},
        {
            "beat_number": 3,
            "scene_ref": {"scene_id": "store", "lock_camera_beat": 2, "lock_spatial_mode": "full_body"},
        },
    ]

    class FakeStore:
        def __init__(self):
            self.updates = []

        async def get_beats_as_dicts(self, _ep):
            return list(stored_beats)

        async def update_beat_asset(self, *, episode_number, beat_number, scene_ref):
            self.updates.append((episode_number, beat_number, scene_ref))
            return True

    async def fake_plan(beats):
        return fixed_camera_planner.normalize_fixed_camera_plan(
            beats,
            [
                BeatCameraLockDecision(beat_number=1, lock_camera_beat=None),
                BeatCameraLockDecision(beat_number=2, lock_camera_beat=1, lock_spatial_mode="upper_body"),
                BeatCameraLockDecision(beat_number=3, lock_camera_beat=2, lock_spatial_mode="full_body"),
            ],
        )

    monkeypatch.setattr(fixed_camera_planner, "plan_fixed_camera_locks", fake_plan)
    store = FakeStore()
    summary = await fixed_camera_planner.apply_fixed_camera_plan_for_episode(store, 1)

    assert summary["total_beats"] == 3
    assert summary["locked_beats"] == [2, 3]
    # Beat 1 stayed unlocked, Beat 3 stayed (2, full_body), only Beat 2 changed
    assert summary["updated_count"] == 1
    assert len(store.updates) == 1
    ep, bn, saved_ref = store.updates[0]
    assert (ep, bn) == (1, 2)
    assert saved_ref["scene_id"] == "store"
    assert saved_ref["lock_camera_beat"] == 1
    assert saved_ref["lock_spatial_mode"] == "upper_body"


def test_partition_selected_beat_jobs_only_splits_locked_chain():
    from novelvideo.generators.nanobanana_grid import _partition_selected_beat_jobs

    beats = [{"beat_number": i} for i in range(1, 10)]
    # Beat 3 and Beat 4 lock onto Beat 2; Beats 1, 5..9 are unlocked.
    locked_anchors = {
        3: {"anchor_beat_number": 2, "spatial_mode": "upper_body"},
        4: {"anchor_beat_number": 2, "spatial_mode": "upper_body"},
    }
    jobs = _partition_selected_beat_jobs(
        beats,
        "5x5_16-9_sketch",
        capacity=25,
        is_sketch=True,
        locked_anchors=locked_anchors,
    )
    assert [(mk, [b["beat_number"] for b in chunk]) for mk, chunk in jobs] == [
        ("1x1_16-9_sketch", [1]),
        ("1x1_16-9_sketch", [2]),
        ("1x1_16-9_sketch", [3]),
        ("1x1_16-9_sketch", [4]),
        ("5x5_16-9_sketch", [5, 6, 7, 8, 9]),
    ]


def test_fixed_camera_planner_uses_active_agent_model_or_scene_route(monkeypatch):
    from novelvideo import config as app_config
    from novelvideo import model_gateway_settings as mgs
    from novelvideo.agents import fixed_camera_planner

    monkeypatch.delenv("FIXED_CAMERA_PLANNER_MODEL", raising=False)
    monkeypatch.setattr(app_config, "uses_local_ce_runtime", lambda: True)

    captured_direct = []
    monkeypatch.setattr(
        app_config,
        "_newapi_text_openai_model",
        lambda model_name, **kwargs: captured_direct.append((model_name, kwargs))
        or f"direct:{model_name}",
    )
    monkeypatch.setattr(
        app_config,
        "get_newapi_runtime_credentials",
        lambda **kwargs: (
            kwargs.get("api_key_override") or "sk-test",
            kwargs.get("base_url_override") or "https://agent.example/v1",
        ),
    )

    # 1. Settings -> Agent 模型 configured as third_party: uses active Agent model directly
    monkeypatch.setattr(
        mgs,
        "get_effective_agent_llm_config",
        lambda: mgs.EffectiveLlmConfig(
            mode=mgs.AGENT_LLM_MODE_THIRD_PARTY,
            source=mgs.AGENT_LLM_MODE_THIRD_PARTY,
            base_url="https://agent.example/v1",
            api_key="sk-agent",
            model="gpt-6.1-sol",
            is_brainclaw=False,
        ),
    )
    model, settings = fixed_camera_planner._resolve_fixed_camera_planner_model_and_settings()
    assert model == "direct:gpt-6.1-sol"
    assert settings is None
    assert captured_direct[-1][1]["base_url"] == "https://agent.example/v1"

    # 2. Agent 模型 set to inherit: reuses configured EPISODE_SCENE_PLANNER_MODEL instead of unmapped FIXED_CAMERA_PLANNER_MODEL
    captured_routes = []
    monkeypatch.setattr(
        mgs,
        "get_effective_agent_llm_config",
        lambda: mgs.EffectiveLlmConfig(
            mode="advanced",
            source="custom",
            base_url="http://newapi:3000/v1",
            api_key="sk-newapi",
            model="",
            is_brainclaw=False,
        ),
    )
    monkeypatch.setattr(
        app_config,
        "get_newapi_text_pydantic_model",
        lambda env_name, **kwargs: captured_routes.append((env_name, kwargs))
        or f"route:{env_name}",
    )
    model2, settings2 = fixed_camera_planner._resolve_fixed_camera_planner_model_and_settings()
    assert model2 == "route:EPISODE_SCENE_PLANNER_MODEL"
    assert settings2 == app_config.get_newapi_structured_output_model_settings()

    # 3. Explicit FIXED_CAMERA_PLANNER_MODEL env var overrides everything
    monkeypatch.setenv("FIXED_CAMERA_PLANNER_MODEL", "custom-planner-alias")
    model3, _ = fixed_camera_planner._resolve_fixed_camera_planner_model_and_settings()
    assert model3 == "route:FIXED_CAMERA_PLANNER_MODEL"
    assert captured_routes[-1][1]["model_name_override"] == "custom-planner-alias"


