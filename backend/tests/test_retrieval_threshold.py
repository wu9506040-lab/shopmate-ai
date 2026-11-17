"""检索阈值分档单测（unittest，无 pytest 依赖）。

覆盖：
1. 意图判定（政策 / 商品事实 / 闲聊 / 泛问）
2. 一句话里同时有政策词和尺码词 → 取更严的政策档
3. 分档开关关闭 → 回落 0.40 基线
4. 空召回重试降档，且不低于下限
5. 阈值真的传到了 vector_search（不是只算不用）

运行：cd backend && .venv/Scripts/python.exe -m unittest tests.test_retrieval_threshold -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config
from app.services import retrieval_threshold as rt


class TestClassify(unittest.TestCase):
    def test_policy_queries(self):
        for q in [
            "七天无理由退货怎么弄",
            "退货运费谁出",
            "什么时候发货",
            "能开发票吗",
            "双11满减还有吗",
        ]:
            self.assertEqual(rt.classify(q), rt.TIER_POLICY, q)

    def test_fact_queries(self):
        for q in [
            "这件夹克尺码怎么选",
            "面料成分是什么",
            "能机洗吗",
            "衣长多少",
            "这个颜色有色差吗",
        ]:
            self.assertEqual(rt.classify(q), rt.TIER_FACT, q)

    def test_chitchat_queries(self):
        for q in ["你好", "在吗", "谢谢啦", "收到", "嗯嗯"]:
            self.assertEqual(rt.classify(q), rt.TIER_CHITCHAT, q)

    def test_default_tier(self):
        # 没有明确关键词的开放问法 → 基线档，不擅自放宽也不收紧
        self.assertEqual(rt.classify("这个怎么样"), rt.TIER_DEFAULT)
        self.assertEqual(rt.classify("帮我看看这件"), rt.TIER_DEFAULT)

    def test_mixed_prefers_policy(self):
        # 政策答错代价更高，一句话里混了尺码也按政策档处理
        q = "这件夹克尺码偏大吗，退货运费怎么算"
        self.assertEqual(rt.classify(q), rt.TIER_POLICY)

    def test_empty_query(self):
        self.assertEqual(rt.classify(""), rt.TIER_DEFAULT)
        self.assertEqual(rt.classify("   "), rt.TIER_DEFAULT)


class TestThresholdValue(unittest.TestCase):
    def test_tiers_are_ordered(self):
        self.assertGreater(
            rt.threshold_for(rt.TIER_POLICY), rt.threshold_for(rt.TIER_FACT)
        )
        self.assertGreater(
            rt.threshold_for(rt.TIER_FACT), rt.threshold_for(rt.TIER_DEFAULT)
        )
        self.assertGreater(
            rt.threshold_for(rt.TIER_DEFAULT), rt.threshold_for(rt.TIER_CHITCHAT)
        )

    def test_disabled_falls_back_to_default(self):
        old = config.settings.RETRIEVAL_THRESHOLD_ENABLED
        config.settings.RETRIEVAL_THRESHOLD_ENABLED = False
        try:
            for tier in (rt.TIER_POLICY, rt.TIER_FACT, rt.TIER_CHITCHAT):
                self.assertEqual(
                    rt.threshold_for(tier),
                    config.settings.RETRIEVAL_THRESHOLD_DEFAULT,
                )
        finally:
            config.settings.RETRIEVAL_THRESHOLD_ENABLED = old

    def test_relax_lowers_but_not_below_floor(self):
        thr, tier = rt.resolve("退货怎么弄")
        relaxed, _ = rt.resolve(
            "退货怎么弄", relax=config.settings.RETRIEVAL_THRESHOLD_RETRY_RELAX
        )
        self.assertEqual(tier, rt.TIER_POLICY)
        self.assertLess(relaxed, thr)
        self.assertGreaterEqual(relaxed, config.settings.RETRIEVAL_THRESHOLD_FLOOR)

        huge, _ = rt.resolve("退货怎么弄", relax=999)
        self.assertEqual(huge, config.settings.RETRIEVAL_THRESHOLD_FLOOR)

    def test_resolve_does_not_touch_query(self):
        q = "退货运费怎么算"
        rt.resolve(q)
        self.assertEqual(q, "退货运费怎么算")


class TestThresholdReachesVectorSearch(unittest.TestCase):
    """阈值必须真的作用在向量检索上，而不是只算一个数放着。"""

    @staticmethod
    def _run(query: str, relax: float = 0.0) -> float:
        from app.services.policy_service import PolicyService

        old_hybrid = config.settings.USE_HYBRID_BM25
        old_rerank = config.settings.USE_RERANK
        config.settings.USE_HYBRID_BM25 = False
        config.settings.USE_RERANK = False
        try:
            with patch(
                "app.services.policy_service.vector_search", return_value=[]
            ) as mock_vec, patch(
                "app.core.providers.embedding.get_embedding_provider",
                return_value=MagicMock(embed_text=MagicMock(return_value=[0.0] * 8)),
            ):
                PolicyService.search_policy(query, top_k=3, threshold_relax=relax)
                return mock_vec.call_args.kwargs["score_threshold"]
        finally:
            config.settings.USE_HYBRID_BM25 = old_hybrid
            config.settings.USE_RERANK = old_rerank

    def test_policy_threshold_applied(self):
        self.assertEqual(
            self._run("退货运费谁承担"),
            round(config.settings.RETRIEVAL_THRESHOLD_POLICY, 3),
        )

    def test_fact_threshold_applied(self):
        self.assertEqual(
            self._run("这件尺码怎么选"),
            round(config.settings.RETRIEVAL_THRESHOLD_FACT, 3),
        )

    def test_chitchat_threshold_applied(self):
        self.assertEqual(
            self._run("你好"),
            round(config.settings.RETRIEVAL_THRESHOLD_CHITCHAT, 3),
        )

    def test_retry_relax_applied(self):
        self.assertEqual(
            self._run("退货运费谁承担", relax=0.06),
            round(config.settings.RETRIEVAL_THRESHOLD_POLICY - 0.06, 3),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
