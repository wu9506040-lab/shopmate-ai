"""
政策服务 - 政策 RAG（doc_type=policy 过滤）

按 PROJECT_DESIGN.md §3：policy_query 走 RAG，过滤 doc_type='policy' 排除商品/FAQ。

埋点 retrieve_hits + hit@K（policy 检索是 synthesizer 主路径，必须统计）

P1-检索 A：两阶段检索（粗排 → rerank 精排）
- USE_RERANK=true 时：Vector top-15 → LLM rerank → top-3
- USE_RERANK=false 时：保持原 Vector 直接 top-k（向后兼容）
- rerank 失败时降级到原始排序（rerank.py 内部处理）

P1-检索 B：混合检索（dense vector + BM25 + RRF）
- USE_HYBRID_BM25=true 时：
    1. Vector dense vector top-K
    2. BM25 稀疏检索 top-K（从 Vector corpus 内存索引）
    3. RRF 融合（Cormack 2009, k=60）→ top-K 融合候选
    4. 可选：rerank 精排 → top-3
- USE_HYBRID_BM25=false 时：仅 dense vector（兼容旧路径）
- BM25 索引构建失败时降级到仅 vector 路径（业务不崩）

A4: Multi-Query 多路检索
- queries: List[str]
- 每条 query 走一遍 search_policy（含 hybrid + rerank）→ 拿到该路的 top-K 候选
- N 路结果用 RRF 融合 → 截断到 top_k
- 任一路异常 → 仅该路降级（其他路继续）

A5: Multi-Query 并行检索（性能优化）
- 默认 MULTI_QUERY_PARALLEL=True + MULTI_QUERY_WORKERS=3
- ThreadPoolExecutor per-request（with 块，离开自动 shutdown）
- N 路并行 ≈ 1 路耗时（加速比 ~2.9x @ 3 路）
- 关掉并行：MULTI_QUERY_PARALLEL=False 走原串行（debug 用）

A8: 融合后 rerank（LLM 成本优化）
- 默认 MULTI_QUERY_FUSE_FIRST_RERANK=True
- 路径变化：N×(coarse→rerank) → N×coarse → RRF → 1×rerank
- 收益：3×rerank LLM 调用 → 1×，token -66% / 延迟 -66% / rerank 视角更全局
- 关闭回退：A8 flag=False → 走 A5 per-query rerank 路径

A8 实现要点：
- 抽取 _coarse_retrieval 公共方法（search_policy / search_policy_coarse 共用，零漂移）
- 新增 search_policy_coarse（粗排不带 rerank，给 fuse-first 模式用）
- 运行指标日志：[multi_query_metrics] mode= queries= rerank_calls= latency_ms=
  便于 grep 对比 A5 vs A8 真实效果（成本 / 延迟 / 召回质量）
"""
import contextvars
import logging
import re
import time
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from app.clients.vector_db import COLLECTION_NAME, search as vector_search
from app.core.config import settings
from app.schemas.knowledge import SearchResult
from app.services.metrics import metrics

logger = logging.getLogger(__name__)

# Vector collection 名（与现有 KB 对齐）
# 注：M2 阶段 KB 67 条全部是政策（source 命名 policy_* / warranty_*），
# 没有 doc_type 字段，不做后过滤。等 V2.6 引入商品/政策混合 KB 时再加 doc_type 过滤。
# 修复：原硬编码 "knowledge_base" 与 ingest 的 "faq_v1" 不一致 → 政策检索 0 命中
COLLECTION_NAME = COLLECTION_NAME

# 渠道隔离上下文：contextvar —— asyncio.to_thread 会透传 context，
# 每请求独立，web(mall) 与 xianyu 并发互不串扰。_coarse_retrieval 是
# @staticmethod 被多层调用，加参数波及全部调用点，contextvar 最轻。
_kb_channel_var: "contextvars.ContextVar" = contextvars.ContextVar(
    "kb_channel", default=None
)


def set_kb_channel(channel: Optional[str]) -> None:
    """设置当前请求的渠道过滤（"xianyu"/"mall"/None=不过滤）"""
    _kb_channel_var.set(channel)


# 商品切片绑定：从商品页进入会话时，检索只认该款切片，避免问 A 款命中 B 款数据。
# 与渠道隔离同为 contextvar —— 多层静态调用无需逐个加参数，每请求独立。
_SLICE_DOC_PREFIX = "product_"
_kb_model_var: "contextvars.ContextVar" = contextvars.ContextVar(
    "kb_model", default=None
)


def set_kb_model(model: Optional[str]) -> None:
    """设置当前请求绑定的款号（None=不按款隔离，保持原有通用检索）"""
    _kb_model_var.set(model)


_GENERIC_SIZE_TABLE_MARK = "| 尺码 |"


