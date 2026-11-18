from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from novelvideo.api.routes.model_credits import (
    _fixed_image_billing_params,
    _image_selection_billing_params,
)
from novelvideo.generators import scene_reference_images
from novelvideo.generators.image_resolution import (
    resolve_highest_image_resolution,
    resolve_image_model_profile,
)
from novelvideo.generators.nanobanana_character import NanoBananaCharacterGenerator
from novelvideo.generators.scene_reference_images import build_scene_reference_prompt
from novelvideo.models import NovelScene
from novelvideo.skills.realistic_asset_skills import (
    CHARACTER_SKILL_ID,
    SCENE_SKILL_ID,
    clear_realistic_asset_skills_cache,
    get_live_action_character_sheet_skill_directives,
    get_realist_scene_master_skill_directives,
    validate_realistic_asset_skills,
)


def test_bundled_realistic_asset_skills_validate() -> None:
    clear_realistic_asset_skills_cache()
    report = validate_realistic_asset_skills()
    assert CHARACTER_SKILL_ID in report
    assert SCENE_SKILL_ID in report
    assert "默认版式：遵守画面含义而非“三视图”字面" in report[CHARACTER_SKILL_ID]
    assert "六个生成模型" in report[SCENE_SKILL_ID]


def test_realistic_asset_skills_hot_reload_on_file_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clear_realistic_asset_skills_cache()
    source_root = Path("src/novelvideo/asset_skills")
    custom_root = tmp_path / "asset_skills"
    shutil.copytree(source_root, custom_root)
    monkeypatch.setenv("REELFORGE_ASSET_SKILLS_DIR", str(custom_root))

    try:
        char_md = custom_root / CHARACTER_SKILL_ID / "SKILL.md"
        scene_md = custom_root / SCENE_SKILL_ID / "SKILL.md"

        char_text = char_md.read_text(encoding="utf-8")
        char_md.write_text(
            char_text.replace("## 提示词组织", "## 提示词组织\n\n- UPGRADE_MARKER_CHAR_V2"),
            encoding="utf-8",
        )
        # Ensure mtime_ns advances even on coarse filesystems.
        stat_c = char_md.stat()
        os.utime(char_md, ns=(stat_c.st_atime_ns, stat_c.st_mtime_ns + 2_000_000))

        scene_text = scene_md.read_text(encoding="utf-8")
        scene_md.write_text(
            scene_text.replace("## 十条条件规则", "## 十条条件规则\n\n- UPGRADE_MARKER_SCENE_V2"),
            encoding="utf-8",
        )
        stat_s = scene_md.stat()
        os.utime(scene_md, ns=(stat_s.st_atime_ns, stat_s.st_mtime_ns + 2_000_000))

        assert "UPGRADE_MARKER_CHAR_V2" in get_live_action_character_sheet_skill_directives()
        assert "UPGRADE_MARKER_SCENE_V2" in get_realist_scene_master_skill_directives()
    finally:
        clear_realistic_asset_skills_cache()


@pytest.mark.parametrize(
    "style_id",
    [
        "realistic",
        "chinese_period_drama",
        "republican_era_drama",
        "post_apocalyptic",
    ],
)
def test_live_action_styles_use_3_panel_character_sheet_and_portrait_skill(
    style_id: str,
) -> None:
    gen = NanoBananaCharacterGenerator.__new__(NanoBananaCharacterGenerator)
    portrait_prompt = gen._build_character_prompt(
        character_name="林默",
        character_prompt="女性，28岁，黑色低马尾，深棕色杏眼，自然肤色",
        character_tag="[林默_a1b2]",
        style_name=style_id,
        project_dir="",
        style_keywords="natural realism",
        negative_keywords="no anime",
    )
    assert "LIVE-ACTION CHARACTER PORTRAIT SKILL DIRECTIVES" in portrait_prompt
    assert "generate-live-action-character-sheet/SKILL.md" in portrait_prompt
    assert "High-quality, 4K resolution" not in portrait_prompt

    sheet_prompt = gen._build_identity_locked_prompt(
        character_name="林默",
        character_prompt="浅灰棉麻衬衫，深蓝长裤，黑布鞋",
        character_tag="[林默_a1b2]",
        target_view="front",
        style_name=style_id,
        project_dir="",
        style_keywords="natural realism",
        negative_keywords="no anime",
    )
    assert "EXACTLY THREE adjacent non-overlapping zones" in sheet_prompt
    assert "Zone 1 (LEFT 28%)" in sheet_prompt
    assert "Zone 2 (MIDDLE 28%)" in sheet_prompt
    assert "Zone 3 (RIGHT 44%)" in sheet_prompt
    assert "4-panel" not in sheet_prompt
    assert "LIVE-ACTION CHARACTER SHEET SKILL DIRECTIVES" in sheet_prompt


