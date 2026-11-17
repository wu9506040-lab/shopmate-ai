"""质检编排 —— 全量规则 + 分桶抽样 + LLM 打分 + 统计 + 转 badcase。

口径（看板上的数必须能解释给人听）：
- 分母只算「AI 真答了」的轮次（有 assistant 消息），不含用户只发「在吗」的比例争议；
  转人工的轮次单独按 handoff 桶统计，不混进 AI 合格率；
- 合格 = 规则层零命中 **且** 四维最低分 ≥ 4；低分 = 规则放行但某一维 ≤ 3；
- 规则命中的直接 blocked，不打分——那是确定性缺陷，不需要模型再表态。
"""
import datetime as dt
import json
import logging
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.message import Message
from app.models.qa_result import (
    REVIEW_ACCEPTED, SOURCE_LLM, SOURCE_RULE, VERDICT_BLOCKED, VERDICT_LOW,
    VERDICT_PASS, QaResult,
)
from app.services.qa import judge as judge_mod
from app.services.qa import rules, sampler
from app.services.qa.redact import redact
from app.services.session_kind import non_biz_session_ids

logger = logging.getLogger(__name__)

MIN_PASS_DIM = 3  # 3 分=可接受；<=2 才进人工队列（5 分制下 4 分门槛会把「缺资料」误判成差）


def handoff_of(row: QaResult) -> bool:
    """这轮是否走了转人工（note 前缀标记，避免为此单开一列）。"""
    return (row.note or "").startswith("handoff=True")


def _prev_user_msg(db: Session, msg: Message) -> Optional[Message]:
    return db.execute(
        select(Message).where(
            Message.session_id == msg.session_id,
            Message.role == "user",
            Message.id < msg.id,
            Message.deleted == 0,
        ).order_by(Message.id.desc()).limit(1)
    ).scalar_one_or_none()


def _load_dropped(since_hours: int) -> Dict[Tuple[str, str], List[str]]:
    """从 trace 里捞「被句级自检替换掉的原句」，供规则层复用。

    不重写判据的原因：自检已经判过一遍「这句是不是幻觉」，质检再判一次必然出现
    「自检拦了、质检说没问题」的两套真相。这里只取结论。
    读不到（trace 关闭/文件缺失）就返回空——质检不能因为附加信息缺失而跑不起来。
    """
    out: Dict[Tuple[str, str], List[str]] = {}
    try:
        base = Path(getattr(settings, "AGENT_TRACE_DIR", "data/agent_traces"))
        if not base.exists():
            return out
        cutoff = dt.datetime.now().timestamp() - since_hours * 3600
        for f in sorted(base.glob("trace-*.jsonl"))[-3:]:
            if f.stat().st_mtime < cutoff:
                continue
            for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                dropped = d.get("answer_dropped") or []
                if dropped:
                    key = (str(d.get("session_id") or ""), str(d.get("query") or "")[:40])
                    out[key] = [str(x.get("sentence") or "") for x in dropped if isinstance(x, dict)]
    except Exception as e:  # 附加信息，失败不影响主流程
        logger.warning("质检读取 trace 失败（忽略）: %s", e)
    return out


def _ctx_summary(msg: Message) -> str:
    ctxs = msg.contexts or []
    parts = []
    for c in ctxs[:3]:
        if isinstance(c, dict):
            parts.append(str(c.get("text") or c.get("content") or ""))
        else:
            parts.append(str(c))
    return " / ".join(p for p in parts if p)[:600]


def run_batch(
    db: Session,
    *,
    since_hours: int = 24,
    limit: Optional[int] = None,
    ratio: Optional[float] = None,
    use_llm: bool = True,
    provider: Any = None,
) -> Dict[str, Any]:
    """跑一轮质检。返回本轮计数（给 API 直接回显，不做二次查询）。"""
    limit = limit or int(getattr(settings, "QA_BATCH_LIMIT", 500))
    ratio = float(getattr(settings, "QA_SAMPLE_RATIO", 0.1)) if ratio is None else ratio
    batch_id = dt.datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
    cutoff = dt.datetime.now() - dt.timedelta(hours=since_hours)

    msgs = db.execute(
        select(Message).where(
            Message.role == "assistant",
            Message.deleted == 0,
            Message.create_time >= cutoff,
            # 质检分母只算真实业务会话：评测/排障会话的答案本来就不该进质检队列
            Message.session_id.notin_(non_biz_session_ids()),
        ).order_by(Message.id.desc()).limit(limit)
    ).scalars().all()
    if not msgs:
        return {"batch_id": batch_id, "scanned": 0, "blocked": 0, "sampled": 0,
                "judged": 0, "low": 0, "calibrated": None}

    done = set(db.execute(
        select(QaResult.message_id).where(QaResult.message_id.in_([m.id for m in msgs]))
    ).scalars().all())
    dropped_map = _load_dropped(since_hours)

    rows: List[QaResult] = []
    blocked = 0
    for m in msgs:
        if m.id in done:
            continue
        user_msg = _prev_user_msg(db, m)
        q = (user_msg.content if user_msg else "") or ""
        snap = m.intent_snapshot or {}
        intent = snap.get("primary") if isinstance(snap, dict) else None
        handoff = bool(snap.get("handoff")) if isinstance(snap, dict) else False
        hits = rules.check(
            m.content or "",
            intent=intent,
            dropped=dropped_map.get((m.session_id, q[:40])),
            min_chars=int(getattr(settings, "QA_MIN_ANSWER_CHARS", 4)),
        )
        if hits:
            blocked += 1
        row = QaResult(
            batch_id=batch_id, session_id=m.session_id, message_id=m.id,
            user_id=m.user_id or 0,
            query=redact(q, 400), answer=redact(m.content or "", 1200),
            intent=intent, rule_hits=hits or None,
            failure_layer=rules.worst_layer(hits), dims=None, score=None,
            verdict=VERDICT_BLOCKED if hits else VERDICT_PASS,
            source=SOURCE_RULE, calibrated=True,
            note=("handoff=True | " if handoff else None),
            latency_ms=m.latency_ms,
        )
        db.add(row)
        rows.append(row)
    db.flush()

    clean = [r for r in rows if r.verdict == VERDICT_PASS]
    buckets = [{"id": r.id, "bucket": sampler.bucket_key(r.intent, len(r.answer or ""))}
               for r in clean]
    picked = set(sampler.select(buckets, ratio=ratio, seed=batch_id))

    judged = low = 0
    calibrated: Optional[bool] = None
    if use_llm and picked:
        if provider is None:
            from app.core.providers.llm import get_llm_provider
            provider = get_llm_provider()
        calibrated, bad_avg = judge_mod.calibrate(provider)
        if not calibrated:
            logger.warning("质检批次 %s 未通过校准（坏答案均分 %s），本批分数不可信", batch_id, bad_avg)
        for r in clean:
            if r.id not in picked:
                continue
            msg = next((m for m in msgs if m.id == r.message_id), None)
            d = judge_mod.judge_one(provider, r.query, r.answer,
                                    _ctx_summary(msg) if msg else "")
            if d is None:
                continue
            r.source = SOURCE_LLM
            r.dims = {k: d[k] for k in judge_mod.DIMS}
            r.score = judge_mod.score_of(d)
            r.calibrated = calibrated
            prefix = "handoff=True | " if handoff_of(r) else ""
            r.note = (prefix + (d.get("reason") or ""))[:500] or None
            if d.get("failure") and d["failure"] != "none":
                r.failure_layer = str(d["failure"])[:24]
            if judge_mod.worst_dim(d) >= MIN_PASS_DIM:
                r.verdict = VERDICT_PASS
            else:
                r.verdict = VERDICT_LOW
                low += 1
            judged += 1

    db.commit()
    return {"batch_id": batch_id, "scanned": len(rows), "blocked": blocked,
            "sampled": len(picked), "judged": judged, "low": low,
            "calibrated": calibrated}


