"""知识库条目元信息 · 标签与有效期（2026-11）

## 为什么需要

政策分两类：**长期**（尺码指南、退换货政策、洗护规范）和**临时**（大促满减、
节假日活动调整、阶段性包邮门槛）。临时政策一旦过期，如果还留在检索结果里，
AI 会把**失效政策当现行政策**答出去——这比"没有引用"更糟：它看起来有据可依，
用户会照着过期活动要求兑现，客服再解释就是二次纠纷。

## 分工

- 入库（scripts/ingest_kb.py）：把 front matter 的 doc_type / category / tags /
  validity / effective_from / effective_to 写进 chunk 的标量字段
- 检索（PolicyService）：已过期条目**不进候选**；未生效的也不进（避免"双 12 提前
  预告"被当成现行规则）；输出时附 `validity_label`
- 回答（prompt）：临时政策必须说明有效期，长期政策不必啰嗦

## 日期口径

- 只到"日"粒度，`effective_to` 含当天（23:59:59 失效）
- 时区固定 Asia/Shanghai（门店/客服都在国内；用 UTC 会导致"最后一天"提前 8 小时失效）
- 缺 `validity` 字段的历史条目按 **长期** 处理（不能因为没标就全部过滤掉）
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

# 业务时区固定东八区（不依赖宿主机 TZ，容器里常是 UTC）
_TZ = _dt.timezone(_dt.timedelta(hours=8))

PERMANENT = "permanent"
TEMPORARY = "temporary"

VALIDITY_LABELS = {
    PERMANENT: "长期有效",
    TEMPORARY: "临时政策",
}


def now_local() -> _dt.datetime:
    """当前东八区时间（测试可 monkeypatch 本函数）"""
    return _dt.datetime.now(_TZ)


def _to_ts(value: Any) -> Optional[int]:
    """日期 → 秒级时间戳（按东八区当天 00:00 或 23:59:59 由调用方决定）。

    接受：None / 空串 / int(已是秒级 ts) / datetime / "2026-11-01" / "2026/11/01"
    无法解析 → None（当作"未标注"，按长期处理，不因脏数据把政策全部过滤掉）
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):  # bool 是 int 子类，先挡掉
        return None
    if isinstance(value, (int, float)):
        # 0 / 负数 = "未标注"的哨兵值（Milvus INT64 nullable 字段缺省写入 0）。
        # 不能当时间戳用——0 会解析成 1970-01-01，让所有长期政策被判过期。
        return int(value) if value > 0 else None
    if isinstance(value, _dt.datetime):
        return int(value.replace(tzinfo=value.tzinfo or _TZ).timestamp())
    if isinstance(value, _dt.date):
        return int(_dt.datetime(value.year, value.month, value.day, tzinfo=_TZ).timestamp())
    s = str(value).strip().replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = _dt.datetime.strptime(s[: len(fmt) + 2] if "%H" in fmt else s[:10], fmt)
            return int(dt.replace(tzinfo=_TZ).timestamp())
        except ValueError:
            continue
    return None


def _end_of_day_ts(ts: Optional[int]) -> Optional[int]:
    """把"某天 00:00"推到当天 23:59:59（有效期截止日含当天）"""
    if ts is None:
        return None
    return ts + 86399


def validity_of(payload: dict, now: Optional[_dt.datetime] = None) -> dict:
    """解析一个 chunk 的有效期状态。

    Args:
        payload: Milvus chunk 的标量字段（含 validity / effective_from / effective_to）
        now: 判定基准时间（默认东八区当前时间；测试可注入）

    Returns:
        {
          "kind": "permanent" | "temporary" | "expired" | "scheduled",
          "validity": "permanent" | "temporary",
          "effective_from": int | None,
          "effective_to": int | None,
          "days_left": int | None,      # 临时且有效时给出剩余天数
          "label": str,                 # 给 prompt 用的人话
          "active": bool,               # 当前是否可被引用
        }
    """
    payload = payload or {}
    now_ts = int((now or now_local()).timestamp())

    declared = str(payload.get("validity") or "").strip().lower()
    start_ts = _to_ts(payload.get("effective_from"))
    end_ts = _end_of_day_ts(_to_ts(payload.get("effective_to")))

    # 没标 validity 时按"有没有截止日"推断：有截止日 = 临时
    validity = declared if declared in (PERMANENT, TEMPORARY) else (
        TEMPORARY if end_ts is not None else PERMANENT
    )

    base = {
        "validity": validity,
        "effective_from": start_ts,
        "effective_to": end_ts,
        "days_left": None,
    }

    # 未生效判定必须在"长期有效"快捷返回之前：长期政策也可能带未来生效日
    # （如 12 月才执行的新收费标准），不能在 11 月就当成现行版本。
    if start_ts is not None and now_ts < start_ts:
        return {
            **base,
            "kind": "scheduled",
            "label": f"{_fmt_day(start_ts)} 起生效（尚未开始）",
            "active": False,
        }

    if validity == PERMANENT and end_ts is None:
        return {**base, "kind": PERMANENT, "label": VALIDITY_LABELS[PERMANENT], "active": True}

    if end_ts is not None and now_ts > end_ts:
        return {
            **base,
            "kind": "expired",
            "label": f"已过期（有效期至 {_fmt_day(end_ts)}）",
            "active": False,
        }

    days_left = None
    if end_ts is not None:
        days_left = max(0, (_dt.datetime.fromtimestamp(end_ts, _TZ).date() - (now or now_local()).date()).days)
    period = ""
    if start_ts and end_ts:
        period = f"{_fmt_day(start_ts)} ~ {_fmt_day(end_ts)}"
    elif end_ts:
        period = f"至 {_fmt_day(end_ts)}"
    label = f"临时政策（{period}）" if period else VALIDITY_LABELS[TEMPORARY]
    if days_left is not None:
        label += f"，剩 {days_left} 天"
    return {**base, "kind": TEMPORARY, "days_left": days_left, "label": label, "active": True}


def is_active(payload: dict, now: Optional[_dt.datetime] = None) -> bool:
    """当前是否可被引用（已过期 / 未生效 → False）"""
    return bool(validity_of(payload, now).get("active"))


def validity_label(payload: dict, now: Optional[_dt.datetime] = None) -> str:
    """给 prompt 展示的一行人话"""
    return str(validity_of(payload, now).get("label") or "")


def tags_of(payload: dict) -> list[str]:
    """标签解析：入库时统一存成逗号分隔字符串（Milvus 标量字段不支持数组）。"""
    raw = (payload or {}).get("tags")
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [t.strip() for t in str(raw).replace("，", ",").split(",") if t.strip()]


def _fmt_day(ts: int) -> str:
    return _dt.datetime.fromtimestamp(ts, _TZ).strftime("%Y-%m-%d")


__all__ = [
    "PERMANENT",
    "TEMPORARY",
    "now_local",
    "validity_of",
    "is_active",
    "validity_label",
    "tags_of",
]
