"""知识运营闭环单测（假 session，不连库、不调检索）

锁的是**判据**，不是 SQL：
- 四类根因的分界在哪、每一类的证据能不能自证
- 检索失败不能被判成"知识缺失"（这是最容易埋的坑）
- 聚合按知识源分组，主 action 取占比最高的一类
SQL 正确性由 `scripts/kb_ops.py` 在真库上跑验。

为什么不用真库：tests/conftest.py 会把 DATABASE_URL 设成占位 host，
真库测试在 full suite 里必然失败（L3 那轮踩过，见 BUGS 47 相关记录）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")

from app.services.kb_ops import diagnose as dg  # noqa: E402
from app.services.kb_ops.report import source_report  # noqa: E402


class _Result:
    def __init__(self, first=None, rows=None, rowcount=0):
        self._first = first
        self._rows = rows or []
        self.rowcount = rowcount

    def first(self):
        return self._first

    def fetchall(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._first


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.calls = []
        self.committed = 0

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params))
        return self._results.pop(0) if self._results else _Result()

    def commit(self):
        self.committed += 1


# ---------------------------------------------------------------
# classify：四类根因的边界
# ---------------------------------------------------------------
def test_no_expected_source_is_unknown():
    cause, ev = dg.classify(expected_source=None)
    assert cause == dg.ROOT_UNKNOWN
    assert "待标注" in ev["reason"]


def test_retrieve_failed_never_becomes_no_kb():
    """检索挂了 ≠ 知识库没有。判成 no_kb 会让运营去补一堆已存在的卡。"""
    cause, ev = dg.classify(
        expected_source="return_policy",
        retrieved_sources=None,
        kb_has_source=False,          # 连库也查不到，很容易误判
        retrieve_failed=True,
    )
    assert cause == dg.ROOT_UNKNOWN
    assert ev["retrieve_failed"] is True
    assert cause != dg.ROOT_NO_KB


def test_miss_and_no_doc_is_no_kb():
    cause, ev = dg.classify(
        expected_source="return_policy",
        retrieved_sources=["size_guide", "shipping"],
        kb_has_source=False,
    )
    assert cause == dg.ROOT_NO_KB
    assert ev["rank"] is None
    assert "没有" in ev["reason"]


def test_miss_but_doc_exists_is_retrieval_miss():
    cause, ev = dg.classify(
        expected_source="return_policy",
        retrieved_sources=["size_guide"],
        kb_has_source=True,
    )
    assert cause == dg.ROOT_RETRIEVAL_MISS
    assert ev["kb_has_source"] is True


def test_hit_but_conflict_is_kb_stale():
    cause, ev = dg.classify(
        expected_source="return_policy",
        retrieved_sources=["return_policy"],
        kb_has_source=True,
        dims={"grounding": 4, "consistency": 2},
    )
    assert cause == dg.ROOT_KB_STALE
    assert ev["rank"] == 1
    assert "consistency=2" in ev["reason"]


def test_hit_but_low_grounding_is_generation():
    cause, ev = dg.classify(
        expected_source="return_policy",
        retrieved_sources=["x", "return_policy"],
        kb_has_source=True,
        dims={"grounding": 2, "consistency": 4},
    )
    assert cause == dg.ROOT_GENERATION
    assert ev["rank"] == 2


def test_both_dims_low_prefers_generation():
    """consistency 和 grounding 都差：先怀疑模型没依据，而不是资料过期。

    资料过期时模型至少有东西可依（grounding 不会低），
    两个都低说明它没在看资料，归生成侧检修更省事。
    """
    cause, _ = dg.classify(
        expected_source="p",
        retrieved_sources=["p"],
        kb_has_source=True,
        dims={"grounding": 1, "consistency": 1},
    )
    assert cause == dg.ROOT_GENERATION


def test_hit_at_tail_is_ranking_weak():
    cause, ev = dg.classify(
        expected_source="p",
        retrieved_sources=["a", "b", "c", "p", "e"],
        kb_has_source=True,
        dims={"grounding": 4, "consistency": 4},
    )
    assert cause == dg.ROOT_RANKING_WEAK
    assert ev["rank"] == 4


def test_hit_top_and_healthy_is_generation():
    cause, ev = dg.classify(
        expected_source="p",
        retrieved_sources=["p", "b"],
        kb_has_source=True,
        dims={"grounding": 5, "consistency": 5},
    )
    assert cause == dg.ROOT_GENERATION
    assert ev["rank"] == 1


def test_hit_beyond_top_k_counts_as_miss():
    """第 6 位命中不算 top-5 命中——判据要和运营口头说的"没搜到"一致。"""
    cause, ev = dg.classify(
        expected_source="p",
        retrieved_sources=["a", "b", "c", "d", "e", "p"],
        kb_has_source=True,
        dims={"grounding": 4, "consistency": 4},
        top_k=5,
    )
    assert cause == dg.ROOT_RETRIEVAL_MISS
    assert ev["rank"] is None


def test_evidence_is_json_serializable():
    import json

    _, ev = dg.classify(
        expected_source="p",
        retrieved_sources=["p"],
        kb_has_source=True,
        dims={"grounding": 3.5, "consistency": 4},
    )
    json.dumps(ev, ensure_ascii=False)  # 不抛就行：要直接写 JSON 列


# ---------------------------------------------------------------
# run_batch：跳过没标注的，回写带证据
# ---------------------------------------------------------------
def test_answer_saying_no_data_is_context_missing():
    """实盘例子：qa#2392「我要换货」答"您当前没有订单"。

    这是订单上下文没拿到，跟知识库无关。判成 generation 会让人去改 prompt，
    判成 no_kb 会让人去补卡——两条路都是白费功夫。
    """
    cause, ev = dg.classify(
        expected_source=None,            # 连标注都没有
        answer="您当前没有订单，暂时无法处理哦～",
        failure_layer="generation",
    )
    assert cause == dg.ROOT_CONTEXT_MISSING
    assert "非知识问题" in ev["reason"]


def test_normal_policy_wording_is_not_context_failure():
    """别把政策里的正常表述误伤成"没查到数据"。"""
    assert not dg.looks_like_context_failure(
        "没有订单号也能申请退货，提供下单手机号即可"
    )
    assert dg.looks_like_context_failure("未查询到您的订单记录")


def test_context_missing_skips_retrieval(monkeypatch):
    """已经明确是上下文失败就别花一次检索。"""

    def _boom(*a, **kw):
        raise AssertionError("上下文失败不该触发检索")

    monkeypatch.setattr(dg, "retrieve_sources", _boom)
    res = dg.diagnose_one(
        _FakeDB([]), query="我要换货", expected_source=None,
        answer="您当前没有订单", failure_layer="generation",
    )
    assert res["root_cause"] == dg.ROOT_CONTEXT_MISSING


def test_run_batch_keeps_context_case_without_label(monkeypatch):
    """没标注但答案是"没查到数据"：要落 context_missing，不是 skip。"""
    db = _FakeDB([
        _Result(rows=[(11, "我要换货", {"grounding": 3}, "您当前没有订单", "generation")]),
        _Result(rows=[]),   # 没有任何 expected_source
        _Result(rowcount=1),
    ])
    res = dg.run_batch(db, limit=1)

    assert res["updated"] == 1
    assert res["skipped"] == 0
    assert res["by_cause"] == {dg.ROOT_CONTEXT_MISSING: 1}


def test_load_candidates_only_takes_knowledge_side_failures():
    """blocked 是安全拦截、intent_router 是理解错，都不该进知识清单。

    intent_router 判错时检索必然搜不到，判成 no_kb 会让人去补一堆本该
    改同义词/意图规则的卡。这条锁的是筛选口径，不是 SQL 语法。
    """
    db = _FakeDB([_Result(rows=[])])
    dg._load_candidates(db, 10)
    sql = db.calls[0][0]

    assert "verdict='low'" in sql
    assert "retrieval" in sql and "generation" in sql
    assert "verdict<>'pass'" not in sql      # 放宽成<>\'pass\' 会把 blocked 带进来


def test_run_batch_skips_unlabeled(monkeypatch):
    db = _FakeDB([
        _Result(rows=[(1, "退货运费谁出", {"grounding": 4}, "答得不好", "generation"),
                  (2, "没标注的问题", None, "也答得不好", None)]),
        _Result(rows=[]),  # expected_source 查不到任何一条
    ])

    def _boom(*a, **kw):  # 不该走到检索
        raise AssertionError("没标注的记录不该触发检索")

    monkeypatch.setattr(dg, "diagnose_one", _boom)
    res = dg.run_batch(db, limit=10)

    assert res["scanned"] == 2
    assert res["skipped"] == 2
    assert res["updated"] == 0
    assert res["pending_review"] == 2


def test_run_batch_writes_cause_and_evidence(monkeypatch):
    from app.services.badcase_service import query_hash

    q = "退货运费谁出"
    db = _FakeDB([
        _Result(rows=[(7, q, {"grounding": 4, "consistency": 4}, "答得不好", "generation")]),
        _Result(rows=[(query_hash(q), "fashion_return_policy")]),
        _Result(rowcount=1),
    ])
    monkeypatch.setattr(
        dg, "diagnose_one",
        lambda *a, **kw: {"root_cause": dg.ROOT_NO_KB,
                          "evidence": {"expected_source": "fashion_return_policy",
                                       "reason": "库里没有"}},
    )

    res = dg.run_batch(db, limit=1)

    assert res["updated"] == 1
    assert res["by_cause"] == {dg.ROOT_NO_KB: 1}
    update_sql, params = db.calls[-1]
    assert "UPDATE qa_results SET root_cause" in update_sql
    assert params["c"] == dg.ROOT_NO_KB
    assert "fashion_return_policy" in params["e"]
    assert db.committed == 1


def test_run_batch_dry_run_does_not_write(monkeypatch):
    from app.services.badcase_service import query_hash

    q = "怎么换货"
    db = _FakeDB([
        _Result(rows=[(9, q, None, "答得不好", "retrieval")]),
        _Result(rows=[(query_hash(q), "exchange_policy")]),
    ])
    monkeypatch.setattr(
        dg, "diagnose_one",
        lambda *a, **kw: {"root_cause": dg.ROOT_RETRIEVAL_MISS, "evidence": {}},
    )

    res = dg.run_batch(db, limit=1, dry_run=True)

    assert res["dry_run"] is True
    assert res["updated"] == 0
    assert db.committed == 0
    assert all("UPDATE" not in sql for sql, _ in db.calls)


# ---------------------------------------------------------------
# report：按知识源聚合
# ---------------------------------------------------------------
def _qa_rows():
    return [
        (dg.ROOT_NO_KB, {"expected_source": "return_policy", "query": "退货运费谁出",
                         "kb_has_source": False}),
        (dg.ROOT_NO_KB, {"expected_source": "return_policy", "query": "退货谁付邮费",
                         "kb_has_source": False}),
        (dg.ROOT_RETRIEVAL_MISS, {"expected_source": "return_policy", "query": "七天无理由怎么算",
                                  "kb_has_source": True}),
        (dg.ROOT_RANKING_WEAK, {"expected_source": "size_guide", "query": "180斤穿什么码",
                                "kb_has_source": True}),
        (dg.ROOT_UNKNOWN, {"expected_source": None, "reason": "未标注"}),
    ]


def test_report_groups_by_source_and_picks_dominant_action():
    db = _FakeDB([_Result(rows=_qa_rows())])
    rep = source_report(db, top=10)

    assert rep["total"] == 4
    assert rep["pending_review"] == 1
    assert rep["by_cause"] == {
        dg.ROOT_NO_KB: 2, dg.ROOT_RETRIEVAL_MISS: 1, dg.ROOT_RANKING_WEAK: 1,
    }

    top = rep["sources"][0]
    assert top["source"] == "return_policy"
    assert top["total"] == 3
    assert top["causes"] == {dg.ROOT_NO_KB: 2, dg.ROOT_RETRIEVAL_MISS: 1}
    assert "补新卡" in top["action"]      # 占比最高的那类决定动作
    assert len(top["queries"]) <= 3


def test_report_can_filter_one_cause():
    db = _FakeDB([_Result(rows=_qa_rows())])
    rep = source_report(db, cause=dg.ROOT_NO_KB)

    assert rep["by_cause"] == {dg.ROOT_NO_KB: 2}
    assert [s["source"] for s in rep["sources"]] == ["return_policy"]


def test_report_handles_json_string_evidence():
    """pymysql 对 JSON 列可能回 str 而不是 dict，报表不能因此变空。"""
    import json as _json

    db = _FakeDB([_Result(rows=[
        (dg.ROOT_KB_STALE, _json.dumps(
            {"expected_source": "size_guide", "query": "尺码表在哪"},
            ensure_ascii=False)),
    ])])
    rep = source_report(db)
    assert rep["sources"][0]["source"] == "size_guide"
    assert rep["sources"][0]["causes"] == {dg.ROOT_KB_STALE: 1}


def test_hit_doc_source_uses_document_level_field():
    """检索返回 chunk 级 source（带 _secN），归因要比对文档级 doc_source。

    用错字段 → 永远判未命中 → 全部误报 no_kb，运营会去补已存在的卡。
    """
    hit = {"payload": {"source": "fashion_return_policy_v202606_sec4",
                       "doc_source": "fashion_return_policy_v202606.md"}}
    assert dg._hit_doc_source(hit) == "fashion_return_policy_v202606.md"


def test_hit_doc_source_falls_back_to_chunk_source():
    assert dg._hit_doc_source({"payload": {"source": "x_sec1"}}) == "x_sec1"
    assert dg._hit_doc_source({}) == ""


def test_doc_stem_normalizes_both_shapes():
    assert dg.doc_stem("fashion_return_policy_v202606.md") == "fashion_return_policy_v202606"
    assert dg.doc_stem("fashion_return_policy_v202606_sec4") == "fashion_return_policy_v202606"
    assert dg.doc_stem("") == ""
    assert dg.doc_stem(None) == ""


def test_doc_stem_keeps_version():
    """新旧两版是不同文档，不能混成一个 stem。

    混了的话，「命中已废止旧版」会被当成「命中新版」，kb_stale 永远报不出来。
    """
    assert dg.doc_stem("x_v202611.md") != dg.doc_stem("x_v202606.md")


def test_cross_shape_hit_counts():
    """expected 写文档级、检索回 chunk 级，仍要判命中。"""
    cause, ev = dg.classify(
        expected_source="fashion_return_policy_v202611.md",
        retrieved_sources=["fashion_return_policy_v202611_sec2", "size_guide_sec1"],
        kb_has_source=True,
        dims={"grounding": 5, "consistency": 5},
    )
    assert ev["rank"] == 1
    assert cause == dg.ROOT_GENERATION


def test_old_version_hit_does_not_satisfy_new_version_expectation():
    """检索回的是已废止旧版，不算命中新版——这才是 kb_stale 要抓的场景。"""
    cause, ev = dg.classify(
        expected_source="fashion_return_policy_v202611.md",
        retrieved_sources=["fashion_return_policy_v202606_sec4"],
        kb_has_source=True,
        dims={"grounding": 4, "consistency": 4},
    )
    assert ev["rank"] is None
    assert cause == dg.ROOT_RETRIEVAL_MISS


def test_report_empty_is_safe():
    db = _FakeDB([_Result(rows=[])])
    rep = source_report(db)
    assert rep["total"] == 0
    assert rep["sources"] == []


def test_context_missing_is_counted_not_pending_review():
    """上下文失败已归因，只是没有知识源，不该算"待标注"。"""
    db = _FakeDB([_Result(rows=[
        (dg.ROOT_CONTEXT_MISSING, {"expected_source": None, "reason": "没查到订单"}),
        (dg.ROOT_UNKNOWN, {"expected_source": None, "reason": "未标注"}),
    ])])
    rep = source_report(db)

    assert rep["by_cause"] == {dg.ROOT_CONTEXT_MISSING: 1}
    assert rep["total"] == 1
    assert rep["pending_review"] == 1     # 只有 unknown 算待标注
    assert rep["sources"] == []           # 没有知识源维度，清单为空是对的


# ---------------------------------------------------------------
# 过期卡：实盘踩出来的第 8 类根因
# ---------------------------------------------------------------
def test_expired_card_is_not_retrieval_miss():
    """双11 卡有效期至 2026-11-11，11-17 再问"现在有什么优惠"。

    它在库里，但检索层按设计过滤失效条目 → 永远搜不到。
    判成 retrieval_miss 会让运营去调同义词/补锚点，补完照样被过滤。
    """
    cause, ev = dg.classify(
        expected_source="fashion_promo_2026_double11.md",
        retrieved_sources=["fashion_shipping_policy_sec2"],
        kb_status="expired",
        kb_detail="已过期（有效期至 2026-11-11）",
    )
    assert cause == dg.ROOT_KB_EXPIRED
    assert cause != dg.ROOT_RETRIEVAL_MISS
    assert "补检索锚点不会生效" in ev["reason"]
    assert ev["kb_status"] == "expired"


def test_expired_with_live_sibling_is_real_retrieval_miss():
    """本条过期但同主题有生效版本 → 该搜新版没搜到，是检索问题，不是缺卡。"""
    cause, ev = dg.classify(
        expected_source="fashion_return_policy_v202606.md",
        retrieved_sources=["fashion_shipping_policy_sec1"],
        kb_status="superseded",
        kb_detail="已过期（有效期至 2026-06-30）",
        superseded_by="fashion_return_policy_v202611.md",
    )
    assert cause == dg.ROOT_RETRIEVAL_MISS
    assert ev["superseded_by"] == "fashion_return_policy_v202611.md"
    assert "没检出新版" in ev["reason"]


def test_active_card_still_judged_retrieval_miss():
    """有效但没搜到，还是 retrieval_miss——别把过期那一档套到所有未命中上。"""
    cause, _ = dg.classify(
        expected_source="fashion_promo_2026_double11.md",
        retrieved_sources=["other_sec1"],
        kb_status="active",
    )
    assert cause == dg.ROOT_RETRIEVAL_MISS