@pytest.mark.parametrize("style_id", ["anime", "guoman_fantasy"])
def test_animation_styles_keep_animated_4_panel_and_exclude_live_action_skills(
    style_id: str,
) -> None:
    gen = NanoBananaCharacterGenerator.__new__(NanoBananaCharacterGenerator)
    portrait_prompt = gen._build_character_prompt(
        character_name="林默",
        character_prompt="女性，黑发",
        character_tag="[林默_a1b2]",
        style_name=style_id,
        project_dir="",
        style_keywords="stylized animation",
        negative_keywords="no photorealism",
    )
    assert "LIVE-ACTION CHARACTER PORTRAIT SKILL DIRECTIVES" not in portrait_prompt
    assert "animated character identity portrait" in portrait_prompt

    sheet_prompt = gen._build_identity_locked_prompt(
        character_name="林默",
        character_prompt="青色长袍",
        character_tag="[林默_a1b2]",
        target_view="front",
        style_name=style_id,
        project_dir="",
        style_keywords="stylized animation",
        negative_keywords="no photorealism",
    )
    assert "4-panel animated character reference sheet" in sheet_prompt
    assert "LIVE-ACTION CHARACTER SHEET SKILL DIRECTIVES" not in sheet_prompt


def test_scene_prompt_gates_realist_skill_to_live_action_master_only() -> None:
    scene = NovelScene(
        name="修车铺",
        scene_type="exterior",
        environment_prompt="正面：敞开卷帘门的县城修车铺。\n左侧：工具架。\n右侧：旧轮胎堆。\n背面：水泥马路。",
    )

    live_master = build_scene_reference_prompt(
        "master",
        scene,
        style_name="写实现代 (realistic)",
        style_family="live_action",
    )
    assert "REALIST SCENE GENERATION SKILL DIRECTIVES" in live_master
    assert "FRONT-FACING HALF ONLY" in live_master

    inferred_live_master = build_scene_reference_prompt(
        "master",
        scene,
        style_name="写实古装剧 (chinese_period_drama)",
    )
    assert "REALIST SCENE GENERATION SKILL DIRECTIVES" in inferred_live_master

    anime_master = build_scene_reference_prompt(
        "master",
        scene,
        style_name="动漫风格 (anime)",
        style_family="animation",
    )
    assert "REALIST SCENE GENERATION SKILL DIRECTIVES" not in anime_master

    spatial = build_scene_reference_prompt(
        "spatial_layout",
        scene,
        style_name="写实现代 (realistic)",
        style_family="live_action",
        has_master_reference=True,
    )
    assert "REALIST SCENE GENERATION SKILL DIRECTIVES" not in spatial

    reverse_with_master = build_scene_reference_prompt(
        "reverse_master",
        scene,
        style_name="写实现代 (realistic)",
        style_family="live_action",
        has_master_reference=True,
    )
    assert "REALIST SCENE GENERATION SKILL DIRECTIVES" not in reverse_with_master


