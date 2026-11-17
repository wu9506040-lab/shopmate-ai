"""平台买家身份绑定 —— 把本地 user_id 映射到各平台的买家 ID。

为什么单独一张表：
    淘宝查「我卖出的订单」要买家 open_uid，抖店要 open_id。
    这些 ID 跟本地 users.id 没有任何关系，必须显式绑定。
    没绑定就不能查——拿本地 user_id 去平台接口查，查回来的单可能不是同一个人的，
    这比查不到更危险（泄露他人订单）。

表不存在时（老库未执行 11_platform_bindings.sql）静默返空：
    先探一次表，缺表就记住并降级为「全部渠道未绑定」，
    避免每轮对话都打一条 SQL 报错日志。
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

TABLE = "platform_buyer_bindings"

# 表缺失标记：探测到一次之后不再重复试探
_table_missing = False


def _table_ready(db) -> bool:
    """查 information_schema 判断表是否存在（比捕获 SQL 报错干净）。"""
    row = db.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :t LIMIT 1"
        ),
        {"t": TABLE},
    ).first()
    return row is not None


def load_platform_ids(user_id: int) -> dict[str, str]:
    """读该用户在各平台的买家 ID；无绑定 / 表缺失 / DB 故障一律返空 dict。"""
    global _table_missing
    if _table_missing or not user_id:
        return {}
    try:
        from app.clients.mysql_client import with_safe_session

        with with_safe_session(commit=False) as db:
            if not _table_ready(db):
                _table_missing = True
                logger.warning(
                    f"{TABLE} 表不存在，外部平台渠道按未绑定处理。"
                    "执行 deploy/mysql/init/11_platform_bindings.sql 后重启即可启用。"
                )
                return {}
            rows = db.execute(
                text(f"SELECT platform, platform_buyer_id FROM {TABLE} WHERE user_id = :uid"),
                {"uid": user_id},
            ).fetchall()
        return {str(r[0]): str(r[1]) for r in rows if r[0] and r[1]}
    except Exception as e:  # noqa: BLE001 —— DB 故障不该阻塞对话
        logger.warning(f"load_platform_ids failed: user_id={user_id}, {e}")
        return {}


def reset_binding_cache() -> None:
    """测试钩子：清除「表缺失」缓存。"""
    global _table_missing
    _table_missing = False


def bind_platform_buyer(
    user_id: int, platform: str, platform_buyer_id: str, shop_id: Optional[str] = None,
) -> bool:
    """写入/更新绑定（供后台或 OAuth 回调调用）。成功返 True。"""
    if not (user_id and platform and platform_buyer_id):
        return False
    try:
        from app.clients.mysql_client import with_safe_session

        with with_safe_session(commit=True) as db:
            db.execute(
                text(
                    f"INSERT INTO {TABLE} (user_id, platform, platform_buyer_id, shop_id) "
                    "VALUES (:uid, :pf, :pid, :sid) "
                    "ON DUPLICATE KEY UPDATE platform_buyer_id = VALUES(platform_buyer_id), "
                    "shop_id = VALUES(shop_id)"
                ),
                {"uid": user_id, "pf": platform, "pid": platform_buyer_id, "sid": shop_id},
            )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"bind_platform_buyer failed: {e}")
        return False
