"""Cinematic storyboard, camera motion, shot transition, and cross-episode continuity directives.

Centralizes the three mandatory cinematic skills:
1. Shot Scale Combinations & Anti-Jump-Cut Rules (`docs/skills/ai-shot-scale-combinations/SKILL.md`)
2. Azhe AI Camera Motion & Optical Causality (`docs/skills/ai-camera-motion/SKILL.md`)
3. AI Shot Transition & Cross-Episode Seamless Bridge (`docs/skills/ai-shot-transition/SKILL.md`)

Pure stdlib module (no internal `novelvideo.*` imports) to avoid circular imports across
workflows, agents, API routes, task runners, and prompt builders.
"""

from __future__ import annotations

import inspect
import json
import re
from typing import Any

LITERAL_BEAT_STORYBOARD_SKILL_DIRECTIVES = """
## 必用电影视听法则：15套景别组合、防跳切铁律与6大转场锚点（强制执行）
1. **五级标准景别与叙事功能（必须精确匹配剧情需要）**：
   - `远景`：环境吞没人物，开篇空间定调或孤立/压迫感；
   - `全景`：人物全身 + 完整空间关系，走位/群体对抗/入场；
   - `中景`：膝盖或腰部以上，双人交互/社交博弈与动作展开；
   - `近景`：胸部以上，面部微表情与台词情绪主导；
   - `特写`：局部器官（眼神/唇/手）或关键道具，戏剧重音与悬念揭示。
2. **15套景别组合自动编排（严禁同类景别堆砌）**：
   - 按情绪阶段推进景别层次：2镜常用（远+近 / 全+特 / 中+特）；3镜常用（远+全+近 压入、全+中+特 递进、中+近+特 交锋、近+特+近 情绪震荡、特+近+全 局部悬念后拉揭晓）；4~6镜按「远/全建立空间 → 中景交互 → 近/特写放大危机或反转 → 回拉全景重构压迫」成组调度。
3. **反同质化防跳切铁律（严禁连续相近中景死切）**：
   - 同一场次内**严禁连续 3 拍及以上停留在相同角度、相同景别**（例如连续正反打平视中景）；相邻镜头切换必须推进景别梯度（至少跨越半级~一级景别变化）或切换机位轴线，或穿插局部特写（手部动作特写、眼神特写、关键道具/屏幕特写），除非画面明确要求 `[固定机位锁定]`。
4. **静态首帧与动态运镜严格分离**：
   - `visual_description` 描述**单一静止瞬时构图 + 人物此刻姿态、视线方向与手部接触状态**，**严禁**把“推镜头/拉远/摇镜/环绕/跟拍”等连续运镜过程词直接写进首帧静态画面描述。
5. **6大无痕转场视觉锚点（相邻 Beat 必含衔接线索）**：
   - 相邻两拍切换时，在 `visual_description` 中至少埋入一种镜头转场锚点：① **动作顺接**（上一拍出拳/抬头/递物/敲键起势，下一拍换景别承接动作落点）；② **视线顺接**（上一拍角色视线凝视方向，下一拍直接切向视线落点的目标或对手反应）；③ **相似形/构图/色彩匹配**（跨场景用同类轮廓、几何构图或色温对齐过渡）；④ **前景遮挡擦除**（前景门框/人物肩背/货架掠过边缘实现换景）；⑤ **甩镜动势衔接**；⑥ **声画错位**（J-Cut 声音先于画面进入 / L-Cut 尾音带入下一拍）。
6. **跨集丝滑承接铁律（`Episode N > 1` 必看上一集结尾）**：
   - 当生成第 N 集（N > 1）开篇前 1~3 个 Beat 且存在「上一集结尾上下文 / 末拍锚点」时，开篇 Beat 必须以动作顺接、视线顺接、局部特写揭示或声音延入的方式直接衔接上一集结尾的空间、人物姿态、手持道具与悬念张力，严禁出现毫无承接逻辑的断层开篇。
"""

