"""（1.5，2026-10-10）：转人工词表分级 + guard.yaml 覆盖一致性

审计背景：原"user_requested"类把 转人工/机器人 与 起诉/律师 混在一起全判 P0，
"转人工"是路由诉求不是风险事件，霸占 P0 队列稀释真正的高风险工单
（随口一句"你是不是机器人"也升 P0）。拆分：
- user_requested（起诉/律师）= P0 法律行动
- user_transfer（转人工类）= P1 正常升级排队
词表与优先级现可被 guard.yaml ESCALATE_P0_* 覆盖，代码内置为兜底，
本文件同时锁定两处一致性（防双源漂移）。
"""
import os

os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://x:x@localhost:3306/x")

from app.services.escalation_service import (
    ESCALATE_P0_KEYWORDS,
    detect_p0_escalate,
    get_p0_category_info,
)


class TestPriorityGrading:

    def test_plain_transfer_is_p1(self):
        hit = detect_p0_escalate("转人工")
        assert hit is not None and hit[0] == "user_transfer"
        assert get_p0_category_info("user_transfer")[0] == "P1"

    def test_bot_word_no_longer_p0(self):
        """'你是不是机器人'类口语不再触发 P0（原实现中招）"""
        hit = detect_p0_escalate("你是不是机器人啊")
        assert hit is not None and hit[0] == "user_transfer"
        assert get_p0_category_info("user_transfer")[0] == "P1"

    def test_legal_threat_stays_p0(self):
        hit = detect_p0_escalate("再不处理我就起诉、找律师")
        assert hit is not None and hit[0] == "user_requested"
        assert get_p0_category_info("user_requested")[0] == "P0"

    def test_complaint_priority_over_transfer(self):
        """同时含'投诉'与'转人工'→ complaint 优先（高风险 > 路由诉求）"""
        hit = detect_p0_escalate("我要投诉你们，转人工！")
        assert hit is not None and hit[0] == "complaint"
        assert get_p0_category_info("complaint")[0] == "P0"

    def test_guard_yaml_override_consistency(self):
        """guard.yaml 是覆盖源：类别集合包含内置默认、P1 分级两处一致"""
        from app.services.config_loader import get_config_loader
        g = get_config_loader().load("guard") or {}
        yk = g.get("ESCALATE_P0_KEYWORDS") or {}
        assert yk, "guard.yaml 应携带 ESCALATE_P0_KEYWORDS 段"
        assert set(yk) <= set(ESCALATE_P0_KEYWORDS)  # 加载后服务字典是并集
        assert yk.get("user_requested") == ["起诉", "律师"]
        assert g["ESCALATE_P0_PRIORITY"]["user_transfer"] == "P1"


class TestQualityNotP0:
    """2026-11-17 审计：工单库里 71 张工单全是 P0，其中 10 张的「触发依据：质量问题」
    实际只出现在 **AI 自己的回复**里——买家原话是「退货运费谁出」，AI 答
    「非质量问题退换（如尺码不符）商家承担运费」，检测把这四个字当成了用户诉求，
    产出 P0 质量问题工单，坐席点开看不到任何投诉，工单形同虚设。

    两层修复锁定在此：
    1. 检测只认用户 query，AI 回复的措辞不得回流成触发依据；
    2. quality 降为 P1，P0 只留健康/投诉/赔付这类「答错会真出事」的类别。
    """

    def test_user_query_wording_does_not_trigger_quality(self):
        """买家问运费归属，不应被判质量问题（线上假工单的原始句子）"""
        assert detect_p0_escalate("退货运费谁出") is None

    def test_ai_reply_text_would_trigger_if_fed_in(self):
        """把 AI 回复当输入会命中 quality —— 正是必须隔离它的原因，防止有人'顺手'把
        history 或回复拼进检测输入。此断言是反例而非期望行为。"""
        ai_reply = "根据2026.11新版退换货政策：非质量问题退换（如尺码不符）商家承担运费"
        hit = detect_p0_escalate(ai_reply)
        assert hit is not None and hit[0] == "quality"

    def test_quality_still_detected_from_real_user_complaint(self):
        """用户真的说质量问题，仍要识别出来（降级不是关掉）"""
        hit = detect_p0_escalate("衣服到手发现有质量问题，开线了")
        assert hit is not None and hit[0] == "quality"

    def test_quality_priority_is_p1(self):
        assert get_p0_category_info("quality")[0] == "P1"

    def test_guard_yaml_quality_p1_consistent(self):
        """guard.yaml 与代码内置兜底必须同值，否则改一处等于没改"""
        from app.services.config_loader import get_config_loader
        g = get_config_loader().load("guard") or {}
        assert g["ESCALATE_P0_PRIORITY"]["quality"] == "P1"

    def test_p0_reserved_for_real_damage(self):
        """P0 只剩健康/投诉/赔付/法律：这三类答错会真出事"""
        assert get_p0_category_info("health")[0] == "P0"
        assert get_p0_category_info("complaint")[0] == "P0"
        assert get_p0_category_info("compensation")[0] == "P0"
