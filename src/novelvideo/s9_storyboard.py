"""S9 Cinematic Segment Storyboard core schemas, prompts, validators, and asset binders.

Implements the two-stage workflow from ``老唐电影级分镜skill-s9``:
1. Stage 1 (Simplified Preview): Duration assessment, asset manifest, and coherent
   story segments (大段) capped at 30s or 15s, each containing timed sub-shots (分镜).
2. Stage 2 (Execution Prompt): Full Seedance 2.0/2.5 continuous multi-shot prompt
   per 大段 (【参考职责】【全局光影画风】【注意事项】【角色声音】 + 分镜1..M),
   plus dynamic runtime rebinding between manifest slots and generated 虾塘 assets.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
from pathlib import Path
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
    constraint_summary: str = Field(
        default="单段上限30秒",
        description="分段时长约束说明，如 单段上限30秒 / 单段上限15秒",
    )
    core_chain_seconds: str = Field(
        default="",
        description="核心表演/台词/动作链估算，如 约 55 秒（完整保留原文全部台词与关键反应）",
    )
    estimated_total_seconds: int = Field(default=0, ge=0, description="预计成片总时长（秒）")
    segment_count: int = Field(default=0, ge=0, description="大段数量")
    conclusion: str = Field(default="满足约束", description="评估结论")


class S9AssetManifestItem(BaseModel):
    """Single item in 【素材准备清单】."""

    slot_label: str = Field(description="如 @图片1 / @音频1")
    media_type: Literal["image", "audio"] = Field(description="image 或 audio")
    asset_kind: Literal["identity", "scene", "prop", "voice", "first_frame"] = Field(
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
    asset_kind: Literal["first_frame", "identity", "scene", "prop", "voice"]
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
    content: str = Field(default="", description="分镜内容（镜头画面+动作+对白/独白）")
    audio_type: Literal["dialogue", "narration", "silence"] = Field(default="silence")
    speaker: str = Field(default="", description="说话人/OS归属角色")
    spoken_text: str = Field(default="", description="本镜完整台词/内心独白/旁白（原词保留）")
    identity_ids: list[str] = Field(default_factory=list)
    scene_id: str = Field(default="")
    scene_variant_id: str = Field(default="")
    prop_ids: list[str] = Field(default_factory=list)


class S9SegmentPlan(BaseModel):
    """LLM Stage 1 output for one 大段."""

    segment_number: int = Field(default=1, ge=1)
    title: str = Field(default="", description="大段叙事小标题")
    planned_duration_seconds: int = Field(default=15, ge=1, description="本大段计划整数秒数")
    story_beat: str = Field(default="", description="本大段要完整讲完的叙事段落及衔接状态")
    shots: list[S9SubShot] = Field(default_factory=list)


class S9StoryboardPlan(BaseModel):
    """Structured Stage 1 output for a whole episode."""

    duration_assessment: S9DurationAssessment = Field(default_factory=S9DurationAssessment)
    asset_manifest: list[S9AssetManifestItem] = Field(default_factory=list)
    segments: list[S9SegmentPlan] = Field(default_factory=list)


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


class S9SegmentMeta(BaseModel):
    """Persisted per-Beat (per-大段) s9 metadata."""

    segment_number: int = 1
    segment_title: str = ""
    planned_duration_seconds: int = 15
    max_segment_seconds: int = DEFAULT_S9_MAX_SEGMENT_SECONDS
    story_beat: str = ""
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
        f"动画（{subtype.upper()}）"
        if family == "animation" and subtype
        else ("动画" if family == "animation" else "真人电影实拍")
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


def _format_planned_assets_summary(
    *,
    characters: list[Any],
    episode: Any,
    voice_available_names: set[str],
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

    scene_items = list(getattr(episode, "scene_menu", []) or []) if episode else []
    if scene_items:
        scene_lines = []
        for item in scene_items:
            sid = str(getattr(item, "scene_id", "") or "").strip()
            if not sid:
                continue
            vid = str(getattr(item, "variant_id", "") or "").strip()
            desc = str(getattr(item, "description", "") or getattr(item, "notes", "") or "").strip()
            scene_lines.append(
                f"- scene_id=`{sid}`"
                + (f" (变体: `{vid}`)" if vid else "")
                + (f" — {desc}" if desc else "")
            )
        if scene_lines:
            lines.append("【已规划场景菜单】\n" + "\n".join(scene_lines))

    prop_items = list(getattr(episode, "prop_menu", []) or []) if episode else []
    if prop_items:
        prop_lines = []
        for item in prop_items:
            pid = str(getattr(item, "prop_id", "") or getattr(item, "name", "") or "").strip()
            if not pid:
                continue
            desc = str(getattr(item, "description", "") or getattr(item, "visual_prompt", "") or "").strip()
            prop_lines.append(f"- prop_id=`{pid}`" + (f" — {desc}" if desc else ""))
        if prop_lines:
            lines.append("【已规划道具菜单】\n" + "\n".join(prop_lines))

    return "\n\n".join(lines) if lines else "（本集暂无预设资产清单，请从正文提取所需角色、场景、道具并填写名称）"


def build_s9_stage1_system_prompt(
    *,
    max_segment_seconds: int,
    spine_template: str = "drama",
    tail_anchor: bool = False,
) -> str:
    """Build the Stage 1 (Simplified Preview) system prompt faithful to s9 Skill."""

    cap = normalize_max_segment_seconds(max_segment_seconds)
    short_tail_floor = max(4, int(round(cap * 0.4)))
    subshot_range = "5～9个分镜" if cap >= 25 else "3～6个分镜"
    mode_rule = (
        "【当前为解说剧（narrated）模式】\n"
        "- 原文中的旁白、解说词、心里独白必须完整保留（不得删句/吞词），在对应分镜的 `audio_type` 标为 `narration`，"
        "`spoken_text` 填完整旁白词，`speaker` 填讲述视角（如 `林默 OS` 或 `旁白`）；\n"
        "- 原文中的明确角色对话标为 `dialogue`，`speaker` 填说话角色身份ID，`spoken_text` 填原句对白。"
        if spine_template == "narrated"
        else
        "【当前为精品剧（drama）模式】\n"
        "- 完整保留原文中的全部对白、关键内心独白（OS）与核心戏剧动作；\n"
        "- 角色开口对白标 `dialogue`（`speaker` 填对应身份ID，`spoken_text` 保留原话）；"
        "内心独白/画外音标 `narration`（`speaker` 填 `角色名 OS`）；纯画面反应/动作/空镜标 `silence`。"
    )
    anchor_note = (
        "已开启「尾帧锚点镜」：在需要跨段衔接的大段末尾预留 1 秒稳态静止锚点缓冲。"
        if tail_anchor
        else "「尾帧锚点镜」默认关闭：大段结尾应设计自然可承接下一大段的动作/视线/环境延展，不强行定格静止。"
    )

    return f"""你是老唐电影级影视分镜导演（s9 · 整片连续模式 Stage 1）。你的任务是把整集剧本精确拆分为可一步生成连贯视频的「大段」（每大段一个完整小故事闭环），并产出可供导演审阅的结构化「简易预览版分镜（S9StoryboardPlan）」。

