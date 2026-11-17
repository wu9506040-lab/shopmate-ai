"""L3 跨会话长期事实（写入闸 / 读取闸 / 删除权）

设计顺序是**先有删除权，再有写入**：user_long_term_facts.deleted 是 tombstone，
删掉之后不会被下一轮对话悄悄重建（旧的 profile_service.clear() 就有这个毛病）。

三条硬约束：
1. 只从 kind='biz' 的会话学——评测/smoke 流量不许进长期记忆
2. 同一事实要跨**两个不同会话**出现才生效（active=1）；同一会话重复问不算确认
3. 读的时候只给 active=1 且未过期的白名单 key，值一律是标量字符串

为什么不做自由文本 summary：142 行 user_profiles 全是空壳，说明"让模型写摘要"
这条路没跑起来；而且自由文本无法单条删除、无法验证、是新的泄露面。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import text as sa_text

from app.clients.mysql_client import with_safe_session
from app.models.session_meta import KIND_BIZ
from app.models.user_long_term_fact import KEY_LABELS, L3_KEYS

logger = logging.getLogger(__name__)

# 90 天 TTL；同一事实跨会话出现 2 次才升为长期记忆
L3_TTL_DAYS = 90
CONFIRM_HITS = 2

# 用户级删除墓碑：全量删除时写一行这个 key，之后任何新 fact 都不许再写入。
_ALL_TOMBSTONE = "__all__"

# 注入 prompt 的硬上限（比原来的 profile_block 200 字更紧）
MAX_L3_PROMPT_LEN = 150


def is_allowed_key(key: Optional[str]) -> bool:
    return bool(key) and key in L3_KEYS


def _tombstoned(db, user_id: int) -> bool:
    """用户是否行使过全量删除权。"""
    row = db.execute(
        sa_text(
            "SELECT 1 FROM user_long_term_facts "
            "WHERE user_id=:u AND fact_key=:k AND deleted=1 LIMIT 1"
        ),
        {"u": user_id, "k": _ALL_TOMBSTONE},
    ).first()
    return row is not None


def get_active_facts(user_id: int) -> dict:
    """读取可注入 prompt 的事实（active + 未过期 + 未删除）。

    返回 {key: value}；白名单之外的一个都不会返回。
    """
    if not user_id or user_id <= 0:
        return {}
    try:
        with with_safe_session(commit=False) as db:
            rows = db.execute(
                sa_text(
                    "SELECT fact_key, fact_value FROM user_long_term_facts "
                    "WHERE user_id=:u AND deleted=0 AND active=1 "
                    "AND expire_at > NOW() "
                    "AND fact_key IN ('size', 'fabric_pref', 'after_sales_conclusion')"
                ),
                {"u": user_id},
            ).fetchall()
            return {r[0]: r[1] for r in rows if is_allowed_key(r[0])}
    except Exception as e:
        logger.warning(f"L3 读取失败（放行）: user_id={user_id}, {e}")
        return {}


def list_facts(user_id: int) -> list[dict]:
    """给用户看自己存了什么（查看权）。候选也列出来，标注 active。"""
    if not user_id or user_id <= 0:
        return []
    try:
        with with_safe_session(commit=False) as db:
            rows = db.execute(
                sa_text(
                    "SELECT fact_key, fact_value, prev_value, source_session_id, hits, active, "
                    "expire_at, create_time "
                    "FROM user_long_term_facts "
                    "WHERE user_id=:u AND deleted=0 AND fact_key<>:tomb "
                    "AND expire_at > NOW() ORDER BY fact_key"
                ),
                {"u": user_id, "tomb": _ALL_TOMBSTONE},
            ).fetchall()
            return [
                {
                    "key": r[0],
                    "label": KEY_LABELS.get(r[0], r[0]),
                    "value": r[1],
                    "prev_value": r[2],
                    "source_session_id": r[3],
                    "hits": r[4],
                    "active": bool(r[5]),
                    "expire_at": str(r[6]),
                    "create_time": str(r[7]),
                }
                for r in rows
            ]
    except Exception as e:
        logger.warning(f"L3 列举失败: user_id={user_id}, {e}")
        return []


def offer_fact(
    user_id: int,
    key: str,
    value: str,
    session_id: str,
    session_kind: str,
) -> bool:
    """提交一条候选事实。返回是否真的变更了库。

    写入闸：
    - kind 必须是 biz（评测/排障一律不学）
    - key 必须在白名单
    - 用户没删过档案
    - 同一会话重复提交不累加 hits（否则一次对话就能刷到 2 次）
    """
    if not is_allowed_key(key):
        return False
    if session_kind != KIND_BIZ:
        return False
    if not user_id or user_id <= 0 or not session_id:
        return False
    value = (value or "").strip()
    if not value or len(value) > 128:
        return False

    try:
        with with_safe_session(commit=True) as db:
            if _tombstoned(db, user_id):
                logger.info(f"[L3] 用户已删除档案，拒绝写入: user_id={user_id}")
                return False

            row = db.execute(
                sa_text(
                    "SELECT id, fact_value, source_session_id, hits, active "
                    "FROM user_long_term_facts "
                    "WHERE user_id=:u AND fact_key=:k AND deleted=0 LIMIT 1"
                ),
                {"u": user_id, "k": key},
            ).first()

            if row is None:
                db.execute(
                    sa_text(
                        "INSERT INTO user_long_term_facts "
                        "(user_id, fact_key, fact_value, source_session_id, hits, active, expire_at) "
                        "VALUES (:u, :k, :v, :s, 1, 0, DATE_ADD(NOW(), INTERVAL :d DAY))"
                    ),
                    {"u": user_id, "k": key, "v": value, "s": session_id, "d": L3_TTL_DAYS},
                )
                return True

            row_id, old_value, old_session, hits, _active = row

            # 顺序要紧：先判"值变了"，再判"同会话重复"。
            # 反过来写的话，同一会话内的改口会被当成普通重复只续期不更新——
            # 买家说"我改成 M"再说"不对，是 XXL"，库里永远停在 M（BUGS 50）。
            if old_value != value:
                # 改口：新值立即生效，旧值留 prev。改口是明确陈述，
                # 不该再等一次确认——否则买家已经改成 XL，系统还在用旧 L。
                db.execute(
                    sa_text(
                        "UPDATE user_long_term_facts SET fact_value=:v, prev_value=:p, "
                        "source_session_id=:s, hits=1, active=1, "
                        "expire_at=DATE_ADD(NOW(), INTERVAL :d DAY) WHERE id=:id"
                    ),
                    {"v": value, "p": old_value, "s": session_id,
                     "d": L3_TTL_DAYS, "id": row_id},
                )
                return True

            # 同会话重复（同值）：只续期，不加 hits——一次会话不能自己确认自己
            if old_session == session_id:
                db.execute(
                    sa_text(
                        "UPDATE user_long_term_facts "
                        "SET expire_at=DATE_ADD(NOW(), INTERVAL :d DAY) WHERE id=:id"
                    ),
                    {"d": L3_TTL_DAYS, "id": row_id},
                )
                return False

            # 同值、不同会话：算一次确认
            new_hits = int(hits or 0) + 1
            db.execute(
                sa_text(
                    "UPDATE user_long_term_facts SET hits=:h, active=:a, "
                    "expire_at=DATE_ADD(NOW(), INTERVAL :d DAY) WHERE id=:id"
                ),
                {"h": new_hits, "a": 1 if new_hits >= CONFIRM_HITS else 0,
                 "d": L3_TTL_DAYS, "id": row_id},
            )
            return True
    except Exception as e:
        logger.warning(f"L3 写入失败（放行）: user_id={user_id} key={key}, {e}")
        return False


def delete_fact(user_id: int, key: Optional[str] = None) -> int:
    """删除事实。key=None = 全量删除并写 tombstone（之后不许再写）。

    返回受影响行数。软删保留审计痕迹，但读取/注入一律看不到。
    """
    if not user_id or user_id <= 0:
        return 0
    try:
        with with_safe_session(commit=True) as db:
            if key is None:
                # 先写用户级墓碑，再删所有行——顺序不能反，
                # 否则并发的 offer_fact 可能在墓碑写入前插一行
                db.execute(
                    sa_text(
                        "INSERT INTO user_long_term_facts "
                        "(user_id, fact_key, fact_value, source_session_id, hits, active, expire_at, deleted) "
                        "VALUES (:u, :k, 'tombstone', '', 0, 0, DATE_ADD(NOW(), INTERVAL :d DAY), 1) "
                        "ON DUPLICATE KEY UPDATE deleted=1, fact_value='tombstone'"
                    ),
                    {"u": user_id, "k": _ALL_TOMBSTONE, "d": L3_TTL_DAYS},
                )
                result = db.execute(
                    sa_text(
                        "UPDATE user_long_term_facts SET deleted=1 "
                        "WHERE user_id=:u AND deleted=0"
                    ),
                    {"u": user_id},
                )
                return int(result.rowcount or 0)

            if not is_allowed_key(key):
                return 0
            result = db.execute(
                sa_text(
                    "UPDATE user_long_term_facts SET deleted=1 "
                    "WHERE user_id=:u AND fact_key=:k AND deleted=0"
                ),
                {"u": user_id, "k": key},
            )
            return int(result.rowcount or 0)
    except Exception as e:
        logger.warning(f"L3 删除失败: user_id={user_id} key={key}, {e}")
        return 0


def to_prompt_block(facts: dict, max_len: int = MAX_L3_PROMPT_LEN) -> str:
    """固定格式注入 LLM。只接受白名单 key，不接受自由文本。

    "本轮为准"这半句不能省：L3 是上次会话的事，改口后必须本轮优先。
    """
    lines = []
    for key in L3_KEYS:
        value = (facts or {}).get(key)
        if not value:
            continue
        lines.append(f"- {KEY_LABELS[key]}：{value}")
    if not lines:
        return ""
    block = (
        "【上次会话记住的】(买家上次提过，本轮若改口一律以本轮为准)\n"
        + "\n".join(lines)
    )
    if len(block) > max_len:
        block = block[: max_len - 1] + "…"
    return block
