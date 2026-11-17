"""
test_ingest_rollback_integration.py — P1-3 L2 集成测试：FakeVector 验证数据状态

按 SOP-V1 §2.2 数据可信验证：L2 必须用真 DB 集成（不是 MagicMock），
本测试用 FakeVector（dict 存储 + delete_calls tracking）模拟 Vector 真行为，
用真实 embedding mock（避免调 DashScope）+ patch upsert_knowledge_meta 模拟失败。

验证维度（不是函数调用序列，而是数据状态）：
- FakeVector.points 真存点 → 真删点
- delete_calls 真记录被删的 chunk_ids
- ingest_text 返回值结构正确

L1 mock 测试在 test_ingest_rollback.py；本文件是 L2 状态断言。
"""
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from app.services.rag import ingest


class FakeVector:
    """内存 Vector 模拟：支持 upsert / delete / count + 调用追踪

    设计目标：
    - 真存点（dict）→ L2 能验证 rollback 真删了点（不仅是函数调用）
    - delete_calls 记录被删的 id 列表 → 验证 rollback 删的是正确的 chunk_id
    - upsert_calls 记录写入的 id 列表 → 验证 rollback 触发的源头是哪些点
    """

    def __init__(self) -> None:
        self.points: Dict[str, Dict[str, Any]] = {}
        self.upsert_calls: List[List[str]] = []
        self.delete_calls: List[List[str]] = []

    def upsert(self, collection_name: str, points: List, **kwargs) -> Dict[str, Any]:
        ids: List[str] = []
        for p in points:
            # Milvus 迁移后写入点是 dict；旧代码用 PointStruct 对象，两种都兼容
            if isinstance(p, dict):
                pid, payload = p.get("id"), p.get("payload")
            else:
                pid, payload = p.id, p.payload
            self.points[str(pid)] = dict(payload or {})
            ids.append(str(pid))
        self.upsert_calls.append(ids)
        return {"status": "completed", "count": len(ids)}

    def delete(self, collection_name: str, points_selector: Any, **kwargs) -> Dict[str, Any]:
        # vector_client 接受 list[int|str] 或 PointIdsSelector；本测试只走 list 路径
        if isinstance(points_selector, list):
            for pid in points_selector:
                self.points.pop(str(pid), None)
            self.delete_calls.append([str(p) for p in points_selector])
            return {"status": "completed"}
        raise NotImplementedError(f"FakeVector 仅支持 list selector，收到 {type(points_selector)}")

    def count(self) -> int:
        return len(self.points)


@pytest.fixture
def fake_vector():
    """注入 FakeVector 实例：替换 ingest.upsert_points + ingest.delete_points

    用 wrapper 函数对齐真实签名：
    - upsert_points(points, collection_name=None) -> int
    - delete_points(point_ids, collection_name=None) -> int
    FakeVector.upsert/delete 接 (collection_name, points, **kwargs)，
    wrapper 在中间转换参数顺序。
    """
    fq = FakeVector()

    def _upsert_wrapper(points, collection_name=None):
        fq.upsert(collection_name or "fake", points)
        return len(points)

    def _delete_wrapper(point_ids, collection_name=None):
        return fq.delete(collection_name or "fake", point_ids)

    with patch.object(ingest, "ensure_collection", return_value=True), \
         patch.object(ingest, "upsert_points", side_effect=_upsert_wrapper), \
         patch.object(ingest, "delete_points", side_effect=_delete_wrapper):
        yield fq


@pytest.fixture
def mock_embed():
    """mock embedding provider：返 N 个 1024 维零向量（与 Vector VECTOR_SIZE 对齐）"""
    provider = MagicMock()
    provider.embed_texts.side_effect = lambda texts: [[0.0] * 1024 for _ in texts]
    with patch.object(ingest, "get_embedding_provider", return_value=provider):
        yield provider


def _text_for_3_chunks() -> str:
    """1200 字符差异化文本 → chunk_size=500/overlap=50 → 切 3 片（每片内容不同）

    注：必须用差异化文本！P1-1 让内容相同的 chunk 共享同一 ID（即使下标不同），
    纯 "a" 文本会导致 chunk 1 和 chunk 2 ID 相同，dict 写入时互相覆盖。
    """
    return (
        "退换货政策：7 天无理由退货，需保持商品完好。" * 30   # ~810 字符
        + "运费险：7 天内退货可申请运费险理赔。" * 5            # ~100 字符
        + "质量问题 15 天内可换货，需提供购物凭证。" * 10       # ~310 字符
        # 总 ~1220 字符 → 切 3 片（每片有差异内容，3 个 ID 不同）
    )


