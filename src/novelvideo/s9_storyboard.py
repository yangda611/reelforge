"""YD Cinematic Segment Storyboard core schemas, prompts, validators, and asset binders.

Implements the two-stage workflow from ``YD分镜skills`` (``yd-cinematic-storyboard``,
replacing legacy ``s9`` while preserving storage/API compatibility):
1. Stage 1 (Compact Storyboard Preview): Duration assessment (defaulting to
   ``紧凑`` pacing), asset manifest, and coherent story segments (大段) capped at
   30s or 15s (ceiling, not target), where each clip finishes its full story beat
   and lands an explicit ``exit_state`` for seamless next-clip handoff.
2. Stage 2 (Execution Prompt): Full Seedance 2.0/2.5 continuous multi-shot prompt
   per 大段 (【参考职责】【全局光影画风】【场景布局】【注意事项】【角色声音】 + 分镜1..M
   with YD acting/facial-timing choreography), plus dynamic runtime rebinding
   between manifest slots and generated 虾塘 assets.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, Field

from novelvideo.brainclaw_contract import BrainClawProfile

logger = logging.getLogger("novelvideo.s9_storyboard")

DEFAULT_S9_MAX_SEGMENT_SECONDS = 30
VALID_S9_MAX_SEGMENT_SECONDS = (15, 30)
MIN_S9_SEGMENT_SECONDS = 4
S9_PROMPT_STYLE = "s9_segment"
S9_PROMPT_SOURCE = "s9_script"

_MENTION_RE = re.compile(r"@?(图片|音频)\s*(\d+)")


class S9DurationAssessment(BaseModel):
    """Stage 1 【时长评估】 summary."""

    language: str = Field(default="普通话", description="对白/旁白语言")
    pace_tier: str = Field(default="紧凑", description="节奏档位：紧凑 / 标准 / 舒缓")
    constraint_summary: str = Field(
        default="单段上限30秒（上限非目标，按剧情自然长度分段）",
        description="分段时长约束说明，如 单段上限30秒 / 单段上限15秒",
    )
    speech_total_seconds: float = Field(
        default=0.0,
        ge=0.0,
        description="全部对白/旁白发声合计 S（秒）",
    )
    core_chain_seconds: str = Field(
        default="",
        description="核心表演/台词/动作链估算与演一遍复核结论",
    )
    estimated_total_seconds: int = Field(default=0, ge=0, description="预计成片总时长（秒）")
    segment_count: int = Field(default=0, ge=0, description="大段数量")
    conclusion: str = Field(default="满足约束", description="评估结论")


class S9AssetManifestItem(BaseModel):
    """Single item in 【素材准备清单】."""

    slot_label: str = Field(description="如 @图片1 / @音频1")
    media_type: Literal["image", "audio"] = Field(description="image 或 audio")
    asset_kind: Literal["identity", "scene", "scene_reverse", "prop", "voice", "first_frame"] = Field(
        description="资产类型"
    )
    name: str = Field(description="资产展示名/角色名/场景名/道具名")
    asset_id: str = Field(default="", description="匹配的 ReelForge 内部 ID")
    variant_id: str = Field(default="", description="场景变体 ID（如有）")
    status_label: str = Field(default="待绑定", description="已就绪 / 待绑定")


class S9SegmentAssetSlot(BaseModel):
    """Per-segment ordered reference binding between prompt @图片N/@音频N and ReelForge assets."""

    slot: int = Field(ge=1, description="1-based reference slot index")
    media_type: Literal["image", "audio"]
    asset_kind: Literal["first_frame", "identity", "scene", "scene_reverse", "prop", "voice"]
    name: str
    asset_id: str = ""
    variant_id: str = ""


class S9SubShot(BaseModel):
    """Single 分镜 inside a 大段."""

    shot_number: int = Field(default=1, ge=1)
    label: str = Field(default="", description="如 大段1-分镜1")
    title: str = Field(default="", description="分镜小标题")
    planned_duration_seconds: float = Field(default=4.0, gt=0)
    start_seconds: float = Field(default=0.0, ge=0)
    end_seconds: float = Field(default=4.0, gt=0)
    shot_scale_movement: str = Field(default="中景", description="景别，如 中景转特写")
    camera_angle: str = Field(default="平视", description="视角，如 平视 / 俯视45°转平视")
    camera_motion: str = Field(default="缓慢推进", description="运镜，如 缓慢推近 / 固定机位")
    content: str = Field(default="", description="分镜内容（景别机位运镜+具体动作表演+完整原话+必要拟音）")
    audio_type: Literal["dialogue", "narration", "silence"] = Field(default="silence")
    speaker: str = Field(default="", description="说话人/OS归属角色")
    spoken_text: str = Field(default="", description="本镜完整台词/内心独白/旁白（原词保留）")
    sfx_cue: str = Field(default="", description="本镜关键物理拟音与环境声变化")
    transition_note: str = Field(default="", description="本镜切出动作相位、余势与衔接点")
    identity_ids: list[str] = Field(default_factory=list)
    scene_id: str = Field(default="")
    scene_variant_id: str = Field(default="")
    prop_ids: list[str] = Field(default_factory=list)


class S9SceneLightingAnchor(BaseModel):
    """Unified per-scene lighting & visual style anchor shared across segments."""

    scene_id: str = Field(default="", description="场景 ID（与场景菜单一致）")
    time_of_day: str = Field(default="", description="锁定时段（日/夜/黄昏等）")
    visual_style_summary: str = Field(default="", description="本场景成像媒介与渲染基调")
    key_lighting: str = Field(default="", description="主光来源、场内固定方向与明暗软硬")
    color_and_material: str = Field(default="", description="统一色温色彩基调与核心材质")


class S9SceneLayoutAnchor(BaseModel):
    """Unified per-scene world geometry & working-side anchor across segments."""

    scene_id: str = Field(default="", description="场景 ID")
    primary_view_direction: str = Field(default="", description="世界几何主视方向")
    landmarks: str = Field(default="", description="2~4 个固定地标及其左右方位")
    camera_working_side: str = Field(default="", description="主关系轴及允许机位工作侧与同场光向")


class S9VoiceProfileAnchor(BaseModel):
    """Unified character voice profile locked across all episode segments."""

    speaker: str = Field(default="", description="说话角色名或身份ID")
    voice_design: str = Field(
        default="",
        description="稳定音色特征（音高、质地、共鸣/气息、常态吐字节奏与唯一特征）",
    )


class S9EpisodeContinuityBible(BaseModel):
    """Episode-wide continuity rules locked in Pass 1A and shared across parallel workers."""

    scene_lighting_rules: list[S9SceneLightingAnchor] = Field(default_factory=list)
    scene_layout_rules: list[S9SceneLayoutAnchor] = Field(default_factory=list)
    prop_state_chain: list[str] = Field(default_factory=list)
    voice_profiles: list[S9VoiceProfileAnchor] = Field(default_factory=list)


class S9SegmentPlan(BaseModel):
    """LLM Stage 1 output for one 大段."""

    segment_number: int = Field(default=1, ge=1)
    title: str = Field(default="", description="大段叙事小标题")
    planned_duration_seconds: int = Field(default=15, ge=1, description="本大段计划整数秒数（按剧情自然长度，不强凑上限）")
    source_excerpt: str = Field(default="", description="本大段对应的原稿连续原文切片")
    story_beat: str = Field(default="", description="本大段内必须100%讲完的叙事闭环（起势→推进→收束落点）")
    entry_state: str = Field(default="", description="本大段第0秒起始状态：人物站位、姿态、持物、视线与未完动作")
    exit_state: str = Field(default="", description="本大段结束瞬间必达落点状态，供下一大段第0秒无缝承接")
    scene_layout: str = Field(default="", description="本大段【场景布局】：世界地标、人物锚位、机位工作侧与左右投影（多人同场/走位必填）")
    shots: list[S9SubShot] = Field(default_factory=list)


class S9StoryboardPlan(BaseModel):
    """Structured Stage 1 output for a whole episode."""

    duration_assessment: S9DurationAssessment = Field(default_factory=S9DurationAssessment)
    asset_manifest: list[S9AssetManifestItem] = Field(default_factory=list)
    continuity_bible: S9EpisodeContinuityBible | None = Field(default=None)
    segments: list[S9SegmentPlan] = Field(default_factory=list)


class S9Stage1SkeletonSegment(BaseModel):
    """Lightweight Pass 1A segment skeleton with source slice & boundary contract."""

    segment_number: int = Field(default=1, ge=1)
    title: str = Field(default="", description="大段叙事小标题")
    planned_duration_seconds: int = Field(default=15, ge=1, description="本大段自然时长整数秒（<= 单段上限）")
    source_excerpt: str = Field(default="", description="原样复制自本集完整原文的对应连续文本段落")
    story_beat: str = Field(default="", description="本大段内100%讲完的叙事闭环（起势→推进→收束落点）")
    entry_state: str = Field(default="", description="本大段第0秒起始状态：站位、姿态、持物、视线、未完动作")
    exit_state: str = Field(default="", description="本大段结束瞬间必达落点状态，供下一大段第0秒衔接")
    opening_shot_direction: str = Field(default="", description="本大段第1镜开场景别/机位与动作起势方向")
    closing_shot_direction: str = Field(default="", description="本大段末镜收束景别/机位与余势方向")
    scene_layout: str = Field(default="", description="本大段【场景布局】要点（多人同场/走位必填）")
    identity_ids: list[str] = Field(default_factory=list)
    scene_ids: list[str] = Field(default_factory=list)
    prop_ids: list[str] = Field(default_factory=list)


class S9Stage1SkeletonPlan(BaseModel):
    """Pass 1A output: global timing, manifest, continuity bible, and segment skeletons."""

    duration_assessment: S9DurationAssessment = Field(default_factory=S9DurationAssessment)
    asset_manifest: list[S9AssetManifestItem] = Field(default_factory=list)
    continuity_bible: S9EpisodeContinuityBible = Field(default_factory=S9EpisodeContinuityBible)
    segments: list[S9Stage1SkeletonSegment] = Field(default_factory=list)


class S9SegmentExpansionOutput(BaseModel):
    """Pass 1B output: detailed shots & layout for one segment."""

    planned_duration_seconds: int = Field(default=15, ge=1, description="本大段整数秒数（自然长度，严禁凑满上限）")
    story_beat: str = Field(default="", description="本大段完整叙事闭环")
    entry_state: str = Field(default="", description="本大段第0秒起始状态")
    exit_state: str = Field(default="", description="本大段结束瞬间必达收束状态")
    scene_layout: str = Field(default="", description="本大段【场景布局】（多人同场/走位/正反打必填）")
    shots: list[S9SubShot] = Field(default_factory=list)


class S9EpisodePreviewMeta(BaseModel):
    """Persisted episode-level metadata rendered in 脚本 -> 脚本预览."""

    version: int = 1
    generation_mode: str = "s9"
    max_segment_seconds: int = DEFAULT_S9_MAX_SEGMENT_SECONDS
    tail_anchor: bool = False
    visual_style_id: str = ""
    visual_style_label: str = ""
    aspect_ratio: str = "9:16"
    duration_assessment: S9DurationAssessment | None = None
    asset_manifest: list[S9AssetManifestItem] = Field(default_factory=list)
    continuity_bible: S9EpisodeContinuityBible | None = None


class S9SegmentMeta(BaseModel):
    """Persisted per-Beat (per-大段) segment storyboard metadata."""

    segment_number: int = 1
    segment_title: str = ""
    planned_duration_seconds: int = 15
    max_segment_seconds: int = DEFAULT_S9_MAX_SEGMENT_SECONDS
    story_beat: str = ""
    entry_state: str = ""
    exit_state: str = ""
    scene_layout: str = ""
    source_excerpt: str = ""
    tail_anchor: bool = False
    include_first_frame: bool = False
    segment_asset_slots: list[S9SegmentAssetSlot] = Field(default_factory=list)


class S9StyleContext(BaseModel):
    """Resolved project style and framing directives expanded per s9 rules."""

    style_id: str = "chinese_period_drama"
    style_label: str = "写实电影质感"
    style_family: str = "live_action"
    animation_subtype: str = ""
    style_instructions: str = ""
    avoid_instructions: str = ""
    style_tag: str = ""
    aspect_ratio: str = "9:16"
    s9_style_anchor: str = ""


def normalize_max_segment_seconds(value: Any) -> int:
    """Clamp requested segment cap to supported s9 options (15 or 30)."""

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_S9_MAX_SEGMENT_SECONDS
    return 15 if parsed <= 15 else 30


def aspect_ratio_to_video_ratio(aspect_ratio: str | None) -> str:
    """Convert project image aspect ratio (2:3 / 16:9 / 9:16) to Seedance video ratio."""

    cleaned = str(aspect_ratio or "").strip()
    if cleaned in {"16:9", "4:3", "21:9", "3:2"}:
        return "16:9" if cleaned == "16:9" else cleaned
    if cleaned in {"9:16", "1:1", "3:4", "2:3"}:
        return "9:16" if cleaned == "2:3" else cleaned
    return "9:16"


def resolve_s9_style_context(
    *,
    visual_style: str | None = None,
    aspect_ratio: str | None = None,
    state_dir: str | Path | None = None,
    username: str | None = None,
    project: str | None = None,
) -> S9StyleContext:
    """Resolve project style configuration and build concrete s9 lighting/style hints."""

    from novelvideo.services.style_service import StyleService

    style_id = str(visual_style or "").strip() or "chinese_period_drama"
    config = None
    try:
        config = StyleService.get_style(
            style_id,
            username=username,
            project=project,
            state_dir=state_dir,
        )
    except Exception:
        config = None
    if config is None:
        config = StyleService.get_preset(style_id) or StyleService.get_preset(
            "chinese_period_drama"
        )

    label = style_id
    family = "live_action"
    subtype = ""
    style_instructions = ""
    avoid_instructions = ""
    style_tag = ""
    if config is not None:
        label = str(getattr(config, "label", "") or getattr(config, "name", "") or style_id)
        family = str(getattr(config, "style_family", "") or "live_action")
        subtype = str(getattr(config, "animation_subtype", "") or "")
        style_instructions = str(getattr(config, "style_instructions", "") or "")
        avoid_instructions = str(getattr(config, "avoid_instructions", "") or "")
        style_tag = str(getattr(config, "style_tag", "") or "")

    family_desc = (
        f"三维CG（{subtype.upper()}）"
        if family == "animation" and subtype and "3d" in subtype.lower()
        else (
            f"动画（{subtype.upper()}）"
            if family == "animation" and subtype
            else ("动画" if family == "animation" else "真人电影实拍")
        )
    )
    anchor_parts = [f"画风基调：{label}（{family_desc}）"]
    if style_instructions:
        anchor_parts.append(f"风格细节：{style_instructions}")
    if style_tag:
        anchor_parts.append(f"氛围标签：{style_tag}")
    if avoid_instructions:
        anchor_parts.append(f"规避项：{avoid_instructions}")
    video_ratio = aspect_ratio_to_video_ratio(aspect_ratio)
    anchor_parts.append(f"画面比例：{video_ratio}")
    if family == "live_action":
        anchor_parts.append("媒介规范：严格写实摄影光学成像与物理质感，禁混UE5/CG术语")
    elif family == "animation" and subtype and "3d" in subtype.lower():
        anchor_parts.append("媒介规范：三维CG建模与光影渲染质感，禁混胶片/实拍相机型号")

    return S9StyleContext(
        style_id=style_id,
        style_label=label,
        style_family=family,
        animation_subtype=subtype,
        style_instructions=style_instructions,
        avoid_instructions=avoid_instructions,
        style_tag=style_tag,
        aspect_ratio=video_ratio,
        s9_style_anchor="；".join(anchor_parts),
    )


_REVERSE_SCENE_CUE_RE = re.compile(
    r"(背面|反面|reverse|入户门|玄关|门铃|门外|开门|门口|门后|推门|门框|大门|破门|门槛|房门|防盗门|撞开)",
    re.IGNORECASE,
)
_REVERSE_NEGATED_CLAUSE_RE = re.compile(
    r"[^。；;\n]*(?:不开启|未开启|不开门|紧闭|不拍门|背对大门|远离大门|严禁开启|门未开)[^。；;\n]*",
    re.IGNORECASE,
)
_HALLUCINATED_REVERSE_SUFFIX_RE = re.compile(
    r"(?:_scene_reverse|_scene_back|_reverse|_scene|背面|反面)$",
    re.IGNORECASE,
)
_HALLUCINATED_REVERSE_VARIANTS = frozenset({"reverse", "scene_reverse", "back", "背面", "反面"})


def text_uses_reverse_scene_view(text: str) -> bool:
    """Return True when active visual prose calls for the scene's reverse/door angle."""

    cleaned = _REVERSE_NEGATED_CLAUSE_RE.sub("", str(text or ""))
    return bool(_REVERSE_SCENE_CUE_RE.search(cleaned))


def _normalize_scene_id_variant(scene_id: str, variant_id: str = "") -> tuple[str, str]:
    sid = str(scene_id or "").strip()
    vid = str(variant_id or "").strip()
    if vid.lower() in _HALLUCINATED_REVERSE_VARIANTS:
        vid = ""
    while sid and _HALLUCINATED_REVERSE_SUFFIX_RE.search(sid):
        stripped = _HALLUCINATED_REVERSE_SUFFIX_RE.sub("", sid).strip(" _")
        if not stripped or stripped == sid:
            break
        sid = stripped
    # Collapse accidental full-name duplication like "艾莉森公寓客厅_狼藉版_艾莉森公寓客厅_狼藉版"
    if sid and "_" in sid:
        parts = sid.split("_")
        if len(parts) % 2 == 0:
            half = len(parts) // 2
            if parts[:half] == parts[half:]:
                sid = "_".join(parts[:half])
    if sid and vid and (vid == sid or sid.endswith(f"_{vid}")):
        vid = ""
    return sid, vid


def _scene_record_lookup(scenes: list[Any] | None) -> dict[str, Any]:
    lookup: dict[str, Any] = {}
    for sc in scenes or []:
        name = str(getattr(sc, "name", "") or "").strip()
        if name:
            lookup[name] = sc
    return lookup


def _resolve_scene_and_base(
    scene_lookup: dict[str, Any],
    scene_id: str,
    variant_id: str = "",
) -> tuple[str, Any | None, Any | None]:
    """Resolve (plate_key, scene_obj, base_obj) including base_scene_id inheritance."""

    sid, vid = _normalize_scene_id_variant(scene_id, variant_id)
    plate_key = f"{sid}_{vid}" if vid else sid
    scene_obj = scene_lookup.get(plate_key) or scene_lookup.get(sid)
    base_obj = None
    if scene_obj is not None:
        base_id = str(getattr(scene_obj, "base_scene_id", "") or "").strip()
        if base_id:
            cand = scene_lookup.get(base_id)
            if cand is not None and cand is not scene_obj:
                base_obj = cand
        if base_obj is None and vid:
            cand = scene_lookup.get(sid)
            if cand is not None and cand is not scene_obj:
                base_obj = cand
        if base_obj is None and "_" in sid:
            cand = scene_lookup.get(sid.split("_", 1)[0])
            if cand is not None and cand is not scene_obj:
                base_obj = cand
    return plate_key, scene_obj, base_obj


def _scene_has_reverse_master_on_disk(
    output_dir: str | Path | None,
    scene_id: str,
    variant_id: str = "",
) -> bool:
    if not output_dir or not scene_id:
        return False
    from novelvideo.utils.path_resolver import canonical_scene_reverse_master_path

    sid, vid = _normalize_scene_id_variant(scene_id, variant_id)
    root = Path(output_dir)
    candidates = []
    if vid:
        candidates.append(canonical_scene_reverse_master_path(root, f"{sid}_{vid}"))
    candidates.append(canonical_scene_reverse_master_path(root, sid))
    if "_" in sid:
        candidates.append(canonical_scene_reverse_master_path(root, sid.split("_", 1)[0]))
    return any(p.exists() for p in candidates)


def _format_planned_assets_summary(
    *,
    characters: list[Any],
    episode: Any,
    voice_available_names: set[str],
    scenes: list[Any] | None = None,
    props: list[Any] | None = None,
    output_dir: str | Path | None = None,
) -> str:
    """Format episode cast, scenes, props, and voices for LLM grounding."""

    lines: list[str] = []
    default_map = {}
    raw_map = getattr(episode, "identity_default_map_json", "") if episode else ""
    if isinstance(raw_map, str) and raw_map.strip():
        try:
            default_map = json.loads(raw_map)
        except (TypeError, json.JSONDecodeError):
            default_map = {}

    char_lines: list[str] = []
    episode_identity_ids = list(getattr(episode, "identity_ids", []) or []) if episode else []
    for char in characters or []:
        char_name = str(getattr(char, "name", "") or "").strip()
        if not char_name:
            continue
        identities = list(getattr(char, "identities", []) or [])
        relevant_ids = [
            str(getattr(item, "identity_id", "") or "").strip()
            for item in identities
            if getattr(item, "identity_id", "")
        ]
        if episode_identity_ids:
            scoped = [iid for iid in relevant_ids if iid in episode_identity_ids]
            if scoped:
                relevant_ids = scoped
        has_voice = "有参考音色" if (
            char_name in voice_available_names
            or any(iid in voice_available_names for iid in relevant_ids)
            or bool(str(getattr(char, "reference_audio_path", "") or "").strip())
        ) else "待生成参考音色"
        default_id = str(default_map.get(char_name) or (relevant_ids[0] if relevant_ids else char_name))
        char_lines.append(
            f"- 角色 `{char_name}` | 身份ID列表: {relevant_ids or [default_id]} | "
            f"默认身份: `{default_id}` | 音色状态: {has_voice}"
        )
    if char_lines:
        lines.append("【已规划角色与身份】\n" + "\n".join(char_lines))

    scene_lookup = _scene_record_lookup(scenes)
    scene_items = list(getattr(episode, "scene_menu", []) or []) if episode else []
    seen_scene_plates: set[str] = set()
    if scene_items:
        from novelvideo.models import build_scene_effective_prompt

        scene_lines = []
        for item in scene_items:
            sid, vid = _normalize_scene_id_variant(
                str(getattr(item, "scene_id", "") or "").strip(),
                str(getattr(item, "variant_id", "") or "").strip(),
            )
            if not sid:
                continue
            desc = str(getattr(item, "description", "") or getattr(item, "notes", "") or "").strip()
            plate_key, scene_obj, base_obj = _resolve_scene_and_base(scene_lookup, sid, vid)
            seen_scene_plates.add(plate_key)
            seen_scene_plates.add(sid)
            env_detail = ""
            time_note = ""
            if scene_obj is not None:
                env_detail = build_scene_effective_prompt(scene_obj, base_obj).strip()
                sc_time = str(getattr(scene_obj, "time_of_day", "") or "").strip()
                if sc_time:
                    time_note = f" | 固定时段: {sc_time}"
                notes = str(getattr(scene_obj, "notes", "") or "").strip()
                if notes and notes not in env_detail:
                    env_detail = f"{env_detail}\n  空间/设定补充：{notes}" if env_detail else notes
            has_reverse = _scene_has_reverse_master_on_disk(output_dir, sid, vid)
            reverse_note = (
                " | 已有背面参考图(reverse_master.png，拍入户门/背面反打时系统会自动附带；scene_id 必须严格填写原值，严禁自行拼接 _reverse 或 _scene_reverse)"
                if has_reverse
                else ""
            )
            detail_parts = [p for p in [desc, env_detail] if p]
            combined_desc = "；".join(dict.fromkeys(detail_parts))
            scene_lines.append(
                f"- scene_id=`{sid}`"
                + (f" (变体: `{vid}`)" if vid else "")
                + time_note
                + reverse_note
                + (f" — {combined_desc}" if combined_desc else "")
            )
        if scene_lines:
            lines.append("【已规划场景菜单】\n" + "\n".join(scene_lines))

    if scenes:
        from novelvideo.models import build_scene_effective_prompt

        extra_scene_lines: list[str] = []
        for sc in scenes:
            sc_name = str(getattr(sc, "name", "") or "").strip()
            if not sc_name or sc_name in seen_scene_plates:
                continue
            # Avoid cluttering with base scenes whose derived variant is already planned for this episode
            if any(
                planned.startswith(f"{sc_name}_")
                for planned in seen_scene_plates
            ):
                continue
            seen_scene_plates.add(sc_name)
            _pk, _so, base_obj = _resolve_scene_and_base(scene_lookup, sc_name, "")
            env_detail = build_scene_effective_prompt(sc, base_obj).strip()
            extra_scene_lines.append(
                f"- scene_id=`{sc_name}`" + (f" — {env_detail}" if env_detail else "")
            )
        if extra_scene_lines:
            lines.append(
                "【项目已有场景库（跨集复用：仅当本集画面实际切到该地点时选用对应 scene_id）】\n"
                + "\n".join(extra_scene_lines)
            )

    prop_items = list(getattr(episode, "prop_menu", []) or []) if episode else []
    prop_lines: list[str] = []
    seen_prop_ids: set[str] = set()
    for item in prop_items:
        pid = str(getattr(item, "prop_id", "") or getattr(item, "name", "") or "").strip()
        if not pid or pid in seen_prop_ids:
            continue
        seen_prop_ids.add(pid)
        desc = str(getattr(item, "description", "") or getattr(item, "visual_prompt", "") or "").strip()
        prop_lines.append(f"- prop_id=`{pid}`" + (f" — {desc}" if desc else ""))
    if prop_lines:
        lines.append("【已规划道具菜单】\n" + "\n".join(prop_lines))

    extra_prop_lines: list[str] = []
    for prop_obj in props or []:
        pid = str(getattr(prop_obj, "name", "") or "").strip()
        if not pid or pid in seen_prop_ids:
            continue
        seen_prop_ids.add(pid)
        desc = str(
            getattr(prop_obj, "description", "")
            or getattr(prop_obj, "visual_prompt", "")
            or ""
        ).strip()
        extra_prop_lines.append(f"- prop_id=`{pid}`" + (f" — {desc}" if desc else ""))
    if extra_prop_lines:
        lines.append(
            "【项目已有道具库（跨集复用：仅当本集画面实际出现/携带时选用对应 prop_id）】\n"
            + "\n".join(extra_prop_lines)
        )

    return "\n\n".join(lines) if lines else "（本集暂无预设资产清单，请从正文提取所需角色、场景、道具并填写名称）"