VIDEO_MOTION_AND_TRANSITION_SKILL_DIRECTIVES_EN = """
## Mandatory Cinematography Skills: Azhe Camera Control, Shot-Scale Rhythm & 6 Transitions
1. **Azhe 5-Variable Camera Architecture (Camera Trajectory ≠ Subject Action)**:
   - Drive every moving camera shot with clear physical trajectory across the 5 camera control variables:
     (1) Distance / Push-Pull (`dolly in`, `dolly out`),
     (2) Axis Rotation (`pan left/right`, `tilt up/down`, `Dutch angle`),
     (3) Vertical Elevation (`crane up/down`, `pedestal`),
     (4) Lateral / Orbital Arc (`truck left/right`, `arc orbit`),
     (5) Focal Length (`optical zoom in/out` vs physical displacement).
   - **Visible Parallax Proof**: Whenever the camera translates (dolly, truck, arc, crane), explicitly describe how foreground occluders, midground subject, and background architecture shift at different speeds to prevent fake 2D sliding motion.
   - **Optical Causality & Dolly Zoom (`f/d` Conservation)**: Physical push-in widens perspective and separates planes; optical zoom flattens depth without parallax shift; a Dolly Zoom / vertigo shot must simultaneously move opposite to the zoom (`camera dollies backward while zooming in, keeping subject framing constant as the background violently stretches/compresses`).
   - **26 Camera Cards & `[LOCKED FIXED CAMERA]` Discipline**: Choose camera movement strictly from narrative intent (reveal, pressure, intimacy, isolation, disorientation). When `[LOCKED FIXED CAMERA]` is active, hold a rock-steady tripod (zero pan, tilt, dolly, or zoom) and drive 100% of energy through continuous forward subject motion and ambient dynamics.
2. **15 Shot-Scale Progression (`Extreme Long / Full / Medium / Medium-Close / Extreme Close-Up`)**:
   - Honor the intentional scale step between adjacent beats: translate wide/full beats with clear spatial blocking arcs and close-ups/inserts with tactile micro-motion (finger twitch, pupil contraction, breath mist, paper tremor). Never flatten distinct shot-scale inserts into generic medium coverage.
3. **6 Seamless Shot Transitions & Cross-Episode Bridge (`Ep N-1 Finale` → `Ep N Beat 1`)**:
   - Shape the opening alignment and closing 1–1.5s momentum using the 6 transition methods:
     (1) **Action Match**: exit mid-momentum toward the next shot's opening motion or inherit the previous shot's action vector;
     (2) **Form / Composition / Color Match**: align silhouette geometry or focal light across cuts;
     (3) **Eyeline Match**: lead gaze/camera toward the object or reaction revealed next;
     (4) **Foreground Occlusion Wipe**: pass a visibly present foreground element (doorframe, pillar, shoulder, shelf) across frame edges;
     (5) **Whip Pan / Rapid Sweep**: unidirectional directional blur only when high-energy matching speed warrants it;
     (6) **Audio J-Cut / L-Cut Bridge**: carry diegetic sound cue cleanly across the cut boundary.
   - **Cross-Episode Continuity (`Previous Episode Finale`)**: When Continuity Context identifies `Previous Episode Finale`, Episode N Beat 1 MUST inherit its ending screen direction, eyeline polarity, prop state, lighting mood, or unresolved cliffhanger momentum so Episode N opens as a seamless continuation of Episode N-1.
"""

SEEDANCE2_CINEMATIC_SKILL_DIRECTIVES = """
## Mandatory Storyboard Skills (15 Shot-Scale Combos + Azhe Camera Control + 6 Transitions)
1. **Azhe 5-Variable Camera Choreography & Parallax Proof**:
   - Specify physical camera displacement (`推轨前进/后拉/横移/环绕/升降/甩镜`) separately from subject action, and state visible foreground/midground/background parallax shifts so movement feels three-dimensional.
   - Respect optical causality: physical dolly shifts perspective/parallax, focal zoom compresses background, Hitchcock Dolly Zoom pairs reverse dolly with opposite zoom (`主体占比不变、背景纵深剧烈压缩或拉伸`).
   - When `locked_with_prev` / `locked_with_next` / `locked_camera_beat` is set, enforce a locked tripod (`固定机位严禁推拉摇移`) and carry continuity purely via subject motion.
2. **Shot-Scale Rhythm & Avoid Stagnant Medium Shots**:
   - Match visual motion density to shot scale (Far/Full = spatial blocking & silhouette sweep; Medium = torso/interaction axis; Close-Up/ECU = iris, fingertip, key prop micro-dynamics) and preserve deliberate scale progression across `prev_beat` → current → `next_beat`.
3. **6 Seamless Shot Transitions & Cross-Episode Continuation**:
   - Use Action Match, Form/Color Match, Eyeline Match, Foreground Wipe (only visible foreground objects), Whip Pan, or Audio J-Cut/L-Cut across adjacent beats.
   - When `cross_episode_transition` is `true` (or `prev_beat.from_previous_episode` is present), Beat 1 MUST inspect and smoothly extend the previous episode finale's ending posture, eyeline, prop state, camera momentum, or cliffhanger tension so there is zero jarring gap between episodes.
"""


