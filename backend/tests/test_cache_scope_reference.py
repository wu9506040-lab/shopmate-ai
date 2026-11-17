"""缓存商品隔离 + 多轮指代消解 单测

覆盖 2026-11 这批改动：
  1. 缓存 key 带 scope（SKU），切商品后不能命中上一款的答案
  2. 引用快照与答案同源落库，命中时不重新检索
  3. 旧格式（纯文本）缓存读取兼容
  4. 指代消解：只补上下文、不改 query、不动"要换款"的用户
"""
import unittest
from typing import Optional
from unittest.mock import patch

from app.services import reference_resolver as ref
from app.services import response_cache as rc


class FakeRedis:
    """够用的内存版 Redis（只实现缓存层用到的方法）"""

    def __init__(self):
        self.store: dict = {}
        self.sets: dict = {}

    def get(self, k):
        return self.store.get(k)

    def setex(self, k, ttl, v):
        self.store[k] = v

    def sadd(self, k, v):
        self.sets.setdefault(k, set()).add(v)

    def smembers(self, k):
        return self.sets.get(k, set())

    def srem(self, k, v):
        self.sets.get(k, set()).discard(v)

    def scard(self, k):
        return len(self.sets.get(k, set()))

    def delete(self, k):
        self.store.pop(k, None)

    def expire(self, k, ttl):
        return True

    def pipeline(self):
        return _FakePipe(self)


class _FakePipe:
    def __init__(self, r):
        self.r = r

    def __getattr__(self, name):
        def _call(*a, **kw):
            getattr(self.r, name)(*a, **kw)
            return self

        return _call

    def execute(self):
        return []


class TestCacheScope(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRedis()
        self._p = patch.object(rc, "redis_get", return_value=self.fake)
        self._p.start()
        # semantic 层在这里不需要真 embedding，patch 掉避免联网
        self._e = patch.object(rc, "get_embedding_provider")
        self.mock_emb = self._e.start()
        self.mock_emb.return_value.embed_text.return_value = [1.0, 0.0]

    def tearDown(self):
        self._e.stop()
        self._p.stop()

    def test_scope_key_normalizes(self):
        self.assertEqual(rc._scope_key(None), "global")
        self.assertEqual(rc._scope_key(""), "global")
        self.assertEqual(rc._scope_key("FS368998-M"), "FS368998-M")
        # 冒号会被过滤，防止破坏 key 结构
        self.assertEqual(rc._scope_key("a:b"), "ab")

    def test_exact_isolated_by_scope(self):
        """同一个 user 同一句话，切到另一个 SKU 不能命中上一款的答案"""
        rc.put_cached_answer("这个会缩水吗", 7, "夹克的答案", scope="FS368998-M")
        hit_same = rc.get_cached_answer("这个会缩水吗", 7, scope="FS368998-M")
        hit_other = rc.get_cached_answer("这个会缩水吗", 7, scope="FS881698-M")
        hit_global = rc.get_cached_answer("这个会缩水吗", 7, scope=None)

        self.assertIsNotNone(hit_same)
        self.assertEqual(hit_same["answer"], "夹克的答案")
        self.assertIsNone(hit_other, "换 SKU 后不该命中上一款的缓存")
        self.assertIsNone(hit_global, "无商品上下文也不该命中某款的缓存")

    def test_same_model_different_size_isolated(self):
        """同款不同码必须隔离（这就是选 SKU 而不是款号的原因）"""
        rc.put_cached_answer("胸围多少", 8, "M 码胸围 126", scope="FS368998-M")
        self.assertIsNone(rc.get_cached_answer("胸围多少", 8, scope="FS368998-XL"))

    def test_contexts_snapshot_roundtrip(self):
        """引用快照要和答案一起存回来，命中时不用重新检索"""
        ctx = [{"source": "product_FS368998_sec2", "title": "商品参数"}]
        sc = [0.91]
        rc.put_cached_answer(
            "面料成分", 9, "聚酯纤维 100%", scope="FS368998-M", contexts=ctx, scores=sc
        )
        hit = rc.get_cached_answer("面料成分", 9, scope="FS368998-M")
        self.assertEqual(hit["contexts"], ctx)
        self.assertEqual(hit["scores"], sc)

    def test_semantic_isolated_by_scope(self):
        """语义命中（sim>0.95）也不能跨 SKU"""
        rc.put_cached_answer("这个洗了会缩水吗", 10, "夹克答案", scope="FS368998-M")
        # 同一句几乎一样的问法，换 SKU 应视为未命中
        self.assertIsNone(
            rc.get_cached_answer("这个洗了会缩水嘛", 10, scope="FS881698-M")
        )

    def test_legacy_plaintext_answer_still_readable(self):
        """改版前存的是纯文本，读完当"无引用快照"处理，不能报错"""
        key = f"{rc._EXACT_KEY_PREFIX}11:global:{rc._md5('旧问题')}"
        self.fake.store[key] = "这是旧格式的答案"
        hit = rc.get_cached_answer("旧问题", 11, scope=None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["answer"], "这是旧格式的答案")
        self.assertEqual(hit["contexts"], [])


class TestReferenceResolver(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRedis()
        self._p = patch.object(ref, "redis_get", return_value=self.fake)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_remember_and_resolve(self):
        ref.remember("s1", "FS368998", "FS368998-M")
        hit = ref.resolve("它洗了会缩水吗", "s1")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["model"], "FS368998")
        self.assertEqual(hit["sku"], "FS368998-M")

    def test_no_reference_word_no_resolve(self):
        """没有指代词就不要自作主张绑定商品"""
        ref.remember("s2", "FS368998", "FS368998-M")
        self.assertIsNone(ref.resolve("七天无理由怎么退", "s2"))

    def test_explicit_model_wins(self):
        """前端已经带了商品，指代不覆盖它"""
        ref.remember("s3", "FS368998", "FS368998-M")
        self.assertIsNone(ref.resolve("这个多少钱", "s3", model="FS881698"))

    def test_refuse_and_switch_words_skip(self):
        """用户想走 / 要换款时，不要把上一款拽回来"""
        ref.remember("s4", "FS368998", "FS368998-M")
        self.assertIsNone(ref.resolve("不要这个了，看看别的款", "s4"))
        self.assertIsNone(ref.resolve("换一款吧", "s4"))

    def test_query_contains_own_model_skips(self):
        """句子里自己写了款号，以用户写的为准"""
        ref.remember("s5", "FS368998", "FS368998-M")
        self.assertIsNone(ref.resolve("这个 FS881698 有货吗", "s5"))

    def test_empty_session_returns_none(self):
        ref.remember("s6", "FS368998", "FS368998-M")
        self.assertIsNone(ref.resolve("这个多少钱", "s6-nonexistent"))

    def test_resolve_does_not_mutate_query(self):
        """硬约束：指代补全只动上下文，问题原文一个字不改"""
        ref.remember("s7", "FS368998", "FS368998-M")
        q = "它洗了会缩水吗"
        before = q
        ref.resolve(q, "s7")
        self.assertEqual(q, before)


if __name__ == "__main__":
    unittest.main()
