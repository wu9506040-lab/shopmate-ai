"""质检抽样单测 —— 锁住「可复现」「小桶不漏」「长答案多抽」三件事。"""
import unittest

from app.services.qa import sampler


def _rows(n=100, bucket="policy_query|ai|long"):
    return [{"id": i, "bucket": bucket} for i in range(n)]


class QaSamplerTest(unittest.TestCase):
    def test_bucket_key(self):
        self.assertEqual(sampler.bucket_key("policy_query", 50), "policy_query|ai|short")
        self.assertEqual(sampler.bucket_key(None, 200, handoff=True), "unknown|handoff|mid")
        self.assertEqual(sampler.bucket_key("faq", 900), "faq|ai|long")

    def test_stable_across_runs(self):
        a = sampler.select(_rows(), ratio=0.1, seed="batch-1")
        b = sampler.select(_rows(), ratio=0.1, seed="batch-1")
        self.assertEqual(a, b)

    def test_ratio_roughly_honored(self):
        picked = sampler.select(_rows(200), ratio=0.1, seed="x", min_per_bucket=0)
        self.assertTrue(15 <= len(picked) <= 30, f"抽中 {len(picked)} 条，偏离 10% 太多")

    def test_min_per_bucket_covers_small_bucket(self):
        rows = (_rows(50, "policy_query|ai|long")
                + [{"id": 900, "bucket": "refund_query|handoff|short"}])
        picked = sampler.select(rows, ratio=0.0, seed="x", min_per_bucket=1)
        self.assertIn(900, picked, "冷门桶必须被抽到，否则小流量意图永远看不到问题")

    def test_max_total_caps(self):
        picked = sampler.select(_rows(500), ratio=1.0, seed="x", max_total=20)
        self.assertEqual(len(picked), 20)

    def test_empty(self):
        self.assertEqual(sampler.select([], ratio=0.1), [])


if __name__ == "__main__":
    unittest.main()
