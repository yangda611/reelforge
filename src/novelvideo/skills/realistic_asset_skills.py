"""Runtime loader and compiler for realistic character and scene asset skills.

Reads the unpacked skill packages under ``src/novelvideo/asset_skills/`` (or
``REELFORGE_ASSET_SKILLS_DIR`` when overridden) with ``mtime_ns`` caching so
that replacing or upgrading a skill's ``SKILL.md`` takes effect automatically
without code edits.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

CHARACTER_SKILL_ID = "generate-live-action-character-sheet"
SCENE_SKILL_ID = "generate-realist-scenes"

REQUIRED_CHARACTER_SECTIONS = (
    "首版目标与执行顺序",
    "默认版式：竖版9:16四宫格",
    "建立可复用的身份与衣装锁定段",
    "真人实拍质感",
    "提示词组织",
    "交付前检查",
)

REQUIRED_SCENE_SECTIONS = (
    "风格锚点与优先级",
    "六个生成模型",
    "十条条件规则",
    "反模式与修正",
    "核心张力与选择",
)

_DEFAULT_SKILLS_ROOT = Path(__file__).resolve().parents[1] / "asset_skills"
_SKILL_CACHE: dict[str, tuple[Path, int, str, dict[str, str]]] = {}


def get_asset_skills_root() -> Path:
    """Return the active root directory containing asset skill folders."""
    override = str(os.environ.get("REELFORGE_ASSET_SKILLS_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return _DEFAULT_SKILLS_ROOT


def resolve_skill_markdown_path(skill_id: str) -> Path:
    """Locate ``SKILL.md`` for ``skill_id``, supporting flat or nested zips."""
    root = get_asset_skills_root()
    direct = root / skill_id / "SKILL.md"
    if direct.is_file():
        return direct
    nested = root / skill_id / skill_id / "SKILL.md"
    if nested.is_file():
        return nested
    raise FileNotFoundError(
        f"Asset skill {skill_id!r} SKILL.md not found under {root}"
    )


def _strip_frontmatter(markdown_text: str) -> str:
    match = re.match(r"^---\s*\n.*?\n---\s*\n", markdown_text, flags=re.DOTALL)
    if match:
        return markdown_text[match.end() :]
    return markdown_text


def _parse_markdown_sections(markdown_text: str) -> tuple[str, dict[str, str]]:
    """Parse ``##`` and ``###`` sections from a SKILL.md file."""
    body = _strip_frontmatter(markdown_text).strip()
    sections: dict[str, str] = {}
    current_h2 = ""
    current_h3 = ""
    h2_lines: list[str] = []
    h3_lines: list[str] = []

    def _flush_h3() -> None:
        nonlocal current_h3, h3_lines
        if current_h3:
            text = "\n".join(h3_lines).strip()
            if text:
                sections[current_h3] = text
        current_h3 = ""
        h3_lines = []

    def _flush_h2() -> None:
        nonlocal current_h2, h2_lines
        _flush_h3()
        if current_h2:
            text = "\n".join(h2_lines).strip()
            if text:
                sections[current_h2] = text
        current_h2 = ""
        h2_lines = []

    for line in body.splitlines():
        h2_match = re.match(r"^##\s+(.+?)\s*$", line)
        if h2_match:
            _flush_h2()
            current_h2 = h2_match.group(1).strip()
            continue
        h3_match = re.match(r"^###\s+(.+?)\s*$", line)
        if h3_match:
            _flush_h3()
            current_h3 = h3_match.group(1).strip()
            if current_h2:
                h2_lines.append(line)
            continue
        if current_h2:
            h2_lines.append(line)
        if current_h3:
            h3_lines.append(line)

    _flush_h2()
    return body, sections


