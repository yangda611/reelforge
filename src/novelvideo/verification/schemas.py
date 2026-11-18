"""验证 API 请求/响应 Schema。"""

from pydantic import BaseModel, Field


class VerifyRequest(BaseModel):
    type: str = "sketch"  # "sketch" | "frame"


class ConsistencyVerifyRequest(BaseModel):
    verify_type: str = "sketch"  # "sketch" | "frame"


class ContinuityRequest(BaseModel):
    """T6: 连贯性评估请求。"""

    beat_range: list[int] = Field(default_factory=list, description="[start, end] 或空=全部")
    window_size: int = Field(default=2, ge=2, le=3, description="每次看几个连续 beat")
