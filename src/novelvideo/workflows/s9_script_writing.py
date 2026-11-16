"""S9 Segment Storyboard Workflow.

Replaces ultra-fine line-by-line beat splitting with coherent s9 segment splits
(capped at 30s or 15s per segment), runs Stage 1 preview + Stage 2 continuous
Seedance 2.0/2.5 prompts in one pass, and persists 1 segment = 1 Beat cleanly.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from novelvideo.models import NarrationScript, VisualBeat, build_scene_ref
from novelvideo.project_config import load_project_config_from_state_dir
from novelvideo.s9_storyboard import (
    DEFAULT_S9_MAX_SEGMENT_SECONDS,
    S9EpisodePreviewMeta,
    S9SegmentAssetSlot,
    S9SegmentMeta,
    S9_PROMPT_SOURCE,
    S9_PROMPT_STYLE,
    aggregate_segment_narration,
    aggregate_segment_visual_summary,
    generate_all_s9_segment_prompts,
    normalize_max_segment_seconds,
    normalize_s9_storyboard_plan,
    resolve_s9_style_context,
    run_s9_stage1_plan,
)
from novelvideo.seedance2_i2v.models import Seedance2I2VMode, Seedance2VideoConfig, dump_seedance2_config
from novelvideo.skills.cinematic_storyboard_skill import (
    load_prev_episode_script_context_from_store,
)
from novelvideo.sqlite_store import load_episode_planning_content
from novelvideo.video_duration import seedance2_media_limits_for_backend

logger = logging.getLogger("novelvideo.s9_script_writing")


class S9ScriptWritingWorkflow:
    """Two-stage s9 segment script writing workflow."""

    def __init__(
        self,
        cognee_store: Any,
        sqlite_store: Any | None = None,
        *,
        output_dir: str = "",
        genre: str = "",
        story_setting: str = "",
        spine_template: str = "drama",
    ) -> None:
        self.cognee_store = cognee_store
        self.sqlite_store = sqlite_store or cognee_store
        self.output_dir = output_dir
        self.genre = genre
        self.story_setting = story_setting
        self.spine_template = spine_template or "drama"
        self.last_review_passed: bool = True
        self.last_review_summary: str = ""
        self.last_review_issues: list[str] = []
        self.last_degraded_lines: list[dict[str, Any]] = []

    def _collect_available_voices(
        self,
        *,
        characters: list[Any],
        output_dir: str,
    ) -> set[str]:
        names: set[str] = set()
        for char in characters or []:
            char_name = str(getattr(char, "name", "") or "").strip()
            has_ref = bool(str(getattr(char, "reference_audio_path", "") or "").strip())
            if has_ref and char_name:
                names.add(char_name)
            for ident in list(getattr(char, "identities", []) or []):
                iid = str(getattr(ident, "identity_id", "") or "").strip()
                has_voice = has_ref or bool(
                    str(getattr(ident, "voice_sample_path", "") or "").strip()
                )
                if not has_voice and output_dir and char_name:
                    try:
                        p = (
                            Path(output_dir)
                            / "assets"
                            / "characters"
                            / char_name
                            / "voices"
                            / "voice_default.mp3"
                        )
                        if p.exists():
                            has_voice = True
                    except Exception:
                        pass
                if has_voice and iid:
                    names.add(iid)
                    if char_name:
                        names.add(char_name)
        return names

    async def run(
        self,
        *,
        episode_num: int,
        source_text: str | None = None,
        max_segment_seconds: int = DEFAULT_S9_MAX_SEGMENT_SECONDS,
        tail_anchor: bool = False,
        visual_style: str | None = None,
        aspect_ratio: str | None = None,
        video_backend: str | None = None,
        spine_template: str | None = None,
        on_progress: Optional[Callable[[float, str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        **_: Any,
    ) -> NarrationScript:
        def report_progress(progress: float, task: str) -> None:
            if on_progress:
                on_progress(progress, task)

        def log(message: str) -> None:
            if on_log:
                on_log(message)

        cap = normalize_max_segment_seconds(max_segment_seconds)
        effective_spine = str(spine_template or self.spine_template or "drama").strip()
        state_dir = getattr(self.sqlite_store, "state_dir", None) or getattr(
            self.cognee_store, "state_dir", None
        )
        proj_cfg = load_project_config_from_state_dir(state_dir) if state_dir else {}
        effective_style = visual_style or str(proj_cfg.get("visual_style") or "chinese_period_drama")
        effective_ratio = aspect_ratio or str(proj_cfg.get("aspect_ratio") or "2:3")
        effective_backend = video_backend or str(proj_cfg.get("video_backend") or "")
        effective_output_dir = self.output_dir or (str(Path(state_dir).parent) if state_dir else "")

        report_progress(0.08, f"s9 分镜：准备第 {episode_num} 集正文与资产清单")
        episode = self.cognee_store.get_episode(episode_num)
        if episode is None and hasattr(self.cognee_store, "get_episode_from_graph"):
            episode = await self.cognee_store.get_episode_from_graph(episode_num)
        if episode is None:
            raise ValueError(f"第 {episode_num} 集尚未初始化")

        if source_text is not None and source_text.strip():
            resolved_text = source_text.strip()
        else:
            resolved_text = (
                await load_episode_planning_content(self.sqlite_store, episode) or ""
            ).strip()
        if not resolved_text:
            raise ValueError(f"第 {episode_num} 集正文为空，请先填写分镜源文本")

        characters = list(self.cognee_store.get_all_characters() or [])
        if not characters and hasattr(self.cognee_store, "list_characters"):
            characters = list(await self.cognee_store.list_characters() or [])

        prev_episode_context = ""
        if episode_num > 1:
            try:
                prev_episode_context, _ = await load_prev_episode_script_context_from_store(
                    self.sqlite_store, episode_num
                )
            except Exception:
                prev_episode_context = ""

        voice_names = self._collect_available_voices(
            characters=characters,
            output_dir=effective_output_dir,
        )
        style_context = resolve_s9_style_context(
            visual_style=effective_style,
            aspect_ratio=effective_ratio,
            state_dir=state_dir,
        )
        limits = seedance2_media_limits_for_backend(effective_backend)
        max_images = int(limits["max_images"])
        max_audios = int(limits["max_audios"])

        log(
            f"开始按 s9 规则拆分第 {episode_num} 集：单段上限 {cap} 秒，"
            f"画风 {style_context.style_label}（{style_context.aspect_ratio}），"
            f"尾帧锚点={'开启' if tail_anchor else '关闭'}"
        )
        report_progress(0.22, f"Stage 1：按 ≤{cap}秒 拆分大段并构建【简易预览版分镜】")
        raw_plan = await run_s9_stage1_plan(
            source_text=resolved_text,
            episode_num=episode_num,
            episode_title=str(getattr(episode, "title", "") or ""),
            style_context=style_context,
            characters=characters,
            episode=episode,
            voice_available_names=voice_names,
            max_segment_seconds=cap,
            spine_template=effective_spine,
            tail_anchor=bool(tail_anchor),
            prev_episode_context=prev_episode_context,
        )
        plan, per_segment_slots = normalize_s9_storyboard_plan(
            raw_plan,
            max_segment_seconds=cap,
            characters=characters,
            episode=episode,
            max_images_per_segment=max_images,
            max_audios_per_segment=max_audios,
        )
        if not plan.segments:
            raise ValueError("s9 Stage 1 未产出有效大段分镜")

        log(
            f"Stage 1 完成：共 {len(plan.segments)} 大段，"
            f"预计成片总长 {plan.duration_assessment.estimated_total_seconds} 秒"
        )
        report_progress(
            0.55,
            f"Stage 2：为 {len(plan.segments)} 个大段生成完整 Seedance2.0 主体提示词",
        )
        stage2_prompts = await generate_all_s9_segment_prompts(
            plan=plan,
            per_segment_slots=per_segment_slots,
            style_context=style_context,
            prev_episode_context=prev_episode_context,
            tail_anchor=bool(tail_anchor),
            include_first_frame=False,
        )

        report_progress(0.85, "持久化 s9 预览与 Seedance2.0 主体提示词")
        beats: list[VisualBeat] = []
        for idx, segment in enumerate(plan.segments):
            slots: list[S9SegmentAssetSlot] = per_segment_slots[idx]
            full_prompt = stage2_prompts[idx] if idx < len(stage2_prompts) else ""
            audio_type, speaker, narration_summary = aggregate_segment_narration(segment.shots)
            visual_summary = aggregate_segment_visual_summary(segment)

            identities: list[str] = []
            props: list[str] = []
            first_scene_id = ""
            first_scene_var = ""
            for s in segment.shots:
                for iid in s.identity_ids:
                    if iid and iid not in identities:
                        identities.append(iid)
                for pid in s.prop_ids:
                    if pid and pid not in props:
                        props.append(pid)
                if not first_scene_id and s.scene_id:
                    first_scene_id = s.scene_id
                    first_scene_var = s.scene_variant_id

            planned_dur = int(segment.planned_duration_seconds)
            seg_title = f"大段{segment.segment_number} · {segment.title}"
            seg_meta = S9SegmentMeta(
                segment_number=segment.segment_number,
                segment_title=seg_title,
                planned_duration_seconds=planned_dur,
                max_segment_seconds=cap,
                story_beat=segment.story_beat,
                tail_anchor=bool(tail_anchor),
                include_first_frame=False,
                segment_asset_slots=slots,
            )
            sd_cfg = Seedance2VideoConfig(
                mode=Seedance2I2VMode.MULTIMODAL_REFERENCE,
                mode_user_set=True,
                duration=planned_dur,
                duration_user_set=False,
                resolution="720p",
                ratio=style_context.aspect_ratio,
                ratio_user_set=False,
                generate_audio=True,
                generate_audio_user_set=True,
                return_last_frame=False,
                human_review=True,
                prompt_style=S9_PROMPT_STYLE,
                prompt_source=S9_PROMPT_SOURCE,
                final_prompt=full_prompt,
                s9_tail_anchor=bool(tail_anchor),
                s9_include_first_frame=False,
            )
            beats.append(
                VisualBeat(
                    beat_number=segment.segment_number,
                    narration_segment=narration_summary,
                    visual_description=visual_summary,
                    scene_ref=(
                        build_scene_ref(first_scene_id, first_scene_var)
                        if first_scene_id
                        else None
                    ),
                    detected_identities=identities,
                    detected_props=props,
                    audio_type=audio_type,
                    speaker=speaker,
                    speaker_kind="character" if speaker else "non_character",
                    estimated_duration=float(planned_dur),
                    duration_seconds=float(planned_dur),
                    video_mode="first_frame",
                    video_prompt=segment.story_beat or visual_summary,
                    seedance2_config_json=dump_seedance2_config(sd_cfg),
                    segment_title=seg_title,
                    s9_subshots_json=json.dumps(
                        [s.model_dump(mode="json") for s in segment.shots],
                        ensure_ascii=False,
                    ),
                    s9_segment_meta_json=json.dumps(
                        seg_meta.model_dump(mode="json"),
                        ensure_ascii=False,
                    ),
                )
            )

        preview_meta = S9EpisodePreviewMeta(
            version=1,
            generation_mode="s9",
            max_segment_seconds=cap,
            tail_anchor=bool(tail_anchor),
            visual_style_id=style_context.style_id,
            visual_style_label=style_context.style_label,
            aspect_ratio=style_context.aspect_ratio,
            duration_assessment=plan.duration_assessment,
            asset_manifest=plan.asset_manifest,
        )
        script = NarrationScript(
            episode_number=episode_num,
            title=str(getattr(episode, "title", "") or f"第{episode_num}集"),
            beats=beats,
            total_duration_seconds=float(plan.duration_assessment.estimated_total_seconds),
            s9_preview_meta_json=json.dumps(
                preview_meta.model_dump(mode="json"),
                ensure_ascii=False,
            ),
        )
        await self.cognee_store.persist_narration_script(script)

        self.last_review_passed = True
        self.last_review_issues = []
        self.last_degraded_lines = []
        self.last_review_summary = (
            f"s9 分段完成：共 {len(beats)} 个大段（单段上限 {cap} 秒），"
            f"预计总长 {int(script.total_duration_seconds)} 秒"
        )
        report_progress(1.0, self.last_review_summary)
        return script


def create_s9_script_writing_workflow(
    cognee_store: Any,
    sqlite_store: Any | None = None,
    *,
    output_dir: str = "",
    genre: str = "",
    story_setting: str = "",
    spine_template: str = "drama",
) -> S9ScriptWritingWorkflow:
    return S9ScriptWritingWorkflow(
        cognee_store=cognee_store,
        sqlite_store=sqlite_store or cognee_store,
        output_dir=output_dir,
        genre=genre,
        story_setting=story_setting,
        spine_template=spine_template,
    )


__all__ = [
    "S9ScriptWritingWorkflow",
    "create_s9_script_writing_workflow",
]