def test_resolve_highest_image_resolution_supports_4k_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "novelvideo.model_gateway_settings.get_newapi_media_model_mappings",
        lambda: {
            "custom-ultra": {
                "provider": "newapi",
                "upstreamModel": "custom-ultra-upstream",
                "mediaType": "image",
                "config": {
                    "resolutionOptions": ["1k", "2k", "4k"],
                    "qualityOptions": ["medium", "high"],
                    "minPixels": 3686400,
                },
            }
        },
    )

    assert resolve_highest_image_resolution("newapi", "gpt-image-2.6-4k") == "4K"
    assert resolve_highest_image_resolution("newapi", "LingShan-G2") == "4K"
    assert resolve_highest_image_resolution("newapi", "LingShan-NB-2") == "4K"
    assert resolve_highest_image_resolution("newapi", "seedream-4.5") == "4K"
    assert resolve_highest_image_resolution("newapi", "seedream-5.0-lite") == "3K"
    assert resolve_highest_image_resolution("newapi", "seedream-5.0-pro") == "2K"
    assert resolve_highest_image_resolution("google", "gemini-3-pro-image-preview") == "4K"

    custom_profile = resolve_image_model_profile("newapi", "custom-ultra")
    assert custom_profile.max_image_size == "4K"
    assert custom_profile.request_schema.get("includeQuality") is True
    assert custom_profile.request_schema.get("minPixels") == 3686400

    assert _fixed_image_billing_params("scene_master", model="gpt-image-2.6-4k")["size"] == "4K"
    assert (
        _image_selection_billing_params(model="gpt-image-2.6-4k", image_role="character")["size"]
        == "4K"
    )


@pytest.mark.asyncio
async def test_character_and_scene_assets_send_4k_for_4k_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novelvideo.config as config
    import novelvideo.generators.nanobanana_character as char_mod

    recorded_calls: list[dict] = []

    async def fake_newapi_call(**kwargs):
        recorded_calls.append(
            {
                "model": kwargs.get("model"),
                "image_config": dict(kwargs.get("image_config") or {}),
                "prompt": kwargs.get("prompt"),
                "ref_count": len(kwargs.get("reference_images") or []),
            }
        )
        return b"png-bytes", "", ""

    monkeypatch.setattr(
        config,
        "get_effective_newapi_gateway_config",
        lambda: SimpleNamespace(api_key="test-key", base_url="https://example.invalid/v1"),
    )
    monkeypatch.setattr(char_mod, "_call_newapi_image_api", fake_newapi_call)
    monkeypatch.setattr(scene_reference_images, "_call_newapi_image_api", fake_newapi_call)

    gen = NanoBananaCharacterGenerator(
        config={
            "provider": "newapi",
            "api_key": "test-key",
            "model": "gpt-image-2.6-4k",
            "base_url": "https://example.invalid/v1",
        }
    )

    portrait_res = await gen.generate_character_portrait(
        character_name="林默",
        character_prompt="女性，28岁，黑色低马尾",
        style="realistic",
        output_dir=str(tmp_path / "char"),
    )
    assert portrait_res.success

    identity_path = tmp_path / "char" / "identities" / "默认.png"
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_res = await gen.generate_identity_with_reference(
        character_name="林默",
        identity_prompt="浅灰衬衫，深蓝长裤",
        reference_image_path=portrait_res.primary_reference,
        output_path=str(identity_path),
        style="realistic",
    )
    assert identity_res.success

    await scene_reference_images.generate_scene_reference_image(
        project_dir=tmp_path / "proj",
        scene=NovelScene(name="修车铺", scene_type="exterior", environment_prompt="正面：修车铺大门。"),
        kind="master",
        provider="newapi",
        model="gpt-image-2.6-4k",
        style_name="写实现代 (realistic)",
        style_family="live_action",
    )

    assert len(recorded_calls) == 3
    assert recorded_calls[0]["image_config"]["image_size"] == "4K"
    assert recorded_calls[1]["image_config"]["image_size"] == "4K"
    assert recorded_calls[1]["ref_count"] == 1
    assert recorded_calls[2]["image_config"]["image_size"] == "4K"