def _bind_product_slice(hits: list[dict], model: Optional[str]) -> list[dict]:
    """过滤：排除其他款切片；从商品页进来时排除通用尺码数值表。

    通用尺码指南里的是「HM261139 系列」等参考表，与具体在售款数值不同；
    两套数值同时进候选会让答案自相矛盾，因此商品上下文存在时只留该款切片。
    量体方法 / 选码原则等不含表格的通用说明仍然保留。
    """
    if not hits:
        return hits
    if not model:
        # 商品切片只在「从商品页进来」时使用；通用咨询不引入任一具体款的参数，
        # 避免污染政策检索，也保证无来源时不会答出某个款的尺码数值。
        return [
            h for h in hits
            if not str((h.get("payload") or {}).get("doc_key") or "")
            .startswith(_SLICE_DOC_PREFIX)
        ]
    kept: list[dict] = []
    dropped = 0
    for h in hits:
        payload = h.get("payload") or {}
        dk = str(payload.get("doc_key") or "")
        text = str(payload.get("text") or "")
        if dk.startswith(_SLICE_DOC_PREFIX) and dk != _SLICE_DOC_PREFIX + model:
            dropped += 1
            continue
        if not dk.startswith(_SLICE_DOC_PREFIX) and _GENERIC_SIZE_TABLE_MARK in text:
            dropped += 1
            continue
        kept.append(h)
    if dropped:
        logger.info(f"kb 商品切片绑定: model={model} dropped={dropped} kept={len(kept)}")
    return kept


# 尺码类问题优先带「尺码表」章节，其余优先带「商品参数」章节
_SIZE_HINTS = (
    "尺码", "胸围", "腰围", "衣长", "肩宽", "袖长", "臀围", "裤长",
    "腿围", "选码", "合身", "身高", "体重", "多大", "穿得", "号型",
)


@lru_cache(maxsize=32)
def _slice_rows_cached(model: str) -> tuple:
    """按 doc_key 精确读取该款切片全部 chunk（内容稳定，进程内缓存）。"""
    try:
        from app.clients.vector_db import COLLECTION_NAME, get_client

        rows = get_client().query(
            collection_name=COLLECTION_NAME,
            filter=f'doc_key == "{_SLICE_DOC_PREFIX}{model}"',
            output_fields=["*"],
            limit=12,
        )
        return tuple(
            {k: v for k, v in r.items() if k != "vector"}
            for r in (rows or [])
        )
    except Exception as e:
        logger.warning(f"商品切片精确读取失败 model={model}: {e}")
        return ()


def _with_pinned_slice(hits: list[dict], model: Optional[str], query: str) -> list[dict]:
    """当前款切片必带：向量召回可能把它挤出候选（通用尺码表更字面相关），
    因此按 doc_key 精确取回并插入最前，保证答案与商品页数值一致。"""
    if not model:
        return hits
    rows = _slice_rows_cached(model)
    if not rows:
        return hits

    def sec_no(r: dict) -> int:
        m = re.search(r"_sec(\d+)$", str(r.get("source") or ""))
        return int(m.group(1)) if m else 99

    by_sec: dict[int, dict] = {}
    for r in rows:
        by_sec.setdefault(sec_no(r), r)
    preferred = (3, 2) if any(w in (query or "") for w in _SIZE_HINTS) else (2, 3)
    picked = [by_sec[n] for n in preferred if n in by_sec]

    # 已在候选里的取原 hit，没命中的按 doc_key 精确结果补入；
    # 无论哪种都按「尺码表 / 参数表」的固定顺序排到最前。
    in_hits = {
        str((h.get("payload") or {}).get("source") or ""): h for h in hits
    }
    front = []
    for r in picked:
        src = str(r.get("source") or "")
        front.append(in_hits.get(src) or {"payload": r, "score": 1.0})
    wanted = {str(r.get("source") or "") for r in picked}
    rest = [
        h for h in hits
        if str((h.get("payload") or {}).get("source") or "") not in wanted
    ]
    logger.info(f"kb 商品切片置顶: model={model} pinned={len(front)} rest={len(rest)}")
    return front + rest


def _get_knowledge_source():
    """获取 KnowledgeSource（spec §3.4 #5 mock 替换点）

    ：PolicyService 走 KnowledgeSource Protocol 而非直接 import vector。
    这里用函数包装方便测试 monkeypatch。
    """
    from app.rag.factory import get_knowledge_source
    return get_knowledge_source()


