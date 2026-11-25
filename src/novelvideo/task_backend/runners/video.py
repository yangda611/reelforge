"""Celery runners for video-generation tasks."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from novelvideo.project_context import ProjectContext
from novelvideo.task_backend.cancel import (
    DEFAULT_PROJECT_TASK_TIMEOUT_SECONDS,
    TaskTimedOut,
    await_envelope_with_cancel_watch,
    raise_if_envelope_cancel_requested,
    remaining_timeout_seconds,
)
from novelvideo.task_backend.registry import register_project_task_runner
from novelvideo.task_backend.subprocesses import run_project_subprocess
from novelvideo.task_identity import project_task_state_key
from novelvideo.task_state import get_task_manager

logger = logging.getLogger(__name__)


def _log(manager, ctx: ProjectContext, envelope: dict[str, Any], message: str) -> None:
    manager.update_progress_for_project(
        ctx,
        str(envelope["task_type"]),
        int(envelope.get("episode") or 0),
        beat_num=envelope.get("beat_num"),
        scope=envelope.get("scope"),
        current_task=message,
        logs=[message],
    )


def _resolve_video_aspect_ratio(value: object, frame_path: object) -> str:
    """Use the provider's adaptive mode when I2V should follow its first frame.

    Legacy Seedance 1.x tasks historically omitted ``ratio`` and were forced to
    9:16.  Seedance does not expose a fixed 2:3 output preset, but its I2V API
    supports ``adaptive`` specifically for following the supplied first frame.
    """
    requested = str(value or "").strip()
    if requested and requested.lower() != "auto":
        return requested
    return "adaptive" if str(frame_path or "").strip() else "9:16"


def _append_freezone_video_node_history(
    *,
    ctx: ProjectContext,
    project_dir: Path,
    payload: dict[str, Any],
    job_id: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any] | None:
    node_id = str(payload.get("node_id") or "").strip()
    if not node_id:
        return None

    from novelvideo.freezone.history import (
        append_generation_history,
        build_node_history_record,
    )

    extra: dict[str, Any] = {}
    if payload.get("model_id"):
        extra["model"] = str(payload["model_id"])
    history_mode = payload.get("requested_gen_mode") or payload.get("gen_mode")
    if history_mode:
        extra["gen_mode"] = str(history_mode)
    for key in ("generation_attempt_id", "product_operation_id"):
        if payload.get(key):
            extra[key] = str(payload[key])

    record = build_node_history_record(
        task_type="freezone_video_gen",
        job_id=job_id,
        task_key=project_task_state_key(
            "freezone_video_gen", ctx.project_id, 0, scope=job_id
        ),
        status="failed" if error else "completed",
        media_type="video",
        result=result,
        error=error,
        prompt=payload.get("prompt"),
        extra=extra or None,
    )

    return append_generation_history(
        project_dir=project_dir,
        canvas_id=str(payload.get("canvas_id") or "default"),
        node_id=node_id,
        record=record,
    )


async def _run_single_video_async(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any]:
    task_type = "single_video"
    episode = int(envelope.get("episode") or 0)
    beat_num = int(envelope.get("beat_num") or 0)
    payload = envelope.get("payload") or {}
    config = dict(payload.get("config") or {})
    output_dir = str(payload.get("output_dir") or ctx.output_dir)

    manager = get_task_manager()
    _log(manager, ctx, envelope, f"开始生成 Beat {beat_num} 视频")

    from novelvideo.generators.video_generator import (
        ShotReference,
        create_video_generator,
    )
    from novelvideo.seedance2_i2v.pipeline import is_huimeng_seedance2_backend
    from novelvideo.utils.path_resolver import PathResolver

    beat = config.get("beat", {})
    frame_path = config.get("frame_path")
    video_mode = config.get("video_mode", "first_frame")
    prompt = config.get("prompt", "")
    video_duration = config.get("video_duration", 5.0)
    backend_str = config.get("video_backend", "comfyui")
    last_frame_path = config.get("last_frame_path")
    seedance2_config = config.get("seedance2_config") or beat.get(
        "seedance2_config_json"
    )
    is_seedance2_backend = is_huimeng_seedance2_backend(backend_str)

    paths = PathResolver(output_dir, episode)
    videos_dir = paths.videos_dir()
    videos_dir.mkdir(parents=True, exist_ok=True)
    video_path = paths.video(beat_num)
    gen_kwargs: dict[str, Any] = {}
    egress_context = envelope.get("__trusted_egress_context")
    if egress_context is not None:
        gen_kwargs["egress_context"] = egress_context
    # 非 seedance2 后端（含 seedance-1.5-pro）的清晰度走构造参数透传；
    # seedance2 的清晰度在 prepare 阶段并入 seedance2_config，无需在此重复。
    single_resolution = config.get("resolution")
    if single_resolution and not is_seedance2_backend:
        gen_kwargs["resolution"] = str(single_resolution)
    if "generate_audio" in config and not is_seedance2_backend:
        gen_kwargs["generate_audio"] = bool(config["generate_audio"])
    video_gen = create_video_generator(backend=backend_str, **gen_kwargs)

    def on_log(msg: str) -> None:
        _log(manager, ctx, envelope, msg)

    def on_progress(value: float) -> None:
        manager.update_progress_for_project(
            ctx,
            task_type,
            episode,
            beat_num=beat_num,
            progress=value,
            current_task=f"生成 Beat {beat_num} 视频",
        )

    if (
        video_mode == "keyframe"
        and last_frame_path
        and not is_seedance2_backend
        and not config.get("gen_mode")
    ):
        video_duration = 5.0

    seedance2_references = []
    if is_seedance2_backend:
        from novelvideo.seedance2_i2v.models import Seedance2I2VMode
        from novelvideo.seedance2_i2v.pipeline import (
            prepare_seedance2_generation_inputs,
        )

        prepared = await prepare_seedance2_generation_inputs(
            project_output=output_dir,
            state_dir=ctx.state_dir,
            episode=episode,
            beat={**beat, "seedance2_config_json": seedance2_config or "{}"},
            next_beat=config.get("next_beat"),
            video_mode=video_mode,
            prompt=prompt,
            duration=video_duration,
            resolution=(
                str(config["resolution"])
                if config.get("resolution") is not None
                else None
            ),
            ratio=str(config["ratio"]) if config.get("ratio") is not None else None,
            prop_menu=config.get("prop_menu"),
            video_backend=backend_str,
        )
        prompt = prepared.prompt
        video_duration = prepared.duration
        frame_path = prepared.image_path
        last_frame_path = prepared.last_frame_path
        seedance2_config = prepared.seedance2_config_json
        seedance2_references = prepared.references
        video_mode = (
            "keyframe"
            if prepared.mode == Seedance2I2VMode.FIRST_LAST_FRAME
            else "first_frame"
        )

    model_references = seedance2_references
    if not is_seedance2_backend:
        model_references = [
            ShotReference(
                str(item.get("type") or "image"),
                str(item.get("path") or ""),
                str(item.get("role") or ""),
            )
            for item in config.get("references") or []
            if isinstance(item, dict) and str(item.get("path") or "").strip()
        ]

    generate_kwargs = {
        "image_path": frame_path,
        "prompt": prompt,
        "output_path": video_path.as_posix(),
        "aspect_ratio": _resolve_video_aspect_ratio(config.get("ratio"), frame_path),
        "duration": video_duration,
        "on_log": on_log,
        "on_progress": on_progress,
        "last_frame_path": last_frame_path,
        "project_output_dir": output_dir,
        "episode": episode,
        "beat_num": beat_num,
        "task_type": task_type,
        "scope": str(envelope.get("scope") or "beat"),
    }
    if egress_context is not None:
        generate_kwargs["egress_context"] = egress_context
    if model_references:
        generate_kwargs["references"] = model_references
    if config.get("audio_setting"):
        generate_kwargs["audio_setting"] = str(config["audio_setting"])
    if config.get("gen_mode") and not is_seedance2_backend:
        generate_kwargs["gen_mode"] = str(config["gen_mode"])
    if seedance2_config:
        generate_kwargs["seedance2_config"] = seedance2_config

    result = await video_gen.generate(**generate_kwargs)
    if result.status.value != "done":
        raise RuntimeError(result.error or "视频生成失败")

    video_pool_id = None
    try:
        from novelvideo.generators.video_pool_indexer import add_video_to_pool

        entry = add_video_to_pool(
            videos_ep_dir=videos_dir,
            episode=episode,
            beat_num=beat_num,
            source_video_path=Path(video_path),
            duration=video_duration,
            video_mode=video_mode,
            backend=backend_str,
            prompt=prompt,
        )
        video_pool_id = entry.id
    except Exception as exc:  # noqa: BLE001
        on_log(f"添加到视频池失败 (非致命): {exc}")

    task_result = {
        "video_path": video_path.as_posix(),
        "beat_num": beat_num,
        "video_pool_id": video_pool_id,
    }
    provider_task_id = getattr(result, "provider_task_id", None) or getattr(
        result, "task_id", None
    )
    if provider_task_id:
        task_result["provider_task_id"] = provider_task_id
    if result.last_frame_path:
        task_result["last_frame_path"] = result.last_frame_path
    if result.last_frame_url:
        task_result["last_frame_url"] = result.last_frame_url
    return task_result


def run_single_video(envelope: dict[str, Any], ctx: ProjectContext) -> dict[str, Any]:
    return asyncio.run(
        await_envelope_with_cancel_watch(
            _run_single_video_async(envelope, ctx),
            envelope,
            task_type="single_video",
        )
    )


register_project_task_runner("single_video", run_single_video)


def _audio_duration(
    audio_path: Path, *, timeout_seconds: int | None = 30
) -> float | None:
    if not audio_path.exists():
        return None
    import subprocess

    try:
        result = run_project_subprocess(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TaskTimedOut(timeout_seconds=timeout_seconds) from exc
    try:
        return float(result.stdout.strip())
    except Exception:
        return None


def _video_stream_duration(
    video_path: Path, *, timeout_seconds: int | None = 30
) -> float | None:
    if not video_path.exists():
        return None
    import math
    import subprocess

    try:
        result = run_project_subprocess(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=duration:format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TaskTimedOut(timeout_seconds=timeout_seconds) from exc
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        token = line.strip()
        if not token or token.upper() == "N/A":
            continue
        try:
            value = float(token)
        except ValueError:
            continue
        if math.isfinite(value) and value > 0.0:
            return value
    return None


def _video_has_audio_stream(
    video_path: Path, *, timeout_seconds: int | None = 30
) -> bool:
    if not video_path.exists():
        return False
    import subprocess

    try:
        result = run_project_subprocess(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TaskTimedOut(timeout_seconds=timeout_seconds) from exc
    return result.returncode == 0 and bool(result.stdout.strip())


def run_compose_episode(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any]:
    import subprocess
    import tempfile

    from novelvideo.utils.path_resolver import PathResolver

    payload = envelope.get("payload") or {}
    episode = int(envelope.get("episode") or payload.get("episode") or 0)
    output_dir = str(payload.get("output_dir") or ctx.output_dir)
    beats = list(payload.get("beats") or [])
    resolution = str(payload.get("resolution") or "720x1280")
    add_subtitles = bool(payload.get("add_subtitles"))
    manager = get_task_manager()
    paths = PathResolver(output_dir, episode)
    final_dir = Path(output_dir) / "videos" / "episodes"
    final_dir.mkdir(parents=True, exist_ok=True)
    output_path = final_dir / f"ep{episode:03d}_final.mp4"

    def check_cancel() -> None:
        raise_if_envelope_cancel_requested(
            envelope,
            task_type="compose_episode",
            episode=episode,
        )

    def subprocess_timeout(default_seconds: int) -> int | None:
        return remaining_timeout_seconds(envelope, default_seconds=default_seconds)

    def run_checked(cmd: list[str], *, default_timeout_seconds: int):
        try:
            return run_project_subprocess(
                cmd,
                envelope=envelope,
                capture_output=True,
                text=True,
                timeout=subprocess_timeout(default_timeout_seconds),
            )
        except subprocess.TimeoutExpired as exc:
            raise TaskTimedOut(
                timeout_seconds=int(
                    envelope.get("__timeout_seconds")
                    or DEFAULT_PROJECT_TASK_TIMEOUT_SECONDS
                )
            ) from exc

    try:
        target_width, target_height = map(int, resolution.split("x"))
    except Exception:
        target_width, target_height = 720, 1280

    video_clips: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for index, beat in enumerate(beats):
            check_cancel()
            beat_num = int(beat.get("beat_number") or index + 1)
            video_path = paths.video(beat_num)
            audio_path = paths.audio(beat_num)
            if not video_path.exists():
                continue
            clip_path = tmp_dir / f"beat_{beat_num:04d}.mp4"
            manager.update_progress_for_project(
                ctx,
                "compose_episode",
                episode,
                progress=index / max(1, len(beats)),
                current_task=f"合成 Beat {beat_num}...",
            )
            cmd = ["ffmpeg", "-y", "-i", str(video_path)]
            has_embedded_audio = False
            video_duration = _video_stream_duration(
                video_path,
                timeout_seconds=subprocess_timeout(30),
            )
            check_cancel()
            audio_filter = (
                f"asetpts=PTS-STARTPTS,aresample=44100:async=1,apad,atrim=0:{video_duration:.6f}"
                if video_duration is not None
                else "apad"
            )
            duration_args = (
                ["-t", f"{video_duration:.6f}"]
                if video_duration is not None
                else ["-shortest"]
            )
            if audio_path.exists():
                cmd.extend(["-i", str(audio_path)])
                cmd.extend(
                    [
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0",
                        "-vf",
                        "setpts=PTS-STARTPTS",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "fast",
                        "-crf",
                        "23",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
                        "-pix_fmt",
                        "yuv420p",
                        "-af",
                        audio_filter,
                        *duration_args,
                    ]
                )
                manager.update_progress_for_project(
                    ctx,
                    "compose_episode",
                    episode,
                    logs=[f"Beat {beat_num} 使用独立音频: {audio_path.name}"],
                )
            else:
                has_embedded_audio = _video_has_audio_stream(
                    video_path,
                    timeout_seconds=subprocess_timeout(30),
                )
                check_cancel()
            if has_embedded_audio:
                cmd.extend(
                    [
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0",
                        "-vf",
                        "setpts=PTS-STARTPTS",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "fast",
                        "-crf",
                        "23",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
                        "-pix_fmt",
                        "yuv420p",
                        "-af",
                        audio_filter,
                        *duration_args,
                    ]
                )
                manager.update_progress_for_project(
                    ctx,
                    "compose_episode",
                    episode,
                    logs=[f"Beat {beat_num} 使用视频内置音轨"],
                )
            elif not audio_path.exists():
                cmd.extend(
                    [
                        "-f",
                        "lavfi",
                        "-i",
                        "anullsrc=r=44100:cl=stereo",
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0",
                        "-vf",
                        "setpts=PTS-STARTPTS",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "fast",
                        "-crf",
                        "23",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
                        "-pix_fmt",
                        "yuv420p",
                        *duration_args,
                    ]
                )
            cmd.append(str(clip_path))
            result = run_checked(
                cmd, default_timeout_seconds=DEFAULT_PROJECT_TASK_TIMEOUT_SECONDS
            )
            check_cancel()
            if result.returncode == 0:
                video_clips.append(str(clip_path))
            else:
                manager.update_progress_for_project(
                    ctx,
                    "compose_episode",
                    episode,
                    logs=[f"Beat {beat_num} 合成失败: {result.stderr[:500]}"],
                )

        if not video_clips:
            raise RuntimeError("没有可用的视频片段")

        check_cancel()
        cmd = ["ffmpeg", "-y"]
        for clip in video_clips:
            cmd.extend(["-i", clip])
        filter_parts = []
        for index in range(len(video_clips)):
            filter_parts.append(
                f"[{index}:v]scale={target_width}:{target_height}:"
                f"force_original_aspect_ratio=decrease,"
                f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:black,"
                f"setsar=1,setpts=PTS-STARTPTS,format=yuv420p[v{index}]"
            )
            filter_parts.append(
                f"[{index}:a]aresample=44100:async=1,asetpts=PTS-STARTPTS[a{index}]"
            )
        concat_inputs = "".join(
            f"[v{index}][a{index}]" for index in range(len(video_clips))
        )
        filter_parts.append(
            f"{concat_inputs}concat=n={len(video_clips)}:v=1:a=1[outv][outa]"
        )
        cmd.extend(
            [
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                "[outv]",
                "-map",
                "[outa]",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(output_path),
            ]
        )
        result = run_checked(
            cmd, default_timeout_seconds=DEFAULT_PROJECT_TASK_TIMEOUT_SECONDS
        )
        check_cancel()
        if result.returncode != 0:
            raise RuntimeError(f"拼接失败: {result.stderr[:500]}")

    return {
        "video_path": output_path.as_posix(),
        "add_subtitles_requested": add_subtitles,
    }


register_project_task_runner("compose_episode", run_compose_episode)


def _omni_video_edit_hint(payload: dict[str, Any], error: Exception) -> str | None:
    mode = str(payload.get("gen_mode") or "").strip()
    if mode not in {"all_reference", "allReference"}:
        return None
    if not any(
        str(item.get("type") or "").lower() == "video"
        for item in payload.get("reference_items") or []
        if isinstance(item, dict)
    ):
        return None
    message = str(error).lower().replace("`", "")
    if not all(
        marker in message
        for marker in (
            "seedance identified your task as video editing",
            "ratio must be adaptive",
            "duration must be -1",
        )
    ):
        return None
    return (
        "Seedance 将该提示词判定为视频编辑，输出比例和时长需跟随源视频。"
        "请改用「视频编辑」模式，或改写提示词以明确生成新视频。"
        "用于编辑的源视频需为 4–30 秒。"
    )


async def _run_freezone_video_gen_async(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any]:
    from novelvideo.api.deps import make_static_url_for_context
    from novelvideo.freezone.jobs import ensure_freezone_dirs, run_freezone_video_gen

    payload = envelope.get("payload") or {}
    job_id = str(payload["job_id"])
    project_dir = Path(str(payload.get("project_dir") or ctx.output_dir))
    ensure_freezone_dirs(project_dir)

    manager = get_task_manager()
    manager.update_progress_for_project(
        ctx,
        "freezone_video_gen",
        0,
        scope=job_id,
        progress=0.1,
        current_task="调用视频生成器...",
        logs=["开始 freezone 视频生成"],
    )

    trusted_context = envelope.get("__trusted_egress_context")
    video_leaf = run_freezone_video_gen
    trusted_kwargs: dict[str, Any] = {}
    if trusted_context is not None and trusted_context.billing_principal.kind in {
        "organization",
        "local",
    }:
        from novelvideo.freezone.video_node import run_trusted_freezone_video_gen

        video_leaf = run_trusted_freezone_video_gen
        trusted_kwargs = {
            "egress_context": trusted_context,
            "task_type": str(envelope.get("task_type") or "freezone_video_gen"),
            "episode": int(envelope.get("episode") or 0),
            "beat_num": int(envelope.get("beat_num") or 0),
            "scope": str(envelope.get("scope") or job_id),
        }

    try:
        out_path = await video_leaf(
            project_dir=project_dir,
            job_id=job_id,
            prompt=str(payload.get("prompt") or ""),
            reference_items=payload.get("reference_items") or None,
            aspect_ratio=str(payload.get("aspect_ratio") or "16:9"),
            resolution=str(payload.get("resolution") or "720p"),
            duration_seconds=int(payload.get("duration_seconds") or 5),
            generate_audio=bool(payload.get("generate_audio")),
            human_review=bool(payload.get("human_review")),
            scene_optimize=str(payload.get("scene_optimize") or ""),
            backend=str(payload.get("backend") or ""),
            last_frame_path=payload.get("last_frame_path"),
            audio_setting=payload.get("audio_setting") or None,
            gen_mode=payload.get("gen_mode") or None,
            model_params=payload.get("model_params") or None,
            request_schema=payload.get("request_schema") or None,
            **trusted_kwargs,
        )
    except Exception as exc:
        user_hint = _omni_video_edit_hint(payload, exc)
        _append_freezone_video_node_history(
            ctx=ctx,
            project_dir=project_dir,
            payload=payload,
            job_id=job_id,
            error=user_hint or str(exc),
        )
        if user_hint:
            raise RuntimeError(user_hint) from exc
        raise

    rel = out_path.relative_to(project_dir).as_posix()
    result = {
        "job_id": job_id,
        "output_path": str(out_path),
        "output_url": make_static_url_for_context(ctx, rel),
    }
    if bool(payload.get("image_animate_gif")):
        from novelvideo.task_backend.client import enqueue_project_task

        gif_job_id = f"{job_id}-gif"
        try:
            queued = await enqueue_project_task(
                ctx,
                task_type="freezone_image_animate_gif",
                product_surface="freezone",
                queue_kind="ffmpeg",
                episode=0,
                scope=gif_job_id,
                payload={
                    "job_id": gif_job_id,
                    "project_dir": str(project_dir),
                    "video_path": str(out_path),
                },
            )
            result.update(
                {
                    "gif_task_type": "freezone_image_animate_gif",
                    "gif_job_id": gif_job_id,
                    "gif_task_key": project_task_state_key(
                        "freezone_image_animate_gif", ctx.project_id, 0, scope=gif_job_id
                    ),
                    "gif_queue": queued.queue,
                }
            )
        except Exception as exc:
            # Preserve the paid video so the canvas can retry only local conversion.
            logger.warning(
                "automatic GIF dispatch failed project=%s video_job=%s: %s",
                ctx.project_id,
                job_id,
                exc,
            )
            result["gif_enqueue_error"] = "GIF 转换任务未能启动，请重试转换。"
    history_record = _append_freezone_video_node_history(
        ctx=ctx,
        project_dir=project_dir,
        payload=payload,
        job_id=job_id,
        result=result,
    )
    if history_record:
        result["generation_history_record"] = history_record
    return result


def run_freezone_video_gen(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any]:
    return asyncio.run(
        await_envelope_with_cancel_watch(
            _run_freezone_video_gen_async(envelope, ctx),
            envelope,
            task_type="freezone_video_gen",
        )
    )


register_project_task_runner(
    "freezone_video_gen", run_freezone_video_gen, requires_home_node=False
)
