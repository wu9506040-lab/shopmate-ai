"""guard L2 灰区豁免词 × P0 威胁词 的回归测试

两处缺陷都来自 2026-11-17 的多轮压测（60 条真实对话），不是推演出来的：

1. 「七天无理由怎么算」「你们的三包政策」在**首轮**被判 off_domain，直接回欢迎语。
   词表里只有单字"退"，没有"无理由/三包/售后"，灰区否决（无领域词 + 无诉求词）
   把它们当闲聊拒掉。更隐蔽的是：会话一旦有历史就走多轮阈值放行 ——
   即"越熟的买家越能问，新客反而问不到"，问题只在首轮暴露。

2. 「……不然我去你公司门口」未命中任何 P0 词，走普通退款流程回一句"没有订单"，
   威胁话术完全没被接收。词表里只有 12315/工商局/曝光 这类"已经去举报"的写法，
   缺"要上门闹"这类前置威胁。

同时锁住防误伤：「能上门取件吗」「这件衣服颜色挺热闹」不得被判 P0。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://u:p@localhost:3306/db?charset=utf8mb4")
os.environ.setdefault("QWEN_API_KEY", "sk-test-fake-key")


def _guard():
    from app.services.guard import InputGuard

    return InputGuard()


class TestAfterSalesPolicyNotBlocked:
    """售后规则类问句：首轮必须放行"""

    @pytest.mark.parametrize("query", [
        "七天无理由怎么算",
        "你们的三包政策",
        "七天无理由退货有什么条件",
        "售后怎么联系",
    ])
    def test_first_turn_allowed(self, query):
        """首轮（has_history=False）不得判 off_domain"""
        r = _guard().check(query, user_id=7000001, has_history=False)
        assert r.allowed, f"{query!r} 被判 {r.reason}（首轮不该拦售后规则问句）"

    def test_words_present_in_both_tables(self):
        """两张表都要有：只有 DOMAIN_WORDS 会漏「怎么算」这类无名词句"""
        from app.services.guard import _DOMAIN_WORDS
        from app.services.keyword_sets import SUBSTANTIVE_KEYWORDS

        for w in ("无理由", "三包", "售后"):
            assert w in _DOMAIN_WORDS, f"DOMAIN_WORDS 缺 {w}"
            assert w in SUBSTANTIVE_KEYWORDS, f"SUBSTANTIVE_KEYWORDS 缺 {w}"


class TestThreatEscalation:
    """上门/闹事型威胁：必须进 P0 投诉"""

    @pytest.mark.parametrize("query,keyword", [
        ("我不管，今天必须给我退款，不然我去你公司门口", "去你公司"),
        ("不解决我就去店里闹", "去店里"),
        ("你再拖我就堵门了", "堵门"),
    ])
    def test_threat_hits_complaint(self, query, keyword):
        from app.services.escalation_service import detect_p0_escalate

        hit = detect_p0_escalate(query)
        assert hit is not None, f"{query!r} 未命中 P0"
        assert hit[0] == "complaint", f"应归 complaint，实际 {hit[0]}"
        assert hit[1] == keyword, f"命中词应为 {keyword}，实际 {hit[1]}"

    @pytest.mark.parametrize("query", [
        "能上门取件吗",          # 正常售后：快递上门取件
        "这件衣服颜色挺热闹",     # 裸词「闹」误伤样本
        "节假日热闹吗",
        "七天无理由退货运费谁出",
    ])
    def test_no_false_positive(self, query):
        from app.services.escalation_service import detect_p0_escalate

        assert detect_p0_escalate(query) is None, f"{query!r} 被误判 P0"
