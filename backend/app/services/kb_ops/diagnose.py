"""知识运营闭环第 1 步：把「答得不好」归因成「该干什么」。

为什么必须拆细
--------------
qa_results.failure_layer 只说"哪一层坏了"（retrieval / generation / ...），
但 retrieval 一档里混了三种完全不同的病，修法互不相同：

    知识库里根本没这条          → 补一张新卡
    有这条，但 top-k 没搜到      → 调检索（同义词/切分/权重）
    搜到了，但内容已过期/冲突    → 改这张卡

归因只到 retrieval 这一层，运营拿到清单还是不知道该做什么。所以这里拆成细类：

    知识库里根本没这条          → 补一张新卡            (no_kb)
    有这条，有效，但 top-k 没搜到 → 调检索               (retrieval_miss)
    搜到了但排位靠后            → 调排序               (ranking_weak)
    搜到了，但内容已过期/冲突    → 改卡                 (kb_stale)
    这条已过期，同主题没有新版    → 补新版本卡            (kb_expired)
    资料没问题，是模型没照资料答  → 改生成               (generation)

kb_expired 是实盘补的：双 11 活动卡有效期至 2026-11-11，11-17 再问"现在有什么优惠"，
它在库里但检索层按设计过滤失效条目。若判成 retrieval_miss，运营去调同义词、补锚点，
补完照样被过滤——派一个做了也没用的活，比不派更浪费人。

为什么不用 LLM 判根因
--------------------
给系统再加一个不可验证的环节，判错了还查不出来。判据全部是确定性的：
拿 query 去检索，看 top-k 里有没有 expected_source；再查 knowledge_documents
里到底有没有这条。每一步都能在 evidence 里写明依据。

一个必须区分的情况
------------------
检索服务挂了（Milvus 不可用 / embedding 超时）不等于"知识库里没有这条"。
混在一起会让运营按 no_kb 去补一堆已经存在的卡，越补越乱。
所以检索失败单独记 unknown + retrieve_failed，宁可这一轮不判。
"""

import logging
import re
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

ROOT_NO_KB = "no_kb"
ROOT_RETRIEVAL_MISS = "retrieval_miss"
ROOT_KB_STALE = "kb_stale"
ROOT_RANKING_WEAK = "ranking_weak"
ROOT_KB_EXPIRED = "kb_expired"
ROOT_GENERATION = "generation"
ROOT_CONTEXT_MISSING = "context_missing"
ROOT_UNKNOWN = "unknown"

ROOT_CAUSE_LABELS = {
    ROOT_NO_KB: "知识库缺这条（补新卡）",
    ROOT_RETRIEVAL_MISS: "有这条但没搜到（调检索）",
    ROOT_KB_STALE: "搜到了但内容冲突（改卡）",
    ROOT_RANKING_WEAK: "搜到了但排位靠后（调排序）",
    ROOT_KB_EXPIRED: "知识已过期（补新版本，不是调检索）",
    ROOT_GENERATION: "资料对但答案歪（改生成）",
    ROOT_CONTEXT_MISSING: "上下文没拿到（查工具/订单链路，非知识问题）",
    ROOT_UNKNOWN: "无法判定（待标注/检索不可用）",
}

# 四维分是 1-5，2 分及以下视为"明显不合格"。
# 与 qa/judge.py 的 MIN_PASS_DIM 同口径，改一处要同步改另一处。
LOW_DIM = 2
# 命中但排在第 4 位之后：资料在候选里，容易被上下文预算挤掉或模型忽略。
RANK_WEAK = 3

# 工具/上下文没拿到时的典型话术。出现这些说明答案是"我没查到数据"，
# 跟知识库全不全无关——按知识根因去补卡/改 prompt 都是白费功夫。
# 只匹配明确指向"查不到"的表述，避免把"没有订单号也能退货"这类正常政策话术误伤。
_CONTEXT_FAIL_MARKERS = (
    "您当前没有订单",
    "当前没有订单",
    "暂无订单",
    "未查询到",
    "没有查询到",
    "查不到您的",
    "找不到您的订单",
    "无法查询到",
)

DEFAULT_TOP_K = 5


def looks_like_context_failure(answer: Optional[str]) -> bool:
    """答案是不是在说"我没拿到数据"。"""
    text_ = answer or ""
    return any(m in text_ for m in _CONTEXT_FAIL_MARKERS)