def _drop_inactive(hits: list[dict], query: str = "") -> list[dict]:
    """过滤已过期 / 尚未生效的知识库条目（2026-11）。

    为什么必须在检索层过滤而不是靠提示词：
    模型看到一段"满 3000 减 300"的过期活动条款，很容易照着答；
    提示词里的"不要引用过期政策"是软约束，且模型无从判断哪条过期。
    在候选集里直接拿掉，才叫"答不出来"而不是"答错"。

    未标注有效期的历史条目 payload 里没有 effective_to，按长期保留
    （不能因为没标就全过滤掉）。
    """
    from app.services.kb_meta import validity_of

    kept: list[dict] = []
    dropped: list[tuple[dict, dict]] = []
    for h in hits:
        payload = h.get("payload") or {}
        v = validity_of(payload) if payload else {"active": True}
        if payload and not v.get("active"):
            dropped.append((h, v))
            logger.info(
                f"kb 失效条目已过滤: source={payload.get('source')} "
                f"validity={payload.get('validity')} effective_to={payload.get('effective_to')}"
            )
            continue
        kept.append(h)

    if not dropped:
        return kept

    logger.info(f"kb 失效条目共过滤 {len(dropped)} 条（过期/未生效）")

    # 用户问的正是那条已结束的活动时，静默丢弃会变成"答非所问"：
    # 模型只能拿无关条款去凑答案。这里补一条**只含结论、不含原文**的提示，
    # 让模型能答"该活动已结束"，同时不把过期优惠数字喂进去。
    from app.services.kb_meta import tags_of as _tags

    top_dropped, top_v = max(dropped, key=lambda x: float(x[0].get("score") or 0))
    kept_top = max((float(h.get("score") or 0) for h in kept), default=0.0)
    _pl = top_dropped.get("payload") or {}
    # 两个触发条件：
    #   ① 该条本来就是最高分（说明用户问的就是它）
    #   ② 查询里出现了它的标签/标题词（如"国庆"）—— 分数常因短查询偏低，标签更可靠
    kw_hit = any(t and t in (query or "") for t in _tags(_pl)) or (
        bool(_pl.get("title")) and str(_pl.get("title"))[:4] in (query or "")
    )
    d_score = float(top_dropped.get("score") or 0)
    # 触发条件收窄，避免"问了无关问题时突然蹦出一条活动已结束"：
    #   ① 查询里出现了它的标签/标题词（最可靠）
    #   ② 它是唯一命中且分数够高
    #   ③ 它明显高于所有有效条目（高出 0.05）
    trigger = kw_hit or (not kept and d_score >= 0.65) or (bool(kept) and d_score > kept_top + 0.05)
    if trigger:
        import datetime as _dt

        payload = dict(top_dropped.get("payload") or {})
        _tz = _dt.timezone(_dt.timedelta(hours=8))
        title = payload.get("title") or payload.get("source")
        if top_v.get("kind") == "scheduled":
            start = top_v.get("effective_from")
            start_txt = _dt.datetime.fromtimestamp(start, _tz).strftime("%Y-%m-%d") if start else "稍后"
            payload["text"] = (
                f"【未生效】《{title}》自 {start_txt} 起生效，当前不适用，不得作为现行政策引用。"
            )
        else:
            end = top_v.get("effective_to")
            end_txt = _dt.datetime.fromtimestamp(end, _tz).strftime("%Y-%m-%d") if end else ""
            payload["text"] = (
                f"【已失效】《{title}》已于 {end_txt or '此前'} 结束，不再受理，"
                f"其优惠内容不得引用；如用户询问，只需说明已结束、以当前公告为准。"
            )
        # 必须插到最前面：后面 search_policy 会 hits[:top_k] 截断，
        # 追加在末尾会被直接丢掉（调试时踩过：日志有、返回没有）
        kept.insert(0, {"payload": payload, "score": float(top_dropped.get("score") or 0),
                        "expired_notice": True})
        logger.info(f"kb 补失效提示: {payload.get('source')} kind={top_v.get('kind')}")

    return kept


def _boost_tag_matches(hits: list[dict], query: str) -> list[dict]:
    """查询命中条目标签时优先采用（2026-11）。

    为什么需要：RRF 只看"两路各自排第几"，不认语义。用户问"双十一买加绒夹克能优惠多少"，
    BM25 里双十一活动排第一，但退换条款在两路都靠前 → 融合后退换反超，
    答案就变成退换政策（实测踩过）。
    标签是人工维护的强信号（问题="双十一" ↔ 文档 tags 含"双十一"），
    用它做一次稳定重排，比调 RRF 权重更精准、也不影响无标签命中的查询。
    """
    from app.services.kb_meta import tags_of

    q = query or ""
    if not q:
        return hits

    # 促销意图词：用户说"双十一/补贴/以旧换新"时，临时活动条目必须排在长期政策前。
    # 为什么不能只靠 tags 文本匹配：文档标的是"双11"，用户写"双十一"，字面不相等。
    _PROMO_HINTS = ("活动", "促销", "优惠", "补贴", "以旧换新", "满减", "双11", "双十一",
                    "双12", "双十二", "折扣", "优惠券", "券后", "限时", "涨价", "降价", "价保")
    promo_intent = any(k in q for k in _PROMO_HINTS)

    for h in hits:
        payload = h.get("payload") or {}
        tags = tags_of(payload)
        tag_hit = any(t and t in q for t in tags)
        h["_tag_hit"] = tag_hit or (promo_intent and str(payload.get("doc_type") or "") == "promo")
    # sorted 是稳定排序：命中优先，其余保持原 RRF 顺序
    return sorted(hits, key=lambda h: (1 if h.get("_tag_hit") else 0), reverse=True)


