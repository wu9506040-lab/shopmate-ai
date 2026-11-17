"""知识运营闭环第 4 步：入库后回归验证，通过才关单。

为什么关单条件必须是「问句 rank」而不是「人点了完成」
------------------------------------------------
补卡这件事有两种失败方式，肉眼都看不出来：
  1. 补了卡但没重灌（向量没更新）——人以为做完了
  2. 重灌了但新段落跟买家说法仍不贴近（写成了第二份政策原文）——检索还是没变化
只有在补完之后**用同一批问句重跑检索**，才知道有没有用。所以工单关单条件是
`queries 全部进 top-n`，不是状态位。

验证口径必须跟主链路一致（BUGS 53 的教训）
----------------------------------------
归因时用 skip_rerank=True 省钱，但验证时必须走**完整链路（含精排）**：
用户实际拿到的是精排后的 top-3，跳过精排验的是另一条路，补卡无效也会判过。

关单顺手回填 expected_source，这是整个闭环的复利点
--------------------------------------------
补卡之前这批问句没有金标准（所以归因只能判 unknown）。补完验证通过，
就说明「这条知识确实是这个问句该命中的」——把这层结论写回 badcase，
下次同样的问句再失败就有金标准可比，不必再人工标注。
只填空值，不覆盖人工已填的：人的标注永远优先。
"""