def _dims_of(dims: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """只取归因用到的两维，避免 evidence 变成整份质检结果的副本。"""
    if not isinstance(dims, dict):
        return {}
    out = {}
    for key in ("grounding", "consistency"):
        val = dims.get(key)
        if isinstance(val, (int, float)):
            out[key] = val
    return out


def classify(
    *,
    expected_source: Optional[str],
    retrieved_sources: Optional[List[str]] = None,
    kb_has_source: bool = False,
    kb_status: Optional[str] = None,
    kb_detail: str = "",
    superseded_by: str = "",
    dims: Optional[Dict[str, Any]] = None,
    top_k: int = DEFAULT_TOP_K,
    retrieve_failed: bool = False,
    answer: Optional[str] = None,
    failure_layer: Optional[str] = None,
) -> tuple[str, Dict[str, Any]]:
    """纯函数：按确定判据给出根因 + 证据。

    Args:
        expected_source: 人工标注的"本该命中的知识源"
        retrieved_sources: 检索返回的 source 序列（按相关度降序）
        kb_has_source: knowledge_documents 里该 source 是否有效存在
        kb_status: 更细的知识状态，见 `_kb_status`：
            active（有效）/ expired（本条过期，同主题无新版）/ superseded（本条过期但有新版）
            / missing（库里没有或已停用）
        kb_detail: 过期/未生效的人话说明（如"已过期（有效期至 2026-11-11）"）
        superseded_by: 同主题下当前生效的版本文件名
        dims: 质检四维分（只读 grounding / consistency）
        retrieve_failed: 检索链路是否失败（失败时不能判"没有"）
        answer: 当轮答案，用于识别"工具没查到数据"这类非知识问题
        failure_layer: 质检给的失败层，intent_router 等非知识层要排除

    Returns:
        (root_cause, evidence)
    """
    # 最先判：答案本身在说"我没拿到数据"。这类跟知识库无关，
    # 也不该因为没标 expected_source 就扔进标注队列——标注员标不出来，
    # 因为它压根不是知识缺失（实盘例子：qa#2392「我要换货」答"您当前没有订单"）。
    if looks_like_context_failure(answer) and failure_layer in (None, "generation"):
        return ROOT_CONTEXT_MISSING, {
            "reason": "答案在说没查到数据（订单/工具链路），非知识问题",
            "expected_source": (expected_source or "").strip() or None,
            "failure_layer": failure_layer,
        }

    src = (expected_source or "").strip()
    if not src:
        return ROOT_UNKNOWN, {
            "reason": "badcase 未标注 expected_source，无法归因，进待标注队列",
            "expected_source": None,
        }

    if retrieve_failed:
        return ROOT_UNKNOWN, {
            "reason": "检索链路不可用，本轮不判定（避免误判成知识缺失）",
            "expected_source": src,
            "retrieve_failed": True,
        }

    if kb_status is None:
        kb_status = "active" if kb_has_source else "missing"

    sources = [s for s in (retrieved_sources or []) if s]
    target = doc_stem(src)
    rank: Optional[int] = None
    for idx, item in enumerate(sources[:top_k], 1):
        if doc_stem(item) == target:
            rank = idx
            break

    dim_snapshot = _dims_of(dims)
    base = {
        "expected_source": src,
        "rank": rank,
        "retrieved_sources": sources[:top_k],
        "kb_has_source": kb_status in ("active", "expired", "superseded"),
        "kb_status": kb_status,
        "kb_detail": kb_detail,
        "superseded_by": superseded_by or None,
        "dims": dim_snapshot,
        "top_k": top_k,
    }

    if rank is None:
        # 过期这一档必须在"没搜到"之前判：检索层（_drop_inactive）按设计过滤失效条目，
        # 所以过期卡**永远**搜不到。判成 retrieval_miss 会让运营去调同义词/补锚点，
        # 补完照样被过滤——派一个做了也没用的活，是这个闭环最危险的错法。
        if kb_status == "expired":
            return ROOT_KB_EXPIRED, {
                **base,
                "reason": f"top-{top_k} 未命中，且 {src} {kb_detail or '已过期'}；"
                          f"检索层按设计过滤失效条目，补检索锚点不会生效",
            }
        # 本例过期但同主题有生效版本：说明该搜新版而没搜到，是真检索问题
        if kb_status == "superseded":
            return ROOT_RETRIEVAL_MISS, {
                **base,
                "reason": f"{src} {kb_detail or '已过期'}，同主题生效版本是 "
                          f"{superseded_by}，但 top-{top_k} 没检出新版",
            }
        if kb_status == "missing":
            return ROOT_NO_KB, {
                **base,
                "reason": f"top-{top_k} 未命中，且知识库里没有 {src}",
            }
        return ROOT_RETRIEVAL_MISS, {
            **base,
            "reason": f"知识库里有 {src}，但 top-{top_k} 没检出来",
        }

    grounding = dim_snapshot.get("grounding")
    consistency = dim_snapshot.get("consistency")

    # 资料给了、答案也用上了，但结论冲突 → 优先怀疑资料本身过期/自相矛盾。
    # 注意：consistency 低也可能是模型自己前后矛盾，所以报表里标"疑似"，
    # 由人确认是改卡还是改生成——这一步不做自动改卡。
    if isinstance(consistency, (int, float)) and consistency <= LOW_DIM:
        if not isinstance(grounding, (int, float)) or grounding > LOW_DIM:
            return ROOT_KB_STALE, {
                **base,
                "reason": f"命中第 {rank} 位，但 consistency={consistency}，疑与资料冲突",
            }

    # 资料就在眼前却没被用上（grounding 低）→ 生成侧问题
    if isinstance(grounding, (int, float)) and grounding <= LOW_DIM:
        return ROOT_GENERATION, {
            **base,
            "reason": f"命中第 {rank} 位，但 grounding={grounding}，答案没依据资料",
        }

    # 命中但排位靠后：多半被上下文预算挤掉或排在资料位之外
    if rank > RANK_WEAK:
        return ROOT_RANKING_WEAK, {
            **base,
            "reason": f"命中但排在第 {rank} 位，容易被上下文预算挤掉",
        }

    return ROOT_GENERATION, {
        **base,
        "reason": f"命中第 {rank} 位且分数不低，仍答得不好，归生成侧",
    }


def _hit_doc_source(hit: Dict[str, Any]) -> str:
    """命中条目所属的**文档级** source（可能是原始写法，比较前要过 doc_stem）。

    坑：同一个文档有四种写法——
    - Milvus payload：`doc_source` = `xxx.md`，`source` = `xxx_sec4`（chunk 级）
    - knowledge_documents.source：`xxx.md`
    - badcase.expected_source：人工填的 `xxx.md`
    - 主链路 search_policy 返回扁平结构，只有 `source`（chunk 级）和 `doc_key`
    直接等值比较会**永远判"未命中"**，把每个案子都归成 no_kb / retrieval_miss。
    """
    payload = hit.get("payload") or hit
    return (payload.get("doc_source") or payload.get("source")
            or payload.get("doc_key") or "")


_SEC_SUFFIX = re.compile(r"_sec\d+$")


def doc_stem(name: Optional[str]) -> str:
    """把 source 归一成「文档标识」，用于跨口径比较。

    `fashion_return_policy_v202606.md`   → `fashion_return_policy_v202606`
    `fashion_return_policy_v202606_sec4` → `fashion_return_policy_v202606`

    保留版本号是刻意的：新旧两版是**不同文档**，混成一个 stem
    就判不出"检索命中了已废止的旧版"这件事。
    """
    s = (name or "").strip()
    if s.endswith(".md"):
        s = s[:-3]
    return _SEC_SUFFIX.sub("", s)


def retrieve_sources(
    query: str, top_k: int, *, skip_rerank: bool = True,
) -> Optional[List[str]]:
    """检索 top-k 的文档级 source 序列。返回 None 表示检索不可用（不是"没结果"）。

    走 PolicyService.search_policy 而不是裸 vector search，是为了让归因和
    主链路**同一个口径**：search_policy 里会过滤已失效条目并补失效提示
    （见 policy_service._drop_inactive）。用裸检索会看到主链路根本不会用的
    旧版政策，把"系统其实答得对"的案子误判成检索失败。

    skip_rerank 默认 True：归因只需要候选集，不值得为每条再付一次精排的 LLM 成本。
    但**验证**（verify）必须传 False —— 要判的是"用户实际拿到的 top-3 里有没有它"，
    跳过精排等于验的是另一条链路，补卡没效果也会被判通过。
    """
    try:
        from app.services.policy_service import PolicyService

        hits = PolicyService.search_policy(
            query, top_k=top_k, skip_rerank=skip_rerank) or []
        return [_hit_doc_source(h) for h in hits]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[kb_ops] 检索失败（本轮不判定）: {e}")
        return None


def _kb_status(db: Session, source: str) -> Dict[str, Any]:
    """knowledge_documents 里这条知识的**有效期状态**。

    为什么要看有效期，而不是只看"在不在库里"
    ----------------------------------------
    实盘踩过：`fashion_promo_2026_double11.md` 有效期至 2026-11-11，
    查询当天是 11-17。它在库里（status=1），但检索层 `_drop_inactive()`
    按设计把它过滤了——所以它**永远**搜不到。只按"在不在库里"判，就会判成
    retrieval_miss，派运营去调同义词/补锚点，补完了还是搜不到。

    status:
      active     存在、启用、当前有效
      expired    存在但已过期/未生效，且同主题没有其它生效版本 → 该补新版本
      superseded 本条已过期，但同 doc_key 下有生效版本 → 该让新版被搜到（真检索问题）
      missing    库里没有，或已停用（status=0）
    """
    stem = doc_stem(source)
    if not stem:
        return {"status": "missing", "detail": ""}
    variants = (stem, f"{stem}.md")
    try:
        row = db.execute(
            text(
                "SELECT source, status, validity, effective_from, effective_to, doc_key "
                "FROM knowledge_documents WHERE source IN :v AND deleted=0 LIMIT 1"
            ),
            {"v": variants},
        ).fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[kb_ops] 查知识库元数据失败: {e}")
        return {"status": "missing", "detail": f"查库失败：{e}"}

    if row is None or int(row[1] or 0) != 1:
        return {"status": "missing", "detail": "库里没有或已停用"}

    from app.services.kb_meta import validity_of

    def _v(validity, ef, et) -> Dict[str, Any]:
        return validity_of({"validity": validity,
                            "effective_from": ef, "effective_to": et})

    state = _v(row[2], row[3], row[4])
    if state.get("active"):
        return {"status": "active", "detail": state.get("label") or ""}

    doc_key = row[5]
    if not doc_key:
        return {"status": "expired", "detail": state.get("label") or ""}

    # 同主题下还有没有别的生效版本：有的话问题不在"缺卡"，在"新版没被搜到"
    try:
        sibs = db.execute(
            text(
                "SELECT source, status, validity, effective_from, effective_to "
                "FROM knowledge_documents WHERE deleted=0 AND doc_key=:k "
                "AND source<>:s AND status=1"
            ),
            {"k": doc_key, "s": row[0]},
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[kb_ops] 查同主题版本失败: {e}")
        sibs = []

    for r in sibs:
        if _v(r[2], r[3], r[4]).get("active"):
            return {"status": "superseded",
                    "detail": state.get("label") or "",
                    "superseded_by": r[0]}

    return {"status": "expired", "detail": state.get("label") or ""}


def diagnose_one(
    db: Session,
    *,
    query: str,
    expected_source: Optional[str],
    dims: Optional[Dict[str, Any]] = None,
    top_k: int = DEFAULT_TOP_K,
    answer: Optional[str] = None,
    failure_layer: Optional[str] = None,
) -> Dict[str, Any]:
    """单条归因：检索 + 查库 + 分类。"""
    src = (expected_source or "").strip()

    # 已经是明确的上下文失败就没必要花一次检索：省成本，也避免把
    # "检索不到"当证据（这类问题本来就不该走知识检索）。
    if looks_like_context_failure(answer) and failure_layer in (None, "generation"):
        root, evidence = classify(
            expected_source=src, answer=answer, failure_layer=failure_layer,
        )
        evidence["query"] = (query or "")[:200]
        return {"root_cause": root, "evidence": evidence}

    sources = retrieve_sources(query, top_k) if src else None
    kb = _kb_status(db, src) if src else {"status": "missing", "detail": ""}
    root, evidence = classify(
        expected_source=src,
        retrieved_sources=sources,
        kb_status=kb.get("status"),
        kb_detail=kb.get("detail") or "",
        superseded_by=kb.get("superseded_by") or "",
        dims=dims,
        top_k=top_k,
        retrieve_failed=src != "" and sources is None,
        answer=answer,
        failure_layer=failure_layer,
    )
    evidence["query"] = (query or "")[:200]
    return {"root_cause": root, "evidence": evidence}


def _load_candidates(db: Session, limit: int) -> List[Dict[str, Any]]:
    """待归因的质检记录：只取知识侧的失败。

    两个收窄条件，都是防误判：
    1. `verdict='low'` —— blocked 是 guard 拦截（安全侧），它在库里同样有记录，
       但"被拦下来"跟知识全不全没关系，归因进来只会污染清单。
    2. `failure_layer` 只认 retrieval / generation —— intent_router 判错时
       检索必然搜不到，此时判 no_kb 会让人去补一堆本来不该补的卡（真缺的是
       同义词或意图规则）；safety_policy / length_fuse 同理，都不是知识问题。
       failure_layer 为空说明规则命中但 LLM 没给层级，仍纳入（由后续判据兜底）。
    """
    rows = db.execute(
        text(
            "SELECT id, query, dims, answer, failure_layer FROM qa_results "
            "WHERE deleted=0 AND root_cause IS NULL AND verdict='low' "
            "AND (failure_layer IS NULL "
            "     OR failure_layer IN ('retrieval','generation')) "
            "ORDER BY id DESC LIMIT :n"
        ),
        {"n": limit},
    ).fetchall()
    return [
        {
            "id": r[0],
            "query": r[1] or "",
            "dims": r[2],
            "answer": r[3] or "",
            "failure_layer": r[4],
        }
        for r in rows
    ]


def _expected_source_map(db: Session, queries: List[str]) -> Dict[str, str]:
    """query_hash → expected_source。

    badcase 的 (query_hash, signal) 唯一约束意味着同一句话的标注是共享的，
    这正是我们要的：同一句话该命中哪条知识不随会话变。
    """
    from app.services.badcase_service import query_hash

    hashes = {query_hash(q): q for q in queries if q}
    if not hashes:
        return {}
    rows = db.execute(
        text(
            "SELECT query_hash, expected_source FROM badcase_candidates "
            "WHERE deleted=0 AND expected_source IS NOT NULL AND expected_source<>'' "
            "AND query_hash IN :hs"
        ),
        {"hs": tuple(hashes.keys())},
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def run_batch(
    db: Session,
    *,
    limit: int = 100,
    top_k: int = DEFAULT_TOP_K,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """批量归因并回写 qa_results.root_cause / root_cause_evidence。

    回写是为了让报表不必每次重跑检索（检索有延迟也有成本），
    同时让人工复核时能看到"当时凭什么这么判"。
    """
    from app.services.badcase_service import query_hash

    candidates = _load_candidates(db, limit)
    if not candidates:
        return {"scanned": 0, "updated": 0, "skipped": 0, "by_cause": {}}

    src_map = _expected_source_map(db, [c["query"] for c in candidates])

    by_cause: Dict[str, int] = {}
    updated = 0
    skipped = 0
    pending_review = 0
    details: List[Dict[str, Any]] = []

    for item in candidates:
        expected = src_map.get(query_hash(item["query"]))

        # 上下文失败不需要人工标注：它跟"该命中哪条知识"无关，
        # 扔进标注队列只会让标注员面对一个标不出来的题。
        context_case = (
            looks_like_context_failure(item.get("answer"))
            and item.get("failure_layer") in (None, "generation")
        )
        if not expected and not context_case:
            pending_review += 1
            skipped += 1
            continue

        res = diagnose_one(
            db,
            query=item["query"],
            expected_source=expected or "",
            dims=item["dims"],
            top_k=top_k,
            answer=item.get("answer"),
            failure_layer=item.get("failure_layer"),
        )
        cause = res["root_cause"]
        by_cause[cause] = by_cause.get(cause, 0) + 1
        details.append({"id": item["id"], "root_cause": cause, **res["evidence"]})

        if dry_run:
            continue
        db.execute(
            text(
                "UPDATE qa_results SET root_cause=:c, root_cause_evidence=:e "
                "WHERE id=:id"
            ),
            {
                "c": cause,
                "e": _json_dumps(res["evidence"]),
                "id": item["id"],
            },
        )
        updated += 1

    if not dry_run:
        db.commit()

    return {
        "scanned": len(candidates),
        "updated": updated,
        "skipped": skipped,
        "pending_review": pending_review,
        "by_cause": by_cause,
        "details": details,
        "dry_run": dry_run,
    }


def _json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)