def stats(db: Session, *, days: int = 7) -> Dict[str, Any]:
    since = dt.datetime.now() - dt.timedelta(days=days)
    base = select(QaResult).where(QaResult.deleted == 0, QaResult.create_time >= since)
    rows = db.execute(base).scalars().all()

    total = len(rows)
    c = Counter(r.verdict for r in rows)
    defects: Counter = Counter()
    for r in rows:
        for h in (r.rule_hits or []):
            defects[str(h)] += 1
        if r.verdict == VERDICT_LOW and r.failure_layer:
            defects[f"llm:{r.failure_layer}"] += 1
    by_day: Dict[str, Counter] = {}
    for r in rows:
        day = (r.create_time or dt.datetime.now()).strftime("%m-%d")
        by_day.setdefault(day, Counter())[r.verdict] += 1

    # 分母：只算 AI 真作答的轮次。转人工的答案单独统计，不混进合格率。
    ai_rows = [r for r in rows if handoff_of(r) is False]
    ai_total = len(ai_rows) or 1
    ai_pass = sum(1 for r in ai_rows if r.verdict == VERDICT_PASS)
    return {
        "window_days": days,
        "total": total,
        "pass": c.get(VERDICT_PASS, 0),
        "low": c.get(VERDICT_LOW, 0),
        "blocked": c.get(VERDICT_BLOCKED, 0),
        "pass_rate": round(ai_pass / ai_total, 3) if ai_rows else None,
        "low_rate": round(c.get(VERDICT_LOW, 0) / ai_total, 3) if ai_rows else None,
        "uncalibrated": sum(1 for r in rows if not r.calibrated),
        "pending_review": sum(1 for r in rows
                              if r.verdict == VERDICT_LOW and r.review_status != REVIEW_ACCEPTED),
        "top_defects": [{"code": k, "count": v} for k, v in defects.most_common(5)],
        "by_day": [{"date": d, **{k: v2 for k, v2 in cnt.items()}}
                   for d, cnt in sorted(by_day.items())],
    }


def list_results(db: Session, *, verdict: Optional[str] = None, limit: int = 50,
                 offset: int = 0) -> Tuple[List[QaResult], int]:
    q = select(QaResult).where(QaResult.deleted == 0)
    if verdict:
        q = q.where(QaResult.verdict == verdict)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar() or 0
    rows = db.execute(q.order_by(QaResult.id.desc()).limit(limit).offset(offset)).scalars().all()
    return rows, total


def to_badcase(db: Session, qa_id: int, admin_id: int = 0) -> Dict[str, Any]:
    """低分会话一键入 badcase 池（signal=qa_low）。

    复用 enqueue 的 (query_hash, signal) 唯一约束：同一问句被 👎 和质检各命中一次时
    会 bump hit_count 而不是建两行——重复建行会让「问题复发次数」这个信号失真。
    """
    from app.services import badcase_service

    row = db.get(QaResult, qa_id)
    if row is None:
        raise ValueError(f"质检记录 {qa_id} 不存在")
    res = badcase_service.enqueue(
        db, signal="qa_low", query=row.query, session_id=row.session_id,
        message_id=row.message_id, user_id=row.user_id,
        snapshot={"qa_score": row.score, "dims": row.dims, "rule_hits": row.rule_hits,
                  "failure_layer": row.failure_layer, "answer": (row.answer or "")[:200],
                  "note": row.note},
    )
    row.review_status = REVIEW_ACCEPTED
    db.commit()
    return {"qa_id": qa_id, "badcase": res}
