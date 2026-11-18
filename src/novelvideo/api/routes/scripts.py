"""剧本生成与 Beat 编辑端点。

2.0 主线以 SQLite/Cognee 为唯一脚本状态源；不再读写 scripts/epXXX_script.json。
"""

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

logger = logging.getLogger("novelvideo.api.scripts")

from novelvideo.api.auth import get_api_user
from novelvideo.api.deps import (
    make_cognee_store,
    make_cognee_store_for_context,
    make_sqlite_store_for_context,
    make_sqlite_store,
    resolve_project_scope,
)
from novelvideo.api.schemas import (
    BeatUpdate,
    Seedance2PromptGenerateRequest,
    ScriptGenerateRequest,
    ScriptSaveRequest,
)
from novelvideo.models import sync_beat_asset_refs
from novelvideo.ports import get_task_backend, get_usage_meter
from novelvideo.skills.cinematic_storyboard_skill import (
    load_prev_episode_last_beat_from_store,
)
from novelvideo.task_identity import project_task_state_key
from novelvideo.utils.source_language import detect_episode_asset_language

router = APIRouter()

SEEDANCE2_PROMPT_FEATURE_KEY = "mainline.seedance2_prompt"
SEEDANCE2_PROMPT_TASK_TYPE = "seedance2_prompt"
MODEL_CALL_CREDIT_POLICY_FEATURE_INCLUDED = "feature_included"


def _requester_user_id_for_billing(resolved: Any, user: dict) -> str:
    ctx = getattr(resolved, "ctx", None)
    return str(
        getattr(ctx, "requester_user_id", "")
        or user.get("id")
        or user.get("user_id")
        or user.get("username")
        or ""
    )


async def _audio_duration_seconds(output_dir: str | Path, episode: int, beat_num: int):
    """Duration of one beat's audio, or ``None`` if there isn't one to report.

    ``None`` already means "no duration for this beat" here (the file may simply
    not have been generated yet), so a probe that fails or times out reports the
    same thing rather than failing the whole beat list. On network storage an
    unreadable file is a routine condition, not a request-level error.
    """
    from novelvideo.utils.media_io import get_audio_duration_async
    from novelvideo.utils.path_resolver import PathResolver

    audio_path = PathResolver(str(output_dir), episode).audio(beat_num)
    if not audio_path.exists():
        return None
    try:
        return await get_audio_duration_async(str(audio_path))
    except Exception:
        return None


@router.get("/projects/{project}/episodes/{episode_num}/script")
async def get_script(project: str, episode_num: int, user: dict = Depends(get_api_user)):
    """获取指定集数的剧本。"""
    resolved = await resolve_project_scope(project, user, required_role="viewer")

    try:
        store = (
            await make_sqlite_store_for_context(resolved.ctx)
            if resolved.ctx
            else await make_sqlite_store(resolved.username, resolved.project_name)
        )
        script_data = await store.get_script_as_dict(episode_num)
        if script_data:
            raw_meta = script_data.get("s9_preview_meta") or {}
            if isinstance(raw_meta, dict) and raw_meta:
                from novelvideo.s9_storyboard import compute_s9_manifest_with_live_status

                ep_obj = store.get_episode(episode_num)
                chars = list(store.get_all_characters() or [])
                if not chars and hasattr(store, "list_characters"):
                    chars = list(await store.list_characters() or [])
                script_data["s9_preview_meta"] = compute_s9_manifest_with_live_status(
                    raw_meta,
                    output_dir=resolved.output_dir,
                    characters=chars,
                    episode=ep_obj,
                )
            return {"ok": True, "data": script_data}
    except Exception as exc:
        logger.exception("从 store 读取剧本失败: episode=%s", episode_num)
        raise HTTPException(status_code=500, detail=f"Script store read failed: {exc}") from exc

    return {"ok": True, "data": None, "message": "Script not generated yet"}


@router.post("/projects/{project}/episodes/{episode_num}/script/generate")
async def generate_script(
    project: str,
    episode_num: int,
    body: ScriptGenerateRequest | None = None,
    user: dict = Depends(get_api_user),
):
    """生成指定集数的剧本。"""
    logger.info("[%s] EP%d generate_script", project, episode_num)
    resolved = await resolve_project_scope(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    output_dir = resolved.output_dir
    store = (
        await make_sqlite_store_for_context(ctx)
        if ctx
        else await make_sqlite_store(username, project_name)
    )
    episode = store.get_episode(episode_num)
    if not getattr(episode, "identity_ids", None):
        return {
            "ok": False,
            "code": "identity_plan_required",
            "error": f"第 {episode_num} 集尚未规划角色身份，请先规划身份",
        }

    config: dict[str, Any] = {
        "max_segment_seconds": int(getattr(body, "max_segment_seconds", 30) or 30),
        "tail_anchor": bool(getattr(body, "tail_anchor", False)),
        "generation_mode": str(getattr(body, "generation_mode", "s9") or "s9"),
    }

    if ctx is not None:
        queued = await get_task_backend().enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="script_writer",
            queue_kind="default",
            episode=episode_num,
            payload={"episode": episode_num, "config": config, "output_dir": output_dir},
        )
        return {
            "ok": True,
            "task_type": "script_writer",
            "task_id": queued.task_state.task_id,
            "task_key": project_task_state_key("script_writer", ctx.project_id, episode_num),
            "backend": queued.backend,
            "queue": queued.queue,
            "message": f"第 {episode_num} 集剧本生成任务已进入队列",
        }

    return {"ok": False, "error": "剧本生成需要 project context"}


