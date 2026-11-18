"""验证系统数据模型。"""

from pydantic import BaseModel, Field


class VerificationIssue(BaseModel):
    type: str  # "character_count_mismatch", "action_mismatch", "scene_mismatch", etc.
    severity: str  # "critical", "warning", "info"
    description: str
    confidence: float  # 0.0-1.0


class VerificationResult(BaseModel):
    """LLM 输出结构（用作 Agent 的 output_type）。"""

    passed: bool
    score: float  # 0-10
    issues: list[VerificationIssue]
    summary: str
    suggested_action: str  # "none" | "regenerate" | "edit_script"
    edit_suggestion: str = ""


# --- Legacy ConsistencyResult (kept for backward compat) ---


class ConsistencyIssue(BaseModel):
    type: str  # "character_appearance_inconsistency", "costume_mismatch"
    severity: str  # "critical", "warning", "info"
    beats: list[int]
    character: str
    description: str
    confidence: float


# --- Enhanced Consistency Models ---


class ConsistencyDimension(BaseModel):
    """单个一致性维度的评估。"""

    dimension: str = Field(
        description="face, hair, skin_tone, gender, clothing_style, clothing_color, accessories, body_type"
    )
    score: float = Field(ge=0, le=10, description="0-10 评分")
    severity: str = Field(description="critical, warning, info")
    description: str = Field(description="问题描述")


class CharacterConsistencyReport(BaseModel):
    """单个角色的一致性报告。"""

    character: str
    identity_id: str
    beats_checked: list[int]
    dimensions: list[ConsistencyDimension]
    face_score: float = Field(
        ge=0, le=10, description="脸部综合评分 (face + hair + skin_tone 均值)"
    )
    clothing_score: float = Field(
        ge=0, le=10, description="服装综合评分 (clothing_style + clothing_color + accessories 均值)"
    )
    passed: bool = Field(description="face_score >= 7.0 AND clothing_score >= 7.0 AND 无 critical")


class ConsistencyResult(BaseModel):
    """全局一致性验证结果（增强版，分维度评分）。"""

    characters: list[CharacterConsistencyReport] = Field(default_factory=list)
    summary: str
    overall_passed: bool = Field(default=True)


# --- T6: Continuity Assessment Models ---


class ContinuityTransition(BaseModel):
    """单个过渡点评估。"""

    from_beat: int
    to_beat: int
    spatial_consistency: float = Field(ge=0, le=10, description="空间关系连续性")
    action_continuity: float = Field(ge=0, le=10, description="动作衔接")
    scene_transition: float = Field(ge=0, le=10, description="场景过渡合理性")
    total: float = Field(ge=0, le=10, description="三维均值")
    issues: list[str] = Field(default_factory=list, description="具体问题")


class ContinuityResult(BaseModel):
    """T6 连贯性评估结果（LLM 输出结构）。"""

    transitions: list[ContinuityTransition] = Field(default_factory=list)
    weak_transitions: list[int] = Field(
        default_factory=list, description="连贯性较差的过渡点 beat_number"
    )
    overall_score: float = Field(ge=0, le=10, default=10.0)


# --- T7: Similarity Detection Models ---


class SimilarityPair(BaseModel):
    """一对相似草图。"""

    beat_a: int
    beat_b: int
    similarity: float = Field(ge=0, le=1, description="相似度 0-1")
    warning: bool = Field(default=False, description="是否超过阈值")


class SimilarityResult(BaseModel):
    """T7 相似度检测结果（像素级，零 LLM）。"""

    pairs: list[SimilarityPair] = Field(default_factory=list)
    duplicate_beats: list[int] = Field(
        default_factory=list, description="与其他 beat 相似度 > 阈值的"
    )
    overall_passed: bool = Field(default=True)


# --- T8: Episode Overview Models ---


class EpisodeIssue(BaseModel):
    """全局分镜审片发现的问题。"""

    beat_number: int = Field(description="主要相关 beat")
    issue_type: str = Field(
        description="composition_repetition | style_drift | weak_climax | visual_monotony | pacing_imbalance | proportion_inconsistency"
    )
    severity: str = Field(description="critical | warning")
    description: str
    suggested_action: str = Field(description="swap_candidate | regenerate | info")
    related_beats: list[int] = Field(default_factory=list, description="相关 beat（上下文）")


class EpisodeOverviewResult(BaseModel):
    """T8 全局分镜审片结果（LLM 输出结构）。"""

    visual_rhythm: float = Field(ge=0, le=10, description="视觉节奏 0-10")
    composition_diversity: float = Field(ge=0, le=10, description="构图多样性 0-10")
    narrative_arc: float = Field(ge=0, le=10, description="叙事弧线视觉化 0-10")
    style_unity: float = Field(ge=0, le=10, description="风格统一性 0-10")
    total: float = Field(ge=0, le=10, description="四维均值")
    issues: list[EpisodeIssue] = Field(default_factory=list, description="最多 5 个")
    overall_passed: bool = Field(description="无 critical issue 且 total >= 6.0")
    summary: str = Field(description="一句话整体印象")
