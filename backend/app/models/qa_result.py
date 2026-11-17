"""质检结果 ORM model - 对应 qa_results 表

一条 = 一轮「买家问 + AI 答」的质检结论。两种来源：
- rule：全量规则扫描（零成本、确定性缺陷），verdict=blocked
- llm ：抽样四维打分，verdict=pass/low

设计要点（都是踩过的坑）：
- `(session_id, message_id)` 唯一：同一个答案被跑批两次只算一次，否则合格率会被重复计算；
- 存的是**脱敏后**的 query/answer：质检会把正文喂给上游模型，原文里有手机号/订单号，
  不能直接外发（trace 有脱敏，messages 表没有）；
- `calibrated=False` 的行不进人工队列：LLM 打分批次若没通过「坏答案校准」，这一批的分数
  不可信，只能留档不能当结论（质检自己也要被质检）。
"""
import datetime
from typing import Optional

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, Float, Integer, SmallInteger, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

SOURCE_RULE = "rule"
SOURCE_LLM = "llm"

VERDICT_PASS = "pass"
VERDICT_LOW = "low"
VERDICT_BLOCKED = "blocked"

REVIEW_PENDING = "pending"
REVIEW_ACCEPTED = "accepted"
REVIEW_REJECTED = "rejected"


class QaResult(Base):
    __tablename__ = "qa_results"
    __table_args__ = (
        UniqueConstraint("session_id", "message_id", name="uq_qa_session_message"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(40), nullable=False, default="", index=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, index=True)
    # 脱敏后的问句/答案（原文不落这张表）
    query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    intent: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    # 失败层：retrieval / generation / intent_router / safety_policy / length_fuse
    failure_layer: Mapped[Optional[str]] = mapped_column(String(24), nullable=True, index=True)
    # 知识侧根因（知识运营闭环）：failure_layer 的 retrieval 一档混了三种病，
    # 不拆开运营没法知道该补卡、该改卡还是该调检索。
    # no_kb / retrieval_miss / kb_stale / generation / ranking_weak / unknown
    root_cause: Mapped[Optional[str]] = mapped_column(String(24), nullable=True, index=True)
    # 判定依据快照（命中位次、库里是否存在、dims 取值），人工复核"为什么判这一类"
    root_cause_evidence: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 规则命中列表：["empty_answer","self_check_dropped", ...]
    rule_hits: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # 四维分：{"relevance":4,"grounding":3,"consistency":5,"actionable":2}
    dims: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    verdict: Mapped[str] = mapped_column(String(12), nullable=False, default="pass", index=True)
    source: Mapped[str] = mapped_column(String(8), nullable=False, default=SOURCE_RULE)
    # 批次校准结果：坏答案拿到高分 → False，这一批不能进人工队列
    calibrated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    review_status: Mapped[str] = mapped_column(
        String(12), nullable=False, default=REVIEW_PENDING)
    note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    create_time: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now())
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    def __repr__(self) -> str:
        return (
            f"<QaResult id={self.id} verdict={self.verdict} "
            f"score={self.score} hits={self.rule_hits}>"
        )
