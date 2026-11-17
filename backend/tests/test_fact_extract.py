"""事实抽取单测。

三条设计意图，每条都要能挡住退化：
  1. 助手承诺必须进摘要（否则"你上次说给我补发"无据可查）
  2. 否定约束要归一（否则"要红色"和"不要红色"并列，模型靠猜）
  3. 摘要不能盖掉 L2（改口后旧值必须让位）
"""
from __future__ import annotations

from app.services.chat.fact_extract import (
    conflicts_with_known,
    extract_round_facts,
)
from app.services.chat.memory_contract import compact_round


def _round(user: str = "", assistant: str = "") -> list[list[dict]]:
    msgs = []
    if user:
        msgs.append({"role": "user", "content": user})
    if assistant:
        msgs.append({"role": "assistant", "content": assistant})
    return [msgs]


# ---------------------------------------------------------------
# 承诺：摘要里必须留得住
# ---------------------------------------------------------------
def test_commitment_extracted():
    f = extract_round_facts(_round("我的补发了吗", "已为您登记补发，预计 48 小时到账"))

    assert f["commitments"] == ["登记补发"]


def test_query_is_not_commitment():
    """「已为您查询」没有承诺任何事。抽进摘要 = 模型以为答应过要处理。"""
    f = extract_round_facts(_round(assistant="已为您查询订单状态"))

    assert f["commitments"] == []


def test_commitment_appears_in_summary():
    rnd = [
        {"role": "user", "content": "补发了吗"},
        {"role": "assistant", "content": "已为您登记补发"},
    ]
    assert "已办：登记补发" in compact_round(rnd)


# ---------------------------------------------------------------
# 否定约束：归一成「排除：X」
# ---------------------------------------------------------------
def test_negation_extracted():
    f = extract_round_facts(_round("别要黑色的"))

    assert f["constraints"] == ["黑色的"]


def test_negation_not_duplicated_when_in_entity_sentence():
    """实体原句里已经写了"别要黑色"，不再追加一次"排除"。"""
    out = compact_round([{"role": "user", "content": "我预算500元，别要黑色的"}])

    assert out.count("黑色") == 1


def test_afraid_is_not_negation():
    """"怕冷" = 需要保暖，不是排除冷。抽成否定会答反。"""
    f = extract_round_facts(_round("我怕冷"))

    assert f["constraints"] == []


# ---------------------------------------------------------------
# 敏感项：宁可少记，不做危险裁剪
# ---------------------------------------------------------------
def test_sensitive_sentence_dropped():
    f = extract_round_facts(_round("订单ORD2026110900123要退款"))

    assert f["entities"] == []


def test_phone_number_sentence_dropped():
    """含手机号的分句丢掉，同一轮里干净的分句照留（分句是独立信息）。"""
    f = extract_round_facts(_round("手机号13800000000，我穿175码"))

    texts = [e["text"] for e in f["entities"]]
    assert texts == ["我穿175码"]
    assert not any("13800000000" in t for t in texts)


# ---------------------------------------------------------------
# 与 L2 冲突：摘要必须让位
# ---------------------------------------------------------------
def test_conflict_detected_on_value_mismatch():
    entity = {"text": "我穿170码", "key": "size", "value": "170"}

    assert conflicts_with_known(entity, {"size": {"value": "175"}})
    assert not conflicts_with_known(entity, {"size": {"value": "170码"}})
    assert not conflicts_with_known(entity, {"budget": {"value": "500"}})
    assert not conflicts_with_known(entity, None)


def test_compact_round_drops_overridden_value():
    """买家改口 175，摘要里的 170 必须消失——两处同时进 prompt 必答错。"""
    rnd = [{"role": "user", "content": "我穿170码"}]

    assert compact_round(rnd, known_facts={"size": {"value": "175"}}) == ""
    assert compact_round(rnd, known_facts={"size": {"value": "170"}}) == "我穿170码"


def test_summary_respects_known_facts_end_to_end():
    """整条链路：超预算 → 裁掉旧轮 → 摘要不含被 L2 覆盖的值。"""
    from app.services.chat.memory_contract import fit_history_budget

    history = [
        {"role": "user", "content": "我穿170码"},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "我预算500元"},
        {"role": "assistant", "content": "好的"},
    ]
    for i in range(12):
        history.append({"role": "user", "content": f"第{i}轮随便问问什么的"})
        history.append({"role": "assistant", "content": "好的"})

    out, stats = fit_history_budget(history, max_rounds=2, char_budget=100,
                                    max_msg_chars=60, summary_budget=240,
                                    known_facts={"size": {"value": "175"}})

    assert stats["dropped_rounds"] > 0
    assert out[0]["role"] == "summary"
    assert "预算500元" in out[0]["content"]        # 不冲突的照常留
    assert "170" not in out[0]["content"]          # 被 L2 覆盖的必须消失
