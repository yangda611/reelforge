"""Mainline episode two-stage autopilot REST API endpoints."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from novelvideo.api.auth import get_api_user
from novelvideo.api.deps import resolve_project_scope
from novelvideo.i18n_message import lmsg, message_text
from novelvideo.services.episode_autopilot import (
    ACTIVE_AUTOPILOT_STATUSES,
    AutopilotOptions,
    episode_autopilot_coordinator,
)

router = APIRouter()


def _response_message(code: str, fallback: str, **params: Any) -> dict[str, Any]:
    msg = lmsg(code, fallback, **params)
    result: dict[str, Any] = {
        "message": message_text(msg),
        "message_code": msg.code,
    }
    if msg.params:
        result["message_params"] = dict(msg.params)
    return result


class AutopilotStartRequest(BaseModel):
    mode: Literal["two_stage", "full_auto"] = "two_stage"
    start_phase: Literal["phase1", "phase2"] = "phase1"
    video_backend: Optional[str] = None
    resolution: Optional[str] = None
    ratio: Optional[str] = None
    tail_anchor: bool = False
    max_segment_seconds: int = Field(default=30, ge=5, le=60)
    max_retries: int = Field(default=2, ge=0, le=5)
    max_concurrent_videos: int = Field(default=4, ge=1, le=10)
    max_concurrent_assets: int = Field(default=6, ge=1, le=10)
    add_subtitles: bool = False
    add_bgm: bool = False
    use_director_render: bool = False
    force_regenerate_videos: bool = False


class AutopilotBatchStartRequest(AutopilotStartRequest):
    episodes: list[int] = Field(min_length=1, max_length=200)


class AutopilotContinueRequest(BaseModel):
    mode: Optional[Literal["two_stage", "full_auto"]] = None
    video_backend: Optional[str] = None
    resolution: Optional[str] = None
    ratio: Optional[str] = None
    tail_anchor: Optional[bool] = None
    max_segment_seconds: Optional[int] = Field(default=None, ge=5, le=60)
    max_retries: Optional[int] = Field(default=None, ge=0, le=5)
    max_concurrent_videos: Optional[int] = Field(default=None, ge=1, le=10)
    add_subtitles: Optional[bool] = None
    add_bgm: Optional[bool] = None
    use_director_render: Optional[bool] = None
    force_regenerate_videos: Optional[bool] = None


class AutopilotCancelRequest(BaseModel):
    cancel_episode_tasks: bool = True


def _build_options(body: AutopilotStartRequest) -> AutopilotOptions:
    return AutopilotOptions(
        mode=body.mode,
        video_backend=body.video_backend,
        resolution=body.resolution,
        ratio=body.ratio,
        tail_anchor=body.tail_anchor,
        max_segment_seconds=body.max_segment_seconds,
        max_retries=body.max_retries,
        max_concurrent_videos=body.max_concurrent_videos,
        max_concurrent_assets=body.max_concurrent_assets,
        add_subtitles=body.add_subtitles,
        add_bgm=body.add_bgm,
        use_director_render=body.use_director_render,
        force_regenerate_videos=body.force_regenerate_videos,
    )


@router.get("/projects/{project}/autopilot/runs")
async def list_project_autopilot_runs(
    project: str,
    user: dict = Depends(get_api_user),
):
    """列出当前项目各集的两段式托管状态（严格项目级隔离）。"""
    resolved = await resolve_project_scope(project, user, required_role="viewer")
    if resolved.ctx is None:
        return {"ok": True, "data": {"runs": [], "summary": {}}}
    runs = episode_autopilot_coordinator.list_runs(resolved.ctx)
    items = [run.to_dict() for run in runs]
    summary = {
        "total": len(runs),
        "running": sum(1 for r in runs if r.status in ACTIVE_AUTOPILOT_STATUSES),
        "awaiting_asset_review": sum(
            1 for r in runs if r.status == "awaiting_asset_review"
        ),
        "paused": sum(1 for r in runs if r.status == "paused"),
        "completed": sum(1 for r in runs if r.status == "completed"),
    }
    return {
        "ok": True,
        "data": {
            "project": project,
            "runs": items,
            "summary": summary,
        },
    }


@router.get("/projects/{project}/autopilot/episodes/{episode_num}")
async def get_episode_autopilot_run(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
):
    """获取单集的托管运行状态。"""
    resolved = await resolve_project_scope(project, user, required_role="viewer")
    if resolved.ctx is None:
        return {"ok": True, "data": None}
    run = episode_autopilot_coordinator.get_run(resolved.ctx, episode_num)
    return {"ok": True, "data": run.to_dict() if run is not None else None}


@router.post("/projects/{project}/autopilot/episodes/{episode_num}/start")
async def start_episode_autopilot(
    project: str,
    episode_num: int,
    body: AutopilotStartRequest | None = None,
    user: dict = Depends(get_api_user),
):
    """启动单集两段式半托管（或全自动）流水线。"""
    if episode_num <= 0:
        raise HTTPException(status_code=400, detail="episode_num must be > 0")
    resolved = await resolve_project_scope(project, user, required_role="editor")
    if resolved.ctx is None:
        return {"ok": False, "error": "启动托管流水线需要 project context"}
    req = body or AutopilotStartRequest()
    run = await episode_autopilot_coordinator.start_episode(
        resolved.ctx,
        episode=episode_num,
        options=_build_options(req),
        start_phase=req.start_phase,
    )
    return {
        "ok": True,
        "data": run.to_dict(),
        **_response_message(
            "episode.autopilot.api.started",
            f"第 {episode_num} 集两段式托管已启动",
            episode=episode_num,
        ),
    }


@router.post("/projects/{project}/autopilot/batch-start")
async def batch_start_episode_autopilot(
    project: str,
    body: AutopilotBatchStartRequest,
    user: dict = Depends(get_api_user),
):
    """并发启动多集两段式托管（每集独立状态机与任务隔离）。"""
    resolved = await resolve_project_scope(project, user, required_role="editor")
    if resolved.ctx is None:
        return {"ok": False, "error": "批量启动托管流水线需要 project context"}
    valid_episodes = [int(ep) for ep in body.episodes if int(ep) > 0]
    if not valid_episodes:
        raise HTTPException(status_code=400, detail="episodes must contain positive integers")
    runs = await episode_autopilot_coordinator.batch_start(
        resolved.ctx,
        episodes=valid_episodes,
        options=_build_options(body),
        start_phase=body.start_phase,
    )
    return {
        "ok": True,
        "data": {
            "runs": [run.to_dict() for run in runs],
            "count": len(runs),
        },
        **_response_message(
            "episode.autopilot.api.batchStarted",
            f"已并发启动 {len(runs)} 集两段式托管",
            count=len(runs),
        ),
    }


@router.post("/projects/{project}/autopilot/episodes/{episode_num}/continue")
async def continue_episode_autopilot(
    project: str,
    episode_num: int,
    body: AutopilotContinueRequest | None = None,
    user: dict = Depends(get_api_user),
):
    """资产验收通过进入第二阶段（成片），或从暂停/失败处继续恢复。"""
    if episode_num <= 0:
        raise HTTPException(status_code=400, detail="episode_num must be > 0")
    resolved = await resolve_project_scope(project, user, required_role="editor")
    if resolved.ctx is None:
        return {"ok": False, "error": "继续托管流水线需要 project context"}
    overrides = body.model_dump(exclude_none=True) if body is not None else None
    run = await episode_autopilot_coordinator.continue_episode(
        resolved.ctx,
        episode=episode_num,
        overrides=overrides,
    )
    return {
        "ok": True,
        "data": run.to_dict(),
        **_response_message(
            "episode.autopilot.api.continued",
            f"第 {episode_num} 集已继续推进",
            episode=episode_num,
        ),
    }


@router.post("/projects/{project}/autopilot/episodes/{episode_num}/pause")
async def pause_episode_autopilot(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
):
    """暂停单集托管（不影响同项目其他集与其他项目）。"""
    resolved = await resolve_project_scope(project, user, required_role="editor")
    if resolved.ctx is None:
        return {"ok": False, "error": "暂停托管流水线需要 project context"}
    run = await episode_autopilot_coordinator.pause_episode(
        resolved.ctx,
        episode=episode_num,
    )
    if run is None:
        return {"ok": False, "error": f"第 {episode_num} 集尚无托管运行记录"}
    return {
        "ok": True,
        "data": run.to_dict(),
        **_response_message(
            "episode.autopilot.api.paused",
            f"第 {episode_num} 集托管已暂停",
            episode=episode_num,
        ),
    }


@router.post("/projects/{project}/autopilot/episodes/{episode_num}/cancel")
async def cancel_episode_autopilot(
    project: str,
    episode_num: int,
    body: AutopilotCancelRequest | None = None,
    user: dict = Depends(get_api_user),
):
    """取消单集托管及本集专属运行中任务（不影响共享资产任务与其他集）。"""
    resolved = await resolve_project_scope(project, user, required_role="editor")
    if resolved.ctx is None:
        return {"ok": False, "error": "取消托管流水线需要 project context"}
    req = body or AutopilotCancelRequest()
    run = await episode_autopilot_coordinator.cancel_episode(
        resolved.ctx,
        episode=episode_num,
        cancel_episode_tasks=req.cancel_episode_tasks,
    )
    if run is None:
        return {"ok": False, "error": f"第 {episode_num} 集尚无托管运行记录"}
    return {
        "ok": True,
        "data": run.to_dict(),
        **_response_message(
            "episode.autopilot.api.cancelled",
            f"第 {episode_num} 集托管已取消",
            episode=episode_num,
        ),
    }
