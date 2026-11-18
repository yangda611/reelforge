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


register_project_task_runner("script_writer", run_script_writer)
