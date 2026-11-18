"""Runtime loader and compiler for YD分镜skills (`yd-cinematic-storyboard`).

Reads the unpacked skill package under ``src/novelvideo/storyboard_skills/``
(or ``REELFORGE_STORYBOARD_SKILLS_DIR`` when overridden) with ``mtime_ns``
caching so replacing or updating the skill takes effect automatically.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

YD_STORYBOARD_SKILL_ID = "yd-cinematic-storyboard"

REQUIRED_SKILL_SECTIONS = (
    "本版范围",
    "创作与增强功能的统一顺序",
    "所有阶段的底线",
)

REQUIRED_REFERENCE_FILES = (
    "story.md",
    "timing.md",
    "camera.md",
    "acting.md",
    "audio.md",
    "space.md",
    "style.md",
    "full-output.md",
    "preview.md",
    "platform.md",
    "anchors.md",
    "assets.md",
    "readiness.md",
    "timing-capacity.md",
    "facial-timing.md",
    "performance-dimensions.md",
    "quality-checklist.md",
)

_DEFAULT_SKILLS_ROOT = Path(__file__).resolve().parents[1] / "storyboard_skills"
_FILE_CACHE: dict[Path, tuple[int, str]] = {}


@dataclass(frozen=True)
class YDStoryboardSkillBundle:
    """Loaded YD storyboard skill metadata and reference documents."""

    skill_id: str
    version: str
    root_dir: Path
    skill_md: str
    references: dict[str, str]


def get_storyboard_skills_root() -> Path:
    """Return the active root directory containing storyboard skill folders."""

    override = str(os.environ.get("REELFORGE_STORYBOARD_SKILLS_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return _DEFAULT_SKILLS_ROOT


def _read_cached_text(path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Storyboard skill file not found: {resolved}")
    mtime_ns = resolved.stat().st_mtime_ns
    cached = _FILE_CACHE.get(resolved)
    if cached and cached[0] == mtime_ns:
        return cached[1]
    text = resolved.read_text(encoding="utf-8")
    _FILE_CACHE[resolved] = (mtime_ns, text)
    return text


def _extract_version(skill_md: str) -> str:
    match = re.search(r'^\s*version:\s*["\']?([^"\'\r\n]+)["\']?', skill_md, re.MULTILINE)
    return match.group(1).strip() if match else "YD分镜skills v1"


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return text.strip()


def _extract_markdown_section(text: str, heading: str) -> str:
    pattern = re.compile(
        rf"(?ms)^##\s+{re.escape(heading)}\s*\r?\n(.*?)(?=^##\s+|\Z)"
    )
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def load_yd_storyboard_skill() -> YDStoryboardSkillBundle:
    """Load and validate the ``yd-cinematic-storyboard`` skill bundle."""

    skill_dir = get_storyboard_skills_root() / YD_STORYBOARD_SKILL_ID
    skill_md = _read_cached_text(skill_dir / "SKILL.md")
    body = _strip_frontmatter(skill_md)
    for sec in REQUIRED_SKILL_SECTIONS:
        if not _extract_markdown_section(body, sec):
            raise ValueError(
                f"YD storyboard skill missing required section '## {sec}' in {skill_dir / 'SKILL.md'}"
            )

    refs: dict[str, str] = {}
    ref_dir = skill_dir / "references"
    for ref_name in REQUIRED_REFERENCE_FILES:
        refs[ref_name] = _read_cached_text(ref_dir / ref_name)

    return YDStoryboardSkillBundle(
        skill_id=YD_STORYBOARD_SKILL_ID,
        version=_extract_version(skill_md),
        root_dir=skill_dir,
        skill_md=body,
        references=refs,
    )


def read_yd_reference(ref_name: str) -> str:
    """Read a specific reference markdown file from the loaded YD skill bundle."""

    bundle = load_yd_storyboard_skill()
    if ref_name not in bundle.references:
        ref_path = bundle.root_dir / "references" / ref_name
        return _read_cached_text(ref_path)
    return bundle.references[ref_name]


def build_yd_stage1_directives(
    *,
    max_segment_seconds: int,
    pace_tier: str = "紧凑",
) -> str:
    """Compile YD分镜skills Stage 1 planning, compact timing, and clip-closure rules."""

    bundle = load_yd_storyboard_skill()
    timing_doc = bundle.references["timing.md"]
    story_doc = bundle.references["story.md"]
    preview_doc = bundle.references["preview.md"]

    # Extract live rule anchors from loaded references so updates to disk take effect.
    s0_section = _extract_markdown_section(story_doc, "S0 总顺序：剧情 → 分段 → 段内分镜")
    t2_section = _extract_markdown_section(timing_doc, "T2 三档、发声合计及适用边界")
    t3_section = _extract_markdown_section(timing_doc, "T3 采用值与反注水检查")
    has_compact_anchor = "紧凑每节点默认约1秒" in t2_section or "紧凑" in t2_section
    has_anti_bloat = "采用值与反注水检查" in timing_doc and bool(t3_section)
    has_preview_focus = "画面重点" in preview_doc

    cap = 15 if int(max_segment_seconds) <= 15 else 30
    short_tail_floor = max(4, round(cap * 0.4))

    lines = [
        f"## YD分镜skills（{bundle.version}）Stage 1 核心导演法则",
        "### A. 总顺序铁律（S0）：先剧情自然长度 → 再按剧情断点分段 → 最后段内排镜",
        f"1. **单段上限 U={cap} 秒是上限，不是目标**（来自 S0/T4）：每大段 `planned_duration_seconds` 必须等于这段剧情在「{pace_tier}」档下的真实自然长度；若一段剧情只需 14 秒或 18 秒就讲完，直接输出 14 秒或 18 秒，**严禁为了凑满 {cap} 秒而拉长镜头、加停顿、加空镜、加慢放或硬塞新动作**。",
        "2. **废除固定镜数配额**：每大段内分镜数完全由这段剧情的观察需求决定（紧凑一场戏通常 2～5 镜即可讲透，绝不强凑 5～9 镜）；切点只落在视线找到对象、策略改变、关键接触、信息到达或后果可读处，不因简单动作拆碎。",
        "3. **全片一次可行切点（T4/短尾防护）**：多段时每段须在剧情断点（场次/地点变化、节拍转折、完整交流或动作链收束）切分，不劈开一句对白或一次接触；最后一段不得短于 "
        f"{short_tail_floor} 秒（若初分偏短，优先压缩前段冗余或重平衡最后两段切点）。",
        "",
        "### B. 默认「紧凑」节奏与演一遍复核（T1/T2/T3，严防节奏拖沓与无效镜）",
    ]
    if has_compact_anchor:
        lines.extend(
            [
                "1. **真实自然语速**：普通话对白/旁白按 **4～5 字/秒**（急促 5～7 字/秒），英语按 **2.5～3 词/秒**（紧凑档对白主导总比 `T/S ≈ 1.3～1.6`）。禁止按 2～3 字/秒拖慢语速，禁止在每句前后机械加 0.5～1 秒空等！",
                "2. **边做边说并行不相加**：角色敲键盘、走路、递物同时说台词或响起 OS 旁白时，动作与声音共用同一时间窗（如 32 字 OS 边敲键盘边播报 = **7～8 秒**，绝不把“敲键盘 8 秒 + 念 OS 8 秒”串行加倍成 16 秒！）。",
                "3. **紧凑动作节点默认约 1 秒（0.8～1.3 秒）**：普通推门、拿放、起身、掏钱拍桌等常规过程能从结果看清就省略或并入主镜头；纯动作/反应无声镜通常控制在 **1.5～3.5 秒**，绝不给单拍门面/雨景/空等配 4 秒以上无用镜头。",
                "4. **开场直入戏眼，严禁废片式前置空镜**：不要用“4秒空拍雨夜店门 + 3秒空拍代码屏幕”浪费前7秒；开篇第1镜直接把环境氛围（如雨夜霓虹透窗）与主体核心动作/信息同框建立。",
                "5. **说话完/事件完即切，严禁空尾拖秒**：台词或动作完成后，若该镜已无新的信息揭示或关键表演职责，立即在动作余势处切走；绝不允许留 4～10 秒毫无变化的空敲键盘或干瞪眼发呆。",
            ]
        )
    if has_anti_bloat:
        lines.append(
            "6. **演一遍复核与 T3 反注水**：输出前在心里按真实时间演一遍全部事件与台词：核查是否拆碎同一次接触、是否把边做边说串行相加、是否含无职责空镜/慢推拉/统一缓冲；发现注水立即删并压缩。"
        )

    lines.extend(
        [
            "",
            "### C. 每一大段（Clip）内部故事闭环与段尾衔接铁律",
            "1. **本段承诺的故事必须在本段内讲完**：`story_beat` 描述的每一项动作、对白、交易与转折，都必须在当前大段的 `分镜1..M` 内真实拍完并落定，不得因前段废镜头耗时过长把结尾挤没。",
            "2. **段尾必达 `exit_state`（段界接续契约）**：每一个大段的最后一个分镜必须留出清晰时间完成段尾关键交接/反应/视线落点，并在 `exit_state` 准确写出结束瞬间的站位、持物、动作相位与视线方向；下一大段的 `entry_state` 与第 1 个分镜必须从该状态无缝承接（不重演已完成动作、不凭空跳转）。",
            "",
            "### D. 空间锚定、可见表演与四列预览信息密度（C02/A01/S02/Preview）",
            "1. **多人空间蓝图**：两人以上同场交流/交接/走位/正反打时，必须在 `scene_layout` 用 3～5 句写明世界地标、人物锚位、机位工作侧与画面左右投影，保证同场景不跳轴。",
            "2. **表演写进具体可见变化**：不用“神色复杂/眼神有戏”替代动作；写清起态 → 信息触发 → 具体部位（眼/头/手/重心/停顿）如何由旧态变成新态，且写在已排镜长内，不为此加镜或拉长时长。",
        ]
    )
    if has_preview_focus:
        lines.append(
            "3. **分镜内容信息完整（Preview 级可读性）**：每个分镜的 `content` 必须写清「景别机位与运镜 + 人物具体动作交互与关键反应 + 完整逐字原话（若有） + 必要关键拟音」，严禁压成空洞的一句话概要。"
        )
    if s0_section:
        lines.append(f"4. **规范来源校验**：已加载 {bundle.skill_id}（{bundle.version}）完整 S0–S8 / T1–T5 / A01–A08 规范。")

    return "\n".join(lines)


def build_yd_stage2_directives(
    *,
    tail_anchor: bool = False,
    include_first_frame: bool = False,
    include_scene_layout: bool = False,
) -> str:
    """Compile YD分镜skills Stage 2 full Seedance 2.0/2.5 execution prompt rules."""

    bundle = load_yd_storyboard_skill()
    full_doc = bundle.references["full-output.md"]
    acting_doc = bundle.references["acting.md"]
    facial_doc = bundle.references["facial-timing.md"]
    audio_doc = bundle.references["audio.md"]

    has_acting_compile = "A06" in acting_doc or "动态时间轴" in acting_doc
    has_facial_stagger = "Onset" in facial_doc and "Offset" in facial_doc
    has_audio_design = "本次文字音色设计" in audio_doc

    tail_rule = (
        "【尾帧锚点镜规则（已开启）】\n"
        "- 仅在需要跨段参考且非全片末段时，于末镜结束后追加 `【尾帧锚点镜】`（占末段完整 1 秒，计入总时长 L）："
        "采用四分之三侧面全景固定无运镜，人物与环境保持前镜末瞬间定格、完全静音（无台词/拟音/环境声）。"
        if tail_anchor
        else "【尾帧衔接规则（默认关闭锚点定格）】\n"
        "- 禁止为衔接硬造 1 秒完全静止僵硬定格；保留自然呼吸与动作余势，在最后一镜末拍稳妥落定本段 `exit_state`（位置、持物、视线落点），以动作顺延、视线引导或声画延续切入下一大段。"
    )
    first_frame_rule = (
        "- 本大段已纳入 `首帧渲染图` 作为 `@图片1`：首镜起幅必须严格锚定 `@图片1` 的构图、光线与人物起始姿态，只描写 `t=0` 之后发生的连续动作。"
        if include_first_frame
        else "- 本大段采用纯资产全能参考模式（未锁定单张首帧渲染图）：首镜根据已绑定角色/场景/道具 `@图片N` 与 `entry_state` 主动建立空间位置与起势。"
    )
    layout_rule = (
        "3. `【场景布局】`：必须保留（本段含多人同场/走位/交互）：写明世界几何主视方向、2～4个固定地标、人物锚位、允许机位工作侧、当前画面左右投影及同场光向。禁止出现块外代号（如 `A位`/`机位B`）。\n"
        if include_scene_layout
        else "3. `【场景布局】`：若本段仅单人轻量镜头或空镜可省略；若存在两人以上同场互动/正反打/走位，则必须保留并写清地标、锚位与工作侧。\n"
    )

    lines = [
        f"你是 YD 电影级分镜提示词大师（{bundle.skill_id} · {bundle.version} Stage 2 整片连续模式执行编译）。",
        "你的唯一任务：严格把给定的【大段规划】【内部各分镜】【本段可用素材索引表】【视觉画风规则】编译为一段**立即可复制/投喂 Seedance 2.0 / 2.5 的纯净完整生成提示词**。",
        "",
        "### 输出结构铁律（严格按以下模块与顺序）",
        "1. `【参考职责】`：逐条明确本大段中每一个 `@图片N` 与 `@音频N` 的**继承维度与适用隔离边界**（角色图绑定形象与服饰、隔离设定板背景/无关站姿；场景图绑定结构光色；道具图绑定外形材质与当前状态；音频仅绑定稳定音色、不迁原样本台词与混响）。",
        f"   {first_frame_rule}",
        "2. `【全局光影画风】`：严格按已确认媒介展开三行，不混入跨媒介术语（真人实拍绝不写 UE5/CG 渲染；三维CG绝不写胶片或真实摄影机型号）：",
        "   - `视觉风格：`（展开成像/绘制方式、光的形成与衰减、材质/笔触、景深与轮廓、运动反馈五项）",
        "   - `全局主光：`（主光来源、场内固定方向、明暗软硬）",
        "   - `色彩与材质：`（本段必要色调与核心材质基调）",
        layout_rule.rstrip(),
        "4. `【注意事项】`：必须包含完整规则原文：",
        "   `画面全域禁止生成任何硬字幕、文字条或水印。音轨禁止添加任何BGM音乐，声音仅由角色人声台词配音、物理拟音SFX与环境自然底噪构成。有画内说话动作的分镜匹配该说者唇形咬字（Lip-sync）。旁白和内心独白不驱动画面人物逐字对口型，人物继续既定动作与自然表演。`",
        "   若本段后还有下一大段，另起一句写明：`本输入止于[本段结尾必达事件/exit_state]，严禁提前演绎下一大段事件。`",
        "5. `【角色声音】`：逐实际发声者写声线（静默者不写）：",
    ]
    if has_audio_design:
        lines.append(
            "   - 有 `@音频N` 时：绑定该音频核心音色与语言基调，不编造未听样本细节；"
            "无 `@音频N` 时：作四项文字音色设计（音高倾向、音色质地、共鸣或气息位置、常态吐字节奏）＋一项角色稳定发声特征；当句语气压力放到对白原句前。"
        )
    lines.extend(
        [
            "6. 连续分镜块（`分镜N（a秒到b秒）：[核心事件]，[景别]，[光学表达]，[机位与运镜]。`，连续整数秒从 `0秒` 到 `D秒`）：",
            "   每镜仅保留适用字段，不填废话：",
            "   - `- 空间与主体：` 画面左右站位/纵深、朝向、视线方向、起始动作与关键持物；有 `@图片N` 时禁止复写静态长相身高衣着，只写动态神态/干湿污渍/受伤变化。",
        ]
    )
    if has_acting_compile:
        lines.append(
            "   - `- 动态时间轴：` 按 `起态 → 足够信息触发 → 注意对象 → 具体部位动作/神态变化（写明启动点、方向、幅度、快慢） → 对手反馈/克制 → 切出余势` 组织，严格贴合该镜时间窗，"
            "**禁止出现说话完成后空敲键盘/干等凑秒**；所有表演必须在指定秒数内紧凑完成。"
        )
    if has_facial_stagger:
        lines.append(
            "     （近景细腻面部表演与混合情绪时，把主行动与泄露通道按 Onset/Apex/Offset 错峰写进句中相对位置，如“话尾前嘴角先收住，半拍后目光才移开”；远景/背影改用身体轮廓与重心。）"
        )
    lines.extend(
        [
            "   - `- 物理与光影：` 仅在本镜有光影变化或关键物理接触/惯性/环境作用时写入；无变化可省略。",
            "   - `- 音频与对白：` `[区间、声源、画内外、必要受话者]（[当句读法]）：“[逐字原句]”；[贴动作的SFX与必要环境底噪/Audio Ducking]`（禁止提及BGM）。",
            "   - `- 尾帧与转场：` 切出瞬间动作阶段、持物/接触、视线方向与转场接点；**本大段最后一镜必须精确落定 `exit_state`**，确保下一大段（Clip）第 0 秒可自然衔接。",
            "   - `- 局部风险锁：` 仅对高风险穿帮项（如双手持物手别、门窗开合）写一条正向约束；无高风险可省略。",
            tail_rule,
            "",
            "### 核心红线",
            "- **引用精准一致**：正文内只能使用下方【本段可用素材索引表】里实际存在的 `@图片N` / `@音频N` 编号，禁止编造表外编号！",
            "- **不泄元指令**：禁止在提示词正文里写出“按YD系统/Onset/Apex/FACS/AU”等教程元术语，直接把动作和神态变化写出来！",
            "- **纯净直出**：不要输出 Markdown 标题符号（`###`）或代码块围栏 ```，直接输出从 `【参考职责】` 到最后一镜的纯净提示词正文。",
        ]
    )
    if "【参考职责】" not in full_doc:
        raise ValueError("YD full-output.md missing 【参考职责】")
    return "\n".join(lines)


__all__ = [
    "YDStoryboardSkillBundle",
    "YD_STORYBOARD_SKILL_ID",
    "build_yd_stage1_directives",
    "build_yd_stage2_directives",
    "get_storyboard_skills_root",
    "load_yd_storyboard_skill",
    "read_yd_reference",
]
