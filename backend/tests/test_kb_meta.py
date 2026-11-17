"""知识库元信息（标签 + 有效期）单测

覆盖重点（都是会真实出事的分支）：
1. 老条目没标有效期 → 必须按长期保留（否则一批历史政策直接查不到）
2. 临时政策在有效期内 → 可引用，且提示"剩 N 天"
3. 临时政策已过期 → 不可引用（这是本功能的核心：宁可答不出，不能答过期）
4. 未到生效日 → 不可引用（双 12 预告不该当现行规则）
5. 截止日当天仍有效（含当天 23:59）
6. 脏日期格式（2026/11/1、2026.11.1）→ 解析成功
7. 只写了 effective_to 没写 validity → 推断为临时
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.services.kb_meta import now_local, tags_of, validity_label, validity_of

TZ = dt.timezone(dt.timedelta(hours=8))


def _at(*args) -> dt.datetime:
    return dt.datetime(*args, tzinfo=TZ)


def test_legacy_chunk_without_meta_is_permanent():
    """历史条目（无 validity / effective_to）→ 长期有效，必须保留"""
    v = validity_of({}, now=_at(2026, 11, 14))
    assert v["kind"] == "permanent"
    assert v["active"] is True
    assert "长期" in v["label"]


def test_permanent_with_start_only():
    v = validity_of({"validity": "permanent", "effective_from": "2026-01-01"}, now=_at(2026, 11, 14))
    assert v["active"] is True
    assert v["kind"] == "permanent"


def test_temporary_active_counts_days_left():
    """双 11 活动 11-30 截止，11-14 查询 → 有效，剩 16 天"""
    payload = {
        "validity": "temporary",
        "effective_from": "2026-11-01",
        "effective_to": "2026-11-30",
    }
    v = validity_of(payload, now=_at(2026, 11, 14, 10, 0))
    assert v["active"] is True
    assert v["kind"] == "temporary"
    assert v["days_left"] == 16
    assert "2026-11-01" in v["label"] and "2026-11-30" in v["label"]
    assert "剩 16 天" in v["label"]


def test_temporary_expired_is_inactive():
    """国庆活动 10-07 截止，11-14 查询 → 已过期，不进候选"""
    payload = {
        "validity": "temporary",
        "effective_from": "2026-09-25",
        "effective_to": "2026-10-07",
    }
    v = validity_of(payload, now=_at(2026, 11, 14))
    assert v["active"] is False
    assert v["kind"] == "expired"
    assert "已过期" in v["label"]


def test_last_day_still_active():
    """截止日当天 23:59 仍算有效（含当天，避免最后一天提前失效）"""
    payload = {"validity": "temporary", "effective_to": "2026-11-30"}
    assert validity_of(payload, now=_at(2026, 11, 30, 23, 59))["active"] is True
    assert validity_of(payload, now=_at(2026, 12, 1, 0, 1))["active"] is False


def test_not_started_is_inactive():
    """未到生效日 → 不可引用（避免把预告当现行政策）"""
    payload = {"validity": "temporary", "effective_from": "2026-12-01", "effective_to": "2026-12-31"}
    v = validity_of(payload, now=_at(2026, 11, 14))
    assert v["active"] is False
    assert v["kind"] == "scheduled"
    assert "尚未开始" in v["label"]


def test_dirty_date_format_tolerated():
    """2026/11/1、2026.11.1 这类手写格式要能解析（运营填表不规范是常态）"""
    # 用未来截止日，避免把"格式解析"和"是否过期"两件事混在一起断言
    for raw in ("2026/12/31", "2026.12.31", "2026-12-31"):
        v = validity_of({"validity": "temporary", "effective_to": raw}, now=_at(2026, 11, 14))
        assert v["active"] is True, raw
        assert v["effective_to"] is not None, raw


def test_infer_temporary_when_only_end_date():
    """只填了截止日没填 validity → 按临时处理（有截止日就是会过期）"""
    v = validity_of({"effective_to": "2026-10-07"}, now=_at(2026, 11, 14))
    assert v["validity"] == "temporary"
    assert v["active"] is False


def test_invalid_date_not_filtered_out():
    """脏到解析不了的日期 → 按未标注处理（不因脏数据把条目全过滤掉）"""
    v = validity_of({"validity": "temporary", "effective_to": "待定"}, now=_at(2026, 11, 14))
    assert v["effective_to"] is None
    assert v["active"] is True


def test_tags_of_parses_comma_string():
    assert tags_of({"tags": "退换, 三包，压缩机"}) == ["退换", "三包", "压缩机"]
    assert tags_of({"tags": ["a", " b "]}) == ["a", "b"]
    assert tags_of({}) == []


def test_validity_label_helper():
    assert validity_label({"validity": "permanent"}, now=_at(2026, 11, 14)) == "长期有效"


def test_now_local_is_east8():
    """now_local 固定东八区（容器 TZ 常为 UTC，不能用本地时区判断截止日）"""
    n = now_local()
    assert n.utcoffset() == dt.timedelta(hours=8)


def test_permanent_doc_with_future_start_is_not_active():
    """长期政策也可能有未来生效日（12 月新版收费标准），生效前不可当现行版本"""
    payload = {"validity": "permanent", "effective_from": "2026-12-01"}
    v = validity_of(payload, now=_at(2026, 11, 14))
    assert v["active"] is False
    assert v["kind"] == "scheduled"
    assert validity_of(payload, now=_at(2026, 12, 2))["active"] is True
