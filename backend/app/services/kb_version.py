"""知识库**版本选择**（2026-11）——同一主题按标签 + 时间选当前版本

## 解决的问题

同一份政策/商品资料会改版：尺码表修订、退换条款调整、商品参数换代。
改版后旧文档往往仍标着「长期有效」（没有 effective_to），仅靠有效期过滤
**不会**把它排除——于是检索结果里新旧两版同时出现，AI 可能把两版数字混着答，
比如「退货运费买家承担」和「退货运费商家承担」各引用一次。

## 规则

1. 用 `doc_key` 把「同一主题的所有版本」归组（标签里的人工约定，不是文件名猜）
2. 组内先按 `kb_meta.validity_of` 判定当前是否生效（复用同一套时间语义，不另写一套）
3. 生效版本里取 **effective_from 最大**（最新生效）；同一天并列时取
   `revision` 字典序最大，再并列取 source 最大 —— 保证确定性，不依赖 DB 返回顺序
4. 检索结果中，`doc_key` 存在但其 `doc_source` 不是当前版本的 chunk 全部丢弃

## 为什么放在运行期而不是入库时固化 is_current

"当前版本"随日期变化（12 月的新价目今天还没生效）。入库时写死 is_current，
到生效日还得重灌一次，很容易忘。运行期按当前时间算，到点自动切换。

版本清单直接复用 `knowledge_documents`（一行一个文档版本），不另建表：
入库脚本已经要写这张表，少一个真相源。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from app.services.kb_meta import validity_of

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60
_cache: dict = {"ts": 0.0, "map": {}}


def select_current_versions(rows: list[dict], now=None) -> dict[str, str]:
    """从版本清单算出 {doc_key: 当前版本的 source}（纯函数，便于单测）。

    Args:
        rows: knowledge_documents 行，至少含 doc_key / source / validity /
              effective_from / effective_to；其余字段（status/deleted）可选。
        now: 判定基准（默认东八区当前时间）
    """
    grouped: dict[str, list[dict]] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        if r.get("status", 1) in (0, "0") or r.get("deleted", 0) not in (0, "0", None):
            continue
        dk = str(r.get("doc_key") or "").strip()
        src = str(r.get("source") or "").strip()
        if not dk or not src:
            continue
        payload = {
            "validity": str(r.get("validity") or "permanent"),
            "effective_from": r.get("effective_from") or 0,
            "effective_to": r.get("effective_to") or 0,
        }
        if not validity_of(payload, now).get("active"):
            continue
        grouped.setdefault(dk, []).append(r)

    result: dict[str, str] = {}
    for dk, candidates in grouped.items():
        best = max(
            candidates,
            key=lambda r: (
                int(r.get("effective_from") or 0),
                str(r.get("revision") or ""),
                str(r.get("source") or ""),
            ),
        )
        result[dk] = str(best["source"])
    return result


def filter_superseded(hits: list[dict], current: dict[str, str]) -> list[dict]:
    """丢掉「同主题但不是当前版本」的 chunk。

    兼容策略：
    - chunk 没有 doc_key（历史文档）→ 保留，不误杀
    - current 里没有这个 doc_key（版本表缺数据 / 查询失败）→ 保留，fail-open；
      宁可短暂出现新旧并列，也不能因为元数据问题把知识库整个搜空
    """
    if not current:
        return hits
    kept: list[dict] = []
    for h in hits:
        payload = h.get("payload") or {}
        dk = str(payload.get("doc_key") or "").strip()
        if not dk or dk not in current:
            kept.append(h)
            continue
        if str(payload.get("doc_source") or "") == current[dk]:
            kept.append(h)
        else:
            logger.info(
                f"kb 旧版本已过滤: doc_key={dk} doc_source={payload.get('doc_source')} "
                f"current={current[dk]}"
            )
    return kept


def load_current_versions(force: bool = False) -> dict[str, str]:
    """读 MySQL 版本清单（60 秒 TTL 缓存）。

    失败时返回上一次成功的 map（或空 map）——版本选择是**增强**而非必需，
    不能因元数据查询失败让整个检索链路 500。
    """
    now_ts = time.time()
    if not force and now_ts - _cache["ts"] < _CACHE_TTL_SECONDS:
        return _cache["map"]
    try:
        from sqlalchemy import text
        from app.clients.mysql_client import get_session_local

        db = get_session_local()()
        try:
            rows = db.execute(text(
                "SELECT source, doc_key, revision, validity, effective_from, effective_to, "
                "status, deleted FROM knowledge_documents WHERE deleted = 0 AND status = 1"
            )).mappings().all()
        finally:
            db.close()
        current = select_current_versions([dict(r) for r in rows])
        _cache["ts"] = now_ts
        _cache["map"] = current
        logger.info(f"kb 版本清单已加载: {len(current)} 个主题")
        return current
    except Exception as e:
        logger.warning(f"kb 版本清单读取失败（沿用缓存/放行）: {type(e).__name__}: {e}")
        _cache["ts"] = now_ts  # 避免每次请求都重试打库
        return _cache["map"]


def clear_cache() -> None:
    """测试/手动重灌后强制下次重读"""
    _cache["ts"] = 0.0
    _cache["map"] = {}


__all__ = [
    "select_current_versions",
    "filter_superseded",
    "load_current_versions",
    "clear_cache",
]
