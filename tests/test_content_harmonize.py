"""Tests for selective content harmonization rules and config defaults."""

from __future__ import annotations

import json
from pathlib import Path

from novelvideo.content_harmonize import (
    HARMONIZE_STORYBOARD_DIRECTIVE,
    build_image_compliance_guard,
    detect_sensitive_categories,
)
from novelvideo.generators.nanobanana_prop import build_prop_reference_prompt
from novelvideo.generators.scene_reference_images import build_scene_reference_prompt
from novelvideo.models import NovelScene
from novelvideo.project_config import (
    CONTENT_HARMONIZE_KEY,
    load_content_harmonize_from_state_dir,
)
from novelvideo.s9_storyboard import (
    build_s9_stage1_system_prompt,
    build_s9_stage2_system_prompt,
)


def test_content_harmonize_defaults_true_and_respects_explicit_false(tmp_path: Path):
    state_dir = tmp_path / "proj_state"
    state_dir.mkdir(parents=True)
    assert load_content_harmonize_from_state_dir(state_dir) is True

    cfg_path = state_dir / "project_config.json"
    cfg_path.write_text(json.dumps({CONTENT_HARMONIZE_KEY: False}), encoding="utf-8")
    assert load_content_harmonize_from_state_dir(state_dir) is False

    cfg_path.write_text(json.dumps({CONTENT_HARMONIZE_KEY: True}), encoding="utf-8")
    assert load_content_harmonize_from_state_dir(state_dir) is True


def test_detect_sensitive_categories_selective():
    assert detect_sensitive_categories("一个白色陶瓷咖啡杯放在木桌上") == []
    assert "law_enforcement" in detect_sensitive_categories("纽约市警察局 NYPD 巡逻警车")
    assert "weapons" in detect_sensitive_categories("黑色格洛克 19 手枪")
    assert "gambling_vice" in detect_sensitive_categories("屏幕亮着地下赌局页面")
    assert "physical_violence" in detect_sensitive_categories("狠狠重戳左肩并撞门而入")


def test_image_compliance_guard_is_selective_and_respects_flag(tmp_path: Path):
    state_dir = tmp_path / "proj_state"
    state_dir.mkdir(parents=True)

    # Normal prop: no compliance override injected
    normal_prop_prompt = build_prop_reference_prompt(
        "深棕色硬质皮革律师公文包",
        state_dir=str(state_dir),
    )
    assert "CONTENT SAFETY COMPLIANCE OVERRIDE" not in normal_prop_prompt

    # Sensitive prop when content_harmonize=True (default): guard injected
    police_prop_prompt = build_prop_reference_prompt(
        "白底蓝纹 NYPD 巡逻警车",
        state_dir=str(state_dir),
    )
    assert "CONTENT SAFETY COMPLIANCE OVERRIDE" in police_prop_prompt

    # Sensitive prop when content_harmonize=False: no guard injected
    (state_dir / "project_config.json").write_text(
        json.dumps({CONTENT_HARMONIZE_KEY: False}),
        encoding="utf-8",
    )
    raw_police_prop_prompt = build_prop_reference_prompt(
        "白底蓝纹 NYPD 巡逻警车",
        state_dir=str(state_dir),
    )
    assert "CONTENT SAFETY COMPLIANCE OVERRIDE" not in raw_police_prop_prompt
    assert build_image_compliance_guard("白底蓝纹 NYPD 巡逻警车", enabled=False) == ""


def test_scene_and_s9_system_prompts_respect_content_harmonize():
    scene = NovelScene(name="艾莉森公寓客厅", environment_prompt="现代温馨客厅")
    normal_scene_prompt = build_scene_reference_prompt(
        "master", scene, content_harmonize=True
    )
    assert "CONTENT SAFETY COMPLIANCE OVERRIDE" not in normal_scene_prompt

    sensitive_scene = NovelScene(name="警局审讯室", environment_prompt="审讯桌与手铐")
    guarded_scene_prompt = build_scene_reference_prompt(
        "master", sensitive_scene, content_harmonize=True
    )
    assert "CONTENT SAFETY COMPLIANCE OVERRIDE" in guarded_scene_prompt

    s1_raw = build_s9_stage1_system_prompt(max_segment_seconds=30, content_harmonize=False)
    s1_safe = build_s9_stage1_system_prompt(max_segment_seconds=30, content_harmonize=True)
    assert HARMONIZE_STORYBOARD_DIRECTIVE not in s1_raw
    assert HARMONIZE_STORYBOARD_DIRECTIVE in s1_safe

    s2_raw = build_s9_stage2_system_prompt(content_harmonize=False)
    s2_safe = build_s9_stage2_system_prompt(content_harmonize=True)
    assert HARMONIZE_STORYBOARD_DIRECTIVE not in s2_raw
    assert HARMONIZE_STORYBOARD_DIRECTIVE in s2_safe
