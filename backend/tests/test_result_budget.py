"""工具结果预算单测。

重点不是"能裁短"，而是四个不能破的约束：
  1. 错误不裁（截了模型没法自我修正）
  2. 业务字段不删（删了模型会重复执行写操作）
  3. 裁了必须说（否则模型以为数据就这么多）
  4. 裁完仍是合法 JSON（半截 JSON 会让整轮 FC 崩）
"""
from __future__ import annotations

import json

from app.tools.result_budget import (
    BudgetRules,
    enforce_total_budget,
    load_rules,
    slim,
)

RULES = BudgetRules(max_chars=2000, max_list_items=5, max_field_chars=300)


def _products(n: int) -> dict:
    return {
        "total": n,
        "items": [
            {"product_id": i, "name": f"针织衫 {i}", "desc": "羊毛混纺"}
            for i in range(n)
        ],
    }


def test_small_result_untouched():
    """小结果一个字节都不该变——预算不是"统一瘦身"。"""
    result = {"total": 1, "items": [{"name": "针织衫"}]}
    out, stats = slim("search_product", result, RULES)

    assert out == result
    assert "_truncated" not in out
    assert stats["truncated"] is False
    assert stats["orig_chars"] == stats["slim_chars"]


def test_error_never_truncated():
    """错误信息必须完整，哪怕它自己就超预算。"""
    result = {"error": "tool execution failed: " + "x" * 5000}
    out, stats = slim("lookup_order", result, RULES)

    assert out == result
    assert stats["truncated"] is False


def test_list_truncated_with_total():
    out, stats = slim("search_product", _products(20), RULES)

    assert len(out["items"]) == 5
    assert out["total"] == 20                       # 工具自带的 total 保留
    assert stats["dropped_items"] == 15
    assert out["_truncated"]["fields"][0]["total"] == 20
    assert "不要用相同参数重复调用" in out["_truncated"]["hint"]


def test_long_field_truncated():
    out, _ = slim("search_product", {"desc": "羊毛" * 500}, RULES)

    assert out["desc"].endswith("…")
    assert len(out["desc"]) == 301                   # 300 + 省略号


def test_drop_keys_removed():
    out, stats = slim("lookup_order", {"order_no": "ORD1", "debug": {"sql": "..."},
                                       "raw": "x" * 100}, RULES)

    assert "debug" not in out and "raw" not in out
    assert out["order_no"] == "ORD1"                 # 业务字段照留
    assert stats["dropped_fields"] == 2


def test_protected_keys_survive_hard_cap():
    """压到必须 hard cap 时，单据字段也不能丢。"""
    big = {f"f{i}": "内容" * 40 for i in range(200)}
    big.update({"ticket_id": "TK-9", "confirmed": True, "order_no": "ORD123"})
    out, stats = slim("modify_order", big, RULES)

    assert out["ticket_id"] == "TK-9"
    assert out["confirmed"] is True
    assert out["order_no"] == "ORD123"
    assert out["_truncated"]["hard_cap"] is True
    assert stats["truncated"] is True


def test_output_always_json_serializable():
    """半截 JSON 会让下一轮 FC 直接报错，必须保证能序列化。"""
    big = {f"f{i}": "内容" * 40 for i in range(200)}
    out, _ = slim("search_product", big, RULES)

    assert isinstance(out, dict)
    assert json.loads(json.dumps(out, ensure_ascii=False))


def test_non_dict_wrapped():
    out, _ = slim("search_product", ["a", "b"], RULES)
    assert out == {"result": ["a", "b"]}


def test_char_budget_respected_when_possible():
    out, stats = slim("search_product", _products(20), RULES)
    assert stats["slim_chars"] <= RULES.max_chars
    assert stats["slim_chars"] < stats["orig_chars"]


def test_degrade_truncates_fields_before_items():
    """轻微超预算时砍字段，不许整条商品消失。

    真实场景：5 个商品各 410 字、总共 2050，只超 50 字。
    砍条目会丢 2 个商品；截字段只让描述变短。后者才是对的降级方向。
    """
    fat = {"items": [{"sku": i, "a": "内容" * 140, "b": "内容" * 140}
                     for i in range(5)]}
    out, stats = slim("search_product", fat, RULES)

    assert len(out["items"]) == 5                     # 一个都没丢
    assert out["items"][0]["a"].endswith("…")
    assert stats["truncated"] is True                 # 统计不能被降级吞掉
    assert stats["slim_chars"] <= RULES.max_chars


