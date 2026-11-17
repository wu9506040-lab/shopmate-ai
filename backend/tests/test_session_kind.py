"""session_kind：测试流量分类的唯一真相源

锁死三件事，都是这条链路上真出过问题的地方：
1. 请求头优先（脚本自己声明），没声明才用前缀正则兜底；
2. 只升不降——首轮声明 eval 后，后续轮次不带这个头也不能被改回 biz；
3. 未知声明值不报错，退回兜底逻辑（脚本写 `X-Traffic-Kind: benchmark` 不该 500）。
"""
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.session_meta import KIND_BIZ, KIND_DEBUG, KIND_EVAL, KIND_SMOKE, SessionMeta
from app.services.session_kind import ensure_session_kind, infer_kind


class TestInferKind(unittest.TestCase):
    def test_header_wins_over_prefix(self):
        self.assertEqual(infer_kind("eval7-anything", "biz"), (KIND_BIZ, "header"))

    def test_header_alias_normalized(self):
        self.assertEqual(infer_kind("s-1", "EVAL"), (KIND_EVAL, "header"))
        self.assertEqual(infer_kind("s-1", "smoke"), (KIND_SMOKE, "header"))
        self.assertEqual(infer_kind("s-1", "diag"), (KIND_DEBUG, "header"))

    def test_prefix_fallback_when_header_missing(self):
        self.assertEqual(infer_kind("eval7-P01-1763", ""), (KIND_EVAL, "prefix-regex"))
        self.assertEqual(infer_kind("smoke-abc", None), (KIND_EVAL, "prefix-regex"))
        self.assertEqual(infer_kind("qa-1763", ""), (KIND_EVAL, "prefix-regex"))

    def test_business_session_defaults_to_biz(self):
        self.assertEqual(infer_kind("sess-20261116-abcd", ""), (KIND_BIZ, "default"))
        # 真实渠道前缀 xianyu- 是业务会话；只有裸 "xianyu" 在历史里是测试会话
        self.assertEqual(infer_kind("xianyu-20261116-1", ""), (KIND_BIZ, "default"))
        self.assertEqual(infer_kind("xianyu", ""), (KIND_EVAL, "prefix-regex"))

    def test_unknown_header_falls_back(self):
        # 脚本写了没见过的值：不能当成 biz 直接放过，也不能炸
        self.assertEqual(infer_kind("eval7-x", "benchmark"), (KIND_EVAL, "prefix-regex"))
        self.assertEqual(infer_kind("sess-1", "whatever"), (KIND_BIZ, "default"))


class TestEnsureSessionKind(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_insert_once_then_idempotent(self):
        self.assertEqual(ensure_session_kind("s-1", "eval", db=self.db), KIND_EVAL)
        self.assertEqual(ensure_session_kind("s-1", "eval", db=self.db), KIND_EVAL)
        rows = self.db.query(SessionMeta).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kind, KIND_EVAL)

    def test_only_upgrades_never_downgrades(self):
        # 首轮声明 eval，后续轮次前端不带这个头 → 仍然是 eval
        ensure_session_kind("s-2", "eval", db=self.db)
        self.assertEqual(ensure_session_kind("s-2", "", db=self.db), KIND_EVAL)
        # 反过来：业务会话后来带了 smoke 头 → 升级为 smoke（宁可多排，不可漏排）
        ensure_session_kind("s-3", "", db=self.db)
        self.assertEqual(ensure_session_kind("s-3", "smoke", db=self.db), KIND_SMOKE)

    def test_empty_session_id_is_noop(self):
        self.assertEqual(ensure_session_kind("", "eval", db=self.db), KIND_BIZ)
        self.assertEqual(self.db.query(SessionMeta).count(), 0)


if __name__ == "__main__":
    unittest.main()
