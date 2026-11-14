"""AI 固定机位空间锁定规划 Agent。

通读整集 Beat 列表（场景 ID、景别、画面描述、台词/动作、已识别角色），
判断哪些连续镜头属于同一场景下的固定机位持续拍摄，输出结构化的
``lock_camera_beat`` 与 ``lock_spatial_mode`` 并经过确定性规则校验落库。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from novelvideo.brainclaw_contract import BrainClawProfile
from novelvideo.models import (
    beat_lock_camera_beat,
    beat_lock_spatial_mode,
    beat_scene_id,
    sync_beat_asset_refs,
)

logger = logging.getLogger(__name__)


class BeatCameraLockDecision(BaseModel):
    """单个 Beat 的固定机位锁定建议。"""

    beat_number: int = Field(description="Beat 编号")
    lock_camera_beat: Optional[int] = Field(
        default=None,
        description="锚点 Beat 编号（必须是同一场景内编号更小的镜头）；不锁定时为 null",
    )
    lock_spatial_mode: Literal["full_body", "upper_body", ""] = Field(
        default="",
        description="锁定范围：仅头部/表情/手势或上半身动作变化填 upper_body；全身站位与环境透视整图对齐填 full_body；不锁填空字符串",
    )
    reason: str = Field(default="", description="一句话判断依据")


class FixedCameraPlanOutput(BaseModel):
    """整集 Beat 固定机位规划输出。"""

    decisions: list[BeatCameraLockDecision] = Field(
        default_factory=list,
        description="每个 Beat 的固定机位锁定建议",
    )


FIXED_CAMERA_PLANNER_INSTRUCTIONS = """# Film Cinematography & Continuity Supervisor

You review a sequence of storyboard beats for one episode and decide which consecutive beats should use a **Fixed-Camera Spatial Lock** (`lock_camera_beat` and `lock_spatial_mode`).

## Core Principle
Lock ONLY when consecutive beats take place in the **same scene** with the **same stationary camera setup and background framing**, where keeping the background geometry and character standing coordinates locked prevents visual drift across cuts.

## When to LOCK (`lock_camera_beat` = anchor beat number)
1. **Continuous stationary dialogue / reaction**: Characters stay at roughly the same spots (standing at a counter, seated at a table, facing each other) across consecutive beats with only expression, gaze, speaking, or upper-body/hand gesture changes.
   - Set `lock_camera_beat` to the anchor beat number of that stationary camera setup (typically the opening shot of the setup, or the immediately preceding beat inside the same continuous setup; strictly `< beat_number`).
   - Prefer `lock_spatial_mode = "upper_body"` when feet/lower-body stance remains anchored and only upper-body/head/hands move.
   - Prefer `lock_spatial_mode = "full_body"` when full-frame environment perspective and full-body placement must stay locked together.

## When NOT to lock (`lock_camera_beat` = null, `lock_spatial_mode` = "")
1. Scene changes (different `scene_id`).
2. Dramatic shot-size or angle change: cut-in close-ups / inserts (hands handing cash, typing on a keyboard, phone/computer screen, clock, empty establishing shot), exterior vs interior switches, wide shot cutting to tight over-the-shoulder or reverse angle.
3. Major blocking relocation: character walks into/out of frame, crosses the room, turns around completely to a new background wall, or camera tracks/pans/moves to follow them.
4. The very first beat of a scene or a new camera setup (this shot IS the anchor for later shots, so it stays unlocked itself).

