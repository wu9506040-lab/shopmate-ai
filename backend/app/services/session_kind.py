"""会话流量分类（测试流量排除的唯一真相源）

历史包袱：看板要在统计里排掉评测/排障会话，早期做法是在每个统计入口写一条
`session_id` 前缀正则。同一个正则被复制到 Python 与 Java 两份实现里，谁都不知道
对方改了没有；新增评测脚本忘了加前缀，数据也不会报错，只会静默计入业务指标
（一次七维全量评测给看板灌进 1542 条消息，就是这么来的）。

现在的分工：
- **写入期**：/api/chat 拿到会话 id 时落一行 session_meta，kind 来自请求头
  `X-Traffic-Kind`（评测/smoke/排障脚本显式声明），没带头才用前缀正则兜底；
- **读取期**：统计只认 `kind='biz'`，不再各写各的正则。

所以正则只保留这一份，而且只影响打标、不参与读数——改脚本忘了带头，顶多是
这条会话被当业务流量（有迹可循：session_meta.source=prefix-regex）。
"""
import logging
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.mysql_client import get_session_local, with_safe_session
from app.models.session_meta import (
    KIND_BIZ, KIND_DEBUG, KIND_EVAL, KIND_SMOKE, VALID_KINDS, SessionMeta,
)

logger = logging.getLogger(__name__)

# 兜底前缀（唯一一份）。只用于「请求没声明 kind」时判来源，不参与任何读数。
TEST_SESSION_RE = re.compile(
    r"^(test|demo_|debug_|verify|unq_|force_|sequ|fresh|final_|robust|snap|v3|m8|full_"
    r"|xianyu$|dedup|eval|diag|chk|edge|smoke|probe|qa-)",
    re.IGNORECASE,
)

# 声明值到内部 kind 的归一（脚本可能写 EVAL / e2e / benchmark 之类）
_ALIAS = {
    "biz": KIND_BIZ, "business": KIND_BIZ, "prod": KIND_BIZ,
    "eval": KIND_EVAL, "evaluation": KIND_EVAL, "bench": KIND_EVAL,
    "debug": KIND_DEBUG, "diag": KIND_DEBUG, "dev": KIND_DEBUG,
    "smoke": KIND_SMOKE, "test": KIND_SMOKE, "e2e": KIND_SMOKE,
}


def infer_kind(session_id: Optional[str], declared: Optional[str] = None) -> tuple[str, str]:
    """返回 (kind, source)。source 说明这个结论从哪来，便于事后归因。"""
    raw = (declared or "").strip().lower()
    if raw in _ALIAS:
        kind = _ALIAS[raw]
        return kind, "header"
    if raw in VALID_KINDS:
        return raw, "header"
    if session_id and TEST_SESSION_RE.match(session_id):
        return KIND_EVAL, "prefix-regex"
    return KIND_BIZ, "default"


def ensure_session_kind(
    session_id: Optional[str],
    declared: Optional[str] = None,
    db: Optional[Session] = None,
) -> str:
    """幂等打标：只升不降（biz → eval 会生效，eval → biz 不会）。

    打标失败不能影响对话本身——统计脏一条远不如让用户等不到回复严重，
    所以这里吞异常只记日志。
    """
    if not session_id:
        return KIND_BIZ
    kind, source = infer_kind(session_id, declared)
    own = db is None
    session = db or get_session_local()()
    try:
        row = session.get(SessionMeta, session_id)
        if row is None:
            session.add(SessionMeta(session_id=session_id, kind=kind, source=source))
            session.commit()
            return kind
        # 只升不降：已是 eval/debug/smoke 的会话，后续轮次缺头也不会被改回 biz
        if row.kind == KIND_BIZ and kind != KIND_BIZ:
            row.kind = kind
            row.source = source
            session.commit()
            return kind
        return row.kind
    except Exception as e:  # noqa: BLE001
        logger.warning(f"session_kind 打标失败 session={session_id[:16]}: {e}")
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return kind
    finally:
        if own:
            session.close()


def get_session_kind(session_id: Optional[str]) -> str:
    """读一条会话的流量分类。

    写入闸用（L3 只能从 biz 会话学）。缺记录按 biz 处理——与 NON_BIZ_SQL
    同口径：没打标的历史会话不该被当作脏数据丢掉。
    """
    if not session_id:
        return KIND_BIZ
    try:
        with with_safe_session(commit=False) as db:
            row = db.execute(
                select(SessionMeta.kind).where(SessionMeta.session_id == session_id)
            ).scalar_one_or_none()
            return row or KIND_BIZ
    except Exception as e:
        logger.warning(f"session_kind 读取失败（按 biz 处理）: {e}")
        return KIND_BIZ


def non_biz_session_ids():
    """给 ORM 查询用：`Message.session_id.notin_(non_biz_session_ids())`。

    返回的是 select 子查询而不是 Python list —— 会话量增长后不要把几千个 id
    拉进内存再拼 IN。
    """
    return select(SessionMeta.session_id).where(SessionMeta.kind != KIND_BIZ)


# 给原生 SQL（下推聚合）用的片段。别名约定为 m（messages）
#
# 注意这里用的是「不在非业务名单里」而不是「在业务名单里」：
# 只有 /api/chat 会写 session_meta，工单/闲鱼通道/种子数据等路径不写。
# 用 IN (kind='biz') 会把「没有记录」的会话整段排掉——那是漏统计，不是排脏数据。
# 缺记录 = 业务会话（未知按业务算，测出来的脏数据有观测指标兜着）。
NON_BIZ_SQL = (
    "m.session_id NOT IN (SELECT session_id FROM session_meta WHERE kind <> 'biz')"
)
# 语义别名：读侧统一用 BIZ_SQL，避免两个片段漂移
BIZ_SQL = NON_BIZ_SQL
