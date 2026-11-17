"""知识库版本选择 单测（纯函数 + 真实版本清单）

覆盖：
1. 同主题多版本 → 取当前生效且 effective_from 最新
2. 未来生效版本不能提前当选
3. 同生效日并列 → revision 决胜（确定性）
4. 旧版即使标「长期有效」也要被过滤
5. 元数据不可用 → fail-open（不能把知识库搜空）
"""
from __future__ import annotations

import datetime as dt

from app.services.kb_version import filter_superseded, select_current_versions

TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 11, 14, 12, 0, tzinfo=TZ)


def _row(doc_key, source, *, from_ts, to_ts=0, revision="1", validity="permanent", status=1, deleted=0):
    return {
        "doc_key": doc_key, "source": source, "effective_from": from_ts,
        "effective_to": to_ts, "revision": revision, "validity": validity,
        "status": status, "deleted": deleted,
    }


def _ts(day: str) -> int:
    d = dt.date.fromisoformat(day)
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp())


def _hit(doc_key, doc_source):
    return {"payload": {"doc_key": doc_key, "doc_source": doc_source}}


def test_latest_active_version_wins():
    rows = [
        _row("service_charges", "v1.md", from_ts=_ts("2026-01-01"), revision="2026.01"),
        _row("service_charges", "v2.md", from_ts=_ts("2026-11-10"), revision="2026.11"),
        _row("service_charges", "v3.md", from_ts=_ts("2026-12-01"), revision="2026.12"),
    ]
    assert select_current_versions(rows, NOW) == {"service_charges": "v2.md"}


def test_future_permanent_version_is_not_current():
    rows = [
        _row("policy", "old.md", from_ts=_ts("2026-01-01")),
        _row("policy", "dec.md", from_ts=_ts("2026-12-01"), validity="permanent"),
    ]
    assert select_current_versions(rows, NOW) == {"policy": "old.md"}
    assert select_current_versions(rows, dt.datetime(2026, 12, 5, tzinfo=TZ)) == {"policy": "dec.md"}


def test_same_day_tie_uses_revision():
    rows = [
        _row("p", "a.md", from_ts=_ts("2026-11-01"), revision="2026.11-a"),
        _row("p", "b.md", from_ts=_ts("2026-11-01"), revision="2026.11-b"),
    ]
    assert select_current_versions(rows, NOW) == {"p": "b.md"}


def test_expired_temporary_version_ignored():
    rows = [
        _row("specs", "oct.md", from_ts=_ts("2026-10-01"), to_ts=_ts("2026-10-31"), validity="temporary"),
        _row("specs", "nov.md", from_ts=_ts("2026-11-01"), to_ts=_ts("2026-11-30"), validity="temporary"),
    ]
    assert select_current_versions(rows, NOW) == {"specs": "nov.md"}


def test_offline_documents_ignored():
    rows = [
        _row("p", "a.md", from_ts=_ts("2026-01-01"), status=0),
        _row("p", "b.md", from_ts=_ts("2026-01-01"), deleted=1),
    ]
    assert select_current_versions(rows, NOW) == {}


def test_filter_superseded_drops_old_version_chunks():
    current = {"service_charges": "v2.md"}
    hits = [
        _hit("service_charges", "v1.md"),
        _hit("service_charges", "v2.md"),
        _hit("", "legacy_no_key.md"),
    ]
    out = filter_superseded(hits, current)
    assert [h["payload"]["doc_source"] for h in out] == ["v2.md", "legacy_no_key.md"]


def test_filter_superseded_fail_open_when_version_map_missing():
    hits = [_hit("service_charges", "v1.md")]
    assert filter_superseded(hits, {}) == hits
    # doc_key 不在版本表里也放行（元数据缺失不应清空知识库）
    assert filter_superseded(hits, {"other": "x.md"}) == hits
