"""Celery runner for screenplay generation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from novelvideo.project_context import ProjectContext
from novelvideo.i18n_message import MessageLike, lmsg
from novelvideo.model_gateway_runtime import model_gateway_scope_for_runner
from novelvideo.task_backend.cancel import await_envelope_with_cancel_watch
from novelvideo.task_backend.registry import register_project_task_runner
from novelvideo.task_state import get_task_manager


def run_script_writer(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any] | None:
    return asyncio.run(
        await_envelope_with_cancel_watch(
            _run_script_writer(envelope, ctx),
            envelope,
            task_type="script_writer",
        )
    )


async def _run_script_writer(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any] | None:
    with model_gateway_scope_for_runner(envelope):
        return await _run_script_writer_scoped(envelope, ctx)


async def _run_script_writer_scoped(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any] | None:
    from novelvideo.cognee import CogneeStore
    from novelvideo.project_config import load_project_config_from_state_dir
    from novelvideo.workflows.script_writing import create_script_writing_workflow

    payload = envelope.get("payload") or {}
    episode = int(envelope.get("episode") or payload.get("episode") or 0)
    config = dict(payload.get("config") or {})
    output_dir = str(payload.get("output_dir") or ctx.output_dir)
    manager = get_task_manager()

    def update_progress(progress: float, task: MessageLike) -> None:
        manager.update_progress_for_project(
            ctx,
            "script_writer",
            episode,
            progress=progress,
            current_task=task,
            logs=[task],
        )

    update_progress(
        0.02,
        lmsg("tasks.progress.script.start", "开始生成脚本..."),
    )

    store = CogneeStore(
        ctx.owner_project_label,
        output_dir=output_dir,
        state_dir=str(ctx.state_dir),
    )
    await store.initialize()
    try:
        project_config = load_project_config_from_state_dir(
            ctx.state_dir,
            username=ctx.owner_username,
            project=ctx.project_name,
        )
        merged_config = {**project_config, **config}
        workflow = create_script_writing_workflow(
            store,
            genre=merged_config.get("genre", ""),
            story_setting=merged_config.get("story_setting", ""),
            spine_template=merged_config.get("spine_template", "drama"),
            output_dir=output_dir,
        )

        import inspect

        run_kwargs: dict[str, Any] = {
            "episode_num": episode,
            "on_progress": update_progress,
            "on_log": lambda message: manager.update_progress_for_project(
                ctx,
                "script_writer",
                episode,
                logs=[message],
            ),
        }
        try:
            sig = inspect.signature(workflow.run)
            accepts_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
        except (TypeError, ValueError):
            accepts_kwargs = False

        if accepts_kwargs or "generation_mode" in getattr(sig, "parameters", {}):
            run_kwargs.update(
                {
                    "generation_mode": str(merged_config.get("generation_mode") or "s9"),
                    "max_segment_seconds": int(
                        merged_config.get("max_segment_seconds") or 30
                    ),
                    "tail_anchor": bool(merged_config.get("tail_anchor", False)),
                    "visual_style": str(
                        merged_config.get("visual_style") or "chinese_period_drama"
                    ),
                    "aspect_ratio": str(merged_config.get("aspect_ratio") or "2:3"),
                    "video_backend": str(merged_config.get("video_backend") or ""),
                    "spine_template": str(merged_config.get("spine_template") or "drama"),
                }
            )

        script = await workflow.run(**run_kwargs)

        refreshed_beats = (
            await store.get_beats_as_dicts(episode)
            if hasattr(store, "get_beats_as_dicts")
            else []
        )
        result = {
            "episode": episode,
            "beats": len(script.beats),
            "beats_data": refreshed_beats or [beat.model_dump() for beat in script.beats],
            "review_passed": workflow.last_review_passed,
            "review_summary": workflow.last_review_summary,
            "degraded_lines": list(
                getattr(workflow, "last_degraded_lines", []) or []
            ),
            "output_dir": str(Path(output_dir)),
        }
        return result
    finally:
        await store.close()


class _LockedStoreProxy:
    """Serialize SQLite store writes across concurrent batch workers."""

    def __init__(self, inner: Any, lock: asyncio.Lock) -> None:
        self._inner = inner
        self._lock = lock

    async def update_beat_asset(self, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            return await self._inner.update_beat_asset(*args, **kwargs)

    async def get_episode_from_graph(self, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            return await self._inner.get_episode_from_graph(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def run_seedance2_prompt_batch(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any] | None:
    return asyncio.run(
        await_envelope_with_cancel_watch(
            _run_seedance2_prompt_batch(envelope, ctx),
            envelope,
            task_type="seedance2_prompt_batch",
        )
    )


async def _run_seedance2_prompt_batch(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any] | None:
    with model_gateway_scope_for_runner(envelope):
        return await _run_seedance2_prompt_batch_scoped(envelope, ctx)


async def _run_seedance2_prompt_batch_scoped(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any] | None:
    from novelvideo.cognee import CogneeStore
    from novelvideo.s9_storyboard import get_s9_llm_concurrency
    from novelvideo.seedance2_i2v.models import (
        default_seedance2_ratio_for_state_dir,
        dump_seedance2_config,
        parse_seedance2_config,
    )
    from novelvideo.seedance2_i2v.panel_service import generate_seedance2_prompt_for_panel
    from novelvideo.skills.cinematic_storyboard_skill import (
        load_prev_episode_last_beat_from_store,
    )
    from novelvideo.utils.source_language import detect_episode_asset_language

    payload = envelope.get("payload") or {}
    episode = int(envelope.get("episode") or payload.get("episode") or 0)
    output_dir = str(payload.get("output_dir") or ctx.output_dir)
    requested_nums = [int(n) for n in (payload.get("beat_numbers") or []) if int(n) > 0]
    only_missing = bool(payload.get("only_missing", False))
    prompt_guidance = str(payload.get("prompt_guidance") or "")
    prompt_guidance_template_keys = list(
        payload.get("prompt_guidance_template_keys") or []
    )
    tail_anchor_override = payload.get("s9_tail_anchor")
    manager = get_task_manager()

    def update_progress(progress: float, task: MessageLike) -> None:
        manager.update_progress_for_project(
            ctx,
            "seedance2_prompt_batch",
            episode,
            progress=progress,
            current_task=task,
            logs=[task],
        )

    update_progress(
        0.05,
        lmsg("tasks.progress.seedance2_batch.start", "准备并发生成本集视频提示词..."),
    )

    store = CogneeStore(
        ctx.owner_project_label,
        output_dir=output_dir,
        state_dir=str(ctx.state_dir),
    )
    await store.initialize()
    try:
        script_data = await store.get_script_as_dict(episode)
        beats = sorted(
            list((script_data or {}).get("beats") or []),
            key=lambda b: int(b.get("beat_number") or 0),
        )
        if not script_data or not beats:
            raise ValueError(f"Episode {episode} script not found")

        default_ratio = default_seedance2_ratio_for_state_dir(Path(ctx.state_dir))
        requested_set = set(requested_nums)
        targets: list[dict[str, Any]] = []
        for beat in beats:
            bn = int(beat.get("beat_number") or 0)
            if bn <= 0:
                continue
            if requested_set and bn not in requested_set:
                continue
            if only_missing:
                cfg = parse_seedance2_config(
                    beat.get("seedance2_config_json"),
                    default_ratio=default_ratio,
                )
                if str(cfg.final_prompt or "").strip():
                    continue
            targets.append(beat)

        if not targets:
            update_progress(
                1.0,
                lmsg("tasks.progress.seedance2_batch.empty", "所有目标分镜均已有视频提示词"),
            )
            return {"episode": episode, "updated": 0, "beat_numbers": []}

        fallback_text = "\n".join(
            str(b.get(field) or "")
            for b in targets
            for field in ("narration_segment", "dialogue", "visual_description")
        )
        language = await detect_episode_asset_language(
            store,
            episode,
            fallback_text=fallback_text,
        )
        prop_menu = list(script_data.get("prop_menu") or [])
        beat_by_num = {int(b.get("beat_number") or 0): b for b in beats}
        prev_ep_last_beat: dict[str, Any] | None = None
        if episode > 1 and any(int(b.get("beat_number") or 0) <= 1 for b in targets):
            prev_ep_last_beat, _ = await load_prev_episode_last_beat_from_store(
                store, episode
            )

        db_lock = asyncio.Lock()
        locked_store = _LockedStoreProxy(store, db_lock)
        progress_lock = asyncio.Lock()
        sem = asyncio.Semaphore(max(1, get_s9_llm_concurrency()))
        total = len(targets)
        completed = 0

        async def _process_one(target: dict[str, Any]) -> int:
            nonlocal completed
            beat_num = int(target.get("beat_number") or 0)
            target_copy = dict(target)
            if tail_anchor_override is not None:
                cfg = parse_seedance2_config(
                    target_copy.get("seedance2_config_json"),
                    default_ratio=default_ratio,
                )
                cfg.s9_tail_anchor = bool(tail_anchor_override)
                target_copy["seedance2_config_json"] = dump_seedance2_config(cfg)

            prev_raw = beat_by_num.get(beat_num - 1)
            if prev_raw is None and episode > 1 and beat_num <= 1:
                prev_raw = prev_ep_last_beat
            next_raw = beat_by_num.get(beat_num + 1)

            async with sem:
                await generate_seedance2_prompt_for_panel(
                    store=locked_store,
                    state_dir=Path(ctx.state_dir),
                    episode=episode,
                    beat=target_copy,
                    project_dir=Path(ctx.output_dir),
                    prev_beat=dict(prev_raw) if prev_raw else None,
                    next_beat=dict(next_raw) if next_raw else None,
                    prompt_guidance=prompt_guidance,
                    prompt_guidance_template_keys=prompt_guidance_template_keys,
                    language=language,
                    prop_menu=prop_menu,
                )
            async with progress_lock:
                completed += 1
                pct = min(0.98, 0.08 + 0.88 * (completed / max(1, total)))
                update_progress(
                    pct,
                    f"已更新 {completed}/{total} 个分镜视频提示词",
                )
            return beat_num

        results = await asyncio.gather(*(_process_one(t) for t in targets))
        update_progress(
            1.0,
            f"第 {episode} 集 {len(results)} 个分镜视频提示词更新完成",
        )
        return {
            "episode": episode,
            "updated": len(results),
            "beat_numbers": sorted(results),
            "output_dir": str(Path(output_dir)),
        }
    finally:
        await store.close()


register_project_task_runner("script_writer", run_script_writer)
register_project_task_runner("seedance2_prompt_batch", run_seedance2_prompt_batch)
