"""工单 + 回归验证的单测（不连库、不调真检索）。

锁的是三个「会让人白干活」的判断：
- 三类根因必须出三类工单（no_kb 补卡，retrieval_miss 不能补卡）
- 重复开单要合并，否则同一处毛病在清单里出现十几次
- 检索不可用时不能判「验证不通过」，否则人会去改一份本来没问题的稿

真库端到端由 `scripts/kb_ops.py` 跑验（tests/conftest.py 把 DATABASE_URL
设成占位 host，真库测试在 full suite 里必然失败）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")

from app.services.kb_ops import diagnose as dg  # noqa: E402
from app.services.kb_ops import ticket as tk  # noqa: E402
from app.services.kb_ops import verify as vf  # noqa: E402


def _ticket(**kw):
    src = kw.pop("source", "fashion_promo_2026_double11.md")
    base = {
        "id": "TK-001",
        "kind": tk.KIND_PATCH,
        "kind_label": tk.KIND_LABELS[tk.KIND_PATCH],
        "source": src,
        "source_stem": dg.doc_stem(src),
        "root_cause": dg.ROOT_RETRIEVAL_MISS,
        "status": tk.STATUS_DRAFTED,
        "top_n": 3,
        "queries": ["现在有什么优惠活动"],
        "baseline": {"现在有什么优惠活动": None},
        "evidence": {"reason": "知识库里有该文档，但 top-5 没检出来",
                     "retrieved_sources": ["fashion_shipping_policy_sec2"]},
        "created_at": "2026-11-17 15:00:00",
        "updated_at": "2026-11-17 15:00:00",
        "history": [],
    }
    base.update(kw)
    return base


class _FakeRes:
    def __init__(self, rowcount=0, first=None):
        self.rowcount = rowcount
        self._first = first

    def first(self):
        return self._first

    def fetchall(self):
        return []


class _FakeDB:
    def __init__(self, rowcount=0):
        self.rowcount = rowcount
        self.calls = []
        self.commits = 0

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params))
        return _FakeRes(rowcount=self.rowcount)

    def commit(self):
        self.commits += 1


# ---------------------------------------------------------------
# 根因 → 工单类型
# ---------------------------------------------------------------
def test_cause_to_kind_covers_three_knowledge_causes():
    assert tk.CAUSE_TO_KIND[dg.ROOT_NO_KB] == tk.KIND_ADD
    assert tk.CAUSE_TO_KIND[dg.ROOT_RETRIEVAL_MISS] == tk.KIND_PATCH
    assert tk.CAUSE_TO_KIND[dg.ROOT_RANKING_WEAK] == tk.KIND_PATCH
    assert tk.CAUSE_TO_KIND[dg.ROOT_KB_STALE] == tk.KIND_UPDATE


def test_expired_card_opens_add_card_not_patch():
    """过期卡要补**新版本**，不是给旧卡补锚点——旧卡再改也过不了失效过滤。"""
    assert tk.CAUSE_TO_KIND[dg.ROOT_KB_EXPIRED] == tk.KIND_ADD


def test_non_knowledge_cause_opens_no_ticket():
    """generation / context_missing 不是改知识库能解决的，不该出现在运营清单里。"""
    assert dg.ROOT_GENERATION not in tk.CAUSE_TO_KIND
    assert dg.ROOT_CONTEXT_MISSING not in tk.CAUSE_TO_KIND
    assert dg.ROOT_UNKNOWN not in tk.CAUSE_TO_KIND


def test_no_kb_drafts_new_card_with_todo():
    t = _ticket(kind=tk.KIND_ADD, root_cause=dg.ROOT_NO_KB)
    text = tk.render_draft(t)
    assert "doc_key: fashion_promo_2026_double11" in text         # front matter 齐
    assert "title: TODO" in text                                   # 标题留人填
    assert "## 1. 现在有什么优惠活动" in text                      # 每条问句一节
    assert "TODO 结论" in text                                     # 结论不编


def test_add_card_front_matter_must_be_first_line():
    """front matter 必须落在文件第一行。

    ingest 的 read_front_matter 用 re.match 从头匹配，前面多一个工单注释块，
    整份 YAML 就失效——活动卡会按 permanent 入库，永远不过期（实盘踩过）。
    """
    t = _ticket(kind=tk.KIND_ADD, root_cause=dg.ROOT_KB_EXPIRED)
    text = tk.render_draft(t)
    assert text.startswith("---\n")
    assert "validity: temporary" in text        # 活动类强制临时
    assert "effective_to: TODO" in text         # 有效期必填


def test_policy_card_keeps_permanent_but_requires_effective_date():
    """政策卡按项目惯例长期有效，但版本生效日必须人确认。"""
    t = _ticket(kind=tk.KIND_ADD, root_cause=dg.ROOT_NO_KB,
                source="fashion_return_policy_v202612.md")
    text = tk.render_draft(t)
    assert "validity: permanent" in text
    assert "effective_from: TODO" in text


def test_retrieval_miss_draft_is_patch_not_new_card():
    """retrieval_miss 的草稿必须是"往现有文档追加锚点"，不是一张新卡。"""
    t = _ticket()
    text = tk.render_draft(t)
    assert "补丁草稿" in text
    assert "不要新建卡" in text
    assert "front matter" not in text.split("-->")[1]              # 没有第二份 front matter
    assert "kb_ticket:TK-001 BEGIN" in text


# ---------------------------------------------------------------
# apply：落盘与幂等
# ---------------------------------------------------------------
def test_apply_patch_appends_block(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    target = kb / "fashion_promo_2026_double11.md"
    target.write_text("# 双11规则\n\n满199减30\n", encoding="utf-8")

    t = _ticket()
    tk.write_draft(t, root=tmp_path)
    out = tk.apply_ticket(t, root=tmp_path, kb=kb, force=True)

    assert out["mode"] == "append"
    body = target.read_text(encoding="utf-8")
    assert "满199减30" in body                        # 原文没被覆盖
    assert "kb_ticket:TK-001 BEGIN" in body
    assert t["status"] == tk.STATUS_APPLIED


def test_apply_patch_twice_replaces_not_duplicates(tmp_path):
    """重复 apply 必须替换：重复插入等于同一段话被检索两次，引用编号会错乱。"""
    kb = tmp_path / "kb"
    kb.mkdir()
    target = kb / "fashion_promo_2026_double11.md"
    target.write_text("# 双11规则\n", encoding="utf-8")

    t = _ticket()
    tk.write_draft(t, root=tmp_path)
    tk.apply_ticket(t, root=tmp_path, kb=kb, force=True)
    out2 = tk.apply_ticket(t, root=tmp_path, kb=kb, force=True)

    assert out2["mode"] == "replace"
    assert target.read_text(encoding="utf-8").count("kb_ticket:TK-001 BEGIN") == 1


def test_apply_patch_missing_target_raises(tmp_path):
    """expected_source 标错时不能静默新建——那会凭空造出一条知识。"""
    kb = tmp_path / "kb"
    kb.mkdir()
    t = _ticket()
    tk.write_draft(t, root=tmp_path)
    with pytest.raises(RuntimeError, match="目标文档不存在"):
        tk.apply_ticket(t, root=tmp_path, kb=kb)
    assert not (kb / "fashion_promo_2026_double11.md").exists()


def test_apply_add_card_strips_marker(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    t = _ticket(kind=tk.KIND_ADD, root_cause=dg.ROOT_NO_KB,
                source="fashion_promo_2026_newyear.md")
    tk.write_draft(t, root=tmp_path)
    out = tk.apply_ticket(t, root=tmp_path, kb=kb, force=True)

    assert out["mode"] == "create"
    body = (kb / t["source"]).read_text(encoding="utf-8")
    assert "kb_ticket:" not in body              # 标记不进正文，免得被切进 chunk
    assert "title: TODO" not in body             # 标题占位被替换成文件名


def test_apply_refuses_unfilled_todo(tmp_path):
    """没填完的草稿不许入库：TODO 变成可检索知识，模型会照着它编。"""
    kb = tmp_path / "kb"
    kb.mkdir()
    t = _ticket(kind=tk.KIND_ADD, root_cause=dg.ROOT_NO_KB, source="fashion_x.md")
    tk.write_draft(t, root=tmp_path)

    with pytest.raises(RuntimeError, match="TODO 未填"):
        tk.apply_ticket(t, root=tmp_path, kb=kb)
    assert not (kb / "fashion_x.md").exists()


def test_apply_patch_also_guards_todo(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "fashion_promo_2026_double11.md").write_text("# 旧\n", encoding="utf-8")
    t = _ticket()
    tk.write_draft(t, root=tmp_path)
    with pytest.raises(RuntimeError, match="TODO 未填"):
        tk.apply_ticket(t, root=tmp_path, kb=kb)


def test_apply_add_card_refuses_overwrite(tmp_path):
    """补新卡撞上已有文件必须拒绝：覆盖掉旧卡就再也查不到"当时怎么规定的"。"""
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "fashion_promo_2026_double11.md").write_text("旧卡正文", encoding="utf-8")
    t = _ticket(kind=tk.KIND_ADD, root_cause=dg.ROOT_KB_EXPIRED)
    tk.write_draft(t, root=tmp_path)

    with pytest.raises(RuntimeError, match="不覆盖"):
        tk.apply_ticket(t, root=tmp_path, kb=kb)
    assert (kb / "fashion_promo_2026_double11.md").read_text(encoding="utf-8") == "旧卡正文"


def test_apply_add_card_as_renames_and_rewrites_doc_key(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    t = _ticket(kind=tk.KIND_ADD, root_cause=dg.ROOT_KB_EXPIRED)
    tk.write_draft(t, root=tmp_path)
    out = tk.apply_ticket(t, root=tmp_path, kb=kb, as_name="fashion_promo_2026_dec",
                          force=True)

    assert out["path"].name == "fashion_promo_2026_dec.md"
    assert t["source"] == "fashion_promo_2026_dec.md"
    assert t["source_stem"] == "fashion_promo_2026_dec"
    body = out["path"].read_text(encoding="utf-8")
    assert "doc_key: fashion_promo_2026_dec" in body
    assert "doc_key: fashion_promo_2026_double11" not in body   # 主题键跟着换


# ---------------------------------------------------------------
# 开单去重
# ---------------------------------------------------------------
def test_open_ticket_merges_same_source(monkeypatch, tmp_path):
    monkeypatch.setattr(dg, "retrieve_sources", lambda *a, **k: ["fashion_shipping_policy_sec1"])
    monkeypatch.setattr(dg, "_kb_status",
                        lambda *a, **k: {"status": "active", "detail": "长期有效"})

    store = {"next_seq": 1, "tickets": []}
    r1 = tk.open_ticket(None, query="现在有什么优惠活动",
                        expected_source="fashion_promo_2026_double11.md",
                        store=store, root=tmp_path)
    r2 = tk.open_ticket(None, query="双11还有满减吗",
                        expected_source="fashion_promo_2026_double11_sec3",
                        store=store, root=tmp_path)

    assert r1["created"] is True
    assert r2["created"] is False                       # 同一处毛病不再开第二张单
    assert len(store["tickets"]) == 1
    assert store["tickets"][0]["queries"] == ["现在有什么优惠活动", "双11还有满减吗"]


def test_open_ticket_expired_uses_new_filename(monkeypatch, tmp_path):
    """过期卡开单必须另起文件名，原文件只留在证据里——否则 apply 会覆盖历史依据。"""
    monkeypatch.setattr(dg, "retrieve_sources", lambda *a, **k: ["other_sec1"])
    monkeypatch.setattr(dg, "_kb_status",
                        lambda *a, **k: {"status": "expired",
                                         "detail": "已过期（有效期至 2026-11-11）"})
    res = tk.open_ticket(None, query="现在有什么优惠活动",
                         expected_source="fashion_promo_2026_double11.md",
                         store={"next_seq": 1, "tickets": []}, root=tmp_path)
    t = res["ticket"]

    assert t["root_cause"] == dg.ROOT_KB_EXPIRED
    assert t["kind"] == tk.KIND_ADD
    assert t["source"] == "fashion_promo_2026_double11_new.md"
    assert t["evidence"]["expired_source"] == "fashion_promo_2026_double11.md"


def test_open_ticket_skips_non_knowledge_cause(monkeypatch, tmp_path):
    """答案是"查不到订单"这类，归因成 context_missing，不该开知识工单。"""
    monkeypatch.setattr(dg, "retrieve_sources", lambda *a, **k: [])
    t = _ticket(kind=tk.KIND_ADD, root_cause=dg.ROOT_NO_KB)
    res = tk.open_ticket(None, query="我要换货", expected_source=t["source"],
                         store={"next_seq": 1, "tickets": []}, root=tmp_path)
    assert res["ticket"] is not None            # no_kb 正常开单

    monkeypatch.setattr(dg, "retrieve_sources", lambda *a, **k: None)  # 检索挂了
    res2 = tk.open_ticket(None, query="我要换货", expected_source=t["source"],
                          store={"next_seq": 1, "tickets": []}, root=tmp_path)
    assert res2["ticket"] is None               # 检索不可用 → unknown → 不开单
    assert "unknown" in res2["skipped"]


# ---------------------------------------------------------------
# 验证与关单
# ---------------------------------------------------------------
def test_verify_pass_when_rank_in_top_n(monkeypatch):
    monkeypatch.setattr(vf, "retrieve_sources",
                        lambda *a, **k: ["fashion_shipping_policy_sec1",
                                         "fashion_promo_2026_double11_sec2"])
    t = _ticket()
    r = vf.verify_ticket(t)
    assert r["details"][0]["rank"] == 2
    assert r["passed"] is True


def test_verify_fail_when_rank_beyond_top_n(monkeypatch):
    top = [f"other_{i}_sec1" for i in range(3)] + ["fashion_promo_2026_double11_sec1"]
    monkeypatch.setattr(vf, "retrieve_sources", lambda *a, **k: top)
    r = vf.verify_ticket(_ticket())
    assert r["details"][0]["rank"] == 4
    assert r["passed"] is False


def test_verify_uses_full_pipeline_by_default(monkeypatch):
    """验证必须走完整链路（含精排）；跳过精排等于验的是另一条路。"""
    seen = []

    def fake(q, k, *, skip_rerank=True):
        seen.append(skip_rerank)
        return ["fashion_promo_2026_double11_sec1"]

    monkeypatch.setattr(vf, "retrieve_sources", fake)
    vf.verify_ticket(_ticket())
    assert seen == [False]


def test_verify_inconclusive_when_retrieval_down(monkeypatch):
    monkeypatch.setattr(vf, "retrieve_sources", lambda *a, **k: None)
    r = vf.verify_ticket(_ticket())
    assert r["inconclusive"] is True
    assert r["passed"] is False


def test_settle_inconclusive_leaves_status(monkeypatch, tmp_path):
    """检索挂了不改状态：既不冤枉人也不假装通过。"""
    monkeypatch.setattr(vf, "retrieve_sources", lambda *a, **k: None)
    t = _ticket(status=tk.STATUS_APPLIED)
    db = _FakeDB()
    r = vf.settle_ticket(db, t, store={"next_seq": 2, "tickets": [t]}, root=tmp_path)
    assert r["inconclusive"] is True
    assert t["status"] == tk.STATUS_APPLIED
    assert db.commits == 0


def test_settle_pass_closes_and_backfills(monkeypatch, tmp_path):
    monkeypatch.setattr(vf, "retrieve_sources",
                        lambda *a, **k: ["fashion_promo_2026_double11_sec1"])
    t = _ticket(status=tk.STATUS_APPLIED)
    db = _FakeDB(rowcount=3)
    r = vf.settle_ticket(db, t, store={"next_seq": 2, "tickets": [t]}, root=tmp_path)

    assert r["passed"] is True
    assert t["status"] == tk.STATUS_CLOSED
    assert r["backfilled"]["updated"] == 3
    stmt = db.calls[0][0]
    assert "expected_source" in stmt
    # 只填空位：人工标过的绝不覆盖
    assert "expected_source IS NULL" in stmt


def test_settle_fail_reopens(monkeypatch, tmp_path):
    monkeypatch.setattr(vf, "retrieve_sources", lambda *a, **k: ["other_sec1"])
    t = _ticket(status=tk.STATUS_APPLIED)
    r = vf.settle_ticket(_FakeDB(), t, store={"next_seq": 2, "tickets": [t]}, root=tmp_path)
    assert r["passed"] is False
    assert t["status"] == tk.STATUS_REOPENED


def test_closed_ticket_refuses_new_draft(tmp_path):
    t = _ticket(status=tk.STATUS_CLOSED)
    with pytest.raises(RuntimeError, match="已关闭"):
        tk.draft_ticket(t, root=tmp_path)


# ---------------------------------------------------------------
# 落盘格式
# ---------------------------------------------------------------
def test_store_roundtrip_and_seq(tmp_path):
    store = tk.load_store(tmp_path)
    assert store == {"next_seq": 1, "tickets": []}
    store["tickets"].append(_ticket())
    tk.save_store(store, tmp_path)
    assert tk.load_store(tmp_path)["tickets"][0]["id"] == "TK-001"


def test_save_store_refuses_writing_real_dir_under_pytest(monkeypatch):
    """测试里忘传 root 会写脏真实运营台账，必须当场失败而不是静默写。"""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x (call)")
    with pytest.raises(RuntimeError, match="必须显式传 root"):
        tk.save_store({"next_seq": 1, "tickets": []})


def test_list_tickets_hides_closed_by_default():
    store = {"next_seq": 3, "tickets": [
        _ticket(id="TK-001", status=tk.STATUS_OPEN),
        _ticket(id="TK-002", status=tk.STATUS_CLOSED),
    ]}
    assert [t["id"] for t in tk.list_tickets(store)] == ["TK-001"]
    assert len(tk.list_tickets(store, include_closed=True)) == 2
