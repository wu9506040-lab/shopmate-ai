"""记忆契约：失败答案不当事实 / 历史剥角标 / 多轮不查缓存 / 按轮预算裁剪。

线上体检依据（2026-11-16）：
- 1300 条 assistant 里 237 条（18.2%）是兜底话术，全部无差别写进 history
- 1079 条（83%）带引用角标 [n]，角标是当轮 contexts 下标，跨轮无意义
- 缓存 key 不含 history，多轮里同一句能命中上一轮答案（答反）
- history 按 20 条全量拼接，没有长度上限：一条长答案就能把资料位挤掉
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.chat.memory_contract import (  # noqa: E402
    INVALID_ANSWER_PLACEHOLDER,
    can_use_cache,
    compact_round,
    fit_history_budget,
    is_invalid_answer,
    prepare_history_safe,
    sanitize_history,
    sanitize_history_safe,
    split_rounds,
    strip_citation_marks,
)


# ---------------------------------------------------------------
# 角标
# ---------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("参考价 128 元[3]。", "参考价 128 元。"),
    ("双11 期间享 9 折 [3][4]。", "双11 期间享 9 折。"),
    ("按质量问题退换[1][2][3]，商家承担往返运费。", "按质量问题退换，商家承担往返运费。"),
    ("[12] 长角标也要剥", "长角标也要剥"),
])
def test_strip_citation_marks(raw, expected):
    assert strip_citation_marks(raw) == expected


@pytest.mark.parametrize("raw", [
    "【已失效】《双 11 囤货节活动规则》已于 2026-11-11 结束",
    "见《退换货政策（2026-11 新政版）》第 3 节",
    "[已失效] 这条知识库原文带方括号，不是角标",
])
def test_strip_citation_keeps_text_brackets(raw):
    """只剥纯数字角标。知识库正文里的 [已失效] 是语义的一部分，剥了会改意思。"""
    assert strip_citation_marks(raw) == raw


# ---------------------------------------------------------------
# 失败答案判定
# ---------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "这块资料我没查准，建议转人工核对～",
    "系统繁忙，请稍后再试或联系人工客服。",
    "",
    "   ",
    "用户尚未登录，无法查询个人订单/退款信息。请礼貌引导用户登录后再来查询。",
])
def test_invalid_answer(text):
    assert is_invalid_answer(text) is True


@pytest.mark.parametrize("text", [
    "当前售价 128 元，双11 期间享 9 折。",
    "本店价格以商品页面实时标价为准，客服不能单独改价。",
    "在的～ 尺码、退换、物流、价格都能查，直接说就行。",
])
def test_valid_answer(text):
    assert is_invalid_answer(text) is False


# ---------------------------------------------------------------
# 历史清洗
# ---------------------------------------------------------------
def test_failed_answer_becomes_placeholder():
    history = [
        {"role": "user", "content": "什么时候发货"},
        {"role": "assistant", "content": "这块资料我没查准，建议转人工核对～"},
    ]
    cleaned, stats = sanitize_history(history)
    assert cleaned[1]["content"] == INVALID_ANSWER_PLACEHOLDER
    assert stats["invalid"] == 1
    # 买家的提问必须原样保留——哪怕是"在吗"也是信息
    assert cleaned[0]["content"] == "什么时候发货"


def test_user_message_never_replaced():
    """买家自己说"系统繁忙"不是失败答案，是他在描述现象。"""
    history = [
        {"role": "user", "content": "你们系统繁忙的时候我要等多久"},
        {"role": "assistant", "content": "一般几分钟内恢复。"},
    ]
    cleaned, stats = sanitize_history(history)
    assert cleaned[0]["content"] == "你们系统繁忙的时候我要等多久"
    assert stats["invalid"] == 0


def test_citations_stripped_from_history():
    history = [
        {"role": "user", "content": "退货运费谁出"},
        {"role": "assistant", "content": "非质量问题由买家承担[3]，质量问题商家承担[4]。"},
    ]
    cleaned, stats = sanitize_history(history)
    assert "[" not in cleaned[1]["content"]
    assert "质量问题商家承担" in cleaned[1]["content"]
    assert stats["citations"] == 1


def test_sanitize_does_not_mutate_input():
    history = [{"role": "assistant", "content": "价格 128 元[3]。"}]
    sanitize_history(history)
    assert history[0]["content"] == "价格 128 元[3]。"


def test_sanitize_handles_garbage():
    """脏数据不能把主链路带崩。"""
    assert sanitize_history(None)[0] == []
    assert sanitize_history([])[0] == []
    cleaned, stats = sanitize_history([None, "字符串", {"role": "user"}])
    assert len(cleaned) == 1 and stats["total"] == 1
    assert sanitize_history_safe(None) == []


# ---------------------------------------------------------------
# 缓存闸门
# ---------------------------------------------------------------
def test_cache_only_on_first_turn():
    assert can_use_cache([]) is True
    assert can_use_cache(None) is True
    assert can_use_cache([{"role": "user", "content": "在吗"}]) is False


# ---------------------------------------------------------------
# 预算：按轮 + 字符
# ---------------------------------------------------------------
def _dialog(rounds: int, answer_len: int = 10) -> list[dict]:
    out = []
    for i in range(rounds):
        out.append({"role": "user", "content": f"问题{i}"})
        out.append({"role": "assistant", "content": "答" * answer_len})
    return out


def test_split_rounds_pairs_user_and_assistant():
    rounds = split_rounds(_dialog(3))
    assert len(rounds) == 3
    assert [len(r) for r in rounds] == [2, 2, 2]


def test_split_rounds_isolated_leading_assistant():
    """历史从 assistant 开头（回填截断的产物）→ 单独成段，不并进下一轮"""
    rounds = split_rounds([
        {"role": "assistant", "content": "上一轮的回答"},
        {"role": "user", "content": "新的问题"},
    ])
    assert len(rounds) == 2


def test_budget_keeps_last_n_rounds():
    out, stats = fit_history_budget(_dialog(15), max_rounds=10, char_budget=99999, max_msg_chars=600)
    assert stats["rounds"] == 15
    assert stats["kept_rounds"] == 10
    assert stats["dropped_rounds"] == 5
    assert out[0]["content"] == "问题5"


def test_budget_char_limit_drops_whole_rounds():
    """按字符超限 → 整轮丢，不出现「只有 user 没有 assistant」的半轮"""
    out, stats = fit_history_budget(
        _dialog(10, answer_len=200), max_rounds=10, char_budget=500, max_msg_chars=600
    )
    assert stats["kept_rounds"] == 2  # 每轮约 204 字（4+200）
    assert [m["role"] for m in out] == ["user", "assistant"] * 2


def test_budget_always_keeps_last_round():
    """最后一轮再长也要留：本轮问题的上文全丢了，比超预算更糟"""
    out, stats = fit_history_budget(
        _dialog(3, answer_len=5000), max_rounds=10, char_budget=100, max_msg_chars=600
    )
    assert stats["kept_rounds"] == 1
    assert len(out) == 2


def test_budget_truncates_single_long_message():
    out, stats = fit_history_budget(
        _dialog(1, answer_len=2000), max_rounds=10, char_budget=1800, max_msg_chars=600
    )
    assert stats["truncated"] == 1
    assert out[-1]["content"].endswith("…")
    assert len(out[-1]["content"]) == 601


def test_budget_empty_and_garbage():
    assert fit_history_budget([]) == ([], {
        "rounds": 0, "kept_rounds": 0, "dropped_rounds": 0, "chars": 0, "truncated": 0,
        "compacted_rounds": 0, "summary_chars": 0,
    })
    assert fit_history_budget(None)[0] == []


def test_budget_uses_yaml_defaults():
    """不传参数 → 走 config/business_rules/memory.yaml（10 轮 / 1800 字）"""
    out, stats = fit_history_budget(_dialog(12))
    assert stats["kept_rounds"] == 10


def test_prepare_history_cleans_then_fits():
    """组合入口：先洗（兜底话术 → 占位、剥角标）再裁"""
    history = _dialog(12)
    history[-1]["content"] = "这块资料我没查准，请稍后再试[3]"
    out = prepare_history_safe(history)
    assert len(out) == 20
    assert out[-1]["content"] == INVALID_ANSWER_PLACEHOLDER


def test_prepare_history_never_raises(monkeypatch):
    """预算异常不该让主链路 500"""
    import app.services.chat.memory_contract as mc

    def _boom(*_a, **_k):
        raise RuntimeError("配置读不到")

    monkeypatch.setattr(mc, "fit_history_budget", _boom)
    out = prepare_history_safe(_dialog(2))
    assert len(out) == 4


# ---------------------------------------------------------------
# 被裁轮次的结构化摘要
# ---------------------------------------------------------------
def test_compact_round_picks_verifiable_lines():
    rnd = [
        {"role": "user", "content": "我预算500元，别要黑色的[3]"},
        {"role": "assistant", "content": "好的，L码运费商家承担[2]"},
    ]
    out = compact_round(rnd)
    assert "预算500元" in out
    assert "别要黑色" in out
    assert "商家承担" not in out  # 助手话术不当事实
    assert "[3]" not in out


def test_compact_round_rejects_sensitive():
    """含敏感项的分句整句不摘——宁可少记，不做"去掉手机号留下别的"这种危险裁剪。

    注意口径是「分句」不是「整句」：分句按逗号切了之后，"我预算500元" 和
    "手机号13800000000" 本来就是两条独立信息，前者保留不算危险裁剪。
    """
    rnd = [{"role": "user", "content": "我预算500元，手机号13800000000，地址北京市朝阳区"}]
    assert compact_round(rnd) == "我预算500元"
    # 同一轮里另有干净的句子时，干净的仍可摘
    rnd.append({"role": "user", "content": "我预算500元"})
    assert compact_round(rnd) == "我预算500元"


def test_compact_round_empty_for_chitchat():
    assert compact_round([{"role": "user", "content": "在吗"}]) == ""
    assert compact_round([{"role": "user", "content": "这块资料我没查准"}]) == ""
    assert compact_round([]) == ""


def test_dropped_rounds_become_summary():
    """超预算丢掉的轮次不直接蒸发：可证明的短句留在历史最前面"""
    history = _dialog(12)
    history[0]["content"] = "我预算500元，别要黑色的"
    out, stats = fit_history_budget(history)
    assert stats["dropped_rounds"] == 2
    assert stats["compacted_rounds"] == 1
    assert out[0]["role"] == "summary"
    assert "预算500元" in out[0]["content"]
    assert out[-1]["role"] == "assistant"


def test_summary_has_own_budget():
    history = _dialog(12)
    for i in range(3):
        history[i]["content"] = "我预算500元，别要黑色的"
    out, stats = fit_history_budget(history, summary_budget=20)
    assert stats["summary_chars"] <= 20
    assert out[0]["content"].endswith("…")


def test_summary_not_added_when_nothing_dropped():
    history = _dialog(3)
    history[0]["content"] = "我预算500元"
    out, stats = fit_history_budget(history)
    assert stats["dropped_rounds"] == 0
    assert all(m["role"] != "summary" for m in out)


def test_long_conversation_keeps_fact_from_first_round():
    """40 轮构造：第 1 轮的关键事实被裁掉后，仍能从摘要里找到"""
    history = _dialog(40, answer_len=30)
    history[0]["content"] = "我预算500元，不想买加绒的"
    out, stats = fit_history_budget(history)
    assert stats["rounds"] == 40
    assert stats["kept_rounds"] == 10
    assert stats["dropped_rounds"] == 30
    assert "预算500元" in out[0]["content"]
    assert "不想买加绒" in out[0]["content"]
    assert stats["chars"] < 1800 + 240  # 正文 + 摘要都在预算内
