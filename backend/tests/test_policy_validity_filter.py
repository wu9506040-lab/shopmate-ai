"""检索层有效期过滤 + 失效提示 单测

为什么单独测这一层：
- `kb_meta` 只回答"这条有没有过期"（纯函数，已单独覆盖）
- 这里回答"检索结果该怎么处理过期条目" —— 丢弃？补提示？会影响用户看到的答案，
  是业务判断，不能只靠集成测试碰运气

三个关键行为：
1. 过期条目不出现在结果里（不能把过期优惠喂给模型）
2. 用户明确问那条已结束的活动时，补一条**只含结论**的提示（否则答非所问）
3. Milvus 未标注字段写入的 0 不能当 1970 年（否则长期政策全被判过期）
"""
from __future__ import annotations

import datetime as dt

from app.services.policy_service import _boost_tag_matches, _drop_inactive

TZ = dt.timezone(dt.timedelta(hours=8))


def _hit(source, *, title="", tags="", validity="permanent", eff_to=0, eff_from=0, score=0.6):
    return {
        "id": source,
        "score": score,
        "payload": {
            "source": source,
            "title": title,
            "tags": tags,
            "validity": validity,
            "effective_from": eff_from,
            "effective_to": eff_to,
        },
    }


def test_expired_hit_is_dropped():
    """过期条目：连同原文一起移除；分数不高时也不补提示（避免无关问题被打扰）"""
    expired = _hit("promo_九月活动_sec1", title="九月活动", validity="temporary",
                   eff_to=1759248000, score=0.55)  # 2025-10-01
    hits = _drop_inactive([expired], query="随便问问")
    assert hits == []


def test_permanent_hit_with_zero_effective_to_is_kept():
    """Milvus INT64 未标注写 0 —— 必须当"无截止日"，不能当 1970 年"""
    hits = _drop_inactive([_hit("warranty_sec1", title="退换政策")], query="退换多久")
    assert len(hits) == 1


def test_expired_notice_added_when_query_names_it():
    """查询里出现过期文档的标签 → 补一条"已失效"提示，且置顶（防被 top_k 截掉）"""
    expired = _hit(
        "promo_国庆_sec1", title="国庆满减活动", tags="国庆,满减",
        validity="temporary", eff_to=1791302400, score=0.7,
    )
    live = _hit("warranty_sec1", title="退换政策", tags="退换", score=0.5)
    hits = _drop_inactive([expired, live], query="国庆满减还能用吗")
    assert hits[0].get("expired_notice") is True
    text = hits[0]["payload"]["text"]
    assert "已失效" in text
    # 关键：不能把过期优惠数字带进来（否则模型照样会复述）
    assert "满 3000" not in text and "300" not in text
    # 有效条目仍在
    assert any(h["payload"]["source"] == "warranty_sec1" for h in hits[1:])


def test_no_notice_when_query_unrelated():
    """过期条目分数只比有效条目略高时不补提示（差距 ≤0.05，说明用户不是在问它）"""
    expired = _hit("promo_国庆_sec1", title="国庆满减活动", tags="国庆,满减",
                   validity="temporary", eff_to=1791302400, score=0.70)
    live = _hit("warranty_sec1", title="退换政策", tags="退换", score=0.68)
    hits = _drop_inactive([expired, live], query="退换多久")
    assert all(not h.get("expired_notice") for h in hits)


def test_scheduled_future_doc_is_dropped():
    """未生效（尚未开始）也要过滤；问起时给的是"未生效"提示，不是"已失效"。"""
    future = _hit("promo_双12_sec1", title="双 12 预告", tags="双12",
                  validity="temporary", eff_from=1796112000, eff_to=1798704000)  # 2026-12
    hits = _drop_inactive([future], query="双12有什么活动")
    assert hits and hits[0].get("expired_notice") is True
    assert hits[0]["payload"]["text"].startswith("【未生效】")


# =============================================================
# 标签 / 促销意图重排
# =============================================================
def _doc(source, *, doc_type="policy", tags="", score=0.6):
    return {"id": source, "score": score, "payload": {
        "source": source, "doc_type": doc_type, "tags": tags, "validity": "permanent",
    }}


def test_tag_hit_moves_doc_first():
    """查询里出现条目标签（如"以旧换新"）→ 该条排到最前，其余保持原顺序"""
    policy = _doc("warranty_sec1", tags="退换,三包")
    promo = _doc("promo_sec1", doc_type="promo", tags="以旧换新,补贴")
    hits = _boost_tag_matches([policy, promo], "以旧换新怎么算")
    assert hits[0]["payload"]["source"] == "promo_sec1"
    assert hits[1]["payload"]["source"] == "warranty_sec1"


def test_promo_intent_hint_beats_literal_tag_mismatch():
    """文档标签写"双11"、用户写"双十一"——字面不匹配，但促销意图词要能兜住"""
    policy = _doc("warranty_sec1", tags="退换")
    promo = _doc("promo_sec1", doc_type="promo", tags="双11,补贴")
    hits = _boost_tag_matches([policy, promo], "双十一买加绒夹克能补多少")
    assert hits[0]["payload"]["source"] == "promo_sec1"


def test_no_hint_keeps_original_order():
    """无标签命中、无促销意图 → 顺序不变（不能把退换问题也重排到活动上）"""
    policy = _doc("warranty_sec1", tags="退换")
    promo = _doc("promo_sec1", doc_type="promo", tags="双11")
    hits = _boost_tag_matches([policy, promo], "加绒夹克用三年还退换吗")
    assert [h["payload"]["source"] for h in hits] == ["warranty_sec1", "promo_sec1"]