def build_s9_stage1_system_prompt(
    *,
    max_segment_seconds: int,
    spine_template: str = "drama",
    tail_anchor: bool = False,
) -> str:
    """Build the Stage 1 (Compact Storyboard Preview) system prompt from YD分镜skills."""

    from novelvideo.skills.yd_storyboard_skill import build_yd_stage1_directives

    cap = normalize_max_segment_seconds(max_segment_seconds)
    yd_directives = build_yd_stage1_directives(
        max_segment_seconds=cap,
        pace_tier="紧凑",
    )
    mode_rule = (
        "【当前为解说剧（narrated）模式】\n"
        "- 原文中的旁白、解说词、心里独白必须完整保留（不得删句/吞词），在对应分镜的 `audio_type` 标为 `narration`，"
        "`spoken_text` 填完整旁白词，`speaker` 填讲述视角（如 `林默 OS` 或 `旁白`）；\n"
        "- 原文中的明确角色对话标为 `dialogue`，`speaker` 填说话角色身份ID，`spoken_text` 填原句对白。\n"
        "- 旁白/OS 声画并行：人物在画面中继续自然做事（如敲键盘/走位/反应），不另加纯动作秒数。"
        if spine_template == "narrated"
        else
        "【当前为精品剧（drama）模式】\n"
        "- 完整保留原文中的全部对白、关键内心独白（OS）与核心戏剧动作；\n"
        "- 角色开口对白标 `dialogue`（`speaker` 填对应身份ID，`spoken_text` 保留原话）；"
        "内心独白/画外音标 `narration`（`speaker` 填 `角色名 OS`）；纯画面反应/动作/插入标 `silence`。"
    )
    anchor_note = (
        "已开启「尾帧锚点镜」：在需要跨段衔接的大段末尾预留 1 秒稳态静止锚点缓冲（计入该段总长）。"
        if tail_anchor
        else "「尾帧锚点镜」默认关闭：大段末镜结尾须自然落定 `exit_state`（姿态/持物/视线落点），以动作顺延或视线引导无缝承接下一大段。"
    )

    return f"""你是 YD 电影级分镜导演（Stage 1 整片连续模式 · 默认「紧凑」高密度剪辑节奏）。
你的任务是严格按 YD分镜skills 把整集剧本拆分为可直接生成连贯视频 Clip 的「大段」（每大段必须在单段上限 {cap} 秒内讲完本段完整故事闭环，确保下一大段能无缝接续），并输出结构化 `S9StoryboardPlan` JSON。

{yd_directives}

### E. 本项目双剧型与资产输出约束
- **双剧型台词与发声规则**：
{mode_rule}
- **尾帧衔接开关**：
  {anchor_note}
- **时间轴精确对齐**：
  - 每一大段的 `planned_duration_seconds` 为整数秒且满足 `4 <= planned_duration_seconds <= {cap}`（按内容自然长度取整，**绝对禁止为了凑满 {cap} 秒而拉长镜头或硬加废镜头**）。
  - 段内所有分镜 `start_seconds` / `end_seconds` 从 `0` 连续拼接，最后一镜 `end_seconds` 精确等于该大段的 `planned_duration_seconds`。
- **资产与结构字段要求（极重要，严禁留空！）**：
  - `duration_assessment`：填 `pace_tier="紧凑"`、`speech_total_seconds`（全部发声合计 S 秒）与含演一遍复核结论的 `core_chain_seconds`。
  - `asset_manifest`：列出全集通用 `@图片1..N`（角色→场景→道具顺序）和 `@音频1..M`（有实际发声角色，含对白与角色OS）。
  - **每个分镜（`S9SubShot`）必须绑定资产字段**：
    - `identity_ids`：必须填入本镜出场或发声的全部角色身份ID（精确选用给定【已规划角色与身份】里的 `identity_id`，如 `["林默_青年时期"]`），禁止留空 `[]`（仅纯无人空镜除外）；
    - `scene_id`（与 `scene_variant_id`）：必须填入本镜所在场景ID（精确选用【已规划场景菜单】的 `scene_id`），禁止留空 `""`；
    - `prop_ids`：必须填入本镜出现、使用或提及的关键道具ID列表（精确选用【已规划道具菜单】的 `prop_id`，如 `["二手显示器", "机械键盘"]`）；
  - 每个大段填写：`story_beat`（本段从起势到落点的完整故事闭环）、`entry_state`（第0秒起始状态）、`exit_state`（末镜结束瞬间的必达站位/持物/视线落点，供下一段第0秒衔接）、`scene_layout`（多人同场/走位/正反打时写3～5句地标、锚位与机位工作侧）。
  - 每个分镜填写：`content`（景别机位运镜+具体动作交互与可见变化+完整原话+必要拟音）、`spoken_text`（逐字原句）、`sfx_cue`（关键物理拟音）、`transition_note`（切出余势与接续点）。"""


def build_s9_stage1_task_prompt(
    *,
    source_text: str,
    episode_num: int,
    episode_title: str,
    style_context: S9StyleContext,
    planned_assets_summary: str,
    max_segment_seconds: int,
    prev_episode_context: str = "",
) -> str:
    """Build Stage 1 user task prompt with episode source, assets, and style."""

    cap = normalize_max_segment_seconds(max_segment_seconds)
    prev_block = (
        f"\n【上一集末尾衔接上下文（本集 EP{episode_num} 第一大段 `entry_state` 与首镜须顺畅承接）】\n{prev_episode_context.strip()}\n"
        if prev_episode_context.strip()
        else ""
    )
    return f"""请使用 **YD分镜skills（紧凑节奏档）** 对第 {episode_num} 集《{episode_title or f'EP{episode_num}'}》进行整片连续模式拆分，单大段上限 **{cap} 秒**（注意：{cap} 秒只是上限，不是每段都要凑满 {cap} 秒！）。

【执行前强制自检】
1. 先计算全集发声总时长 S（普通话按 4～5 字/秒），边做边说的动作与台词并行不相加。
2. 砍掉所有无用废镜头（如无戏剧职责的 4 秒雨夜门面空镜、说话完后空敲 8 秒键盘的拖尾），保证每个分镜都承载不可替代的故事/表演信息。
3. 确保每一个大段（Clip）内部 **100% 讲完自己的 `story_beat`**，并在末镜明确落定 `exit_state`，让下一大段第 0 秒能够直接接上！
4. **每一个分镜都必须填充 `identity_ids`、`scene_id`、`prop_ids`**（精确引用下方【本集已有资产与音色】里的 ID），否则下游无法绑定角色图、场景图、道具图与参考音色！

【项目视觉规格】
- {style_context.s9_style_anchor}
{prev_block}
【本集已有资产与音色】
{planned_assets_summary}

【本集完整原文 / 工作稿】
{source_text.strip()}

请输出完整的 `S9StoryboardPlan` JSON。"""


def _coerce_identity_lookup(characters: list[Any], episode: Any) -> tuple[dict[str, str], dict[str, str], dict[str, Any]]:
    """Build fast lookups for identity_id, char_name -> default identity_id, and voice existence."""

    valid_identities: dict[str, str] = {}
    char_to_default: dict[str, str] = {}
    voice_by_key: dict[str, Any] = {}
    default_map: dict[str, str] = {}
    raw_map = getattr(episode, "identity_default_map_json", "") if episode else ""
    if isinstance(raw_map, str) and raw_map.strip():
        try:
            parsed = json.loads(raw_map)
            if isinstance(parsed, dict):
                default_map = {str(k): str(v) for k, v in parsed.items()}
        except (TypeError, json.JSONDecodeError):
            default_map = {}

    episode_identities = set(getattr(episode, "identity_ids", []) or []) if episode else set()
    for char in characters or []:
        char_name = str(getattr(char, "name", "") or "").strip()
        identities = list(getattr(char, "identities", []) or [])
        char_has_voice = bool(str(getattr(char, "reference_audio_path", "") or "").strip())
        if char_has_voice and char_name:
            voice_by_key[char_name] = char
        picked_default = default_map.get(char_name, "")
        first_id = ""
        for ident in identities:
            iid = str(getattr(ident, "identity_id", "") or "").strip()
            if not iid:
                continue
            valid_identities[iid] = iid
            valid_identities[iid.lower()] = iid
            if not first_id or (iid in episode_identities and first_id not in episode_identities):
                first_id = iid
            if char_has_voice or bool(str(getattr(ident, "voice_sample_path", "") or "").strip()):
                voice_by_key[iid] = char
        resolved_default = picked_default if picked_default in valid_identities else first_id
        if char_name and resolved_default:
            char_to_default[char_name] = resolved_default
            char_to_default[char_name.lower()] = resolved_default
    return valid_identities, char_to_default, voice_by_key


def _match_identity_id(
    raw_name: str,
    valid_identities: dict[str, str],
    char_to_default: dict[str, str],
) -> str:
    clean = re.sub(r"\s*[(（][^)）]*[)）]", "", str(raw_name or "")).strip()
    clean = clean.replace("OS", "").replace("os", "").strip()
    if not clean:
        return ""
    if clean in valid_identities:
        return valid_identities[clean]
    if clean.lower() in valid_identities:
        return valid_identities[clean.lower()]
    if clean in char_to_default:
        return char_to_default[clean]
    if clean.lower() in char_to_default:
        return char_to_default[clean.lower()]
    # Fallback prefix match e.g. "林默_青年" vs "林默"
    for key, iid in char_to_default.items():
        if key and (key in clean or clean in key):
            return iid
    return clean


def _match_scene_id(
    raw_scene: str,
    episode: Any,
    scenes: list[Any] | None = None,
) -> tuple[str, str]:
    clean, _ = _normalize_scene_id_variant(str(raw_scene or "").strip(), "")
    if not clean:
        return "", ""
    scene_menu = list(getattr(episode, "scene_menu", []) or []) if episode else []
    for item in scene_menu:
        sid, vid = _normalize_scene_id_variant(
            str(getattr(item, "scene_id", "") or "").strip(),
            str(getattr(item, "variant_id", "") or "").strip(),
        )
        if sid == clean or (vid and f"{sid}_{vid}" == clean):
            return sid, vid
    # Prefer the episode's active derived variant of the same base scene (e.g.
    # "艾莉森公寓客厅" -> "艾莉森公寓客厅_狼藉版") over the un-planned clean base scene.
    for item in scene_menu:
        sid, vid = _normalize_scene_id_variant(
            str(getattr(item, "scene_id", "") or "").strip(),
            str(getattr(item, "variant_id", "") or "").strip(),
        )
        if sid and sid.startswith(f"{clean}_"):
            return sid, vid
    for sc in scenes or []:
        sc_name = str(getattr(sc, "name", "") or "").strip()
        if sc_name and sc_name == clean:
            return sc_name, ""
    for item in scene_menu:
        sid, vid = _normalize_scene_id_variant(
            str(getattr(item, "scene_id", "") or "").strip(),
            str(getattr(item, "variant_id", "") or "").strip(),
        )
        if sid and (clean in sid or sid in clean):
            return sid, vid
    base_clean = clean.split("_", 1)[0]
    if base_clean and len(base_clean) >= 2:
        for item in scene_menu:
            sid, vid = _normalize_scene_id_variant(
                str(getattr(item, "scene_id", "") or "").strip(),
                str(getattr(item, "variant_id", "") or "").strip(),
            )
            if sid and (sid == base_clean or sid.startswith(f"{base_clean}_")):
                return sid, vid
    return clean, ""


def _match_prop_id(
    raw_prop: str,
    episode: Any,
    props: list[Any] | None = None,
) -> str:
    clean = str(raw_prop or "").strip()
    if not clean:
        return ""
    prop_menu = list(getattr(episode, "prop_menu", []) or []) if episode else []
    for item in prop_menu:
        pid = str(getattr(item, "prop_id", "") or getattr(item, "name", "") or "").strip()
        if pid == clean:
            return pid
    for p in props or []:
        pid = str(getattr(p, "name", "") or "").strip()
        if pid == clean:
            return pid
    for item in prop_menu:
        pid = str(getattr(item, "prop_id", "") or getattr(item, "name", "") or "").strip()
        if pid and (clean in pid or pid in clean):
            return pid
    for p in props or []:
        pid = str(getattr(p, "name", "") or "").strip()
        if pid and (clean in pid or pid in clean):
            return pid
    return clean


def _normalize_subshot_timeline(shots: list[S9SubShot], target_duration: int) -> list[S9SubShot]:
    """Ensure sub-shots form a contiguous [0, target_duration] timeline.

    Uses integer-second boundaries whenever ``target_duration >= len(shots)``
    (matching Seedance 2.5 continuous integer-second windows), falling back to a
    0.5s grid only when there are more sub-shots than integer seconds.
    """

    if not shots:
        return [
            S9SubShot(
                shot_number=1,
                planned_duration_seconds=float(target_duration),
                start_seconds=0.0,
                end_seconds=float(target_duration),
                content="镜头承接本大段主体画面与动作推进。",
            )
        ]

    raw_durations = []
    for shot in shots:
        dur = float(shot.planned_duration_seconds or 0.0)
        if dur <= 0 and float(shot.end_seconds or 0.0) > float(shot.start_seconds or 0.0):
            dur = float(shot.end_seconds) - float(shot.start_seconds)
        raw_durations.append(max(1.0, dur if dur > 0 else 2.5))

    total_raw = sum(raw_durations) or float(target_duration)
    scale = float(target_duration) / total_raw
    use_integer_grid = int(target_duration) >= len(shots)

    normalized: list[S9SubShot] = []
    cursor = 0.0
    ideal_cursor = 0.0
    count = len(shots)
    for idx, shot in enumerate(shots):
        shot_num = idx + 1
        ideal_cursor += raw_durations[idx] * scale
        if idx == count - 1:
            end_sec = float(target_duration)
        elif use_integer_grid:
            remaining_min = float(count - 1 - idx)
            min_end = cursor + 1.0
            max_end = max(min_end, float(target_duration) - remaining_min)
            snapped = float(round(ideal_cursor))
            end_sec = min(max_end, max(min_end, snapped))
        else:
            step = max(0.5, round((raw_durations[idx] * scale) * 2.0) / 2.0)
            remaining_min = (count - 1 - idx) * 0.5
            max_end = max(cursor + 0.5, float(target_duration) - remaining_min)
            end_sec = min(max_end, cursor + step)
            dur_half = max(0.5, round((end_sec - cursor) * 2.0) / 2.0)
            end_sec = round(cursor + dur_half, 1)

        dur = round(max(0.5, end_sec - cursor), 1)
        if use_integer_grid:
            dur = float(int(round(end_sec - cursor)))
            end_sec = float(int(round(end_sec)))
        normalized.append(
            shot.model_copy(
                update={
                    "shot_number": shot_num,
                    "start_seconds": round(cursor, 1),
                    "end_seconds": round(end_sec, 1),
                    "planned_duration_seconds": dur,
                }
            )
        )
        cursor = end_sec
    return normalized


def _format_seconds_label(value: float) -> str:
    rounded = round(float(value), 1)
    if abs(rounded - int(rounded)) < 0.05:
        return str(int(rounded))
    return f"{rounded:.1f}"


def _infer_identities_from_text(
    text: str,
    characters: list[Any],
    valid_identities: dict[str, str],
    char_to_default: dict[str, str],
) -> list[str]:
    """Recover character identity IDs mentioned in shot or segment prose."""

    haystack = str(text or "").strip()
    if not haystack:
        return []
    found: list[str] = []
    # Prefer exact identity_id matches first, then character name / aliases.
    for iid in dict.fromkeys(valid_identities.values()):
        if iid and iid in haystack and iid not in found:
            found.append(iid)
    for char in characters or []:
        char_name = str(getattr(char, "name", "") or "").strip()
        aliases = [str(a or "").strip() for a in (getattr(char, "aliases", []) or []) if str(a or "").strip()]
        candidates = [char_name, *aliases] if char_name else aliases
        if any(token and token in haystack for token in candidates):
            resolved = char_to_default.get(char_name) or char_to_default.get(char_name.lower(), "")
            if resolved and resolved not in found:
                found.append(resolved)
    return found


def _infer_scene_from_text(
    text: str,
    episode: Any,
    manifest_scenes: list[tuple[str, str]],
) -> tuple[str, str]:
    """Recover scene_id/variant_id from shot or segment prose and episode scene_menu."""

    haystack = str(text or "").strip()
    scene_menu = list(getattr(episode, "scene_menu", []) or []) if episode else []
    if haystack:
        for item in scene_menu:
            sid = str(getattr(item, "scene_id", "") or "").strip()
            vid = str(getattr(item, "variant_id", "") or "").strip()
            if not sid:
                continue
            base_sid = sid.split("_", 1)[0]
            if sid in haystack or (base_sid and len(base_sid) >= 2 and base_sid in haystack):
                return sid, vid
        for sid, vid in manifest_scenes:
            base_sid = sid.split("_", 1)[0]
            if sid and (sid in haystack or (base_sid and len(base_sid) >= 2 and base_sid in haystack)):
                return sid, vid
    if len(scene_menu) == 1:
        sid = str(getattr(scene_menu[0], "scene_id", "") or "").strip()
        vid = str(getattr(scene_menu[0], "variant_id", "") or "").strip()
        if sid:
            return sid, vid
    if len(manifest_scenes) == 1:
        return manifest_scenes[0]
    return "", ""


def _infer_props_from_text(
    text: str,
    episode: Any,
    manifest_props: list[str],
    props: list[Any] | None = None,
) -> list[str]:
    """Recover prop_ids mentioned in visual shot or segment prose from episode & project props."""

    haystack = str(text or "").strip()
    if not haystack:
        return []
    found: list[str] = []
    prop_menu = list(getattr(episode, "prop_menu", []) or []) if episode else []
    for item in prop_menu:
        pid = str(getattr(item, "prop_id", "") or getattr(item, "name", "") or "").strip()
        if not pid:
            continue
        aliases = [str(a or "").strip() for a in (getattr(item, "aliases", []) or []) if str(a or "").strip()]
        tokens = [pid, *aliases]
        if any(tok and tok in haystack for tok in tokens) and pid not in found:
            found.append(pid)
    for pid in manifest_props:
        if pid and pid in haystack and pid not in found:
            found.append(pid)
    for prop_obj in props or []:
        pid = str(getattr(prop_obj, "name", "") or "").strip()
        if not pid or pid in found:
            continue
        aliases = [str(a or "").strip() for a in (getattr(prop_obj, "aliases", []) or []) if str(a or "").strip()]
        tokens = [pid, *aliases]
        if any(tok and tok in haystack for tok in tokens):
            found.append(pid)
    return found


