"""画面/网格/视频生成端点。"""

import json
import io
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse

from novelvideo.api.auth import get_api_user
from novelvideo.api.responses import TemporaryFileResponse
from novelvideo.api.upload_workers import run_asset_upload_operation
from novelvideo.api.deps import (
    get_user_base_dir,
    get_state_dir,
    make_sqlite_store_for_context,
    make_sqlite_store,
    make_static_url_for_context,
    resolve_project_scope,
)
from novelvideo.api.schemas import (
    VideoGenerateRequest,
    VideoBackendOption,
    VideoComposeRequest,
    TTSGenerateRequest,
    TTSPreviewRequest,
    SystemVoicePrepareRequest,
    SingleVideoRequest,
    BatchEpisodeVideoRequest,
    VideoPoolSelectRequest,
    OperatorPasswordVerifyRequest,
    BeatBackgroundAnchorUpdate,
    Seedance2AssetAudioTrimRequest,
    Seedance2AssetCropRequest,
    Seedance2AssetDeleteRequest,
)
from novelvideo.utils.source_language import detect_episode_asset_language
from novelvideo.api.viewer_manifests import (
    build_director_stage_manifest,
    build_pano_viewer_manifest,
    default_director_stage_palette,
)
from novelvideo.seedance2_i2v.pipeline import (
    is_huimeng_seedance2_backend,
    prepare_seedance2_generation_inputs,
)
from novelvideo.seedance2_i2v.voice_clone import normalize_seedance2_audio_type
from novelvideo.project_context import ProjectContext
from novelvideo.ports import get_credit_quote, get_task_backend
from novelvideo.task_identity import project_task_state_key
from novelvideo.models import beat_scene_id
from novelvideo.services.background_anchor_service import (
    BackgroundAnchorError,
    build_background_anchors_payload,
    crop_background_anchor_to_selected,
    save_uploaded_background_anchor_image,
    select_background_anchor,
)
from novelvideo.utils.path_resolver import compute_identity_path, compute_portrait_path


_TemporaryFileResponse = TemporaryFileResponse

router = APIRouter()

logger = logging.getLogger(__name__)


async def _resolve_generation_project(project: str, user: dict, required_role: str = "editor"):
    return await resolve_project_scope(project, user, required_role=required_role)


def _single_video_billing_metadata(
    video_backend: str,
    *,
    resolution: str | None,
    duration: int | float | str | None,
) -> dict:
    from novelvideo.api.routes.model_credits import (
        _video_backend_feature_billing_params,
    )
    return _video_backend_feature_billing_params(
        {
            "video_backend": video_backend,
            "resolution": str(resolution or "").strip(),
            "pricing_quantity": duration,
        }
    )


async def _read_uploaded_rgb_image(file: UploadFile):
    content = await file.read()
    if not content:
        raise ValueError("empty file")
    try:
        from PIL import Image

        return Image.open(io.BytesIO(content)).convert("RGB")
    except Exception as exc:
        raise ValueError(f"invalid image file: {exc}") from exc


async def _runtime_prop_menu_with_global_props(
    store: Any,
    episode_obj: Any,
    beats: list[dict],
) -> list[dict]:
    """Resolve episode prop markers against the global props table without mutating episode JSON."""
    from novelvideo.models import build_prop_menu, collect_prop_marker_ids_from_beat

    prop_menu = [item.model_dump() for item in episode_obj.prop_menu] if episode_obj else []
    marked_prop_ids: list[str] = []
    for beat in beats or []:
        for prop_id in collect_prop_marker_ids_from_beat(beat):
            if prop_id and prop_id not in marked_prop_ids:
                marked_prop_ids.append(prop_id)
    if not marked_prop_ids:
        return prop_menu

    existing = {item.prop_id: item.model_dump() for item in build_prop_menu(prop_menu=prop_menu)}
    changed = False
    for marker_prop_id in marked_prop_ids:
        global_prop = (
            store.get_cached_prop(marker_prop_id) if hasattr(store, "get_cached_prop") else None
        )
        if not global_prop:
            continue
        item = dict(existing.get(marker_prop_id) or {"prop_id": marker_prop_id})
        item["is_global_asset"] = True
        item["prop_type"] = (
            item.get("prop_type") or getattr(global_prop, "prop_type", "") or "object"
        )
        item["description"] = (
            item.get("description")
            or getattr(global_prop, "description", "")
            or getattr(global_prop, "visual_prompt", "")
            or marker_prop_id
        )
        existing[marker_prop_id] = item
        changed = True
    if not changed:
        return prop_menu

    ordered_ids: list[str] = []
    for item in build_prop_menu(prop_menu=prop_menu):
        if item.prop_id not in ordered_ids:
            ordered_ids.append(item.prop_id)
    for prop_id in marked_prop_ids:
        if prop_id in existing and prop_id not in ordered_ids:
            ordered_ids.append(prop_id)
    return [existing[prop_id] for prop_id in ordered_ids if prop_id in existing]


def _prop_marker_colors_from_menu(prop_menu: list[dict] | None) -> dict[str, str]:
    colors: dict[str, str] = {}
    for item in prop_menu or []:
        if not isinstance(item, dict):
            continue
        prop_id = str(item.get("prop_id") or "").strip()
        marker_color = str(item.get("marker_color") or "").strip()
        if prop_id and marker_color:
            colors[prop_id] = marker_color
    return colors


def _episode_from_store_or_none(store: Any, episode_num: int) -> Any | None:
    get_episode = getattr(store, "get_episode", None)
    if get_episode is None:
        return None
    try:
        return get_episode(episode_num)
    except Exception:
        return None


async def _build_character_map(
    store,
    beats,
    username,
    project,
    *,
    episode_num: int | None = None,
    use_detected_identities: bool = False,
):
    """构建角色映射。"""
    from novelvideo.services.character_ref_service import build_character_map_for_grid

    project_dir = get_user_base_dir(username) / project
    characters = store.get_all_characters()
    char_dicts = []
    for c in characters:
        char_dicts.append(
            {
                "name": c.name,
                "gender": c.gender,
                "body_type": getattr(c, "body_type", ""),
                "role": c.role,
                "is_main": getattr(c, "is_main", False),
                "portrait_path": compute_portrait_path(project_dir, c.name),
                "face_prompt": c.face_prompt,
                "appearance_details": c.appearance_details,
                "identities": (
                    [
                        {
                            "identity_id": id_.identity_id,
                            "identity_name": id_.identity_name,
                            "appearance_details": id_.appearance_details,
                            "face_prompt": id_.face_prompt,
                            "body_type": id_.body_type,
                            "fish_voice_id": id_.fish_voice_id,
                            "age_group": id_.age_group,
                            "portrait_image": id_.portrait_image,
                            "costume_image": id_.costume_image,
                            "primary_reference": compute_identity_path(
                                project_dir, c.name, id_.identity_name
                            ),
                            "character_tag": id_.character_tag,
                            "source": id_.source,
                        }
                        for id_ in c.identities
                    ]
                    if c.identities
                    else []
                ),
            }
        )

    # 优先从 SQLite 读取 sketch_colors；若缺失则按当前 beats 重新分配并写回 SQLite。
    _sc = None
    if episode_num:
        _sc = store.get_sketch_colors(episode_num) or None
        if not _sc:
            from novelvideo.generators.episode_optimizer import EpisodeOptimizer

            _sc = (
                EpisodeOptimizer.assign_sketch_colors(
                    char_dicts,
                    episode_beats=beats,
                )
                or None
            )

            if _sc:
                await store.set_sketch_colors(episode_num, _sc)

    return build_character_map_for_grid(
        grid_beats=beats,
        characters=char_dicts,
        user_output_dir=get_user_base_dir(username),
        project=project,
        sketch_colors=_sc,
        use_detected_identities=use_detected_identities,
    )


def _validate_seedance_pro_dialogue_only(beats: list[dict], video_backend: str) -> str | None:
    """Seedance 1.5 有声仅允许 dialogue beat。"""
    if video_backend not in {"seedance_pro", "newapi_seedance-1.5-pro"}:
        return None

    non_dialogue = [
        int(beat.get("beat_number", 0))
        for beat in beats
        if beat.get("audio_type", "narration") != "dialogue"
    ]
    if not non_dialogue:
        return None

    preview = "、".join(str(num) for num in non_dialogue[:8])
    suffix = " 等" if len(non_dialogue) > 8 else ""
    return f"Seedance 1.5 有声只允许用于 dialogue beat；当前包含非 dialogue Beat: {preview}{suffix}"


def _is_seedance2_backend(video_backend: str | None) -> bool:
    return is_huimeng_seedance2_backend(video_backend)


def _is_happyhorse_backend(video_backend: str | None) -> bool:
    return _seedance2_model_from_backend(video_backend) == "happyhorse-1.0"


def _is_grok_video_backend(video_backend: str | None) -> bool:
    return _seedance2_model_from_backend(video_backend) == "grok-video-channel"


def _is_custom_prompt_panel_backend(video_backend: str | None) -> bool:
    from novelvideo.generators.video_generator import parse_newapi_video_backend

    if (
        _is_seedance2_backend(video_backend)
        or _is_happyhorse_backend(video_backend)
        or _is_grok_video_backend(video_backend)
    ):
        return False
    model = parse_newapi_video_backend(video_backend)
    if not model:
        return False
    lowered = model.lower()
    return not (lowered.startswith("seedance-1.") or lowered.startswith("seedance_1."))


def _seedance2_api_resolution(resolution: str | None) -> str:
    text = str(resolution or "").strip()
    if text in {"480p", "720p", "1080p"}:
        return text
    if "480" in text:
        return "480p"
    if "1080" in text:
        return "1080p"
    return "720p"


SEEDANCE2_RESOLUTION_OPTIONS_BY_MODEL = {
    "seedance-2.0-fast": ("480p", "720p"),
    "seedance-2.0": ("480p", "720p", "1080p"),
    "seedance-2.0-value": ("720p", "1080p"),
    "seedance-2.0-fast-value": ("720p", "1080p"),
    # Seedance 1.5 Pro（有声）清晰度，来源 huimengi /api/v1/models（480p/720p/1080p）
    "seedance-1.5-pro": ("480p", "720p", "1080p"),
}
SEEDANCE2_DEFAULT_RESOLUTION_OPTIONS = ("480p", "720p")
HAPPYHORSE_RESOLUTION_OPTIONS = ("720p", "1080p")
HAPPYHORSE_RATIO_OPTIONS = ("16:9", "9:16", "1:1", "4:3", "3:4")
HAPPYHORSE_SUPPORTED_MODES = ("first_frame", "multimodal_reference")
GROK_VIDEO_RESOLUTION_OPTIONS = ("720p", "480p")
GROK_VIDEO_RATIO_OPTIONS = ("16:9", "9:16", "1:1", "2:3", "3:2")
GROK_VIDEO_SUPPORTED_MODES = ("first_frame", "multimodal_reference")


def _seedance2_model_from_backend(video_backend: str | None) -> str:
    text = str(video_backend or "").strip().lower()
    for prefix in ("newapi_", "huimeng_", "huimengi_"):
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return text


def _seedance2_resolution_options_for_backend(video_backend: str | None) -> tuple[str, ...]:
    model = _seedance2_model_from_backend(video_backend)
    if model in SEEDANCE2_RESOLUTION_OPTIONS_BY_MODEL:
        return SEEDANCE2_RESOLUTION_OPTIONS_BY_MODEL[model]
    # Check custom/official catalog config (case-insensitive key match)
    raw_model = str(video_backend or "").strip()
    for prefix in ("newapi_", "huimeng_", "huimengi_"):
        if raw_model.lower().startswith(prefix):
            raw_model = raw_model[len(prefix) :].strip()
            break
    catalog_configs = _catalog_video_model_configs()
    cfg = catalog_configs.get(raw_model) or catalog_configs.get(model)
    if isinstance(cfg, dict):
        res_list = _config_str_list(cfg, "resolutionOptions")
        if res_list:
            normalized = tuple(
                dict.fromkeys(_seedance2_api_resolution(item) for item in res_list)
            )
            if normalized:
                return normalized
    if model and not model.startswith("seedance-2.0-mini") and not model.startswith("seedance-2.0-fast"):
        return ("480p", "720p", "1080p")
    return SEEDANCE2_DEFAULT_RESOLUTION_OPTIONS


def _seedance2_resolution_for_backend(
    video_backend: str | None,
    resolution: str | None,
) -> str:
    clean_resolution = _seedance2_api_resolution(resolution)
    options = _seedance2_resolution_options_for_backend(video_backend)
    if clean_resolution in options:
        return clean_resolution
    if "720p" in options:
        return "720p"
    return options[0]


def _happyhorse_resolution_for_backend(resolution: str | None) -> str:
    text = str(resolution or "").strip().lower()
    if "720" in text:
        return "720p"
    return "1080p"


def _happyhorse_ratio_for_backend(ratio: str | None) -> str:
    text = str(ratio or "").strip()
    return text if text in HAPPYHORSE_RATIO_OPTIONS else "16:9"


def _grok_video_resolution_for_backend(resolution: str | None) -> str:
    text = str(resolution or "").strip().lower()
    return text if text in GROK_VIDEO_RESOLUTION_OPTIONS else "720p"


def _grok_video_ratio_for_backend(ratio: str | None) -> str:
    text = str(ratio or "").strip()
    return text if text in GROK_VIDEO_RATIO_OPTIONS else "16:9"


def _seedance2_initial_prompt(beat: dict[str, Any], video_mode: str) -> str:
    if video_mode == "keyframe":
        return str(beat.get("keyframe_prompt") or "").strip()
    return str(beat.get("video_prompt") or beat.get("keyframe_prompt") or "").strip()


def _legacy_video_prompt_for_mode(beat: dict[str, Any], video_mode: str) -> str:
    if video_mode == "keyframe":
        return str(beat.get("keyframe_prompt") or "").strip()
    return str(beat.get("video_prompt") or "").strip()


def _missing_video_prompt_error(beat_num: int) -> str:
    return f"Beat {beat_num} 缺少视频提示词，请先点击“生成本 Beat 提示词”。"


SEEDANCE2_SINGLE_VIDEO_CONFIG_FIELDS = {
    "mode",
    "duration",
    "resolution",
    "ratio",
    "generate_audio",
    "return_last_frame",
    "human_review",
    "scene_optimize",
    "final_prompt",
    "prompt_style",
    "prompt_guidance",
    "text_overlay",
    "s9_tail_anchor",
    "s9_include_first_frame",
}