### 1. 硬性时间与分段铁律（严禁违背）
1. **单大段硬上限**：每一大段的 `planned_duration_seconds` 必须是整数秒，且 **严格满足 `4 <= planned_duration_seconds <= {cap}`**（本次用户指定单段上限：**{cap} 秒**）。
2. **故事闭环与段间衔接**：不要为了凑秒数机械切碎！每一个大段必须在 `{cap}` 秒内讲完一个完整、连贯的小剧情节拍（起势 → 推进 → 小高潮/反应落点），且大段结尾的状态能顺畅过渡到下一大段开篇。
3. **基于内容的真实语速与节奏估算时长**：
   - 有声台词/旁白按自然表演语速估算（普通话约 3～4 字/秒 + 必要换气停顿），禁止人为慢放拉长或强塞无声慢镜头。
   - 纯动作/建立/反应镜头按真实物理耗时（通常 1.5～4 秒）。
4. **短尾三级校验与消解**：
   - 当总大段数 >= 2 时，最后一大段的时长 **不得短于 {short_tail_floor} 秒**（上限 {cap} 秒的 40%）。
   - 若初算末段过短：① 优先回查前段可压缩动作余量合入；② 若合并会超 {cap} 秒，则将最后两段重平衡为相近段长；③ 仅当仅1段或物理不可拆时才例外保留。
5. **时间轴严谨对齐**：
   - 每大段内部包含 **{subshot_range}**（可随节奏灵活调整，保证电影级密度与呼吸感）。
   - 每个大段内所有 `分镜` 的 `start_seconds` / `end_seconds` 从 `0` 连续拼接，最后一个分镜的 `end_seconds` **必须精确等于** 该大段的 `planned_duration_seconds`。

### 2. s9 专业分镜导演核心规则（8步内化执行）
- **双轨推进与信息分级**：核心剧情按起承转合推进，**每个分镜聚焦 1～2 个清晰视觉/叙事信息点**；情绪重击前/后插入反应镜、细节特写或微停顿。
- **空间双重锚定**：同一场景内连续分镜保持人物屏幕左右站位、视线方向（左看右/右看左）与机位轴线一致，绝不左右跳轴；切换场景必须在首镜完成空间锚定（人物相对环境与道具的位置）。
- **景别节奏与运镜克制**：远景/全景/中景/近景/特写错落搭配，**严禁连续 3 镜使用相同景别**；用有叙事动机的克制推近、微幅跟摇、固定机位代替无意义大幅甩镜炫技。
- **资产名称一致（极重要）**：在 `asset_manifest` 列出全集通用 `@图片1..N`（角色→场景→道具顺序）和 `@音频1..M`（有发声角色），并在每个分镜的 `identity_ids`、`scene_id`、`prop_ids` 尽量引用给定菜单中的精确 ID，未在菜单里的也可按真实名称填写。
- **双剧型台词处理**：
{mode_rule}
- **尾帧衔接开关**：
  {anchor_note}