def _format_hits(hits: list[dict]) -> list[dict]:
    """统一格式化输出 schema（search_policy / search_policy_coarse 共用）

    字段：text / source / score / rerank_score / rrf_score
    - rerank_score: None 表示未走 rerank
    - rrf_score: 仅混合检索时存在
    2026-11 新增元信息（标签 + 有效期），供 prompt 标注引用口径：
    - title / section / category / doc_type / tags
    - validity / validity_label / effective_from / effective_to
    """
    from app.services.kb_meta import tags_of, validity_of

    out: list[dict] = []
    for h in hits:
        payload = h.get("payload", {}) or {}
        v = validity_of(payload)
        out.append({
            "text": payload.get("text", "") or h.get("text", ""),
            "source": payload.get("source", "") or h.get("source", ""),
            "score": h.get("score", 0.0),
            "rerank_score": h.get("rerank_score"),  # rerank 才有；降级时 None
            "rrf_score": h.get("rrf_score"),  # 混合检索才有
            # ---- 元信息（2026-11）----
            "title": payload.get("title", ""),
            "section": payload.get("section", ""),
            "category": payload.get("category", ""),
            "doc_type": payload.get("doc_type", ""),
            "tags": tags_of(payload),
            "doc_key": payload.get("doc_key", ""),
            "revision": payload.get("revision", ""),
            "validity": v.get("validity"),
            "validity_label": v.get("label"),
            "effective_from": v.get("effective_from"),
            "effective_to": v.get("effective_to"),
        })
    return out