## Strict Output Rules
- Return one entry for every input Beat (`beat_number`).
- `lock_camera_beat` MUST strictly be less than `beat_number` and share the exact same `scene_id` (if `scene_id` is non-empty).
- Keep locks conservative and film-authentic: never force a lock across an insert cut-in or major framing change.
"""


def _resolve_fixed_camera_planner_model_and_settings() -> (
    tuple[Any, dict[str, Any] | None]
):
    """解析固定机位规划模型：优先环境变量覆盖，其次直接使用当前 Agent 模型，最后回退场景规划路线。"""
    from novelvideo.config import (
        _env_float,
        _get_newapi_text_model_profile,
        _newapi_text_openai_model,
        get_newapi_runtime_credentials,
        get_newapi_structured_output_model_settings,
        get_newapi_text_pydantic_model,
        uses_local_ce_runtime,
    )

    explicit_model = os.environ.get("FIXED_CAMERA_PLANNER_MODEL", "").strip()
    if explicit_model:
        return (
            get_newapi_text_pydantic_model(
                "FIXED_CAMERA_PLANNER_MODEL",
                model_name_override=explicit_model,
                brainclaw_profile=BrainClawProfile.EPISODE_SCENE_PLANNING,
                capability="text.generate.agent",
            ),
            get_newapi_structured_output_model_settings(),
        )

    if uses_local_ce_runtime():
        from novelvideo.model_gateway_settings import (
            AGENT_LLM_MODE_THIRD_PARTY,
            get_effective_agent_llm_config,
        )

        agent_llm = get_effective_agent_llm_config()
        if agent_llm.mode == AGENT_LLM_MODE_THIRD_PARTY:
            if not agent_llm.model:
                raise ValueError(
                    "当前 Agent 模型未填写模型名称，请在设置 -> 模型设置 -> Agent 模型中配置。"
                )
            api_key, base_url = get_newapi_runtime_credentials(
                api_key_override=agent_llm.api_key,
                base_url_override=agent_llm.base_url,
                env_api_key="MODEL_API_KEY",
                env_base_url="MODEL_BASE_URL",
            )
            if not api_key or not base_url:
                raise ValueError(
                    "当前第三方 Agent 模型凭证不完整，请在设置 -> 模型设置 -> Agent 模型中补全 Base URL 与 API Key。"
                )
            timeout_seconds = _env_float(
                "FIXED_CAMERA_PLANNER_MODEL_TIMEOUT_SECONDS",
                _env_float("NEWAPI_TEXT_TIMEOUT_SECONDS", 300.0),
            )
            # 第三方 Agent 模型网关常拒绝 openai_reasoning_effort="none"，此处不强制下发该参数。
            return (
                _newapi_text_openai_model(
                    agent_llm.model,
                    api_key=api_key,
                    base_url=base_url,
                    timeout_seconds=timeout_seconds,
                    profile=_get_newapi_text_model_profile(agent_llm.model),
                    default_headers=None,
                ),
                None,
            )

    return (
        get_newapi_text_pydantic_model(
            "EPISODE_SCENE_PLANNER_MODEL",
            brainclaw_profile=BrainClawProfile.EPISODE_SCENE_PLANNING,
            capability="text.generate.agent",
        ),
        get_newapi_structured_output_model_settings(),
    )


def _create_fixed_camera_planner_agent() -> Agent:
    """创建 AI 固定机位规划 Agent。"""
    model, model_settings = _resolve_fixed_camera_planner_model_and_settings()
    kwargs: dict[str, Any] = {
        "system_prompt": FIXED_CAMERA_PLANNER_INSTRUCTIONS,
        "output_type": FixedCameraPlanOutput,
        "name": "固定机位规划",
    }
    if model_settings is not None:
        kwargs["model_settings"] = model_settings
    return Agent(model, **kwargs)


def _format_beats_prompt(beats: list[dict[str, Any]]) -> str:
    """将整集 Beats 格式化为紧凑的导演上下文提示词。"""
    lines: list[str] = []
    ordered = sorted(beats, key=lambda b: int(b.get("beat_number") or 0))
    for beat in ordered:
        bn = int(beat.get("beat_number") or 0)
        if bn <= 0:
            continue
        scene_id = beat_scene_id(beat) or "(未指定场景)"
        shot_type = str(beat.get("shot_type") or "").strip()
        vd = str(beat.get("visual_description") or "").strip()
        narration = str(
            beat.get("narration_segment") or beat.get("dialogue") or ""
        ).strip()
        identities = [
            str(x)
            for x in (beat.get("detected_identities") or [])
            if str(x).strip()
        ]
        parts = [f"- Beat {bn} | scene={scene_id}"]
        if shot_type:
            parts.append(f"shot={shot_type}")
        if identities:
            parts.append(f"chars={','.join(identities)}")
        header = " | ".join(parts)
        lines.append(header)
        if vd:
            lines.append(f"  visual: {vd}")
        if narration:
            lines.append(f"  line: {narration}")
    return (
        "Analyze the following ordered episode beats and return the fixed-camera "
        "spatial lock plan for every beat.\n\n"
        + "\n".join(lines)
    )


def normalize_fixed_camera_plan(
    beats: list[dict[str, Any]],
    decisions: list[BeatCameraLockDecision],
) -> tuple[dict[int, dict[str, Any]], list[BeatCameraLockDecision]]:
    """用确定性规则校验 AI 输出并生成规范化的 scene_ref 字典。

    约束：
    - 锚点必须存在且 ``anchor_bn < beat_number``；
    - 两镜均绑定 ``scene_id`` 时必须一致（或任一有值时另一侧也需一致），严禁跨场景锁定；
    - ``lock_spatial_mode`` 仅允许 ``full_body`` / ``upper_body``，未锁定时清空；
    - 保留原 ``scene_ref`` 上的 ``scene_id``、``variant_id``、``custom_background_path`` 等字段。
    """
    ordered = sorted(
        [b for b in beats if int(b.get("beat_number") or 0) > 0],
        key=lambda b: int(b.get("beat_number") or 0),
    )
    beat_by_num = {int(b["beat_number"]): b for b in ordered}
    scene_by_num = {bn: beat_scene_id(b) for bn, b in beat_by_num.items()}
    decision_by_num = {int(d.beat_number): d for d in decisions}

    updated_scene_refs: dict[int, dict[str, Any]] = {}
    normalized_decisions: list[BeatCameraLockDecision] = []

    for bn, beat in beat_by_num.items():
        raw = decision_by_num.get(bn)
        anchor_bn = raw.lock_camera_beat if raw else None
        spatial_mode = str(raw.lock_spatial_mode or "").strip().lower() if raw else ""
        reason = str(raw.reason or "").strip() if raw else ""

        valid_anchor: Optional[int] = None
        if anchor_bn is not None:
            try:
                anchor_int = int(anchor_bn)
            except (TypeError, ValueError):
                anchor_int = 0
            if 0 < anchor_int < bn and anchor_int in beat_by_num:
                curr_scene = scene_by_num.get(bn, "")
                anchor_scene = scene_by_num.get(anchor_int, "")
                if not curr_scene and not anchor_scene:
                    valid_anchor = anchor_int
                elif curr_scene and anchor_scene and curr_scene == anchor_scene:
                    valid_anchor = anchor_int

        if valid_anchor is not None:
            mode_norm: Literal["full_body", "upper_body", ""] = (
                "upper_body" if spatial_mode == "upper_body" else "full_body"
            )
        else:
            mode_norm = ""

        existing_ref = beat.get("scene_ref")
        next_ref = dict(existing_ref) if isinstance(existing_ref, dict) else {}
        if valid_anchor is not None:
            next_ref["lock_camera_beat"] = valid_anchor
            next_ref["lock_spatial_mode"] = mode_norm
        else:
            next_ref.pop("lock_camera_beat", None)
            next_ref.pop("lock_spatial_mode", None)

        payload = {"scene_ref": next_ref}
        sync_beat_asset_refs(payload)
        updated_scene_refs[bn] = dict(payload.get("scene_ref") or {})
        normalized_decisions.append(
            BeatCameraLockDecision(
                beat_number=bn,
                lock_camera_beat=valid_anchor,
                lock_spatial_mode=mode_norm,
                reason=reason if valid_anchor is not None else "",
            )
        )

    return updated_scene_refs, normalized_decisions


async def plan_fixed_camera_locks(
    beats: list[dict[str, Any]],
) -> tuple[dict[int, dict[str, Any]], list[BeatCameraLockDecision]]:
    """调用结构化 AI 规划整集 Beats 的固定机位锁定方案。"""
    ordered = sorted(
        [b for b in beats if int(b.get("beat_number") or 0) > 0],
        key=lambda b: int(b.get("beat_number") or 0),
    )
    if len(ordered) < 2:
        return normalize_fixed_camera_plan(ordered, [])

    agent = _create_fixed_camera_planner_agent()
    prompt = _format_beats_prompt(ordered)
    run_result = await agent.run(prompt)
    output = run_result.output
    decisions = list(getattr(output, "decisions", []) or [])
    return normalize_fixed_camera_plan(ordered, decisions)


async def apply_fixed_camera_plan_for_episode(
    store: Any,
    episode_number: int,
) -> dict[str, Any]:
    """为指定集执行 AI 固定机位规划并只持久化发生变化的 Beat。"""
    beats = await store.get_beats_as_dicts(episode_number)
    if not beats:
        return {
            "total_beats": 0,
            "locked_beats": [],
            "updated_count": 0,
            "decisions": [],
        }

    updated_refs, decisions = await plan_fixed_camera_locks(beats)
    beat_by_num = {
        int(b.get("beat_number") or 0): b
        for b in beats
        if int(b.get("beat_number") or 0) > 0
    }

    updated_count = 0
    for bn, next_ref in updated_refs.items():
        beat = beat_by_num.get(bn)
        if not beat:
            continue
        prev_anchor = beat_lock_camera_beat(beat)
        prev_mode = beat_lock_spatial_mode(beat) if prev_anchor else ""
        next_anchor = next_ref.get("lock_camera_beat")
        next_mode = str(next_ref.get("lock_spatial_mode") or "")
        if prev_anchor == next_anchor and prev_mode == next_mode:
            continue
        saved = await store.update_beat_asset(
            episode_number=episode_number,
            beat_number=bn,
            scene_ref=next_ref,
        )
        if saved:
            updated_count += 1

    locked_beats = [
        d.beat_number for d in decisions if d.lock_camera_beat is not None
    ]
    return {
        "total_beats": len(beat_by_num),
        "locked_beats": locked_beats,
        "updated_count": updated_count,
        "decisions": [d.model_dump() for d in decisions],
    }