### 3. 输出字段约束
- 输出严格的 `S9StoryboardPlan` JSON。
- `asset_manifest` 中每项标明 `slot_label`（`@图片1`, `@图片2`, ... `@音频1`）、`media_type`、`asset_kind`、`name`、`asset_id`。
- 每个分镜的 `content` 用精炼中文写出可直接用于审阅的画面+动作+声音概括（类似 s9 简易预览版 `分镜内容` 栏），`spoken_text` 单独保留完整台词/旁白原句以便精确口型与音频对齐。"""


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
        f"\n【上一集末尾衔接上下文（本集 EP{episode_num} 第一大段需顺畅承接或自然开场）】\n{prev_episode_context.strip()}\n"
        if prev_episode_context.strip()
        else ""
    )
    return f"""请对第 {episode_num} 集《{episode_title or f'EP{episode_num}'}》进行 s9 整片连续模式拆分，单大段上限 **{cap} 秒**。

【项目视觉规格】
- {style_context.s9_style_anchor}
{prev_block}
【本集已有资产与音色】
{planned_assets_summary}

【本集完整原文 / 工作稿】
{source_text.strip()}

请先完成【时长评估】与【素材准备清单】，再按故事完整段落拆分为若干大段（每大段 <= {cap} 秒且讲述完整小段落），细化每大段内部的分镜 `镜头基础信息` 与 `分镜内容`。"""


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


def _match_scene_id(raw_scene: str, episode: Any) -> tuple[str, str]:
    clean = str(raw_scene or "").strip()
    if not clean:
        return "", ""
    scene_menu = list(getattr(episode, "scene_menu", []) or []) if episode else []
    for item in scene_menu:
        sid = str(getattr(item, "scene_id", "") or "").strip()
        vid = str(getattr(item, "variant_id", "") or "").strip()
        if sid == clean or (vid and f"{sid}_{vid}" == clean):
            return sid, vid
    for item in scene_menu:
        sid = str(getattr(item, "scene_id", "") or "").strip()
        vid = str(getattr(item, "variant_id", "") or "").strip()
        if sid and (clean in sid or sid in clean):
            return sid, vid
    return clean, ""


def _match_prop_id(raw_prop: str, episode: Any) -> str:
    clean = str(raw_prop or "").strip()
    if not clean:
        return ""
    prop_menu = list(getattr(episode, "prop_menu", []) or []) if episode else []
    for item in prop_menu:
        pid = str(getattr(item, "prop_id", "") or getattr(item, "name", "") or "").strip()
        if pid == clean:
            return pid
    for item in prop_menu:
        pid = str(getattr(item, "prop_id", "") or getattr(item, "name", "") or "").strip()
        if pid and (clean in pid or pid in clean):
            return pid
    return clean


def _normalize_subshot_timeline(shots: list[S9SubShot], target_duration: int) -> list[S9SubShot]:
    """Ensure sub-shots form a contiguous [0, target_duration] timeline in 0.5s grid."""

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

    normalized: list[S9SubShot] = []
    cursor = 0.0
    count = len(shots)
    for idx, shot in enumerate(shots):
        shot_num = idx + 1
        if idx == count - 1:
            end_sec = float(target_duration)
        else:
            step = max(0.5, round((raw_durations[idx] * scale) * 2.0) / 2.0)
            remaining_min = (count - 1 - idx) * 1.0
            max_end = max(cursor + 0.5, float(target_duration) - remaining_min)
            end_sec = min(max_end, cursor + step)
        dur = max(0.5, round((end_sec - cursor) * 2.0) / 2.0)
        end_sec = round(cursor + dur, 1)
        if idx == count - 1:
            end_sec = float(target_duration)
            dur = round(max(0.5, end_sec - cursor), 1)
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


def normalize_s9_storyboard_plan(
    plan: S9StoryboardPlan,
    *,
    max_segment_seconds: int = DEFAULT_S9_MAX_SEGMENT_SECONDS,
    characters: list[Any] | None = None,
    episode: Any = None,
    max_images_per_segment: int = 9,
    max_audios_per_segment: int = 3,
) -> tuple[S9StoryboardPlan, list[list[S9SegmentAssetSlot]]]:
    """Validate/repair durations, timelines, short tails, and segment asset slots."""

    cap = normalize_max_segment_seconds(max_segment_seconds)
    valid_identities, char_to_default, _voice_by_key = _coerce_identity_lookup(
        list(characters or []),
        episode,
    )

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

        # Normalize asset references per sub-shot and collect segment ordered manifest
        seg_identity_ids: list[str] = []
        seg_scene_keys: list[tuple[str, str]] = []
        seg_prop_ids: list[str] = []
        seg_speakers: list[str] = []

        cleaned_shots: list[S9SubShot] = []
        for shot in fixed_shots:
            label = shot.label.strip() or f"大段{seg_idx}-分镜{shot.shot_number}"
            mapped_identities: list[str] = []
            for raw_id in shot.identity_ids:
                resolved = _match_identity_id(raw_id, valid_identities, char_to_default)
                if resolved and resolved not in mapped_identities:
                    mapped_identities.append(resolved)
                if resolved and resolved not in seg_identity_ids:
                    seg_identity_ids.append(resolved)

            scene_id, scene_var = _match_scene_id(shot.scene_id, episode)
            if scene_id:
                pair = (scene_id, scene_var)
                if pair not in seg_scene_keys:
                    seg_scene_keys.append(pair)

            mapped_props: list[str] = []
            for raw_prop in shot.prop_ids:
                pid = _match_prop_id(raw_prop, episode)
                if pid and pid not in mapped_props:
                    mapped_props.append(pid)
                if pid and pid not in seg_prop_ids:
                    seg_prop_ids.append(pid)

            speaker_clean = str(shot.speaker or "").strip()
            if shot.audio_type in {"dialogue", "narration"} and speaker_clean:
                is_pure_os = "旁白" in speaker_clean or speaker_clean.lower() == "narrator"
                if shot.audio_type == "dialogue" or (not is_pure_os):
                    resolved_spk = _match_identity_id(speaker_clean, valid_identities, char_to_default)
                    if resolved_spk:
                        if shot.audio_type == "dialogue" and resolved_spk not in mapped_identities:
                            mapped_identities.append(resolved_spk)
                        if resolved_spk not in seg_identity_ids and (
                            resolved_spk in valid_identities or resolved_spk in char_to_default.values()
                        ):
                            seg_identity_ids.append(resolved_spk)
                        if shot.audio_type == "dialogue" and resolved_spk not in seg_speakers:
                            seg_speakers.append(resolved_spk)

            cleaned_shots.append(
                shot.model_copy(
                    update={
                        "label": label,
                        "identity_ids": mapped_identities,
                        "scene_id": scene_id,
                        "scene_variant_id": scene_var or shot.scene_variant_id,
                        "prop_ids": mapped_props,
                    }
                )
            )

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
        for sid, vid in seg_scene_keys:
            if len(image_slots) >= max(1, max_images_per_segment):
                break
            image_slots.append(
                S9SegmentAssetSlot(
                    slot=len(image_slots) + 1,
                    media_type="image",
                    asset_kind="scene",
                    name=f"{sid}({vid})" if vid else sid,
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
                {"identity": 0, "scene": 1, "prop": 2, "voice": 3, "first_frame": 4}.get(
                    x.asset_kind, 9
                ),
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
    assessment = (plan.duration_assessment or S9DurationAssessment()).model_copy(
        update={
            "constraint_summary": f"单段上限{cap}秒（含短尾三步校验）",
            "estimated_total_seconds": total_seconds,
            "segment_count": len(normalized_segments),
            "core_chain_seconds": (
                plan.duration_assessment.core_chain_seconds
                or f"约 {total_seconds} 秒（共 {len(normalized_segments)} 大段）"
            ),
            "conclusion": "满足约束",
        }
    )

    return (
        S9StoryboardPlan(
            duration_assessment=assessment,
            asset_manifest=numbered_manifest,
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
        "scene": "场景",
        "prop": "道具",
        "voice": "角色音色",
    }
    for s in slots:
        prefix = "@图片" if s.media_type == "image" else "@音频"
        kind_text = kind_labels.get(s.asset_kind, s.asset_kind)
        id_suffix = f"（资产ID: {s.asset_id}）" if s.asset_id and s.asset_id != s.name else ""
        lines.append(f"- {prefix}{s.slot} -> {kind_text}：{s.name}{id_suffix}")
    return "\n".join(lines)


def build_s9_stage2_system_prompt(
    *,
    tail_anchor: bool = False,
    include_first_frame: bool = False,
) -> str:
    """Build Stage 2 full Seedance 2.0/2.5 continuous-mode prompt generator instructions."""

    tail_rule = (
        "【尾帧锚点镜规则（已开启）】\n"
        "- 本大段最后一个分镜结束后，额外追加一段 `【尾帧锚点镜】`（占末段约1秒）：画面中所有人物与物体完全静止保持同一稳态姿态与构图，无新增运镜、无新台词，作为下一大段首尾衔接的清晰锚点。"
        if tail_anchor
        else
        "【尾帧衔接规则（默认无锚点定格）】\n"
        "- 禁止为衔接硬造 1 秒完全静止僵硬定格；每一分镜的 `- 尾帧与转场` 应通过动作顺延、视线引导、前景掠过或同向运镜收束自然切入下一镜/下一大段。"
    )
    first_frame_rule = (
        "- 本大段已纳入 `首帧渲染图` 作为 `@图片1`：首镜起幅必须严格锚定 `@图片1` 的构图、光线与人物起始姿态，只描写 `t=0` 之后发生的连续动作。"
        if include_first_frame
        else "- 本大段采用纯资产全能参考模式（未锁定单张首帧渲染图），首镜请根据角色 `@图片`、场景 `@图片`、道具 `@图片` 主动构图建立空间位置与起势。"
    )

    return f"""你是老唐电影级影视分镜提示词大师（s9 · Stage 2 整片连续模式执行方案）。