# =============================================================
# Case 1: rollback 路径 → FakeVector 真有点 → rollback 后真清空
# =============================================================
def test_rollback_clears_fake_vector_points(fake_vector, mock_embed, monkeypatch):
    """MySQL 失败 → rollback 真删 FakeVector 里的 3 个点（数据状态断言）

    验证维度（不仅函数被调，而是真实数据被改）：
    - upsert_calls 记录 3 个 id（写入证据）
    - delete_calls 记录 3 个 id（rollback 证据）
    - 最终 fq.points 清空（count == 0，关键：rollback 真删了点）
    """
    monkeypatch.setattr(ingest.settings, "RAG_ROLLBACK_ON_MYSQL_FAIL", True)

    with patch.object(ingest, "upsert_knowledge_meta", return_value=None):
        result = ingest.ingest_text(_text_for_3_chunks(), source="integration_case_1")

    # ✅ 数据状态断言 1：upsert 真写入 3 个点
    assert len(fake_vector.upsert_calls) == 1
    assert len(fake_vector.upsert_calls[0]) == 3

    # ✅ 数据状态断言 2：rollback 真触发 delete
    assert len(fake_vector.delete_calls) == 1
    assert len(fake_vector.delete_calls[0]) == 3

    # ✅ 数据状态断言 3：rollback 后 FakeVector 真清空（关键证据）
    assert fake_vector.count() == 0

    # ✅ 返回值结构正确
    assert result["ingested_chunks"] == 3
    assert len(result["chunk_ids"]) == 3


# =============================================================
# Case 2: rollback 关闭 → FakeVector 保留点（保留脏点行为）
# =============================================================
def test_rollback_off_keeps_orphan_points(fake_vector, mock_embed, monkeypatch):
    """开关关闭 → rollback 不触发 → FakeVector 保留 3 个点（验证原 M14 V3 前行为）"""
    monkeypatch.setattr(ingest.settings, "RAG_ROLLBACK_ON_MYSQL_FAIL", False)

    with patch.object(ingest, "upsert_knowledge_meta", return_value=None):
        ingest.ingest_text(_text_for_3_chunks(), source="integration_case_2")

    assert len(fake_vector.delete_calls) == 0  # ✅ 不调 delete
    assert fake_vector.count() == 3  # ✅ 3 个点保留（脏点行为，符合 M14 V3 前）


# =============================================================
# Case 3: 多次 ingest → 每次失败都 rollback → Vector 状态干净
# =============================================================
def test_repeated_rollback_keeps_state_clean(fake_vector, mock_embed, monkeypatch):
    """连跑 3 次失败 ingest → 每次都 rollback → Vector 始终干净（幂等）"""
    monkeypatch.setattr(ingest.settings, "RAG_ROLLBACK_ON_MYSQL_FAIL", True)

    for i in range(3):
        with patch.object(ingest, "upsert_knowledge_meta", return_value=None):
            ingest.ingest_text(_text_for_3_chunks(), source=f"repeat_case_{i}")
        assert fake_vector.count() == 0, f"第 {i + 1} 次 ingest 后 Vector 应为空"

    assert len(fake_vector.upsert_calls) == 3
    assert len(fake_vector.delete_calls) == 3


# =============================================================
# Case 4: rollback delete 失败 → 不掩盖原 MySQL 错误（不抛）
# =============================================================
def test_rollback_failure_does_not_propagate(fake_vector, mock_embed, monkeypatch):
    """delete_points 内部抛异常 → FakeVector 删不干净 → ingest_text 仍正常返回（不掩盖）"""
    monkeypatch.setattr(ingest.settings, "RAG_ROLLBACK_ON_MYSQL_FAIL", True)

    def _broken_delete(collection_name, points_selector, **kwargs):
        # 模拟 Vector delete 失败：抛异常 + FakeVector 状态保留（不退点）
        raise RuntimeError("fake vector delete failed")

    with patch.object(ingest, "delete_points", side_effect=_broken_delete), \
         patch.object(ingest, "upsert_knowledge_meta", return_value=None):
        # ✅ 关键断言：rollback 失败时 ingest_text 不重新抛（让原 MySQL 失败信号透出）
        result = ingest.ingest_text(_text_for_3_chunks(), source="integration_case_4")

    assert result["ingested_chunks"] == 3
    # 注：本测试中 fake_vector.delete_calls 不会更新，因为 side_effect 替换了 fake_vector.delete
    # 但 FakeVector.points 也不变（因为 delete 抛异常前没改 state）
    # 真实场景中 Vector 客户端会留脏点，需要 sweep_orphan_vector.py 兜底（后续 P1-1/P1-2 加）