def normalize_s9_storyboard_plan(
    plan: S9StoryboardPlan,
    *,
    max_segment_seconds: int = DEFAULT_S9_MAX_SEGMENT_SECONDS,
    characters: list[Any] | None = None,
    episode: Any = None,
    scenes: list[Any] | None = None,
    props: list[Any] | None = None,
    output_dir: str | Path | None = None,
    max_images_per_segment: int = 9,
    max_audios_per_segment: int = 3,
) -> tuple[S9StoryboardPlan, list[list[S9SegmentAssetSlot]]]:
    """Validate/repair durations, timelines, short tails, and segment asset slots."""

    cap = normalize_max_segment_seconds(max_segment_seconds)
    char_list = list(characters or [])
    valid_identities, char_to_default, _voice_by_key = _coerce_identity_lookup(
        char_list,
        episode,
    )

    manifest_identities: list[str] = []
    manifest_scenes: list[tuple[str, str]] = []
    manifest_reverse_scenes: set[str] = set()
    manifest_props: list[str] = []
    for item in plan.asset_manifest or []:
        if item.asset_kind == "identity":
            resolved = _match_identity_id(item.asset_id or item.name, valid_identities, char_to_default)
            if resolved and resolved not in manifest_identities:
                manifest_identities.append(resolved)
        elif item.asset_kind in {"scene", "scene_reverse"}:
            sid, vid = _match_scene_id(item.asset_id or item.name, episode, scenes=scenes)
            sid, vid = _normalize_scene_id_variant(sid, item.variant_id or vid)
            if sid and (sid, vid) not in manifest_scenes:
                manifest_scenes.append((sid, vid))
            if item.asset_kind == "scene_reverse" and sid:
                manifest_reverse_scenes.add(sid)
        elif item.asset_kind == "prop":
            pid = _match_prop_id(item.asset_id or item.name, episode, props=props)
            if pid and pid not in manifest_props:
                manifest_props.append(pid)

    segments = [seg.model_copy(deep=True) for seg in (plan.segments or [])]
    # Short-tail safety pass when LLM leaves a trailing <4s segment that cleanly fits in prev
    if len(segments) >= 2:
        last = segments[-1]
        prev = segments[-2]
        if int(last.planned_duration_seconds or 0) < MIN_S9_SEGMENT_SECONDS:
            if int(prev.planned_duration_seconds or 0) + int(last.planned_duration_seconds or 0) <= cap:
                offset_start = float(prev.planned_duration_seconds or 0)
                for shot in last.shots:
                    prev.shots.append(
                        shot.model_copy(
                            update={
                                "start_seconds": shot.start_seconds + offset_start,
                                "end_seconds": shot.end_seconds + offset_start,
                            }
                        )
                    )
                prev.planned_duration_seconds = min(
                    cap,
                    max(
                        MIN_S9_SEGMENT_SECONDS,
                        int(prev.planned_duration_seconds or 0)
                        + int(last.planned_duration_seconds or 0),
                    ),
                )
                prev.story_beat = " ".join(
                    part for part in [prev.story_beat.strip(), last.story_beat.strip()] if part
                )
                if str(getattr(last, "source_excerpt", "") or "").strip():
                    prev.source_excerpt = "\n".join(
                        part
                        for part in [
                            str(getattr(prev, "source_excerpt", "") or "").strip(),
                            str(last.source_excerpt).strip(),
                        ]
                        if part
                    )
                if str(getattr(last, "exit_state", "") or "").strip():
                    prev.exit_state = last.exit_state
                segments.pop()

    normalized_segments: list[S9SegmentPlan] = []
    per_segment_slots: list[list[S9SegmentAssetSlot]] = []
    manifest_seen: dict[tuple[str, str], S9AssetManifestItem] = {}

    for seg_idx, seg in enumerate(segments, start=1):
        # Respect s9 computed duration as exact integer in [MIN_S9_SEGMENT_SECONDS, cap]
        raw_seg_dur = int(round(float(seg.planned_duration_seconds or 0) or sum(
            float(s.planned_duration_seconds or 0) for s in seg.shots
        ) or 15))
        seg_dur = max(MIN_S9_SEGMENT_SECONDS, min(cap, raw_seg_dur))
        fixed_shots = _normalize_subshot_timeline(seg.shots, seg_dur)
        seg_prose = " ".join(
            part
            for part in [
                seg.title,
                seg.story_beat,
                getattr(seg, "entry_state", ""),
                getattr(seg, "exit_state", ""),
                getattr(seg, "scene_layout", ""),
                *[f"{sh.title} {sh.content} {sh.speaker}" for sh in fixed_shots],
            ]
            if part
        )

        # Normalize asset references per sub-shot and collect segment ordered manifest
        seg_identity_ids: list[str] = []
        seg_scene_keys: list[tuple[str, str]] = []
        seg_prop_ids: list[str] = []
        seg_speakers: list[str] = []

        cleaned_shots: list[S9SubShot] = []
        for shot in fixed_shots:
            label = shot.label.strip() or f"大段{seg_idx}-分镜{shot.shot_number}"
            # Keep visual/speaker prose separate from spoken dialogue text so characters
            # merely mentioned in dialogue (e.g. "我叔叔") are not dragged into visual slots.
            shot_visual_prose = " ".join(
                part for part in [shot.title, shot.content, shot.speaker] if part
            )
            mapped_identities: list[str] = []
            for raw_id in shot.identity_ids:
                resolved = _match_identity_id(raw_id, valid_identities, char_to_default)
                if resolved and resolved not in mapped_identities:
                    mapped_identities.append(resolved)
                if resolved and resolved not in seg_identity_ids:
                    seg_identity_ids.append(resolved)
            if not mapped_identities:
                for inferred_id in _infer_identities_from_text(
                    shot_visual_prose, char_list, valid_identities, char_to_default
                ):
                    if inferred_id not in mapped_identities:
                        mapped_identities.append(inferred_id)
                    if inferred_id not in seg_identity_ids:
                        seg_identity_ids.append(inferred_id)

            scene_id, scene_var = _match_scene_id(shot.scene_id, episode, scenes=scenes)
            if not scene_id:
                scene_id, scene_var = _infer_scene_from_text(
                    shot_visual_prose or seg_prose, episode, manifest_scenes
                )
            if not scene_id:
                scene_id, scene_var = _infer_scene_from_text(
                    seg_prose, episode, manifest_scenes
                )
            scene_id, scene_var = _normalize_scene_id_variant(
                scene_id, scene_var or shot.scene_variant_id
            )
            if scene_id:
                pair = (scene_id, scene_var)
                if pair not in seg_scene_keys:
                    seg_scene_keys.append(pair)

            mapped_props: list[str] = []
            for raw_prop in shot.prop_ids:
                pid = _match_prop_id(raw_prop, episode, props=props)
                if pid and pid not in mapped_props:
                    mapped_props.append(pid)
                if pid and pid not in seg_prop_ids:
                    seg_prop_ids.append(pid)
            for inferred_pid in _infer_props_from_text(
                shot_visual_prose, episode, manifest_props, props=props
            ):
                if inferred_pid not in mapped_props:
                    mapped_props.append(inferred_pid)
                if inferred_pid not in seg_prop_ids:
                    seg_prop_ids.append(inferred_pid)

            speaker_clean = str(shot.speaker or "").strip()
            if shot.audio_type in {"dialogue", "narration"} and speaker_clean:
                is_pure_os = "旁白" in speaker_clean or speaker_clean.lower() == "narrator"
                if shot.audio_type == "dialogue" or (not is_pure_os):
                    resolved_spk = _match_identity_id(speaker_clean, valid_identities, char_to_default)
                    if resolved_spk:
                        if resolved_spk not in mapped_identities and (
                            resolved_spk in valid_identities or resolved_spk in char_to_default.values()
                        ):
                            mapped_identities.append(resolved_spk)
                        if resolved_spk not in seg_identity_ids and (
                            resolved_spk in valid_identities or resolved_spk in char_to_default.values()
                        ):
                            seg_identity_ids.append(resolved_spk)
                        if (
                            shot.audio_type == "dialogue"
                            or resolved_spk in valid_identities
                            or resolved_spk in char_to_default.values()
                        ) and resolved_spk not in seg_speakers:
                            seg_speakers.append(resolved_spk)

            cleaned_shots.append(
                shot.model_copy(
                    update={
                        "label": label,
                        "identity_ids": mapped_identities,
                        "scene_id": scene_id,
                        "scene_variant_id": scene_var,
                        "prop_ids": mapped_props,
                    }
                )
            )

        # Segment-level safety recovery when LLM omitted shot-level arrays
        if not seg_identity_ids:
            for inferred_id in _infer_identities_from_text(
                seg_prose, char_list, valid_identities, char_to_default
            ) or manifest_identities:
                if inferred_id not in seg_identity_ids:
                    seg_identity_ids.append(inferred_id)
            if seg_identity_ids and cleaned_shots:
                cleaned_shots[0] = cleaned_shots[0].model_copy(
                    update={"identity_ids": list(seg_identity_ids)}
                )
        if not seg_prop_ids:
            for inferred_pid in _infer_props_from_text(
                seg_prose, episode, manifest_props, props=props
            ):
                if inferred_pid not in seg_prop_ids:
                    seg_prop_ids.append(inferred_pid)
            if seg_prop_ids and cleaned_shots:
                cleaned_shots[0] = cleaned_shots[0].model_copy(
                    update={"prop_ids": list(seg_prop_ids)}
                )

        # Deduplicate scene keys by scene_id AND collapse base+variant of the same
        # physical scene (e.g. "艾莉森公寓客厅" + "艾莉森公寓客厅_狼藉版" -> variant).
        dedup_scene_by_sid: dict[str, str] = {}
        ordered_scene_sids: list[str] = []
        for raw_sid, raw_vid in seg_scene_keys:
            sid, vid = _normalize_scene_id_variant(raw_sid, raw_vid)
            if sid not in dedup_scene_by_sid:
                dedup_scene_by_sid[sid] = vid
                ordered_scene_sids.append(sid)
            elif vid and not dedup_scene_by_sid[sid]:
                dedup_scene_by_sid[sid] = vid
        merged_scene_keys: list[tuple[str, str]] = []
        plate_keys = [
            f"{sid}_{dedup_scene_by_sid[sid]}" if dedup_scene_by_sid[sid] else sid
            for sid in ordered_scene_sids
        ]
        for sid, plate in zip(ordered_scene_sids, plate_keys):
            has_specific_variant = any(
                other != plate and other.startswith(f"{plate}_")
                for other in plate_keys
            )
            if has_specific_variant:
                continue
            merged_scene_keys.append((sid, dedup_scene_by_sid[sid]))
        seg_scene_keys = merged_scene_keys

        # Build local ordered @图片1..P and @音频1..A slots for THIS segment
        image_slots: list[S9SegmentAssetSlot] = []
        for iid in seg_identity_ids:
            if len(image_slots) >= max(1, max_images_per_segment):
                break
            image_slots.append(
                S9SegmentAssetSlot(
                    slot=len(image_slots) + 1,
                    media_type="image",
                    asset_kind="identity",
                    name=iid.split("_", 1)[0] or iid,
                    asset_id=iid,
                )
            )
        seg_action_prose = " ".join(
            part
            for part in [
                seg.title,
                seg.story_beat,
                getattr(seg, "entry_state", ""),
                getattr(seg, "exit_state", ""),
                *[
                    f"{sh.title} {sh.content} {getattr(sh, 'camera_angle', '')} {getattr(sh, 'transition_note', '')}"
                    for sh in fixed_shots
                ],
            ]
            if part
        )
        wants_reverse_scene = text_uses_reverse_scene_view(seg_action_prose)
        for sid, vid in seg_scene_keys:
            if len(image_slots) >= max(1, max_images_per_segment):
                break
            scene_display = f"{sid}({vid})" if vid else sid
            image_slots.append(
                S9SegmentAssetSlot(
                    slot=len(image_slots) + 1,
                    media_type="image",
                    asset_kind="scene",
                    name=scene_display,
                    asset_id=sid,
                    variant_id=vid,
                )
            )
            has_reverse_disk = _scene_has_reverse_master_on_disk(output_dir, sid, vid)
            if (
                len(image_slots) < max(1, max_images_per_segment)
                and (
                    (wants_reverse_scene and has_reverse_disk)
                    or (sid in manifest_reverse_scenes and (not output_dir or has_reverse_disk))
                )
            ):
                image_slots.append(
                    S9SegmentAssetSlot(
                        slot=len(image_slots) + 1,
                        media_type="image",
                        asset_kind="scene_reverse",
                        name=f"{scene_display}背面",
                        asset_id=sid,
                        variant_id=vid,
                    )
                )
        for pid in seg_prop_ids:
            if len(image_slots) >= max(1, max_images_per_segment):
                break
            image_slots.append(
                S9SegmentAssetSlot(
                    slot=len(image_slots) + 1,
                    media_type="image",
                    asset_kind="prop",
                    name=pid,
                    asset_id=pid,
                )
            )

        audio_slots: list[S9SegmentAssetSlot] = []
        for spk in seg_speakers:
            if len(audio_slots) >= max(1, max_audios_per_segment):
                break
            audio_slots.append(
                S9SegmentAssetSlot(
                    slot=len(audio_slots) + 1,
                    media_type="audio",
                    asset_kind="voice",
                    name=spk.split("_", 1)[0] or spk,
                    asset_id=spk,
                )
            )

        segment_slots = image_slots + audio_slots
        per_segment_slots.append(segment_slots)

        for s in segment_slots:
            key = (s.asset_kind, s.asset_id or s.name)
            if key not in manifest_seen:
                manifest_seen[key] = S9AssetManifestItem(
                    slot_label="",
                    media_type=s.media_type,
                    asset_kind=s.asset_kind,
                    name=s.name,
                    asset_id=s.asset_id,
                    variant_id=s.variant_id,
                )

        normalized_segments.append(
            seg.model_copy(
                update={
                    "segment_number": seg_idx,
                    "title": seg.title.strip() or f"第{seg_idx}幕",
                    "planned_duration_seconds": seg_dur,
                    "shots": cleaned_shots,
                }
            )
        )

    # Build clean unified 【素材准备清单】 numbering across episode
    manifest_items = [
        item.model_copy()
        for item in sorted(
            manifest_seen.values(),
            key=lambda x: (
                0 if x.media_type == "image" else 1,
                {
                    "identity": 0,
                    "scene": 1,
                    "scene_reverse": 2,
                    "prop": 3,
                    "voice": 4,
                    "first_frame": 5,
                }.get(x.asset_kind, 9),
            ),
        )
    ]
    img_counter = 0
    aud_counter = 0
    numbered_manifest: list[S9AssetManifestItem] = []
    for item in manifest_items:
        if item.media_type == "image":
            img_counter += 1
            slot_label = f"@图片{img_counter}"
        else:
            aud_counter += 1
            slot_label = f"@音频{aud_counter}"
        numbered_manifest.append(item.model_copy(update={"slot_label": slot_label}))

    # Preserve LLM explicit manifest entries only when none derived automatically
    if not numbered_manifest and plan.asset_manifest:
        numbered_manifest = list(plan.asset_manifest)

    total_seconds = sum(s.planned_duration_seconds for s in normalized_segments)
    computed_speech = round(
        sum(
            _estimate_spoken_seconds(shot.spoken_text)
            for seg in normalized_segments
            for shot in seg.shots
            if shot.spoken_text
        ),
        1,
    )
    prev_assess = plan.duration_assessment or S9DurationAssessment()
    assessment = prev_assess.model_copy(
        update={
            "pace_tier": prev_assess.pace_tier or "紧凑",
            "constraint_summary": f"单段上限{cap}秒（上限非目标，含短尾校验）",
            "speech_total_seconds": (
                prev_assess.speech_total_seconds
                if prev_assess.speech_total_seconds > 0
                else computed_speech
            ),
            "estimated_total_seconds": total_seconds,
            "segment_count": len(normalized_segments),
            "core_chain_seconds": (
                prev_assess.core_chain_seconds
                or f"约 {total_seconds} 秒（共 {len(normalized_segments)} 大段，紧凑节奏）"
            ),
            "conclusion": "满足约束",
        }
    )

    return (
        S9StoryboardPlan(
            duration_assessment=assessment,
            asset_manifest=numbered_manifest,
            continuity_bible=plan.continuity_bible,
            segments=normalized_segments,
        ),
        per_segment_slots,
    )


def format_slot_table_for_stage2(slots: list[S9SegmentAssetSlot]) -> str:
    """Format per-segment @图片N/@音频N binding table for Stage 2 prompt generation."""

    if not slots:
        return "（本大段暂无外部图音绑定，请使用精准外观与场景视觉描述）"
    lines: list[str] = []
    kind_labels = {
        "first_frame": "开篇首帧渲染图",
        "identity": "角色",
        "scene": "场景正面",
        "scene_reverse": "场景背面（反打/入户侧）",
        "prop": "道具",
        "voice": "角色音色",
    }
    for s in slots:
        prefix = "@图片" if s.media_type == "image" else "@音频"
        kind_text = kind_labels.get(s.asset_kind, s.asset_kind)
        id_suffix = f"（资产ID: {s.asset_id}）" if s.asset_id and s.asset_id != s.name else ""
        lines.append(f"- {prefix}{s.slot} -> {kind_text}：{s.name}{id_suffix}")
    return "\n".join(lines)


def _segment_needs_scene_layout(segment: S9SegmentPlan) -> bool:
    """Return True when YD space.md requires explicit 【场景布局】."""

    if str(getattr(segment, "scene_layout", "") or "").strip():
        return True
    identities: set[str] = set()
    for shot in segment.shots or []:
        for iid in shot.identity_ids or []:
            if iid:
                identities.add(iid)
    return len(identities) >= 2


def build_s9_stage2_system_prompt(
    *,
    tail_anchor: bool = False,
    include_first_frame: bool = False,
    include_scene_layout: bool = False,
) -> str:
    """Build Stage 2 full Seedance 2.0/2.5 continuous-mode prompt instructions from YD分镜skills."""

    from novelvideo.skills.yd_storyboard_skill import build_yd_stage2_directives

    return build_yd_stage2_directives(
        tail_anchor=tail_anchor,
        include_first_frame=include_first_frame,
        include_scene_layout=include_scene_layout,
    )


def build_s9_stage2_task_prompt(
    *,
    segment: S9SegmentPlan,
    slots: list[S9SegmentAssetSlot],
    style_context: S9StyleContext,
    prev_segment: S9SegmentPlan | None = None,
    next_segment: S9SegmentPlan | None = None,
    prev_episode_context: str = "",
    user_guidance: str = "",
    source_text: str = "",
    current_prompt: str = "",
    scene_context_notes: str = "",
    continuity_anchor: str = "",
) -> str:
    """Build Stage 2 user prompt for compiling one 大段 into a full Seedance prompt."""

    shots_lines: list[str] = []
    for shot in segment.shots:
        start_s = _format_seconds_label(shot.start_seconds)
        end_s = _format_seconds_label(shot.end_seconds)
        spoken_note = (
            f" | [{shot.audio_type}] 说话人={shot.speaker or '旁白'} | 原句: “{shot.spoken_text}”"
            if shot.spoken_text
            else f" | [{shot.audio_type}]"
        )
        extra_parts: list[str] = []
        if getattr(shot, "sfx_cue", ""):
            extra_parts.append(f"拟音/声场：{shot.sfx_cue}")
        if getattr(shot, "transition_note", ""):
            extra_parts.append(f"切出与衔接：{shot.transition_note}")
        extra_line = f"\n  {' | '.join(extra_parts)}" if extra_parts else ""
        shots_lines.append(
            f"- {shot.label or f'分镜{shot.shot_number}'}（{start_s}秒到{end_s}秒）《{shot.title}》\n"
            f"  基础信息：计划{_format_seconds_label(shot.planned_duration_seconds)}秒｜"
            f"{shot.shot_scale_movement}｜{shot.camera_angle}｜{shot.camera_motion}{spoken_note}\n"
            f"  分镜内容：{shot.content}{extra_line}"
        )

    continuity_parts: list[str] = []
    if prev_segment is not None:
        prev_tail = str(getattr(prev_segment, "exit_state", "") or "").strip()
        continuity_parts.append(
            f"- 前一大段（大段{prev_segment.segment_number}·{prev_segment.title}）结尾事件：{prev_segment.story_beat}"
            + (f"｜结尾状态：{prev_tail}" if prev_tail else "")
        )
        prev_shots = list(getattr(prev_segment, "shots", []) or [])
        if prev_shots:
            prev_last = prev_shots[-1]
            prev_shot_label = prev_last.label or f"大段{prev_segment.segment_number}-分镜{prev_last.shot_number}"
            prev_shot_parts = [
                f"{prev_shot_label}｜{prev_last.shot_scale_movement}｜{prev_last.camera_angle}｜{prev_last.camera_motion}",
                str(prev_last.content or "").strip(),
            ]
            if str(getattr(prev_last, "transition_note", "") or "").strip():
                prev_shot_parts.append(f"切出余势：{prev_last.transition_note.strip()}")
            if str(getattr(prev_last, "spoken_text", "") or "").strip():
                prev_shot_parts.append(f"末镜原话：“{prev_last.spoken_text.strip()}”")
            continuity_parts.append(
                "- 前段末镜实拍快照："
                + "；".join(p for p in prev_shot_parts if p)
                + "\n  衔接指令：本段第1镜必须顺接上述结尾状态与动作余势，除非有明确连续表演依据否则切换景别/机位推进至新动作，严禁复演上段末镜已完成的台词或动作。"
            )
    elif prev_episode_context.strip():
        continuity_parts.append(f"- 上一集末尾语境：{prev_episode_context.strip()}")
    if getattr(segment, "entry_state", "").strip():
        continuity_parts.append(f"- 本段第0秒起始画面状态：{segment.entry_state.strip()}")
    if getattr(segment, "exit_state", "").strip():
        continuity_parts.append(
            f"- 本段末镜必达收束状态（必须完整拍完并止于此）：{segment.exit_state.strip()}"
        )
    if next_segment is not None:
        next_entry = str(getattr(next_segment, "entry_state", "") or "").strip()
        continuity_parts.append(
            f"- 下一大段（大段{next_segment.segment_number}·{next_segment.title}）即将承接：{next_segment.story_beat}"
            + (f"｜下段起始状态：{next_entry}" if next_entry else "")
        )
        next_shots = list(getattr(next_segment, "shots", []) or [])
        if next_shots:
            next_first = next_shots[0]
            next_shot_label = next_first.label or f"大段{next_segment.segment_number}-分镜{next_first.shot_number}"
            next_shot_parts = [
                f"{next_shot_label}｜{next_first.shot_scale_movement}｜{next_first.camera_angle}｜{next_first.camera_motion}",
                str(next_first.content or "").strip(),
            ]
            if str(getattr(next_first, "spoken_text", "") or "").strip():
                next_shot_parts.append(f"下段首句：“{next_first.spoken_text.strip()}”")
            continuity_parts.append(
                "- 下段首镜待接快照："
                + "；".join(p for p in next_shot_parts if p)
                + "\n  边界指令：本段末镜必须完整落定收束状态，严禁提前越界拍出下一大段首镜的内容或台词。"
            )
    continuity_block = "\n".join(continuity_parts) if continuity_parts else "- 本段独立完成当前闭环"

    layout_block = (
        f"\n【已规划场景空间蓝图】\n{segment.scene_layout.strip()}\n"
        if str(getattr(segment, "scene_layout", "") or "").strip()
        else ""
    )

    source_block = (
        f"\n【本集原稿参考（完整版须从原稿复核台词原字、知情与表演细节，严禁演到下一大段）】\n{source_text.strip()}\n"
        if str(source_text or "").strip()
        else ""
    )

    guidance_block = (
        f"\n【用户附加微调要求（必须融入本段执行方案）】\n{user_guidance.strip()}\n"
        if user_guidance.strip()
        else ""
    )

    scene_consistency_parts = [
        p.strip()
        for p in (scene_context_notes, continuity_anchor)
        if str(p or "").strip()
    ]
    scene_consistency_block = (
        "\n【场景锁图光、空间地标与跨段道具一致性铁律（严禁擅自改动）】\n"
        + "\n".join(scene_consistency_parts)
        + "\n"
        if scene_consistency_parts
        else ""
    )

    current_clean = str(current_prompt or "").strip()
    current_block = ""
    if current_clean:
        draft_issues = _audit_s9_stage2_completeness(
            current_clean,
            segment=segment,
            slots=slots,
        )
        if draft_issues:
            current_block = (
                "\n【当前待补全与优化的提示词草稿（检测到内容不完整）】\n"
                f"{current_clean}\n\n"
                "【补全与优化指令】\n"
                f"检测到当前草稿存在未完成问题：{'；'.join(draft_issues)}。\n"
                f"请保留草稿中合理的镜头质感与已写设定，**完整补齐所有缺失栏目、缺失分镜与未写完句子**，"
                f"确保完整覆盖从 0秒 到 {segment.planned_duration_seconds}秒 的全部 {max(1, len(segment.shots))} 个分镜，"
                "严禁中途截断、省略或只输出片段。\n"
            )
        else:
            current_block = (
                "\n【当前版本提示词（用户点击「AI 优化」的基准稿）】\n"
                f"{current_clean}\n\n"
                "【AI 优化指令】\n"
                "用户主动发起了「AI 优化」。请在保留逐字台词原文、正确 @图片N/@音频N 绑定、"
                f"用户手动修改意图以及完整 0秒—{segment.planned_duration_seconds}秒（共 {max(1, len(segment.shots))} 镜）结构的前提下，"
                "进行**实质性的导演级精修升级**（不要原封不动照抄原文）：\n"
                "1. **强制纠偏旧稿冲突（最高优先）**：若基准稿中的 `@图片N`/`@音频N` 编号与上方【本段可用素材索引表】不一致或存在空缺引用（如 `- ：`），"
                "或者基准稿的光影时段/虚构灯具、门窗方位、道具颜色或遮挡位置与上方【场景锁图光、空间地标与跨段道具一致性铁律】冲突，"
                "必须直接按最新索引表与铁律推翻并修正旧稿，严禁沿用旧错误；\n"
                "2. 若上方存在【用户附加微调要求】，优先落实用户指定维度（主体/场景/光影/镜头/风格/无字幕等）；\n"
                "3. 细化镜内亚区间动态时间轴中的眼神落点、微表情时程错峰、身体重心传导与道具拿放最终归属；\n"
                "4. 强化机位动机、景别转换与物理光影/贴动作拟音细节，删去空泛套话，完整输出到最后一镜结束。\n"
            )

    return f"""请为「大段{segment.segment_number}：{segment.title}」（计划总时长 **{segment.planned_duration_seconds}秒**，必须在这 {segment.planned_duration_seconds} 秒内讲完本段故事闭环、零空等拖秒）生成完整的 Seedance 2.0/2.5 生成提示词：

【项目视觉与画风要求】
{style_context.s9_style_anchor}

【本段可用素材索引表（仅允许使用下列编号）】
{format_slot_table_for_stage2(slots)}

【上下文衔接要求】
{continuity_block}
{layout_block}
【本大段叙事核心】
{segment.story_beat}
{scene_consistency_block}{source_block}
【本大段内部各分镜规划（时间轴共 {segment.planned_duration_seconds} 秒）】
{chr(10).join(shots_lines)}
{guidance_block}{current_block}
请立即直接输出纯净的 Stage 2 完整生成提示词文本（严格执行 YD分镜skills 的参考职责、全局光影画风、适用场景布局、注意事项、角色声音与分亚区间动态时间轴表演写法，禁止出现任何代码字段名）。"""