你的唯一任务是：**严格根据给定的【大段规划】【内部各分镜】【本段可用素材索引表】【视觉画风规则】**，直接输出一段**立即可复制进 Seedance 2.0 / 2.5 主体提示词框执行的纯文本完整生成提示词**。

### 输出结构铁律（必须严格保留以下段落标题与结构）
1. `【参考职责】`：逐条明确本大段中每一个 `@图片N`（绑定角色脸型服饰/场景空间结构/道具造型特征）和 `@音频N`（仅绑定说话人核心音色，按情绪贴合台词节奏）的精确职责。
   {first_frame_rule}
2. `【全局光影画风】`：围绕项目指定的视觉画风，具体展开四行：`画风锁定`、`光线质感`、`材质纹理`、`色调氛围`。
3. `【注意事项】`：写明物理自然、杜绝瞬移/多指穿模/背景错位、口型与台词同步、**禁止自动添加背景音乐（BGM）**、**严禁出现字幕/文字水印/胶片边框**、首尾衔接流畅。
4. `【角色声音】`：写出本段每个发声角色 `@音频N` 的音色质感、语速情绪基调及内心独白处理方式（内心独白/旁白为画外音，角色嘴唇闭合）。
5. 连续分镜块（`分镜1（0秒到X秒）：` … 直到 `分镜M（Y秒到D秒）：`，精确对齐给定秒数）：
   每个分镜下必须写出清晰可执行的五要素：
   - `- 空间与主体：`（严守场景内屏幕左右站位、朝向与视线锚定）
   - `- 动态时间轴：`（按起手→过程→落点写透物理微动作与运镜方式）
   - `- 物理与光影：`（反光、雨雪风动、衣摆/手指微颤等细节）
   - `- 音频与对白：`（引用 `@音频N`，**一字不改写入台词/独白原文**，说明嘴部动作/OS画外音与写实环境拟音 SFX，禁止提及 BGM）
   - `- 尾帧与转场：`（如何自然过渡到下一分镜或下一大段）
{tail_rule}

