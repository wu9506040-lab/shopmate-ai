"""知识源单例（FastAPI Depends 注入）。"""
from functools import lru_cache

from app.rag.protocols import KnowledgeSource
from app.rag.vector_impl import VectorKnowledgeSource


@lru_cache(maxsize=1)
def get_knowledge_source() -> KnowledgeSource:
    """获取全局知识源（单例）。"""
    return VectorKnowledgeSource()


def reset_knowledge_source() -> None:
    """测试用：清 lru_cache 单例。"""
    get_knowledge_source.cache_clear()
