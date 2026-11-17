"""评测侧关键词覆盖归一化测试。

原用例测的是 `scripts/m14_validation/answer_quality.evaluate_coverage`，
该模块已随评测器重构消失（现由 `scripts/eval_seven.py` 承担七维评测）。
这里把断言重新指向**当前真实实现** `kw_normalize` / `kw_all_hit`，
锁住的行为不变：空白/标点差异不能让"同义表述"判成"没答到"，
但归一化也不能把无关文本凑成命中。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]  # backend/tests → backend → 项目根
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.eval_seven import kw_all_hit, kw_normalize  # noqa: E402


def test_agent_whitespace_variant_matches_canonical_keyword():
    """模型把"24小时"写成"24 小时"仍算命中。"""
    ok, hit = kw_all_hit(["24小时"], "我们会在 24 小时 内处理")

    assert ok is True
    assert hit == ["24小时"]


def test_reference_whitespace_variant_is_still_measurable():
    """期望文本自己带空格，也不影响判定。"""
    ok, hit = kw_all_hit(["7天无理由"], "支持7天无理由退货")

    assert ok is True
    assert hit == ["7天无理由"]


def test_newline_and_tab_variants_match():
    """换行/制表符等同空格（\\s+ 归一化）。"""
    assert kw_normalize("预计24\n小时内完成") == kw_normalize("预计24\t小时内完成")
    ok, _ = kw_all_hit(["24小时"], "预计24\n小时内完成")
    assert ok is True


def test_normalization_does_not_create_unrelated_match():
    """归一化只抹空白与标点，不能把"尽快处理"凑成"24小时"。"""
    ok, hit = kw_all_hit(["24小时"], "我们会尽快处理")

    assert ok is False
    assert hit == []


def test_empty_expected_keywords_pass_trivially():
    """没有期望词时不打分（全中且命中列表为空），与旧 coverage_rate=None 语义一致。"""
    ok, hit = kw_all_hit([], "我们会尽快处理")

    assert ok is True
    assert hit == []


def test_pipe_separator_accepts_equivalent_phrasings():
    """`发货前|尚未发货` 任一命中即通过——改的是措辞等价，不是放宽业务口径。"""
    ok, hit = kw_all_hit(["发货前|尚未发货"], "订单尚未发货，预计明天出库")

    assert ok is True
    assert hit == ["发货前|尚未发货"]


def test_date_separator_variants_match():
    """2026-11 与 2026.11 / 2026．11 视为同一写法。"""
    ok, _ = kw_all_hit(["2026-11"], "活动时间是 2026.11 期间")

    assert ok is True
