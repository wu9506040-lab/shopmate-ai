"""知识运营闭环第 2 步：把归因结果按「知识源」聚合成待办清单。

为什么按 source 分组而不是按问题聚类
--------------------------------
1. badcase 已经有 query_hash 去重和 hit_count 计数，相似问题不必再聚一次
2. 运营要动手的单位是"哪条知识有问题"，不是"哪些问题长得像"
3. 按 source 分组的结果可以直接说"这条政策卡拖了 12 次失败"，
   聚类还要额外解释"为什么这两个问题被合并成一类"

一个 source 可能同时出现多种根因（比如既有缺内容又有过期段落），
所以保留 causes 分布，主 action 取占比最高的那类，其余在明细里可见。
"""

import json
from collections import defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.kb_ops.diagnose import ROOT_CAUSE_LABELS, ROOT_UNKNOWN


def _as_dict(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _action_of(causes: Dict[str, int]) -> str:
    if not causes:
        return ROOT_CAUSE_LABELS[ROOT_UNKNOWN]
    top = max(causes.items(), key=lambda kv: kv[1])[0]
    return ROOT_CAUSE_LABELS.get(top, top)


def source_report(
    db: Session,
    *,
    cause: Optional[str] = None,
    top: int = 15,
    max_queries: int = 3,
) -> Dict[str, Any]:
    """按知识源聚合的待办清单。

    Args:
        cause: 只看某一类根因（如 no_kb）
        top: 返回前 N 个问题源
        max_queries: 每个源附带几条代表性问句（给补卡时参考）
    """
    rows = db.execute(
        text(
            "SELECT root_cause, root_cause_evidence FROM qa_results "
            "WHERE deleted=0 AND root_cause IS NOT NULL"
        )
    ).fetchall()

    by_cause: Dict[str, int] = defaultdict(int)
    per_source: Dict[str, Dict[str, Any]] = {}
    pending_review = 0

    for root_cause, raw_evidence in rows:
        evidence = _as_dict(raw_evidence)
        src = (evidence.get("expected_source") or "").strip()

        if cause and root_cause != cause:
            continue

        # 只有 unknown 才是"待标注"。context_missing 已经归因完了，
        # 它只是没有知识源维度（本来就不是知识问题），不能算进待标注队列，
        # 否则运营会看到一堆"要标注"却是订单链路的题。
        if root_cause == ROOT_UNKNOWN:
            pending_review += 1
            continue

        by_cause[root_cause] += 1
        if not src:
            continue

        bucket = per_source.setdefault(
            src,
            {
                "source": src,
                "total": 0,
                "causes": defaultdict(int),
                "kb_has_source": bool(evidence.get("kb_has_source")),
                "queries": [],
            },
        )
        bucket["total"] += 1
        bucket["causes"][root_cause] += 1
        q = evidence.get("query")
        if q and q not in bucket["queries"] and len(bucket["queries"]) < max_queries:
            bucket["queries"].append(q)

    sources: List[Dict[str, Any]] = []
    for bucket in per_source.values():
        causes = dict(bucket["causes"])
        bucket["causes"] = causes
        bucket["action"] = _action_of(causes)
        sources.append(bucket)

    sources.sort(key=lambda x: (-x["total"], x["source"]))

    return {
        "total": sum(by_cause.values()),
        "by_cause": dict(by_cause),
        "pending_review": pending_review,
        "sources": sources[:top],
        "source_count": len(sources),
    }