### 核心红线
- **引用精准一致**：正文内只能使用下方【本段可用素材索引表】里实际存在的 `@图片N` / `@音频N` 编号，禁止凭空编造表外编号！
- **有参考图不重复写静态外貌**：凡已在 `@图片N` 绑定参考图的角色/场景/道具，主体描述中直接写 `@图片N` + 角色名 + 动态神态/污渍/动作/光影反应，禁止把参考图里本就固定的长相、身高、常规衣着再啰嗦抄一遍。
- **纯净直出**：不要输出 Markdown 标题符号（如 `###`）、不要输出代码块围栏 ```、不要附加额外解释废话，直接输出【参考职责】至最后一镜的纯净提示词正文。"""


def build_s9_stage2_task_prompt(
    *,
    segment: S9SegmentPlan,
    slots: list[S9SegmentAssetSlot],
    style_context: S9StyleContext,
    prev_segment: S9SegmentPlan | None = None,
    next_segment: S9SegmentPlan | None = None,
    prev_episode_context: str = "",
    user_guidance: str = "",
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
        shots_lines.append(
            f"- {shot.label or f'分镜{shot.shot_number}'}（{start_s}秒到{end_s}秒）《{shot.title}》\n"
            f"  基础信息：计划{_format_seconds_label(shot.planned_duration_seconds)}秒｜"
            f"{shot.shot_scale_movement}｜{shot.camera_angle}｜{shot.camera_motion}{spoken_note}\n"
            f"  分镜内容：{shot.content}"
        )

    continuity_parts: list[str] = []
    if prev_segment is not None:
        continuity_parts.append(
            f"- 前一大段（大段{prev_segment.segment_number}·{prev_segment.title}）结尾事件：{prev_segment.story_beat}"
        )
    elif prev_episode_context.strip():
        continuity_parts.append(f"- 上一集末尾语境：{prev_episode_context.strip()}")
    if next_segment is not None:
        continuity_parts.append(
            f"- 下一大段（大段{next_segment.segment_number}·{next_segment.title}）即将承接：{next_segment.story_beat}"
        )
    continuity_block = "\n".join(continuity_parts) if continuity_parts else "- 本段独立完成当前闭环"

    guidance_block = (
        f"\n【用户附加微调要求（必须融入本段执行方案）】\n{user_guidance.strip()}\n"
        if user_guidance.strip()
        else ""
    )

    return f"""请为「大段{segment.segment_number}：{segment.title}」（计划总时长 **{segment.planned_duration_seconds}秒**）生成完整的 Seedance 2.0/2.5 生成提示词：

【项目视觉与画风要求】
{style_context.s9_style_anchor}

【本段可用素材索引表（仅允许使用下列编号）】
{format_slot_table_for_stage2(slots)}

【上下文衔接要求】
{continuity_block}

【本大段叙事核心】
{segment.story_beat}

【本大段内部各分镜规划（时间轴共 {segment.planned_duration_seconds} 秒）】
{chr(10).join(shots_lines)}
{guidance_block}
请立即直接输出纯净的 Stage 2 完整生成提示词文本。"""


def build_s9_stage2_fallback_prompt(
    *,
    segment: S9SegmentPlan,
    slots: list[S9SegmentAssetSlot],
    style_context: S9StyleContext,
    tail_anchor: bool = False,
) -> str:
    """Deterministic fallback builder matching exact s9 prompt structure."""

    duty_lines: list[str] = []
    voice_lines: list[str] = []
    for s in slots:
        if s.media_type == "image":
            if s.asset_kind == "first_frame":
                duty_lines.append(
                    f"@图片{s.slot} 绑定开篇首帧构图、人物起始姿态与光影基调，首镜动作自该画面状态向后连续发生。"
                )
            elif s.asset_kind == "identity":
                duty_lines.append(
                    f"@图片{s.slot} 仅绑定{s.name}的脸型五官、发型与核心服饰造型，严格维持跨镜角色一致性。"
                )
            elif s.asset_kind == "scene":
                duty_lines.append(
                    f"@图片{s.slot} 仅绑定{s.name}的空间布局、建筑材质与光源氛围。"
                )
            else:
                duty_lines.append(
                    f"@图片{s.slot} 仅绑定道具{s.name}的造型结构、材质纹理与视觉细节。"
                )
        else:
            duty_lines.append(
                f"@音频{s.slot} 仅绑定{s.name}的核心声线音色，贴合情绪与呼吸节奏演绎对白。"
            )
            voice_lines.append(f"@音频{s.slot}（{s.name}）音色贴合人物年龄性格，口播咬字自然清晰")

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
        audio_desc = "环境写实拟音与自然呼吸声，无任何背景音乐。"
        if shot.spoken_text:
            speaker_tag = shot.speaker or "角色"
            voice_ref = voice_slot_by_id.get(speaker_tag) or voice_slot_by_name.get(
                speaker_tag.split("_", 1)[0].lower(), ""
            )
            if shot.audio_type == "narration":
                audio_desc = (
                    f"{speaker_tag}内心独白/画外音（人物嘴唇闭合）：\"{shot.spoken_text}\"；"
                    "叠加轻柔真实环境拟音，禁止配乐。"
                )
            else:
                ref_part = f"以{voice_ref}声线" if voice_ref else ""
                audio_desc = (
                    f"{speaker_tag}{ref_part}清晰开口说出：\"{shot.spoken_text}\"，嘴唇开合精准同步；"
                    "伴随现场真实拟音。"
                )
        next_hint = (
            "画面动作、视线与运镜自然延展并切入下一分镜。"
            if idx + 1 < len(segment.shots)
            else (
                "末尾保持人物动作微收束，视线与空间关系稳妥承接下一大段。"
                if not tail_anchor
                else "末段动作收束落定后保留1秒平稳姿态，便于下一段首尾衔接。"
            )
        )
        shot_blocks.append(
            f"分镜{shot.shot_number}（{start_s}秒到{end_s}秒）：\n"
            f"- 空间与主体：{shot.shot_scale_movement}，{shot.camera_angle}，{shot.content}\n"
            f"- 动态时间轴：镜头{shot.camera_motion}，主体动作连贯推进，细节节奏贴合真实表演时长。\n"
            f"- 物理与光影：保持{style_context.style_label}统一光影反射与自然物理惯性。\n"
            f"- 音频与对白：{audio_desc}\n"
            f"- 尾帧与转场：{next_hint}"
        )

    sections = [
        "【参考职责】\n" + ("\n".join(duty_lines) if duty_lines else "纯文生画面与连贯时空演绎。"),
        f"【全局光影画风】\n"
        f"画风锁定：{style_context.style_label}，{style_context.style_instructions or '写实影视质感，杜绝僵硬静态感'}；\n"
        f"光线质感：主辅光源层次分明，明暗过渡与物体材质反光符合真实物理规律；\n"
        f"材质纹理：人物肌肤细节、服装织物与环境器物纹理清晰可辨；\n"
        f"色调氛围：保持 {style_context.aspect_ratio} 统一电影级色彩调性与暗部通透层次。",
        "【注意事项】\n"
        "全程物理运动自然连贯，严禁人物变形、多指穿模、道具瞬移或背景错位；严格维持同一场景内的屏幕左右方位与视线方向一致；"
        "所有台词与嘴部动作精准同步；禁止自动添加任何背景音乐（BGM）；严禁出现字幕、文字水印或画面边框。",
        "【角色声音】\n"
        + ("；".join(voice_lines) + "；无对白段落保持写实环境音。" if voice_lines else "以画外内心独白与现场真实环境音为主，不添加配乐。"),
        "\n\n".join(shot_blocks),
    ]
    if tail_anchor:
        sections.append(
            "【尾帧锚点镜】\n"
            "最后1秒画面中人物与所有环境要素保持稳态定格构图，无新增运镜与新台词，提供下一大段平滑衔接点。"
        )
    return "\n\n".join(sections)


def _strip_code_fences(text: str) -> str:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    return cleaned.strip()


async def run_s9_stage1_plan(
    *,
    source_text: str,
    episode_num: int,
    episode_title: str,
    style_context: S9StyleContext,
    characters: list[Any],
    episode: Any,
    voice_available_names: set[str],
    max_segment_seconds: int = DEFAULT_S9_MAX_SEGMENT_SECONDS,
    spine_template: str = "drama",
    tail_anchor: bool = False,
    prev_episode_context: str = "",
) -> S9StoryboardPlan:
    """Invoke LLM to compute Stage 1 S9StoryboardPlan."""

    from pydantic_ai import Agent

    from novelvideo.config import (
        get_newapi_structured_output_model_settings,
        get_newapi_text_pydantic_model,
    )

    cap = normalize_max_segment_seconds(max_segment_seconds)
    system_prompt = build_s9_stage1_system_prompt(
        max_segment_seconds=cap,
        spine_template=spine_template,
        tail_anchor=tail_anchor,
    )
    planned_summary = _format_planned_assets_summary(
        characters=characters,
        episode=episode,
        voice_available_names=voice_available_names,
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
            "LITERAL_SCRIPT_MODEL",
            brainclaw_profile=BrainClawProfile.LITERAL_BEAT_METADATA,
            capability="text.generate.agent",
        ),
        system_prompt=system_prompt,
        output_type=S9StoryboardPlan,
        model_settings=get_newapi_structured_output_model_settings(),
        retries=3,
        name="S9 Stage1 Storyboard Planner",
    )
    result = await agent.run(task_prompt)
    return result.output


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
) -> str:
    """Invoke LLM to compile Stage 2 full Seedance 2.0/2.5 prompt for one 大段."""

    from pydantic_ai import Agent

    from novelvideo.config import get_newapi_text_pydantic_model

    system_prompt = build_s9_stage2_system_prompt(
        tail_anchor=tail_anchor,
        include_first_frame=include_first_frame,
    )
    task_prompt = build_s9_stage2_task_prompt(
        segment=segment,
        slots=slots,
        style_context=style_context,
        prev_segment=prev_segment,
        next_segment=next_segment,
        prev_episode_context=prev_episode_context,
        user_guidance=user_guidance,
    )
    agent = Agent(
        get_newapi_text_pydantic_model(
            "SEEDANCE2_PROMPT_MODEL",
            brainclaw_profile=BrainClawProfile.SEEDANCE2_PROMPT_COMPOSITION,
            capability="text.generate.agent",
        ),
        system_prompt=system_prompt,
        output_type=str,
        retries=2,
        name="S9 Stage2 Segment Prompt Director",
    )
    try:
        result = await agent.run(task_prompt)
        rendered = _strip_code_fences(result.output)
        if rendered:
            return rendered
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


async def generate_all_s9_segment_prompts(
    *,
    plan: S9StoryboardPlan,
    per_segment_slots: list[list[S9SegmentAssetSlot]],
    style_context: S9StyleContext,
    prev_episode_context: str = "",
    tail_anchor: bool = False,
    include_first_frame: bool = False,
    max_concurrency: int = 4,
) -> list[str]:
    """Generate Stage 2 continuous prompts across all 大段 with bounded concurrency."""

    semaphore = asyncio.Semaphore(max(1, max_concurrency))
    segments = plan.segments

    async def _compile_one(idx: int) -> str:
        async with semaphore:
            return await run_s9_stage2_segment_prompt(
                segment=segments[idx],
                slots=per_segment_slots[idx],
                style_context=style_context,
                prev_segment=segments[idx - 1] if idx > 0 else None,
                next_segment=segments[idx + 1] if idx + 1 < len(segments) else None,
                prev_episode_context=prev_episode_context if idx == 0 else "",
                tail_anchor=tail_anchor,
                include_first_frame=include_first_frame,
            )

    return await asyncio.gather(*(_compile_one(i) for i in range(len(segments))))


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
            return category == "scene" or key.startswith("scene")
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

    for asset in candidates:
        key = str(getattr(asset, "key", "") or "").lower()
        label = str(getattr(asset, "label", "") or "").lower()
        suffix = key.split(":", 1)[-1] if ":" in key else key
        if slot_id and (suffix == slot_id or key == slot_id or slot_id in key or slot_id in label):
            return asset
    for asset in candidates:
        label = str(getattr(asset, "label", "") or "").lower()
        if slot_name and (slot_name == label or slot_name in label or label in slot_name):
            return asset
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
            ordered_assets.append(ff)
            seen_paths.add(str(getattr(ff, "path", "")))
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
        if slot.media_type == "image":
            image_index += 1
            runtime_num = image_index
        else:
            audio_index += 1
            runtime_num = audio_index
        slot_to_runtime[(slot.media_type, slot.slot)] = runtime_num
        if matched_path and matched_path not in seen_paths:
            ordered_assets.append(matched)
            seen_paths.add(matched_path)

    # Also include any additional selected assets (e.g. user manual uploads) after manifest slots
    for asset in assets:
        key = str(getattr(asset, "key", "") or "")
        path = str(getattr(asset, "path", "") or "")
        if key == "first_frame" and not include_first_frame:
            continue
        if path and path not in seen_paths and getattr(asset, "selected", True):
            ordered_assets.append(asset)
            seen_paths.add(path)
            if str(getattr(asset, "media_type", "")) == "image":
                image_index += 1
            elif str(getattr(asset, "media_type", "")) == "audio":
                audio_index += 1

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
            return ""

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
            if s.audio_type == "dialogue" and s.speaker and s.speaker not in seen_voice:
                seen_voice.add(s.speaker)
                aud_slots.append(
                    S9SegmentAssetSlot(
                        slot=len(aud_slots) + 1,
                        media_type="audio",
                        asset_kind="voice",
                        name=s.speaker.split("_", 1)[0] or s.speaker,
                        asset_id=s.speaker,
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

    from novelvideo.utils.path_resolver import PathResolver

    resolver = PathResolver(output_dir)
    ready_identities: set[str] = set()
    ready_scenes: set[str] = set()
    ready_props: set[str] = set()
    ready_voices: set[str] = set()

    for char in characters or []:
        char_name = str(getattr(char, "name", "") or "").strip()
        has_voice = bool(str(getattr(char, "reference_audio_path", "") or "").strip())
        if has_voice and char_name:
            ready_voices.add(char_name)
        for ident in list(getattr(char, "identities", []) or []):
            iid = str(getattr(ident, "identity_id", "") or "").strip()
            if not iid:
                continue
            has_img = False
            for getter in (
                lambda idx=iid: resolver.identity_reference_path(idx),
                lambda idx=iid: resolver.identity_portrait_path(idx),
            ):
                try:
                    p = getter()
                    if p and Path(p).exists():
                        has_img = True
                        break
                except Exception:
                    continue
            if has_img:
                ready_identities.add(iid)
            if has_voice or bool(str(getattr(ident, "voice_sample_path", "") or "").strip()):
                ready_voices.add(iid)
                if char_name:
                    ready_voices.add(char_name)

    ep_num = int(getattr(episode, "number", 0) or 0) if episode else 0
    for item in list(getattr(episode, "scene_menu", []) or []) if episode else []:
        sid = str(getattr(item, "scene_id", "") or "").strip()
        vid = str(getattr(item, "variant_id", "") or "").strip()
        if not sid:
            continue
        for candidate_fn in (
            lambda s=sid, v=vid: resolver.scene_variant_path(s, v) if v else None,
            lambda s=sid: resolver.scene_path(s),
            lambda s=sid, e=ep_num: resolver.episode_scene_path(e, s) if e > 0 else None,
        ):
            try:
                p = candidate_fn()
                if p and Path(p).exists():
                    ready_scenes.add(sid)
                    break
            except Exception:
                continue

    for item in list(getattr(episode, "prop_menu", []) or []) if episode else []:
        pid = str(getattr(item, "prop_id", "") or getattr(item, "name", "") or "").strip()
        if not pid:
            continue
        for candidate_fn in (
            lambda p_id=pid: resolver.prop_path(p_id),
            lambda p_id=pid, e=ep_num: resolver.episode_prop_path(e, p_id) if e > 0 else None,
        ):
            try:
                p = candidate_fn()
                if p and Path(p).exists():
                    ready_props.add(pid)
                    break
            except Exception:
                continue

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
    """Build opening-shot visual description used for Sketch/Render首帧 and BeatCard."""

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
    "S9EpisodePreviewMeta",
    "S9SegmentAssetSlot",
    "S9SegmentMeta",
    "S9SegmentPlan",
    "S9StoryboardPlan",
    "S9StyleContext",
    "S9SubShot",
    "S9_PROMPT_SOURCE",
    "S9_PROMPT_STYLE",
    "VALID_S9_MAX_SEGMENT_SECONDS",
    "aggregate_segment_narration",
    "aggregate_segment_visual_summary",
    "aspect_ratio_to_video_ratio",
    "build_s9_stage1_system_prompt",
    "build_s9_stage1_task_prompt",
    "build_s9_stage2_fallback_prompt",
    "build_s9_stage2_system_prompt",
    "build_s9_stage2_task_prompt",
    "compute_segment_subshots_update",
    "compute_s9_manifest_with_live_status",
    "format_slot_table_for_stage2",
    "generate_all_s9_segment_prompts",
    "is_s9_segment_beat",
    "normalize_max_segment_seconds",
    "normalize_s9_storyboard_plan",
    "parse_s9_segment_meta",
    "parse_s9_subshots",
    "rebind_s9_prompt_with_active_assets",
    "resolve_s9_style_context",
    "run_s9_stage1_plan",
    "run_s9_stage2_segment_prompt",
]
