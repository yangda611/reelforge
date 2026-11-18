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


class S9SegmentPlan(BaseModel):
    """LLM Stage 1 output for one 大段."""

    segment_number: int = Field(default=1, ge=1)
    title: str = Field(default="", description="大段叙事小标题")
    planned_duration_seconds: int = Field(default=15, ge=1, description="本大段计划整数秒数（按剧情自然长度，不强凑上限）")
    story_beat: str = Field(default="", description="本大段内必须100%讲完的叙事闭环（起势→推进→收束落点）")
    entry_state: str = Field(default="", description="本大段第0秒起始状态：人物站位、姿态、持物、视线与未完动作")
    exit_state: str = Field(default="", description="本大段结束瞬间必达落点状态，供下一大段第0秒无缝承接")
    scene_layout: str = Field(default="", description="本大段【场景布局】：世界地标、人物锚位、机位工作侧与左右投影（多人同场/走位必填）")
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
    """Persisted per-Beat (per-大段) segment storyboard metadata."""

    segment_number: int = 1
    segment_title: str = ""
    planned_duration_seconds: int = 15
    max_segment_seconds: int = DEFAULT_S9_MAX_SEGMENT_SECONDS
    story_beat: str = ""
    entry_state: str = ""
    exit_state: str = ""
    scene_layout: str = ""
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
- **资产与结构字段要求**：
  - `duration_assessment`：填 `pace_tier="紧凑"`、`speech_total_seconds`（全部发声合计 S 秒）与含演一遍复核结论的 `core_chain_seconds`。
  - `asset_manifest`：列出全集通用 `@图片1..N`（角色→场景→道具顺序）和 `@音频1..M`（有实际发声角色），`identity_ids`、`scene_id`、`prop_ids` 尽量精确匹配给定菜单 ID。
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
    elif prev_episode_context.strip():
        continuity_parts.append(f"- 上一集末尾语境：{prev_episode_context.strip()}")
    if getattr(segment, "entry_state", "").strip():
        continuity_parts.append(f"- 本段第0秒起态（entry_state）：{segment.entry_state.strip()}")
    if getattr(segment, "exit_state", "").strip():
        continuity_parts.append(
            f"- 本段末镜必达落点（exit_state，必须完整拍完并止于此）：{segment.exit_state.strip()}"
        )
    if next_segment is not None:
        next_entry = str(getattr(next_segment, "entry_state", "") or "").strip()
        continuity_parts.append(
            f"- 下一大段（大段{next_segment.segment_number}·{next_segment.title}）即将承接：{next_segment.story_beat}"
            + (f"｜下段起态：{next_entry}" if next_entry else "")
        )
    continuity_block = "\n".join(continuity_parts) if continuity_parts else "- 本段独立完成当前闭环"

    layout_block = (
        f"\n【已规划场景空间蓝图】\n{segment.scene_layout.strip()}\n"
        if str(getattr(segment, "scene_layout", "") or "").strip()
        else ""
    )

    guidance_block = (
        f"\n【用户附加微调要求（必须融入本段执行方案）】\n{user_guidance.strip()}\n"
        if user_guidance.strip()
        else ""
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

【本大段内部各分镜规划（时间轴共 {segment.planned_duration_seconds} 秒）】
{chr(10).join(shots_lines)}
{guidance_block}
请立即直接输出纯净的 Stage 2 完整生成提示词文本（严格执行 YD分镜skills 的参考职责、全局光影画风、适用场景布局、注意事项、角色声音与动态时间轴表演写法）。"""


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
                    f"@图片{s.slot} 是场景{s.name}参考：锁定空间结构、陈设材质与环境光色；仅作空间与光线基准，不冻结镜头运镜。"
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
        sfx_part = f"；{shot.sfx_cue}" if getattr(shot, "sfx_cue", "").strip() else "；伴随贴动作的真实物理拟音与自然环境底噪"
        audio_desc = f"纯环境写实拟音与呼吸声{sfx_part if getattr(shot, 'sfx_cue', '').strip() else ''}。"
        if shot.spoken_text:
            speaker_tag = shot.speaker or "角色"
            voice_ref = voice_slot_by_id.get(speaker_tag) or voice_slot_by_name.get(
                speaker_tag.split("_", 1)[0].lower(), ""
            )
            if shot.audio_type == "narration":
                audio_desc = (
                    f"{start_s}秒到{end_s}秒，{speaker_tag}画外音/内心独白（贴合当下呼吸与节奏）：“{shot.spoken_text}”"
                    f"（画面人物嘴唇闭合，继续既定动作）{sfx_part}。"
                )
            else:
                ref_part = f"（{voice_ref}音色，自然吐字）" if voice_ref else "（自然吐字）"
                audio_desc = (
                    f"{start_s}秒到{end_s}秒，{speaker_tag}画内开口{ref_part}：“{shot.spoken_text}”"
                    f"，说话者唇形咬字精准同步{sfx_part}。"
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
        shot_blocks.append(
            f"分镜{shot.shot_number}（{start_s}秒到{end_s}秒）：{shot.title or f'镜{shot.shot_number}'}，{shot.shot_scale_movement}，{shot.camera_angle}，{shot.camera_motion}。\n"
            f"- 空间与主体：{shot.content}\n"
            f"- 动态时间轴：承接前态后在触发点直接切入核心动作与可见部位变化，动作与台词并行紧凑推进，不留无效空等。\n"
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


def _strip_code_fences(text: str) -> str:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    return cleaned.strip()


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

    for seg in segments:
        seg_num = seg.segment_number
        if seg.planned_duration_seconds > cap:
            issues.append(f"大段{seg_num} 计划时长 {seg.planned_duration_seconds}s 超出上限 {cap}s")
        shots = list(seg.shots or [])
        if not shots:
            issues.append(f"大段{seg_num} 缺少分镜列表")
            continue

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

        if len(segments) >= 2 and seg_num < len(segments):
            if not str(getattr(seg, "exit_state", "") or "").strip():
                issues.append(
                    f"大段{seg_num} 缺少 `exit_state` 段尾落点状态，可能导致下一大段无法衔接"
                )

    return issues


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
    """Invoke LLM to compute Stage 1 S9StoryboardPlan with YD compact pacing audit."""

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
        name="YD Stage1 Storyboard Planner",
    )
    result = await agent.run(task_prompt)
    plan = result.output

    issues = audit_s9_plan_pacing(plan, max_segment_seconds=cap)
    if issues and plan.segments:
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
            "3. 确保每个大段完整讲完自己的 story_beat 并在末镜落定清晰的 exit_state（下一大段 entry_state 直接承接）；\n"
            f"4. 单段时长 <= {cap} 秒，不为了凑满 {cap} 秒拉长镜头。"
        )
        try:
            revised = await agent.run(revision_prompt)
            if revised.output and revised.output.segments:
                revised_issues = audit_s9_plan_pacing(revised.output, max_segment_seconds=cap)
                if len(revised_issues) <= len(issues):
                    plan = revised.output
        except Exception as exc:
            logger.warning("YD Stage 1 compact revision failed, keeping initial plan: %s", exc)

    return plan


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
        name="YD Stage2 Segment Prompt Director",
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
    "audit_s9_plan_pacing",
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