def _resolve_voice_slot_ref(
    speaker_tag: str,
    voice_slot_by_id: dict[str, str],
    voice_slot_by_name: dict[str, str],
) -> str:
    clean = str(speaker_tag or "").strip()
    if not clean:
        return ""
    return voice_slot_by_id.get(clean) or voice_slot_by_name.get(
        clean.split("_", 1)[0].lower(), ""
    )


def _format_fallback_audio_desc(
    *,
    shot: S9SubShot,
    start_s: str,
    end_s: str,
    voice_slot_by_id: dict[str, str],
    voice_slot_by_name: dict[str, str],
) -> str:
    sfx_raw = str(getattr(shot, "sfx_cue", "") or "").strip()
    sfx_part = f"；{sfx_raw}" if sfx_raw else "；伴随贴动作的真实物理拟音与自然环境底噪"
    if not shot.spoken_text:
        return f"纯环境写实拟音与呼吸声{sfx_part if sfx_raw else ''}。"

    speaker_raw = str(shot.speaker or "角色").strip()
    if shot.audio_type == "narration":
        return (
            f"{start_s}秒到{end_s}秒，{speaker_raw}画外音/内心独白（贴合当下呼吸与节奏）：“{shot.spoken_text}”"
            f"（画面人物嘴唇闭合，继续既定动作）{sfx_part}。"
        )

    turns = [t.strip() for t in str(shot.spoken_text).split("|") if t.strip()]
    speakers = [s.strip() for s in re.split(r"[;；、/]+", speaker_raw) if s.strip()]
    if len(turns) <= 1:
        voice_ref = _resolve_voice_slot_ref(speaker_raw, voice_slot_by_id, voice_slot_by_name)
        ref_part = f"（{voice_ref}音色，自然吐字）" if voice_ref else "（自然吐字）"
        return (
            f"{start_s}秒到{end_s}秒，{speaker_raw}画内开口{ref_part}：“{shot.spoken_text}”"
            f"，说话者唇形咬字精准同步{sfx_part}。"
        )

    # Multi-turn dialogue inside one shot: partition into contiguous integer sub-windows.
    start_i = int(round(float(shot.start_seconds or 0.0)))
    end_i = max(start_i + 1, int(round(float(shot.end_seconds or start_i + 1))))
    total_i = max(1, end_i - start_i)
    weights = [max(1.0, _estimate_spoken_seconds(t)) for t in turns]
    weight_sum = sum(weights) or float(len(turns))
    cursor = start_i
    turn_parts: list[str] = []
    for idx, turn_text in enumerate(turns):
        spk = speakers[idx] if idx < len(speakers) else speakers[-1]
        vref = _resolve_voice_slot_ref(spk, voice_slot_by_id, voice_slot_by_name)
        ref_part = f"（{vref}音色，自然吐字）" if vref else "（自然吐字）"
        if idx == len(turns) - 1:
            t_end = end_i
        else:
            span = max(1, int(round(total_i * (weights[idx] / weight_sum))))
            t_end = min(end_i - (len(turns) - 1 - idx), cursor + span)
        turn_parts.append(f"{cursor}秒—{t_end}秒，{spk}画内开口{ref_part}：“{turn_text}”")
        cursor = t_end
    return "；".join(turn_parts) + f"，说话者按轮次唇形咬字精准同步{sfx_part}。"


