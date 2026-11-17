"""
Redis 8 向量后端回归测试（真 Redis 容器，独立前缀 kbtest: 不污染生产 key）。

覆盖 vector_db 对外全部 10 个 API 在 redis 后端下的语义对齐：
- COSINE 距离语义（score 越小越像，threshold 剔除）
- channel 过滤检索
- 按 source 删除（含路径特殊字符 TAG 转义）
- 按 doc_key 精确读取（policy_service 切片路径）
- scroll 分页 / retrieve / get_collection_info / health_check
"""
import os
import sys
import uuid

import pytest

sys.stdout.reconfigure(encoding="utf-8")

# 必须在 import vector_db 之前改（settings 属性运行时读取，改实例即可）
from app.core.config import settings

settings.VECTOR_BACKEND = "redis"
settings.VECTOR_REDIS_PREFIX = "kbtest:"

from app.clients.vector_db import (
    COLLECTION_NAME,
    ensure_collection,
    upsert_points,
    search,
    delete_points,
    delete_source_points,
    get_collection_info,
    health_check,
    get_client,
)
from app.clients.vector_db import _redis, _redis_index_name

DIM = 1024

# 三个语义明确的方向向量 + 一个路径型 source（TAG 转义必须活下来）
VEC_A = [0.0] * DIM
VEC_A[0] = 1.0
VEC_B = [0.0] * DIM
VEC_B[1] = 1.0
VEC_QUERY_LIKE_A = [0.0] * DIM
VEC_QUERY_LIKE_A[0] = 0.9
VEC_QUERY_LIKE_A[2] = 0.1

POINTS = [
    {"id": "t1", "vector": VEC_A, "payload": {
        "text": "七天无理由退货政策", "channel": "tmall",
        "source": "docs/退货政策_v2.pdf", "page": 3, "section": "售后",
        "title": "退货", "doc_type": "policy", "doc_key": "slice:SKU001",
        "category": "售后",
    }},
    {"id": "t2", "vector": VEC_B, "payload": {
        "text": "运费险说明", "channel": "douyin",
        "source": "docs/运费险.pdf", "page": 1, "section": "物流",
        "title": "运费", "doc_type": "policy", "category": "物流",
    }},
    {"id": "t3", "vector": VEC_A, "payload": {
        "text": "同款政策条款", "channel": "tmall",
        "source": "docs/退货政策_v2.pdf", "page": 4, "section": "售后",
        "title": "退货", "doc_type": "policy", "category": "售后",
    }},
]


@pytest.fixture(scope="module", autouse=True)
def _setup():
    # 幂等清场：删旧索引与残留 key
    conn = _redis()
    try:
        conn.execute_command("FT.DROPINDEX", _redis_index_name(COLLECTION_NAME))
    except Exception:
        pass
    for k in conn.scan_iter(match="kbtest:*"):
        conn.delete(k)

    ensure_collection(COLLECTION_NAME, DIM)
    upsert_points(POINTS, COLLECTION_NAME)
    yield
    # 收尾清场
    try:
        conn.execute_command("FT.DROPINDEX", _redis_index_name(COLLECTION_NAME))
    except Exception:
        pass
    for k in conn.scan_iter(match="kbtest:*"):
        conn.delete(k)


# ---------------------------------------------------------------
def test_upsert_count():
    info = get_collection_info(COLLECTION_NAME)
    assert info["points_count"] == 3
    assert info["vector_size"] == DIM


def test_search_top1_semantics():
    """与 A 同方向的 query → top1 是 A 类向量，score 是相似度且远高于 B。"""
    hits = search(VEC_QUERY_LIKE_A, top_k=3, collection_name=COLLECTION_NAME)
    assert len(hits) == 3
    assert hits[0]["id"] in ("t1", "t3")
    assert hits[0]["score"] > 0.9  # 相似度语义：越大越像（实测对齐 Milvus）
    assert hits[0]["payload"]["channel"] == "tmall"
    assert "text" in hits[0]["payload"]


def test_search_channel_filter():
    """channel 过滤：douyin 渠道里只能搜到 t2。"""
    hits = search(VEC_QUERY_LIKE_A, top_k=5, collection_name=COLLECTION_NAME,
                  channel="douyin")
    assert [h["id"] for h in hits] == ["t2"]


def test_search_score_threshold():
    """threshold 剔除：相似度线 0.5 卡掉正交的 t2（sim≈0），保留 t1/t3。"""
    hits = search(VEC_QUERY_LIKE_A, top_k=5, collection_name=COLLECTION_NAME,
                  score_threshold=0.5)
    assert {h["id"] for h in hits} == {"t1", "t3"}


def test_page_payload_type():
    """page 字段 Milvus 端是 int64，Redis 端读回应还原 int。"""
    client = get_client()
    recs = client.retrieve(COLLECTION_NAME, ["t1"])
    assert recs and recs[0].payload["page"] == 3


def test_delete_points():
    delete_points(["t3"], COLLECTION_NAME)
    hits = search(VEC_QUERY_LIKE_A, top_k=5, collection_name=COLLECTION_NAME)
    assert "t3" not in {h["id"] for h in hits}
    upsert_points([POINTS[2]], COLLECTION_NAME)  # 还原，给后续测试用


def test_delete_source_with_path_chars():
    """delete-before-write：source 含路径特殊字符（/ _ .）必须精确命中。"""
    deleted = delete_source_points("docs/退货政策_v2.pdf", COLLECTION_NAME)
    assert deleted == 2  # t1 + t3
    ids = {h["id"] for h in search(VEC_QUERY_LIKE_A, top_k=5,
                                   collection_name=COLLECTION_NAME)}
    assert ids == {"t2"}
    upsert_points(POINTS, COLLECTION_NAME)  # 还原


def test_query_by_doc_key():
    """policy_service 切片路径：doc_key 精确读取。"""
    rows = get_client().query(
        collection_name=COLLECTION_NAME,
        filter='doc_key == "slice:SKU001"',
        limit=12,
    )
    assert {r["id"] for r in rows} == {"t1"}


def test_scroll_pagination():
    client = get_client()
    page1, nxt = client.scroll(COLLECTION_NAME, limit=2, offset=0)
    assert len(page1) == 2 and nxt == 2
    page2, nxt2 = client.scroll(COLLECTION_NAME, limit=2, offset=nxt)
    assert len(page2) == 1 and nxt2 is None
    all_ids = {r.id for r in page1} | {r.id for r in page2}
    assert all_ids == {"t1", "t2", "t3"}
    assert all(not hasattr(r, "vector") or r.vector is None for r in page1)


def test_health_check():
    h = health_check()
    assert h["ok"] is True
    assert h["points_count"] == 3


# ---------------------------------------------------------------
# 变异验证（手工执行，不进 CI）：确认测试真的锁得住行为
# ---------------------------------------------------------------
# 变异 1：_tag_escape 换成恒等函数（不转义）→ test_delete_source_with_path_chars
#         必转红（source 被 TAG 分隔符切开，删不到）
# 变异 2：_redis_search 里 sort_by("score") 删掉 → test_search_top1_semantics
#         的 top1 断言可能翻车（无序时 t2 可能排前）
# 变异 3：相似度换算删掉（直接用 Redis 距离当 score）→ test_search_top1_semantics
#         的 score>0.9 与 test_search_score_threshold 双红（语义反转）
# 验证方式：临时改代码 → 跑对应测试确认变红 → 还原。已验证通过。
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
