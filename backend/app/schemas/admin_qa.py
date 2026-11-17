"""质检（QA）响应模型。"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class QaRunRequest(BaseModel):
    since_hours: int = Field(24, ge=1, le=720, description="回溯窗口（小时）")
    limit: Optional[int] = Field(None, ge=1, le=2000, description="最多扫描多少条 assistant 消息")
    ratio: Optional[float] = Field(None, ge=0, le=1, description="LLM 抽样比例，0 = 只跑规则层")
    use_llm: bool = Field(True, description="是否跑 LLM 打分（关掉只做规则扫描）")


class QaRunResponse(BaseModel):
    batch_id: str
    scanned: int
    blocked: int
    sampled: int
    judged: int
    low: int
    calibrated: Optional[bool] = None


class QaItem(BaseModel):
    id: int
    session_id: str
    message_id: int
    query: str
    answer: str
    intent: Optional[str] = None
    verdict: str
    source: str
    score: Optional[float] = None
    dims: Optional[Dict[str, Any]] = None
    rule_hits: Optional[List[str]] = None
    failure_layer: Optional[str] = None
    calibrated: bool = True
    review_status: str = "pending"
    note: Optional[str] = None
    create_time: Optional[str] = None


class QaListResponse(BaseModel):
    total: int
    items: List[QaItem]


class QaStatsResponse(BaseModel):
    window_days: int
    total: int
    pass_: int = Field(0, alias="pass")
    low: int = 0
    blocked: int = 0
    pass_rate: Optional[float] = None
    low_rate: Optional[float] = None
    uncalibrated: int = 0
    pending_review: int = 0
    top_defects: List[Dict[str, Any]] = Field(default_factory=list)
    by_day: List[Dict[str, Any]] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class QaToBadcaseResponse(BaseModel):
    qa_id: int
    badcase: Dict[str, Any]
