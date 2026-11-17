"""句级答案自检的判定口径（防回归）。

背景：自检命中会把整句替换成「这块资料我没查准，建议转人工核对～」，
误杀的代价不是「少说一句」，而是用户问发货时效却被告知资料没查准。
实测在真模型评测里连续两轮触发，根因是订单号里的数字被当成了幻觉数字。

这几条锁死「什么该拦、什么不该拦」：
  该拦：编造的数字（资料里没有的时效/金额/尺码数）
  不该拦：订单号、货号、日期、用户自己说过的数字
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.chat.answer_verifier import (  # noqa: E402
    SentenceVerifier, verify_sentence, collect_allowed_numbers,
)

CTX = [
    {
        "type": "policy",
        "source": "fashion_shipping_policy_sec1",
        "title": "发货时效",
        "text_preview": "| 现货商品 | 付款后 **48 小时内**发出（活动期间可能顺延至 72 小时） |",
    },
    {
        "type": "policy",
        "source": "fashion_size_guide_v2026aw_sec6",
        "title": "2026 秋冬尺码表",
        "text_preview": "| 45-55kg | M |\n| 55-65kg | L |",
    },
]


class TestAllowedNumbers(unittest.TestCase):
    def test_collects_from_preview_title_source(self):
        allowed = collect_allowed_numbers(CTX)
        self.assertIn("48", allowed)
        self.assertIn("72", allowed)
        self.assertIn("2026", allowed)   # 只出现在 title/source 里
        self.assertIn("55", allowed)


class TestIdentifierNotTreatedAsNumber(unittest.TestCase):
    """订单号 / 货号里的数字不是事实数字——这是线上误杀的主因。"""

    def test_order_no_does_not_fail_sentence(self):
        v = SentenceVerifier(CTX, user_query="什么时候能发货")
        v.feed("您的订单 ORD202611165CF92C 已付款，现货 48 小时内发出[1]。")
        self.assertFalse(v.flagged, f"订单号被误判: {v.reasons}")

    def test_sku_does_not_fail_sentence(self):
        v = SentenceVerifier(CTX, user_query="这件有货吗")
        v.feed("货号 FS368998-M 现货，付款后 48 小时内发出[1]。")
        self.assertFalse(v.flagged, f"货号被误判: {v.reasons}")

    def test_date_not_split_into_numbers(self):
        v = SentenceVerifier(CTX, user_query="尺码表改了吗")
        v.feed("新版自 2026-09-15 起执行，现货 48 小时内发出[1]。")
        self.assertFalse(v.flagged, f"日期被拆开校验: {v.reasons}")


class TestUserNumbersAllowed(unittest.TestCase):
    def test_user_height_weight_can_be_echoed(self):
        v = SentenceVerifier(CTX, user_query="身高 168 体重 52 公斤穿什么码")
        v.feed("按您说的 168cm、52kg，参考 55kg 档位对应 M[1]。")
        self.assertFalse(v.flagged, f"用户自述数字被误判: {v.reasons}")


class TestUnitConversionAllowed(unittest.TestCase):
    """斤↔kg 换算是资料本身要求的（尺码表用斤制），不能当幻觉拦掉。"""

    def test_jin_to_kg_conversion_passes(self):
        ctx = [{"type": "policy", "source": "fashion_size_guide_v2026aw_sec6",
                "text_preview": "| M | 160~170 | 95~110 |"}]
        v = SentenceVerifier(ctx, user_query="身高 165 体重 55 公斤穿什么码")
        v.feed("M 码对应体重 95~110 斤（即 47.5~55kg），命中 M[1]。")
        self.assertFalse(v.flagged, f"单位换算被误判: {v.reasons}")

    def test_non_conversion_still_blocked(self):
        ok, reason = verify_sentence("M 码体重上限 47.5kg[1]。",
                                     {"95", "110", "160", "170"}, len(CTX))
        # 47.5 是 95 的一半，属换算，放行；这里验证 47.5 不被当作幻觉
        self.assertTrue(ok, reason)
        # 但 42 既不在资料、也不是任何值的 2 倍关系 → 仍要拦
        ok2, reason2 = verify_sentence("M 码体重上限 42kg[1]。",
                                       {"95", "110", "160", "170"}, len(CTX))
        self.assertFalse(ok2)
        self.assertIn("42", reason2)


class TestRealHallucinationStillBlocked(unittest.TestCase):
    """放宽误杀不能把真拦截一起放掉。"""

    def test_invented_number_blocked(self):
        ok, reason = verify_sentence("付款后 99 小时内发出[1]。",
                                     collect_allowed_numbers(CTX), len(CTX))
        self.assertFalse(ok)
        self.assertIn("99", reason)

    def test_cite_out_of_range_blocked(self):
        ok, reason = verify_sentence("付款后 48 小时内发出[9]。",
                                     collect_allowed_numbers(CTX), len(CTX))
        self.assertFalse(ok)
        self.assertEqual(reason, "cite_out_of_range")

    def test_blocked_sentence_is_dropped_and_recorded(self):
        v = SentenceVerifier(CTX, user_query="什么时候发货")
        out = v.feed("付款后 99 小时内发出[1]。")
        self.assertTrue(v.flagged)
        # 拦下的句子直接丢弃，不再替换成兜底话术（否则会出现
        # 「参考价 128 元。这块资料我没查准」这种自相矛盾的答案）
        self.assertEqual(out, [""])
        # 原句必须留档，否则无法判断是误杀还是拦住幻觉
        self.assertEqual(len(v.dropped), 1)
        self.assertIn("99", v.dropped[0]["sentence"])
        self.assertEqual(v.dropped[0]["reason"], "number_not_in_sources:99")

    def test_need_fallback_only_when_nothing_left(self):
        v = SentenceVerifier(CTX, user_query="什么时候发货")
        v.feed("付款后 99 小时内发出[1]。")
        # 还有别的正常句子 → 不认输，别把整轮回答变成故障话术
        self.assertFalse(v.need_fallback("付款后 48 小时内发出[1]，顺丰配送。"))
        # 核心结论被拦光 → 必须补兜底
        self.assertTrue(v.need_fallback(""))

    def test_clean_answer_never_needs_fallback(self):
        v = SentenceVerifier(CTX, user_query="什么时候发货")
        v.feed("付款后 48 小时内发出[1]。")
        self.assertFalse(v.need_fallback(""))


class TestDocCountIsTotalItems(unittest.TestCase):
    """引用编号按 contexts 全序编号，混入非 policy 条目时不能少算。"""

    def test_mixed_types_cite_ok(self):
        ctx = CTX + [{"type": "product", "source": "FS368998",
                      "text_preview": "夹克 128 元"}]
        v = SentenceVerifier(ctx, user_query="夹克多少钱")
        v.feed("这件夹克 128 元，现货 48 小时内发出[3]。")
        self.assertFalse(v.flagged, f"合法引用被判越界: {v.reasons}")


class TestPolicyNumbersAllowed(unittest.TestCase):
    """店铺常驻政策数字（7 天无理由 / 15 天 / 运费险 12 元）不该被当成编造。

    问尺码时召回的是尺码指南，退换货政策通常不在 contexts 里，
    但提示词要求收尾补一句试穿邀请——按「资料里没有 7」拦掉的话，
    用户问「穿什么码」会看到「这块资料我没查准」（实测已发生）。
    """

    def test_seven_day_return_sentence_passes(self):
        v = SentenceVerifier(CTX, user_query="身高165体重55公斤穿什么码")
        v.feed("本店支持 7 天无理由退换，同款换码的往返运费由商家承担、下单自动赠运费险[1]。")
        self.assertFalse(v.flagged, f"政策数字被误杀: {v.reasons}")

    def test_arbitrary_number_still_blocked(self):
        # 政策句里出现不属于政策事实的数字，仍要拦
        v = SentenceVerifier(CTX, user_query="能退吗")
        v.feed("本店支持 30 天无理由退换[1]。")
        self.assertTrue(v.flagged, "30 天退换不是店铺政策，应被拦住")
        self.assertIn("30", v.dropped[0]["reason"])

    def test_non_policy_sentence_seven_blocked(self):
        # 非政策句里的 7 不放行（避免政策白名单被滥用）
        v = SentenceVerifier(CTX, user_query="夹克多重")
        v.feed("这件夹克重 7 克[1]。")
        self.assertTrue(v.flagged)


if __name__ == "__main__":
    unittest.main()
