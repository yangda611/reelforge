"""验证 API 路由。"""

import json
import logging
import re

from fastapi import APIRouter, Depends

logger = logging.getLogger(__name__)

from novelvideo.api.auth import get_api_user
from novelvideo.api.deps import make_sqlite_store_for_context, resolve_project_scope

from .consistency_verifier import ConsistencyVerifier
from .continuity_verifier import ContinuityVerifier
from .episode_reviewer import EpisodeReviewer
from .frame_verifier import FrameVerifier
from .image_verifier import ImageVerifier, resolve_verification_scene_context
from .report_formatter import (
    format_consistency_report,
    format_episode_overview_report,
    format_verification_report,
    save_verify_report,
)
from .schemas import ConsistencyVerifyRequest, ContinuityRequest, VerifyRequest
from .similarity_detector import detect_similarity
from .utils import find_frame_for_beat, find_sketch_for_beat

router = APIRouter()


async def _resolve_verification_project(
    project: str,
    user: dict,
    *,
    required_role: str = "viewer",
):
    return await resolve_project_scope(project, user, required_role=required_role)


async def _load_beat_data(store, episode_num: int, beat_num: int) -> dict:
    beats = await store.get_beats_as_dicts(episode_num)
    for beat in beats:
        if int(beat.get("beat_number") or 0) == beat_num:
            return beat
    raise IndexError(f"Beat {beat_num} not found in episode {episode_num}")


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/verify")
async def verify_beat(
    project: str,
    episode_num: int,
    beat_num: int,
    body: VerifyRequest,
    user: dict = Depends(get_api_user),
):
    """验证单个 beat 的草图/首帧是否匹配描述。"""
    resolved = await _resolve_verification_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    logger.info("verify_beat: project=%s ep=%d beat=%d type=%s", project, episode_num, beat_num, body.type)
    store = await make_sqlite_store_for_context(resolved.ctx)

    # 1. 读取 beat 数据
    try:
        beat = await _load_beat_data(store, episode_num, beat_num)
    except (FileNotFoundError, IndexError) as e:
        return {"ok": False, "error": str(e)}

    # 2. 找到对应图片
    if body.type == "sketch":
        image_path = find_sketch_for_beat(project_dir, episode_num, beat_num)
    elif body.type == "frame":
        image_path = find_frame_for_beat(project_dir, episode_num, beat_num)
    else:
        return {"ok": False, "error": f"Unsupported verify type: {body.type}"}

    if not image_path:
        return {"ok": False, "error": f"No {body.type} image found for beat {beat_num}"}

    # 路径安全检查
    if not image_path.resolve().is_relative_to(project_dir.resolve()):
        return {"ok": False, "error": "Image path outside project directory"}

    # 3. 加载颜色映射（用于角色动作归属验证）
    color_mapping: dict[str, str] = {}
    scenes = []
    try:
        color_mapping = store.get_sketch_colors(episode_num) or {}
        scenes = await store.list_scenes()
    except Exception:
        pass  # 无颜色映射/场景列表时退化为原有行为

    # 4. 调用验证
    visual_desc = beat.get("visual_description", "")
    named_characters = re.findall(r"\{\{([^}]+)\}\}", visual_desc)
    camera_context = beat.get("keyframe_prompt") or beat.get("video_prompt", "")
    scene_context = resolve_verification_scene_context(
        project_dir,
        beat,
        episode_number=episode_num,
        scenes=scenes,
    )

    verifier = ImageVerifier()
    try:
        result = await verifier.verify_sketch(
            str(image_path),
            visual_desc,
            named_characters,
            scene_context["scene_id"],
            beat.get("time_of_day", ""),
            camera_context,
            color_mapping=color_mapping,
            resolved_scene_name=scene_context["resolved_scene_name"],
            time_baked=scene_context["time_baked"],
            prompt_time_of_day=scene_context["prompt_time_of_day"],
        )
    except Exception as e:
        logger.error("verify_beat failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}

    # 5. 构建响应数据
    data = {
        **result.model_dump(),
        "beat_number": beat_num,
        "verify_type": body.type,
        "image_path": image_path.relative_to(project_dir).as_posix(),
        "description_used": beat.get("visual_description", ""),
    }

    # 6. 格式化可读报告 + 持久化
    data["report_text"] = format_verification_report(result.model_dump(), beat_num, body.type)
    report_path = save_verify_report(project_dir, episode_num, beat_num, body.type, data)
    data["report_path"] = report_path.relative_to(project_dir).as_posix()

    return {"ok": True, "data": data}


@router.post("/projects/{project}/episodes/{episode_num}/verify/consistency")
async def verify_consistency(
    project: str,
    episode_num: int,
    body: ConsistencyVerifyRequest = ConsistencyVerifyRequest(),
    user: dict = Depends(get_api_user),
):
    """检查整集跨 beat 的角色/服装一致性。支持 verify_type="sketch"(默认) 或 "frame"。"""
    resolved = await _resolve_verification_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    logger.info("verify_consistency: project=%s ep=%d type=%s", project, episode_num, body.verify_type)

    store = await make_sqlite_store_for_context(resolved.ctx)
    verifier = ConsistencyVerifier()
    try:
        data = await verifier.verify_consistency(project_dir, episode_num, verify_type=body.verify_type, sqlite_store=store)
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error("verify_consistency failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}

    # 报告类型区分
    report_type = "frame_consistency" if body.verify_type == "frame" else "consistency"
    data["report_text"] = format_consistency_report(data, episode_num)
    report_path = save_verify_report(project_dir, episode_num, None, report_type, data)
    data["report_path"] = report_path.relative_to(project_dir).as_posix()

    return {"ok": True, "data": data}


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/verify-frame")
async def verify_frame(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """验证单个 beat 的首帧渲染质量（对比草图）。"""
    resolved = await _resolve_verification_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    logger.info("verify_frame: project=%s ep=%d beat=%d", project, episode_num, beat_num)
    store = await make_sqlite_store_for_context(resolved.ctx)

    # 1. 读取 beat 数据
    try:
        beat = await _load_beat_data(store, episode_num, beat_num)
    except (FileNotFoundError, IndexError) as e:
        return {"ok": False, "error": str(e)}

    # 2. 找到首帧和草图
    frame_path = find_frame_for_beat(project_dir, episode_num, beat_num)
    if not frame_path:
        return {"ok": False, "error": f"No frame image found for beat {beat_num}"}

    sketch_path = find_sketch_for_beat(project_dir, episode_num, beat_num)
    if not sketch_path:
        return {"ok": False, "error": f"No sketch image found for beat {beat_num} (needed for comparison)"}

    # 路径安全检查
    resolved_project = project_dir.resolve()
    if not frame_path.resolve().is_relative_to(resolved_project):
        return {"ok": False, "error": "Frame path outside project directory"}
    if not sketch_path.resolve().is_relative_to(resolved_project):
        return {"ok": False, "error": "Sketch path outside project directory"}

    # 3. 读取项目视觉风格
    project_style = ""
    project_config_path = project_dir / "config.json"
    if project_config_path.exists():
        try:
            config_data = json.loads(project_config_path.read_text(encoding="utf-8"))
            project_style = config_data.get("visual_style", "")
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to load config.json for project style: %s", e)

    # 4. 调用验证
    visual_desc = beat.get("visual_description", "")
    verifier = FrameVerifier()
    try:
        result = await verifier.verify_frame(
            str(frame_path),
            str(sketch_path),
            visual_desc,
            project_style,
        )
    except Exception as e:
        logger.error("verify_frame failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}

    # 5. 构建响应数据
    data = {
        **result.model_dump(),
        "beat_number": beat_num,
        "verify_type": "frame",
        "frame_path": frame_path.relative_to(project_dir).as_posix(),
        "sketch_path": sketch_path.relative_to(project_dir).as_posix(),
        "description_used": visual_desc,
    }

    # 6. 格式化可读报告 + 持久化
    data["report_text"] = format_verification_report(result.model_dump(), beat_num, "frame")
    report_path = save_verify_report(project_dir, episode_num, beat_num, "frame", data)
    data["report_path"] = report_path.relative_to(project_dir).as_posix()

    return {"ok": True, "data": data}


# ══════════════════════════════════════════════════════════════════════════════
# T6: 连贯性评估
# ══════════════════════════════════════════════════════════════════════════════


@router.post("/projects/{project}/episodes/{episode_num}/verify/continuity")
async def verify_continuity(
    project: str,
    episode_num: int,
    body: ContinuityRequest = ContinuityRequest(),
    user: dict = Depends(get_api_user),
):
    """T6: 检查相邻 beat 之间的叙事连贯性。"""
    resolved = await _resolve_verification_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    logger.info("verify_continuity: project=%s ep=%d range=%s window=%d", project, episode_num, body.beat_range, body.window_size)

    store = await make_sqlite_store_for_context(resolved.ctx)
    verifier = ContinuityVerifier()
    try:
        data = await verifier.verify_continuity(
            project_dir,
            episode_num,
            beat_range=body.beat_range if body.beat_range else None,
            window_size=body.window_size,
            sqlite_store=store,
        )
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error("verify_continuity failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}

    report_path = save_verify_report(project_dir, episode_num, None, "continuity", data)
    data["report_path"] = report_path.relative_to(project_dir).as_posix()

    return {"ok": True, "data": data}


# ══════════════════════════════════════════════════════════════════════════════
# T7: 相似度检测
# ══════════════════════════════════════════════════════════════════════════════


@router.post("/projects/{project}/episodes/{episode_num}/verify/similarity")
async def verify_similarity(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
):
    """T7: 像素级草图相似度检测（零 LLM 成本）。"""
    resolved = await _resolve_verification_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    logger.info("verify_similarity: project=%s ep=%d", project, episode_num)

    store = await make_sqlite_store_for_context(resolved.ctx)
    try:
        result = await detect_similarity(project_dir, episode_num, sqlite_store=store)
    except Exception as e:
        logger.error("verify_similarity failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}

    data = result.model_dump()
    report_path = save_verify_report(project_dir, episode_num, None, "similarity", data)
    data["report_path"] = report_path.relative_to(project_dir).as_posix()

    return {"ok": True, "data": data}


# ══════════════════════════════════════════════════════════════════════════════
# T8: 全局分镜审片
# ══════════════════════════════════════════════════════════════════════════════


@router.post("/projects/{project}/episodes/{episode_num}/verify/episode-overview")
async def verify_episode_overview(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
):
    """T8: 导演视角全局分镜审片 — 整集草图拼网格图，一次 LLM 调用评估整体表现。"""
    resolved = await _resolve_verification_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    logger.info("verify_episode_overview: project=%s ep=%d", project, episode_num)

    store = await make_sqlite_store_for_context(resolved.ctx)
    reviewer = EpisodeReviewer()
    try:
        data = await reviewer.review_episode(project_dir, episode_num, sqlite_store=store)
    except (FileNotFoundError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error("verify_episode_overview failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}

    data["report_text"] = format_episode_overview_report(data, episode_num)
    report_path = save_verify_report(project_dir, episode_num, None, "episode_overview", data)
    data["report_path"] = report_path.relative_to(project_dir).as_posix()

    return {"ok": True, "data": data}