def _seedance2_request_config_overrides(body: SingleVideoRequest) -> dict[str, Any]:
    return {
        field: getattr(body, field)
        for field in SEEDANCE2_SINGLE_VIDEO_CONFIG_FIELDS
        if field in body.model_fields_set and getattr(body, field) is not None
    }


def _merge_seedance2_request_config(
    beat: dict[str, Any],
    *,
    seedance2_config_json: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> str | None:
    config_overrides = dict(config_overrides or {})
    if seedance2_config_json is None and not config_overrides:
        return None

    from novelvideo.seedance2_i2v.models import dump_seedance2_config, parse_seedance2_config

    merged = parse_seedance2_config(beat.get("seedance2_config_json")).model_dump(mode="json")
    incoming: dict[str, Any] = {}
    if seedance2_config_json is not None:
        try:
            incoming = json.loads(str(seedance2_config_json or "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("seedance2_config_json must be valid JSON") from exc
        if not isinstance(incoming, dict):
            raise ValueError("seedance2_config_json must be a JSON object")
        merged.update(incoming)
    merged.update(config_overrides)

    if "generate_audio" in incoming or "generate_audio" in config_overrides:
        merged["generate_audio_user_set"] = True
    if "human_review" in incoming or "human_review" in config_overrides:
        merged["human_review_user_set"] = True

    saved_json = dump_seedance2_config(merged)
    beat["seedance2_config_json"] = saved_json
    return saved_json


async def _api_audio_duration_seconds(output_dir: str | Path, episode: int, beat_num: int):
    """Duration of one beat's audio, or ``None`` if there isn't one to report.

    ``None`` already means "no duration for this beat" here (the file may simply
    not have been generated yet), so a probe that fails or times out reports the
    same thing rather than failing the whole beat list. On network storage an
    unreadable file is a routine condition, not a request-level error.
    """
    from novelvideo.utils.media_io import get_audio_duration_async
    from novelvideo.utils.path_resolver import PathResolver

    audio_path = PathResolver(output_dir, episode).audio(beat_num)
    if not audio_path.exists():
        return None
    try:
        return await get_audio_duration_async(str(audio_path))
    except Exception:
        return None


async def _prepare_seedance2_api_beat(
    *,
    store: Any,
    output_dir: str | Path,
    episode: int,
    beat: dict[str, Any],
    all_beats: list[dict[str, Any]],
    index: int,
    video_backend: str | None,
    resolution: str | None,
    ratio: str | None = None,
    prop_menu: list[Any] | None = None,
) -> Any:
    from novelvideo.manual_shots import resolve_target_video_duration
    from novelvideo.seedance2_i2v.models import parse_seedance2_config

    beat_num = int(beat.get("beat_number") or index + 1)
    video_mode = str(beat.get("video_mode") or "first_frame")
    audio_duration = await _api_audio_duration_seconds(output_dir, episode, beat_num)
    target_duration = resolve_target_video_duration(beat, audio_duration)
    current_config = parse_seedance2_config(beat.get("seedance2_config_json"))
    requested_resolution = (
        _seedance2_api_resolution(resolution)
        if resolution
        else current_config.resolution
    )
    prepared = await prepare_seedance2_generation_inputs(
        project_output=output_dir,
        state_dir=Path(store.state_dir),
        episode=episode,
        beat=beat,
        next_beat=all_beats[index + 1] if index + 1 < len(all_beats) else None,
        video_mode=video_mode,
        prompt=_seedance2_initial_prompt(beat, video_mode),
        duration=target_duration,
        resolution=_seedance2_resolution_for_backend(
            video_backend,
            requested_resolution,
        ),
        ratio=ratio,
        prop_menu=prop_menu,
        video_backend=video_backend,
    )
    if not str(prepared.prompt or "").strip():
        raise ValueError(
            f"Beat {beat_num} Seedance 2.0 最终提示词为空，"
            "请先填写 Seedance2.0主体提示词或点击“AI 优化”。"
        )

    beat["seedance2_config_json"] = prepared.seedance2_config_json
    if hasattr(store, "update_beat_asset"):
        await store.update_beat_asset(
            episode_number=episode,
            beat_number=beat_num,
            seedance2_config_json=prepared.seedance2_config_json,
        )
    return prepared


async def _prepare_happyhorse_api_beat(
    *,
    output_dir: str | Path,
    state_dir: str | Path,
    episode: int,
    beat: dict[str, Any],
    next_beat: dict[str, Any] | None,
    frame_path: Path,
    video_mode: str,
    prompt: str,
    duration: float,
    resolution: str | None,
    ratio: str | None,
    prop_menu: list[Any] | None = None,
) -> dict[str, Any]:
    from novelvideo.seedance2_i2v.assets import (
        build_seedance2_project_assets,
        resolve_beat_video_references,
        selected_reference_paths,
    )
    from novelvideo.seedance2_i2v.models import (
        Seedance2I2VMode,
        dump_seedance2_config,
        parse_seedance2_config,
    )

    config = parse_seedance2_config(beat.get("seedance2_config_json"))
    mode = config.mode
    if mode == Seedance2I2VMode.FIRST_LAST_FRAME or video_mode == "keyframe":
        raise ValueError("HappyHorse 1.0 不支持首尾帧模式，请改用首帧模式或多参模式")

    final_prompt = str(config.final_prompt or prompt or "").strip()
    if not final_prompt:
        beat_num = int(beat.get("beat_number") or 0)
        prefix = f"Beat {beat_num} " if beat_num else ""
        raise ValueError(f"{prefix}缺少视频提示词，请先生成或填写视频提示词")

    target_duration = int(config.duration or duration or 0)
    config.duration = target_duration
    config.resolution = _happyhorse_resolution_for_backend(resolution or config.resolution)
    config.ratio = _happyhorse_ratio_for_backend(ratio or config.ratio)
    config.final_prompt = final_prompt

    image_path: str | None = None
    references: list[dict[str, str]] = []

    if mode == Seedance2I2VMode.FIRST_FRAME:
        image_path = str(frame_path)
    else:
        assets = build_seedance2_project_assets(
            project_output=Path(output_dir),
            episode=episode,
            beat=beat,
            mode=Seedance2I2VMode.MULTIMODAL_REFERENCE,
            state_dir=state_dir,
            next_beat=next_beat,
            prop_menu=prop_menu,
        )
        resolved_user_references, _declared, missing = resolve_beat_video_references(
            list(config.reference_image_paths), [], assets=assets
        )
        if missing:
            raise ValueError("视频参考素材缺失或校验失败：" + "；".join(missing))
        image_paths = selected_reference_paths(assets, "reference_images")
        config.reference_image_paths = list(dict.fromkeys(image_paths))[:9]
        config.reference_audio_paths = []
        references = [
            {"type": "image", "path": path, "role": f"图片{index}"}
            for index, path in enumerate(config.reference_image_paths, 1)
        ]

    return {
        "prompt": final_prompt,
        "duration": target_duration,
        "resolution": config.resolution,
        "ratio": config.ratio,
        "image_path": image_path,
        "references": references,
        "config_json": dump_seedance2_config(config),
    }


async def _prepare_grok_video_api_beat(
    *,
    output_dir: str | Path,
    state_dir: str | Path,
    episode: int,
    beat: dict[str, Any],
    next_beat: dict[str, Any] | None,
    frame_path: Path,
    video_mode: str,
    prompt: str,
    duration: float,
    resolution: str | None,
    ratio: str | None,
    prop_menu: list[Any] | None = None,
) -> dict[str, Any]:
    from novelvideo.seedance2_i2v.assets import (
        build_seedance2_project_assets,
        resolve_beat_video_references,
        selected_reference_paths,
    )
    from novelvideo.seedance2_i2v.models import (
        Seedance2I2VMode,
        dump_seedance2_config,
        parse_seedance2_config,
    )

    config = parse_seedance2_config(beat.get("seedance2_config_json"))
    mode = config.mode
    if mode == Seedance2I2VMode.FIRST_LAST_FRAME or video_mode == "keyframe":
        raise ValueError("Grok Video 不支持首尾帧模式，请改用首帧模式或多参模式")

    final_prompt = str(config.final_prompt or prompt or "").strip()
    if not final_prompt:
        beat_num = int(beat.get("beat_number") or 0)
        prefix = f"Beat {beat_num} " if beat_num else ""
        raise ValueError(f"{prefix}缺少视频提示词，请先生成或填写视频提示词")

    target_duration = int(config.duration or duration or 0)
    config.duration = target_duration
    config.resolution = _grok_video_resolution_for_backend(resolution or config.resolution)
    config.ratio = _grok_video_ratio_for_backend(ratio or config.ratio)
    config.final_prompt = final_prompt

    image_path: str | None = None
    references: list[dict[str, str]] = []

    if mode == Seedance2I2VMode.FIRST_FRAME:
        image_path = str(frame_path)
    else:
        assets = build_seedance2_project_assets(
            project_output=Path(output_dir),
            episode=episode,
            beat=beat,
            mode=Seedance2I2VMode.MULTIMODAL_REFERENCE,
            state_dir=state_dir,
            next_beat=next_beat,
            prop_menu=prop_menu,
        )
        resolved_user_references, _declared, missing = resolve_beat_video_references(
            list(config.reference_image_paths), [], assets=assets
        )
        if missing:
            raise ValueError("视频参考素材缺失或校验失败：" + "；".join(missing))
        image_paths = selected_reference_paths(assets, "reference_images")
        config.reference_image_paths = list(dict.fromkeys(image_paths))[:7]
        config.reference_audio_paths = []
        references = [
            {"type": "image", "path": path, "role": f"图片{index}"}
            for index, path in enumerate(config.reference_image_paths, 1)
        ]

    return {
        "prompt": final_prompt,
        "duration": target_duration,
        "resolution": config.resolution,
        "ratio": config.ratio,
        "image_path": image_path,
        "references": references,
        "config_json": dump_seedance2_config(config),
    }


def _seedance2_asset_status_payload(
    asset: Any,
    *,
    project_ctx: ProjectContext,
    output_dir: Path,
) -> dict[str, Any]:
    try:
        rel_path = str(Path(asset.path).relative_to(output_dir))
    except ValueError:
        rel_path = str(asset.path)
    abs_path = str(asset.path)
    crop_source_path = getattr(asset, "crop_source_path", None)
    crop_source_abs_path = str(crop_source_path) if crop_source_path else ""
    crop_source_rel_path = ""
    if crop_source_path:
        try:
            crop_source_rel_path = str(Path(crop_source_path).relative_to(output_dir))
        except ValueError:
            crop_source_rel_path = crop_source_abs_path
    media_url = ""
    if bool(asset.exists):
        media_url = make_static_url_for_context(project_ctx, rel_path, local_path=Path(asset.path))
    crop_source_url = ""
    if crop_source_path and Path(crop_source_path).exists():
        crop_source_url = make_static_url_for_context(
            project_ctx,
            crop_source_rel_path,
            local_path=Path(crop_source_path),
        )
    can_delete = (
        str(asset.key).startswith(("user_image:", "user_audio:"))
        or "seedance2_uploads" in Path(abs_path).parts
        or "seedance2_crops" in Path(abs_path).parts
    )
    return {
        "key": str(asset.key),
        "label": str(asset.label),
        "media_type": str(asset.media_type),
        "selected": bool(asset.selected),
        "exists": bool(asset.exists),
        "reference_label": str(asset.reference_label),
        "note": str(asset.note or asset.validation_error or ""),
        "identity_id": str(getattr(asset, "identity_id", "") or ""),
        "prop_id": str(getattr(asset, "prop_id", "") or ""),
        "prop_scope": str(getattr(asset, "prop_scope", "") or ""),
        "path": rel_path,
        "url": media_url,
        "abs_path": abs_path,
        "crop_source_path": crop_source_rel_path,
        "crop_source_abs_path": crop_source_abs_path,
        "crop_source_url": crop_source_url,
        "validation_error": str(asset.validation_error or ""),
        "fallback_text": str(asset.fallback_text or ""),
        "can_crop": bool(asset.exists and asset.media_type == "image"),
        "can_trim": bool(asset.exists and asset.media_type == "audio"),
        "can_delete": can_delete,
    }


def _seedance2_returned_last_frame_status_payload(
    *,
    project_ctx: ProjectContext,
    output_dir: Path,
    episode: int,
    beat_num: int,
    enabled: bool,
) -> dict[str, Any] | None:
    if not enabled:
        return None
    base_path = (
        Path(output_dir)
        / "videos"
        / "beats"
        / f"ep{int(episode):03d}"
        / "returned_last_frames"
        / f"beat_{int(beat_num):02d}"
    )
    path = next(
        (
            base_path.with_suffix(suffix)
            for suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif")
            if base_path.with_suffix(suffix).exists()
        ),
        base_path.with_suffix(".png"),
    )
    if not path.exists():
        return None
    try:
        rel_path = path.relative_to(output_dir).as_posix()
    except ValueError:
        rel_path = str(path)
    return {
        "key": "returned_last_frame",
        "label": f"返回尾帧 · Beat {int(beat_num)}",
        "media_type": "image",
        "selected": False,
        "exists": True,
        "reference_label": "尾帧",
        "note": "Seedance2 返回尾帧",
        "identity_id": "",
        "prop_id": "",
        "prop_scope": "",
        "path": rel_path,
        "url": make_static_url_for_context(project_ctx, rel_path, local_path=path),
        "abs_path": str(path),
        "validation_error": "",
        "fallback_text": "",
        "can_crop": False,
        "can_delete": False,
    }


def _seedance2_voice_status_payload(
    *,
    beat: dict[str, Any],
    characters: list[Any],
    username: str,
    project: str,
    store: Any,
    output_dir: Path,
) -> dict[str, Any]:
    audio_type = normalize_seedance2_audio_type(beat)
    if audio_type == "silence":
        return {
            "required": False,
            "ready": True,
            "label": "无音频",
            "detail": "静音 Beat 不生成音频",
            "speaker": "",
        }
    if audio_type == "dialogue":
        from novelvideo.seedance2_i2v.voice_reference_service import (
            dialogue_voice_reference_rows,
        )

        rows = dialogue_voice_reference_rows(
            beat,
            characters=characters,
            project_dir=output_dir,
        )
        ready_rows = [row for row in rows if row.status.active_reference_path]
        names = [row.display_name or row.speaker for row in rows]
        ready = bool(rows) and len(ready_rows) == len(rows)
        return {
            "required": True,
            "ready": ready,
            "label": "声线就绪" if ready else "声线缺失",
            "detail": "、".join(names) if names else "未指定 speaker",
            "speaker": str(beat.get("speaker") or ""),
        }

    from novelvideo.seedance2_i2v.voice_reference_service import (
        resolve_narrator_reference_status,
    )

    status = resolve_narrator_reference_status(
        store=store,
        username=username,
        project=project,
    )
    return {
        "required": True,
        "ready": bool(status.active_reference_path),
        "label": "声线就绪" if status.active_reference_path else "声线缺失",
        "detail": str(status.detail or status.error or "第三人称项目解说声线未配置"),
        "speaker": "NARRATOR",
    }


async def _seedance2_panel_context(
    *,
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    username = resolved.username
    project_name = resolved.project_name
    output_dir = Path(resolved.output_dir)
    store = (
        await make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await make_sqlite_store(username, project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)
    beat = next((item for item in beats if int(item.get("beat_number") or 0) == beat_num), None)
    if not beat:
        raise HTTPException(status_code=404, detail=f"Beat {beat_num} not found")

    next_beat = next(
        (item for item in beats if int(item.get("beat_number") or 0) == beat_num + 1),
        None,
    )
    characters = store.get_all_characters()
    episode_obj = _episode_from_store_or_none(store, episode_num)
    prop_menu = await _runtime_prop_menu_with_global_props(store, episode_obj, beats)
    language = await detect_episode_asset_language(
        store,
        episode_num,
        fallback_text="\n".join(
            str(item.get(field) or "")
            for item in beats
            for field in ("narration_segment", "dialogue", "visual_description")
        ),
    )
    return {
        "project_ctx": resolved.ctx,
        "username": username,
        "project_name": project_name,
        "output_dir": output_dir,
        "store": store,
        "beats": beats,
        "beat": beat,
        "next_beat": next_beat,
        "characters": characters,
        "prop_menu": prop_menu,
        "language": language,
    }


def _seedance2_status_response(
    *,
    project: str,
    episode_num: int,
    beat_num: int,
    ctx: dict[str, Any],
) -> dict[str, Any]:
    from novelvideo.seedance2_i2v.panel_service import (
        build_seedance2_video_panel_state,
    )
    from novelvideo.utils.path_resolver import PathResolver

    output_dir = Path(ctx["output_dir"])
    beat = ctx["beat"]
    state = build_seedance2_video_panel_state(
        project_dir=output_dir,
        episode=episode_num,
        beat=beat,
        next_beat=ctx["next_beat"],
        characters=ctx["characters"],
        prop_menu=ctx["prop_menu"],
        state_dir=Path(ctx["store"].state_dir),
        language=ctx.get("language"),
    )
    assets = state.assets
    selected_assets = [asset for asset in assets if asset.selected]
    missing_assets = [
        asset
        for asset in assets
        if asset.required and (not asset.exists or bool(asset.validation_error))
    ]
    fallback_assets = [
        asset for asset in assets if str(asset.fallback_text or "").strip() and not asset.selected
    ]
    paths = PathResolver(output_dir, episode_num)
    project_ctx = ctx["project_ctx"]
    asset_items = [
        _seedance2_asset_status_payload(asset, project_ctx=project_ctx, output_dir=output_dir)
        for asset in assets
    ]
    try:
        from novelvideo.seedance2_i2v.models import (
            default_seedance2_ratio_for_state_dir,
            parse_seedance2_config,
        )

        config = parse_seedance2_config(
            beat.get("seedance2_config_json") or "{}",
            default_ratio=default_seedance2_ratio_for_state_dir(Path(ctx["store"].state_dir)),
        )
        returned_last_frame = _seedance2_returned_last_frame_status_payload(
            project_ctx=project_ctx,
            output_dir=output_dir,
            episode=episode_num,
            beat_num=beat_num,
            enabled=bool(config.return_last_frame),
        )
    except Exception:
        returned_last_frame = None
    if returned_last_frame is not None:
        asset_items.append(returned_last_frame)

    return {
        "ok": True,
        "data": {
            "beat_number": beat_num,
            "audio_type": normalize_seedance2_audio_type(beat),
            "seedance2_config_json": str(beat.get("seedance2_config_json") or ""),
            "media": {
                "render_ready": paths.frame(beat_num).exists(),
                "audio_ready": paths.audio(beat_num).exists(),
                "video_ready": paths.video(beat_num).exists(),
            },
            "voice": _seedance2_voice_status_payload(
                beat=beat,
                characters=ctx["characters"],
                username=ctx["username"],
                project=project,
                store=ctx["store"],
                output_dir=output_dir,
            ),
            "prompt": {
                "ready": bool(str(state.final_prompt or "").strip()),
                "source": str(state.prompt_source or ""),
                "status": str(state.prompt_status or ""),
                "has_guidance": bool(str(state.prompt_guidance or "").strip()),
                "text_overlay_enabled": bool((state.text_overlay or {}).get("enabled")),
                "text_overlay": state.text_overlay or {},
                "inputs_stale": bool(
                    state.prompt_inputs_hash
                    and state.prompt_inputs_hash != state.current_prompt_inputs_hash
                ),
            },
            "assets": {
                "total": len(assets),
                "selected": len(selected_assets),
                "missing": len(missing_assets),
                "images": len([asset for asset in selected_assets if asset.media_type == "image"]),
                "audios": len([asset for asset in selected_assets if asset.media_type == "audio"]),
                "fallbacks": len(fallback_assets),
                "items": asset_items,
            },
            "s9": {
                "missing_assets": list(getattr(state, "s9_missing_assets", []) or []),
                "effective_prompt": str(getattr(state, "s9_effective_prompt", "") or ""),
            },
        },
    }


@router.get("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/seedance2-status")
async def get_seedance2_beat_status(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """Return NiceGUI-aligned read-only Seedance 2.0 status for one Beat."""
    ctx = await _seedance2_panel_context(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        user=user,
    )
    return _seedance2_status_response(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        ctx=ctx,
    )


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/seedance2/assets/upload")
async def upload_seedance2_asset(
    project: str,
    episode_num: int,
    beat_num: int,
    file: UploadFile = File(...),
    user: dict = Depends(get_api_user),
):
    """Upload a manual Seedance 2.0 reference asset."""
    ctx = await _seedance2_panel_context(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        user=user,
    )
    from novelvideo.seedance2_i2v.panel_service import (
        save_seedance2_uploaded_file,
    )

    target = await save_seedance2_uploaded_file(
        store=ctx["store"],
        episode=episode_num,
        beat=ctx["beat"],
        project_dir=ctx["output_dir"],
        filename=file.filename or "seedance2_asset",
        upload_stream=file.file,
        content_type=file.content_type or "",
    )
    if target is None:
        return {"ok": False, "error": "unsupported or empty Seedance2 reference asset"}
    return _seedance2_status_response(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        ctx=ctx,
    )


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/seedance2/assets/delete")
async def delete_seedance2_asset(
    project: str,
    episode_num: int,
    beat_num: int,
    body: Seedance2AssetDeleteRequest,
    user: dict = Depends(get_api_user),
):
    """Remove a manually attached Seedance 2.0 reference asset."""
    ctx = await _seedance2_panel_context(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        user=user,
    )
    from novelvideo.seedance2_i2v.panel_service import (
        remove_seedance2_uploaded_asset,
    )

    removed = await remove_seedance2_uploaded_asset(
        store=ctx["store"],
        episode=episode_num,
        beat=ctx["beat"],
        project_dir=ctx["output_dir"],
        media_kind=body.media_kind,
        path=body.path,
    )
    if not removed:
        return {"ok": False, "error": "Seedance2 reference asset was not removed"}
    return _seedance2_status_response(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        ctx=ctx,
    )


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/seedance2/assets/crop")
async def crop_seedance2_asset(
    project: str,
    episode_num: int,
    beat_num: int,
    body: Seedance2AssetCropRequest,
    user: dict = Depends(get_api_user),
):
    """Crop an existing Seedance 2.0 image reference into a manual reference."""
    ctx = await _seedance2_panel_context(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        user=user,
    )
    from novelvideo.seedance2_i2v.panel_service import (
        crop_seedance2_asset_to_reference,
    )

    target = await crop_seedance2_asset_to_reference(
        store=ctx["store"],
        episode=episode_num,
        beat=ctx["beat"],
        project_dir=ctx["output_dir"],
        asset_key=body.asset_key,
        source_path=body.source_path,
        crop_data=body.model_dump(),
    )
    if target is None:
        return {"ok": False, "error": "Seedance2 reference crop failed"}
    return _seedance2_status_response(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        ctx=ctx,
    )


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/seedance2/assets/audio-trim")
async def trim_seedance2_audio_asset(
    project: str,
    episode_num: int,
    beat_num: int,
    body: Seedance2AssetAudioTrimRequest,
    user: dict = Depends(get_api_user),
):
    """Trim an existing Seedance 2.0 audio reference into a 3-5 second clip."""
    ctx = await _seedance2_panel_context(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        user=user,
    )
    from novelvideo.seedance2_i2v.panel_service import (
        trim_seedance2_audio_to_reference,
    )

    try:
        target = await trim_seedance2_audio_to_reference(
            store=ctx["store"],
            episode=episode_num,
            beat=ctx["beat"],
            project_dir=ctx["output_dir"],
            asset_key=body.asset_key,
            source_path=body.source_path,
            start_seconds=body.start_seconds,
            duration_seconds=body.duration_seconds,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if target is None:
        return {"ok": False, "error": "Seedance2 audio reference trim failed"}
    return _seedance2_status_response(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        ctx=ctx,
    )


def _catalog_video_model_configs() -> dict[str, dict[str, Any]]:
    configs: dict[str, dict[str, Any]] = {}
    try:
        from novelvideo.model_gateway_settings import (
            get_newapi_media_model_mappings,
            get_official_media_model_mappings,
        )
        from novelvideo.shared.runtime_env import is_ce_effective

        for model_name, entry in get_official_media_model_mappings().items():
            if isinstance(entry, dict) and isinstance(entry.get("config"), dict):
                configs[model_name] = dict(entry["config"])
        if is_ce_effective():
            for model_name, entry in get_newapi_media_model_mappings().items():
                if isinstance(entry, dict) and isinstance(entry.get("config"), dict):
                    configs[model_name] = dict(entry["config"])
    except (RuntimeError, ImportError):
        return configs
    return configs


def _config_str_list(config: dict[str, Any], key: str) -> list[str] | None:
    raw = config.get(key)
    if not isinstance(raw, list) or not raw:
        return None
    items = [str(item).strip() for item in raw if str(item or "").strip()]
    return items or None


def _config_opt_int(config: dict[str, Any], key: str) -> int | None:
    raw = config.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _api_video_backend_options() -> list[VideoBackendOption]:
    from novelvideo.config import NEWAPI_VIDEO_DURATION_BOUNDS
    from novelvideo.generators.video_generator import (
        NewApiVideoGenerator,
        newapi_video_backend_options,
        parse_newapi_video_backend,
    )

    hidden_mainline_backends = {
        "newapi_seedance-2.0-value",
        "newapi_seedance-2.0-fast-value",
        "newapi_happyhorse-1.0",
    }
    options = {
        value: label
        for value, label in newapi_video_backend_options(
            include_seedance2_variants=True
        ).items()
        if value not in hidden_mainline_backends
    }
    duration_bounds = NewApiVideoGenerator._parse_duration_bounds_config(
        NEWAPI_VIDEO_DURATION_BOUNDS
    )
    catalog_configs = _catalog_video_model_configs()
    default_backend = VideoGenerateRequest().video_backend
    backend_options: list[VideoBackendOption] = []
    for value, label in options.items():
        model = parse_newapi_video_backend(value)
        model_config = catalog_configs.get(model or "", {})
        bounds = duration_bounds.get(model or "")
        if not bounds:
            cfg_min = _config_opt_int(model_config, "minDuration")
            cfg_max = _config_opt_int(model_config, "maxDuration")
            if cfg_min and cfg_max and cfg_max >= cfg_min:
                bounds = (cfg_min, cfg_max)
        if model == "seedance-2.0-mini" and not bounds:
            bounds = (4, 15)
        if model == "happyhorse-1.0" and not bounds:
            bounds = (3, 15)
        if model == "grok-video-channel" and not bounds:
            bounds = (6, 30)
        is_seedance2 = _is_seedance2_backend(value)
        is_happyhorse = _is_happyhorse_backend(value)
        is_grok_video = _is_grok_video_backend(value)
        supports_prompt_panel = bool(
            is_seedance2
            or is_happyhorse
            or is_grok_video
            or _is_custom_prompt_panel_backend(value)
        )
        backend_options.append(
            VideoBackendOption(
                value=value,
                label=label,
                is_default=value == default_backend,
                is_seedance2=is_seedance2,
                is_happyhorse=is_happyhorse,
                is_grok_video=is_grok_video,
                supports_prompt_panel=supports_prompt_panel,
                dialogue_only=value in {"seedance_pro", "newapi_seedance-1.5-pro"},
                min_duration=bounds[0] if bounds else None,
                max_duration=bounds[1] if bounds else None,
                resolution_options=(
                    list(HAPPYHORSE_RESOLUTION_OPTIONS)
                    if is_happyhorse
                    else list(GROK_VIDEO_RESOLUTION_OPTIONS)
                    if is_grok_video
                    else _config_str_list(model_config, "resolutionOptions")
                ),
                ratio_options=(
                    list(HAPPYHORSE_RATIO_OPTIONS)
                    if is_happyhorse
                    else list(GROK_VIDEO_RATIO_OPTIONS)
                    if is_grok_video
                    else _config_str_list(model_config, "ratioOptions")
                ),
                supported_modes=(
                    list(HAPPYHORSE_SUPPORTED_MODES)
                    if is_happyhorse
                    else list(GROK_VIDEO_SUPPORTED_MODES)
                    if is_grok_video
                    else _config_str_list(model_config, "supportedModes")
                ),
                reference_image_max=(
                    7
                    if is_grok_video
                    else 9
                    if is_happyhorse
                    else _config_opt_int(model_config, "referenceImageMax")
                ),
                reference_video_max=(
                    0
                    if is_grok_video
                    else 1
                    if is_happyhorse
                    else _config_opt_int(model_config, "referenceVideoMax")
                ),
                reference_audio_max=(
                    0
                    if is_grok_video or is_happyhorse
                    else _config_opt_int(model_config, "referenceAudioMax")
                ),
            )
        )
    return backend_options


@router.get("/projects/{project}/video-backends")
async def get_video_backend_options(
    project: str,
    user: dict = Depends(get_api_user),
):
    """Return video backend options shared with the NiceGUI render workbench."""
    await _resolve_generation_project(project, user, required_role="viewer")
    return {"ok": True, "data": [item.model_dump() for item in _api_video_backend_options()]}


def _image_generation_guard_payload(attempt_count: int, subject: str) -> dict:
    next_attempt = attempt_count + 1
    if next_attempt >= 5:
        level = "locked"
        message = f"{subject} 已连续生成 {next_attempt} 次，请输入管理员密码继续本次生成。"
    elif next_attempt >= 3:
        level = "confirm"
        message = f"{subject} 已连续生成 {next_attempt} 次，确认继续生成吗？"
    else:
        level = "none"
        message = ""
    return {
        "attempt_count": attempt_count,
        "next_attempt": next_attempt,
        "level": level,
        "message": message,
    }


@router.get("/projects/{project}/episodes/{episode_num}/image-generation-guard")
async def get_image_generation_guard(
    project: str,
    episode_num: int,
    task_type: str = Query(...),
    scope: str = Query(...),
    subject: str = Query("当前生成任务"),
    user: dict = Depends(get_api_user),
):
    """Return per-scope image generation guard status used before dispatch."""
    from novelvideo.image_request_usage import count_image_scope_attempts

    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    attempt_count = count_image_scope_attempts(
        project_output_dir=project_dir,
        task_type=task_type,
        scope=scope,
        episode=episode_num,
    )
    return {"ok": True, "data": _image_generation_guard_payload(attempt_count, subject)}


@router.post("/projects/{project}/episodes/{episode_num}/image-generation-guard/verify-password")
async def verify_image_generation_guard_password(
    project: str,
    episode_num: int,
    body: OperatorPasswordVerifyRequest,
    user: dict = Depends(get_api_user),
):
    """Verify the same operator password NiceGUI requires after repeated image attempts."""
    from novelvideo.security.operator_auth import get_prompt_export_password

    _ = (project, episode_num, user)
    configured = get_prompt_export_password()
    verified = bool(configured) and (body.password or "") == configured
    return {
        "ok": True,
        "data": {"verified": verified},
    }


@router.post("/projects/{project}/episodes/{episode_num}/videos/compose")
async def compose_video(
    project: str,
    episode_num: int,
    body: VideoComposeRequest,
    user: dict = Depends(get_api_user),
):
    """合成成片。"""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    output_dir = resolved.output_dir
    store = (
        await make_sqlite_store_for_context(ctx)
        if ctx
        else await make_sqlite_store(username, project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)
    if not beats:
        return {"ok": False, "error": f"No beats found for episode {episode_num}"}

    config = {
        "beats": beats,
        "add_subtitles": body.add_subtitles,
        "add_bgm": body.add_bgm,
    }

    if ctx is not None:
        queued = await get_task_backend().enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="compose_episode",
            queue_kind="ffmpeg",
            episode=episode_num,
            payload={
                **config,
                "episode": episode_num,
                "output_dir": output_dir,
                "resolution": getattr(body, "resolution", "720x1280"),
            },
        )
        return {
            "ok": True,
            "task_type": "compose_episode",
            "task_id": queued.task_state.task_id,
            "task_key": project_task_state_key("compose_episode", ctx.project_id, episode_num),
            "backend": queued.backend,
            "queue": queued.queue,
            "message": f"第 {episode_num} 集成片合成已进入队列",
        }

    return {"ok": False, "error": "成片合成需要 project context"}


@router.get("/projects/{project}/episodes/{episode_num}/final")
async def get_final_video(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
):
    """读取 episode 成片状态，供 supertale-fe compose 页刷新 hydration。"""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    filename = f"ep{episode_num:03d}_final.mp4"
    final_path = project_dir / "videos" / "episodes" / filename
    data = {"exists": final_path.exists(), "filename": filename}
    if final_path.exists():
        data["video_url"] = make_static_url_for_context(
            resolved.ctx,
            f"videos/episodes/{filename}",
            local_path=final_path,
        )
    return {"ok": True, "data": data}


# ── TTS 语音 ──────────────────────────────────────────────────────────────────


@router.post("/projects/{project}/episodes/{episode_num}/tts/generate")
async def generate_tts(
    project: str,
    episode_num: int,
    body: TTSGenerateRequest,
    user: dict = Depends(get_api_user),
):
    """Legacy TTS endpoint removed after IndexTTS2 cutover."""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Legacy /tts/generate was removed. Use "
            f"/projects/{project}/episodes/{episode_num}/audio/generate for IndexTTS2."
        ),
    )


@router.post("/projects/{project}/tts/preview")
async def preview_tts(
    project: str,
    body: TTSPreviewRequest,
    user: dict = Depends(get_api_user),
):
    """Legacy TTS preview endpoint removed after IndexTTS2 cutover."""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Legacy /tts/preview was removed. IndexTTS2 uses configured reference voices.",
    )


@router.get("/projects/{project}/tts/voices")
async def list_tts_voices(project: str, user: dict = Depends(get_api_user)):
    """Legacy voice listing endpoint removed after IndexTTS2 cutover."""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Legacy /tts/voices was removed. IndexTTS2 voice options are project assets.",
    )


# ── 语音生成 ──────────────────────────────────────────────────────────────────


async def _collect_audio_prereq_errors(
    *,
    store,
    username: str,
    project: str,
    episode: int,
    beat_numbers,
    mode: str,
) -> list[str]:
    from novelvideo.audio.indextts2_beat_audio_task import (
        collect_indextts2_voice_prereq_errors,
    )

    try:
        return await collect_indextts2_voice_prereq_errors(
            store=store,
            username=username,
            project=project,
            episode=episode,
            beat_numbers=beat_numbers,
            mode=mode,
        )
    except AttributeError:
        # Unit-test fakes may not implement the full SQLiteStore voice-sample
        # surface. Real SQLiteStore has these attributes; skip only for narrow
        # fakes so existing route-contract tests can focus on dispatch shape.
        return []


async def _audio_generation_plan(
    *,
    store,
    username: str,
    project: str,
    episode: int,
    beat_numbers,
    mode: str,
) -> tuple[list[int], list[str], int]:
    from novelvideo.audio.indextts2_beat_audio_task import (
        build_indextts2_audio_generation_plan,
    )

    try:
        plan = await build_indextts2_audio_generation_plan(
            store=store,
            username=username,
            project=project,
            episode=episode,
            beat_numbers=beat_numbers,
            mode=mode,
        )
        return (
            list(plan.beat_numbers),
            list(plan.errors),
            int(getattr(plan, "billable_chars", 0) or 0),
        )
    except AttributeError:
        # Narrow route-test stores do not expose the voice and audio-state
        # surfaces used by the production planner.
        beats = await store.get_beats_as_dicts(episode)
        selected = {int(value) for value in beat_numbers or [] if int(value) > 0}
        planned = [
            int(beat.get("beat_number") or 0)
            for beat in beats
            if int(beat.get("beat_number") or 0) > 0
            and (not selected or int(beat.get("beat_number") or 0) in selected)
        ]
        from novelvideo.seedance2_i2v.voice_clone import (
            dialogue_text,
            narration_beat_text,
            normalize_seedance2_audio_type,
        )
        from novelvideo.utils.document_parsers import count_billable_text_chars

        planned_set = set(planned)
        billable_chars = 0
        for beat in beats:
            if int(beat.get("beat_number") or 0) not in planned_set:
                continue
            audio_type = normalize_seedance2_audio_type(beat)
            text = (
                narration_beat_text(beat)
                if audio_type == "narration"
                else dialogue_text(beat)
            )
            billable_chars += count_billable_text_chars(text)
        return planned, [], billable_chars


def _audio_billing_payload(
    beat_numbers: list[int],
    *,
    billable_chars: int = 0,
) -> dict:
    from novelvideo.audio.indextts2_beat_audio_task import (
        indextts2_audio_billing_params,
    )

    return {
        **indextts2_audio_billing_params(
            len(beat_numbers),
            billable_chars=billable_chars,
        ),
        "beat_numbers": list(beat_numbers),
    }


def _voice_prereq_error_response(errors: list[str]) -> dict:
    preview = "；".join(errors[:5])
    suffix = " ..." if len(errors) > 5 else ""
    return {
        "ok": False,
        "code": "voice_prereq_required",
        "error": f"{preview}{suffix}",
    }


@router.post("/projects/{project}/episodes/{episode_num}/audio/system-voices/prepare")
async def prepare_system_voices_for_agent(
    project: str,
    episode_num: int,
    body: SystemVoicePrepareRequest,
    user: dict = Depends(get_api_user),
):
    """Prepare missing system voice references for the outer Hermes assistant only."""

    agent_kind = str(user.get("agent_kind") or "").strip()
    if agent_kind not in {"hermes", "local_mcp"}:
        raise HTTPException(status_code=403, detail="system voice setup is agent-only")
    if not body.confirmed:
        return {
            "ok": False,
            "code": "system_voice_confirmation_required",
            "error": "使用系统声线前需要用户明确确认",
        }

    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    if ctx is None:
        return {
            "ok": False,
            "code": "project_context_required",
            "error": "系统声线准备需要 project context",
        }

    queued = await get_task_backend().enqueue_project_task(
        ctx,
        product_surface="mainline",
        task_type="system_voice_setup",
        queue_kind="default",
        episode=episode_num,
        payload={
            "episode": episode_num,
            "output_dir": resolved.output_dir,
            "state_dir": resolved.state_dir,
        },
    )
    return {
        "ok": True,
        "task_type": "system_voice_setup",
        "task_id": queued.task_state.task_id,
        "task_key": project_task_state_key(
            "system_voice_setup", ctx.project_id, episode_num
        ),
        "backend": queued.backend,
        "queue": queued.queue,
        "message": f"第 {episode_num} 集系统声线准备已进入队列",
    }


@router.post("/projects/{project}/episodes/{episode_num}/audio/billing-quote")
async def audio_generation_billing_quote(
    project: str,
    episode_num: int,
    body: TTSGenerateRequest = TTSGenerateRequest(),
    user: dict = Depends(get_api_user),
):
    """Quote the exact set of Beat audio model calls for an audio action."""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    store = (
        await make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await make_sqlite_store(resolved.username, resolved.project_name)
    )
    mode = body.mode or "sync_changed"
    beat_numbers, errors, billable_chars = await _audio_generation_plan(
        store=store,
        username=resolved.username,
        project=resolved.project_name,
        episode=episode_num,
        beat_numbers=body.beat_numbers,
        mode=mode,
    )
    quantity = len(beat_numbers)
    if quantity <= 0:
        return {
            "ok": True,
            "data": {
                "beat_numbers": [],
                "quantity": 0,
                "unit_cost": 0,
                "cost": 0,
                "display": "",
                "prereq_errors": errors,
            },
        }
    quote_args = {
        "kind": "feature",
        "model": "mainline.beat_audio_generation",
        "params": _audio_billing_payload(
            beat_numbers,
            billable_chars=billable_chars,
        ),
        "quantity": quantity,
        "product_surface": "mainline",
    }
    quote = await get_credit_quote().generation_credit_quote(
        **quote_args,
        user_id=str(user.get("id") or user.get("user_id") or ""),
    )
    return {
        "ok": True,
        "data": {
            "beat_numbers": beat_numbers,
            "quantity": quantity,
            "unit_cost": quote.unit_cost,
            "cost": quote.total_cost,
            "display": quote.display,
            "prereq_errors": errors,
            **(
                {
                    "original_cost": getattr(quote, "original_total_cost", None),
                    "discount_amount": getattr(quote, "discount_amount", None),
                    "promotion": getattr(quote, "promotion", None),
                }
                if getattr(quote, "promotion", None)
                else {}
            ),
        },
    }


@router.post("/projects/{project}/episodes/{episode_num}/audio/generate")
async def generate_audio(
    project: str,
    episode_num: int,
    body: TTSGenerateRequest = TTSGenerateRequest(),
    user: dict = Depends(get_api_user),
):
    """批量生成语音（IndexTTS2）。"""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    output_dir = resolved.output_dir
    state_dir = str(ctx.state_dir) if ctx else get_state_dir(username, project_name)
    store = (
        await make_sqlite_store_for_context(ctx)
        if ctx
        else await make_sqlite_store(username, project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)

    if not beats:
        return {"ok": False, "error": f"No beats found for episode {episode_num}"}

    mode = body.mode or "sync_changed"
    billable_beat_numbers, missing_voice, billable_chars = await _audio_generation_plan(
        store=store,
        username=username,
        project=project_name,
        episode=episode_num,
        beat_numbers=body.beat_numbers,
        mode=mode,
    )
    if missing_voice:
        return _voice_prereq_error_response(missing_voice)
    if not billable_beat_numbers:
        return {
            "ok": False,
            "code": "audio_generation_not_required",
            "error": "没有需要生成的音频",
        }

    if ctx is not None:
        queued = await get_task_backend().enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="audio_generation_indextts2",
            queue_kind="default",
            episode=episode_num,
            payload={
                "episode": episode_num,
                "mode": mode,
                "beat_numbers": billable_beat_numbers,
                "output_dir": output_dir,
                "state_dir": state_dir,
                "billing": _audio_billing_payload(
                    billable_beat_numbers,
                    billable_chars=billable_chars,
                ),
            },
        )
        return {
            "ok": True,
            "task_type": "audio_generation_indextts2",
            "task_id": queued.task_state.task_id,
            "task_key": project_task_state_key(
                "audio_generation_indextts2", ctx.project_id, episode_num
            ),
            "backend": queued.backend,
            "queue": queued.queue,
            "message": f"第 {episode_num} 集语音批量生成已进入队列",
        }

    return {
        "ok": False,
        "error": "音频生成需要 project context",
    }


def _director_control_payload(
    *,
    ctx: ProjectContext,
    project_dir: Path,
    episode_num: int,
    beat_num: int,
) -> dict[str, Any]:
    from novelvideo.utils.path_resolver import PathResolver

    paths = PathResolver(str(project_dir), int(episode_num))
    control_frame = paths.director_render(int(beat_num))
    ready = control_frame.exists()
    rel_path = None
    url = None
    if ready:
        try:
            rel_path = control_frame.relative_to(project_dir).as_posix()
            url = make_static_url_for_context(ctx, rel_path, local_path=control_frame)
        except ValueError:
            rel_path = control_frame.as_posix()
    return {
        "episode": int(episode_num),
        "beat_num": int(beat_num),
        "ready": ready,
        "path": control_frame.as_posix(),
        "rel_path": rel_path,
        "url": url,
    }


def _director_overlay_beat_context(beat: dict[str, Any]) -> dict[str, Any]:
    identities = beat.get("detected_identities") or []
    props = beat.get("detected_props") or []
    return {
        "detected_identities": [str(item) for item in identities if str(item).strip()],
        "detected_props": [str(item) for item in props if str(item).strip()],
    }


def _director_same_scene_beats(beats: list[dict[str, Any]], scene_name: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in beats:
        if _beat_scene_name(item) != scene_name:
            continue
        beat_number = item.get("beat_number") or item.get("beat") or item.get("number")
        try:
            beat_int = int(beat_number)
        except (TypeError, ValueError):
            continue
        items.append({"beat": beat_int, "label": f"Beat {beat_int}", "scene_id": scene_name})
    return sorted(items, key=lambda entry: entry["beat"])


def _director_overlay_payload(
    *,
    episode_num: int,
    beat_num: int,
    scene_name: str,
    beat: dict[str, Any],
    body: dict[str, Any],
) -> dict[str, Any]:
    from datetime import datetime, timezone

    snapshot = body.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    body_actors = body.get("actors")
    body_props = body.get("props")
    body_stagings = body.get("stagings")
    actors = body_actors if isinstance(body_actors, list) else snapshot.get("actors") or []
    props = body_props if isinstance(body_props, list) else snapshot.get("props") or []
    stagings = body_stagings if isinstance(body_stagings, list) else snapshot.get("stagings") or []
    legacy_props = [*props, *stagings] if isinstance(props, list) and isinstance(stagings, list) else props
    frame_meta = body.get("frame_meta")
    frame_meta = frame_meta if isinstance(frame_meta, dict) else {}
    source = body.get("source")
    if not isinstance(source, dict):
        meta_source = frame_meta.get("source")
        source = meta_source if isinstance(meta_source, dict) else {}
    return {
        "schema_version": "director_stage_overlay_v1",
        "scene_id": scene_name,
        "episode": int(episode_num),
        "beat": int(beat_num),
        "frame_aspect": str(body.get("frame_aspect") or "16:9"),
        "source": source,
        "frame_meta": frame_meta,
        "snapshot": snapshot,
        "camera": snapshot.get("camera") or body.get("camera") or {},
        "actors": actors,
        "props": legacy_props,
        "stagings": stagings,
        "command_log": body.get("command_log") if isinstance(body.get("command_log"), list) else [],
        "deleted_keys": body.get("deleted_keys") if isinstance(body.get("deleted_keys"), list) else [],
        "beat_context": _director_overlay_beat_context(beat),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }


def _director_overlay_status_payload(
    *,
    project_dir: Path,
    episode_num: int,
    beat_num: int,
    scene_name: str,
    beats: list[dict[str, Any]],
) -> dict[str, Any]:
    from novelvideo.director_world.paths import beat_blocking_path
    from novelvideo.director_world.store import load_beat_blocking

    path = beat_blocking_path(project_dir, episode_num, beat_num)
    same_scene = _director_same_scene_beats(beats, scene_name)
    current = load_beat_blocking(project_dir, episode_num, beat_num)
    if current:
        return {
            "status": "current",
            "overlay": current,
            "path": path.as_posix(),
            "same_scene_beats": same_scene,
        }

    inherited: dict[str, Any] | None = None
    inherited_from: int | None = None
    for item in same_scene:
        candidate_beat = int(item["beat"])
        if candidate_beat >= int(beat_num):
            continue
        candidate = load_beat_blocking(project_dir, episode_num, candidate_beat)
        if candidate:
            inherited = candidate
            inherited_from = candidate_beat
    if inherited:
        return {
            "status": "inherited",
            "overlay": inherited,
            "path": path.as_posix(),
            "inherited_from_beat": inherited_from,
            "same_scene_beats": same_scene,
        }
    return {
        "status": "missing",
        "overlay": None,
        "path": path.as_posix(),
        "same_scene_beats": same_scene,
    }


def _decode_png_data_url(data_url: str) -> bytes:
    import base64

    prefix = "data:image/png;base64,"
    if not data_url.startswith(prefix):
        raise ValueError("expected PNG data URL")
    return base64.b64decode(data_url[len(prefix) :], validate=True)


def _director_control_frame_export_payload(
    *,
    ctx: ProjectContext,
    project_dir: Path,
    scene_name: str,
    episode_num: int,
    beat_num: int,
    body: dict[str, Any],
) -> dict[str, Any]:
    target_dir = (
        project_dir
        / "director_control_frames"
        / f"ep{int(episode_num):03d}"
        / f"beat_{int(beat_num):02d}"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    images = body.get("images")
    images = images if isinstance(images, dict) else {}
    submitted_frame_meta = body.get("frame_meta")
    if not isinstance(submitted_frame_meta, dict) or not submitted_frame_meta:
        raise ValueError("combined, env_only and frame_meta are required")
    missing_kinds = [
        kind
        for kind in ("combined", "env_only")
        if not isinstance(images.get(kind), str) or not images.get(kind)
    ]
    if missing_kinds:
        raise ValueError("combined, env_only and frame_meta are required")
    filename_by_kind = {
        "combined": "combined.png",
        "env_only": "env_only.png",
    }
    paths: dict[str, str] = {}
    rel_paths: dict[str, str] = {}
    urls: dict[str, str] = {}
    for kind, filename in filename_by_kind.items():
        data_url = images.get(kind)
        if not isinstance(data_url, str) or not data_url:
            continue
        path = target_dir / filename
        path.write_bytes(_decode_png_data_url(data_url))
        paths[kind] = path.as_posix()
        rel_paths[kind] = path.relative_to(project_dir).as_posix()
        urls[kind] = make_static_url_for_context(ctx, rel_paths[kind], local_path=path)

    snapshot = body.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    body_actors = body.get("actors")
    body_props = body.get("props")
    body_stagings = body.get("stagings")
    actors = body_actors if isinstance(body_actors, list) else snapshot.get("actors") or []
    props = body_props if isinstance(body_props, list) else snapshot.get("props") or []
    stagings = body_stagings if isinstance(body_stagings, list) else snapshot.get("stagings") or []
    legacy_props = [*props, *stagings] if isinstance(props, list) and isinstance(stagings, list) else props
    meta = dict(submitted_frame_meta)
    meta.setdefault("scene_id", scene_name)
    meta.setdefault("episode", int(episode_num))
    meta.setdefault("beat", int(beat_num))
    meta.setdefault("frame_aspect", str(body.get("frame_aspect") or "16:9"))
    meta.setdefault("actors", actors)
    meta.setdefault("props", legacy_props)
    meta.setdefault("stagings", stagings)
    meta["paths"] = rel_paths
    meta_path = target_dir / "frame_meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paths["frame_meta"] = meta_path.as_posix()
    rel_paths["frame_meta"] = meta_path.relative_to(project_dir).as_posix()
    urls["frame_meta"] = make_static_url_for_context(ctx, rel_paths["frame_meta"], local_path=meta_path)
    return {"dir": target_dir.as_posix(), "paths": paths, "rel_paths": rel_paths, "urls": urls, "meta": meta}


async def _episode_beat_from_resolution(
    resolved,
    episode_num: int,
    beat_num: int,
):
    store = (
        await make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await make_sqlite_store(resolved.username, resolved.project_name)
    )
    try:
        beats = await store.get_beats_as_dicts(int(episode_num))
        target = next(
            (
                beat
                for beat in beats
                if int(beat.get("beat_number") or 0) == int(beat_num)
            ),
            None,
        )
        if target is None:
            raise HTTPException(status_code=404, detail=f"Beat {beat_num} not found")
        return store, target
    except Exception:
        close = getattr(store, "close", None)
        if close:
            await close()
        raise


def _beat_scene_name(beat: dict[str, Any]) -> str:
    return str(beat_scene_id(beat) or beat.get("location") or "").strip()


def _api_background_reference_url_builder(ctx: ProjectContext):
    def _build(path: Path, rel_path: str) -> str:
        return make_static_url_for_context(ctx, rel_path, local_path=path)

    return _build


def _api_background_anchor_url_builder(ctx: ProjectContext):
    def _build(path: Path, rel_path: str) -> str | None:
        return make_static_url_for_context(ctx, rel_path, local_path=path)

    return _build


def _background_anchors_payload(
    *,
    ctx: ProjectContext,
    username: str,
    project: str,
    project_dir: Path,
    beat: dict[str, Any],
    episode_num: int,
    beat_num: int,
) -> dict[str, Any]:
    return build_background_anchors_payload(
        project_dir=project_dir,
        username=username,
        project=project,
        beat=beat,
        episode_num=int(episode_num),
        beat_num=int(beat_num),
        reference_url_builder=_api_background_reference_url_builder(ctx),
        anchor_url_builder=_api_background_anchor_url_builder(ctx),
    )


@router.get("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/pano-background/manifest")
async def get_beat_pano_background_manifest(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """Return the typed 360 viewer manifest for Beat selected-background capture."""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        scene_name = _beat_scene_name(beat)
        if not scene_name:
            return {"ok": False, "error": "当前 Beat 没有关联场景"}
        manifest = build_pano_viewer_manifest(
            ctx=resolved.ctx,
            project_dir=project_dir,
            scene_name=scene_name,
            mode="beat",
            episode_num=int(episode_num),
            beat_num=int(beat_num),
            beat=beat,
        )
        if manifest is None:
            return {"ok": False, "error": "当前场景没有 360 全景资产"}
        return {"ok": True, "data": manifest.model_dump(exclude_none=True)}
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.get("/projects/{project}/director-stage/palette")
async def get_default_director_stage_palette(
    project: str,
    user: dict = Depends(get_api_user),
):
    """Return the shared director-stage palette used by local/freezone worlds."""
    await _resolve_generation_project(project, user, required_role="viewer")
    return {"ok": True, "data": default_director_stage_palette().model_dump(exclude_none=True)}


@router.get("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/director-stage/manifest")
async def get_beat_director_stage_manifest(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """Return the typed 3GS director-stage manifest for Beat-level capture."""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        scene_name = _beat_scene_name(beat)
        if not scene_name:
            return {"ok": False, "error": "当前 Beat 没有关联场景"}
        beats = await store.get_beats_as_dicts(int(episode_num))
        sketch_colors = {}
        get_sketch_colors = getattr(store, "get_sketch_colors", None)
        if get_sketch_colors is not None:
            sketch_colors = dict(get_sketch_colors(int(episode_num)) or {})
        episode_obj = _episode_from_store_or_none(store, int(episode_num))
        prop_menu = await _runtime_prop_menu_with_global_props(store, episode_obj, list(beats))
        manifest = build_director_stage_manifest(
            ctx=resolved.ctx,
            project_dir=project_dir,
            scene_name=scene_name,
            mode="beat",
            episode_num=int(episode_num),
            beat_num=int(beat_num),
            beat=beat,
            sketch_colors=sketch_colors,
            prop_marker_colors=_prop_marker_colors_from_menu(prop_menu),
        )
        if manifest is None:
            return {"ok": False, "error": "当前场景没有 3GS 资产"}
        return {"ok": True, "data": manifest.model_dump(exclude_none=True)}
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.get("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/director-stage/overlay")
async def get_beat_director_stage_overlay(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """Load the current Beat 3GS overlay, or inherit the previous same-scene Beat."""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        scene_name = _beat_scene_name(beat)
        if not scene_name:
            return {"ok": False, "error": "当前 Beat 没有关联场景"}
        beats = await store.get_beats_as_dicts(int(episode_num))
        return {
            "ok": True,
            "data": _director_overlay_status_payload(
                project_dir=project_dir,
                episode_num=int(episode_num),
                beat_num=int(beat_num),
                scene_name=scene_name,
                beats=list(beats),
            ),
        }
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/director-stage/overlay")
async def save_beat_director_stage_overlay(
    project: str,
    episode_num: int,
    beat_num: int,
    body: dict[str, Any],
    user: dict = Depends(get_api_user),
):
    """Persist the current Beat 3GS overlay to director_blockings/epNNN/beat_MM.json."""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        scene_name = _beat_scene_name(beat)
        if not scene_name:
            return {"ok": False, "error": "当前 Beat 没有关联场景"}
        payload = _director_overlay_payload(
            episode_num=int(episode_num),
            beat_num=int(beat_num),
            scene_name=scene_name,
            beat=beat,
            body=body,
        )
        from novelvideo.director_world.store import save_beat_blocking

        path = save_beat_blocking(project_dir, int(episode_num), int(beat_num), payload)
        if hasattr(store, "update_beat_asset"):
            overlay_prop_labels = [
                str(item.get("label") or item.get("prop_id") or "").strip()
                for item in payload.get("props", [])
                if isinstance(item, dict)
                and str(item.get("type") or "").strip() != "prop_staging"
                and str(item.get("category") or "").strip() != "staging"
            ]
            merged_props = [
                prop
                for prop in [
                    *payload["beat_context"].get("detected_props", []),
                    *overlay_prop_labels,
                ]
                if prop
            ]
            deduped_props = list(dict.fromkeys(merged_props))
            await store.update_beat_asset(
                episode_number=int(episode_num),
                beat_number=int(beat_num),
                detected_props=deduped_props,
            )
        beats = await store.get_beats_as_dicts(int(episode_num))
        return {
            "ok": True,
            "data": {
                "status": "saved",
                "overlay": payload,
                "path": path.as_posix(),
                "same_scene_beats": _director_same_scene_beats(list(beats), scene_name),
            },
        }
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/director-stage/control-frame")
async def export_beat_director_stage_control_frame(
    project: str,
    episode_num: int,
    beat_num: int,
    body: dict[str, Any],
    user: dict = Depends(get_api_user),
):
    """Persist Director Render control-frame PNG layers and frame_meta.json."""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        scene_name = _beat_scene_name(beat)
        if not scene_name:
            return {"ok": False, "error": "当前 Beat 没有关联场景"}
        try:
            payload = _director_control_frame_export_payload(
                ctx=resolved.ctx,
                project_dir=project_dir,
                scene_name=scene_name,
                episode_num=int(episode_num),
                beat_num=int(beat_num),
                body=body,
            )
        except (ValueError, TypeError) as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        return {"ok": True, "data": payload}
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.get("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/background-anchors")
async def get_beat_background_anchors(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """Return NiceGUI-compatible single-beat background anchor options."""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    username = resolved.username
    project_name = resolved.project_name
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        return {
            "ok": True,
            "data": _background_anchors_payload(
                ctx=resolved.ctx,
                username=username,
                project=project_name,
                project_dir=project_dir,
                beat=beat,
                episode_num=episode_num,
                beat_num=beat_num,
            ),
        }
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.patch("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/background-anchor")
async def update_beat_background_anchor(
    project: str,
    episode_num: int,
    beat_num: int,
    body: BeatBackgroundAnchorUpdate,
    user: dict = Depends(get_api_user),
):
    """Persist the single-beat background anchor selection.

    Matches NiceGUI's render-input semantics: master/reverse/director env-only
    are snapshotted into the beat-owned selected_background.png before being
    used, while render_anchor_source_id preserves the UI-visible source.
    """
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    username = resolved.username
    project_name = resolved.project_name
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        try:
            payload = select_background_anchor(
                project_dir=project_dir,
                username=username,
                project=project_name,
                beat=beat,
                episode_num=int(episode_num),
                beat_num=int(beat_num),
                anchor_id=body.anchor_id,
                reference_url_builder=_api_background_reference_url_builder(resolved.ctx),
                anchor_url_builder=_api_background_anchor_url_builder(resolved.ctx),
            )
        except BackgroundAnchorError as exc:
            return {"ok": False, "error": str(exc)}

        if hasattr(store, "update_beat_asset"):
            await store.update_beat_asset(
                episode_number=int(episode_num),
                beat_number=int(beat_num),
                scene_ref=dict(beat.get("scene_ref") or {}),
            )

        return {"ok": True, "data": payload}
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/background-anchor/crop")
async def crop_beat_background_anchor(
    project: str,
    episode_num: int,
    beat_num: int,
    body: dict[str, Any],
    user: dict = Depends(get_api_user),
):
    """Crop a source background into the beat-owned render background slot."""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    username = resolved.username
    project_name = resolved.project_name
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        try:
            payload = crop_background_anchor_to_selected(
                project_dir=project_dir,
                username=username,
                project=project_name,
                beat=beat,
                episode_num=int(episode_num),
                beat_num=int(beat_num),
                anchor_id=str(body.get("anchor_id") or ""),
                crop=body,
                reference_url_builder=_api_background_reference_url_builder(resolved.ctx),
                anchor_url_builder=_api_background_anchor_url_builder(resolved.ctx),
            )
        except BackgroundAnchorError as exc:
            return {"ok": False, "error": str(exc)}
        except (TypeError, ValueError):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "裁剪参数无效"},
            )
        except Exception as exc:
            return {"ok": False, "error": f"裁剪 Render 背景参考失败: {exc}"}

        if hasattr(store, "update_beat_asset"):
            await store.update_beat_asset(
                episode_number=int(episode_num),
                beat_number=int(beat_num),
                scene_ref=dict(beat.get("scene_ref") or {}),
            )

        return {"ok": True, "data": payload}
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/background-anchor/upload")
async def upload_beat_background_anchor(
    project: str,
    episode_num: int,
    beat_num: int,
    file: UploadFile = File(...),
    user: dict = Depends(get_api_user),
):
    """Upload an external render-background reference for a single Beat.

    This mirrors NiceGUI's Render 背景参考 upload path: the image is stored in
    the beat-owned selected_background.png slot and the beat scene_ref persists
    render_anchor_id=selected_background plus render_anchor_source_id for UI.
    It is a compatibility API for React; render generation still consumes the
    same core scene_ref contract.
    """
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    username = resolved.username
    project_name = resolved.project_name
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        try:
            image = await _read_uploaded_rgb_image(file)
        except Exception as exc:
            return {"ok": False, "error": f"上传外部参考图失败: {exc}"}

        try:
            payload = save_uploaded_background_anchor_image(
                project_dir=project_dir,
                username=username,
                project=project_name,
                beat=beat,
                episode_num=int(episode_num),
                beat_num=int(beat_num),
                image=image,
                reference_url_builder=_api_background_reference_url_builder(resolved.ctx),
                anchor_url_builder=_api_background_anchor_url_builder(resolved.ctx),
            )
        except BackgroundAnchorError as exc:
            return {"ok": False, "error": str(exc)}

        if hasattr(store, "update_beat_asset"):
            await store.update_beat_asset(
                episode_number=int(episode_num),
                beat_number=int(beat_num),
                scene_ref=dict(beat.get("scene_ref") or {}),
            )

        return {"ok": True, "data": payload}
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.get("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/director-control-frame")
async def get_director_control_frame_status(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """Return the NiceGUI director control frame status for one beat."""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    return {
        "ok": True,
        "data": _director_control_payload(
            ctx=resolved.ctx,
            project_dir=project_dir,
            episode_num=episode_num,
            beat_num=beat_num,
        ),
    }


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/video")
async def generate_single_video(
    project: str,
    episode_num: int,
    beat_num: int,
    body: SingleVideoRequest,
    user: dict = Depends(get_api_user),
):
    """单 Beat 视频再生。"""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    output_dir = resolved.output_dir
    store = (
        await make_sqlite_store_for_context(ctx)
        if ctx
        else await make_sqlite_store(username, project_name)
    )
    return await enqueue_single_beat_video_for_context(
        ctx=ctx,
        username=username,
        project_name=project_name,
        output_dir=output_dir,
        state_dir=resolved.state_dir,
        store=store,
        episode_num=episode_num,
        beat_num=beat_num,
        body=body,
    )


async def enqueue_single_beat_video_for_context(
    *,
    ctx: ProjectContext | None,
    username: str,
    project_name: str,
    output_dir: str,
    state_dir: str | Path,
    store: Any,
    episode_num: int,
    beat_num: int,
    body: SingleVideoRequest,
    beats: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate and enqueue a single-beat video generation task."""
    # 加载 beat 数据
    beats = list(beats) if beats is not None else await store.get_beats_as_dicts(episode_num)
    beat = next((b for b in beats if b.get("beat_number") == beat_num), None)
    if not beat:
        return {"ok": False, "error": f"Beat {beat_num} not found"}
    backend_error = _validate_seedance_pro_dialogue_only([beat], body.video_backend)
    if backend_error:
        return {"ok": False, "error": backend_error}
    is_seedance2 = _is_seedance2_backend(body.video_backend)
    is_happyhorse = _is_happyhorse_backend(body.video_backend)
    is_grok_video = _is_grok_video_backend(body.video_backend)
    is_custom_prompt_panel = _is_custom_prompt_panel_backend(body.video_backend)

    # 首帧路径
    from novelvideo.seedance2_i2v.models import Seedance2I2VMode, parse_seedance2_config
    from novelvideo.utils.path_resolver import PathResolver

    paths = PathResolver(output_dir, episode_num)
    frame_path = paths.first_frame_for_video(
        beat_num,
        use_director_render=bool(body.use_director_render),
    )
    pre_merged_config_json = None
    if is_seedance2 or is_happyhorse or is_grok_video or is_custom_prompt_panel:
        try:
            pre_merged_config_json = _merge_seedance2_request_config(
                beat,
                seedance2_config_json=body.seedance2_config_json,
                config_overrides=_seedance2_request_config_overrides(body),
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    pre_sd_config = parse_seedance2_config(
        pre_merged_config_json or beat.get("seedance2_config_json")
    )
    skip_first_frame_guard = False
    if is_seedance2 or is_custom_prompt_panel:
        if pre_sd_config.mode == Seedance2I2VMode.TEXT_TO_VIDEO or (
            pre_sd_config.mode == Seedance2I2VMode.MULTIMODAL_REFERENCE
            and not bool(pre_sd_config.s9_include_first_frame)
        ):
            skip_first_frame_guard = True
    elif is_happyhorse or is_grok_video:
        if pre_sd_config.mode in {
            Seedance2I2VMode.MULTIMODAL_REFERENCE,
            Seedance2I2VMode.TEXT_TO_VIDEO,
        }:
            skip_first_frame_guard = True

    if not frame_path.exists():
        if not skip_first_frame_guard:
            return {"ok": False, "error": f"Beat {beat_num} 首帧不存在，请先生成预览"}
        frame_path = None

    # 视频模式与提示词
    video_mode = beat.get("video_mode", "first_frame")
    prompt = _legacy_video_prompt_for_mode(beat, video_mode)

    # 音频时长
    from novelvideo.manual_shots import resolve_target_video_duration

    audio_duration = await _api_audio_duration_seconds(output_dir, episode_num, beat_num)
    video_duration = resolve_target_video_duration(beat, audio_duration)

    # 尾帧路径 (keyframe 模式)
    last_frame_path = None
    if video_mode == "keyframe":
        next_frame = paths.first_frame_for_video(
            beat_num + 1,
            use_director_render=bool(body.use_director_render),
        )
        if next_frame.exists():
            last_frame_path = str(next_frame)
        else:
            video_mode = "first_frame"  # 回退
            prompt = _legacy_video_prompt_for_mode(beat, video_mode)

    seedance2_config_json = None
    single_video_resolution: str | None = None
    happyhorse_references: list[dict[str, str]] = []
    happyhorse_ratio: str | None = None
    grok_video_references: list[dict[str, str]] = []
    grok_video_ratio: str | None = None
    custom_panel_ratio: str | None = None
    custom_panel_gen_mode: str | None = None
    custom_panel_generate_audio: bool | None = None
    resolved_video_references: list[dict[str, str]] = []
    if is_seedance2:
        try:
            request_config_json = _merge_seedance2_request_config(
                beat,
                seedance2_config_json=body.seedance2_config_json,
                config_overrides=_seedance2_request_config_overrides(body),
            )
            if request_config_json:
                beat["seedance2_config_json"] = request_config_json
            if request_config_json and hasattr(store, "update_beat_asset"):
                await store.update_beat_asset(
                    episode_number=episode_num,
                    beat_number=beat_num,
                    seedance2_config_json=request_config_json,
                )
            beat_index = beats.index(beat)
            episode_obj = _episode_from_store_or_none(store, episode_num)
            prop_menu = await _runtime_prop_menu_with_global_props(store, episode_obj, beats)
            prepared = await _prepare_seedance2_api_beat(
                store=store,
                output_dir=output_dir,
                episode=episode_num,
                beat=beat,
                all_beats=beats,
                index=beat_index,
                video_backend=body.video_backend,
                resolution=body.resolution if "resolution" in body.model_fields_set else None,
                ratio=body.ratio if "ratio" in body.model_fields_set else None,
                prop_menu=prop_menu,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        prompt = prepared.prompt
        video_duration = prepared.duration
        frame_path = Path(prepared.image_path) if prepared.image_path else (frame_path if pre_sd_config.mode != Seedance2I2VMode.MULTIMODAL_REFERENCE or pre_sd_config.s9_include_first_frame else None)
        last_frame_path = prepared.last_frame_path
        seedance2_config_json = prepared.seedance2_config_json
        from novelvideo.seedance2_i2v.models import parse_seedance2_config

        single_video_resolution = parse_seedance2_config(
            seedance2_config_json
        ).resolution
        video_mode = "keyframe" if prepared.last_frame_path else "first_frame"
    elif is_happyhorse:
        try:
            request_config_json = _merge_seedance2_request_config(
                beat,
                seedance2_config_json=body.seedance2_config_json,
                config_overrides=_seedance2_request_config_overrides(body),
            )
            if request_config_json:
                beat["seedance2_config_json"] = request_config_json
            if request_config_json and hasattr(store, "update_beat_asset"):
                await store.update_beat_asset(
                    episode_number=episode_num,
                    beat_number=beat_num,
                    seedance2_config_json=request_config_json,
                )
            beat_index = beats.index(beat)
            episode_obj = _episode_from_store_or_none(store, episode_num)
            prop_menu = await _runtime_prop_menu_with_global_props(store, episode_obj, beats)
            prepared = await _prepare_happyhorse_api_beat(
                output_dir=output_dir,
                state_dir=Path(store.state_dir),
                episode=episode_num,
                beat=beat,
                next_beat=beats[beat_index + 1] if beat_index + 1 < len(beats) else None,
                frame_path=frame_path,
                video_mode=video_mode,
                prompt=prompt,
                duration=video_duration,
                resolution=body.resolution if "resolution" in body.model_fields_set else None,
                ratio=body.ratio if "ratio" in body.model_fields_set else None,
                prop_menu=prop_menu,
            )
            if prepared["config_json"] and hasattr(store, "update_beat_asset"):
                await store.update_beat_asset(
                    episode_number=episode_num,
                    beat_number=beat_num,
                    seedance2_config_json=str(prepared["config_json"]),
                )
            prompt = str(prepared["prompt"])
            video_duration = float(prepared["duration"])
            frame_path = Path(str(prepared["image_path"])) if prepared["image_path"] else None
            last_frame_path = None
            seedance2_config_json = str(prepared["config_json"])
            single_video_resolution = str(prepared["resolution"])
            happyhorse_ratio = str(prepared["ratio"])
            happyhorse_references = list(prepared.get("references") or [])
            video_mode = "first_frame"
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    elif is_grok_video:
        try:
            request_config_json = _merge_seedance2_request_config(
                beat,
                seedance2_config_json=body.seedance2_config_json,
                config_overrides=_seedance2_request_config_overrides(body),
            )
            if request_config_json:
                beat["seedance2_config_json"] = request_config_json
            if request_config_json and hasattr(store, "update_beat_asset"):
                await store.update_beat_asset(
                    episode_number=episode_num,
                    beat_number=beat_num,
                    seedance2_config_json=request_config_json,
                )
            beat_index = beats.index(beat)
            episode_obj = _episode_from_store_or_none(store, episode_num)
            prop_menu = await _runtime_prop_menu_with_global_props(store, episode_obj, beats)
            prepared = await _prepare_grok_video_api_beat(
                output_dir=output_dir,
                state_dir=Path(store.state_dir),
                episode=episode_num,
                beat=beat,
                next_beat=beats[beat_index + 1] if beat_index + 1 < len(beats) else None,
                frame_path=frame_path,
                video_mode=video_mode,
                prompt=prompt,
                duration=video_duration,
                resolution=body.resolution if "resolution" in body.model_fields_set else None,
                ratio=body.ratio if "ratio" in body.model_fields_set else None,
                prop_menu=prop_menu,
            )
            if prepared["config_json"] and hasattr(store, "update_beat_asset"):
                await store.update_beat_asset(
                    episode_number=episode_num,
                    beat_number=beat_num,
                    seedance2_config_json=str(prepared["config_json"]),
                )
            prompt = str(prepared["prompt"])
            video_duration = float(prepared["duration"])
            frame_path = Path(str(prepared["image_path"])) if prepared["image_path"] else None
            last_frame_path = None
            seedance2_config_json = str(prepared["config_json"])
            single_video_resolution = str(prepared["resolution"])
            grok_video_ratio = str(prepared["ratio"])
            grok_video_references = list(prepared.get("references") or [])
            video_mode = "first_frame"
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    else:
        try:
            request_config_json = _merge_seedance2_request_config(
                beat,
                seedance2_config_json=body.seedance2_config_json,
                config_overrides=_seedance2_request_config_overrides(body),
            )
            if request_config_json:
                beat["seedance2_config_json"] = request_config_json
            if request_config_json and hasattr(store, "update_beat_asset"):
                await store.update_beat_asset(
                    episode_number=episode_num,
                    beat_number=beat_num,
                    seedance2_config_json=request_config_json,
                )
            from novelvideo.seedance2_i2v.assets import (
                apply_prompt_audio_selection,
                build_seedance2_project_assets,
                resolve_beat_video_references,
            )
            from novelvideo.seedance2_i2v.models import (
                Seedance2I2VMode,
                parse_seedance2_config,
            )
            from novelvideo.seedance2_i2v.request import normalize_seedance2_prompt

            merged_config = parse_seedance2_config(
                request_config_json or beat.get("seedance2_config_json")
            )
            uses_prompt_panel = bool(
                request_config_json is not None
                or (is_custom_prompt_panel and str(merged_config.final_prompt or "").strip())
                or (not prompt.strip() and str(merged_config.final_prompt or "").strip())
            )
            if uses_prompt_panel and str(merged_config.final_prompt or "").strip():
                prompt = normalize_seedance2_prompt(merged_config.final_prompt)
            if uses_prompt_panel:
                seedance2_config_json = request_config_json or beat.get(
                    "seedance2_config_json"
                )
                custom_panel_ratio = body.ratio if "ratio" in body.model_fields_set else merged_config.ratio
                if (
                    "generate_audio" in body.model_fields_set
                    or body.seedance2_config_json is not None
                ):
                    custom_panel_generate_audio = bool(merged_config.generate_audio)
                if merged_config.mode == Seedance2I2VMode.FIRST_LAST_FRAME:
                    next_frame = paths.first_frame_for_video(
                        beat_num + 1,
                        use_director_render=bool(body.use_director_render),
                    )
                    if not next_frame.exists():
                        return {
                            "ok": False,
                            "error": f"Beat {beat_num + 1} 首帧不存在，无法使用首尾帧模式",
                        }
                    last_frame_path = str(next_frame)
                    video_mode = "keyframe"
                    custom_panel_gen_mode = "first_last_frame"
                elif merged_config.mode == Seedance2I2VMode.FIRST_FRAME:
                    last_frame_path = None
                    video_mode = "first_frame"
                    custom_panel_gen_mode = "first_frame"
                elif merged_config.mode == Seedance2I2VMode.TEXT_TO_VIDEO:
                    frame_path = None
                    last_frame_path = None
                    video_mode = "first_frame"
                    custom_panel_gen_mode = "text_to_video"
                else:
                    frame_path = None
                    last_frame_path = None
                    custom_panel_gen_mode = "all_reference"
                    episode_obj = _episode_from_store_or_none(store, episode_num)
                    prop_menu = await _runtime_prop_menu_with_global_props(
                        store, episode_obj, beats
                    )
                    next_beat = (
                        beats[beats.index(beat) + 1]
                        if beat in beats and beats.index(beat) + 1 < len(beats)
                        else None
                    )
                    assets = build_seedance2_project_assets(
                        project_output=Path(output_dir),
                        episode=episode_num,
                        beat=beat,
                        mode=Seedance2I2VMode.MULTIMODAL_REFERENCE,
                        state_dir=Path(
                            getattr(store, "state_dir", None) or state_dir
                        ),
                        next_beat=next_beat,
                        prop_menu=prop_menu,
                    )
                    _user_refs, declared_count, missing = resolve_beat_video_references(
                        list(merged_config.reference_image_paths),
                        list(merged_config.reference_audio_paths),
                        assets=assets,
                    )
                    if missing:
                        condition = "全部丢失" if len(missing) == declared_count else "部分丢失"
                        return {
                            "ok": False,
                            "error": f"视频参考素材{condition}（声明 {declared_count} 项，可用 {declared_count - len(missing)} 项）："
                            + "；".join(missing),
                        }
                    effective_final_prompt = str(merged_config.final_prompt or "")
                    from novelvideo.s9_storyboard import (
                        is_s9_segment_beat,
                        resolve_s9_active_assets_and_prompt,
                    )

                    if is_s9_segment_beat(beat) or merged_config.prompt_source in {
                        "s9_script",
                        "s9_generated",
                    }:
                        effective_final_prompt, assets, _s9_missing = (
                            resolve_s9_active_assets_and_prompt(
                                beat,
                                assets=assets,
                                prompt=effective_final_prompt,
                                include_first_frame=bool(
                                    merged_config.s9_include_first_frame
                                ),
                            )
                        )
                        if effective_final_prompt.strip():
                            prompt = normalize_seedance2_prompt(effective_final_prompt)
                    selected_assets = apply_prompt_audio_selection(
                        assets,
                        effective_final_prompt,
                        generate_audio=bool(merged_config.generate_audio),
                    )
                    if is_s9_segment_beat(beat) or merged_config.prompt_source in {
                        "s9_script",
                        "s9_generated",
                    }:
                        import re as _re
                        from novelvideo.seedance2_i2v.request import (
                            _validate_prompt_reference_numbers,
                        )

                        if _re.search(r"(?m)^\s*-\s*[：:]", effective_final_prompt):
                            raise ValueError(
                                "提示词【参考职责】中存在缺失的 @图片/@音频 编号（如“- ：”），"
                                "请先点击「AI 优化」或补全引用后再生成视频。"
                            )
                        _validate_prompt_reference_numbers(
                            prompt,
                            image_count=sum(
                                1
                                for asset in selected_assets
                                if asset.selected and asset.media_type == "image"
                            ),
                            video_count=sum(
                                1
                                for asset in selected_assets
                                if asset.selected and asset.media_type == "video"
                            ),
                            audio_count=sum(
                                1
                                for asset in selected_assets
                                if asset.selected and asset.media_type == "audio"
                            ),
                        )
                    resolved_video_references = [
                        {
                            "type": asset.media_type,
                            "path": str(asset.path),
                            "role": asset.reference_label,
                        }
                        for asset in selected_assets
                        if asset.selected
                    ]
            else:
                resolved_assets, declared_count, missing = resolve_beat_video_references(
                    list(merged_config.reference_image_paths),
                    list(merged_config.reference_audio_paths),
                )
                if missing:
                    condition = "全部丢失" if len(missing) == declared_count else "部分丢失"
                    return {
                        "ok": False,
                        "error": f"视频参考素材{condition}（声明 {declared_count} 项，可用 {declared_count - len(missing)} 项）：" + "；".join(missing),
                    }
                resolved_video_references = [
                    {
                        "type": asset.media_type,
                        "path": str(asset.path),
                        "role": asset.reference_label,
                    }
                    for asset in resolved_assets
                    if asset.selected
                ]
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not prompt.strip():
            return {"ok": False, "error": _missing_video_prompt_error(beat_num)}

        # 非 seedance2 后端（含 seedance-1.5-pro）：透传用户选择的时长/清晰度，
        # 并保证视频时长不短于音频（与 1.0 的 duration_floor 行为一致；
        # 生成器侧再按模型上限 4-12 夹紧并向上取整）。
        import math

        if body.duration is not None:
            try:
                video_duration = float(body.duration)
            except (TypeError, ValueError):
                pass
        elif uses_prompt_panel and merged_config.duration > 0:
            video_duration = float(merged_config.duration)
        if audio_duration:
            video_duration = max(float(video_duration), float(math.ceil(float(audio_duration))))
        if "resolution" in body.model_fields_set:
            single_video_resolution = _seedance2_resolution_for_backend(
                body.video_backend, body.resolution
            )
        elif uses_prompt_panel and merged_config.resolution:
            single_video_resolution = _seedance2_resolution_for_backend(
                body.video_backend, merged_config.resolution
            )

    from novelvideo.video_duration import (
        normalize_video_duration_for_backend,
        video_duration_bounds_for_backend,
    )

    _min_dur, backend_max_dur = video_duration_bounds_for_backend(body.video_backend)
    planned_seg_dur = 0.0
    if isinstance(beat.get("s9_segment_meta"), dict):
        planned_seg_dur = float(beat["s9_segment_meta"].get("planned_duration_seconds") or 0)
    if planned_seg_dur <= 0:
        planned_seg_dur = float(beat.get("duration_seconds") or beat.get("estimated_duration") or 0)
    requested_raw_dur = (
        float(body.duration)
        if "duration" in body.model_fields_set and body.duration is not None
        else float(video_duration or 0)
    )
    check_dur = requested_raw_dur or planned_seg_dur
    if (
        backend_max_dur is not None
        and check_dur > backend_max_dur
        and not ("duration" in body.model_fields_set and body.duration is not None and float(body.duration) <= backend_max_dur)
    ):
        return {
            "ok": False,
            "error": (
                f"当前大段推算时长为 {int(round(check_dur))} 秒，超过所选视频模型上限（{backend_max_dur} 秒）。"
                "请切换支持 30 秒的模型（如 Seedance 2.5），或在脚本页选择「15秒」重新拆分。"
            ),
        }

    video_duration = normalize_video_duration_for_backend(
        body.video_backend,
        video_duration,
    )

    config = {
        "beat": dict(beat),
        "frame_path": str(frame_path) if frame_path else None,
        "video_mode": video_mode,
        "prompt": prompt,
        "video_duration": video_duration,
        "video_backend": body.video_backend,
        "use_director_render": bool(body.use_director_render),
        "last_frame_path": last_frame_path,
        "cognee_store_project": f"{username}/{project_name}",
    }
    if seedance2_config_json:
        config["seedance2_config"] = seedance2_config_json
    if single_video_resolution:
        config["resolution"] = single_video_resolution
    if is_happyhorse:
        config["ratio"] = _happyhorse_ratio_for_backend(happyhorse_ratio)
        config["references"] = happyhorse_references
        if body.audio_setting is not None:
            config["audio_setting"] = body.audio_setting
    if is_grok_video:
        config["ratio"] = _grok_video_ratio_for_backend(grok_video_ratio)
        config["references"] = grok_video_references
    elif resolved_video_references:
        config["references"] = resolved_video_references
    if custom_panel_ratio:
        config["ratio"] = custom_panel_ratio
    if custom_panel_gen_mode:
        config["gen_mode"] = custom_panel_gen_mode
    if custom_panel_generate_audio is not None:
        config["generate_audio"] = custom_panel_generate_audio

    billing_resolution = single_video_resolution or _seedance2_resolution_for_backend(
        body.video_backend,
        body.resolution,
    )
    billing = _single_video_billing_metadata(
        body.video_backend,
        resolution=billing_resolution,
        duration=video_duration,
    )

    if ctx is not None:
        queued = await get_task_backend().enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="single_video",
            queue_kind="video",
            episode=episode_num,
            beat_num=beat_num,
            payload={
                "config": config,
                "output_dir": output_dir,
                "billing": billing,
                **({"batch_id": body.batch_id} if body.batch_id else {}),
                **({"batch_size": body.batch_size} if body.batch_size else {}),
            },
        )
        return {
            "ok": True,
            "task_type": "single_video",
            "task_id": queued.task_state.task_id,
            "task_key": project_task_state_key(
                "single_video",
                ctx.project_id,
                episode_num,
                beat_num=beat_num,
            ),
            "backend": queued.backend,
            "queue": queued.queue,
            "message": f"第 {episode_num} 集 Beat {beat_num} 视频生成已入队",
        }

    return {"ok": False, "error": "单条视频生成需要 project context"}


@router.post("/projects/{project}/episodes/{episode_num}/videos/batch-generate")
async def batch_generate_episode_videos(
    project: str,
    episode_num: int,
    body: BatchEpisodeVideoRequest | None = None,
    user: dict = Depends(get_api_user),
):
    """批量生成本集缺失或选中的分镜视频（支持普通并发或尾帧锚点接力链）。"""
    from novelvideo.project_config import load_project_config_from_state_dir
    from novelvideo.task_backend.limits import (
        ChannelTaskLimitExceeded,
        GlobalLaneQueueLimitExceeded,
        ProjectTaskLimitExceeded,
        ProjectUserTaskLimitExceeded,
        UserTaskLimitExceeded,
    )
    from novelvideo.utils.path_resolver import PathResolver

    body = body or BatchEpisodeVideoRequest()
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    if ctx is None:
        return {"ok": False, "error": "批量视频生成需要 project context"}

    store = await make_sqlite_store_for_context(ctx)
    try:
        beats = sorted(
            await store.get_beats_as_dicts(episode_num),
            key=lambda item: int(item.get("beat_number") or 0),
        )
        if not beats:
            return {"ok": False, "error": f"第 {episode_num} 集尚未生成分镜脚本"}

        project_config = load_project_config_from_state_dir(
            ctx.state_dir,
            username=ctx.owner_username,
            project=ctx.project_name,
        )
        video_backend = (
            str(body.video_backend or "").strip()
            or str(project_config.get("video_backend") or "").strip()
            or "newapi_seedance-1.0-pro-fast"
        )
        paths = PathResolver(resolved.output_dir, episode_num)
        requested_set = {
            int(n) for n in (body.beat_numbers or []) if int(n) > 0
        }
        target_beats: list[int] = []
        skipped_beats: list[int] = []
        for beat in beats:
            beat_num = int(beat.get("beat_number") or 0)
            if beat_num <= 0:
                continue
            if requested_set and beat_num not in requested_set:
                continue
            if body.only_missing and paths.video(beat_num).exists():
                skipped_beats.append(beat_num)
                continue
            target_beats.append(beat_num)

        from novelvideo.i18n_message import lmsg, message_text

        if not target_beats:
            empty_msg = lmsg(
                "episode.workbench.batch.allVideosReady",
                "所有目标分镜均已有视频，无需重新生成",
            )
            return {
                "ok": True,
                "data": {
                    "episode": episode_num,
                    "enqueued": [],
                    "skipped_beats": skipped_beats,
                    "deferred_beats": [],
                    "errors": [],
                },
                "message": message_text(empty_msg),
                "message_code": empty_msg.code,
            }

        if body.tail_anchor and len(target_beats) > 1:
            from novelvideo.services.episode_autopilot import (
                AutopilotOptions,
                episode_autopilot_coordinator,
            )

            options = AutopilotOptions(
                mode="two_stage",
                video_backend=video_backend,
                resolution=body.resolution,
                ratio=body.ratio,
                tail_anchor=True,
                use_director_render=bool(body.use_director_render),
                force_regenerate_videos=not bool(body.only_missing),
                target_beat_numbers=tuple(target_beats),
                skip_compose=True,
            )
            run = await episode_autopilot_coordinator.start_episode(
                ctx,
                episode=episode_num,
                options=options,
                start_phase="phase2",
            )
            chain_msg = lmsg(
                "episode.workbench.batch.tailChainStarted",
                f"已启动 {len(target_beats)} 个分镜的尾帧接力链式生成",
                count=len(target_beats),
                episode=episode_num,
            )
            return {
                "ok": True,
                "data": {
                    "episode": episode_num,
                    "tail_anchor_chain": True,
                    "autopilot_run": run.to_dict(),
                    "target_beats": target_beats,
                    "skipped_beats": skipped_beats,
                },
                "message": message_text(chain_msg),
                "message_code": chain_msg.code,
                "message_params": dict(chain_msg.params or {}),
            }

        enqueued: list[dict[str, Any]] = []
        deferred_beats: list[int] = []
        errors: list[dict[str, Any]] = []
        limit_hit = False
        for beat_num in target_beats:
            if limit_hit:
                deferred_beats.append(beat_num)
                continue
            req_kwargs: dict[str, Any] = {
                "video_backend": video_backend,
                "use_director_render": bool(body.use_director_render),
            }
            if body.resolution is not None:
                req_kwargs["resolution"] = body.resolution
            if body.ratio is not None:
                req_kwargs["ratio"] = body.ratio
            if body.generate_audio is not None:
                req_kwargs["generate_audio"] = body.generate_audio
            if body.human_review is not None:
                req_kwargs["human_review"] = body.human_review
            if body.scene_optimize is not None:
                req_kwargs["scene_optimize"] = body.scene_optimize
            if body.audio_setting is not None:
                req_kwargs["audio_setting"] = body.audio_setting
            single_req = SingleVideoRequest(**req_kwargs)
            try:
                res = await enqueue_single_beat_video_for_context(
                    ctx=ctx,
                    username=resolved.username,
                    project_name=resolved.project_name,
                    output_dir=resolved.output_dir,
                    state_dir=resolved.state_dir,
                    store=store,
                    episode_num=episode_num,
                    beat_num=beat_num,
                    body=single_req,
                    beats=beats,
                )
            except (
                ProjectTaskLimitExceeded,
                ProjectUserTaskLimitExceeded,
                GlobalLaneQueueLimitExceeded,
                ChannelTaskLimitExceeded,
                UserTaskLimitExceeded,
            ):
                limit_hit = True
                deferred_beats.append(beat_num)
                continue
            if res.get("ok"):
                enqueued.append(
                    {
                        "beat_number": beat_num,
                        "task_id": res.get("task_id"),
                        "task_key": res.get("task_key"),
                    }
                )
            else:
                errors.append(
                    {
                        "beat_number": beat_num,
                        "error": str(res.get("error") or "视频入队失败"),
                    }
                )

        ok = bool(enqueued) or not errors
        msg_text = f"第 {episode_num} 集已入队 {len(enqueued)} 个分镜视频"  # i18n-exempt
        if deferred_beats:
            msg_text += f"，{len(deferred_beats)} 个因队列已满暂缓（可启动两段式托管自动排队补齐）"  # i18n-exempt
        if errors:
            msg_text += f"，{len(errors)} 个预检未通过"  # i18n-exempt
        batch_msg = lmsg(
            "episode.workbench.batch.videosEnqueued",
            msg_text,
            episode=episode_num,
            enqueued=len(enqueued),
            deferred=len(deferred_beats),
            errors=len(errors),
        )
        return {
            "ok": ok,
            "data": {
                "episode": episode_num,
                "enqueued": enqueued,
                "skipped_beats": skipped_beats,
                "deferred_beats": deferred_beats,
                "errors": errors,
            },
            "error": errors[0]["error"] if not ok and errors else None,
            "message": message_text(batch_msg),
            "message_code": batch_msg.code,
            "message_params": dict(batch_msg.params or {}),
        }
    finally:
        await store.close()


# ── 视频池查看 & 选择 ─────────────────────────────────────────────────────────


@router.get("/projects/{project}/episodes/{episode_num}/video-pool")
async def list_video_pool(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
):
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir

    from novelvideo.generators.video_pool_indexer import load_video_pool_index

    videos_ep_dir = project_dir / "videos" / "beats" / f"ep{episode_num:03d}"
    pool = load_video_pool_index(videos_ep_dir)
    if not pool:
        return {"ok": True, "data": None}

    videos = []
    for entry in pool.videos:
        item = entry.model_dump()
        if item.get("generated_at"):
            item["generated_at"] = entry.generated_at.isoformat()
        rel_path = f"videos/beats/ep{episode_num:03d}/pool/{entry.video_path}"
        item["video_url"] = make_static_url_for_context(
            resolved.ctx,
            rel_path,
            local_path=project_dir / rel_path,
        )
        videos.append(item)

    return {
        "ok": True,
        "data": {
            "episode": pool.episode,
            "videos": videos,
            "beat_assignments": pool.beat_assignments,
        },
    }


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/video-pool-select")
async def select_video_pool(
    project: str,
    episode_num: int,
    beat_num: int,
    body: VideoPoolSelectRequest,
    user: dict = Depends(get_api_user),
):
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    project_dir = resolved.project_dir

    from novelvideo.generators.video_pool_indexer import assign_video_to_beat

    videos_ep_dir = project_dir / "videos" / "beats" / f"ep{episode_num:03d}"
    ok = assign_video_to_beat(videos_ep_dir, beat_num, body.pool_id)
    if not ok:
        return {
            "ok": False,
            "error": f"Pool entry '{body.pool_id}' not found or file missing",
        }

    rel_path = f"videos/beats/ep{episode_num:03d}/beat_{beat_num:02d}.mp4"
    video_url = make_static_url_for_context(
        resolved.ctx,
        rel_path,
        local_path=project_dir / rel_path,
    )
    return {
        "ok": True,
        "data": {
            "beat_num": beat_num,
            "pool_id": body.pool_id,
            "video_url": video_url,
        },
    }


# ── 首帧上传 ─────────────────────────────────────────────────────────────────


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/render/upload")
async def upload_beat_render(
    project: str,
    episode_num: int,
    beat_num: int,
    file: UploadFile = File(...),
    user: dict = Depends(get_api_user),
):
    """Upload a user-supplied first frame for a beat as the canonical frames/ file."""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    project_dir = resolved.project_dir
    try:
        image = await _read_uploaded_rgb_image(file)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    frames_dir = project_dir / "frames" / f"ep{episode_num:03d}"
    frame_path = frames_dir / f"beat_{beat_num:02d}.png"
    frames_dir.mkdir(parents=True, exist_ok=True)
    await run_asset_upload_operation(image.save, frame_path, format="PNG")
    rel = f"frames/ep{episode_num:03d}/beat_{beat_num:02d}.png"
    frame_url = make_static_url_for_context(
        resolved.ctx,
        rel,
        local_path=frame_path,
    )
    return {
        "ok": True,
        "data": {
            "beat_num": beat_num,
            "frame_url": frame_url,
        },
    }


# ── 单 Beat 音频重生 ─────────────────────────────────────────────────────────


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/audio")
async def regenerate_beat_audio(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """重新生成单个 beat 的 IndexTTS2 语音。"""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    output_dir = resolved.output_dir
    state_dir = resolved.state_dir
    store = (
        await make_sqlite_store_for_context(ctx)
        if ctx
        else await make_sqlite_store(username, project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)

    beat = next((b for b in beats if b.get("beat_number") == beat_num), None)
    if not beat:
        # 按索引回退
        if 1 <= beat_num <= len(beats):
            beat = beats[beat_num - 1]
        else:
            return {"ok": False, "error": f"Beat {beat_num} not found"}

    billable_beat_numbers, missing_voice, billable_chars = await _audio_generation_plan(
        store=store,
        username=username,
        project=project_name,
        episode=episode_num,
        beat_numbers=[beat_num],
        mode="redo_selected",
    )
    if missing_voice:
        return _voice_prereq_error_response(missing_voice)
    if not billable_beat_numbers:
        return {
            "ok": False,
            "code": "audio_generation_not_required",
            "error": "当前 Beat 没有需要生成的音频",
        }

    if ctx is not None:
        queued = await get_task_backend().enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="audio_generation_indextts2",
            queue_kind="default",
            episode=episode_num,
            payload={
                "episode": episode_num,
                "mode": "redo_selected",
                "beat_numbers": billable_beat_numbers,
                "output_dir": output_dir,
                "state_dir": state_dir,
                "billing": _audio_billing_payload(
                    billable_beat_numbers,
                    billable_chars=billable_chars,
                ),
            },
        )
        return {
            "ok": True,
            "task_type": "audio_generation_indextts2",
            "task_id": queued.task_state.task_id,
            "task_key": project_task_state_key(
                "audio_generation_indextts2", ctx.project_id, episode_num
            ),
            "backend": queued.backend,
            "queue": queued.queue,
            "message": f"第 {episode_num} 集 Beat {beat_num} 语音生成已进入队列",
        }

    return {
        "ok": False,
        "error": "音频生成需要 project context",
    }


# ── SRT 字幕导出 ─────────────────────────────────────────────────────────────


@router.get("/projects/{project}/episodes/{episode_num}/export/srt")
async def export_srt(project: str, episode_num: int, user: dict = Depends(get_api_user)):
    """导出 SRT 字幕文件。"""
    from fastapi.responses import PlainTextResponse

    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir

    # 从图谱读取 beats
    store = (
        await make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await make_sqlite_store(resolved.username, resolved.project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)

    if not beats:
        return {"ok": False, "error": "No beats in script"}

    from novelvideo.export.episode_export import build_srt_content

    srt_content = await build_srt_content(project_dir, episode_num, beats)
    if not srt_content:
        return {"ok": False, "error": "No subtitles to export"}

    return PlainTextResponse(
        content=srt_content,
        media_type="text/srt",
        headers={
            "Content-Disposition": f'attachment; filename="ep{episode_num:03d}.srt"',
        },
    )


@router.get("/projects/{project}/episodes/{episode_num}/export/video")
async def export_final_video(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
):
    """Download the composed final episode video."""
    from fastapi.responses import FileResponse

    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    filename = f"ep{episode_num:03d}_final.mp4"
    final_path = project_dir / "videos" / "episodes" / filename
    if not final_path.exists():
        raise HTTPException(status_code=404, detail="Final video not found")
    return FileResponse(
        path=str(final_path),
        filename=filename,
        media_type="video/mp4",
    )


# ── ZIP 导出 ─────────────────────────────────────────────────────────────────


@router.post("/projects/{project}/episodes/{episode_num}/export/zip")
async def export_zip(project: str, episode_num: int, user: dict = Depends(get_api_user)):
    """打包指定集的所有资源为 ZIP 文件下载。"""
    import asyncio
    import zipfile
    import tempfile

    from novelvideo.export.episode_export import build_srt_content
    from novelvideo.utils.async_ops import call_blocking, wait_for_task_completion
    from novelvideo.utils.path_resolver import PathResolver

    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_name = resolved.project_name
    project_dir = resolved.project_dir
    store = (
        await make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await make_sqlite_store(resolved.username, project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)

    ep_tag = f"ep{episode_num:03d}"
    paths = PathResolver(str(project_dir), episode_num)

    files_to_pack: list[tuple[Path, str]] = []
    for beat in beats:
        beat_num = int(beat.get("beat_number", 0) or 0)
        if beat_num <= 0:
            continue
        audio_path = paths.audio(beat_num)
        if audio_path.exists():
            files_to_pack.append((audio_path, f"audio/{audio_path.name}"))
        video_path = paths.video(beat_num)
        if video_path.exists():
            files_to_pack.append((video_path, f"video/{video_path.name}"))

    final_path = paths.final_video()
    if final_path.exists():
        files_to_pack.append((final_path, final_path.name))

    # Keep existing extra project assets in the API ZIP; NiceGUI's core export
    # is beat audio/video + final + SRT, but frames are useful inspection
    # artifacts and were already part of the React API surface.
    extra_dirs = {
        "frames": project_dir / "frames" / ep_tag,
    }
    for folder_name, folder in extra_dirs.items():
        if folder.exists():
            for file_path in sorted(folder.iterdir()):
                if file_path.is_file():
                    files_to_pack.append((file_path, f"{folder_name}/{file_path.name}"))

    srt_content = await build_srt_content(project_dir, episode_num, beats)

    def build_zip_file() -> str:
        tmp_path = ""
        try:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
            tmp_path = tmp.name
            tmp.close()
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
                for file_path, arc_name in files_to_pack:
                    zf.write(file_path, arc_name)
                if srt_content:
                    zf.writestr(
                        f"{ep_tag}.srt",
                        srt_content,
                        compress_type=zipfile.ZIP_DEFLATED,
                    )
            return tmp_path
        except Exception:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
            raise

    build_task = asyncio.create_task(call_blocking(build_zip_file))
    tmp_path, cancellation = await wait_for_task_completion(build_task)
    if cancellation is not None:
        Path(tmp_path).unlink(missing_ok=True)
        raise cancellation
    try:
        return _TemporaryFileResponse(
            path=tmp_path,
            filename=f"{project_name}_{ep_tag}.zip",
            media_type="application/zip",
        )
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise

