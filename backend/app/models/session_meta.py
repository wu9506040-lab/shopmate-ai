"""会话元信息 ORM model - 对应 session_meta 表

一条 = 一个会话的流量分类（kind）。存在的理由是「排评测流量」这件事被写错了地方。

历史做法：看板 / KPI 各自在统计入口用 `session_id` 前缀正则猜这轮是不是测试。
同一个正则被复制到 Python 和 Java 两份代码里，于是：
- 新增一个评测脚本忘了加前缀 → 数据静默混进业务指标（曾一次灌进 1542 条）；
- 想改一处前缀，得记得改两处，且没有任何测试能发现两边不一致。

现在：会话第一次进 /api/chat 时落一行 kind，统计侧只认 kind='biz'。
- kind 由请求头 `X-Traffic-Kind` 显式声明（评测 / smoke / 排障脚本自己说清楚）；
- 没带头的仍由唯一一份前缀正则兜底，但正则只参与**写入期打标**，不参与读数；
- 只升不降：第一轮声明 eval 后，后续轮次没带头也不会被改回 biz。
"""
import datetime
from typing import Optional

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

KIND_BIZ = "biz"
KIND_EVAL = "eval"
KIND_DEBUG = "debug"
KIND_SMOKE = "smoke"
VALID_KINDS = (KIND_BIZ, KIND_EVAL, KIND_DEBUG, KIND_SMOKE)


class SessionMeta(Base):
    __tablename__ = "session_meta"

    # 会话 id 就是主键：一个会话一行，不额外造代理键（避免再出现「两个真相」）
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=KIND_BIZ, index=True)
    # 来源留档：header / prefix-regex / backfill —— 排查「这条为什么被排掉」时不用猜
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="")
    create_time: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
