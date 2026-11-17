"""会话事实抽取单测（纯函数层）

覆盖：
- 订单号识别（大小写、非法格式、无匹配）
- 优先级：入参 > 实体 > 本轮原话 > 历史
- 历史里 user 消息优先于 assistant 回显
- resolved_orders 去重 / 保序 / 截断
- 意图白名单（商品咨询不许用订单上下文）
- 服装事实（尺码/身高/体重）：抓得准，且不抓区间、价格、尺码表
"""
from app.services.context.facts import (
    MAX_RESOLVED_ORDERS,
    extract_clothing_facts,
    extract_corrections,
    extract_order_no_from_history,
    extract_order_no_from_text,
    facts_from_history,
    flatten_facts,
    is_valid_order_no_format,
    merge_facts,
    merge_resolved_orders,
    resolve_turn_order_no,
    should_use_context_order,
)


class TestExtractText:

    def test_lowercase_normalized(self):
        assert extract_order_no_from_text("单号 ord20260622003 到哪了") == "ORD20260622003"

    def test_no_order_no(self):
        assert extract_order_no_from_text("这件衣服洗了会缩水吗") is None

    def test_empty(self):
        assert extract_order_no_from_text("") is None
        assert extract_order_no_from_text(None) is None

    def test_partial_date_not_matched(self):
        """位数不足（ORD + 7 位日期）不算订单号，避免把商品码当订单"""
        assert extract_order_no_from_text("ORD2026062 查一下") is None


class TestValidFormat:

    def test_valid(self):
        assert is_valid_order_no_format("ORD20260622003") is True

    def test_invalid(self):
        assert is_valid_order_no_format("ORD2026062") is False
        assert is_valid_order_no_format("123456") is False
        assert is_valid_order_no_format(None) is False

    def test_strip_spaces(self):
        assert is_valid_order_no_format(" ORD20260622003 ") is True


class TestResolvePriority:

    def test_param_wins(self):
        got = resolve_turn_order_no(
            history=[{"role": "user", "content": "ORD20260101001"}],
            query="ORD20260202002 到哪了",
            order_no="ORD20260303003",
        )
        assert got == "ORD20260303003"

    def test_entities_over_query(self):
        got = resolve_turn_order_no(
            query="这个单", entities={"order_no": "ord20260404004"},
        )
        assert got == "ORD20260404004"

    def test_query_when_no_entity(self):
        got = resolve_turn_order_no(query="ORD20260505005 能退吗", entities={})
        assert got == "ORD20260505005"

    def test_history_fallback(self):
        """第二轮只说"那能退吗"——订单号只在上一轮"""
        got = resolve_turn_order_no(
            history=[
                {"role": "user", "content": "ORD20260606006 到哪了"},
                {"role": "assistant", "content": "您的订单 ORD20260606006 已签收。"},
                {"role": "user", "content": "那能退吗"},
            ],
            query="那能退吗",
        )
        assert got == "ORD20260606006"

    def test_invalid_entity_skipped(self):
        """实体抽取给了半截号 → 跳过，不要写进上下文"""
        got = resolve_turn_order_no(
            query="那能退吗", entities={"order_no": "ORD123"},
        )
        assert got is None

    def test_nothing_found(self):
        assert resolve_turn_order_no(query="你好", history=[]) is None


class TestHistoryScan:

    def test_user_message_preferred_over_assistant(self):
        got = extract_order_no_from_history([
            {"role": "user", "content": "ORD20260707007"},
            {"role": "assistant", "content": "参考 ORD20260808008 这笔"},
        ])
        assert got == "ORD20260707007"

    def test_latest_user_message_wins(self):
        got = extract_order_no_from_history([
            {"role": "user", "content": "ORD20260707007"},
            {"role": "user", "content": "改问 ORD20260808008"},
        ])
        assert got == "ORD20260808008"

    def test_only_assistant_still_found(self):
        got = extract_order_no_from_history([
            {"role": "assistant", "content": "订单 ORD20260909009 已发货"},
        ])
        assert got == "ORD20260909009"


class TestMergeResolved:

    def test_append_and_dedupe(self):
        got = merge_resolved_orders(["ORD20260101001", "ORD20260202002"], "ORD20260101001")
        assert got == ["ORD20260101001", "ORD20260202002"]

    def test_new_first(self):
        got = merge_resolved_orders(["ORD20260101001"], "ORD20260303003")
        assert got == ["ORD20260303003", "ORD20260101001"]

    def test_dict_items(self):
        got = merge_resolved_orders([{"order_no": "ORD20260101001"}], "ORD20260202002")
        assert got == ["ORD20260202002", "ORD20260101001"]

    def test_cap(self):
        existing = [f"ORD202601010{i:02d}" for i in range(1, MAX_RESOLVED_ORDERS + 5)]
        got = merge_resolved_orders(existing, "ORD20261231999")
        assert len(got) == MAX_RESOLVED_ORDERS
        assert got[0] == "ORD20261231999"

    def test_none_new_keeps_existing(self):
        assert merge_resolved_orders(["ORD20260101001"], None) == ["ORD20260101001"]


class TestIntentWhitelist:

    def test_order_intents_allowed(self):
        for intent in ("order_query", "refund_query", "logistics_query"):
            assert should_use_context_order(intent) is True

    def test_product_policy_blocked(self):
        """商品/政策咨询里的"这个"指商品，不能拿旧订单号去串"""
        for intent in ("product_query", "policy_query", "", None):
            assert should_use_context_order(intent) is False