def _read_attr(obj: Any, name: str, default: Any = "") -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _strip_asset_markers(text: str) -> str:
    cleaned = re.sub(r"\{\{([^}]*)\}\}", r"\1", str(text or ""))
    cleaned = re.sub(r"\[\[([^\]]*)\]\]", r"\1", cleaned)
    return " ".join(cleaned.split())


def _extract_video_prompt_from_beat(beat: dict[str, Any]) -> str:
    raw_cfg = beat.get("seedance2_config_json")
    prompt_text = ""
    if isinstance(raw_cfg, str) and raw_cfg.strip():
        try:
            parsed = json.loads(raw_cfg)
            if isinstance(parsed, dict):
                prompt_text = str(
                    parsed.get("final_prompt") or parsed.get("prompt") or ""
                ).strip()
        except (TypeError, ValueError):
            prompt_text = ""
    if not prompt_text:
        prompt_text = str(beat.get("video_prompt") or "").strip()
    return prompt_text


def format_prev_episode_script_context(
    prev_episode: Any,
    prev_beats: list[dict[str, Any]],
    target_episode_num: int,
) -> str:
    """Format previous episode (EP N-1) ending state & final beats for cross-episode continuity."""
    if target_episode_num <= 1 or (prev_episode is None and not prev_beats):
        return ""

    prev_ep_num = target_episode_num - 1
    prev_title = str(_read_attr(prev_episode, "title", "") or "").strip()
    prev_cliffhanger = str(_read_attr(prev_episode, "cliffhanger", "") or "").strip()
    prev_content = str(_read_attr(prev_episode, "content", "") or "").strip()

    tail_lines: list[str] = []
    if prev_content:
        raw_lines = [line.strip() for line in prev_content.splitlines() if line.strip()]
        tail_lines = raw_lines[-3:]

    lines = [
        f"## 【上一集（第 {prev_ep_num} 集）结尾状态与跨集丝滑承接锚点 -> 第 {target_episode_num} 集开篇必看】",
    ]
    if prev_title:
        lines.append(f"- 上一集标题: {prev_title}")
    if prev_cliffhanger:
        lines.append(f"- 上一集结尾悬念/卡点: {prev_cliffhanger}")
    if tail_lines:
        lines.append("- 上一集最后文本片段:")
        for item in tail_lines:
            lines.append(f"  * {item[:160]}")

    tail_beats = list(prev_beats[-3:]) if prev_beats else []
    if tail_beats:
        lines.append(f"- 上一集结尾最后 {len(tail_beats)} 个分镜 Beat:")
        for beat in tail_beats:
            bn = beat.get("beat_number", "?")
            scene_ref = beat.get("scene_ref") or {}
            scene_id = ""
            if isinstance(scene_ref, dict):
                scene_id = str(scene_ref.get("scene_id") or "").strip()
            if not scene_id:
                scene_id = str(beat.get("scene_id") or beat.get("location") or "").strip()
            visual = _strip_asset_markers(str(beat.get("visual_description") or ""))
            dialogue = str(
                beat.get("dialogue") or beat.get("narration_segment") or ""
            ).strip()
            audio_type = str(beat.get("audio_type") or "").strip()
            vprompt = _extract_video_prompt_from_beat(beat)
            meta_bits = [f"Beat {bn}"]
            if scene_id:
                meta_bits.append(f"场景={scene_id}")
            if audio_type:
                meta_bits.append(f"音频={audio_type}")
            header = " | ".join(meta_bits)
            lines.append(f"  * [{header}] 画面: {visual[:180] or '无'}")
            if dialogue:
                lines.append(f"    台词/旁白: {dialogue[:120]}")
            if vprompt:
                lines.append(f"    末拍运镜尾势: {vprompt[-180:]}")

    lines.append(
        "- **跨集丝滑过渡硬要求**: 第 {target_ep} 集开篇第 1~2 行/首镜 Beat 必须主动承接第 {prev_ep} 集结尾的空间、情绪、未完动作、视线方向或关键悬念（可用动作顺接、视线顺接、局部特写承接、相似形匹配或 J-Cut/L-Cut 声音延入），严禁产生断层跳切。".format(
            target_ep=target_episode_num,
            prev_ep=prev_ep_num,
        )
    )
    return "\n".join(lines)


