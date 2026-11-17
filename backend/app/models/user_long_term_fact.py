"""跨会话长期事实（L3）ORM model

与 user_profiles 的区别：
- user_profiles 是自由文本画像（summary/preferences 任意 dict）——不可验证、
  不可单条删除，实测 142 行全是空壳；
- 本表是**白名单槽位**：一个用户一个 key 一行，可单条查看/删除/过期。

隐私设计（先有删除权，再有写入）：
- deleted=1 是 tombstone。get_or_create / upsert 必须尊重它，删了不许悄悄复活。
- expire_at 由写入侧给（90 天），读时过滤；不依赖后台清理任务。
- 只存标量字符串，不存订单号/手机号/地址。
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# 白名单：只有这三类事实能进 L3。加 key 要同时改 service 的抽取规则。
L3_KEYS = ("size", "fabric_pref", "after_sales_conclusion")

KEY_LABELS = {
    "size": "常用尺码",
    "fabric_pref": "面料偏好",
    "after_sales_conclusion": "历史售后结论",
}


class UserLongTermFact(Base):
    __tablename__ = "user_long_term_facts"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True,
        comment="所属用户（逻辑外键 → users.id）",
    )
    fact_key: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="白名单 key：size / fabric_pref / after_sales_conclusion",
    )
    fact_value: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="事实值（标量，不含身份信息）",
    )
    prev_value: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, comment="改口前的值，仅供追溯",
    )
    source_session_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="首次确认该值的会话",
    )
    hits: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1,
        comment="跨会话出现次数（同一会话重复不累加）",
    )
    active: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0,
        comment="1=已被跨会话确认，允许注入 prompt；0=候选，仅留库不读",
    )
    expire_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="过期时间（写入期给出，读时过滤）",
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    deleted: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0,
        comment="tombstone：删除后置 1，永不复活",
    )

    def __repr__(self) -> str:
        return (
            f"<L3Fact user={self.user_id} key={self.fact_key} "
            f"value={self.fact_value} hits={self.hits} active={self.active}>"
        )