@router.patch("/projects/{project}/episodes/{episode_num}/beats/{beat_num}")
async def update_beat(
    project: str,
    episode_num: int,
    beat_num: int,
    body: BeatUpdate,
    user: dict = Depends(get_api_user),
):
    """编辑指定 Beat。"""
    resolved = await resolve_project_scope(project, user, required_role="editor")
    logger.info(
        "[%s] EP%d update_beat: beat=%d, fields=%s",
        project,
        episode_num,
        beat_num,
        list(body.model_dump(exclude_none=True).keys()),
    )
    store = (
        await make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await make_sqlite_store(resolved.username, resolved.project_name)
    )
    script_data = await store.get_script_as_dict(episode_num)
    if not script_data:
        raise HTTPException(status_code=404, detail="Script not found")
    beats = list(script_data.get("beats") or [])
    target = next((beat for beat in beats if int(beat.get("beat_number") or 0) == beat_num), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Beat {beat_num} not found")

    updates = body.model_dump(exclude_none=True)
    if "s9_subshots" in updates or "planned_duration_seconds" in updates or "segment_title" in updates:
        import json

        from novelvideo.models import build_scene_ref
        from novelvideo.s9_storyboard import (
            S9SubShot,
            compute_segment_subshots_update,
            parse_s9_segment_meta,
            parse_s9_subshots,
        )
        from novelvideo.seedance2_i2v.models import dump_seedance2_config, parse_seedance2_config

        existing_meta = parse_s9_segment_meta(target.get("s9_segment_meta_json") or target.get("s9_segment_meta"))
        raw_subshots = updates.pop("s9_subshots", None)
        subshots = (
            [S9SubShot.model_validate(item) for item in raw_subshots]
            if raw_subshots is not None
            else parse_s9_subshots(target.get("s9_subshots_json") or target.get("s9_subshots"))
        )
        if "s9_segment_meta" in updates and isinstance(updates["s9_segment_meta"], dict):
            existing_meta = parse_s9_segment_meta(updates.pop("s9_segment_meta")) or existing_meta
        recomputed = compute_segment_subshots_update(
            subshots=subshots,
            existing_meta=existing_meta,
            segment_title=updates.pop("segment_title", None),
            planned_duration_seconds=updates.pop("planned_duration_seconds", None),
        )
        updates["segment_title"] = recomputed["segment_title"]
        updates["s9_subshots_json"] = recomputed["s9_subshots_json"]
        updates["s9_segment_meta_json"] = recomputed["s9_segment_meta_json"]
        updates["audio_type"] = recomputed["audio_type"]
        updates["speaker"] = recomputed["speaker"]
        updates["narration_segment"] = recomputed["narration_segment"]
        if recomputed["visual_description"]:
            updates["visual_description"] = recomputed["visual_description"]
        updates["detected_identities"] = recomputed["detected_identities"]
        updates["detected_props"] = recomputed["detected_props"]
        if recomputed["scene_id"]:
            updates["scene_ref"] = build_scene_ref(
                recomputed["scene_id"], recomputed["scene_variant_id"]
            ).model_dump()
        updates["duration_seconds"] = recomputed["duration_seconds"]
        updates["estimated_duration"] = recomputed["estimated_duration"]

        # Keep seedance2_config duration aligned when user hasn't manually pinned another number
        sd_cfg = parse_seedance2_config(target.get("seedance2_config_json"))
        if not sd_cfg.duration_user_set:
            sd_cfg.duration = int(recomputed["planned_duration_seconds"])
            updates["seedance2_config_json"] = dump_seedance2_config(sd_cfg)

        target["s9_subshots"] = json.loads(recomputed["s9_subshots_json"])
        target["s9_segment_meta"] = json.loads(recomputed["s9_segment_meta_json"])

    for key, value in updates.items():
        target[key] = value
    sync_beat_asset_refs(target)

    try:
        saved = await store.update_beat_asset(
            episode_number=episode_num,
            beat_number=beat_num,
            **{
                k: v
                for k, v in updates.items()
                if k
                in (
                    "narration_segment",
                    "visual_description",
                    "audio_type",
                    "speaker",
                    "detected_identities",
                    "detected_props",
                    "scene_ref",
                    "time_of_day",
                    "video_mode",
                    "video_prompt",
                    "keyframe_prompt",
                    "seedance2_config_json",
                    "segment_title",
                    "s9_subshots_json",
                    "s9_segment_meta_json",
                    "duration_seconds",
                    "estimated_duration",
                )
            },
        )
        if not saved:
            raise RuntimeError(f"Beat {beat_num} was not updated")
    except Exception as exc:
        logger.exception(
            "Beat 保存失败: episode=%s beat=%s", episode_num, beat_num
        )
        raise HTTPException(status_code=500, detail=f"Beat store update failed: {exc}") from exc

    return {"ok": True, "data": target}


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/seedance2-prompt/generate")
async def generate_seedance2_prompt(
    project: str,
    episode_num: int,
    beat_num: int,
    body: Seedance2PromptGenerateRequest | None = None,
    user: dict = Depends(get_api_user),
):
    """AI 生成单个 Beat 的 Seedance2 final_prompt 并保存回配置 JSON。"""
    resolved = await resolve_project_scope(project, user, required_role="editor")
    project_dir = resolved.project_dir
    body = body or Seedance2PromptGenerateRequest()

    store = (
        await make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await make_sqlite_store(resolved.username, resolved.project_name)
    )
    script_data = await store.get_script_as_dict(episode_num)
    if not script_data:
        raise HTTPException(status_code=404, detail="Script not found")

    beats = list(script_data.get("beats") or [])
    target = next((beat for beat in beats if int(beat.get("beat_number") or 0) == beat_num), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Beat {beat_num} not found")

    prev_beat = next(
        (beat for beat in beats if int(beat.get("beat_number") or 0) == beat_num - 1),
        None,
    )
    if prev_beat is None and episode_num > 1 and beat_num <= 1:
        prev_beat, _ = await load_prev_episode_last_beat_from_store(store, episode_num)
    next_beat = next(
        (beat for beat in beats if int(beat.get("beat_number") or 0) == beat_num + 1),
        None,
    )
    language = await detect_episode_asset_language(
        store,
        episode_num,
        fallback_text="\n".join(
            str(target.get(field) or "")
            for field in ("narration_segment", "dialogue", "visual_description")
        ),
    )

    from novelvideo.seedance2_i2v.models import (
        default_seedance2_ratio_for_state_dir,
        parse_seedance2_config,
    )

    default_ratio = default_seedance2_ratio_for_state_dir(Path(resolved.state_dir))
    config = parse_seedance2_config(target.get("seedance2_config_json"), default_ratio=default_ratio)
    if getattr(body, "s9_tail_anchor", None) is not None or getattr(body, "s9_include_first_frame", None) is not None:
        from novelvideo.seedance2_i2v.models import dump_seedance2_config

        if getattr(body, "s9_tail_anchor", None) is not None:
            config.s9_tail_anchor = bool(body.s9_tail_anchor)
        if getattr(body, "s9_include_first_frame", None) is not None:
            config.s9_include_first_frame = bool(body.s9_include_first_frame)
        target["seedance2_config_json"] = dump_seedance2_config(config)
    mode = getattr(config.mode, "value", str(config.mode))
    if mode == "first_last_frame" and next_beat is None:
        return {"ok": False, "error": "这是最后一个 Beat，无法使用首尾帧模式"}

    usage_meter = get_usage_meter()
    ctx = getattr(resolved, "ctx", None)
    project_id = str(getattr(ctx, "project_id", "") or "")
    reservation = await usage_meter.reserve_feature_start_credits(
        user_id=_requester_user_id_for_billing(resolved, user),
        feature_key=SEEDANCE2_PROMPT_FEATURE_KEY,
        product_surface="mainline",
        project_id=project_id,
        resource_kind="script",
        task_type=SEEDANCE2_PROMPT_TASK_TYPE,
        metadata={
            "source": "sync_api",
            "endpoint": "generate_seedance2_prompt",
            "episode": episode_num,
            "beat_num": beat_num,
            "mode": mode,
        },
        require_price_rule=True,
        require_positive_cost=True,
    )
    reservation_id = str(reservation.get("id") or "")
    billing_metadata: dict[str, Any] = {
        "model_call_credit_policy": MODEL_CALL_CREDIT_POLICY_FEATURE_INCLUDED,
        "feature_key": SEEDANCE2_PROMPT_FEATURE_KEY,
        "source": "sync_api",
    }
    if reservation_id:
        billing_metadata.update(
            {
                "feature_credit_reservation_id": reservation_id,
                "feature_credit_charge_id": reservation_id,
                "feature_credit_cost": str(reservation.get("cost") or 0),
            }
        )

    try:
        usage_meter.set_llm_usage_context(
            _requester_user_id_for_billing(resolved, user),
            project_id=project_id,
            resource_kind="script",
            billing_metadata=billing_metadata,
        )
        from novelvideo.seedance2_i2v.panel_service import generate_seedance2_prompt_for_panel

        saved_json = await generate_seedance2_prompt_for_panel(
            store=store,
            state_dir=Path(resolved.state_dir),
            episode=episode_num,
            beat=target,
            project_dir=project_dir,
            prev_beat=prev_beat,
            next_beat=next_beat,
            manual_prompt_reference=body.manual_prompt_reference,
            prompt_guidance=body.prompt_guidance,
            prompt_guidance_template_keys=body.prompt_guidance_template_keys,
            prompt_style=body.prompt_style,
            video_backend=body.video_backend,
            language=language,
            prop_menu=list(script_data.get("prop_menu") or []),
        )
    except ValueError as exc:
        if reservation_id:
            try:
                await usage_meter.settle_cancelled_feature_credit_reservation(
                    reservation_id,
                    metadata={
                        "source": "sync_api",
                        "endpoint": "generate_seedance2_prompt",
                        "episode": episode_num,
                        "beat_num": beat_num,
                        "error": str(exc),
                    },
                )
            except Exception:
                logger.exception(
                    "Failed to settle interrupted Seedance2 prompt reservation"
                )
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        if reservation_id:
            try:
                await usage_meter.settle_cancelled_feature_credit_reservation(
                    reservation_id,
                    metadata={
                        "source": "sync_api",
                        "endpoint": "generate_seedance2_prompt",
                        "episode": episode_num,
                        "beat_num": beat_num,
                        "error": str(exc),
                    },
                )
            except Exception:
                logger.exception(
                    "Failed to settle interrupted Seedance2 prompt reservation"
                )
        raise
    finally:
        usage_meter.clear_llm_usage_context()

    try:
        target["seedance2_config_json"] = saved_json
        sync_beat_asset_refs(target)
        updated_config = parse_seedance2_config(saved_json, default_ratio=default_ratio)
    except Exception as exc:
        if reservation_id:
            try:
                await usage_meter.settle_cancelled_feature_credit_reservation(
                    reservation_id,
                    metadata={
                        "source": "sync_api",
                        "endpoint": "generate_seedance2_prompt",
                        "episode": episode_num,
                        "beat_num": beat_num,
                        "error": str(exc),
                    },
                )
            except Exception:
                logger.exception(
                    "Failed to settle interrupted Seedance2 prompt reservation"
                )
        raise

    if reservation_id:
        try:
            await usage_meter.settle_feature_credit_reservation(
                reservation_id,
                action="confirm",
                metadata={
                    "source": "sync_api",
                    "endpoint": "generate_seedance2_prompt",
                    "episode": episode_num,
                    "beat_num": beat_num,
                    "mode": mode,
                },
            )
        except Exception:
            logger.exception(
                "Seedance2 prompt succeeded but credit confirmation remains pending"
            )

    return {
        "ok": True,
        "data": {
            "beat": target,
            "seedance2_config_json": saved_json,
            "final_prompt": updated_config.final_prompt,
            "prompt_source": updated_config.prompt_source,
            "duration": updated_config.duration,
            "ratio": updated_config.ratio,
        },
    }


@router.put("/projects/{project}/episodes/{episode_num}/script")
async def save_script(
    project: str,
    episode_num: int,
    body: ScriptSaveRequest,
    user: dict = Depends(get_api_user),
):
    """保存（覆盖）指定集数的完整剧本。"""
    resolved = await resolve_project_scope(project, user, required_role="editor")
    logger.info("[%s] EP%d save_script: %d beats", project, episode_num, len(body.beats))

    store = (
        await make_cognee_store_for_context(resolved.ctx)
        if resolved.ctx
        else await make_cognee_store(resolved.username, resolved.project_name)
    )
    await store.load_graph_state()

    normalized_beats = []
    for beat in body.beats:
        beat_payload = dict(beat)
        sync_beat_asset_refs(beat_payload)
        normalized_beats.append(beat_payload)

    try:
        await store.persist_beats_from_script(episode_num, normalized_beats)
    except Exception as e:
        logger.exception("完整脚本保存后回写图谱失败: episode=%s", episode_num)
        raise HTTPException(
            status_code=500,
            detail=f"Script store sync failed: {e}",
        )

    return {
        "ok": True,
        "data": {
            "episode": episode_num,
            "beats_count": len(body.beats),
        },
    }