def extract_prev_episode_last_beat_for_video(
    prev_episode: Any,
    prev_beats: list[dict[str, Any]],
    prev_episode_num: int,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return an annotated shallow copy of EP N-1's final beat plus its video prompt tail."""
    if prev_episode_num < 1 or not prev_beats:
        return None, None

    last_beat = dict(prev_beats[-1])
    last_beat["_from_previous_episode"] = prev_episode_num
    cliffhanger = str(_read_attr(prev_episode, "cliffhanger", "") or "").strip()
    if cliffhanger:
        last_beat["_prev_episode_cliffhanger"] = cliffhanger
    last_prompt = _extract_video_prompt_from_beat(last_beat) or None
    return last_beat, last_prompt


async def _load_prev_episode_and_beats(
    store: Any,
    prev_ep_num: int,
) -> tuple[Any, list[dict[str, Any]]]:
    if store is None or prev_ep_num < 1:
        return None, []

    prev_episode = None
    get_episode = getattr(store, "get_episode", None)
    if callable(get_episode):
        try:
            maybe_ep = get_episode(prev_ep_num)
            prev_episode = (
                await maybe_ep if inspect.isawaitable(maybe_ep) else maybe_ep
            )
        except Exception:
            prev_episode = None

    if prev_episode is None:
        get_ep_graph = getattr(store, "get_episode_from_graph", None)
        if callable(get_ep_graph):
            try:
                maybe_ep = get_ep_graph(prev_ep_num)
                prev_episode = (
                    await maybe_ep if inspect.isawaitable(maybe_ep) else maybe_ep
                )
            except Exception:
                prev_episode = None

    prev_beats: list[dict[str, Any]] = []
    get_beats = getattr(store, "get_beats_as_dicts", None)
    if callable(get_beats):
        try:
            maybe_beats = get_beats(prev_ep_num)
            loaded = (
                await maybe_beats if inspect.isawaitable(maybe_beats) else maybe_beats
            )
            if isinstance(loaded, list):
                prev_beats = [dict(b) for b in loaded if isinstance(b, dict)]
        except Exception:
            prev_beats = []

    if not prev_beats:
        get_script = getattr(store, "get_script_as_dict", None)
        if callable(get_script):
            try:
                maybe_script = get_script(prev_ep_num)
                script_data = (
                    await maybe_script
                    if inspect.isawaitable(maybe_script)
                    else maybe_script
                )
                if isinstance(script_data, dict):
                    raw_beats = script_data.get("beats") or []
                    prev_beats = [dict(b) for b in raw_beats if isinstance(b, dict)]
            except Exception:
                prev_beats = []

    prev_beats.sort(key=lambda b: int(b.get("beat_number") or 0))
    return prev_episode, prev_beats


async def load_prev_episode_script_context_from_store(
    store: Any,
    episode_num: int,
) -> tuple[str, dict[str, Any] | None]:
    """Load EP N-1 script/beats context and annotated final beat from store."""
    if store is None or episode_num <= 1:
        return "", None

    prev_ep_num = episode_num - 1
    prev_episode, prev_beats = await _load_prev_episode_and_beats(store, prev_ep_num)
    if prev_episode is None and not prev_beats:
        return "", None

    context_text = format_prev_episode_script_context(
        prev_episode,
        prev_beats,
        target_episode_num=episode_num,
    )
    last_beat, _ = extract_prev_episode_last_beat_for_video(
        prev_episode,
        prev_beats,
        prev_episode_num=prev_ep_num,
    )
    return context_text, last_beat


async def load_prev_episode_last_beat_from_store(
    store: Any,
    episode_num: int,
) -> tuple[dict[str, Any] | None, str | None]:
    """Load EP N-1 final beat (annotated) and its video prompt tail from store."""
    if store is None or episode_num <= 1:
        return None, None

    prev_ep_num = episode_num - 1
    prev_episode, prev_beats = await _load_prev_episode_and_beats(store, prev_ep_num)
    if not prev_beats:
        return None, None
    return extract_prev_episode_last_beat_for_video(
        prev_episode,
        prev_beats,
        prev_episode_num=prev_ep_num,
    )


__all__ = [
    "LITERAL_BEAT_STORYBOARD_SKILL_DIRECTIVES",
    "VIDEO_MOTION_AND_TRANSITION_SKILL_DIRECTIVES_EN",
    "SEEDANCE2_CINEMATIC_SKILL_DIRECTIVES",
    "format_prev_episode_script_context",
    "extract_prev_episode_last_beat_for_video",
    "load_prev_episode_script_context_from_store",
    "load_prev_episode_last_beat_from_store",
]
