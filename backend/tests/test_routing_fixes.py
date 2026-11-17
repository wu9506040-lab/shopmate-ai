"""意图路由与出口话术的修正单测（unittest，本地 venv 可直接跑）。

锁三件事：
1. chitchat 桶的实质词否决——"你好，尺码怎么选"不再被判闲聊、只回"在的~"
2. 换款识别不再把"换货 / 换码 / 换洗"当成"换一款商品"
3. 出口话术不含反抛词（"请问您想咨询什么""换个说法再发一次"），
   不暴露内部概念（"知识库""检索"）

背景：这三点都是实测出来的真实误判，用测试锁死，避免以后改规则时回退。
"""
import json
import os
import pathlib
import unittest

os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

_BACKEND = pathlib.Path(__file__).resolve().parents[1]


class TestChitchatSubstantiveVeto(unittest.TestCase):
    """闲聊桶的实质词否决。"""

    def setUp(self):
        from app.services.intent_service import _has_substantive_demand

        self.has_demand = _has_substantive_demand

    def test_pure_greetings_have_no_demand(self):
        for q in ("你好", "你好呀", "在吗", "在不在", "谢谢", "hello", "嗯嗯", "好的"):
            self.assertFalse(self.has_demand(q), f"纯问候不该判为实质诉求: {q}")

    def test_real_questions_have_demand(self):
        for q in (
            "你好，尺码怎么选",
            "在吗 我的快递到哪了",
            "你好，请问面料是什么成分",
            "在吗？这个能便宜点吗",
            "七天无理由退货怎么操作",
            "这件起球了",
        ):
            self.assertTrue(self.has_demand(q), f"应识别为实质诉求: {q}")

    def test_rule_layer_does_not_swallow_greeting_prefix(self):
        """问候开头的实质问题不能再被判成 chitchat。"""
        from app.services.intent_service import IntentService

        cases = (
            "你好，我想问一下退货怎么办",
            "在吗 我的快递到哪了",
            "你好 尺码怎么选",
            "你好，请问面料是什么成分",
            "在吗？这个能便宜点吗",
        )
        for q in cases:
            result = IntentService._rule_classify(q)
            # 规则可能不命中（返回 None，交给 LLM 分类），但绝不能是 chitchat
            self.assertTrue(
                result is None or result["primary"] != "chitchat",
                f"问候前缀吞掉了实质问题: {q} -> {result}",
            )

    def test_pure_greeting_still_chitchat(self):
        from app.services.intent_service import IntentService

        for q in ("你好", "你好呀", "在吗？"):
            result = IntentService._rule_classify(q)
            self.assertIsNotNone(result, f"纯问候应命中规则: {q}")
            self.assertEqual(result["primary"], "chitchat")


class TestProductSwitchExcludesAftersale(unittest.TestCase):
    """换款候选不能吃掉售后换货 / 洗护咨询。"""

    @classmethod
    def setUpClass(cls):
        specs = json.loads(
            (_BACKEND / "config" / "product_slices.json").read_text(encoding="utf-8")
        )
        cls.model = next(iter(specs))

    def setUp(self):
        from app.services.product_switch import match_product_choices

        self.match = match_product_choices

    def test_aftersale_not_switch(self):
        for q in ("我要换货", "这件能换码吗", "怎么换洗", "换个颜色有吗", "退换怎么走"):
            self.assertIsNone(
                self.match(q, self.model), f"售后/洗护语义不该弹换款卡片: {q}"
            )

    def test_real_switch_still_works(self):
        for q in ("看看别的款", "我想换一款夹克", "这件不想要了，看看别的"):
            self.assertIsNotNone(
                self.match(q, self.model), f"真实换款意图应给候选: {q}"
            )


class TestExitPhrases(unittest.TestCase):
    """出口话术不允许反抛 / 暴露内部。"""

    # 反抛：把问题原样退回给用户；暴露内部：让用户看到实现细节
    _BAD_PHRASES = (
        "请问您想咨询什么",
        "换个说法再发一次",
        "精简后重新提问",
        "知识库中暂无",
        "无法回答",
        "未检索到",
    )

    def test_guard_responses_clean(self):
        import yaml

        rules = yaml.safe_load(
            (_BACKEND / "config" / "business_rules" / "guard.yaml").read_text(
                encoding="utf-8"
            )
        )
        for key, text in (rules.get("CHITCHAT_RESPONSES") or {}).items():
            for bad in self._BAD_PHRASES:
                self.assertNotIn(
                    bad, str(text), f"guard.yaml[{key}] 含反抛/内部措辞: {bad}"
                )

    def test_orchestrator_short_reply_clean(self):
        from app.services.chat.orchestrator import CHITCHAT_SHORT_REPLY

        for bad in self._BAD_PHRASES:
            self.assertNotIn(bad, CHITCHAT_SHORT_REPLY)

    def test_empty_fallback_clean(self):
        src = (_BACKEND / "app" / "api" / "chat.py").read_text(encoding="utf-8")
        for bad in self._BAD_PHRASES:
            self.assertNotIn(bad, src, f"chat.py 含反抛/内部措辞: {bad}")


if __name__ == "__main__":
    unittest.main()
