"""质检规则层单测 —— 只锁「该拦必须拦、不该拦必须放」两类。

这个文件的存在理由：规则层一旦误杀，好答案会被算成 blocked，合格率长期偏低，
运营会开始不信看板；反过来漏拦就是线上事故进不了池子。两侧都要钉死。
"""
import unittest

from app.services.qa import rules


class QaRulesTest(unittest.TestCase):
    def test_empty_answer_blocked(self):
        self.assertEqual(rules.check("好的呢～"), ["empty_answer"])
        self.assertEqual(rules.check(""), ["empty_answer"])
        self.assertEqual(rules.check("   。。。 "), ["empty_answer"])

    def test_normal_answer_passes(self):
        ans = ("M 码：身高 160~170cm、体重 95~110 斤都能穿，您 165cm/55kg 正好在范围内[1]。"
               "想宽松可以选 L。7 天无理由，换码往返运费我们承担。")
        self.assertEqual(rules.check(ans, intent="policy_query"), [])

    def test_fallback_phrase_blocked(self):
        self.assertIn("fallback_phrase", rules.check("这块资料我没查准，建议转人工核对～"))
        self.assertIn("fallback_phrase", rules.check("系统繁忙，请稍后再试"))

    def test_self_check_dropped_is_reused_not_rewritten(self):
        # 规则层不重新判幻觉，只认自检的结论
        hits = rules.check("运费我们承担，7 天内可退[1]。", dropped=["您的订单 ORD123 已发货"])
        self.assertEqual(hits, ["self_check_dropped"])

    def test_truncated_detected(self):
        long_no_end = "这件加绒夹克的成分是基布 92.7% 聚酯纤维加 7.3% 其他纤维，涂层聚氨酯 PU，" * 6
        self.assertIn("truncated", rules.check(long_no_end))

    def test_short_answer_not_treated_as_truncated(self):
        self.assertNotIn("truncated", rules.check("是 128 元"))

    def test_promise_language_blocked(self):
        self.assertIn("promise_language", rules.check("放心，我们保证能退，还赔您 200 元。"))
        self.assertIn("promise_language", rules.check("这件我给您补 50 块差价。"))

    def test_policy_text_with_insurance_is_not_promise(self):
        # 真实误杀回归：KB 原文写「运费险赔付（最高 12 元）」，首版按词拦了 34 条
        ans = ("根据 2026.11 政策：非质量问题退换商家承担运费，下单自动赠送运费险，"
               "退货运费由运费险赔付（最高 12 元）[1]。同款换码往返运费也由商家承担[2]。")
        self.assertNotIn("promise_language", rules.check(ans, intent="policy_query"))

    def test_no_citation_only_for_policy_intent(self):
        ans = ("退换货按现行政策执行：自签收之日起 7 天内可申请无理由退换，需吊牌完整未剪、未洗涤；"
               "质量问题 15 天内商家承担往返运费；退款签收后 24 小时内审核，1-3 个工作日到账。")
        self.assertIn("no_citation", rules.check(ans, intent="policy_query"))
        self.assertNotIn("no_citation", rules.check(ans, intent="chitchat"))

    def test_worst_layer_priority(self):
        self.assertEqual(rules.worst_layer(["no_citation", "empty_answer"]), "generation")
        self.assertEqual(rules.worst_layer(["no_citation"]), "retrieval")
        self.assertIsNone(rules.worst_layer([]))


if __name__ == "__main__":
    unittest.main()