def _load_skill_document(skill_id: str) -> tuple[str, dict[str, str]]:
    path = resolve_skill_markdown_path(skill_id)
    stat_result = path.stat()
    mtime_ns = stat_result.st_mtime_ns
    cached = _SKILL_CACHE.get(skill_id)
    if cached is not None and cached[0] == path and cached[1] == mtime_ns:
        return cached[2], cached[3]
    raw_text = path.read_text(encoding="utf-8")
    body, sections = _parse_markdown_sections(raw_text)
    _SKILL_CACHE[skill_id] = (path, mtime_ns, body, sections)
    return body, sections


def _strip_h3_subsection(section_text: str, heading: str) -> str:
    """Remove an H3 subsection (up to the next H3 or end of text) from a section."""
    lines = section_text.splitlines()
    kept: list[str] = []
    skipping = False
    target_prefix = f"### {heading.strip()}"
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("### "):
            skipping = stripped == target_prefix
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip()


def _collect_sections(
    skill_id: str,
    headings: tuple[str, ...],
    *,
    exclude_h3: tuple[str, ...] = (),
) -> str:
    body, sections = _load_skill_document(skill_id)
    blocks: list[str] = []
    for heading in headings:
        content = sections.get(heading)
        if content:
            for excluded in exclude_h3:
                content = _strip_h3_subsection(content, excluded)
            if content:
                blocks.append(f"[{heading}]\n{content}")
    if blocks:
        return "\n\n".join(blocks).strip()
    return body.strip()


def clear_realistic_asset_skills_cache() -> None:
    """Clear cached skill markdown parses (primarily for tests)."""
    _SKILL_CACHE.clear()


def is_live_action_project(
    *,
    style_id: str | None = None,
    project_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
) -> bool:
    """Return True only when an explicit or configured project style is live_action."""
    resolved_style = str(style_id or "").strip()
    if not resolved_style and state_dir:
        try:
            from novelvideo.project_config import load_project_config_file_from_state_dir

            cfg = load_project_config_file_from_state_dir(state_dir)
            resolved_style = str(
                cfg.get("visual_style") or cfg.get("project_style") or ""
            ).strip()
        except Exception:
            resolved_style = ""
    if not resolved_style:
        return False
    try:
        from novelvideo.services.style_service import StyleService

        family, _ = StyleService.get_style_branch(
            resolved_style,
            project_dir=project_dir,
            state_dir=state_dir,
        )
        return family == "live_action"
    except Exception:
        return False


def validate_realistic_asset_skills() -> dict[str, list[str]]:
    """Verify both skill packages exist and expose their expected sections."""
    checks = {
        CHARACTER_SKILL_ID: REQUIRED_CHARACTER_SECTIONS,
        SCENE_SKILL_ID: REQUIRED_SCENE_SECTIONS,
    }
    report: dict[str, list[str]] = {}
    for skill_id, required in checks.items():
        _body, sections = _load_skill_document(skill_id)
        missing = [name for name in required if name not in sections]
        if missing:
            raise ValueError(
                f"Skill {skill_id!r} is missing required sections: {', '.join(missing)}"
            )
        report[skill_id] = sorted(sections.keys())
    return report


def get_live_action_portrait_skill_directives() -> str:
    """Return live-action portrait rules derived from the character sheet skill."""
    extracted = _collect_sections(
        CHARACTER_SKILL_ID,
        (
            "首版目标与执行顺序",
            "真人实拍质感",
        ),
    )
    return (
        "LIVE-ACTION CHARACTER PORTRAIT SKILL DIRECTIVES "
        f"(source: {CHARACTER_SKILL_ID}/SKILL.md):\n"
        "- Apply these live-action photography, skin, lighting, and anti-CG rules "
        "to this SINGLE front-facing head-and-shoulders identity portrait.\n"
        "- Keep single-portrait framing (do NOT turn this portrait step into a "
        "multi-panel sheet).\n\n"
        f"{extracted}"
    ).strip()


