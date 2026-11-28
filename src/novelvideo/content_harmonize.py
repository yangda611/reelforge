"""Selective content-safety harmonization rules for compliant asset and prompt generation.

Only applies adjustments when a project enables ``content_harmonize`` (the default)
and the source material or asset touches moderation-sensitive categories
(real-world law-enforcement trademarks/equipment, weapons, gambling, graphic
violence/assault, profanity, or pure-black opening frames). Normal drama and
everyday scenes pass through untouched.
"""

from __future__ import annotations

import re
from typing import Iterable

_CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "law_enforcement",
        re.compile(
            r"(?i)\b(?:nypd|fbi|cia|lapd|swat|police|cop|badge)\b"
            r"|警察局|警徽|警服|警车|巡警|警靴|警员|执法|手铐|枪套|配枪|警用"
        ),
    ),
    (
        "weapons",
        re.compile(
            r"(?i)\b(?:glock|pistol|gun|firearm|rifle|shotgun|weapon)\b"
            r"|格洛克|手枪|步枪|枪械|开枪|枪击|拔枪|持枪|匕首|砍刀|尖刀|炸药|凶器"
        ),
    ),
    (
        "gambling_vice",
        re.compile(
            r"(?i)\b(?:casino|gambling|poker|betting|drugs)\b"
            r"|地下赌局|赌局|赌场|赌博|筹码|博彩|押注|吸毒|毒品"
        ),
    ),
    (
        "physical_violence",
        re.compile(
            r"重戳|狠戳|殴打|暴打|扇耳光|抽打|掐脖|踹倒|砸墙|猛力撞开|破门|撞门|踉跄|血迹|流血|鲜血|血泊|伤口"
        ),
    ),
    (
        "hostile_or_blackscreen",
        re.compile(
            r"滚出去|去死|死盯|锁死|停死|钉死|冰刀|如刃|暴喝|怒吼|咆哮|逃离|全黑画面|黑幕"
        ),
    ),
)


HARMONIZE_ASSET_DIRECTIVE = """【内容合规脱敏规则（仅针对高敏元素生效，常规日常造型/物品原样保留）】
- 仅当角色造型、道具或场景涉及真实执法机构标识、警械武器、赌博/违禁界面或血腥元素时做视觉等价替换；普通日常服装、居家环境与常规物品完全按原文生成。
- 真实执法/警务标识与警械：严禁出现 `NYPD`、`FBI` 等真实执法机构名称、文字标志或金属盾形警徽，严禁出现手铐、枪套、配枪等警械细节；统一替换为无品牌字样的深蓝色制式执勤夹克/城市公务巡逻车（保留涂装色调与警示灯，无文字盾徽）。
- 武器与违禁屏幕：严禁在设定图正面裸露真实枪械型号或血腥伤痕；电脑/手机屏幕若涉及“地下赌局/博彩”等敏感网页，统一替换为冷白色普通数据图表或办公网页界面。"""


HARMONIZE_STORYBOARD_DIRECTIVE = """【内容合规与戏剧替代规范（仅针对命中高敏元素的大段/分镜生效，常规文戏原样保留）】
仅当剧本涉及以下高敏元素时执行等效影视替代与安全包装；不涉及的日常对白与情感文戏不得擅自改动：
1. **真实执法标识与警械去敏**：不得出现 `NYPD`、`纽约市警察局`、`FBI`、真实金属警徽、警靴、手铐、枪套或枪械型号；改为无机构文字的“深蓝制式执勤外套”、“带警示灯的城市蓝白公务巡逻车”、“随行执勤人员”，保持紧张压迫感而不触及真实执法商标与武器红线。
2. **肢体冲突去暴力化**：严禁描写“狠戳身体致踉跄”、“殴打”、“掐脖”、“暴力撞门砸墙”等直接物理攻击与破坏；改为**非接触式压迫调度**（如“情绪激动跨步上前指向对方急声质问，对方神情微震后退半步”、“实木前门被从外急促推开、三人神情严肃快步步入”）。
3. **涉赌/酒精起哄去敏**：若出现“地下赌局网页特写”或“狂饮起哄”，改为镜头掠过“亮着冷白数据图表的普通电脑屏幕”、“棕色玻璃饮料瓶”与人物随意谈笑，不得特写赌博页面或醉酒闹事。
4. **强攻击与极端用语降敏**：避免“滚出去”（可改“请你出去”或克制逐客）及高频“如冰刀/如刃/如钉/锁死/停死/钉死/死盯/暴喝/逃离”等字眼，改用中性影视动词（“冷声说道/定格/停住/直视/喝止/快步离开”）。
5. **开场黑帧与片场语境**：禁止在段首安排 `0秒—1秒 全黑画面（黑幕）`，直接从实景动作切入；涉及对峙/执勤/冲突的大段，在画风描述中补充“现代都市影视剧片场实拍调度，戏剧化克制表演，非真实冲突”。"""