def _build_fallback_dynamic_timeline(
    *,
    shot: S9SubShot,
    next_hint: str,
) -> str:
    start_i = int(round(float(shot.start_seconds or 0.0)))
    end_i = max(start_i + 1, int(round(float(shot.end_seconds or start_i + 1))))
    dur_i = end_i - start_i
    clauses = [
        c.strip()
        for c in re.split(r"[。；！!？?]+", str(shot.content or "").strip())
        if c.strip()
    ]
    lead_action = clauses[0] if clauses else "承接前态起幅并切入本镜主体动作"
    mid_action = "；".join(clauses[1:]) if len(clauses) > 1 else lead_action
    end_action = next_hint.rstrip("。")

    if dur_i <= 2:
        return (
            f"{start_i}秒—{end_i}秒，紧接起幅完成关键动作与神态落点（{lead_action}），"
            f"余势衔接至{end_action}。"
        )
    if dur_i <= 5:
        split_1 = start_i + max(1, dur_i // 2)
        return (
            f"{start_i}秒—{split_1}秒，起态承接后立刻推进核心动作与视线触发（{lead_action}）；"
            f"{split_1}秒—{end_i}秒，完成当前交互落点与情绪反馈（{mid_action}），余势顺延切出（{end_action}）。"
        )
    split_1 = start_i + max(1, round(dur_i * 0.3))
    split_2 = min(end_i - 1, max(split_1 + 1, start_i + round(dur_i * 0.75)))
    return (
        f"{start_i}秒—{split_1}秒，承接前态建立主体站位与起势（{lead_action}）；"
        f"{split_1}秒—{split_2}秒，紧接着展开核心交互、台词咬字与手部/视线传导（{mid_action}）；"
        f"{split_2}秒—{end_i}秒，道具归属与动作稳妥落定（{end_action}）。"
    )


def build_s9_stage2_fallback_prompt(
    *,
    segment: S9SegmentPlan,
    slots: list[S9SegmentAssetSlot],
    style_context: S9StyleContext,
    tail_anchor: bool = False,
) -> str:
    """Deterministic fallback builder matching YD分镜skills full-output structure."""

    duty_lines: list[str] = []
    voice_lines: list[str] = []
    for s in slots:
        if s.media_type == "image":
            if s.asset_kind == "first_frame":
                duty_lines.append(
                    f"@图片{s.slot} 是开篇首帧的构图与起始状态参考：锁定第0秒构图、光线与人物起始姿态，首镜动作自该画面状态向后连续发生。"
                )
            elif s.asset_kind == "identity":
                duty_lines.append(
                    f"@图片{s.slot} 是角色{s.name}的形象参考：锁定脸型五官、发型与核心服饰特征；隔离设定板纯色背景与静态站姿，维持角色动态表演。"
                )
            elif s.asset_kind == "scene":
                duty_lines.append(
                    f"@图片{s.slot} 是场景{s.name}正面参考：锁定空间结构、陈设材质与环境光色；仅作空间与光线基准，不冻结镜头运镜。"
                )
            elif s.asset_kind == "scene_reverse":
                duty_lines.append(
                    f"@图片{s.slot} 是场景{s.name}背面/入户侧参考：锁定反打方向门窗结构、左右位置与同场光色；拍摄门/玄关/反打方向时严格按此图方位执行。"
                )
            else:
                duty_lines.append(
                    f"@图片{s.slot} 是道具{s.name}的结构与状态参考：锁定外形、比例与当前特定状态；不继承未指定持握姿态。"
                )
        else:
            duty_lines.append(
                f"@音频{s.slot} 是角色{s.name}的参考音色，仅参考核心音色（不迁移原样本台词、混响与环境噪声）。"
            )
            voice_lines.append(f"- {s.name}：绑定@音频{s.slot}稳定声线，保持该角色常态语速与咬字特征。")

    voice_slot_by_name = {
        s.name.lower(): f"@音频{s.slot}" for s in slots if s.media_type == "audio"
    }
    voice_slot_by_id = {
        s.asset_id: f"@音频{s.slot}" for s in slots if s.media_type == "audio" and s.asset_id
    }

    shot_blocks: list[str] = []
    for idx, shot in enumerate(segment.shots):
        start_s = _format_seconds_label(shot.start_seconds)
        end_s = _format_seconds_label(shot.end_seconds)
        audio_desc = _format_fallback_audio_desc(
            shot=shot,
            start_s=start_s,
            end_s=end_s,
            voice_slot_by_id=voice_slot_by_id,
            voice_slot_by_name=voice_slot_by_name,
        )
        default_next = (
            "动作余势自然延续并切入下一分镜，严禁复位或重演。"
            if idx + 1 < len(segment.shots)
            else (
                f"末镜结束落定：{segment.exit_state or '人物动作与视线稳妥落点，承接下一大段第0秒起态。'}"
                if not tail_anchor
                else "末段动作落定后保持1秒静止静音稳态构图，供下段参考。"
            )
        )
        next_hint = str(getattr(shot, "transition_note", "") or "").strip() or default_next
        timeline_desc = _build_fallback_dynamic_timeline(shot=shot, next_hint=next_hint)
        shot_blocks.append(
            f"分镜{shot.shot_number}（{start_s}秒到{end_s}秒）：{shot.title or f'镜{shot.shot_number}'}，{shot.shot_scale_movement}，{shot.camera_angle}，{shot.camera_motion}。\n"
            f"- 空间与主体：{shot.content}\n"
            f"- 动态时间轴：{timeline_desc}\n"
            f"- 音频与对白：{audio_desc}\n"
            f"- 尾帧与转场：{next_hint}"
        )

    is_3d_cg = style_context.style_family == "animation" and "3d" in style_context.animation_subtype.lower()
    if is_3d_cg:
        visual_style_line = (
            f"- 视觉风格：三维CG，写实渲染，{style_context.style_label}（{style_context.style_instructions or '全局光照与物理反射，扎实的重力形变与织物垂坠，自然景深与真实物理运动反馈'}）。"
        )
    elif style_context.style_family == "animation":
        visual_style_line = (
            f"- 视觉风格：动画媒介，{style_context.style_label}（{style_context.style_instructions or '明确线稿与色块阴影层次，流畅动画运动节奏与受光衰减'}）。"
        )
    else:
        visual_style_line = (
            f"- 视觉风格：真人电影实拍成像，{style_context.style_label}（{style_context.style_instructions or '场景实际光源自然衰减，皮肤与织物真实纹理，匹配镜距景深与重力惯性反馈'}）。"
        )

    stop_clause = (
        f"\n本输入止于：{segment.exit_state.strip()}，严禁提前演绎下一大段事件。"
        if str(getattr(segment, "exit_state", "") or "").strip()
        else ""
    )
    sections = [
        "【参考职责】\n" + ("\n".join(duty_lines) if duty_lines else "无参考纯文字演绎：严格按文字描述维持人物造型、空间几何与声线一致。"),
        f"【全局光影画风】\n"
        f"{visual_style_line}\n"
        f"- 全局主光：主辅光源方向固定，明暗过渡与物体材质反光符合场景实际照明。\n"
        f"- 色彩与材质：保持 {style_context.aspect_ratio} 统一色彩调性，突出人物皮肤/衣料与器物材质细节。",
    ]
    if _segment_needs_scene_layout(segment):
        layout_text = str(getattr(segment, "scene_layout", "") or "").strip() or (
            "保持同场景固定地标与人物屏幕左右锚位一致，摄影机留在关系轴同一工作侧，反打不越轴。"
        )
        sections.append(f"【场景布局】\n{layout_text}")
    sections.extend(
        [
            "【注意事项】\n"
            "画面全域禁止生成任何硬字幕、文字条或水印。音轨禁止添加任何BGM音乐，声音仅由角色人声台词配音、物理拟音SFX与环境自然底噪构成。"
            "有画内说话动作的分镜匹配该说者唇形咬字（Lip-sync）。旁白和内心独白不驱动画面人物逐字对口型，人物继续既定动作与自然表演。"
            "动作与面部变化保持连续，不要表情突变、面部变形、身份漂移或姿态复位。"
            + stop_clause,
            "【角色声音】\n"
            + ("\n".join(voice_lines) if voice_lines else "- 本段以画外内心独白与现场真实物理拟音为主，未出场说话人不追加声线。"),
            "\n\n".join(shot_blocks),
        ]
    )
    if tail_anchor:
        sections.append(
            "【尾帧锚点镜】\n"
            "最后1秒采用四分之三侧面全景固定无运镜，画面中人物与所有环境要素完全静止承接末镜瞬间构图，音轨完全静音。"
        )
    return "\n\n".join(sections)


_STAGE1_DEFAULT_MAX_TOKENS: int | None = None
_STAGE2_DEFAULT_MAX_TOKENS = 8192
_STAGE2_MAX_CONTINUATIONS = 2
_STAGE2_TERMINAL_CHARS = frozenset("。！？.!?'\"”’」』）)]】~…")
_DEFAULT_S9_LLM_CONCURRENCY = 8


def get_s9_llm_concurrency() -> int:
    """Return bounded LLM concurrency for parallel S9 Stage 1B and Stage 2 compilation."""

    raw = str(os.environ.get("S9_LLM_CONCURRENCY") or "").strip()
    if raw:
        try:
            return max(1, min(16, int(raw)))
        except ValueError:
            pass
    return _DEFAULT_S9_LLM_CONCURRENCY


_BACKTICK_MENTION_RE = re.compile(r"`+(@(?:图片|音频)\d+)`+")


def _strip_code_fences(text: str) -> str:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    cleaned = _BACKTICK_MENTION_RE.sub(r"\1", cleaned)
    return cleaned.strip()


def _audit_s9_stage2_completeness(
    text: str,
    *,
    segment: S9SegmentPlan,
    slots: list[S9SegmentAssetSlot],
    finish_reason: str | None = None,
) -> list[str]:
    """Verify a Stage 2 prompt is not truncated and covers every planned shot & timeline end."""

    issues: list[str] = []
    if finish_reason == "length":
        issues.append("输出达到 token 上限被截断")
    cleaned = str(text or "").strip()
    if not cleaned:
        return [*issues, "输出为空"]

    terminal_check = cleaned.rstrip("*` \t\r\n") or cleaned
    if terminal_check[-1] not in _STAGE2_TERMINAL_CHARS:
        issues.append(f"输出在句中意外中断（结尾字符：{terminal_check[-1]!r}）")

    if slots and "【参考职责】" not in cleaned:
        issues.append("缺少【参考职责】说明")
    if "【全局光影画风】" not in cleaned:
        issues.append("缺少【全局光影画风】")
    if "【注意事项】" not in cleaned:
        issues.append("缺少【注意事项】")

    missing_shots: list[str] = []
    for shot in segment.shots or []:
        marker = f"分镜{shot.shot_number}"
        if marker not in cleaned and (not shot.label or shot.label not in cleaned):
            missing_shots.append(marker)
    if missing_shots:
        issues.append(f"缺少分镜：{'、'.join(missing_shots)}")

    if segment.shots:
        last_end = _format_seconds_label(segment.shots[-1].end_seconds)
        planned_end = str(int(segment.planned_duration_seconds or 0))
        end_candidates = {f"{last_end}秒", f"{last_end}s"}
        if planned_end and planned_end != "0":
            end_candidates.update({f"{planned_end}秒", f"{planned_end}s"})
        if not any(tok in cleaned for tok in end_candidates):
            issues.append(f"时间轴未覆盖至末镜结束时间 {last_end}秒")

    return issues


def _merge_stage2_continuation(prefix: str, continuation: str) -> str:
    """Concatenate a truncated Stage 2 prefix with a model continuation seamlessly."""

    head = str(prefix or "").rstrip()
    tail = _strip_code_fences(continuation)
    if not head:
        return tail
    if not tail:
        return head

    max_overlap = min(len(head), len(tail), 120)
    for overlap in range(max_overlap, 0, -1):
        if head.endswith(tail[:overlap]):
            tail = tail[overlap:]
            break
    if not tail:
        return head

    if tail[0] in {"分", "【", "-"} and head[-1] in _STAGE2_TERMINAL_CHARS:
        return f"{head}\n{tail}"
    return f"{head}{tail}"


def _s9_model_settings(
    model_env: str,
    *,
    default_max_tokens: int | None,
    reasoning_override_env: str = "",
    max_tokens_override_env: str = "",
) -> dict[str, Any]:
    """Build S9 model settings with optional max_tokens floor and stage effort override."""

    from novelvideo.config import get_newapi_structured_output_model_settings
    from novelvideo.utils.model_context_budget import TextContextBudget

    settings = dict(get_newapi_structured_output_model_settings(model_env))
    if reasoning_override_env:
        override = str(os.environ.get(reasoning_override_env) or "").strip().lower()
        if override == "off":
            settings.pop("openai_reasoning_effort", None)
        elif override in {"high", "medium", "low", "minimal"}:
            settings["openai_reasoning_effort"] = override
    if TextContextBudget.from_env() is None:
        env_max_tokens: int | None = None
        if max_tokens_override_env:
            raw_mt = str(os.environ.get(max_tokens_override_env) or "").strip()
            if raw_mt:
                try:
                    parsed_mt = int(raw_mt)
                    if parsed_mt > 0:
                        env_max_tokens = parsed_mt
                except ValueError:
                    pass
        target_max = env_max_tokens if env_max_tokens is not None else default_max_tokens
        if target_max is not None:
            settings.setdefault("max_tokens", target_max)
    return settings


def _s9_stage1_model_settings(model_env: str = "LITERAL_BEAT_META_MODEL") -> dict[str, Any]:
    """Build Stage 1 model settings without imposing an artificial 8192 token ceiling."""

    return _s9_model_settings(
        model_env,
        default_max_tokens=_STAGE1_DEFAULT_MAX_TOKENS,
        reasoning_override_env="S9_STAGE1_REASONING_EFFORT",
        max_tokens_override_env="S9_STAGE1_MAX_TOKENS",
    )


def _s9_stage2_model_settings(model_env: str) -> dict[str, Any]:
    """Build Stage 2 model settings with an explicit completion token floor."""

    return _s9_model_settings(
        model_env,
        default_max_tokens=_STAGE2_DEFAULT_MAX_TOKENS,
        reasoning_override_env="S9_STAGE2_REASONING_EFFORT",
        max_tokens_override_env="S9_STAGE2_MAX_TOKENS",
    )


_SLICE_NORM_RE = re.compile(r"[^\w\u4e00-\u9fff]+")
_SLICE_ELLIPSIS_RE = re.compile(r"^[.…·\-\s]+$")


def _normalize_slice_line(text: str) -> str:
    raw = str(text or "").strip()
    if not raw or _SLICE_ELLIPSIS_RE.match(raw):
        return ""
    return _SLICE_NORM_RE.sub("", raw).lower()


def _line_matches_excerpt_token(
    source_norm: str,
    excerpt_norm: str,
    *,
    allow_fuzzy: bool = True,
) -> bool:
    if not source_norm or not excerpt_norm:
        return False
    if source_norm == excerpt_norm:
        return True
    if len(excerpt_norm) >= 4 and (excerpt_norm in source_norm or source_norm in excerpt_norm):
        return True
    if not allow_fuzzy:
        return False
    shorter, longer = (
        (source_norm, excerpt_norm)
        if len(source_norm) <= len(excerpt_norm)
        else (excerpt_norm, source_norm)
    )
    if len(shorter) >= 6:
        shared = sum(1 for ch in set(shorter) if ch in longer)
        if shared / max(1, len(set(shorter))) >= 0.75:
            return True
    return False


def _find_line_range_in_source(
    source_norms: list[str],
    excerpt_norms: list[str],
    *,
    search_from: int,
) -> tuple[int, int] | None:
    if not excerpt_norms or search_from >= len(source_norms):
        return None
    first_tok = excerpt_norms[0]
    last_tok = excerpt_norms[-1]
    start_idx: int | None = None
    for allow_fuzzy in (False, True):
        for i in range(search_from, len(source_norms)):
            if _line_matches_excerpt_token(source_norms[i], first_tok, allow_fuzzy=allow_fuzzy):
                start_idx = i
                break
        if start_idx is not None:
            break
    if start_idx is None and len(excerpt_norms) > 1:
        for tok in excerpt_norms[1:]:
            for allow_fuzzy in (False, True):
                for i in range(search_from, len(source_norms)):
                    if _line_matches_excerpt_token(source_norms[i], tok, allow_fuzzy=allow_fuzzy):
                        start_idx = i
                        break
                if start_idx is not None:
                    break
            if start_idx is not None:
                break
    if start_idx is None:
        return None

    end_idx = start_idx + 1
    if len(excerpt_norms) == 1:
        return start_idx, end_idx

    # When an excerpt uses anchor pairs (<=3 non-ellipsis lines), scan forward after start_idx
    # prioritizing exact matches before fuzzy matches so multi-line ranges expand cleanly.
    if len(excerpt_norms) <= 3:
        min_j = start_idx + 1 if start_idx + 1 < len(source_norms) else start_idx
        for allow_fuzzy in (False, True):
            for j in range(min_j, len(source_norms)):
                if _line_matches_excerpt_token(source_norms[j], last_tok, allow_fuzzy=allow_fuzzy):
                    return start_idx, j + 1

    search_limit = min(len(source_norms), start_idx + max(len(excerpt_norms) * 3, 24))
    for allow_fuzzy in (False, True):
        for j in range(search_limit - 1, start_idx - 1, -1):
            if _line_matches_excerpt_token(source_norms[j], last_tok, allow_fuzzy=allow_fuzzy):
                return start_idx, j + 1
    for tok in reversed(excerpt_norms[:-1]):
        for allow_fuzzy in (False, True):
            matched_j = next(
                (
                    j
                    for j in range(search_limit - 1, start_idx - 1, -1)
                    if _line_matches_excerpt_token(source_norms[j], tok, allow_fuzzy=allow_fuzzy)
                ),
                None,
            )
            if matched_j is not None:
                return start_idx, matched_j + 1
    return start_idx, max(start_idx + 1, end_idx)


def _format_contextual_slice(source_lines: list[str], start: int, end: int) -> str:
    parts: list[str] = []
    if start > 0:
        prev_ctx = "\n".join(source_lines[max(0, start - 2) : start])
        parts.append(f"[前段末尾邻接参考（已拍完，严禁重演）]\n{prev_ctx}")
    core_text = "\n".join(source_lines[start:end])
    parts.append(f"[本大段核心原文（必须100%忠实拍完，逐字保留台词）]\n{core_text}")
    if end < len(source_lines):
        next_ctx = "\n".join(source_lines[end : min(len(source_lines), end + 2)])
        parts.append(f"[后段开头邻接参考（属下一大段，严禁提前拍摄）]\n{next_ctx}")
    return "\n\n".join(parts)


def slice_s9_source_text_for_segments(
    source_text: str,
    skeletons: list[S9Stage1SkeletonSegment],
) -> tuple[list[str], list[str]]:
    """Map Pass 1A segment excerpts onto contiguous non-dropping slices of ``source_text``."""

    raw_text = str(source_text or "").strip()
    count = len(skeletons)
    if count == 0:
        return [], []
    source_lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    if count == 1 or len(source_lines) <= 6:
        excerpts = [str(sk.source_excerpt or "").strip() or raw_text for sk in skeletons]
        return excerpts, [raw_text] * count

    source_norms = [_normalize_slice_line(ln) for ln in source_lines]
    matched_spans: list[tuple[int, int] | None] = []
    cursor = 0
    matched_count = 0
    for sk in skeletons:
        raw_excerpt = str(sk.source_excerpt or "").strip()
        excerpt_lines = [ln.strip() for ln in raw_excerpt.splitlines() if ln.strip()]
        if not excerpt_lines and sk.story_beat:
            excerpt_lines = [sk.story_beat]
        excerpt_norms = [n for ln in excerpt_lines if (n := _normalize_slice_line(ln))]
        span = _find_line_range_in_source(source_norms, excerpt_norms, search_from=cursor)
        matched_spans.append(span)
        if span is not None:
            matched_count += 1
            cursor = min(len(source_lines), span[1])

    if matched_count * 2 < count:
        excerpts = [str(sk.source_excerpt or "").strip() or raw_text for sk in skeletons]
        return excerpts, [raw_text] * count

    # Repair spans into a strictly contiguous partition covering 100% of [0, len(source_lines)).
    resolved_starts: list[int] = [0] * count
    resolved_ends: list[int] = [len(source_lines)] * count
    for idx, span in enumerate(matched_spans):
        if span is not None:
            resolved_starts[idx], resolved_ends[idx] = span
        else:
            prev_end = resolved_ends[idx - 1] if idx > 0 else 0
            next_start = next(
                (matched_spans[j][0] for j in range(idx + 1, count) if matched_spans[j] is not None),
                len(source_lines),
            )
            resolved_starts[idx] = prev_end
            resolved_ends[idx] = max(prev_end, next_start)

    resolved_starts[0] = 0
    for idx in range(count - 1):
        cur_end = max(resolved_starts[idx] + 1 if resolved_starts[idx] < len(source_lines) else resolved_starts[idx], resolved_ends[idx])
        next_start = resolved_starts[idx + 1]
        if cur_end < next_start:
            # Absorb unmapped gap lines into current segment so nothing is lost.
            cur_end = next_start
        elif cur_end > next_start:
            next_start = cur_end
        resolved_ends[idx] = min(len(source_lines), cur_end)
        resolved_starts[idx + 1] = min(len(source_lines), next_start)
    resolved_ends[-1] = len(source_lines)

    # Guarantee non-empty slices where lines exist.
    clean_excerpts: list[str] = []
    context_slices: list[str] = []
    for idx in range(count):
        s, e = resolved_starts[idx], resolved_ends[idx]
        if s >= e:
            clean_excerpts.append(str(skeletons[idx].source_excerpt or "").strip() or raw_text)
            context_slices.append(raw_text)
        else:
            clean_excerpts.append("\n".join(source_lines[s:e]))
            context_slices.append(_format_contextual_slice(source_lines, s, e))
    return clean_excerpts, context_slices


def build_s9_context_slice_from_episode_text(
    episode_source_text: str,
    segment_excerpt: str,
) -> str:
    """Extract contextual slice for one segment from full episode text when available."""

    full_text = str(episode_source_text or "").strip()
    excerpt = str(segment_excerpt or "").strip()
    if not full_text or not excerpt or len(full_text) <= 300:
        return full_text
    source_lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
    if len(source_lines) <= 6:
        return full_text
    excerpt_lines = [ln.strip() for ln in excerpt.splitlines() if ln.strip()]
    excerpt_norms = [n for ln in excerpt_lines if (n := _normalize_slice_line(ln))]
    source_norms = [_normalize_slice_line(ln) for ln in source_lines]
    span = _find_line_range_in_source(source_norms, excerpt_norms, search_from=0)
    if span is None:
        return full_text
    return _format_contextual_slice(source_lines, span[0], span[1])


def enrich_s9_continuity_bible(
    bible: S9EpisodeContinuityBible | None,
    *,
    segments: list[S9SegmentPlan],
    per_segment_slots: list[list[S9SegmentAssetSlot]],
    style_context: S9StyleContext,
    scenes: list[Any] | None = None,
) -> S9EpisodeContinuityBible:
    """Fill missing per-scene lighting/layout rules deterministically from scene cards."""

    from novelvideo.models import build_scene_effective_prompt

    result = (bible or S9EpisodeContinuityBible()).model_copy(deep=True)
    scene_lookup = _scene_record_lookup(scenes)

    lighting_by_sid: dict[str, S9SceneLightingAnchor] = {}
    for item in result.scene_lighting_rules:
        sid, vid = _normalize_scene_id_variant(item.scene_id, "")
        key = f"{sid}_{vid}" if vid else sid
        if key:
            lighting_by_sid[key] = item

    layout_by_sid: dict[str, S9SceneLayoutAnchor] = {}
    for item in result.scene_layout_rules:
        sid, vid = _normalize_scene_id_variant(item.scene_id, "")
        key = f"{sid}_{vid}" if vid else sid
        if key:
            layout_by_sid[key] = item

    scene_usage_counts: dict[str, int] = {}
    ordered_plates: list[tuple[str, str, str]] = []
    for slots in per_segment_slots:
        seg_plates: set[str] = set()
        for slot in slots or []:
            if slot.asset_kind == "scene" and slot.asset_id:
                sid, vid = _normalize_scene_id_variant(slot.asset_id, slot.variant_id)
                plate = f"{sid}_{vid}" if vid else sid
                if plate not in seg_plates:
                    seg_plates.add(plate)
                    scene_usage_counts[plate] = scene_usage_counts.get(plate, 0) + 1
                if all(p != plate for p, _, _ in ordered_plates):
                    ordered_plates.append((plate, sid, vid))

    for plate, sid, vid in ordered_plates:
        _pk, scene_obj, base_obj = _resolve_scene_and_base(scene_lookup, sid, vid)
        if plate not in lighting_by_sid and sid not in lighting_by_sid:
            env_prompt = (
                build_scene_effective_prompt(scene_obj, base_obj).strip()
                if scene_obj is not None
                else ""
            )
            sc_time = str(getattr(scene_obj, "time_of_day", "") or "").strip() if scene_obj else ""
            rule = S9SceneLightingAnchor(
                scene_id=plate,
                time_of_day=sc_time,
                visual_style_summary=style_context.s9_style_anchor,
                key_lighting=(
                    f"严格锁定场景「{plate}」参考图原有主光方向与时段"
                    + (f"（{sc_time}）" if sc_time else "")
                    + (f"，空间光照参考：{env_prompt[:140]}" if env_prompt else "")
                ),
                color_and_material=f"保持 {style_context.aspect_ratio} {style_context.style_label} 统一色温与材质质感，跨段严禁改变主光色温",
            )
            result.scene_lighting_rules.append(rule)
            lighting_by_sid[plate] = rule

        if plate not in layout_by_sid and sid not in layout_by_sid:
            env_prompt = (
                build_scene_effective_prompt(scene_obj, base_obj).strip()
                if scene_obj is not None
                else ""
            )
            notes = str(getattr(scene_obj, "notes", "") or "").strip() if scene_obj else ""
            first_seg_layout = ""
            for seg_idx, seg in enumerate(segments):
                seg_slots = per_segment_slots[seg_idx] if seg_idx < len(per_segment_slots) else []
                if any(
                    s.asset_kind == "scene" and s.asset_id and (
                        f"{_normalize_scene_id_variant(s.asset_id, s.variant_id)[0]}" in plate
                    )
                    for s in seg_slots
                ):
                    if str(getattr(seg, "scene_layout", "") or "").strip():
                        first_seg_layout = seg.scene_layout.strip()
                        break
            if env_prompt or notes or first_seg_layout or scene_usage_counts.get(plate, 0) > 1:
                layout_rule = S9SceneLayoutAnchor(
                    scene_id=plate,
                    primary_view_direction=notes or first_seg_layout or "保持同场景统一主视方向",
                    landmarks=env_prompt or first_seg_layout or "严格沿用场景参考图固定门窗与家具地标",
                    camera_working_side="同场多段摄影机保持在主关系轴同一工作侧，反打不越轴、左右坐标不镜像翻转",
                )
                result.scene_layout_rules.append(layout_rule)
                layout_by_sid[plate] = layout_rule

    return result


def format_s9_continuity_bible_for_segment(
    bible: S9EpisodeContinuityBible | None,
    *,
    segment: S9SegmentPlan,
    slots: list[S9SegmentAssetSlot],
) -> str:
    """Format relevant Continuity Bible locks for a specific segment."""

    if bible is None:
        return ""

    seg_scene_keys: list[str] = []
    for slot in slots or []:
        if slot.asset_kind in {"scene", "scene_reverse"} and slot.asset_id:
            sid, vid = _normalize_scene_id_variant(slot.asset_id, slot.variant_id)
            plate = f"{sid}_{vid}" if vid else sid
            if plate not in seg_scene_keys:
                seg_scene_keys.append(plate)
    for shot in segment.shots or []:
        if shot.scene_id:
            sid, vid = _normalize_scene_id_variant(shot.scene_id, shot.scene_variant_id)
            plate = f"{sid}_{vid}" if vid else sid
            if plate not in seg_scene_keys:
                seg_scene_keys.append(plate)

    def _matches_scene(rule_sid: str) -> bool:
        if not seg_scene_keys:
            return True
        norm_rule, _ = _normalize_scene_id_variant(rule_sid, "")
        for key in seg_scene_keys:
            if (
                norm_rule == key
                or key.startswith(f"{norm_rule}_")
                or norm_rule.startswith(f"{key}_")
            ):
                return True
        return False

    parts: list[str] = []
    matched_lighting = [r for r in bible.scene_lighting_rules if r.scene_id and _matches_scene(r.scene_id)]
    if matched_lighting:
        lines = ["【全集锁定的同场景光影画风铁律（所有同场景大段必须严格一致，禁止改时段/主光/色温）】"]
        for r in matched_lighting:
            t_note = f"（时段：{r.time_of_day}）" if r.time_of_day else ""
            lines.append(f"- 场景 `{r.scene_id}`{t_note}：")
            if r.visual_style_summary:
                lines.append(f"  - 视觉风格：{r.visual_style_summary}")
            if r.key_lighting:
                lines.append(f"  - 全局主光：{r.key_lighting}")
            if r.color_and_material:
                lines.append(f"  - 色彩与材质：{r.color_and_material}")
        parts.append("\n".join(lines))

    matched_layout = [r for r in bible.scene_layout_rules if r.scene_id and _matches_scene(r.scene_id)]
    if matched_layout:
        lines = ["【全集锁定的同场景空间坐标铁律（固定地标与机位工作侧严禁跨段跳轴）】"]
        for r in matched_layout:
            segs = [
                p
                for p in (
                    f"主视方向={r.primary_view_direction}" if r.primary_view_direction else "",
                    f"固定地标={r.landmarks}" if r.landmarks else "",
                    f"机位工作侧={r.camera_working_side}" if r.camera_working_side else "",
                )
                if p
            ]
            if segs:
                lines.append(f"- 场景 `{r.scene_id}`：{'｜'.join(segs)}")
        if len(lines) > 1:
            parts.append("\n".join(lines))

    if bible.prop_state_chain:
        lines = ["【跨大段道具流转与可见性状态链（严禁道具跨段凭空消失或瞬移）】"]
        lines.extend(f"- {item}" for item in bible.prop_state_chain if str(item).strip())
        if len(lines) > 1:
            parts.append("\n".join(lines))

    seg_speakers = {
        str(sh.speaker or "").replace("OS", "").replace("os", "").strip().lower()
        for sh in (segment.shots or [])
        if str(sh.speaker or "").strip()
    }
    matched_voices = [
        vp
        for vp in bible.voice_profiles
        if vp.speaker
        and (
            not seg_speakers
            or any(
                vp.speaker.lower() in spk or spk in vp.speaker.lower()
                for spk in seg_speakers
            )
        )
    ]
    if matched_voices:
        lines = ["【全集锁定的角色稳定声线档案（跨段必须保持同一声线设定）】"]
        for vp in matched_voices:
            lines.append(f"- {vp.speaker}：{vp.voice_design}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _estimate_spoken_seconds(spoken_text: str) -> float:
    """Estimate natural speech duration (Mandarin ~4.5 chars/s, English ~2.8 words/s)."""

    text = str(spoken_text or "").strip()
    if not text:
        return 0.0
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin_words = len(re.findall(r"[A-Za-z0-9]+", text))
    digits_punct = len(re.findall(r"[，。！？；：、,.!?;:]", text))
    if cjk_chars > 0:
        return (cjk_chars / 4.5) + (latin_words / 2.8) + min(0.8, digits_punct * 0.06)
    return (latin_words / 2.8) + min(0.8, digits_punct * 0.06)


def audit_s9_segment_pacing(
    seg: S9SegmentPlan,
    *,
    max_segment_seconds: int = DEFAULT_S9_MAX_SEGMENT_SECONDS,
    require_exit_state: bool = False,
) -> list[str]:
    """Audit one segment for YD compact-pacing, shot overload, and clip-closure issues."""

    cap = normalize_max_segment_seconds(max_segment_seconds)
    issues: list[str] = []
    seg_num = seg.segment_number
    if seg.planned_duration_seconds > cap:
        issues.append(f"大段{seg_num} 计划时长 {seg.planned_duration_seconds}s 超出上限 {cap}s")
    shots = list(seg.shots or [])
    if not shots:
        issues.append(f"大段{seg_num} 缺少分镜列表")
        return issues

    # Check opening filler silence shots (e.g., 4s empty door + 3s monitor before action starts)
    opening_silence_count = 0
    opening_silence_sec = 0.0
    for s in shots:
        if s.audio_type == "silence" and not str(s.spoken_text or "").strip():
            opening_silence_count += 1
            opening_silence_sec += float(s.planned_duration_seconds or 0.0)
        else:
            break
    if opening_silence_count >= 2 and opening_silence_sec > 5.0:
        issues.append(
            f"大段{seg_num} 段首连续 {opening_silence_count} 个无声铺垫镜共 {opening_silence_sec:.1f}s，涉嫌废镜头拖节奏，应合并进首镜与核心动作并行"
        )

    for s in shots:
        dur = float(s.planned_duration_seconds or 0.0)
        spoken = str(s.spoken_text or "").strip()
        speaker_raw = str(s.speaker or "").strip()
        turns = [t.strip() for t in spoken.split("|") if t.strip()] if spoken else []
        multi_speaker = len(turns) >= 2 or len(
            [p for p in re.split(r"[;；、/]+", speaker_raw) if p.strip()]
        ) >= 2
        prop_count = len(list(getattr(s, "prop_ids", []) or []))
        exchange_hits = re.findall(
            r"(扫码|装袋|找零|掏钱|拍在|递给|接过|拿烟|取烟|拿冰美式|打包)",
            str(getattr(s, "content", "") or ""),
        )

        if s.audio_type == "silence" and not spoken:
            if dur > 4.5:
                issues.append(
                    f"大段{seg_num}-分镜{s.shot_number} 无声镜头长达 {dur:.1f}s（紧凑档应 <= 4s），请压缩空等或合并"
                )
        elif spoken:
            est_speech = _estimate_spoken_seconds(spoken)
            allowed_ceil = max(4.5, est_speech * 1.25 + 1.5)
            if dur > allowed_ceil:
                issues.append(
                    f"大段{seg_num}-分镜{s.shot_number} 台词约需 {est_speech:.1f}s 却分配了 {dur:.1f}s（存在严重空尾/注水），应压至约 {max(2, round(est_speech + 1.0))}s 并与动作并行"
                )
            if est_speech >= 2.0 and dur < est_speech * 0.8:
                issues.append(
                    f"大段{seg_num}-分镜{s.shot_number} 台词约需 {est_speech:.1f}s 却仅分配 {dur:.1f}s（语速过赶/吞词风险），请适当加长或拆分台词"
                )

        if multi_speaker:
            est_multi = _estimate_spoken_seconds(spoken) + 0.6 * max(1, len(turns) - 1)
            if dur < est_multi + 2.0 or prop_count >= 3 or len(exchange_hits) >= 2:
                issues.append(
                    f"大段{seg_num}-分镜{s.shot_number} 单镜内含多人轮流开口与多步动作（仅 {dur:.1f}s），极易动作快进和道具穿帮，请按说话轮次拆分为独立分镜"
                )
        elif prop_count >= 4 or (len(exchange_hits) >= 3 and dur < max(7.0, _estimate_spoken_seconds(spoken) + len(exchange_hits) * 1.5)):
            issues.append(
                f"大段{seg_num}-分镜{s.shot_number} 单镜塞入过多连续道具交接（{max(prop_count, len(exchange_hits))} 项操作仅 {dur:.1f}s），极易导致道具分裂/漏装，请拆分成多个分镜或精简冗余过程"
            )

    if require_exit_state and not str(getattr(seg, "exit_state", "") or "").strip():
        issues.append(
            f"大段{seg_num} 缺少 `exit_state` 段尾落点状态，可能导致下一大段无法衔接"
        )

    return issues


def _audit_s9_segment_boundary(
    prev_seg: S9SegmentPlan | None,
    seg: S9SegmentPlan,
) -> list[str]:
    """Detect duplicated framing/dialogue right across adjacent clip boundaries."""

    if prev_seg is None or not prev_seg.shots or not seg.shots:
        return []
    prev_last = prev_seg.shots[-1]
    cur_first = seg.shots[0]
    issues: list[str] = []
    prev_spoken = _normalize_slice_line(prev_last.spoken_text)
    cur_spoken = _normalize_slice_line(cur_first.spoken_text)
    if prev_spoken and prev_spoken == cur_spoken:
        issues.append(
            f"大段{seg.segment_number}-分镜1 重复了上段末镜台词“{cur_first.spoken_text}”，应直接承接上段尾态推进新动作"
        )
    same_camera = (
        str(prev_last.shot_scale_movement or "").strip()
        and str(prev_last.shot_scale_movement or "").strip() == str(cur_first.shot_scale_movement or "").strip()
        and str(prev_last.camera_angle or "").strip() == str(cur_first.camera_angle or "").strip()
        and str(prev_last.camera_motion or "").strip() == str(cur_first.camera_motion or "").strip()
    )
    if same_camera:
        prev_content = _normalize_slice_line(prev_last.content)
        cur_content = _normalize_slice_line(cur_first.content)
        if prev_content and cur_content and (prev_content in cur_content or cur_content in prev_content):
            issues.append(
                f"大段{seg.segment_number}-分镜1 与大段{prev_seg.segment_number} 末镜同构重复（相同景别机位且动作内容重合），请切换景别或机位推进新事件"
            )
    return issues


def audit_s9_plan_pacing(
    plan: S9StoryboardPlan,
    *,
    max_segment_seconds: int = DEFAULT_S9_MAX_SEGMENT_SECONDS,
) -> list[str]:
    """Audit Stage 1 storyboard plan for YD compact-pacing and clip-closure issues."""

    cap = normalize_max_segment_seconds(max_segment_seconds)
    issues: list[str] = []
    segments = list(plan.segments or [])
    if not segments:
        return ["未生成任何大段分镜"]

    for idx, seg in enumerate(segments):
        issues.extend(
            audit_s9_segment_pacing(
                seg,
                max_segment_seconds=cap,
                require_exit_state=(len(segments) >= 2 and idx < len(segments) - 1),
            )
        )

    return issues


async def _drain_agent_event_stream(_ctx: Any, event_stream: Any) -> None:
    """Drain streaming model events so SSE chunks continuously reset gateway read timeouts."""
    async for _ in event_stream:
        pass


def _agent_method_supports_param(agent: Any, method_name: str, param_name: str) -> bool:
    method = getattr(agent, method_name, None)
    if not callable(method):
        return False
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return False
    return param_name in sig.parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


def _agent_run_supports_event_stream_handler(agent: Any) -> bool:
    """Return True when ``agent.run`` accepts ``event_stream_handler``."""
    return _agent_method_supports_param(agent, "run", "event_stream_handler")


def _is_token_limit_exhausted_error(exc: BaseException) -> bool:
    msg = str(exc or "").lower()
    return (
        "model token limit" in msg
        or "exceeded before any response was generated" in msg
        or "exceeded while generating a tool call" in msg
    )


def _build_token_limit_retry_settings(agent: Any) -> dict[str, Any]:
    from novelvideo.utils.model_context_budget import TextContextBudget

    base = dict(getattr(agent, "model_settings", None) or {})
    retry = dict(base)
    effort = str(retry.get("openai_reasoning_effort") or "").strip().lower()
    if effort in {"high", "medium", ""}:
        retry["openai_reasoning_effort"] = "low"
    elif effort == "low":
        retry["openai_reasoning_effort"] = "minimal"

    if TextContextBudget.from_env() is None:
        cur_max = retry.get("max_tokens")
        if cur_max is None:
            retry["max_tokens"] = 32768
        elif isinstance(cur_max, int) and cur_max < 16384:
            retry["max_tokens"] = 16384
        elif isinstance(cur_max, int) and cur_max < 32768:
            retry["max_tokens"] = 32768
    return retry


async def _run_s9_agent_streaming_once(
    agent: Any,
    prompt: Any,
    *,
    message_history: Any = None,
    model_settings_override: dict[str, Any] | None = None,
) -> Any:
    run_kwargs: dict[str, Any] = {}
    if message_history is not None:
        run_kwargs["message_history"] = message_history
    if _agent_run_supports_event_stream_handler(agent):
        if model_settings_override is not None and _agent_method_supports_param(agent, "run", "model_settings"):
            run_kwargs["model_settings"] = model_settings_override
        run_kwargs["event_stream_handler"] = _drain_agent_event_stream
        return await agent.run(prompt, **run_kwargs)
    if callable(getattr(agent, "run_stream", None)):
        if model_settings_override is not None and _agent_method_supports_param(agent, "run_stream", "model_settings"):
            run_kwargs["model_settings"] = model_settings_override
        async with agent.run_stream(prompt, **run_kwargs) as stream_result:
            output = await stream_result.get_output()
            response = getattr(stream_result, "response", None)
            all_messages_fn = getattr(stream_result, "all_messages", None)
            all_messages = all_messages_fn() if callable(all_messages_fn) else None
            return SimpleNamespace(
                output=output,
                response=response,
                all_messages=(
                    (lambda msgs=all_messages: msgs) if all_messages is not None else None
                ),
            )
    if model_settings_override is not None and _agent_method_supports_param(agent, "run", "model_settings"):
        run_kwargs["model_settings"] = model_settings_override
    return await agent.run(prompt, **run_kwargs)


async def _run_s9_agent_streaming(
    agent: Any,
    prompt: Any,
    *,
    message_history: Any = None,
) -> Any:
    """Run a PydanticAI agent via streaming transport while preserving output retries.

    Passing ``event_stream_handler`` makes ``Agent.run()`` invoke
    ``model.request_stream()`` instead of buffering the entire completion in a
    single non-streaming HTTP response (which triggers Cloudflare's 120s 524
    Proxy Read Timeout on long Stage 1/Stage 2 generations), while keeping
    PydanticAI's structured-output validation retries intact. Also retries once
    with lower reasoning effort / higher output budget when thinking tokens
    exhaust ``max_tokens`` before generating output.
    """
    try:
        return await _run_s9_agent_streaming_once(
            agent, prompt, message_history=message_history
        )
    except Exception as exc:
        if not _is_token_limit_exhausted_error(exc):
            raise
        if not (
            _agent_method_supports_param(agent, "run", "model_settings")
            or _agent_method_supports_param(agent, "run_stream", "model_settings")
        ):
            raise
        retry_settings = _build_token_limit_retry_settings(agent)
        logger.warning(
            "S9 agent hit completion token limit before emitting response (%s); "
            "retrying with adjusted settings: %s",
            exc,
            {k: v for k, v in retry_settings.items() if k in {"max_tokens", "openai_reasoning_effort"}},
        )
        return await _run_s9_agent_streaming_once(
            agent,
            prompt,
            message_history=message_history,
            model_settings_override=retry_settings,
        )


def _should_use_two_pass_stage1(source_text: str) -> bool:
    stripped = str(source_text or "").strip()
    non_empty_lines = [ln for ln in stripped.splitlines() if ln.strip()]
    return len(stripped) > 220 or len(non_empty_lines) > 4


def _build_s9_stage1_skeleton_system_prompt(
    *,
    max_segment_seconds: int,
    spine_template: str = "drama",
    tail_anchor: bool = False,
) -> str:
    cap = normalize_max_segment_seconds(max_segment_seconds)
    short_tail_floor = max(4, round(cap * 0.4))
    mode_rule = (
        "当前为解说剧（narrated）：原文旁白、解说词与角色对白必须完整保留进对应大段的 `source_excerpt`。"
        if spine_template == "narrated"
        else "当前为精品剧（drama）：原文全部对白、关键 OS 与核心动作必须完整保留进对应大段的 `source_excerpt`。"
    )
    anchor_note = (
        "已开启「尾帧锚点镜」：非全片末段在段末预留 1 秒静止锚点预算。"
        if tail_anchor
        else "「尾帧锚点镜」关闭：段末自然落定 `exit_state`，供下一大段第 0 秒直接承接。"
    )
    return f"""你是 YD 电影级分镜总导演（Stage 1A：全局定拍、切段规划与跨段连续性圣经锁定）。
你的任务是通读全集原文与全部资产清单，一次性完成全局时长评估、大段分段规划与跨 Clip 连续性锁定，输出轻量结构化 `S9Stage1SkeletonPlan` JSON（本步**不展开**逐镜 `shots` 列表，由下一步全并发细化）。

### 核心法则
1. **先剧情自然长度 → 再按剧情断点切分大段**：
   - 普通话对白/旁白按 4～5 字/秒，边做边说并行不相加，紧凑动作节点默认约 1 秒。
   - **单段上限 {cap} 秒只是上限，不是目标**；自然长度 14 秒就规划 14 秒，严禁凑秒或把全片硬塞入超出上限的单段。
   - 段界必须落在剧情断点（场次切换、节拍转折、完整动作链/交流结束处），不劈开一句对白或一次接触；多段时末段一般不短于 {short_tail_floor} 秒。
   - {mode_rule}
   - {anchor_note}
2. **`source_excerpt` 边界锚点与零遗漏铁律**：
   - 每个大段的 `source_excerpt` 用于精确标定该段在【本集完整原文】中的起止范围，不得改写或编造。
   - 若该段对应 <= 3 行原文可直接原样复制；若对应多行长段，推荐简写为 `<本段第1行原句>\n...\n<本段最后1行原句>`（后台会自动展开为 100% 连续完整原文传给下游，从而大幅节省 JSON 输出体积），确保相邻大段起止连续、整集零遗漏。
3. **跨 Clip 连续性圣经（`continuity_bible`，极重要！）**：
   - `scene_lighting_rules`：为本集用到的每个物理场景锁定唯一一套【全局光影画风】（时段、视觉风格、主光来源与场内方向、色温与材质），保证后续并发生成时同场景所有 Clip 光影色温 100% 一致。
   - `scene_layout_rules`：为多人同场或跨段复用场景锁定主视方向、2～4 个固定地标及主关系轴允许机位工作侧，彻底杜绝跨段跳轴或左右镜像翻转。
   - `prop_state_chain`：列出关键/跨段随身道具在各大段的持握或摆放归属变化（如“公文包：大段1艾莉森右手拎入 -> 大段2放茶几左侧可见区 -> 大段3维持茶几左侧”）。
   - `voice_profiles`：为每个发声角色锁定稳定声线描述（用于跨段保持声线一致）。
4. **段界交接契约**：
   - 每个大段写清 `story_beat`、`entry_state`、`exit_state`、`opening_shot_direction`（确保与上段末镜自然推进而不雷同）、`closing_shot_direction`、`scene_layout` 及涉及的 `identity_ids` / `scene_ids` / `prop_ids`。"""


def _build_s9_stage1_skeleton_task_prompt(
    *,
    source_text: str,
    episode_num: int,
    episode_title: str,
    style_context: S9StyleContext,
    planned_assets_summary: str,
    max_segment_seconds: int,
    prev_episode_context: str = "",
) -> str:
    cap = normalize_max_segment_seconds(max_segment_seconds)
    prev_block = (
        f"\n【上一集末尾衔接上下文（第一大段须顺畅承接）】\n{prev_episode_context.strip()}\n"
        if prev_episode_context.strip()
        else ""
    )
    return f"""请为第 {episode_num} 集《{episode_title or f'EP{episode_num}'}》输出轻量全局切段骨架与连续性圣经（`S9Stage1SkeletonPlan`，单大段上限 **{cap} 秒**）。

【项目视觉规格】
- {style_context.s9_style_anchor}
{prev_block}
【本集已有资产与音色】
{planned_assets_summary}

【本集完整原文 / 工作稿】
{source_text.strip()}

请输出完整的 `S9Stage1SkeletonPlan` JSON（每段 `source_excerpt` 保持与原文一致的起止锚点，长段可用首行+`...`+末行表达以精简 JSON）。"""


def _build_s9_stage1_expand_task_prompt(
    *,
    skeleton: S9Stage1SkeletonSegment,
    episode_overview: str,
    bible_summary: str,
    prev_skeleton: S9Stage1SkeletonSegment | None,
    next_skeleton: S9Stage1SkeletonSegment | None,
    prev_episode_context: str,
    style_context: S9StyleContext,
    planned_assets_summary: str,
    source_slice: str,
    episode_num: int,
    episode_title: str,
    max_segment_seconds: int,
) -> str:
    cap = normalize_max_segment_seconds(max_segment_seconds)
    handoff_lines: list[str] = []
    if prev_skeleton is not None:
        handoff_lines.append(
            f"- 前一大段（大段{prev_skeleton.segment_number}·{prev_skeleton.title}）闭环：{prev_skeleton.story_beat}\n"
            f"  上段结尾状态：{prev_skeleton.exit_state}\n"
            f"  上段末镜方向：{prev_skeleton.closing_shot_direction or '自然落定尾态'}"
        )
    elif prev_episode_context.strip():
        handoff_lines.append(f"- 上一集末尾语境：{prev_episode_context.strip()}")
    if next_skeleton is not None:
        handoff_lines.append(
            f"- 下一大段（大段{next_skeleton.segment_number}·{next_skeleton.title}）将承接：{next_skeleton.story_beat}\n"
            f"  下段第0秒起态：{next_skeleton.entry_state}\n"
            f"  下段首镜方向：{next_skeleton.opening_shot_direction or '切换景别推进新动作'}"
        )
    handoff_block = "\n".join(handoff_lines) if handoff_lines else "- 本段为全集唯一大段，独立完成完整闭环"

    bible_block = f"\n【全集连续性圣经（必须严格遵守）】\n{bible_summary}\n" if bible_summary else ""
    return f"""请为第 {episode_num} 集《{episode_title or f'EP{episode_num}'}》**第 {skeleton.segment_number} 大段《{skeleton.title}》**规划详细分镜（单段上限 **{cap} 秒**，初步预算 **{skeleton.planned_duration_seconds} 秒**，按真实紧凑节奏排镜，严禁凑秒或超载）。

【全集大段总览（保持全局视野）】
{episode_overview}
{bible_block}
【本大段边界交接契约】
{handoff_block}
- 本大段起始画面状态目标：{skeleton.entry_state}
- 本大段结尾落定状态目标：{skeleton.exit_state}
- 本大段首镜开场方向：{skeleton.opening_shot_direction}
- 本大段末镜收束方向：{skeleton.closing_shot_direction}
- 本大段预拟空间布局：{skeleton.scene_layout or '按圣经与场景卡保持工作侧一致'}
- 预设资产线索：角色={skeleton.identity_ids}｜场景={skeleton.scene_ids}｜道具={skeleton.prop_ids}

【项目视觉规格】
- {style_context.s9_style_anchor}

【本集已有资产与音色（仅允许精确引用其中 ID）】
{planned_assets_summary}

【本大段原文切片与前后边界】
{source_slice}

请严格按 YD分镜skills 输出本大段的 `S9SegmentExpansionOutput` JSON（`shots` 时间轴从 `0` 连续整数秒拼接到 `planned_duration_seconds`，台词逐字保留，每镜填全 `identity_ids`、`scene_id`、`prop_ids`）。"""


async def _revise_s9_segment_local(
    *,
    segment: S9SegmentPlan,
    issues: list[str],
    prev_segment: S9SegmentPlan | None,
    next_segment: S9SegmentPlan | None,
    source_slice: str,
    planned_assets_summary: str,
    style_context: S9StyleContext,
    max_segment_seconds: int,
    spine_template: str,
    tail_anchor: bool,
) -> S9SegmentPlan:
    from pydantic_ai import Agent

    from novelvideo.config import get_newapi_text_pydantic_model

    cap = normalize_max_segment_seconds(max_segment_seconds)
    system_prompt = build_s9_stage1_system_prompt(
        max_segment_seconds=cap,
        spine_template=spine_template,
        tail_anchor=tail_anchor,
    )
    agent = Agent(
        get_newapi_text_pydantic_model(
            "LITERAL_BEAT_META_MODEL",
            brainclaw_profile=BrainClawProfile.LITERAL_BEAT_METADATA,
            capability="text.generate.agent",
        ),
        system_prompt=system_prompt,
        output_type=S9SegmentExpansionOutput,
        model_settings=_s9_stage1_model_settings("LITERAL_BEAT_META_MODEL"),
        retries=2,
        name="YD Stage1 Local Segment Reviser",
    )
    neighbor_lines: list[str] = []
    if prev_segment is not None:
        prev_last_desc = ""
        if prev_segment.shots:
            pl = prev_segment.shots[-1]
            prev_last_desc = f"；末镜={pl.shot_scale_movement}|{pl.camera_angle}|{pl.camera_motion}|{pl.content[:60]}"
        neighbor_lines.append(
            f"- 上一段结尾：{prev_segment.exit_state}{prev_last_desc}"
        )
    if next_segment is not None:
        next_first_desc = ""
        if next_segment.shots:
            nf = next_segment.shots[0]
            next_first_desc = f"；首镜={nf.shot_scale_movement}|{nf.camera_angle}|{nf.camera_motion}|{nf.content[:60]}"
        neighbor_lines.append(
            f"- 下一段开头：{next_segment.entry_state}{next_first_desc}"
        )
    neighbor_block = "\n".join(neighbor_lines) or "- 无邻接大段"

    revision_prompt = (
        f"请局部修订「大段{segment.segment_number}：{segment.title}」（单段上限 {cap} 秒），"
        "修复下列节奏/负荷/衔接问题，输出修订后的 `S9SegmentExpansionOutput` JSON：\n\n"
        "【待修复问题】\n"
        + "\n".join(f"- {item}" for item in issues)
        + f"\n\n【邻接段边界】\n{neighbor_block}\n\n"
        f"【项目视觉规格】\n- {style_context.s9_style_anchor}\n\n"
        f"【本集已有资产与音色】\n{planned_assets_summary}\n\n"
        f"【本大段原文切片】\n{source_slice}\n\n"
        f"【待修订当前大段 JSON】\n{segment.model_dump_json(ensure_ascii=False)}\n\n"
        "要求：保留全部台词原字与关键事件，砍掉无效空镜/空尾、拆分超载镜头、确保段尾落定 `exit_state` 且不与邻段首尾镜同构重复。"
    )
    try:
        res = await _run_s9_agent_streaming(agent, revision_prompt)
        out = res.output
        if out and out.shots:
            candidate = segment.model_copy(
                update={
                    "planned_duration_seconds": max(
                        MIN_S9_SEGMENT_SECONDS,
                        min(cap, int(out.planned_duration_seconds or segment.planned_duration_seconds)),
                    ),
                    "story_beat": out.story_beat or segment.story_beat,
                    "entry_state": out.entry_state or segment.entry_state,
                    "exit_state": out.exit_state or segment.exit_state,
                    "scene_layout": out.scene_layout or segment.scene_layout,
                    "shots": out.shots,
                }
            )
            new_issues = audit_s9_segment_pacing(
                candidate,
                max_segment_seconds=cap,
                require_exit_state=(next_segment is not None),
            ) + _audit_s9_segment_boundary(prev_segment, candidate)
            if len(new_issues) <= len(issues):
                return candidate
    except Exception as exc:
        logger.warning(
            "YD Stage 1 local revision failed for segment %s, keeping initial expansion: %s",
            segment.segment_number,
            exc,
        )
    return segment


async def _run_s9_stage1_monolithic(
    *,
    source_text: str,
    episode_num: int,
    episode_title: str,
    style_context: S9StyleContext,
    planned_summary: str,
    cap: int,
    spine_template: str,
    tail_anchor: bool,
    prev_episode_context: str,
) -> S9StoryboardPlan:
    """Single-pass Stage 1 planner for very short scripts or skeleton fallback."""

    from pydantic_ai import Agent

    from novelvideo.config import get_newapi_text_pydantic_model

    system_prompt = build_s9_stage1_system_prompt(
        max_segment_seconds=cap,
        spine_template=spine_template,
        tail_anchor=tail_anchor,
    )
    task_prompt = build_s9_stage1_task_prompt(
        source_text=source_text,
        episode_num=episode_num,
        episode_title=episode_title,
        style_context=style_context,
        planned_assets_summary=planned_summary,
        max_segment_seconds=cap,
        prev_episode_context=prev_episode_context,
    )
    agent = Agent(
        get_newapi_text_pydantic_model(
            "LITERAL_BEAT_META_MODEL",
            brainclaw_profile=BrainClawProfile.LITERAL_BEAT_METADATA,
            capability="text.generate.agent",
        ),
        system_prompt=system_prompt,
        output_type=S9StoryboardPlan,
        model_settings=_s9_stage1_model_settings("LITERAL_BEAT_META_MODEL"),
        retries=3,
        name="YD Stage1 Storyboard Planner",
    )
    result = await _run_s9_agent_streaming(agent, task_prompt)
    plan = result.output

    issues = audit_s9_plan_pacing(plan, max_segment_seconds=cap)
    if issues and plan.segments:
        if len(plan.segments) == 1:
            logger.warning(
                "YD Stage 1 pacing/closure audit found %d issue(s); running compact revision: %s",
                len(issues),
                "; ".join(issues[:5]),
            )
            revision_prompt = (
                f"{task_prompt}\n\n"
                "【初稿节奏与段尾衔接审校未通过，请按 YD分镜skills 紧凑档立即精简修订】\n"
                "发现以下具体问题：\n"
                + "\n".join(f"- {item}" for item in issues)
                + "\n\n【待修订初稿 JSON】\n"
                + plan.model_dump_json(ensure_ascii=False)
                + "\n\n要求：\n"
                "1. 保留原文全部台词与必要事件，绝对不要删减对白原字；\n"
                "2. 砍掉无用段首空镜、压缩话少时多的空尾秒数、让动作与台词并行；\n"
                "3. 严禁单镜超载：把多人轮流开口或 3 项以上连续道具交接拆成多个独立分镜（必要时多拆一个短 Clip），给每个动作留足物理时间；\n"
                "4. 确保每个大段完整讲完自己的 story_beat 并在末镜落定清晰的 exit_state，且下一大段第 1 镜必须切换景别/机位推进到新动作，严禁与上段末镜拍成同构重复画面；\n"
                f"5. 单段时长 <= {cap} 秒（整数秒），不为了凑满 {cap} 秒硬塞或拉长镜头。"
            )
            try:
                revised = await _run_s9_agent_streaming(agent, revision_prompt)
                if revised.output and revised.output.segments:
                    revised_issues = audit_s9_plan_pacing(revised.output, max_segment_seconds=cap)
                    if len(revised_issues) <= len(issues):
                        plan = revised.output
            except Exception as exc:
                logger.warning("YD Stage 1 compact revision failed, keeping initial plan: %s", exc)
        else:
            # Multi-segment monolithic result: revise only flagged segments in parallel.
            segments = list(plan.segments)
            semaphore = asyncio.Semaphore(get_s9_llm_concurrency())

            async def _fix(idx: int) -> S9SegmentPlan:
                seg_issues = audit_s9_segment_pacing(
                    segments[idx],
                    max_segment_seconds=cap,
                    require_exit_state=(idx < len(segments) - 1),
                ) + _audit_s9_segment_boundary(segments[idx - 1] if idx > 0 else None, segments[idx])
                if not seg_issues:
                    return segments[idx]
                async with semaphore:
                    return await _revise_s9_segment_local(
                        segment=segments[idx],
                        issues=seg_issues,
                        prev_segment=segments[idx - 1] if idx > 0 else None,
                        next_segment=segments[idx + 1] if idx + 1 < len(segments) else None,
                        source_slice=segments[idx].source_excerpt or source_text,
                        planned_assets_summary=planned_summary,
                        style_context=style_context,
                        max_segment_seconds=cap,
                        spine_template=spine_template,
                        tail_anchor=tail_anchor,
                    )

            revised_segments = await asyncio.gather(*(_fix(i) for i in range(len(segments))))
            plan = plan.model_copy(update={"segments": list(revised_segments)})
    return plan


async def run_s9_stage1_plan(
    *,
    source_text: str,
    episode_num: int,
    episode_title: str,
    style_context: S9StyleContext,
    characters: list[Any],
    episode: Any,
    voice_available_names: set[str],
    scenes: list[Any] | None = None,
    props: list[Any] | None = None,
    output_dir: str | Path | None = None,
    max_segment_seconds: int = DEFAULT_S9_MAX_SEGMENT_SECONDS,
    spine_template: str = "drama",
    tail_anchor: bool = False,
    prev_episode_context: str = "",
) -> S9StoryboardPlan:
    """Invoke LLM to compute Stage 1 S9StoryboardPlan.

    Uses Pass 1A (lightweight global skeleton + Continuity Bible) followed by
    parallel Pass 1B (per-segment shot expansion + local selective revision) for
    multi-line/longer episodes, falling back cleanly to a single-pass plan for
    very short inputs.
    """

    from pydantic_ai import Agent

    from novelvideo.config import get_newapi_text_pydantic_model

    cap = normalize_max_segment_seconds(max_segment_seconds)
    planned_summary = _format_planned_assets_summary(
        characters=characters,
        episode=episode,
        voice_available_names=voice_available_names,
        scenes=scenes,
        props=props,
        output_dir=output_dir,
    )

    if not _should_use_two_pass_stage1(source_text):
        return await _run_s9_stage1_monolithic(
            source_text=source_text,
            episode_num=episode_num,
            episode_title=episode_title,
            style_context=style_context,
            planned_summary=planned_summary,
            cap=cap,
            spine_template=spine_template,
            tail_anchor=tail_anchor,
            prev_episode_context=prev_episode_context,
        )

    # Pass 1A: Lightweight global skeleton + Continuity Bible
    skeleton_agent = Agent(
        get_newapi_text_pydantic_model(
            "LITERAL_BEAT_META_MODEL",
            brainclaw_profile=BrainClawProfile.LITERAL_BEAT_METADATA,
            capability="text.generate.agent",
        ),
        system_prompt=_build_s9_stage1_skeleton_system_prompt(
            max_segment_seconds=cap,
            spine_template=spine_template,
            tail_anchor=tail_anchor,
        ),
        output_type=S9Stage1SkeletonPlan,
        model_settings=_s9_stage1_model_settings("LITERAL_BEAT_META_MODEL"),
        retries=3,
        name="YD Stage1A Skeleton Planner",
    )
    skeleton_task_prompt = _build_s9_stage1_skeleton_task_prompt(
        source_text=source_text,
        episode_num=episode_num,
        episode_title=episode_title,
        style_context=style_context,
        planned_assets_summary=planned_summary,
        max_segment_seconds=cap,
        prev_episode_context=prev_episode_context,
    )
    skeleton_res = await _run_s9_agent_streaming(skeleton_agent, skeleton_task_prompt)
    skeleton_plan: S9Stage1SkeletonPlan = skeleton_res.output
    skeletons = list(skeleton_plan.segments or [])
    if not skeletons:
        logger.warning("Stage 1A returned empty segments; falling back to monolithic Stage 1")
        return await _run_s9_stage1_monolithic(
            source_text=source_text,
            episode_num=episode_num,
            episode_title=episode_title,
            style_context=style_context,
            planned_summary=planned_summary,
            cap=cap,
            spine_template=spine_template,
            tail_anchor=tail_anchor,
            prev_episode_context=prev_episode_context,
        )

    clean_excerpts, context_slices = slice_s9_source_text_for_segments(source_text, skeletons)
    episode_overview = "\n".join(
        f"- 大段{sk.segment_number}《{sk.title}》（约{sk.planned_duration_seconds}秒）：{sk.story_beat} "
        f"[起态：{sk.entry_state or '承接前态'} → 终态：{sk.exit_state or '自然收束'}]"
        for sk in skeletons
    )

    bible_summary_parts: list[str] = []
    bible = skeleton_plan.continuity_bible
    for lr in bible.scene_lighting_rules:
        bible_summary_parts.append(
            f"- 场景光影 `{lr.scene_id}`（{lr.time_of_day or '固定时段'}）：{lr.key_lighting}｜{lr.color_and_material}"
        )
    for sr in bible.scene_layout_rules:
        bible_summary_parts.append(
            f"- 场景坐标 `{sr.scene_id}`：主视={sr.primary_view_direction}｜地标={sr.landmarks}｜工作侧={sr.camera_working_side}"
        )
    for pc in bible.prop_state_chain:
        if str(pc).strip():
            bible_summary_parts.append(f"- 道具链：{pc}")
    for vp in bible.voice_profiles:
        if vp.speaker:
            bible_summary_parts.append(f"- 声线 `{vp.speaker}`：{vp.voice_design}")
    bible_summary = "\n".join(bible_summary_parts)

    expand_system_prompt = build_s9_stage1_system_prompt(
        max_segment_seconds=cap,
        spine_template=spine_template,
        tail_anchor=tail_anchor,
    )
    expand_agent = Agent(
        get_newapi_text_pydantic_model(
            "LITERAL_BEAT_META_MODEL",
            brainclaw_profile=BrainClawProfile.LITERAL_BEAT_METADATA,
            capability="text.generate.agent",
        ),
        system_prompt=expand_system_prompt,
        output_type=S9SegmentExpansionOutput,
        model_settings=_s9_stage1_model_settings("LITERAL_BEAT_META_MODEL"),
        retries=2,
        name="YD Stage1B Segment Expander",
    )
    semaphore = asyncio.Semaphore(get_s9_llm_concurrency())

    async def _expand_one(idx: int) -> S9SegmentPlan:
        sk = skeletons[idx]
        task_prompt = _build_s9_stage1_expand_task_prompt(
            skeleton=sk,
            episode_overview=episode_overview,
            bible_summary=bible_summary,
            prev_skeleton=skeletons[idx - 1] if idx > 0 else None,
            next_skeleton=skeletons[idx + 1] if idx + 1 < len(skeletons) else None,
            prev_episode_context=prev_episode_context if idx == 0 else "",
            style_context=style_context,
            planned_assets_summary=planned_summary,
            source_slice=context_slices[idx],
            episode_num=episode_num,
            episode_title=episode_title,
            max_segment_seconds=cap,
        )
        async with semaphore:
            try:
                res = await _run_s9_agent_streaming(expand_agent, task_prompt)
                out: S9SegmentExpansionOutput = res.output
                if out and out.shots:
                    seg_dur = max(
                        MIN_S9_SEGMENT_SECONDS,
                        min(cap, int(out.planned_duration_seconds or sk.planned_duration_seconds or 15)),
                    )
                    return S9SegmentPlan(
                        segment_number=idx + 1,
                        title=sk.title or f"第{idx + 1}幕",
                        planned_duration_seconds=seg_dur,
                        source_excerpt=clean_excerpts[idx],
                        story_beat=out.story_beat or sk.story_beat,
                        entry_state=out.entry_state or sk.entry_state,
                        exit_state=out.exit_state or sk.exit_state,
                        scene_layout=out.scene_layout or sk.scene_layout,
                        shots=out.shots,
                    )
            except Exception as exc:
                logger.warning(
                    "Stage 1B segment %s expansion failed, creating fallback shot from skeleton: %s",
                    idx + 1,
                    exc,
                )
        fallback_dur = max(
            MIN_S9_SEGMENT_SECONDS,
            min(cap, int(sk.planned_duration_seconds or 15)),
        )
        return S9SegmentPlan(
            segment_number=idx + 1,
            title=sk.title or f"第{idx + 1}幕",
            planned_duration_seconds=fallback_dur,
            source_excerpt=clean_excerpts[idx],
            story_beat=sk.story_beat,
            entry_state=sk.entry_state,
            exit_state=sk.exit_state,
            scene_layout=sk.scene_layout,
            shots=[
                S9SubShot(
                    shot_number=1,
                    planned_duration_seconds=float(fallback_dur),
                    start_seconds=0.0,
                    end_seconds=float(fallback_dur),
                    content=sk.story_beat or clean_excerpts[idx][:180] or "主体画面推进。",
                    identity_ids=list(sk.identity_ids),
                    scene_id=sk.scene_ids[0] if sk.scene_ids else "",
                    prop_ids=list(sk.prop_ids),
                )
            ],
        )

    expanded_segments = list(
        await asyncio.gather(*(_expand_one(i) for i in range(len(skeletons))))
    )

    # Selective local pacing & boundary repair (concurrent only on flagged segments)
    async def _maybe_repair(idx: int) -> S9SegmentPlan:
        seg = expanded_segments[idx]
        prev_seg = expanded_segments[idx - 1] if idx > 0 else None
        next_seg = expanded_segments[idx + 1] if idx + 1 < len(expanded_segments) else None
        issues = audit_s9_segment_pacing(
            seg,
            max_segment_seconds=cap,
            require_exit_state=(len(expanded_segments) >= 2 and idx < len(expanded_segments) - 1),
        ) + _audit_s9_segment_boundary(prev_seg, seg)
        if not issues:
            return seg
        logger.warning(
            "YD Stage 1B segment %d audit found %d issue(s); running local repair: %s",
            seg.segment_number,
            len(issues),
            "; ".join(issues[:4]),
        )
        async with semaphore:
            return await _revise_s9_segment_local(
                segment=seg,
                issues=issues,
                prev_segment=prev_seg,
                next_segment=next_seg,
                source_slice=context_slices[idx],
                planned_assets_summary=planned_summary,
                style_context=style_context,
                max_segment_seconds=cap,
                spine_template=spine_template,
                tail_anchor=tail_anchor,
            )

    repaired_segments = list(
        await asyncio.gather(*(_maybe_repair(i) for i in range(len(expanded_segments))))
    )
    return S9StoryboardPlan(
        duration_assessment=skeleton_plan.duration_assessment,
        asset_manifest=skeleton_plan.asset_manifest,
        continuity_bible=skeleton_plan.continuity_bible,
        segments=repaired_segments,
    )


def _has_stage2_truncation_issue(issues: list[str]) -> bool:
    return any(
        "token 上限" in issue
        or "意外中断" in issue
        or "缺少分镜" in issue
        or "时间轴未覆盖" in issue
        or "输出为空" in issue
        for issue in issues
    )


async def run_s9_stage2_segment_prompt(
    *,
    segment: S9SegmentPlan,
    slots: list[S9SegmentAssetSlot],
    style_context: S9StyleContext,
    prev_segment: S9SegmentPlan | None = None,
    next_segment: S9SegmentPlan | None = None,
    prev_episode_context: str = "",
    tail_anchor: bool = False,
    include_first_frame: bool = False,
    user_guidance: str = "",
    source_text: str = "",
    current_prompt: str = "",
    scene_context_notes: str = "",
    continuity_anchor: str = "",
) -> str:
    """Invoke LLM to compile Stage 2 full Seedance 2.0/2.5 prompt for one 大段."""

    from pydantic_ai import Agent

    from novelvideo.config import get_newapi_text_pydantic_model

    system_prompt = build_s9_stage2_system_prompt(
        tail_anchor=tail_anchor,
        include_first_frame=include_first_frame,
        include_scene_layout=_segment_needs_scene_layout(segment),
    )
    task_prompt = build_s9_stage2_task_prompt(
        segment=segment,
        slots=slots,
        style_context=style_context,
        prev_segment=prev_segment,
        next_segment=next_segment,
        prev_episode_context=prev_episode_context,
        user_guidance=user_guidance,
        source_text=source_text,
        current_prompt=current_prompt,
        scene_context_notes=scene_context_notes,
        continuity_anchor=continuity_anchor,
    )
    model_env = "SEEDANCE2_PROMPT_COMPOSER_MODEL"
    agent = Agent(
        get_newapi_text_pydantic_model(
            model_env,
            brainclaw_profile=BrainClawProfile.SEEDANCE2_PROMPT_COMPOSITION,
            capability="text.generate.agent",
        ),
        system_prompt=system_prompt,
        output_type=str,
        model_settings=_s9_stage2_model_settings(model_env),
        retries=2,
        name="YD Stage2 Segment Prompt Director",
    )

    async def _run_with_continuation(initial_prompt: str) -> tuple[str, list[str]]:
        result = await _run_s9_agent_streaming(agent, initial_prompt)
        rendered = _strip_code_fences(result.output)
        finish_reason = getattr(getattr(result, "response", None), "finish_reason", None)
        issues = _audit_s9_stage2_completeness(
            rendered,
            segment=segment,
            slots=slots,
            finish_reason=finish_reason,
        )
        if not issues or not rendered:
            return rendered, issues

        can_continue = _has_stage2_truncation_issue(issues) and (
            "分镜1" in rendered or not segment.shots
        )
        all_messages_fn = getattr(result, "all_messages", None)
        if not can_continue or not callable(all_messages_fn):
            return rendered, issues

        history = all_messages_fn()
        for _ in range(_STAGE2_MAX_CONTINUATIONS):
            if not _has_stage2_truncation_issue(issues):
                break
            cont_prompt = (
                f"你上一条输出未写完整（{'；'.join(issues)}）。"
                "请紧接着上一条输出最后一个字之后无缝续写剩余内容，补全到最后一个分镜及收束状态，"
                "禁止重复前面已经输出过的文字，禁止添加解释或代码块标记。"
            )
            cont_result = await _run_s9_agent_streaming(
                agent,
                cont_prompt,
                message_history=history,
            )
            cont_text = _strip_code_fences(cont_result.output)
            if not cont_text:
                break
            rendered = _merge_stage2_continuation(rendered, cont_text)
            next_history_fn = getattr(cont_result, "all_messages", None)
            if not callable(next_history_fn):
                break
            history = next_history_fn()
            finish_reason = getattr(
                getattr(cont_result, "response", None), "finish_reason", None
            )
            issues = _audit_s9_stage2_completeness(
                rendered,
                segment=segment,
                slots=slots,
                finish_reason=finish_reason,
            )
            if not issues:
                break
        return rendered, issues

    try:
        rendered, issues = await _run_with_continuation(task_prompt)
        if not issues and rendered:
            return rendered
        if issues:
            logger.warning(
                "S9 Stage 2 prompt incomplete for segment %s (%s); running repair pass",
                segment.segment_number,
                "; ".join(issues),
            )
            repair_prompt = (
                f"{task_prompt}\n\n"
                f"【上一次输出校验未通过：{'；'.join(issues)}】\n"
                f"请严格补齐以上缺失栏目与全部 {max(1, len(segment.shots))} 个分镜"
                f"（完整覆盖 0秒 至 {segment.planned_duration_seconds}秒），"
                "保持表演细节扎实但精炼不啰嗦，一次性完整输出到最后一镜结束。"
            )
            repaired, repair_issues = await _run_with_continuation(repair_prompt)
            if not repair_issues and repaired:
                return repaired
            if repaired and not _has_stage2_truncation_issue(repair_issues):
                return repaired
            if rendered and not _has_stage2_truncation_issue(issues):
                return rendered
            logger.warning(
                "S9 Stage 2 prompt repair remained truncated for segment %s (%s); falling back",
                segment.segment_number,
                "; ".join(repair_issues or issues),
            )
    except Exception as exc:
        logger.warning(
            "S9 Stage 2 prompt LLM failed for segment %s, using structured fallback: %s",
            segment.segment_number,
            exc,
        )
    return build_s9_stage2_fallback_prompt(
        segment=segment,
        slots=slots,
        style_context=style_context,
        tail_anchor=tail_anchor,
    )


_LIGHTING_SECTION_RE = re.compile(
    r"(【全局光影画风】\s*\n.*?)(?=\n\s*【|\n\s*分镜|\Z)",
    re.DOTALL,
)


def extract_s9_lighting_style_block(prompt: str) -> str:
    """Extract the 【全局光影画风】 section from an s9 Stage 2 prompt."""

    match = _LIGHTING_SECTION_RE.search(str(prompt or ""))
    return match.group(1).strip() if match else ""


_LIGHTING_IMAGE_MENTION_GROUP_RE = re.compile(
    r"@图片\d+(?:[、,，/与和及\s]+@图片\d+)*"
)


def sanitize_s9_lighting_anchor(lighting_block: str) -> str:
    """Strip segment-specific @图片N indices before reusing 【全局光影画风】 across segments."""

    text = str(lighting_block or "").strip()
    if not text:
        return ""
    return _LIGHTING_IMAGE_MENTION_GROUP_RE.sub("本段场景参考图", text)


def _primary_scene_key_for_slots(slots: list[S9SegmentAssetSlot]) -> str:
    for slot in slots or []:
        if slot.asset_kind == "scene" and slot.asset_id:
            sid, vid = _normalize_scene_id_variant(slot.asset_id, slot.variant_id)
            return f"{sid}_{vid}" if vid else sid
    return ""


def _resolve_scene_lighting_group_key(
    plate_key: str,
    known_keys: dict[str, int],
) -> str:
    """Match exact plate or base/variant sibling so same physical room shares lighting."""

    if not plate_key:
        return ""
    if plate_key in known_keys:
        return plate_key
    for existing in known_keys:
        if plate_key.startswith(f"{existing}_") or existing.startswith(f"{plate_key}_"):
            return existing
    return plate_key


async def generate_all_s9_segment_prompts(
    *,
    plan: S9StoryboardPlan,
    per_segment_slots: list[list[S9SegmentAssetSlot]],
    style_context: S9StyleContext,
    prev_episode_context: str = "",
    tail_anchor: bool = False,
    include_first_frame: bool = False,
    max_concurrency: int | None = None,
    source_text: str = "",
    source_slices_by_segment: list[str] | None = None,
    scene_context_notes_by_segment: list[str] | None = None,
    continuity_anchor: str = "",
    on_segment_done: Any = None,
) -> list[str]:
    """Generate Stage 2 continuous prompts across all 大段 with bounded concurrency.

    When ``plan.continuity_bible`` carries pre-locked scene lighting/layout rules,
    every segment receives those shared rules up-front and compiles in a single
    parallel wave without waiting on wave 1. Otherwise, legacy wave 1 -> wave 2
    lighting inheritance runs for same-scene segments.
    """

    effective_concurrency = (
        max(1, int(max_concurrency))
        if max_concurrency is not None
        else get_s9_llm_concurrency()
    )
    semaphore = asyncio.Semaphore(effective_concurrency)
    segments = plan.segments
    if not segments:
        return []

    bible = getattr(plan, "continuity_bible", None)
    has_prelocked_bible = bool(
        bible is not None
        and (
            bible.scene_lighting_rules
            or bible.scene_layout_rules
            or bible.prop_state_chain
            or bible.voice_profiles
        )
    )

    raw_scene_keys = [
        _primary_scene_key_for_slots(per_segment_slots[i] if i < len(per_segment_slots) else [])
        for i in range(len(segments))
    ]
    scene_keys: list[str] = []
    wave1_indices: list[int] = []
    wave2_indices: list[int] = []
    first_idx_by_scene: dict[str, int] = {}
    if has_prelocked_bible:
        scene_keys = list(raw_scene_keys)
        wave1_indices = list(range(len(segments)))
    else:
        for i, raw_skey in enumerate(raw_scene_keys):
            skey = _resolve_scene_lighting_group_key(raw_skey, first_idx_by_scene)
            scene_keys.append(skey)
            if skey and skey in first_idx_by_scene:
                wave2_indices.append(i)
            else:
                if skey:
                    first_idx_by_scene[skey] = i
                wave1_indices.append(i)

    results: list[str] = [""] * len(segments)

    async def _compile_one(idx: int, extra_anchor: str = "") -> str:
        seg = segments[idx]
        slots = per_segment_slots[idx] if idx < len(per_segment_slots) else []
        seg_notes = (
            scene_context_notes_by_segment[idx]
            if scene_context_notes_by_segment and idx < len(scene_context_notes_by_segment)
            else ""
        )
        seg_source = (
            source_slices_by_segment[idx]
            if source_slices_by_segment and idx < len(source_slices_by_segment)
            else (seg.source_excerpt or source_text)
        )
        bible_anchor = (
            format_s9_continuity_bible_for_segment(bible, segment=seg, slots=slots)
            if has_prelocked_bible
            else ""
        )
        combined_anchor = "\n\n".join(
            part
            for part in (continuity_anchor.strip(), bible_anchor.strip(), extra_anchor.strip())
            if part
        )
        async with semaphore:
            rendered = await run_s9_stage2_segment_prompt(
                segment=seg,
                slots=slots,
                style_context=style_context,
                prev_segment=segments[idx - 1] if idx > 0 else None,
                next_segment=segments[idx + 1] if idx + 1 < len(segments) else None,
                prev_episode_context=prev_episode_context if idx == 0 else "",
                tail_anchor=tail_anchor,
                include_first_frame=include_first_frame,
                source_text=seg_source,
                scene_context_notes=seg_notes,
                continuity_anchor=combined_anchor,
            )
        results[idx] = rendered
        if on_segment_done is not None:
            cb_res = on_segment_done(idx, rendered)
            if inspect.isawaitable(cb_res):
                await cb_res
        return rendered

    wave1_outputs = await asyncio.gather(*(_compile_one(i) for i in wave1_indices))
    if not has_prelocked_bible:
        scene_lighting_anchor: dict[str, str] = {}
        for idx, text in zip(wave1_indices, wave1_outputs):
            results[idx] = text
            skey = scene_keys[idx]
            if skey:
                lighting = sanitize_s9_lighting_anchor(extract_s9_lighting_style_block(text))
                if lighting:
                    scene_lighting_anchor[skey] = (
                        f"同场景首段（大段{segments[idx].segment_number}）已锁定的光影画风基准"
                        "（本段必须严格沿用同一主光来源、方向、时段与色彩基调，禁止改变；"
                        "引用场景图时请按本段【本大段素材槽位表】的实际 @图片N 编号书写，严禁照抄前段编号）：\n"
                        f"{lighting}"
                    )

        if wave2_indices:
            wave2_outputs = await asyncio.gather(
                *(
                    _compile_one(i, extra_anchor=scene_lighting_anchor.get(scene_keys[i], ""))
                    for i in wave2_indices
                )
            )
            for idx, text in zip(wave2_indices, wave2_outputs):
                results[idx] = text

    return results


def parse_s9_segment_meta(raw: Any) -> S9SegmentMeta | None:
    """Safely parse stored beat s9_segment_meta JSON/dict."""

    if not raw:
        return None
    if isinstance(raw, S9SegmentMeta):
        return raw
    payload: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text == "{}":
            return None
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return None
    if not isinstance(payload, dict) or not payload:
        return None
    try:
        return S9SegmentMeta.model_validate(payload)
    except Exception:
        return None


def parse_s9_subshots(raw: Any) -> list[S9SubShot]:
    """Safely parse stored s9_subshots list or JSON."""

    if not raw:
        return []
    payload: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text == "[]":
            return []
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return []
    if not isinstance(payload, list):
        return []
    shots: list[S9SubShot] = []
    for item in payload:
        try:
            shots.append(
                item if isinstance(item, S9SubShot) else S9SubShot.model_validate(item)
            )
        except Exception:
            continue
    return shots


def is_s9_segment_beat(beat: dict[str, Any] | Any) -> bool:
    """Return True if Beat represents an s9 multi-shot segment."""

    if isinstance(beat, dict):
        subshots = beat.get("s9_subshots") or parse_s9_subshots(beat.get("s9_subshots_json"))
        meta = beat.get("s9_segment_meta") or parse_s9_segment_meta(beat.get("s9_segment_meta_json"))
    else:
        subshots = parse_s9_subshots(getattr(beat, "s9_subshots_json", ""))
        meta = parse_s9_segment_meta(getattr(beat, "s9_segment_meta_json", ""))
    return bool(subshots) or meta is not None


def _match_resolved_asset_to_slot(
    slot: S9SegmentAssetSlot,
    assets: list[Any],
) -> Any | None:
    """Find best matching resolved asset in build_seedance2_project_assets output."""

    slot_id = str(slot.asset_id or "").strip().lower()
    slot_name = str(slot.name or "").strip().lower()
    want_media = slot.media_type
    want_kind = slot.asset_kind

    def _matches_kind(asset: Any) -> bool:
        key = str(getattr(asset, "key", "") or "")
        category = str(getattr(asset, "category", "") or "")
        if want_kind == "first_frame":
            return key == "first_frame"
        if want_kind == "identity":
            return category == "identity" or key.startswith("identity:")
        if want_kind == "scene":
            return category == "scene" or key.startswith("scene:")
        if want_kind == "scene_reverse":
            return category == "scene_reverse" or key.startswith("scene_reverse:")
        if want_kind == "prop":
            return category == "prop" or key.startswith("prop:")
        if want_kind == "voice":
            return category == "audio" or str(getattr(asset, "media_type", "")) == "audio"
        return False

    candidates = [
        a
        for a in assets
        if str(getattr(a, "media_type", "")) == want_media
        and _matches_kind(a)
        and bool(getattr(a, "exists", False))
        and bool(getattr(a, "path", None) or getattr(a, "url", None))
    ]
    if want_kind == "first_frame":
        return candidates[0] if candidates else None

    slot_sid, slot_vid = _normalize_scene_id_variant(
        str(slot.asset_id or "").strip(),
        str(slot.variant_id or "").strip(),
    )
    slot_plate_id = (f"{slot_sid}_{slot_vid}" if slot_vid else slot_sid).lower()

    # Pass 1: exact suffix/key match (prevents base "客厅" from stealing variant "客厅_狼藉版")
    for asset in candidates:
        key = str(getattr(asset, "key", "") or "").lower()
        suffix = key.split(":", 1)[-1] if ":" in key else key
        if slot_plate_id and (suffix == slot_plate_id or key == slot_plate_id):
            return asset
        if slot_id and (suffix == slot_id or key == slot_id):
            return asset

    # Pass 2: exact label / display suffix match
    for asset in candidates:
        label = str(getattr(asset, "label", "") or "").lower()
        label_tail = label.split("·")[-1].strip() if "·" in label else label
        if slot_plate_id and (label == slot_plate_id or label_tail == slot_plate_id):
            return asset
        if slot_name and (label == slot_name or label_tail == slot_name):
            return asset

    # Pass 3: substring fallback (prefer longest matching suffix so specific variants win)
    best_fuzzy: tuple[int, Any] | None = None
    for asset in candidates:
        key = str(getattr(asset, "key", "") or "").lower()
        label = str(getattr(asset, "label", "") or "").lower()
        suffix = key.split(":", 1)[-1] if ":" in key else key
        if slot_plate_id and (
            slot_plate_id in key
            or slot_plate_id in label
            or (suffix and suffix in slot_plate_id)
        ):
            score = len(suffix)
            if best_fuzzy is None or score > best_fuzzy[0]:
                best_fuzzy = (score, asset)
        elif slot_name and (slot_name in label or label in slot_name):
            score = len(label)
            if best_fuzzy is None or score > best_fuzzy[0]:
                best_fuzzy = (score, asset)
    if best_fuzzy is not None:
        return best_fuzzy[1]
    return candidates[0] if len(candidates) == 1 and want_kind == "scene" else None


def rebind_s9_prompt_with_active_assets(
    prompt: str,
    *,
    slots: list[S9SegmentAssetSlot],
    assets: list[Any],
    include_first_frame: bool = False,
) -> tuple[str, list[Any], list[dict[str, str]]]:
    """Remap @图片N/@音频N in an s9 prompt to match currently existing files 1..K cleanly.

    Returns ``(rebound_prompt, ordered_selected_assets, missing_slot_infos)``.
    Guarantees every remaining ``@图片N`` / ``@音频N`` in ``rebound_prompt`` refers
    within ``[1..len(attached)]`` so Seedance API validation never fails when a
    虾塘 asset hasn't been generated yet or ``include_first_frame`` is toggled.
    """

    ordered_assets: list[Any] = []
    seen_paths: set[str] = set()
    path_to_runtime: dict[tuple[str, str], int] = {}
    slot_to_runtime: dict[tuple[str, int], int] = {}
    slot_lookup = {(s.media_type, s.slot): s for s in slots}
    missing_slots: list[dict[str, str]] = []

    image_index = 0
    audio_index = 0

    if include_first_frame:
        ff_assets = [
            a for a in assets if str(getattr(a, "key", "")) == "first_frame"
        ]
        if ff_assets:
            ff = ff_assets[0]
            ff_path = str(getattr(ff, "path", "") or "")
            ordered_assets.append(ff)
            if ff_path:
                seen_paths.add(ff_path)
                path_to_runtime[("image", ff_path)] = 1
            image_index += 1

    # Walk configured segment slots in manifest order
    for slot in sorted(slots, key=lambda s: (0 if s.media_type == "image" else 1, s.slot)):
        if slot.asset_kind == "first_frame":
            continue
        matched = _match_resolved_asset_to_slot(slot, assets)
        if matched is None:
            missing_slots.append(
                {
                    "slot_label": f"{'@图片' if slot.media_type == 'image' else '@音频'}{slot.slot}",
                    "media_type": slot.media_type,
                    "asset_kind": slot.asset_kind,
                    "name": slot.name,
                    "asset_id": slot.asset_id,
                }
            )
            continue
        matched_path = str(getattr(matched, "path", "") or "")
        existing_runtime = path_to_runtime.get((slot.media_type, matched_path)) if matched_path else None
        if existing_runtime is not None:
            slot_to_runtime[(slot.media_type, slot.slot)] = existing_runtime
            continue
        if slot.media_type == "image":
            image_index += 1
            runtime_num = image_index
        else:
            audio_index += 1
            runtime_num = audio_index
        slot_to_runtime[(slot.media_type, slot.slot)] = runtime_num
        if matched_path:
            path_to_runtime[(slot.media_type, matched_path)] = runtime_num
            if matched_path not in seen_paths:
                ordered_assets.append(matched)
                seen_paths.add(matched_path)

    # Also include any additional selected assets (e.g. user manual uploads) after manifest slots,
    # keeping all image assets grouped before audio assets so trailing images never land after audios.
    extra_images: list[Any] = []
    extra_audios: list[Any] = []
    for asset in assets:
        key = str(getattr(asset, "key", "") or "")
        path = str(getattr(asset, "path", "") or "")
        if key == "first_frame" and not include_first_frame:
            continue
        if path and path not in seen_paths and getattr(asset, "selected", True):
            seen_paths.add(path)
            if str(getattr(asset, "media_type", "")) == "image":
                image_index += 1
                path_to_runtime[("image", path)] = image_index
                extra_images.append(asset)
            elif str(getattr(asset, "media_type", "")) == "audio":
                audio_index += 1
                path_to_runtime[("audio", path)] = audio_index
                extra_audios.append(asset)
            else:
                extra_audios.append(asset)

    if extra_images or extra_audios:
        base_images = [a for a in ordered_assets if str(getattr(a, "media_type", "")) == "image"]
        base_audios = [a for a in ordered_assets if str(getattr(a, "media_type", "")) != "image"]
        ordered_assets = [*base_images, *extra_images, *base_audios, *extra_audios]

    text = str(prompt or "")
    if slots and text.strip():
        def _replace_mention(match: re.Match[str]) -> str:
            prefix = match.group(1)
            raw_num = int(match.group(2))
            media_type = "image" if prefix == "图片" else "audio"
            runtime_idx = slot_to_runtime.get((media_type, raw_num))
            if runtime_idx is not None:
                return f"@{prefix}{runtime_idx}"
            # Keep if within manual trailing extras count
            total_media = image_index if media_type == "image" else audio_index
            slot_item = slot_lookup.get((media_type, raw_num))
            if slot_item is not None:
                return slot_item.name
            if 1 <= raw_num <= total_media:
                return f"@{prefix}{raw_num}"
            return f"@{prefix}{raw_num}"

        text = _MENTION_RE.sub(_replace_mention, text)
        # If first frame was newly injected as @图片1 and prompt doesn't describe it yet,
        # prepend a concise anchor line under 【参考职责】 when present.
        if include_first_frame and ordered_assets and str(getattr(ordered_assets[0], "key", "")) == "first_frame":
            ff_line = "@图片1 绑定开篇首帧渲染图，首镜动作严格自@图片1的构图、灯光与人物起始姿态向后发生。"
            if "@图片1 绑定开篇首帧" not in text:
                if "【参考职责】" in text:
                    text = text.replace("【参考职责】\n", f"【参考职责】\n{ff_line}\n", 1)
                else:
                    text = f"{ff_line}\n{text}"

    return text, ordered_assets, missing_slots


def resolve_s9_active_assets_and_prompt(
    beat: dict[str, Any] | Any,
    *,
    assets: list[Any],
    prompt: str,
    include_first_frame: bool = False,
) -> tuple[str, list[Any], list[dict[str, str]]]:
    """Rebind an s9 prompt against active assets and return renumbered assets."""

    _subshots, s9_meta, slots = build_s9_slots_from_beat(
        beat,
        include_first_frame=include_first_frame,
    )
    raw_slots = (
        list(s9_meta.segment_asset_slots)
        if s9_meta and s9_meta.segment_asset_slots
        else slots
    )
    effective_prompt, reordered_assets, missing_slots = rebind_s9_prompt_with_active_assets(
        prompt,
        slots=raw_slots,
        assets=assets,
        include_first_frame=include_first_frame,
    )
    return (
        effective_prompt,
        renumber_resolved_seedance2_assets(reordered_assets),
        missing_slots,
    )


def build_s9_scene_prop_consistency_notes(
    *,
    slots: list[S9SegmentAssetSlot],
    scenes: list[Any] | None = None,
    prop_menu: list[Any] | None = None,
    props: list[Any] | None = None,
    time_of_day: str = "",
    has_multiple_segments: bool = False,
) -> str:
    """Build deterministic scene lighting, spatial landmark, and prop color/visibility rules."""

    from novelvideo.models import build_scene_effective_prompt

    lines: list[str] = []
    scene_lookup = _scene_record_lookup(scenes)
    seen_scenes: set[str] = set()
    for slot in slots:
        if slot.asset_kind not in {"scene", "scene_reverse"} or not slot.asset_id:
            continue
        plate_key, scene_obj, base_obj = _resolve_scene_and_base(
            scene_lookup,
            slot.asset_id,
            slot.variant_id,
        )
        if plate_key in seen_scenes:
            continue
        seen_scenes.add(plate_key)
        scene_time = str(getattr(scene_obj, "time_of_day", "") or "").strip() if scene_obj else ""
        effective_time = str(time_of_day or "").strip() or scene_time
        if effective_time:
            lines.append(
                f"- 场景 `{plate_key}` 锁定光影时段为「{effective_time}」：严格沿用对应场景参考图的光源方向、色温与明暗基调，严禁擅自推进成夜晚/冷蓝调或凭空新增参考图没有的吊灯/灯具。"
            )
        else:
            lines.append(
                f"- 场景 `{plate_key}` 开启「锁图光（不重打光）」：主光源方向、冷暖色温与室内外明暗比例必须严格锁定场景参考图，严禁擅自改换时段或凭空添加参考图不存在的头顶吊灯/新光源。"
            )
        if scene_obj is not None:
            env_text = build_scene_effective_prompt(scene_obj, base_obj).strip()
            notes = str(getattr(scene_obj, "notes", "") or "").strip()
            if env_text:
                lines.append(f"- 场景 `{plate_key}` 参考图真实空间设定：{env_text}")
            if notes and notes not in env_text:
                lines.append(f"- 场景 `{plate_key}` 正/背面方位补充：{notes}")

    if has_multiple_segments:
        lines.append(
            "- 跨段同场景连续性：本集包含多个同场景大段，全集在未发生场景或时段切换时必须保持完全一致的光源方向、色温基调与陈设布局。"
        )

    prop_desc_by_id: dict[str, str] = {}
    for item in prop_menu or []:
        pid = str(
            (item.get("prop_id") or item.get("name") if isinstance(item, dict) else getattr(item, "prop_id", "") or getattr(item, "name", ""))
            or ""
        ).strip()
        if not pid:
            continue
        desc = str(
            (item.get("visual_prompt") or item.get("description") if isinstance(item, dict) else getattr(item, "visual_prompt", "") or getattr(item, "description", ""))
            or ""
        ).strip()
        if desc:
            prop_desc_by_id[pid] = desc
    for p in props or []:
        pid = str(getattr(p, "name", "") or "").strip()
        if pid and pid not in prop_desc_by_id:
            desc = str(getattr(p, "visual_prompt", "") or getattr(p, "description", "") or "").strip()
            if desc:
                prop_desc_by_id[pid] = desc

    prop_slots = [s for s in slots if s.asset_kind == "prop" and s.asset_id]
    if prop_slots:
        for s in prop_slots:
            desc = prop_desc_by_id.get(s.asset_id, "")
            desc_part = f"（外观约束：{desc}）" if desc else ""
            lines.append(
                f"- 道具 `{s.asset_id}`（@图片{s.slot}）{desc_part}：颜色、材质与造型必须严格遵循参考图与设定，禁止自行改色。"
            )
        lines.append(
            "- 关键道具可见性铁律：已选入本段的核心道具（尤其跨段随身携带的包袋/器物），除非剧情明确要求藏匿，否则严禁放到桌下、双膝等被前景遮挡的位置；对坐戏应放置在桌面可见区或怀中/手侧，确保中景画面清晰可见。"
        )

    return "\n".join(lines)


def renumber_resolved_seedance2_assets(ordered_assets: list[Any]) -> list[Any]:
    """Return ordered Seedance2ResolvedAsset items with sequential @图片N/@音频N labels."""

    img_num = 0
    aud_num = 0
    out: list[Any] = []
    for asset in ordered_assets:
        selected = bool(getattr(asset, "selected", False))
        media_type = str(getattr(asset, "media_type", ""))
        if selected and media_type == "image":
            img_num += 1
            out.append(
                dataclasses.replace(
                    asset,
                    image_number=img_num,
                    audio_number=None,
                    reference_label=f"图片{img_num}",
                )
            )
        elif selected and media_type == "audio":
            aud_num += 1
            out.append(
                dataclasses.replace(
                    asset,
                    image_number=None,
                    audio_number=aud_num,
                    reference_label=f"音频{aud_num}",
                )
            )
        else:
            out.append(asset)
    return out


def build_s9_slots_from_beat(
    beat: dict[str, Any] | Any,
    *,
    include_first_frame: bool = False,
) -> tuple[list[S9SubShot], S9SegmentMeta | None, list[S9SegmentAssetSlot]]:
    """Extract subshots, meta, and active slots from an s9 beat."""

    if isinstance(beat, dict):
        subshots = parse_s9_subshots(beat.get("s9_subshots") or beat.get("s9_subshots_json"))
        meta = parse_s9_segment_meta(beat.get("s9_segment_meta") or beat.get("s9_segment_meta_json"))
    else:
        subshots = parse_s9_subshots(getattr(beat, "s9_subshots_json", ""))
        meta = parse_s9_segment_meta(getattr(beat, "s9_segment_meta_json", ""))

    base_slots = list(meta.segment_asset_slots) if meta else []
    if not base_slots and subshots:
        seen_id: set[str] = set()
        seen_scene: set[str] = set()
        seen_prop: set[str] = set()
        seen_voice: set[str] = set()
        img_slots: list[S9SegmentAssetSlot] = []
        aud_slots: list[S9SegmentAssetSlot] = []
        for s in subshots:
            for iid in s.identity_ids:
                if iid and iid not in seen_id:
                    seen_id.add(iid)
                    img_slots.append(
                        S9SegmentAssetSlot(
                            slot=len(img_slots) + 1,
                            media_type="image",
                            asset_kind="identity",
                            name=iid.split("_", 1)[0] or iid,
                            asset_id=iid,
                        )
                    )
        for s in subshots:
            if s.scene_id and s.scene_id not in seen_scene:
                seen_scene.add(s.scene_id)
                img_slots.append(
                    S9SegmentAssetSlot(
                        slot=len(img_slots) + 1,
                        media_type="image",
                        asset_kind="scene",
                        name=s.scene_id,
                        asset_id=s.scene_id,
                        variant_id=s.scene_variant_id,
                    )
                )
        for s in subshots:
            for pid in s.prop_ids:
                if pid and pid not in seen_prop:
                    seen_prop.add(pid)
                    img_slots.append(
                        S9SegmentAssetSlot(
                            slot=len(img_slots) + 1,
                            media_type="image",
                            asset_kind="prop",
                            name=pid,
                            asset_id=pid,
                        )
                    )
        for s in subshots:
            spk_clean = str(s.speaker or "").strip()
            is_pure_narrator = "旁白" in spk_clean or spk_clean.lower() == "narrator"
            if (
                spk_clean
                and (s.audio_type == "dialogue" or (s.audio_type == "narration" and not is_pure_narrator))
                and spk_clean not in seen_voice
            ):
                seen_voice.add(spk_clean)
                clean_name = re.sub(r"\s*(?:OS|os)\s*$", "", spk_clean).strip()
                aud_slots.append(
                    S9SegmentAssetSlot(
                        slot=len(aud_slots) + 1,
                        media_type="audio",
                        asset_kind="voice",
                        name=(clean_name.split("_", 1)[0] or clean_name) if clean_name else spk_clean,
                        asset_id=clean_name or spk_clean,
                    )
                )
        # Fallback to beat-level detected fields when subshots lacked asset IDs
        if isinstance(beat, dict):
            if not img_slots:
                for iid in beat.get("detected_identities") or []:
                    clean_iid = str(iid or "").strip()
                    if clean_iid and clean_iid not in seen_id:
                        seen_id.add(clean_iid)
                        img_slots.append(
                            S9SegmentAssetSlot(
                                slot=len(img_slots) + 1,
                                media_type="image",
                                asset_kind="identity",
                                name=clean_iid.split("_", 1)[0] or clean_iid,
                                asset_id=clean_iid,
                            )
                        )
                sref = beat.get("scene_ref") or {}
                if isinstance(sref, dict) and str(sref.get("scene_id") or "").strip():
                    sid = str(sref.get("scene_id") or "").strip()
                    vid = str(sref.get("variant_id") or "").strip()
                    if sid not in seen_scene:
                        seen_scene.add(sid)
                        img_slots.append(
                            S9SegmentAssetSlot(
                                slot=len(img_slots) + 1,
                                media_type="image",
                                asset_kind="scene",
                                name=sid,
                                asset_id=sid,
                                variant_id=vid,
                            )
                        )
                for pid in beat.get("detected_props") or []:
                    clean_pid = str(pid or "").strip()
                    if clean_pid and clean_pid not in seen_prop:
                        seen_prop.add(clean_pid)
                        img_slots.append(
                            S9SegmentAssetSlot(
                                slot=len(img_slots) + 1,
                                media_type="image",
                                asset_kind="prop",
                                name=clean_pid,
                                asset_id=clean_pid,
                            )
                        )
        base_slots = img_slots + aud_slots

    if include_first_frame:
        ff_slot = S9SegmentAssetSlot(
            slot=1,
            media_type="image",
            asset_kind="first_frame",
            name="开篇首帧渲染图",
            asset_id="first_frame",
        )
        shifted = [
            s.model_copy(update={"slot": s.slot + 1})
            if s.media_type == "image" and s.asset_kind != "first_frame"
            else s
            for s in base_slots
            if s.asset_kind != "first_frame"
        ]
        return subshots, meta, [ff_slot, *shifted]

    return subshots, meta, [s for s in base_slots if s.asset_kind != "first_frame"]


def compute_s9_manifest_with_live_status(
    preview_meta_raw: Any,
    *,
    output_dir: str | Path,
    characters: list[Any],
    episode: Any,
) -> dict[str, Any]:
    """Hydrate S9EpisodePreviewMeta dict with live 已就绪 / 待绑定 status for each manifest item."""

    if not preview_meta_raw:
        return {}
    meta_dict = preview_meta_raw
    if isinstance(preview_meta_raw, str):
        text = preview_meta_raw.strip()
        if not text or text == "{}":
            return {}
        try:
            meta_dict = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return {}
    if not isinstance(meta_dict, dict) or not meta_dict:
        return {}

    from novelvideo.seedance2_i2v.voice_clone import (
        find_identity_reference_audio,
        resolve_character_voice,
    )
    from novelvideo.utils.path_resolver import (
        canonical_identity_path,
        canonical_identity_portrait_path,
        canonical_portrait_path,
        canonical_prop_reference_path,
        canonical_scene_master_path,
        canonical_scene_reverse_master_path,
    )

    project_root = Path(output_dir)
    ready_identities: set[str] = set()
    ready_scenes: set[str] = set()
    ready_reverse_scenes: set[str] = set()
    ready_props: set[str] = set()
    ready_voices: set[str] = set()

    for char in characters or []:
        char_name = str(getattr(char, "name", "") or "").strip()
        voice_candidates: list[Path] = []
        ref_audio = str(getattr(char, "reference_audio_path", "") or "").strip()
        if ref_audio:
            voice_candidates.append(
                Path(ref_audio) if Path(ref_audio).is_absolute() else project_root / ref_audio
            )
        if char_name:
            voice_candidates.append(
                project_root / "assets" / "characters" / char_name / "voices" / "voice_default.mp3"
            )
        has_char_voice = any(p.exists() for p in voice_candidates)
        if not has_char_voice and char_name:
            try:
                has_char_voice = resolve_character_voice(project_dir=project_root, character=char).audio_path is not None
            except Exception:
                has_char_voice = False
        if has_char_voice and char_name:
            ready_voices.add(char_name)
        for ident in list(getattr(char, "identities", []) or []):
            iid = str(getattr(ident, "identity_id", "") or "").strip()
            if not iid:
                continue
            ident_name = str(getattr(ident, "identity_name", "") or "").strip() or (
                iid.split("_", 1)[1] if "_" in iid else iid
            )
            candidates = [
                canonical_identity_path(project_root, char_name, ident_name),
                canonical_identity_portrait_path(project_root, char_name, ident_name),
                canonical_portrait_path(project_root, char_name),
            ]
            for portrait in list(getattr(ident, "portraits", []) or []):
                raw_p = str(getattr(portrait, "image_path", "") or "").strip()
                if raw_p:
                    candidates.append(Path(raw_p) if Path(raw_p).is_absolute() else project_root / raw_p)
            if any(p.exists() for p in candidates):
                ready_identities.add(iid)
            has_ident_voice = has_char_voice
            for attr_name in ("voice_sample_path", "reference_audio_path"):
                if has_ident_voice:
                    break
                raw_v = str(getattr(ident, attr_name, "") or "").strip()
                if raw_v:
                    vp = Path(raw_v) if Path(raw_v).is_absolute() else project_root / raw_v
                    if vp.exists():
                        has_ident_voice = True
            if not has_ident_voice and char_name and ident_name:
                if find_identity_reference_audio(project_root, char_name, ident_name) is not None:
                    has_ident_voice = True
            if not has_ident_voice:
                try:
                    has_ident_voice = (
                        resolve_character_voice(project_dir=project_root, character=char, identity=ident).audio_path
                        is not None
                    )
                except Exception:
                    has_ident_voice = False
            if has_ident_voice:
                ready_voices.add(iid)
                if char_name:
                    ready_voices.add(char_name)

    for item in list(getattr(episode, "scene_menu", []) or []) if episode else []:
        sid = str(getattr(item, "scene_id", "") or "").strip()
        vid = str(getattr(item, "variant_id", "") or "").strip()
        if not sid:
            continue
        scene_candidates = [canonical_scene_master_path(project_root, f"{sid}_{vid}") if vid else canonical_scene_master_path(project_root, sid)]
        if vid:
            scene_candidates.append(canonical_scene_master_path(project_root, sid))
        if "_" in sid:
            scene_candidates.append(canonical_scene_master_path(project_root, sid.split("_", 1)[0]))
        if any(p.exists() for p in scene_candidates):
            ready_scenes.add(sid)
        rev_candidates = [canonical_scene_reverse_master_path(project_root, f"{sid}_{vid}") if vid else canonical_scene_reverse_master_path(project_root, sid)]
        if vid:
            rev_candidates.append(canonical_scene_reverse_master_path(project_root, sid))
        if "_" in sid:
            rev_candidates.append(canonical_scene_reverse_master_path(project_root, sid.split("_", 1)[0]))
        if any(p.exists() for p in rev_candidates):
            ready_reverse_scenes.add(sid)

    for item in list(getattr(episode, "prop_menu", []) or []) if episode else []:
        pid = str(getattr(item, "prop_id", "") or getattr(item, "name", "") or "").strip()
        if not pid:
            continue
        prop_candidates = [canonical_prop_reference_path(project_root, pid)]
        ref_img = str(getattr(item, "reference_image_path", "") or "").strip() if not isinstance(item, dict) else str(item.get("reference_image_path") or "").strip()
        if ref_img:
            prop_candidates.append(Path(ref_img) if Path(ref_img).is_absolute() else project_root / ref_img)
        if any(p.exists() for p in prop_candidates):
            ready_props.add(pid)

    manifest = []
    for raw_item in meta_dict.get("asset_manifest") or []:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        kind = str(item.get("asset_kind") or "")
        aid = str(item.get("asset_id") or "").strip()
        name = str(item.get("name") or "").strip()
        is_ready = False
        if kind == "identity":
            is_ready = aid in ready_identities or any(
                rid.startswith(f"{name}_") for rid in ready_identities if name
            )
        elif kind == "scene":
            is_ready = aid in ready_scenes or name in ready_scenes
        elif kind == "scene_reverse":
            is_ready = aid in ready_reverse_scenes or name in ready_reverse_scenes
        elif kind == "prop":
            is_ready = aid in ready_props or name in ready_props
        elif kind == "voice":
            is_ready = aid in ready_voices or name in ready_voices
        item["status_label"] = "已就绪" if is_ready else "待绑定"
        manifest.append(item)

    result = dict(meta_dict)
    result["asset_manifest"] = manifest
    return result


def aggregate_segment_narration(shots: list[S9SubShot]) -> tuple[str, str, str]:
    """Compute Beat-level audio_type, speaker, and combined narration/dialogue summary."""

    spoken_parts: list[str] = []
    first_dialogue_speaker = ""
    has_dialogue = False
    has_narration = False

    for shot in shots:
        text = str(shot.spoken_text or "").strip()
        if shot.audio_type == "dialogue" and text:
            has_dialogue = True
            spk = str(shot.speaker or "").strip()
            if spk and not first_dialogue_speaker:
                first_dialogue_speaker = spk
            spoken_parts.append(f"{spk}：{text}" if spk else text)
        elif shot.audio_type == "narration" and text:
            has_narration = True
            spk = str(shot.speaker or "").strip()
            spoken_parts.append(f"[{spk}] {text}" if spk else text)

    if has_dialogue:
        audio_type = "dialogue"
    elif has_narration:
        audio_type = "narration"
    else:
        audio_type = "silence"

    return audio_type, first_dialogue_speaker, "\n".join(spoken_parts)


def aggregate_segment_visual_summary(segment: S9SegmentPlan) -> str:
    """Build opening-shot visual description used for the first frame and BeatCard."""

    if not segment.shots:
        return segment.story_beat or segment.title
    first = segment.shots[0]
    summary = f"【{first.shot_scale_movement}｜{first.camera_angle}】{first.content}"
    return summary.strip()


def compute_segment_subshots_update(
    *,
    subshots: list[S9SubShot],
    existing_meta: S9SegmentMeta | None,
    segment_title: str | None = None,
    planned_duration_seconds: int | None = None,
) -> dict[str, Any]:
    """Recompute Beat properties cleanly when user edits sub-shots in 脚本预览."""

    max_cap = existing_meta.max_segment_seconds if existing_meta else DEFAULT_S9_MAX_SEGMENT_SECONDS
    if planned_duration_seconds is not None and planned_duration_seconds > 0:
        seg_dur = max(MIN_S9_SEGMENT_SECONDS, min(max_cap, int(planned_duration_seconds)))
    else:
        summed = sum(float(s.planned_duration_seconds or 0) for s in subshots)
        seg_dur = max(MIN_S9_SEGMENT_SECONDS, min(max_cap, int(round(summed or 15))))

    fixed_shots = _normalize_subshot_timeline(subshots, seg_dur)
    audio_type, speaker, narration_summary = aggregate_segment_narration(fixed_shots)

    identities: list[str] = []
    props: list[str] = []
    first_scene_id = ""
    first_scene_var = ""
    for shot in fixed_shots:
        for iid in shot.identity_ids:
            if iid and iid not in identities:
                identities.append(iid)
        for pid in shot.prop_ids:
            if pid and pid not in props:
                props.append(pid)
        if not first_scene_id and shot.scene_id:
            first_scene_id = shot.scene_id
            first_scene_var = shot.scene_variant_id

    meta = (existing_meta or S9SegmentMeta()).model_copy(
        update={
            "planned_duration_seconds": seg_dur,
            **(
                {"segment_title": segment_title}
                if segment_title is not None
                else {}
            ),
        }
    )

    visual_desc = ""
    if fixed_shots:
        first = fixed_shots[0]
        visual_desc = f"【{first.shot_scale_movement}｜{first.camera_angle}】{first.content}".strip()

    return {
        "segment_title": segment_title if segment_title is not None else meta.segment_title,
        "planned_duration_seconds": seg_dur,
        "s9_subshots_json": json.dumps(
            [s.model_dump(mode="json") for s in fixed_shots],
            ensure_ascii=False,
        ),
        "s9_segment_meta_json": json.dumps(meta.model_dump(mode="json"), ensure_ascii=False),
        "audio_type": audio_type,
        "speaker": speaker,
        "narration_segment": narration_summary,
        "visual_description": visual_desc or None,
        "detected_identities": identities,
        "detected_props": props,
        "scene_id": first_scene_id,
        "scene_variant_id": first_scene_var,
        "duration_seconds": float(seg_dur),
        "estimated_duration": float(seg_dur),
    }


__all__ = [
    "DEFAULT_S9_MAX_SEGMENT_SECONDS",
    "MIN_S9_SEGMENT_SECONDS",
    "S9AssetManifestItem",
    "S9DurationAssessment",
    "S9EpisodeContinuityBible",
    "S9EpisodePreviewMeta",
    "S9SceneLayoutAnchor",
    "S9SceneLightingAnchor",
    "S9SegmentAssetSlot",
    "S9SegmentExpansionOutput",
    "S9SegmentMeta",
    "S9SegmentPlan",
    "S9Stage1SkeletonPlan",
    "S9Stage1SkeletonSegment",
    "S9StoryboardPlan",
    "S9StyleContext",
    "S9SubShot",
    "S9VoiceProfileAnchor",
    "S9_PROMPT_SOURCE",
    "S9_PROMPT_STYLE",
    "VALID_S9_MAX_SEGMENT_SECONDS",
    "aggregate_segment_narration",
    "aggregate_segment_visual_summary",
    "aspect_ratio_to_video_ratio",
    "audit_s9_plan_pacing",
    "audit_s9_segment_pacing",
    "build_s9_context_slice_from_episode_text",
    "build_s9_scene_prop_consistency_notes",
    "build_s9_stage1_system_prompt",
    "build_s9_stage1_task_prompt",
    "build_s9_stage2_fallback_prompt",
    "build_s9_stage2_system_prompt",
    "build_s9_stage2_task_prompt",
    "compute_segment_subshots_update",
    "compute_s9_manifest_with_live_status",
    "enrich_s9_continuity_bible",
    "extract_s9_lighting_style_block",
    "format_s9_continuity_bible_for_segment",
    "format_slot_table_for_stage2",
    "generate_all_s9_segment_prompts",
    "get_s9_llm_concurrency",
    "is_s9_segment_beat",
    "normalize_max_segment_seconds",
    "normalize_s9_storyboard_plan",
    "parse_s9_segment_meta",
    "parse_s9_subshots",
    "rebind_s9_prompt_with_active_assets",
    "resolve_s9_active_assets_and_prompt",
    "resolve_s9_style_context",
    "run_s9_stage1_plan",
    "run_s9_stage2_segment_prompt",
    "sanitize_s9_lighting_anchor",
    "slice_s9_source_text_for_segments",
    "text_uses_reverse_scene_view",
]