from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.kb_ops.diagnose import doc_stem, retrieve_sources
from app.services.kb_ops.ticket import (
    DEFAULT_TOP_N,
    STATUS_APPLIED,
    STATUS_CLOSED,
    STATUS_REOPENED,
    load_store,
    save_store,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# 回填 note 的长度上限（badcase_candidates.note 是 String(500)，
# 留 20 字符余量给并发写入/编码差异，超长会被 MySQL 静默截断）
_NOTE_LIMIT = 480


def verify_ticket(
    ticket: Dict[str, Any],
    *,
    top_n: Optional[int] = None,
    top_k: Optional[int] = None,
    skip_rerank: bool = False,
) -> Dict[str, Any]:
    """拿工单里的验证问句重跑检索，算每条问句的 rank 与是否进 top-n。

    检索不可用时返回 inconclusive=True：既不判过也不判不过，
    因为"检索服务挂了"跟"补卡没效果"是两件事，混判会让人白改一轮稿。
    """
    n = int(top_n or ticket.get("top_n") or DEFAULT_TOP_N)
    k = int(top_k or max(n, 5))
    target = doc_stem(ticket.get("source") or "")
    queries = list(ticket.get("queries") or [])

    details: List[Dict[str, Any]] = []
    for q in queries:
        srcs = retrieve_sources(q, k, skip_rerank=skip_rerank)
        if srcs is None:
            details.append({"query": q, "rank": None, "hit": False, "error": "检索不可用"})
            continue
        rank = next((i for i, s in enumerate(srcs, 1) if doc_stem(s) == target), None)
        details.append({
            "query": q,
            "rank": rank,
            "hit": rank is not None and rank <= n,
            "top": srcs[:k],
        })

    inconclusive = any("error" in d for d in details)
    graded = [d for d in details if "error" not in d]
    passed = bool(graded) and all(d["hit"] for d in graded)

    return {
        "ticket": ticket.get("id"),
        "source": ticket.get("source"),
        "top_n": n,
        "skip_rerank": skip_rerank,
        "total": len(details),
        "hits": sum(1 for d in graded if d["hit"]),
        "passed": passed and not inconclusive,
        "inconclusive": inconclusive,
        "details": details,
        "baseline": ticket.get("baseline") or {},
    }


def backfill_expected_sources(
    db: Session,
    *,
    queries: List[str],
    source: str,
    ticket_id: str = "",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """把「该问句本该命中这条知识」写回 badcase_candidates。

    只填空值（expected_source IS NULL OR ''）：人工标注过的绝不覆盖，
    机器回填只在"人还没标"的空位上补，避免把人的判断冲掉。
    """
    from app.services.badcase_service import query_hash

    hashes = tuple({query_hash(q) for q in queries if q})
    if not hashes:
        return {"updated": 0, "reason": "没有可回填的问句"}
    if dry_run:
        return {"updated": 0, "dry_run": True, "query_hashes": len(hashes)}

    tag = f" [kb_ops {ticket_id} 回填]".strip()
    stmt = text(
        "UPDATE badcase_candidates "
        "SET expected_source = :src, "
        "    note = LEFT(CONCAT(COALESCE(note, ''), :tag), :lim), "
        "    update_time = NOW() "
        "WHERE deleted = 0 AND query_hash IN :hs "
        "  AND (expected_source IS NULL OR expected_source = '')"
    )
    res = db.execute(stmt, {"src": source, "tag": tag, "lim": _NOTE_LIMIT, "hs": hashes})
    db.commit()
    return {"updated": int(getattr(res, "rowcount", 0) or 0)}


def settle_ticket(
    db: Session,
    ticket: Dict[str, Any],
    *,
    store: Optional[Dict[str, Any]] = None,
    root: Optional[Path] = None,
    top_n: Optional[int] = None,
    skip_rerank: bool = False,
    backfill: bool = True,
) -> Dict[str, Any]:
    """验证 + 改状态 + 回填，一次做完。

    状态机：
        applied/drafted/open --验证通过--> closed（并回填 expected_source）
                             --验证不过--> reopened（退回改稿）
                             --检索挂了--> 状态不动（不冤枉人也不放过）
    """
    result = verify_ticket(ticket, top_n=top_n, skip_rerank=skip_rerank)

    if result["inconclusive"]:
        result["status"] = ticket.get("status")
        result["note"] = "检索不可用，本轮不判定"
        return result

    if result["passed"]:
        ticket["status"] = STATUS_CLOSED
        ticket["closed_at"] = _now()
        if backfill:
            result["backfilled"] = backfill_expected_sources(
                db,
                queries=list(ticket.get("queries") or []),
                source=ticket.get("source") or "",
                ticket_id=ticket.get("id") or "",
            )
    else:
        ticket["status"] = STATUS_REOPENED

    ticket["updated_at"] = _now()
    ticket["last_verify"] = {
        "at": _now(),
        "passed": result["passed"],
        "hits": result["hits"],
        "total": result["total"],
        "ranks": {d["query"]: d["rank"] for d in result["details"] if "error" not in d},
    }
    ticket.setdefault("history", []).append({
        "at": _now(),
        "event": "verified",
        "note": "pass" if result["passed"] else "fail",
    })
    result["status"] = ticket["status"]

    st = store if store is not None else load_store(root)
    if store is None:
        # 没传 store 时不能直接 save：调用方手里的 ticket 对象可能不在
        # 刚落盘的那份 store 里，改了会被覆盖掉。按 id 合并进去再存。
        idx = next((i for i, x in enumerate(st["tickets"])
                    if x.get("id") == ticket.get("id")), None)
        if idx is None:
            st["tickets"].append(ticket)
        else:
            st["tickets"][idx] = ticket
    save_store(st, root)
    return result


def settle_pending(
    db: Session,
    *,
    root: Optional[Path] = None,
    top_n: Optional[int] = None,
    skip_rerank: bool = False,
) -> List[Dict[str, Any]]:
    """把所有「已入库待验证」的单一起验一遍（每日巡检入口）。

    只挑 applied：open 还没稿、drafted 稿还没落盘、closed 已验证过、
    reopened 等人改——这几种重验都是白花检索成本，且会把状态搅乱。
    """
    st = load_store(root)
    out = []
    for t in list(st.get("tickets", [])):
        if t.get("status") != STATUS_APPLIED:
            continue
        if not t.get("queries"):
            continue
        out.append(settle_ticket(db, t, store=st, root=root, top_n=top_n,
                                 skip_rerank=skip_rerank))
    return out
