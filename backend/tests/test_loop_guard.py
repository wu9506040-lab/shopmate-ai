"""循环检测单测（loop_guard）。

真正要守住的不是"能判重复"，是三条边界：
  1. 写操作同参第二次必须拦住（否则二次发货/二次改地址）
  2. 只读工具允许重看一次（前面结果被总量护栏 stub 掉后重取是合理的）
  3. 拦下来时数据不能丢——回灌内容里原结果还在，否则模型只能编
"""
from __future__ import annotations

import json

from app.services.chat.loop_guard import LoopGuard, LoopRules, load_rules

RULES = LoopRules(max_read_repeat=2, max_dup_turns=2)

ORDER_ARGS = '{"order_no": "ORD001"}'


def test_first_call_runs():
    g = LoopGuard(RULES)
    assert g.check("lookup_order", ORDER_ARGS).run is True


def test_read_tool_allows_one_recheck_then_blocks():
    """限次 2 = 首次 + 重看一次；第三次拦住。"""
    g = LoopGuard(RULES)
    g.check("lookup_order", ORDER_ARGS)
    g.record("lookup_order", ORDER_ARGS, '{"status": "shipped"}')

    assert g.check("lookup_order", ORDER_ARGS).run is True     # 重看一次
    g.record("lookup_order", ORDER_ARGS, '{"status": "shipped"}')

    third = g.check("lookup_order", ORDER_ARGS)
    assert third.run is False
    assert third.reason == "repeat_read"
    assert third.times == 2


def test_args_key_order_is_normalized():
    """键序不同的同一调用必须判成同一个——模型两次生成的 JSON 顺序经常不一样。"""
    g = LoopGuard(RULES)
    g.check("search_product", '{"a": 1, "b": 2}')
    g.record("search_product", '{"a": 1, "b": 2}', '{"items": []}')

    decision = g.check("search_product", '{"b": 2, "a": 1}')
    assert decision.times == 1                  # 认出来了，不是新签名
    assert decision.run is True                 # 第 2 次仍允许


def test_write_tool_blocked_on_second_identical_call():
    """写操作只许一次：同参第二次直接拦，不执行。"""
    g = LoopGuard(RULES)
    args = '{"order_no": "ORD001", "new_address": "北京市朝阳区", "confirmed": true}'
    assert g.check("modify_address", args).run is True
    g.record("modify_address", args, '{"ok": true, "confirmed": true}')

    second = g.check("modify_address", args)
    assert second.run is False
    assert second.reason == "repeat_write"


def test_write_tool_allowed_when_args_change():
    """先 confirmed=false 拿票据，再 confirmed=true 执行——参数不同，不能拦。"""
    g = LoopGuard(RULES)
    soft = '{"order_no": "ORD001", "sku": "SKU1", "confirmed": false}'
    hard = '{"order_no": "ORD001", "sku": "SKU1", "confirmed": true}'

    g.check("auto_delivery", soft)
    g.record("auto_delivery", soft, '{"needs_confirmation": true}')

    assert g.check("auto_delivery", hard).run is True


def test_blocked_content_keeps_original_data():
    """拦截不等于丢数据：回灌内容里原字段还在，另加一条提示。"""
    g = LoopGuard(RULES)
    original = json.dumps({"total": 3, "items": [{"sku": "SKU1"}]}, ensure_ascii=False)
    g.check("search_product", ORDER_ARGS)
    g.record("search_product", ORDER_ARGS, original)
    g.check("search_product", ORDER_ARGS)
    g.record("search_product", ORDER_ARGS, original)

    blocked = g.check("search_product", ORDER_ARGS)
    payload = json.loads(blocked.content)

    assert payload["total"] == 3                    # 业务数据一个不少
    assert payload["items"][0]["sku"] == "SKU1"
    assert "_loop_guard" in payload                 # 且明确告诉它别再调


def test_duplicate_turn_detection_and_reset():
    g = LoopGuard(RULES)
    assert g.should_finalize() is False

    g.end_turn(all_repeated=True)
    assert g.should_finalize() is False            # 一轮不够，可能是"先重看再答"

    g.end_turn(all_repeated=True)
    assert g.should_finalize() is True             # 连续两轮全重复


def test_real_execution_resets_dup_counter():
    """中间真查了一次就清零——不然"查 A 重复、查 B、再查 A 重复"会被误判打转。"""
    g = LoopGuard(RULES)
    g.end_turn(all_repeated=True)
    g.end_turn(all_repeated=False)
    g.end_turn(all_repeated=True)

    assert g.should_finalize() is False


def test_skipped_counter_for_meta():
    g = LoopGuard(RULES)
    g.check("search_product", ORDER_ARGS)
    g.record("search_product", ORDER_ARGS, "{}")
    g.check("search_product", ORDER_ARGS)
    g.record("search_product", ORDER_ARGS, "{}")
    g.check("search_product", ORDER_ARGS)          # 这次被拦

    stats = g.stats()
    assert stats["skipped"] == 1
    assert stats["distinct_calls"] == 1


def test_bad_json_args_do_not_raise():
    """脏参数（半截 JSON）不能让整个 FC 循环崩掉。"""
    g = LoopGuard(RULES)
    assert g.check("search_product", "{not json").run is True
    g.record("search_product", "{not json", "{}")
    assert g.check("search_product", "{not json").run is True


def test_load_rules_defaults():
    rules = load_rules()
    assert rules.max_read_repeat >= 1
    assert rules.max_dup_turns >= 1
    assert "modify_address" in rules.write_tools