class TestClothingFacts:

    # ---- 该抓到的 ----
    def test_full_sentence(self):
        assert extract_clothing_facts("我身高170体重60穿什么码") == {
            "height_cm": 170, "weight_kg": 60.0,
        }

    def test_size_with_suffix(self):
        assert extract_clothing_facts("我平时穿L码") == {"size": "L"}

    def test_size_without_suffix_but_with_hint(self):
        assert extract_clothing_facts("我穿M") == {"size": "M"}

    def test_height_with_unit(self):
        assert extract_clothing_facts("165cm 能穿吗") == {"height_cm": 165}

    def test_height_colloquial_meter(self):
        assert extract_clothing_facts("我一米七五") == {"height_cm": 175}
        assert extract_clothing_facts("1.7米左右") == {"height_cm": 170}

    def test_weight_jin_converted(self):
        assert extract_clothing_facts("120斤") == {"weight_kg": 60.0}

    def test_bare_numbers_with_context(self):
        assert extract_clothing_facts("我170，体重60kg") == {
            "height_cm": 170, "weight_kg": 60.0,
        }

    def test_size_xl_variants(self):
        assert extract_clothing_facts("2XL码有货吗") == {"size": "2XL"}
        assert extract_clothing_facts("我要均码") == {"size": "均码"}

    # ---- 不该抓到的（每条对应一个真实误抓来源）----
    def test_range_not_taken_as_height(self):
        """商品表里的 170~180cm 是尺码区间，不是买家身高"""
        assert extract_clothing_facts("170~180cm 适合多少斤") == {}
        assert extract_clothing_facts("170-180cm 选哪个码") == {}

    def test_price_not_taken(self):
        assert extract_clothing_facts("¥199 这件能便宜点吗") == {}
        assert extract_clothing_facts("到手 199 元") == {}

    def test_size_table_not_taken_as_buyer_size(self):
        """「尺码表：S/M/L/XL 都有货」里的 S 不是买家穿的码"""
        assert extract_clothing_facts("尺码表：S/M/L/XL 都有货") == {}

    def test_order_no_digits_not_taken(self):
        assert extract_clothing_facts("订单 ORD20261116999A 到哪了") == {}

    def test_date_not_taken(self):
        assert extract_clothing_facts("2026年11月16日下的单") == {}

    def test_empty_and_none(self):
        assert extract_clothing_facts("") == {}
        assert extract_clothing_facts(None) == {}

    def test_out_of_range_ignored(self):
        assert extract_clothing_facts("身高 90cm") == {}
        assert extract_clothing_facts("体重 300kg") == {}


class TestFactsMerge:

    def test_override_records_prev(self):
        facts, changes = merge_facts({"height_cm": {"value": 170}}, {"height_cm": 175}, "T")
        assert facts["height_cm"]["value"] == 175
        assert facts["height_cm"]["prev"] == 170
        assert changes == [{"key": "height_cm", "from": 170, "to": 175}]

    def test_same_value_no_change(self):
        facts, changes = merge_facts({"size": {"value": "L"}}, {"size": "L"}, "T")
        assert changes == []
        assert facts["size"]["value"] == "L"

    def test_non_whitelist_key_dropped(self):
        """订单号/手机号这类绝不能进 facts——进去了就会被注入 prompt"""
        facts, _ = merge_facts(None, {"order_no": "ORD20260622003", "phone": "13800000000"}, "T")
        assert facts == {}

    def test_accepts_legacy_bare_value(self):
        facts, _ = merge_facts({"weight_kg": 60.0}, {"weight_kg": 65.0}, "T")
        assert facts["weight_kg"]["prev"] == 60.0

    def test_flatten(self):
        assert flatten_facts({"size": {"value": "M"}, "height_cm": 170}) == {
            "size": "M", "height_cm": 170,
        }


class TestCorrections:

    def test_bare_number_after_correction_mark(self):
        """「我记错了，是175」正常规则抓不到（无单位、无提示词）"""
        assert extract_clothing_facts("我记错了，是175") == {}
        assert extract_corrections("我记错了，是175", {"height_cm": 170}) == {"height_cm": 175}

    def test_disambiguate_by_nearest_old_value(self):
        """同一句里 175 既落身高也落体重范围 → 取离旧值近的那个"""
        out = extract_corrections("我说错了，是65", {"height_cm": 170, "weight_kg": 60})
        assert out == {"weight_kg": 65.0}

    def test_no_mark_no_correction(self):
        assert extract_corrections("175 有货吗", {"height_cm": 170}) == {}

    def test_too_far_from_old_rejected(self):
        """差太多说明那个数字在说别的事，不是改口"""
        assert extract_corrections("记错了，是120", {"height_cm": 170}) == {}

    def test_no_known_facts(self):
        assert extract_corrections("我记错了，是175", {}) == {}


class TestFactsFromHistory:

    def test_later_message_wins(self):
        history = [
            {"role": "user", "content": "我身高170"},
            {"role": "user", "content": "记错了，我175"},
        ]
        assert facts_from_history(history) == {"height_cm": 175}

    def test_assistant_ignored(self):
        history = [{"role": "assistant", "content": "尺码表 170~180cm 选 XL"}]
        assert facts_from_history(history) == {}

    def test_empty(self):
        assert facts_from_history([]) == {}
        assert facts_from_history(None) == {}