class PolicyService:
    """政策 RAG 服务"""

    # =============================================================
    # A8 抽取：粗排公共方法（search_policy / search_policy_coarse 共用）
    # =============================================================
    @staticmethod
    def _coarse_retrieval(
        query: str, coarse_top_k: int, threshold_relax: float = 0.0
    ) -> list[dict]:
        """粗排公共逻辑（embed → vector → hybrid → truncate）。

        search_policy 和 search_policy_coarse 都调本方法，确保两条路径
        在粗排阶段的行为完全一致（避免后续逻辑漂移）。

        Args:
            query: 用户问题
            coarse_top_k: 粗排候选数（默认 = RERANK_CANDIDATE_TOP_K = 15）
            threshold_relax: 阈值降档量（空召回重试那一轮用）

        Returns:
            粗排 hits（每条可能含 payload / rrf_score / score）
            返回空 list 表示该 query 检索失败（embed / vector 异常）
        """
        from app.core.providers.embedding import get_embedding_provider
        from app.services.retrieval_threshold import resolve as resolve_threshold

        # 0. 阈值按意图分档：政策从严、闲聊放宽（不改问题本身）
        score_threshold, tier = resolve_threshold(query, relax=threshold_relax)

        # 1. embedding
        try:
            query_vec = get_embedding_provider().embed_text(query)
        except Exception as e:
            logger.warning(f"PolicyService.embed_text 失败: {e}")
            return []

        # 2. vector top-K 粗排（channel 过滤——多渠道 KB 同 collection 隔离）
        try:
            hits = vector_search(
                query_vector=query_vec,
                top_k=coarse_top_k,
                score_threshold=score_threshold,
                collection_name=COLLECTION_NAME,
                channel=_kb_channel_var.get(),
            )
            metrics.record_retrieve_hits(len(hits))
            logger.debug(
                f"policy 检索阈值: tier={tier} threshold={score_threshold} "
                f"relax={threshold_relax} hits={len(hits)} query='{query[:40]}'"
            )
        except Exception as e:
            logger.warning(f"PolicyService vector 检索失败: {e}")
            return []

        # 3. hybrid: vector + BM25 + RRF（可选）
        if settings.USE_HYBRID_BM25:
            try:
                from app.services.bm25_index import bm25_search
                from app.services.rrf import rrf_fuse

                bm25_hits = bm25_search(query, top_k=settings.BM25_TOP_K)
                if bm25_hits:
                    fused = rrf_fuse(
                        [hits, bm25_hits],
                        k=settings.RRF_K,
                        weights=settings.RAG_TYPE_BOOST,  # P3-3
                    )
                    hits = fused[:coarse_top_k]
                    logger.debug(
                        f"hybrid: vector={len(hits)}/{coarse_top_k} "
                        f"bm25={len(bm25_hits)}/{settings.BM25_TOP_K} "
                        f"→ fused={len(fused)} → top{coarse_top_k}"
                    )
            except Exception as e:
                logger.warning(f"hybrid 检索失败，降级到纯 vector: {e}")

        # 2026-11：过期/未生效的知识库条目不进候选（长期政策无 effective_to，不受影响）
        # 版本选择：同一 doc_key 只留当前生效的最新版本（旧版即使"长期有效"也要淘汰）
        from app.services.kb_version import filter_superseded, load_current_versions

        active = _drop_inactive(hits, query=query)
        versioned = filter_superseded(active, load_current_versions())
        # 按款绑定在粗排出口做一次：search_policy / search_multi_policy 两条路径都经过这里
        model = _kb_model_var.get()
        bound = _bind_product_slice(_boost_tag_matches(versioned, query), model)
        return _with_pinned_slice(bound, model, query)

    # =============================================================
    # 单路检索（保持向后兼容，A4/A5/A8 全部基于它）
    # =============================================================
    @staticmethod
    def search_policy(
        query: str,
        top_k: int = 3,
        skip_rerank: bool = False,
        threshold_relax: float = 0.0,
    ) -> list[dict]:
        """
        检索政策 KB（退换/尺码/物流/促销等）

        流程：_coarse_retrieval → 可选 rerank → top_k

        Args:
            query: 用户问题
            top_k: 返回条数
            skip_rerank: （2.2）True=跳过 LLM 精排（cache_hit 展示型检索专用，
                不为"看个来源"再付一次精排的 token 成本和 1.2s 延迟）
            threshold_relax: 阈值降档量（空召回重试那一轮用）

        Returns:
            [{"text": str, "source": str, "score": float,
              "rerank_score": float|None, "rrf_score": float|None}, ...]
        """
        coarse_top_k = (
            settings.RERANK_CANDIDATE_TOP_K
            if settings.USE_RERANK
            else top_k
        )

        hits = PolicyService._coarse_retrieval(
            query, coarse_top_k, threshold_relax=threshold_relax
        )
        if not hits:
            metrics.record_hit_at_k(0)
            return []

        # 可选：rerank 精排（ 2.2：skip_rerank 直取粗排前 top_k，不再调 LLM）
        if settings.USE_RERANK and len(hits) > top_k:
            if skip_rerank:
                hits = hits[:top_k]
            else:
                try:
                    from app.core.providers.rerank import get_rerank_provider
                    hits = get_rerank_provider().rerank(query, hits, top_n=top_k)
                    logger.debug(
                        f"policy rerank: candidates={len(hits)}/{coarse_top_k} → top{top_k}"
                    )
                except Exception as e:
                    logger.warning(f"policy rerank 失败，降级到粗排: {e}")
                    hits = hits[:top_k]

        # rerank 会重排顺序，出口再绑一次，确保当前款切片留在返回列表最前
        _model = _kb_model_var.get()
        hits = _with_pinned_slice(
            _bind_product_slice(hits, _model), _model, query
        )
        metrics.record_hit_at_k(1 if hits else 0)
        return _format_hits(hits)

    # =============================================================
    # 空召回重试：原问题必留一路，改写只在原句后追加名词
    # =============================================================
    _RETRY_SYNONYMS = (
        ("退", "退换货", "退货", "退款"),
        ("运费", "运费险", "邮费"),
        ("尺码", "胸围", "衣长", "腰围", "选码", "号型"),
        ("洗", "洗护", "机洗", "面料", "起球"),
        ("发货", "物流", "快递", "签收"),
        ("活动", "优惠", "满减", "促销", "双11"),
    )

    @staticmethod
    def _expand_query_for_retry(query: str) -> str | None:
        """只追加名词，不删改原句任何字符 —— 用户问题的意图不可能被改写。

        追加来源必须是已确定的事实：当前商品切片的品类/商品编号/尺码档位，
        以及服饰域同义名词表。不让 LLM 自由重写整句，也就没有改意图的可能。
        """
        extra: list[str] = []
        model = _kb_model_var.get()
        if model:
            try:
                import json as _json
                from pathlib import Path as _Path

                path = _Path(__file__).resolve().parents[2] / "config" / "product_slices.json"
                spec = (_json.loads(path.read_text(encoding="utf-8")) or {}).get(model) or {}
                for value in (spec.get("category"), spec.get("itemId"), spec.get("sizeRange")):
                    if value and str(value) not in query:
                        extra.append(str(value))
                title = str(spec.get("title") or "")
                for word in ("夹克", "毛衣", "针织", "西裤", "弯刀裤", "T恤", "POLO", "开衫"):
                    if word in title and word not in query:
                        extra.append(word)
            except Exception as e:
                logger.warning(f"重试补词读取商品切片失败（放行）: {e}")
        for group in PolicyService._RETRY_SYNONYMS:
            if any(word in query for word in group):
                for word in group:
                    if word not in query and word not in extra:
                        extra.append(word)
        if not extra:
            return None
        seen: set[str] = set()
        uniq: list[str] = []
        for word in extra:
            if word not in seen:
                seen.add(word)
                uniq.append(word)
        return (query + " " + " ".join(uniq[:8])).strip()

    @staticmethod
    def search_with_retry(query: str, top_k: int = 3) -> list[dict]:
        """召回为空时重试一次：原 query 与补词 query 一起做多路 RRF。

        原 query 永远作为第一路参与检索，补词只增加一条召回路径，
        因此「改写句跑偏」最多是无效，不可能覆盖或改变用户原意。
        """
        hits = PolicyService.search_policy(query, top_k=top_k)
        if hits or not settings.RAG_RETRY_ON_EMPTY:
            return hits
        expanded = PolicyService._expand_query_for_retry(query)
        if not expanded:
            return hits
        logger.info(
            f"policy 空召回重试: orig='{query[:60]}' expanded='{expanded[:100]}' "
            f"threshold_relax={settings.RETRIEVAL_THRESHOLD_RETRY_RELAX}"
        )
        try:
            return PolicyService.search_multi_policy(
                [query, expanded],
                top_k=top_k,
                threshold_relax=settings.RETRIEVAL_THRESHOLD_RETRY_RELAX,
            )
        except Exception as e:
            logger.warning(f"policy 重试失败（保留原结果）: {e}")
            return hits

    # =============================================================
    # A8：粗排（不带 rerank，给 fuse-first 模式用）
    # =============================================================
    @staticmethod
    def search_policy_coarse(
        query: str, top_k: int | None = None, threshold_relax: float = 0.0
    ) -> list[dict]:
        """粗排（不带 rerank，给 Phase 4 A8 fuse-first 用）。

        与 search_policy 的区别：
        - search_policy = 粗排 + rerank（单路精排）
        - search_policy_coarse = 仅粗排（多路融合后统一 rerank）

        共用 _coarse_retrieval，零逻辑漂移。

        Args:
            query: 用户问题
            top_k: 粗排候选数（默认 settings.RERANK_CANDIDATE_TOP_K = 15）

        Returns:
            schema 与 search_policy 一致；rerank_score=None（未走 rerank）
        """
        coarse_top_k = (
            top_k if top_k is not None else settings.RERANK_CANDIDATE_TOP_K
        )
        hits = PolicyService._coarse_retrieval(
            query, coarse_top_k, threshold_relax=threshold_relax
        )
        if not hits:
            metrics.record_hit_at_k(0)
            return []
        metrics.record_hit_at_k(1 if hits else 0)
        return _format_hits(hits)

    # =============================================================
    # A4 + A5 + A8: Multi-Query 多路检索
    # =============================================================
    @staticmethod
    def search_multi_policy(
        queries: List[str],
        top_k: int = 3,
        threshold_relax: float = 0.0,
    ) -> List[dict]:
        """
        多路检索（fuse-first rerank / per-query rerank / 单路短路）

        A5：默认并行（N 路 ~2.9x 加速）
        A8：默认 fuse-first（3×rerank → 1×rerank，LLM token -66%）

        路径决策（按优先级）：
        1. 单路（len=1）：直接 search_policy，不做 RRF 融合
        2. fuse-first（A8 flag=True + 多路）：
             每路 search_policy_coarse → RRF → 截断 15 → 1×rerank → top-k
        3. per-query rerank（A8 flag=False + 多路，A5 路径）：
             每路 search_policy → RRF 融合

        运行指标（每调用一行日志，便于 A5/A8 对比）：
            [multi_query_metrics] mode= queries= rerank_calls= latency_ms= ...

        Args:
            queries: 多路改写后的查询列表（≥ 1）
            top_k: 返回条数

        Returns:
            [{"text", "source", "score", "rerank_score", "rrf_score"}, ...]
            与 search_policy schema 一致（业务侧零改动消费）
            降级：queries 空 / 全部失败 → 返空 list
        """
        from app.services.rrf import rrf_fuse

        # A8 运行指标：起点计时 + rerank 调用计数
        start = time.perf_counter()
        rerank_call_count = 0
        mode = "unknown"

        if not queries:
            return []

        valid_queries: List[tuple] = [
            (idx, q) for idx, q in enumerate(queries) if q and q.strip()
        ]
        if not valid_queries:
            return []

        # =============================================================
        # 路径 1：单路短路（len=1）
        # =============================================================
        if len(valid_queries) == 1:
            mode = "single"
            result = PolicyService.search_policy(
                valid_queries[0][1], top_k=top_k, threshold_relax=threshold_relax
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                f"[multi_query_metrics] mode={mode} queries=1 "
                f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f}"
            )
            return result

        # =============================================================
        # 路径 2：Phase 4 A8 fuse-first rerank（默认）
        # =============================================================
        use_fuse_first = settings.MULTI_QUERY_FUSE_FIRST_RERANK and settings.USE_RERANK
        use_parallel = settings.MULTI_QUERY_PARALLEL

        if use_fuse_first:
            mode = "fuse_first"
            coarse_top_k = settings.RERANK_CANDIDATE_TOP_K
            per_query_hits: List[List[dict]] = []

            if use_parallel:
                # 并行粗排（per-request executor，with 块结束自动 shutdown）
                max_workers = max(
                    1, min(settings.MULTI_QUERY_WORKERS, len(valid_queries))
                )
                with ThreadPoolExecutor(
                    max_workers=max_workers,
                    thread_name_prefix="multi-policy",
                ) as ex:
                    futures = {
                        ex.submit(
                            PolicyService.search_policy_coarse,
                            q,
                            top_k=coarse_top_k,
                            threshold_relax=threshold_relax,
                        ): (idx, q)
                        for idx, q in valid_queries
                    }
                    results_by_idx: dict = {}
                    for future in as_completed(futures):
                        idx, q = futures[future]
                        try:
                            hits = future.result()
                            for h in hits:
                                h.setdefault("id", h.get("source", ""))
                            results_by_idx[idx] = hits
                        except Exception as e:
                            logger.warning(
                                f"search_multi_policy[A8] 第 {idx} 路失败 "
                                f"(q='{q[:30]}...'): {e}"
                            )
                            metrics.record_hit_at_k(0)
                    per_query_hits = [
                        results_by_idx[i] for i in sorted(results_by_idx.keys())
                    ]
            else:
                # 串行 fuse-first（debug 用）
                for idx, q in valid_queries:
                    try:
                        hits = PolicyService.search_policy_coarse(
                            q, top_k=coarse_top_k, threshold_relax=threshold_relax
                        )
                        for h in hits:
                            h.setdefault("id", h.get("source", ""))
                        per_query_hits.append(hits)
                    except Exception as e:
                        logger.warning(
                            f"search_multi_policy[A8 serial] 第 {idx} 路失败 "
                            f"(q='{q[:30]}...'): {e}"
                        )
                        metrics.record_hit_at_k(0)

            if not per_query_hits:
                elapsed_ms = (time.perf_counter() - start) * 1000
                logger.info(
                    f"[multi_query_metrics] mode={mode} queries={len(valid_queries)} "
                    f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f} "
                    f"result=empty"
                )
                return []

            # RRF 融合全局候选
            try:
                fused = rrf_fuse(
                    per_query_hits,
                    k=settings.RRF_K,
                    weights=settings.RAG_TYPE_BOOST,  # P3-3
                )
            except Exception as e:
                logger.warning(
                    f"search_multi_policy[A8] RRF 失败，降级到首路前 {top_k}: {e}"
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                logger.info(
                    f"[multi_query_metrics] mode={mode}_rrf_fail "
                    f"queries={len(valid_queries)} "
                    f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f} "
                    f"fallback=first"
                )
                return per_query_hits[0][:top_k]

            # 截断到 MAX_CANDIDATES_PER_CALL 后送 rerank（用 queries[0] 作评估 query）
            rerank_input = fused[:coarse_top_k]
            if len(rerank_input) > top_k:
                try:
                    from app.core.providers.rerank import get_rerank_provider
                    rerank_call_count = 1  # A8 关键：1 次 rerank 调用
                    reranked = get_rerank_provider().rerank(
                        valid_queries[0][1],  # 原始 query（语义最准）
                        rerank_input,
                        top_n=top_k,
                    )
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    logger.info(
                        f"[multi_query_metrics] mode={mode} queries={len(valid_queries)} "
                        f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f} "
                        f"fused={len(fused)} rerank_in={len(rerank_input)} → top{top_k}"
                    )
                    return reranked
                except Exception as e:
                    logger.warning(
                        f"search_multi_policy[A8] rerank 失败，降级到 RRF top-{top_k}: {e}"
                    )
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    logger.info(
                        f"[multi_query_metrics] mode={mode}_rerank_fail "
                        f"queries={len(valid_queries)} "
                        f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f} "
                        f"fused={len(fused)} fallback=rrf_topk"
                    )
                    return fused[:top_k]
            else:
                # 候选数 ≤ top_k，无需 rerank
                elapsed_ms = (time.perf_counter() - start) * 1000
                logger.info(
                    f"[multi_query_metrics] mode={mode}_no_rerank "
                    f"queries={len(valid_queries)} "
                    f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f} "
                    f"fused={len(fused)} → top{top_k}"
                )
                return fused[:top_k]

        # =============================================================
        # 路径 3：A5 per-query rerank（fuse_first=False 时回退）
        # =============================================================
        mode = "per_query"
        per_query_hits: List[List[dict]] = []
        total_hits = 0

        if use_parallel:
            max_workers = max(
                1, min(settings.MULTI_QUERY_WORKERS, len(valid_queries))
            )
            with ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="multi-policy",
            ) as ex:
                futures = {
                    ex.submit(
                        PolicyService.search_policy,
                        q,
                        top_k=top_k,
                        threshold_relax=threshold_relax,
                    ): (idx, q)
                    for idx, q in valid_queries
                }
                results_by_idx: dict = {}
                for future in as_completed(futures):
                    idx, q = futures[future]
                    try:
                        hits = future.result()
                        for h in hits:
                            h.setdefault("id", h.get("source", ""))
                        results_by_idx[idx] = hits
                        total_hits += len(hits)
                        # 估算 rerank 调用次数（search_policy 内部可能调 rerank）
                        if settings.USE_RERANK:
                            rerank_call_count += 1
                    except Exception as e:
                        logger.warning(
                            f"search_multi_policy[A5] 第 {idx} 路失败 "
                            f"(q='{q[:30]}...'): {e}"
                        )
                        metrics.record_hit_at_k(0)
                per_query_hits = [
                    results_by_idx[i] for i in sorted(results_by_idx.keys())
                ]
        else:
            for idx, q in valid_queries:
                try:
                    hits = PolicyService.search_policy(
                        q, top_k=top_k, threshold_relax=threshold_relax
                    )
                    for h in hits:
                        h.setdefault("id", h.get("source", ""))
                    per_query_hits.append(hits)
                    total_hits += len(hits)
                    if settings.USE_RERANK:
                        rerank_call_count += 1
                except Exception as e:
                    logger.warning(
                        f"search_multi_policy[A5 serial] 第 {idx} 路失败 "
                        f"(q='{q[:30]}...'): {e}"
                    )
                    metrics.record_hit_at_k(0)

        if not per_query_hits:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                f"[multi_query_metrics] mode={mode} queries={len(valid_queries)} "
                f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f} "
                f"result=empty"
            )
            return []

        try:
            fused = rrf_fuse(
                per_query_hits,
                k=settings.RRF_K,
                weights=settings.RAG_TYPE_BOOST,  # P3-3
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                f"[multi_query_metrics] mode={mode} queries={len(valid_queries)} "
                f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f} "
                f"hits_total={total_hits} fused={len(fused)} → top{top_k}"
            )
            return fused[:top_k]
        except Exception as e:
            logger.warning(f"search_multi_policy[A5] RRF 失败，降级到首路前 {top_k}: {e}")
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                f"[multi_query_metrics] mode={mode}_rrf_fail queries={len(valid_queries)} "
                f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f} "
                f"fallback=first"
            )
            return per_query_hits[0][:top_k]

    # =============================================================
    # 通过 KnowledgeSource Protocol 检索（spec §3.4 #5 验证点）
    # =============================================================
    @staticmethod
    def search_policy_via_protocol(query: str, top_k: int = 3) -> list[dict]:
        """通过 KnowledgeSource Protocol 检索（同步包装 async）。

        与 search_policy 的区别：
        - search_policy: 直接调 vector_search + BM25/RRF/rerank（含 A4/A5/A8 全部能力）
        - search_policy_via_protocol: 走 KnowledgeSource Protocol 接口
    （简化版，仅 dense + 可选 hybrid；rerank 由调用方决定）

        用途：
        - 接入方自定义 KnowledgeSource 时走此入口（业务不感知底层）
        - spec §3.4 #5 验证 PolicyService 已通过 Protocol 解耦（mock 替换测试用）

        Returns:
            与 search_policy schema 一致：
            [{"text", "source", "score", "rerank_score", "rrf_score"}, ...]
        """
        import asyncio

        if not query or not query.strip():
            return []

        ks = _get_knowledge_source()
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 已在事件循环内（M14 orchestrator 异步路径）；用 run_coroutine_threadsafe
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    future = ex.submit(
                        asyncio.run, ks.search(query, top_k=top_k)
                    )
                    results = future.result(timeout=10)
            else:
                results = loop.run_until_complete(ks.search(query, top_k=top_k))
        except RuntimeError:
            # 没事件循环（pytest 同步场景）；直接 asyncio.run
            results = asyncio.run(ks.search(query, top_k=top_k))

        # SearchResult → 兼容 schema（供 orchestrator/synthesizer 无改动消费）
        return [
            {
                "text": r.content,
                "source": r.metadata.get("source", ""),
                "score": r.score,
                "rerank_score": r.metadata.get("rerank_score"),
                "rrf_score": r.metadata.get("rrf_score"),
            }
            for r in results
        ]