def test_field_truncation_does_not_add_hint():
    """只截字段时不加提示——hint 本身 80 字，会吃回裁掉的量。"""
    out, stats = slim("search_product", {"desc": "羊毛" * 400}, RULES)

    assert out["desc"].endswith("…")
    assert "_truncated" not in out
    assert stats["truncated"] is True


def test_load_rules_defaults_on_missing_config(monkeypatch):
    import app.services.config_loader as cl

    def _boom(*a, **k):
        raise RuntimeError("yaml gone")

    monkeypatch.setattr(cl, "get_config_loader", _boom)
    rules = load_rules()

    assert rules.max_chars == 2000                   # 配置坏了也要能用默认口径
    assert "debug" in rules.drop_keys


# =============================================================
# 总量护栏（enforce_total_budget）
# =============================================================
def _msgs(*contents: str) -> list[dict]:
    out = [{"role": "system", "content": "sys"}, {"role": "user", "content": "问题"}]
    for i, c in enumerate(contents):
        out.append({"role": "tool", "tool_call_id": f"c{i}", "content": c})
    return out


def test_total_budget_untouched_when_under_limit():
    msgs = _msgs("a" * 100, "b" * 100)
    out, stats = enforce_total_budget(msgs, RULES)

    assert out is msgs                    # 没超就原样返回，不新建 list
    assert stats["dropped"] == 0


def test_total_budget_drops_oldest_keeps_recent():
    """超预算时动最早的，最近 2 条必须原样——那才是本轮要用来作答的。"""
    msgs = _msgs("a" * 3000, "b" * 3000, "c" * 3000)
    out, stats = enforce_total_budget(msgs, RULES, keep_recent=2)

    assert stats["dropped"] == 1
    assert out[2]["content"].startswith('{"_dropped"')
    assert out[3]["content"] == "b" * 3000
    assert out[4]["content"] == "c" * 3000
    assert stats["slim_chars"] < stats["orig_chars"]


def test_total_budget_keeps_tool_call_id_and_role():
    """stub 只能换 content：tool_call_id 一丢，整轮请求直接 400。"""
    msgs = _msgs("a" * 9000, "b" * 100, "c" * 100)
    out, _ = enforce_total_budget(msgs, RULES, keep_recent=2)

    assert out[2]["role"] == "tool"
    assert out[2]["tool_call_id"] == "c0"
    assert out[0]["content"] == "sys"     # 非 tool 消息一个都不许动


def test_total_budget_never_touches_recent_even_if_still_over():
    """最近 2 条再怎么超也不 stub——宁可超预算，也不能让模型手里没数据。"""
    msgs = _msgs("a" * 10, "b" * 4000, "c" * 4000)
    out, stats = enforce_total_budget(msgs, RULES, keep_recent=2)

    assert stats["dropped"] == 0          # 唯一可动的比 stub 还短，不再削
    assert out[3]["content"] == "b" * 4000
    assert out[4]["content"] == "c" * 4000
    assert stats["slim_chars"] > RULES.context_max_chars   # 允许超：数据优先


def test_total_budget_skip_when_stub_not_shorter():
    """内容本来就比 stub 短，换了反而更长，跳过。"""
    msgs = _msgs("a" * 10, "b" * 6000, "c" * 6000)
    out, stats = enforce_total_budget(msgs, RULES, keep_recent=2)

    assert out[2]["content"] == "a" * 10
    assert stats["dropped"] == 0


def test_total_budget_idempotent():
    msgs = _msgs("a" * 3000, "b" * 3000, "c" * 3000)
    once, _ = enforce_total_budget(msgs, RULES, keep_recent=2)
    twice, stats = enforce_total_budget(once, RULES, keep_recent=2)

    assert stats["dropped"] == 0          # 已经 stub 过的不再重复计数
    assert twice[2]["content"] == once[2]["content"]


def test_load_rules_reads_context_budget():
    rules = load_rules()
    assert rules.context_max_chars > 0
    assert rules.context_max_chars >= rules.max_chars