def get_live_action_character_sheet_skill_directives() -> str:
    """Return live-action vertical 9:16 2x2 four-panel character sheet rules from SKILL.md."""
    extracted = _collect_sections(
        CHARACTER_SKILL_ID,
        (
            "首版目标与执行顺序",
            "默认版式：竖版9:16四宫格",
            "建立可复用的身份与衣装锁定段",
            "真人实拍质感",
            "提示词组织",
        ),
        exclude_h3=("BODY FRONT 的脸：优先空白脸，做不到改无头",),
    )
    return (
        "LIVE-ACTION CHARACTER SHEET SKILL DIRECTIVES "
        f"(source: {CHARACTER_SKILL_ID}/SKILL.md):\n"
        "- Apply these rules for vertical 9:16 2x2 four-panel live-action "
        "costume-fitting character sheets (FACE FRONT, FACE SIDE, BODY FRONT, BACK).\n"
        "- Preserve any attached identity anchor (Reference 1) and costume "
        "reference (Reference 2) as authoritative visual anchors.\n"
        "- Panel 3 (bottom-left BODY FRONT) MUST keep its face intentionally blurred "
        "and featureless (smooth matte skin-toned blank oval, no eyes/brows/nose/mouth), "
        "unless covered by an explicit full-face mask or hood.\n\n"
        f"{extracted}"
    ).strip()


def get_live_action_character_extraction_directives() -> str:
    """Return concise live-action trait/wardrobe guidance for LLM extractors."""
    extracted = _collect_sections(
        CHARACTER_SKILL_ID,
        (
            "建立可复用的身份与衣装锁定段",
            "真人实拍质感",
        ),
    )
    return (
        "## 写实真人角色描述强化规则（源自 真人电影角色三视图 Skill）\n"
        "- 保留具体成年年龄感、体型、脸型轮廓与2-3个可识别五官特征，避免笼统“帅哥/美女”标签；\n"
        "- 服装造型写清内外层次、面料材质、领口交叠与非对称配件在人物自身左/右侧的固定位置；\n"
        "- 不把影片青橙滤镜、随机泥污伤疤或夸张磨皮写进基础定妆设定。\n\n"
        f"{extracted}"
    ).strip()


def get_realist_scene_master_skill_directives() -> str:
    """Return realistic scene master prompt rules from generate-realist-scenes."""
    extracted = _collect_sections(
        SCENE_SKILL_ID,
        (
            "风格锚点与优先级",
            "六个生成模型",
            "十条条件规则",
            "反模式与修正",
            "核心张力与选择",
        ),
    )
    return (
        "REALIST SCENE GENERATION SKILL DIRECTIVES "
        f"(source: {SCENE_SKILL_ID}/SKILL.md):\n"
        "- Apply these observational-realism rules within the canonical front-facing "
        "180-degree establishing composition, front/back/left/right spatial contract, "
        "and time_of_day / variant_id overrides defined above.\n"
        "- Do NOT override the canonical front-facing empty-set camera position or add "
        "people to an empty scene asset.\n\n"
        f"{extracted}"
    ).strip()


def get_realist_scene_enrichment_directives() -> str:
    """Return realistic scene design rules for upstream 360 environment compilation."""
    extracted = _collect_sections(
        SCENE_SKILL_ID,
        (
            "风格锚点与优先级",
            "六个生成模型",
            "十条条件规则",
            "反模式与修正",
        ),
    )
    return (
        "## 写实电影场景环境编译准则（源自 写实电影场景生成 Skill）\n"
        "- 在严格保持 360 空间合同（正面/左侧/右侧/背面/光源/材质/风格/禁止元素）和空镜无人物的前提下：\n"
        "- 每个空间方向优先安排 3-6 件有明确使用因果的固定物件，避免堆砌无功能装饰；\n"
        "- 在`光源`段写清主光源来源、遮挡与反射路径（除非剧本明确，不默认套阴雨/青绿滤镜）；\n"
        "- 在`材质/风格`段写清 2-3 种核心材质与局部接触磨损，新建或洁净空间保留本色，不强行做旧、不铺随机微纹理颗粒。\n\n"
        f"{extracted}"
    ).strip()