def detect_sensitive_categories(texts: str | Iterable[str]) -> list[str]:
    """Return ordered sensitive category keys detected in ``texts``."""
    if isinstance(texts, str):
        combined = texts
    else:
        combined = "\n".join(str(item or "") for item in texts if item)
    if not combined.strip():
        return []
    hits: list[str] = []
    for key, pattern in _CATEGORY_PATTERNS:
        if pattern.search(combined):
            hits.append(key)
    return hits


def build_image_compliance_guard(
    texts: str | Iterable[str],
    *,
    enabled: bool = True,
) -> str:
    """Return a targeted image-generation compliance block only when sensitive tokens hit."""
    if not enabled:
        return ""
    categories = detect_sensitive_categories(texts)
    if not categories:
        return ""

    rules: list[str] = [
        "CONTENT SAFETY COMPLIANCE OVERRIDE (triggered only because sensitive elements were detected):"
    ]
    if "law_enforcement" in categories or "weapons" in categories:
        rules.extend(
            [
                "- Replace any real-world police department branding, NYPD/FBI text, metallic police shields, badges, handcuffs, gun holsters, or exposed firearms with unbranded civilian utility/security equivalents (plain dark-blue duty jacket, plain blue-and-white municipal patrol vehicle with NO text, NO crest, NO shield emblem, and NO weapons or cuffs).",
            ]
        )
    if "gambling_vice" in categories:
        rules.append(
            "- Replace any gambling/betting interface with neutral abstract office charts or plain light-colored spreadsheet graphics."
        )
    if "physical_violence" in categories or "hostile_or_blackscreen" in categories:
        rules.append(
            "- Keep all subjects calm, non-violent, uninjured, and free of blood, wounds, or aggressive physical confrontation."
        )
    return "\n".join(rules)


def build_harmonized_prompt_rewrite_instructions() -> str:
    """System prompt for rewriting an existing Beat prompt to pass video moderation."""
    return f"""你是影视分镜合规润色专家。你的任务是将一段已经写好的 Seedance 视频生成提示词中的**高敏触发点**改写为可过审的影视等价表达，同时**100% 保持原故事节奏、分镜结构、时间轴与资产绑定**。

{HARMONIZE_STORYBOARD_DIRECTIVE}

【改写铁律】
1. 必须完整保留原有栏目结构（如 `【参考职责】`、`【全局光影画风】`、`【场景布局】`、`【注意事项】`、`【角色声音】`、`分镜1..N`）。
2. 必须原样保留所有 `@图片N` 和 `@音频N` 标记编号，不得增删或改变编号对应关系。
3. 必须保留每镜时间区间（若原分镜1包含 `0秒—1秒 全黑画面/黑幕`，改为从 0 秒起直接切入室内实景动作，总时长不变）以及段末 `exit_state` 收束。
4. 仅替换敏感词与高危动作，正常环境、光影、镜头运动、情绪张力不要无故缩水或改写。
5. 直接输出改写后的完整提示词正文，不要输出解释或 Markdown 代码块。"""
