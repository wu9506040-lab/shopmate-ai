"""VectorKnowledgeSource（KnowledgeSource 默认实现）

行为兼容 PolicyService.search_policy：
- embed → 向量库 search（粗排 top-K）
- 可选 hybrid: BM25 + RRF（复用 services/rag/ 现有能力，不调整）
- 返回 List[SearchResult]

过滤 filters：允许传 dict（映射到向量库 score_threshold 等基础过滤）。

检索指标埋点：record_retrieve_hits / record_hit_at_k。
"""
import logging
from typing import List, Optional

from app.clients.vector_db import COLLECTION_NAME, get_client, search as vector_search
from app.core.config import settings
from app.core.providers.embedding import get_embedding_provider
from app.schemas.knowledge import Document, SearchResult
from app.services.metrics import metrics

logger = logging.getLogger(__name__)


class VectorKnowledgeSource:
    """Vector + 可选 BM25 混合检索（默认 KnowledgeSource 实现）"""

    source_type = "vector"

    def __init__(self, collection_name: Optional[str] = None):
        self.collection_name = collection_name or COLLECTION_NAME

    # =============================================================
    # 核心：search
    # =============================================================
    async def search(
        self,
        query: str,
        top_k: int = 5,
        filters: Optional[dict] = None,
    ) -> List[SearchResult]:
        """关键词/语义检索 → 召回 top_k。

        filters（dict）：
            - score_threshold: float（最低相似度阈值，覆盖 settings 默认）
            - coarse_top_k: int（粗排候选数，默认 settings.RERANK_CANDIDATE_TOP_K=15）
            - category: str（Vector payload 过滤，V3+ 用；MVP 占位）
            - use_hybrid: bool（覆盖 settings.USE_HYBRID_BM25；测试用 False 避免 BM25 干扰）
        """
        if not query or not query.strip():
            return []

        # 默认阈值同样按意图分档（与 PolicyService 检索口径保持一致）；
        # 调用方显式传 filters["score_threshold"] 时以调用方为准
        if filters and "score_threshold" in filters:
            score_threshold = float(filters["score_threshold"])
        else:
            from app.services.retrieval_threshold import resolve as resolve_threshold

            score_threshold = resolve_threshold(query)[0]
        coarse_top_k = (
            int(filters["coarse_top_k"])
            if filters and "coarse_top_k" in filters
            else settings.RERANK_CANDIDATE_TOP_K
        )
        use_hybrid = (
            bool(filters["use_hybrid"])
            if filters and "use_hybrid" in filters
            else settings.USE_HYBRID_BM25
        )

        # 1. embed
        try:
            query_vec = get_embedding_provider().embed_text(query)
        except Exception as e:
            logger.warning(f"VectorKnowledgeSource.embed 失败: {e}")
            return []

        # 2. vector top-K 粗排
        try:
            hits = vector_search(
                query_vector=query_vec,
                top_k=coarse_top_k,
                score_threshold=score_threshold,
                collection_name=self.collection_name,
            )
            metrics.record_retrieve_hits(len(hits))
        except Exception as e:
            logger.warning(f"VectorKnowledgeSource.vector 检索失败: {e}")
            return []

        # 3. hybrid: vector + BM25 + RRF（可选，复用现有 services/rag/）
        if use_hybrid:
            try:
                from app.services.bm25_index import bm25_search
                from app.services.rrf import rrf_fuse

                bm25_hits = bm25_search(query, top_k=settings.BM25_TOP_K)
                if bm25_hits:
                    fused = rrf_fuse(
                        [hits, bm25_hits],
                        k=settings.RRF_K,
                        weights=settings.RAG_TYPE_BOOST,
                    )
                    hits = fused[:coarse_top_k]
            except Exception as e:
                logger.warning(f"hybrid 检索失败，降级到纯 vector: {e}")

        # 4. 截断到 top_k + 转 SearchResult
        metrics.record_hit_at_k(1 if hits else 0)
        top_hits = hits[:top_k]
        return [_hit_to_search_result(h) for h in top_hits if h]

    # =============================================================
    # get_document（MVP 占位：Vector 不直接暴露全文，按 doc_id 检索）
    # =============================================================
    async def get_document(self, doc_id: str) -> Optional[Document]:
        """按 doc_id 取完整文档（MVP：从 Vector 命中 top-1 反查）

        完整实现依赖 MySQL meta 表（与 ingest 路径对齐）；
    只交付协议 + 占位，避免越界。
        """
        try:
            client = get_client()  # : 统一入口兼容客户端（Vector/Milvus 同形）
            records = client.retrieve(
                collection_name=self.collection_name,
                ids=[doc_id],
                with_payload=True,
            )
            if not records:
                return None
            payload = records[0].payload or {}
            return Document(
                doc_id=str(records[0].id),
                title=payload.get("title") or payload.get("source") or "",
                content=payload.get("text") or payload.get("content") or "",
                category=payload.get("category"),
                metadata={k: v for k, v in payload.items()
                         if k not in ("text", "content", "title", "category")},
            )
        except Exception as e:
            logger.warning(f"VectorKnowledgeSource.get_document 失败: {e}")
            return None

    # =============================================================
    # upsert / delete（MVP 占位：留给 ingest 路径实现；这里只签名）
    # =============================================================
    async def upsert(self, document: Document) -> str:
        """新增/更新文档 → 返 doc_id（MVP：直接抛 NotImplementedError）

        完整实现需走 embed + vector.upsert_points（与 services/rag/ingest.py 对齐）；
    只交付协议签名。
        """
        raise NotImplementedError(
    "VectorKnowledgeSource.upsert: MVP 仅交付协议签名。"
            "完整 ingest 流程走 services/rag/ingest.py（M13 已实现）。"
        )

    async def delete(self, doc_id: str) -> bool:
        """删除文档 → 返是否成功（MVP：直接抛 NotImplementedError）"""
        raise NotImplementedError(
    "VectorKnowledgeSource.delete: MVP 仅交付协议签名。"
            "完整删除流程走 services/rag/ingest.py（M13 已实现）。"
        )


# =============================================================
# 辅助：Vector hit dict → SearchResult
# =============================================================
def _hit_to_search_result(hit: dict) -> SearchResult:
    """统一 Vector hit → SearchResult（payload 嵌套 + 直挂两种形态）"""
    payload = hit.get("payload") or {}
    content = (
        payload.get("text") or payload.get("content")
        or hit.get("text") or hit.get("content") or ""
    )
    doc_id = str(hit.get("id") or hit.get("source") or "")
    return SearchResult(
        doc_id=doc_id,
        content=content,
        score=float(hit.get("score") or 0.0),
        metadata={
            "source": payload.get("source") or hit.get("source") or "",
            "doc_type": payload.get("doc_type") or hit.get("doc_type"),
            "rerank_score": hit.get("rerank_score"),
            "rrf_score": hit.get("rrf_score"),
        },
    )