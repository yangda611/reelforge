"""Seedance 2.0 panel-domain helpers shared by REST API and legacy UI."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
from anyio.lowlevel import RunVar

from novelvideo.manual_shots import resolve_target_video_duration
from novelvideo.project_config import set_narrator_reference_audio_in_state_dir
from novelvideo.seedance2_i2v.assets import (
    Seedance2ResolvedAsset,
    apply_prompt_audio_selection,
    build_seedance2_project_assets,
    resolve_beat_video_references,
    selected_reference_paths,
    validate_seedance2_reference_image,
)
from novelvideo.seedance2_i2v.character_voice_storage import (
    VOICE_SAMPLE_EXTENSIONS,
    narrator_voice_resource_key,
    run_voice_media_operation,
    seedance2_asset_resource_key,
    trim_voice_sample_content,
    voice_resource_lock,
    voice_content_sha256,
)
from novelvideo.seedance2_i2v.models import (
    SEEDANCE2_PROMPT_GUIDANCE_TEMPLATE_KEYS,
    Seedance2I2VMode,
    default_seedance2_ratio_for_state_dir,
    dump_seedance2_config,
    normalize_video_prompt_style,
    parse_seedance2_config,
)
from novelvideo.seedance2_i2v.prompt import (
    _localized_prompt_guidance,
    build_seedance2_prompt_draft,
    compute_seedance2_prompt_inputs_hash,
    detect_seedance2_prompt_language,
    generate_seedance2_prompt,
    style_duration_bounds,
)
from novelvideo.seedance2_i2v.voice_clone import normalize_seedance2_audio_type
from novelvideo.utils.async_ops import metadata_io_limiter
from novelvideo.utils.media_io import (
    crop_image_to_path_sync as _crop_image_to_path_sync,
    get_audio_duration,
)
from novelvideo.utils.path_resolver import PathResolver
from novelvideo.utils.source_language import AssetLanguage


SEEDANCE2_PROMPT_GUIDANCE_TEMPLATES: dict[str, str] = {
    "主体": "主体：明确画面核心人物或物体、当前动作和状态，避免多个主体争抢焦点。",
    "场景": "场景：补充空间背景、地点关系、关键道具和环境材质，保持与参考图一致。",
    "光影": "光影：描述主光源、明暗层次、色温和氛围，避免忽明忽暗。",
    "镜头": "镜头：说明景别、视角、运镜速度和运动方向，保持镜头运动清晰可执行。",
    "风格": "风格：限定画面质感、时代感、色彩倾向和真实度，避免风格漂移。",
    "无字幕": "无字幕：避免生成任何文字或字幕，保持画面纯净。",
}
_SEEDANCE2_ASSET_CONCURRENCY = 2
_seedance2_asset_limiter_var: RunVar[anyio.CapacityLimiter] = RunVar(
    "seedance2_asset_limiter"
)


def _seedance2_asset_limiter() -> anyio.CapacityLimiter:
    limiter = _seedance2_asset_limiter_var.get(None)
    if limiter is None:
        limiter = anyio.CapacityLimiter(_SEEDANCE2_ASSET_CONCURRENCY)
        _seedance2_asset_limiter_var.set(limiter)
    return limiter


@dataclass(frozen=True)
class Seedance2VideoPanelState:
    mode: str
    duration: int
    duration_floor: int
    resolution: str
    ratio: str
    generate_audio: bool
    return_last_frame: bool
    human_review: bool
    prompt_guidance: str
    final_prompt: str
    prompt_source: str
    text_overlay: dict[str, Any]
    assets: list[Seedance2ResolvedAsset]
    storyboard_context: list[tuple[str, str]]
    prompt_inputs_hash: str
    current_prompt_inputs_hash: str
    prompt_status: str
    s9_missing_assets: list[dict[str, str]] = field(default_factory=list)
    s9_effective_prompt: str = ""


async def save_seedance2_video_panel_config(
    *,
    store: Any,
    state_dir: str | Path,
    episode: int,
    beat: dict[str, Any],
    mode: str | None = None,
    duration: int | float | None = None,
    resolution: str | None = None,
    ratio: str | None = None,
    generate_audio: bool | None = None,
    return_last_frame: bool | None = None,
    human_review: bool | None = None,
    prompt_guidance: str | None = None,
    final_prompt: str | None = None,
    text_overlay: dict[str, Any] | None = None,
    project_dir: Path | None = None,
    next_beat: dict[str, Any] | None = None,
    prop_menu: list[Any] | None = None,
) -> str:
    beat_num = int(beat.get("beat_number") or 0)
    root = _seedance2_panel_project_dir(
        project_dir=project_dir,
        store=store,
        fallback=state_dir,
    )
    resource_key = seedance2_asset_resource_key(
        project_dir=root,
        episode=episode,
        beat_number=beat_num,
    )

    async def finalize(saved_json: str) -> str:
        await store.update_beat_asset(
            episode_number=episode,
            beat_number=beat_num,
            seedance2_config_json=saved_json,
        )
        beat["seedance2_config_json"] = saved_json
        return saved_json

    async with voice_resource_lock(resource_key):
        return await run_voice_media_operation(
            _prepare_seedance2_video_panel_config,
            state_dir=state_dir,
            episode=episode,
            beat=beat,
            mode=mode,
            duration=duration,
            resolution=resolution,
            ratio=ratio,
            generate_audio=generate_audio,
            return_last_frame=return_last_frame,
            human_review=human_review,
            prompt_guidance=prompt_guidance,
            final_prompt=final_prompt,
            text_overlay=text_overlay,
            project_dir=project_dir,
            next_beat=next_beat,
            prop_menu=prop_menu,
            worker_limiter=metadata_io_limiter(),
            finalize=finalize,
        )


def _prepare_seedance2_video_panel_config(
    *,
    state_dir: str | Path,
    episode: int,
    beat: dict[str, Any],
    mode: str | None,
    duration: int | float | None,
    resolution: str | None,
    ratio: str | None,
    generate_audio: bool | None,
    return_last_frame: bool | None,
    human_review: bool | None,
    prompt_guidance: str | None,
    final_prompt: str | None,
    text_overlay: dict[str, Any] | None,
    project_dir: Path | None,
    next_beat: dict[str, Any] | None,
    prop_menu: list[Any] | None,
) -> str:
    default_ratio = default_seedance2_ratio_for_state_dir(state_dir)
    config = parse_seedance2_config(
        beat.get("seedance2_config_json"),
        default_ratio=default_ratio,
    )
    if mode is not None:
        config.mode = Seedance2I2VMode(mode)
    if duration is not None:
        next_duration = int(duration)
        if next_duration != config.duration:
            config.duration_user_set = True
        config.duration = next_duration
    if resolution is not None:
        config.resolution = str(resolution or "720p").strip()
    if ratio is not None:
        next_ratio = str(ratio or default_ratio or "9:16").strip()
        if next_ratio != config.ratio:
            config.ratio_user_set = True
        config.ratio = next_ratio
    if generate_audio is not None:
        config.generate_audio = bool(generate_audio)
        config.generate_audio_user_set = True
    if return_last_frame is not None:
        config.return_last_frame = bool(return_last_frame)
    if human_review is not None:
        config.human_review = bool(human_review)
        config.human_review_user_set = True
    if prompt_guidance is not None:
        config.prompt_guidance = str(prompt_guidance or "").strip()
    if final_prompt is not None:
        config.final_prompt = str(final_prompt or "").strip()
        config.prompt_source = "manual" if config.final_prompt else ""
    if text_overlay is not None:
        overlay = dict(config.text_overlay or {})
        overlay.update(text_overlay)
        config.text_overlay = overlay
    if project_dir is not None:
        _sync_seedance2_asset_paths(
            config=config,
            project_dir=project_dir,
            episode=episode,
            beat=beat,
            state_dir=state_dir,
            next_beat=next_beat,
            prop_menu=prop_menu,
        )

    saved_json = dump_seedance2_config(config)
    return saved_json


async def append_seedance2_prompt_guidance_template(
    *,
    store: Any | None,
    episode: int,
    beat: dict[str, Any],
    label: str,
    prompt_guidance: str | None = None,
    project_dir: Path | None = None,
) -> str:
    beat_num = int(beat.get("beat_number") or 0)
    root = _seedance2_panel_project_dir(
        project_dir=project_dir,
        store=store,
    )
    resource_key = seedance2_asset_resource_key(
        project_dir=root,
        episode=episode,
        beat_number=beat_num,
    )

    async def finalize(saved_json: str) -> str:
        if store is not None:
            await store.update_beat_asset(
                episode_number=episode,
                beat_number=beat_num,
                seedance2_config_json=saved_json,
            )
        beat["seedance2_config_json"] = saved_json
        return saved_json

    async with voice_resource_lock(resource_key):
        return await run_voice_media_operation(
            _prepare_seedance2_prompt_guidance_template,
            beat=beat,
            label=label,
            prompt_guidance=prompt_guidance,
            worker_limiter=metadata_io_limiter(),
            finalize=finalize,
        )


def _prepare_seedance2_prompt_guidance_template(
    *,
    beat: dict[str, Any],
    label: str,
    prompt_guidance: str | None,
) -> str:
    template = SEEDANCE2_PROMPT_GUIDANCE_TEMPLATES.get(str(label or "").strip())
    config = parse_seedance2_config(beat.get("seedance2_config_json"))
    if prompt_guidance is not None:
        config.prompt_guidance = str(prompt_guidance or "").strip()
    if template and template not in config.prompt_guidance:
        parts = [part for part in [config.prompt_guidance, template] if part]
        config.prompt_guidance = "\n".join(parts)

    saved_json = dump_seedance2_config(config)
    return saved_json


async def generate_seedance2_prompt_for_panel(
    *,
    store: Any,
    state_dir: str | Path,
    episode: int,
    beat: dict[str, Any],
    project_dir: Path,
    prev_beat: dict[str, Any] | None = None,
    next_beat: dict[str, Any] | None = None,
    composer=None,
    manual_prompt_reference: str | None = None,
    prompt_guidance: str | None = None,
    prompt_guidance_template_keys: list[str] | None = None,
    prompt_style: str | None = None,
    video_backend: str | None = None,
    language: AssetLanguage | None = None,
    prop_menu: list[Any] | None = None,
) -> str:
    default_ratio = default_seedance2_ratio_for_state_dir(state_dir)
    if prev_beat is None and episode > 1 and int(beat.get("beat_number") or 0) <= 1 and store is not None:
        from novelvideo.skills.cinematic_storyboard_skill import (
            load_prev_episode_last_beat_from_store,
        )

        prev_beat, _ = await load_prev_episode_last_beat_from_store(store, episode)
    duration_floor = _seedance2_duration_floor(
        project_dir=Path(project_dir),
        episode=episode,
        beat=beat,
    )
    config = parse_seedance2_config(
        beat.get("seedance2_config_json"),
        default_ratio=default_ratio,
        default_duration=duration_floor,
    )
    normalized_style = normalize_video_prompt_style(prompt_style)
    if normalized_style:
        config.prompt_style = normalized_style
    effective_style = config.prompt_style or None
    backend_min, backend_max = (None, None)
    if video_backend:
        from novelvideo.video_duration import video_duration_bounds_for_backend

        backend_min, backend_max = video_duration_bounds_for_backend(video_backend)
    min_duration, max_duration = style_duration_bounds(
        effective_style,
        min_duration=backend_min,
        max_duration=backend_max,
    )
    if prompt_guidance is not None:
        config.prompt_guidance = str(prompt_guidance or "").strip()
    if prompt_guidance_template_keys is not None:
        config.prompt_guidance_template_keys = list(
            dict.fromkeys(
                key
                for value in prompt_guidance_template_keys
                if (key := str(value or "").strip())
                in SEEDANCE2_PROMPT_GUIDANCE_TEMPLATE_KEYS
            )
        )
    assets = build_seedance2_project_assets(
        project_output=Path(project_dir),
        episode=episode,
        beat=beat,
        mode=config.mode,
        state_dir=state_dir,
        next_beat=next_beat,
        prop_menu=prop_menu,
    )
    _append_seedance2_user_reference_assets(assets, config)
    initial_prompt = _seedance2_initial_prompt(beat)
    prompt_beat = _beat_with_seedance2_initial_prompt(beat, initial_prompt)
    output_language = language or detect_seedance2_prompt_language(prompt_beat)
    inputs_hash = _seedance2_prompt_inputs_hash(
        config=config,
        beat=prompt_beat,
        assets=assets,
        language=output_language,
    )

    reference_prompt = str(
        manual_prompt_reference
        if manual_prompt_reference is not None
        else (config.final_prompt or initial_prompt or "")
    ).strip()

    from novelvideo.s9_storyboard import (
        S9AssetManifestItem,
        S9SegmentAssetSlot,
        S9SegmentMeta,
        S9SegmentPlan,
        build_s9_scene_prop_consistency_notes,
        build_s9_slots_from_beat,
        extract_s9_lighting_style_block,
        is_s9_segment_beat,
        rebind_s9_prompt_with_active_assets,
        renumber_resolved_seedance2_assets,
        resolve_s9_style_context,
        run_s9_stage2_segment_prompt,
    )

    if composer is None and is_s9_segment_beat(beat):
        from novelvideo.project_config import load_project_config_from_state_dir

        subshots, s9_meta, slots = build_s9_slots_from_beat(
            beat,
            include_first_frame=bool(config.s9_include_first_frame),
        )
        proj_cfg = load_project_config_from_state_dir(state_dir)
        style_context = resolve_s9_style_context(
            visual_style=proj_cfg.get("visual_style"),
            aspect_ratio=proj_cfg.get("aspect_ratio"),
            state_dir=state_dir,
        )
        planned_dur = int(
            (s9_meta.planned_duration_seconds if s9_meta else 0)
            or beat.get("duration_seconds")
            or beat.get("estimated_duration")
            or config.duration
            or 15
        )
        segment_plan = S9SegmentPlan(
            segment_number=int(beat.get("beat_number") or (s9_meta.segment_number if s9_meta else 1)),
            title=str(beat.get("segment_title") or (s9_meta.segment_title if s9_meta else "") or ""),
            planned_duration_seconds=planned_dur,
            story_beat=str((s9_meta.story_beat if s9_meta else "") or beat.get("visual_description") or ""),
            entry_state=str(getattr(s9_meta, "entry_state", "") or ""),
            exit_state=str(getattr(s9_meta, "exit_state", "") or ""),
            scene_layout=str(getattr(s9_meta, "scene_layout", "") or ""),
            source_excerpt=str(getattr(s9_meta, "source_excerpt", "") or ""),
            shots=subshots,
        )

        def _summary_seg(adj_beat: dict[str, Any] | None) -> S9SegmentPlan | None:
            if not adj_beat:
                return None
            adj_sub, adj_meta, _ = build_s9_slots_from_beat(adj_beat)
            return S9SegmentPlan(
                segment_number=int(adj_beat.get("beat_number") or 0),
                title=str(adj_beat.get("segment_title") or (adj_meta.segment_title if adj_meta else "") or ""),
                planned_duration_seconds=int((adj_meta.planned_duration_seconds if adj_meta else 0) or 15),
                story_beat=str((adj_meta.story_beat if adj_meta else "") or adj_beat.get("visual_description") or ""),
                entry_state=str(getattr(adj_meta, "entry_state", "") or ""),
                exit_state=str(getattr(adj_meta, "exit_state", "") or ""),
                scene_layout=str(getattr(adj_meta, "scene_layout", "") or ""),
                shots=adj_sub,
            )

        # Build live-bound active slot table if assets are on disk; keep manifest slots otherwise
        _tmp, ordered_assets, _missing = rebind_s9_prompt_with_active_assets(
            "",
            slots=slots,
            assets=assets,
            include_first_frame=bool(config.s9_include_first_frame),
        )
        numbered_assets = renumber_resolved_seedance2_assets(ordered_assets)
        active_img_slots: list[S9SegmentAssetSlot] = []
        active_aud_slots: list[S9SegmentAssetSlot] = []
        for a in numbered_assets:
            if not getattr(a, "selected", False):
                continue
            key = str(getattr(a, "key", "") or "")
            label = str(getattr(a, "label", "") or key)
            if str(getattr(a, "media_type", "")) == "image":
                kind = (
                    "first_frame"
                    if key == "first_frame"
                    else (
                        "identity"
                        if key.startswith("identity:")
                        else (
                            "scene_reverse"
                            if key.startswith("scene_reverse:")
                            else ("scene" if key.startswith("scene:") else "prop")
                        )
                    )
                )
                active_img_slots.append(
                    S9SegmentAssetSlot(
                        slot=len(active_img_slots) + 1,
                        media_type="image",
                        asset_kind=kind,
                        name=label.split(" · ")[-1],
                        asset_id=str(getattr(a, "identity_id", "") or getattr(a, "prop_id", "") or key.split(":", 1)[-1]),
                    )
                )
            elif str(getattr(a, "media_type", "")) == "audio":
                active_aud_slots.append(
                    S9SegmentAssetSlot(
                        slot=len(active_aud_slots) + 1,
                        media_type="audio",
                        asset_kind="voice",
                        name=label.replace("声线", "").strip() or label,
                        asset_id=str(getattr(a, "identity_id", "") or key.split(":", 1)[-1]),
                    )
                )
        stage2_slots = (active_img_slots + active_aud_slots) or slots
        episode_source_text = ""
        ep_obj = None
        scenes_list: list[Any] = []
        try:
            from novelvideo.sqlite_store import load_episode_planning_content

            ep_obj = store.get_episode(episode) if hasattr(store, "get_episode") else None
            if ep_obj is None and hasattr(store, "get_episode_from_graph"):
                ep_obj = await store.get_episode_from_graph(episode)
            if ep_obj is not None:
                episode_source_text = (
                    await load_episode_planning_content(store, ep_obj) or ""
                ).strip()
        except Exception:
            episode_source_text = ""

        continuity_bible = None
        if ep_obj is not None:
            raw_pm = (
                ep_obj.get("s9_preview_meta_json")
                if isinstance(ep_obj, dict)
                else getattr(ep_obj, "s9_preview_meta_json", None)
            )
            if isinstance(raw_pm, str) and raw_pm.strip():
                try:
                    from novelvideo.s9_storyboard import S9EpisodePreviewMeta

                    parsed_pm = S9EpisodePreviewMeta.model_validate_json(raw_pm)
                    continuity_bible = parsed_pm.continuity_bible
                except Exception:
                    continuity_bible = None

        raw_sqlite = getattr(store, "sqlite_store", store)
        props_list: list[Any] = []
        try:
            if hasattr(raw_sqlite, "list_scenes"):
                scenes_list = list(await raw_sqlite.list_scenes() or [])
            elif hasattr(store, "get_all_scenes"):
                scenes_list = list(store.get_all_scenes() or [])
        except Exception:
            scenes_list = []
        try:
            if hasattr(raw_sqlite, "list_props"):
                props_list = list(await raw_sqlite.list_props() or [])
            elif hasattr(store, "get_all_props"):
                props_list = list(store.get_all_props() or [])
        except Exception:
            props_list = []
        scene_context_notes = build_s9_scene_prop_consistency_notes(
            slots=stage2_slots,
            scenes=scenes_list,
            prop_menu=prop_menu,
            props=props_list,
            time_of_day=str(beat.get("time_of_day") or ""),
            has_multiple_segments=bool(prev_beat or next_beat),
        )
        continuity_parts: list[str] = []
        if continuity_bible is not None:
            from novelvideo.s9_storyboard import format_s9_continuity_bible_for_segment

            bible_anchor = format_s9_continuity_bible_for_segment(
                continuity_bible,
                segment=segment_plan,
                slots=stage2_slots,
            )
            if bible_anchor:
                continuity_parts.append(bible_anchor)
        if prev_beat:
            from novelvideo.models import beat_scene_ref
            from novelvideo.s9_storyboard import sanitize_s9_lighting_anchor

            cur_sref = beat_scene_ref(beat)
            prev_sref = beat_scene_ref(prev_beat)
            cur_sid = str(cur_sref.scene_id or "").strip() if cur_sref else ""
            prev_sid = str(prev_sref.scene_id or "").strip() if prev_sref else ""
            same_scene = bool(
                cur_sid
                and prev_sid
                and (
                    cur_sid == prev_sid
                    or cur_sid.startswith(f"{prev_sid}_")
                    or prev_sid.startswith(f"{cur_sid}_")
                )
            )
            same_time = str(beat.get("time_of_day") or "").strip() == str(
                prev_beat.get("time_of_day") or ""
            ).strip()
            if same_scene and same_time:
                prev_cfg = parse_seedance2_config(prev_beat.get("seedance2_config_json"))
                prev_lighting = sanitize_s9_lighting_anchor(
                    extract_s9_lighting_style_block(prev_cfg.final_prompt)
                )
                if prev_lighting:
                    continuity_parts.append(
                        f"同场景前一段（Beat {prev_beat.get('beat_number')}）已锁定的光影画风基准"
                        "（本段必须严格沿用同一主光来源、方向、时段与色彩基调，禁止改变；"
                        "引用场景图时请按本段【本大段素材槽位表】的实际 @图片N 编号书写，严禁照抄前段编号）：\n"
                        f"{prev_lighting}"
                    )
        continuity_anchor = "\n\n".join(part for part in continuity_parts if part)
        from novelvideo.s9_storyboard import build_s9_context_slice_from_episode_text

        scoped_source_text = build_s9_context_slice_from_episode_text(
            episode_source_text,
            segment_plan.source_excerpt,
        )
        effective_s9_guidance = _localized_prompt_guidance(
            config.prompt_guidance,
            config.prompt_guidance_template_keys,
            output_language,
        )
        rendered_s9 = await run_s9_stage2_segment_prompt(
            segment=segment_plan,
            slots=stage2_slots,
            style_context=style_context,
            prev_segment=_summary_seg(prev_beat),
            next_segment=_summary_seg(next_beat),
            tail_anchor=bool(config.s9_tail_anchor),
            include_first_frame=bool(config.s9_include_first_frame and any(s.asset_kind == "first_frame" for s in stage2_slots)),
            user_guidance=effective_s9_guidance,
            source_text=scoped_source_text,
            current_prompt=reference_prompt,
            scene_context_notes=scene_context_notes,
            continuity_anchor=continuity_anchor,
        )
        config.final_prompt = mark_seedance2_prompt_references_for_editor(rendered_s9)
        if not config.duration_user_set:
            config.duration = planned_dur
        config.prompt_style = "s9_segment"
        config.prompt_source = "s9_script"
        config.prompt_inputs_hash = inputs_hash
        config.prompt_updated_at = datetime.now().isoformat(timespec="seconds")
        saved_json = dump_seedance2_config(config)
        beat["seedance2_config_json"] = saved_json

        from novelvideo.models import build_scene_ref, real_detected_identities, real_detected_props

        asset_patch: dict[str, Any] = {}
        if not real_detected_identities(beat.get("detected_identities")):
            recovered_ids = [
                s.asset_id for s in stage2_slots if s.asset_kind == "identity" and s.asset_id
            ]
            if recovered_ids:
                asset_patch["detected_identities"] = recovered_ids
                beat["detected_identities"] = recovered_ids
        if not real_detected_props(beat.get("detected_props")):
            recovered_props = [
                s.asset_id for s in stage2_slots if s.asset_kind == "prop" and s.asset_id
            ]
            if recovered_props:
                asset_patch["detected_props"] = recovered_props
                beat["detected_props"] = recovered_props
        existing_scene = beat.get("scene_ref") or {}
        if not (isinstance(existing_scene, dict) and str(existing_scene.get("scene_id") or "").strip()):
            scene_slot = next((s for s in stage2_slots if s.asset_kind == "scene" and s.asset_id), None)
            if scene_slot is not None:
                sref = build_scene_ref(scene_slot.asset_id, scene_slot.variant_id).model_dump()
                asset_patch["scene_ref"] = sref
                beat["scene_ref"] = sref
        if stage2_slots:
            import json as _json

            updated_meta = (s9_meta or S9SegmentMeta()).model_copy(
                update={"segment_asset_slots": stage2_slots}
            )
            meta_json = _json.dumps(updated_meta.model_dump(mode="json"), ensure_ascii=False)
            asset_patch["s9_segment_meta_json"] = meta_json
            beat["s9_segment_meta_json"] = meta_json
            beat["s9_segment_meta"] = updated_meta.model_dump(mode="json")

        await store.update_beat_asset(
            episode_number=episode,
            beat_number=int(beat.get("beat_number") or 0),
            seedance2_config_json=saved_json,
            **asset_patch,
        )
        return saved_json

    generate_kwargs: dict[str, Any] = {
        "mode": config.mode,
        "beat": prompt_beat,
        "assets": assets,
        "text_overlay": config.text_overlay,
        "prompt_guidance": config.prompt_guidance,
        "prompt_guidance_template_keys": config.prompt_guidance_template_keys,
        "request_params": {
            "duration": int(config.duration),
            "resolution": config.resolution,
            "ratio": config.ratio,
        },
        "manual_prompt_reference": reference_prompt,
        "composer": composer,
        "prev_beat": prev_beat,
        "next_beat": next_beat,
        "language": output_language,
    }
    if effective_style:
        generate_kwargs["prompt_style"] = effective_style
    if effective_style or video_backend:
        generate_kwargs["min_duration"] = min_duration
        generate_kwargs["max_duration"] = max_duration
    result = await generate_seedance2_prompt(**generate_kwargs)
    config.final_prompt = mark_seedance2_prompt_references_for_editor(result.prompt)
    if not config.duration_user_set:
        recommended = getattr(result, "recommended_duration", None)
        resolved_duration = (
            max(duration_floor, int(recommended))
            if isinstance(recommended, int) and recommended > 0
            else duration_floor
        )
        config.duration = max(min_duration, min(max_duration, resolved_duration))
    config.prompt_source = "generated" if result.used_ai else "fallback"
    config.prompt_inputs_hash = inputs_hash
    config.prompt_updated_at = datetime.now().isoformat(timespec="seconds")
    saved_json = dump_seedance2_config(config)
    beat["seedance2_config_json"] = saved_json
    await store.update_beat_asset(
        episode_number=episode,
        beat_number=int(beat.get("beat_number") or 0),
        seedance2_config_json=saved_json,
    )
    return saved_json


async def save_seedance2_uploaded_asset(
    *,
    store: Any,
    episode: int,
    beat: dict[str, Any],
    project_dir: Path,
    filename: str,
    content: bytes,
    content_type: str = "",
) -> Path | None:
    media_kind = _seedance2_uploaded_media_kind(filename, content_type)
    if not media_kind or not content:
        return None
    beat_num = int(beat.get("beat_number") or 0)
    resource_key = seedance2_asset_resource_key(
        project_dir=project_dir,
        episode=episode,
        beat_number=beat_num,
    )

    async def finalize(prepared: tuple[Path, int, str] | None) -> Path | None:
        if prepared is None:
            return None
        target, beat_num, saved_json = prepared
        await store.update_beat_asset(
            episode_number=episode,
            beat_number=beat_num,
            seedance2_config_json=saved_json,
        )
        beat["seedance2_config_json"] = saved_json
        return target

    async with voice_resource_lock(resource_key):
        return await run_voice_media_operation(
            _prepare_seedance2_uploaded_asset,
            episode=episode,
            beat=beat,
            project_dir=project_dir,
            filename=filename,
            content=content,
            content_type=content_type,
            worker_limiter=_seedance2_asset_limiter(),
            finalize=finalize,
        )


async def save_seedance2_uploaded_file(
    *,
    store: Any,
    episode: int,
    beat: dict[str, Any],
    project_dir: Path,
    filename: str,
    upload_stream: Any,
    content_type: str = "",
) -> Path | None:
    """Read and publish an upload in one bounded voice-worker transaction."""

    beat_num = int(beat.get("beat_number") or 0)
    resource_key = seedance2_asset_resource_key(
        project_dir=project_dir,
        episode=episode,
        beat_number=beat_num,
    )

    async def finalize(prepared: tuple[Path, int, str] | None) -> Path | None:
        if prepared is None:
            return None
        target, prepared_beat_num, saved_json = prepared
        await store.update_beat_asset(
            episode_number=episode,
            beat_number=prepared_beat_num,
            seedance2_config_json=saved_json,
        )
        beat["seedance2_config_json"] = saved_json
        return target

    async with voice_resource_lock(resource_key):
        return await run_voice_media_operation(
            _read_and_prepare_seedance2_uploaded_asset,
            upload_stream=upload_stream,
            episode=episode,
            beat=beat,
            project_dir=project_dir,
            filename=filename,
            content_type=content_type,
            worker_limiter=_seedance2_asset_limiter(),
            finalize=finalize,
        )


def _read_and_prepare_seedance2_uploaded_asset(
    *,
    upload_stream: Any,
    episode: int,
    beat: dict[str, Any],
    project_dir: Path,
    filename: str,
    content_type: str,
) -> tuple[Path, int, str] | None:
    return _prepare_seedance2_uploaded_asset(
        episode=episode,
        beat=beat,
        project_dir=project_dir,
        filename=filename,
        content=upload_stream.read(),
        content_type=content_type,
    )


def _prepare_seedance2_uploaded_asset(
    *,
    episode: int,
    beat: dict[str, Any],
    project_dir: Path,
    filename: str,
    content: bytes,
    content_type: str,
) -> tuple[Path, int, str] | None:
    """Publish an upload and prepare its metadata entirely in a voice worker."""

    media_kind = _seedance2_uploaded_media_kind(filename, content_type)
    if not media_kind or not content:
        return None

    beat_num = int(beat.get("beat_number") or 0)
    upload_dir = (
        Path(project_dir)
        / "seedance2_uploads"
        / f"ep{episode:03d}"
        / f"beat_{beat_num:02d}"
        / media_kind
    )
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = _next_available_upload_path(upload_dir, filename)

    config = parse_seedance2_config(beat.get("seedance2_config_json"))
    path_value = str(target)
    if media_kind == "images":
        config.reference_image_paths = _unique_paths(
            list(config.reference_image_paths) + [path_value]
        )
    else:
        config.reference_audio_paths = _unique_paths(
            list(config.reference_audio_paths) + [path_value]
        )

    saved_json = dump_seedance2_config(config)
    target.write_bytes(bytes(content))
    return target, beat_num, saved_json


async def crop_seedance2_asset_to_reference(
    *,
    store: Any,
    episode: int,
    beat: dict[str, Any],
    project_dir: Path,
    asset_key: str,
    source_path: str | Path,
    crop_data: dict[str, Any],
) -> Path | None:
    beat_num = int(beat.get("beat_number") or 0)
    resource_key = seedance2_asset_resource_key(
        project_dir=project_dir,
        episode=episode,
        beat_number=beat_num,
    )

    async def finalize(
        prepared: tuple[Path | None, str | None]
    ) -> Path | None:
        output_path, saved_json = prepared
        if output_path is None:
            return None
        if saved_json is not None:
            await store.update_beat_asset(
                episode_number=episode,
                beat_number=beat_num,
                seedance2_config_json=saved_json,
            )
            beat["seedance2_config_json"] = saved_json
        return output_path

    async with voice_resource_lock(resource_key):
        return await run_voice_media_operation(
            _prepare_cropped_seedance2_asset,
            episode=episode,
            beat=beat,
            project_dir=project_dir,
            asset_key=asset_key,
            source_path=source_path,
            crop_data=crop_data,
            worker_limiter=_seedance2_asset_limiter(),
            finalize=finalize,
        )


def _prepare_cropped_seedance2_asset(
    *,
    episode: int,
    beat: dict[str, Any],
    project_dir: Path,
    asset_key: str,
    source_path: str | Path,
    crop_data: dict[str, Any],
) -> tuple[Path | None, str | None]:
    source = Path(source_path)
    if not source.exists():
        return None, None
    width = int(crop_data.get("width") or 0)
    height = int(crop_data.get("height") or 0)
    if width <= 0 or height <= 0:
        return None, None

    beat_num = int(beat.get("beat_number") or 0)
    target = str(crop_data.get("target") or "reference_image")
    if target in {"first_frame", "last_frame"}:
        paths = PathResolver(project_dir, episode)
        output_path = paths.video_input_frame(beat_num, slot=target)
        saved_json = None
    else:
        output_path = (
            Path(project_dir)
            / "seedance2_crops"
            / f"ep{episode:03d}"
            / f"beat_{beat_num:02d}"
            / f"{_seedance2_safe_asset_key(str(asset_key or 'asset'))}.png"
        )
        config = parse_seedance2_config(beat.get("seedance2_config_json"))
        config.reference_image_paths = _unique_paths(
            list(config.reference_image_paths) + [str(output_path)]
        )
        saved_json = dump_seedance2_config(config)

    _crop_image_to_path_sync(
        source,
        x=int(crop_data.get("x") or 0),
        y=int(crop_data.get("y") or 0),
        width=width,
        height=height,
        output_path=output_path,
    )
    if validate_seedance2_reference_image(output_path):
        return None, None
    if target in {"first_frame", "last_frame"}:
        paths.write_video_input_frame_meta(beat_num, slot=target, source_path=source)
    return output_path, saved_json


def _project_relative_path(project_dir: Path, path: Path) -> str:
    try:
        return path.relative_to(project_dir).as_posix()
    except ValueError:
        return str(path)


def _archive_narrator_voice_siblings(target: Path) -> None:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    for ext in VOICE_SAMPLE_EXTENSIONS:
        sibling = target.with_suffix(ext)
        if sibling.exists():
            sibling.replace(sibling.with_name(f"{sibling.stem}_{stamp}{sibling.suffix}"))


def _resolve_project_audio_source(project_dir: Path, source_path: str | Path) -> Path:
    root = Path(project_dir).resolve()
    raw_path = Path(source_path)
    source = raw_path if raw_path.is_absolute() else root / raw_path
    source = source.resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise ValueError("请选择项目内有效的音频文件") from exc
    if not source.exists() or not source.is_file() or source.suffix.lower() not in VOICE_SAMPLE_EXTENSIONS:
        raise ValueError("请选择项目内有效的音频文件")
    return source


async def trim_seedance2_audio_to_reference(
    *,
    store: Any,
    episode: int,
    beat: dict[str, Any],
    project_dir: Path,
    asset_key: str,
    source_path: str | Path,
    start_seconds: float = 0.0,
    duration_seconds: float = 4.0,
) -> Path | None:
    beat_num = int(beat.get("beat_number") or 0)
    if str(asset_key or "").strip() == "voice:narrator":
        resource_key = narrator_voice_resource_key(
            project_dir=project_dir,
            state_dir=str(getattr(store, "state_dir", "") or ""),
        )
    else:
        resource_key = seedance2_asset_resource_key(
            project_dir=project_dir,
            episode=episode,
            beat_number=beat_num,
        )

    async def finalize(prepared: tuple[Path, int | None, str | None]) -> Path:
        target, beat_num, saved_json = prepared
        if beat_num is not None and saved_json is not None:
            await store.update_beat_asset(
                episode_number=episode,
                beat_number=beat_num,
                seedance2_config_json=saved_json,
            )
            beat["seedance2_config_json"] = saved_json
        return target

    async with voice_resource_lock(resource_key):
        return await run_voice_media_operation(
            _prepare_trimmed_seedance2_audio,
            store=store,
            episode=episode,
            beat=beat,
            project_dir=project_dir,
            asset_key=asset_key,
            source_path=source_path,
            start_seconds=start_seconds,
            duration_seconds=duration_seconds,
            finalize=finalize,
        )


def _prepare_trimmed_seedance2_audio(
    *,
    store: Any,
    episode: int,
    beat: dict[str, Any],
    project_dir: Path,
    asset_key: str,
    source_path: str | Path,
    start_seconds: float,
    duration_seconds: float,
) -> tuple[Path, int | None, str | None]:
    """Read, trim, publish, and prepare config in one bounded worker."""

    source = _resolve_project_audio_source(Path(project_dir), source_path)
    content, _filename = trim_voice_sample_content(
        source.read_bytes(),
        filename=source.name,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
    )

    if str(asset_key or "").strip() == "voice:narrator":
        state_dir = str(getattr(store, "state_dir", "") or "")
        if not state_dir:
            raise ValueError("项目 state_dir 未解析，无法保存解说声线")
        target = Path(project_dir) / "assets" / "narrator" / "voice.mp3"
        target.parent.mkdir(parents=True, exist_ok=True)
        _archive_narrator_voice_siblings(target)
        target.write_bytes(content)
        set_narrator_reference_audio_in_state_dir(
            state_dir,
            relative_path=_project_relative_path(Path(project_dir), target),
            sha256=voice_content_sha256(content),
        )
        return target, None, None

    beat_num = int(beat.get("beat_number") or 0)
    output_path = (
        Path(project_dir)
        / "seedance2_crops"
        / f"ep{episode:03d}"
        / f"beat_{beat_num:02d}"
        / f"{_seedance2_safe_asset_key(str(asset_key or 'audio'))}_trimmed.mp3"
    )

    config = parse_seedance2_config(beat.get("seedance2_config_json"))
    config.reference_audio_paths = _unique_paths(
        list(config.reference_audio_paths) + [str(output_path)]
    )
    saved_json = dump_seedance2_config(config)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    return output_path, beat_num, saved_json


async def remove_seedance2_uploaded_asset(
    *,
    store: Any,
    episode: int,
    beat: dict[str, Any],
    media_kind: str,
    path: str,
    project_dir: Path | None = None,
) -> bool:
    beat_num = int(beat.get("beat_number") or 0)
    root = _seedance2_asset_project_dir(
        project_dir=project_dir,
        store=store,
        asset_path=path,
    )
    resource_key = seedance2_asset_resource_key(
        project_dir=root,
        episode=episode,
        beat_number=beat_num,
    )

    async def finalize(prepared: tuple[bool, str | None]) -> bool:
        removed, saved_json = prepared
        if not removed or saved_json is None:
            return False
        await store.update_beat_asset(
            episode_number=episode,
            beat_number=beat_num,
            seedance2_config_json=saved_json,
        )
        beat["seedance2_config_json"] = saved_json
        return True

    async with voice_resource_lock(resource_key):
        return await run_voice_media_operation(
            _prepare_removed_seedance2_asset,
            beat=beat,
            media_kind=media_kind,
            path=path,
            worker_limiter=metadata_io_limiter(),
            finalize=finalize,
        )


def _prepare_removed_seedance2_asset(
    *, beat: dict[str, Any], media_kind: str, path: str
) -> tuple[bool, str | None]:
    config = parse_seedance2_config(beat.get("seedance2_config_json"))
    paths = config.reference_image_paths if media_kind == "images" else config.reference_audio_paths
    path_value = str(path)
    if path_value not in paths:
        return False, None
    paths[:] = [existing for existing in paths if existing != path_value]
    _seedance2_unlink_user_reference_file(path_value)
    return True, dump_seedance2_config(config)


def _seedance2_asset_project_dir(
    *, project_dir: Path | None, store: Any, asset_path: str | Path
) -> Path:
    if project_dir is not None:
        return Path(project_dir)
    stored_project_dir = str(getattr(store, "project_dir", "") or "")
    if stored_project_dir:
        return Path(stored_project_dir)
    candidate = Path(asset_path)
    for parent in candidate.parents:
        if parent.name in {"seedance2_uploads", "seedance2_crops"}:
            return parent.parent
    return candidate.parent


def _seedance2_panel_project_dir(
    *,
    project_dir: Path | None,
    store: Any,
    fallback: str | Path | None = None,
) -> Path:
    """Resolve the canonical project root used by all per-beat asset writers."""

    if project_dir is not None:
        return Path(project_dir)
    stored_project_dir = str(getattr(store, "project_dir", "") or "")
    if stored_project_dir:
        return Path(stored_project_dir)
    if fallback is not None:
        return Path(fallback)
    return Path(".")


def build_seedance2_video_panel_state(
    *,
    project_dir: Path,
    episode: int,
    beat: dict[str, Any],
    state_dir: str | Path,
    next_beat: dict[str, Any] | None = None,
    characters: list[Any] | None = None,
    prop_menu: list[Any] | None = None,
    language: AssetLanguage | None = None,
) -> Seedance2VideoPanelState:
    default_ratio = default_seedance2_ratio_for_state_dir(state_dir)
    duration_floor = _seedance2_duration_floor(
        project_dir=project_dir,
        episode=episode,
        beat=beat,
    )
    config = parse_seedance2_config(
        beat.get("seedance2_config_json"),
        default_ratio=default_ratio,
        default_duration=duration_floor,
    )
    duration = max(int(config.duration or duration_floor), duration_floor)
    assets = build_seedance2_project_assets(
        project_output=project_dir,
        episode=episode,
        beat=beat,
        mode=config.mode,
        next_beat=next_beat,
        characters=characters,
        prop_menu=prop_menu,
        state_dir=state_dir,
    )
    _append_seedance2_user_reference_assets(assets, config)
    prompt_source = config.prompt_source or "saved"
    final_prompt = config.final_prompt
    initial_prompt = _seedance2_initial_prompt(beat)
    output_language = language or detect_seedance2_prompt_language(beat)
    if not final_prompt and initial_prompt:
        final_prompt = _seedance2_default_prompt(
            beat=_beat_with_seedance2_initial_prompt(beat, initial_prompt),
            config=config,
            assets=assets,
            text_overlay=config.text_overlay,
            language=output_language,
        )
        prompt_source = "fallback"
    assets = apply_prompt_audio_selection(assets, final_prompt)
    s9_missing_assets: list[dict[str, str]] = []
    s9_effective_prompt = final_prompt
    from novelvideo.s9_storyboard import (
        is_s9_segment_beat,
        resolve_s9_active_assets_and_prompt,
    )

    if is_s9_segment_beat(beat) and config.mode == Seedance2I2VMode.MULTIMODAL_REFERENCE:
        s9_effective_prompt, assets, s9_missing_assets = resolve_s9_active_assets_and_prompt(
            beat,
            assets=assets,
            prompt=final_prompt,
            include_first_frame=bool(config.s9_include_first_frame),
        )
    current_prompt_inputs_hash = _seedance2_prompt_inputs_hash(
        config=config,
        beat=beat,
        assets=assets,
        language=output_language,
    )
    return Seedance2VideoPanelState(
        mode=config.mode.value,
        duration=duration,
        duration_floor=duration_floor,
        resolution=config.resolution,
        ratio=config.ratio,
        generate_audio=config.generate_audio,
        return_last_frame=config.return_last_frame,
        human_review=config.human_review,
        prompt_guidance=config.prompt_guidance,
        final_prompt=final_prompt,
        prompt_source=prompt_source,
        text_overlay=dict(config.text_overlay or {}),
        assets=assets,
        storyboard_context=_seedance2_storyboard_context(beat),
        prompt_inputs_hash=config.prompt_inputs_hash,
        current_prompt_inputs_hash=current_prompt_inputs_hash,
        prompt_status=_seedance2_prompt_status(config, current_prompt_inputs_hash),
        s9_missing_assets=s9_missing_assets,
        s9_effective_prompt=s9_effective_prompt,
    )


def mark_seedance2_prompt_references_for_editor(prompt: str) -> str:
    text = str(prompt or "")
    text = re.sub(r"@@+(图片|音频)\s*(\d+)", r"@\1\2", text)
    text = re.sub(r"@?(图片|音频)\s+(\d+)", r"@\1\2", text)
    text = re.sub(r"(?<!@)(图片|音频)(\d+)", r"@\1\2", text)
    return text


def _unique_paths(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _sync_seedance2_asset_paths(
    *,
    config: Any,
    project_dir: Path,
    episode: int,
    beat: dict[str, Any],
    state_dir: str | Path,
    next_beat: dict[str, Any] | None = None,
    prop_menu: list[Any] | None = None,
) -> None:
    assets = build_seedance2_project_assets(
        project_output=Path(project_dir),
        episode=episode,
        beat=beat,
        mode=config.mode,
        state_dir=state_dir,
        next_beat=next_beat,
        prop_menu=prop_menu,
    )
    _append_seedance2_user_reference_assets(assets, config)
    assets = apply_prompt_audio_selection(assets, str(config.final_prompt or ""))
    auto_images = selected_reference_paths(assets, "reference_images")
    auto_audios = selected_reference_paths(assets, "reference_audios")
    extra_images = [path for path in config.reference_image_paths if path not in auto_images]
    extra_audios: list[str] = []
    config.reference_image_paths = _unique_paths(auto_images + extra_images)
    config.reference_audio_paths = _unique_paths(auto_audios + extra_audios)


def _seedance2_user_reference_paths(
    config_paths: list[str],
    auto_paths: set[str],
) -> list[str]:
    return [
        str(path)
        for path in config_paths
        if str(path or "").strip() and str(path) not in auto_paths
    ]


def _append_seedance2_user_reference_assets(
    assets: list[Seedance2ResolvedAsset],
    config: Any,
) -> None:
    resolve_beat_video_references(
        list(config.reference_image_paths),
        list(config.reference_audio_paths),
        assets=assets,
    )


def _seedance2_default_prompt(
    *,
    beat: dict[str, Any],
    config: Any,
    assets: list[Seedance2ResolvedAsset],
    text_overlay: dict[str, Any],
    language: str = "zh",
) -> str:
    return build_seedance2_prompt_draft(
        mode=config.mode,
        beat=beat,
        assets=assets,
        text_overlay=text_overlay,
        prompt_guidance=config.prompt_guidance,
        prompt_guidance_template_keys=config.prompt_guidance_template_keys,
        language=language,
    )


def _seedance2_initial_prompt(beat: dict[str, Any]) -> str:
    return str(beat.get("video_prompt") or beat.get("keyframe_prompt") or "").strip()


def _beat_with_seedance2_initial_prompt(
    beat: dict[str, Any],
    initial_prompt: str,
) -> dict[str, Any]:
    if not initial_prompt:
        return beat
    updated = dict(beat)
    if not str(updated.get("video_prompt") or updated.get("keyframe_prompt") or "").strip():
        updated["video_prompt"] = initial_prompt
    return updated


def _seedance2_uploaded_media_kind(filename: str, content_type: str) -> str | None:
    name = str(filename or "").lower()
    mime = str(content_type or "").lower()
    if mime.startswith("image/"):
        return "images"
    if mime.startswith("audio/"):
        return "audios"
    if name.endswith((".png", ".jpg", ".jpeg", ".webp")):
        return "images"
    if name.endswith((".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")):
        return "audios"
    return None


def _safe_upload_name(filename: str, default: str = "seedance2_asset") -> str:
    name = Path(str(filename or default)).name.strip()
    return name or default


def _next_available_upload_path(upload_dir: Path, filename: str) -> Path:
    target = upload_dir / _safe_upload_name(filename)
    if not target.exists():
        return target
    for index in range(1, 1000):
        candidate = target.with_name(f"{target.stem}_{index}{target.suffix}")
        if not candidate.exists():
            return candidate
    return target.with_name(f"{target.stem}_latest{target.suffix}")


def _seedance2_safe_asset_key(asset_key: str) -> str:
    return (
        "".join(
            char if char.isalnum() or char in {"_", ".", "-"} else "_" for char in asset_key
        ).strip(".")
        or "asset"
    )


def _seedance2_unlink_user_reference_file(path: str | Path) -> bool:
    text = str(path or "").strip()
    if not text or text.startswith(("http://", "https://")):
        return False
    file_path = Path(text)
    if not file_path.exists() or not file_path.is_file():
        return False
    if "seedance2_uploads" not in file_path.parts and "seedance2_crops" not in file_path.parts:
        return False
    try:
        file_path.unlink()
    except OSError:
        return False
    return True


def _seedance2_prompt_inputs_hash(
    *,
    config: Any,
    beat: dict[str, Any],
    assets: list[Seedance2ResolvedAsset],
    language: str = "zh",
) -> str:
    return compute_seedance2_prompt_inputs_hash(
        mode=config.mode,
        beat=beat,
        assets=assets,
        text_overlay=config.text_overlay,
        prompt_guidance=config.prompt_guidance,
        prompt_guidance_template_keys=config.prompt_guidance_template_keys,
        language=language,
        prompt_style=str(getattr(config, "prompt_style", "") or ""),
    )


def _seedance2_prompt_status(config: Any, current_hash: str) -> str:
    if not str(config.final_prompt or "").strip():
        return "未生成"
    if str(config.prompt_source or "") in {"manual", "edited"}:
        return "已手动编辑"
    if config.prompt_inputs_hash and config.prompt_inputs_hash != current_hash:
        return "Prompt 已过期"
    if config.prompt_source == "generated":
        return "AI 生成"
    if config.prompt_source == "fallback":
        return "规则草稿"
    return "旧提示词"


def _seedance2_storyboard_context(beat: dict[str, Any]) -> list[tuple[str, str]]:
    spoken_text = ""
    if normalize_seedance2_audio_type(beat) in {"narration", "dialogue"}:
        spoken_text = str(
            beat.get("narration_segment") or beat.get("dialogue") or beat.get("narration") or ""
        ).strip()
    rows = [
        (
            "分镜概要",
            str(beat.get("synopsis") or beat.get("visual_description") or "").strip(),
        ),
        (
            "旧视频提示词",
            str(beat.get("video_prompt") or beat.get("keyframe_prompt") or "").strip(),
        ),
        ("旁白/对话", spoken_text),
    ]
    return [(label, value) for label, value in rows if value]


def _seedance2_duration_floor(*, project_dir: Path, episode: int, beat: dict[str, Any]) -> int:
    beat_num = int(beat.get("beat_number") or 0)
    audio_duration = None
    audio_path = project_dir / "audio" / f"ep{episode:03d}" / f"beat_{beat_num:02d}.mp3"
    if normalize_seedance2_audio_type(beat) in {"narration", "dialogue"} and audio_path.exists():
        try:
            audio_duration = get_audio_duration(str(audio_path))
        except Exception:
            audio_duration = None
    floor = resolve_target_video_duration(beat, audio_duration)
    try:
        return max(4, int(math.ceil(float(floor))))
    except (TypeError, ValueError):
        return 4


__all__ = [
    "SEEDANCE2_PROMPT_GUIDANCE_TEMPLATES",
    "Seedance2VideoPanelState",
    "append_seedance2_prompt_guidance_template",
    "build_seedance2_video_panel_state",
    "crop_seedance2_asset_to_reference",
    "generate_seedance2_prompt_for_panel",
    "mark_seedance2_prompt_references_for_editor",
    "remove_seedance2_uploaded_asset",
    "save_seedance2_uploaded_asset",
    "save_seedance2_video_panel_config",
    "trim_seedance2_audio_to_reference",
]
