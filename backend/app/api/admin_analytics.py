"""admin 运营聚合指标 API（P4-2）。"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.clients.mysql_client import get_db
from app.models.user import User
from app.schemas.admin_analytics import AdminAnalyticsResponse
from app.services.analytics_service import AnalyticsService
from app.services.audit_service import try_log_action
from app.services import kpi_service  # WP1 业务 KPI

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/analytics",
    tags=["admin-analytics"],
)


def _client_ip(request: Request) -> Optional[str]:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def _user_agent(request: Request) -> Optional[str]:
    user_agent = request.headers.get("user-agent", "")
    return user_agent[:500] if user_agent else None


@router.get(
    "",
    response_model=AdminAnalyticsResponse,
    summary="[admin] 运营聚合指标",
    description=(
        "返回按日会话活跃度、assistant 响应延迟 P50/P95、转人工事件分布，"
        "以及当前进程最近 100 次 RAG 检索的 hit@K。强制时间窗且最大 90 天。"
    ),
)
def get_admin_analytics(
    request: Request,
    start_date: Optional[datetime] = Query(
        None,
        description="起始时间（含），缺省返 400",
    ),
    end_date: Optional[datetime] = Query(
        None,
        description="结束时间（不含），缺省返 400",
    ),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """读取 admin 运营驾驶舱聚合数据。"""
    if start_date is None or end_date is None:
        raise HTTPException(
            status_code=400,
            detail="admin analytics 必须传 start_date + end_date（防全表扫描）",
        )
    if end_date <= start_date:
        raise HTTPException(status_code=400, detail="end_date 必须 > start_date")
    if end_date - start_date > timedelta(days=90):
        raise HTTPException(
            status_code=400,
            detail="时间范围 ≤ 90 天（如需更长请分多次查）",
        )

    result = AnalyticsService.get_analytics(db, start_date, end_date)

    try_log_action(
        user=admin,
        action="admin_get_analytics",
        target_type="analytics",
        target_id=None,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
        detail={
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "cache_hit": bool(result.get("cache_hit")),
            "daily_points": len(result.get("daily_activity", [])),
        },
    )
    logger.info(
        f"admin_get_analytics: admin={admin.username}(id={admin.id}) "
        f"window={start_date}~{end_date} cache_hit={result.get('cache_hit')}"
    )
    return result


@router.get("/kpi", summary="业务 KPI 大盘（WP1：deflection/CSAT/拦截率）")
def get_kpi(
    days: int = Query(14, ge=1, le=90),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """客服域业务指标窗口聚合（指标定义见 kpi_service 模块注释）。"""
    end = datetime.now()
    start = end - timedelta(days=days)
    return kpi_service.compute_kpi(db, start, end)


@router.get("/kpi/trend", summary="业务 KPI 按日趋势（自助解决率折线）")
def get_kpi_trend(
    days: int = Query(14, ge=1, le=60),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    return kpi_service.compute_trend(db, days=days)


# =============================================================
# 数据看板真实统计（/board）——DataBoard.vue 数据源
# 口径说明：
#   渠道       = session_id 前缀（xianyu-* → 店铺 IM，其余 → 网页商城）
#               前缀是历史命名的技术标识（该通道以闲鱼 IM 协议实现），
#               对外展示统一叫「店铺 IM」，避免看板出现与主场景不符的渠道名
#   响应时间   = 同会话内 assistant 消息时间 - 前一条 user 消息时间（秒）
#   AI 回复率  = assistant 消息中 intent_snapshot.ai=true 占比（排除人工）
#   转人工率   = 窗口内建工单数 / 去重会话数
#   尺码自助处理率 = 尺码诉求会话中未升级人工工单的占比
#   测试会话过滤：由 session_meta.kind 决定（不再用 session_id 前缀正则猜）
# =============================================================
import re as _re
from collections import Counter as _Counter

from sqlalchemy import text

from app.models.handoff_ticket import HandoffTicket  # noqa: E402
from app.services.session_kind import BIZ_SQL  # noqa: E402

# 测试会话判定统一在 app/services/session_kind.py（唯一一份前缀正则）。
# 统计侧不再自己猜 session_id 前缀：只认 session_meta.kind='biz'。
# 曾经这里和 Java 的 AnalyticsService 各存一条一模一样的正则，改一边没人知道。

# 2026-11 服饰域：阶段分类按"尺码 → 售后 → 物流"重排（顺序敏感，先命中先归类）
#   尺码：选码与商品参数咨询 —— 服饰店最高频、最该被统计的诉求
#   售后：退换/运费/质量问题/投诉
#   物流：发货、改址、预售、订单（原「安装」）
#   其余 → 选购咨询
_STAGE_KEYWORDS = {
    "尺码": ("尺码", "胸围", "腰围", "臀围", "衣长", "肩宽", "袖长", "裤长",
             "什么码", "偏大", "偏小", "号型", "克重", "成分", "版型", "平铺"),
    "售后": ("退款", "退货", "退钱", "换货", "换码", "运费险", "质量问题", "起球",
             "色差", "开线", "破损", "投诉", "人工", "专员", "售后", "吊牌", "七天"),
    "物流": ("发货", "物流", "快递", "签收", "地址", "发票", "运费", "订单",
             "预售", "包邮", "催"),
}


def _stage_of(content: str) -> str:
    for stage, kws in _STAGE_KEYWORDS.items():
        if any(k in content for k in kws):
            return stage
    return "选购咨询"


@router.get("/board", summary="数据看板真实统计（DataBoard 数据源）")
def analytics_board(
    days: int = Query(14, ge=1, le=90),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """数据看板聚合。

    两道口径约束（都是踩过才加上的）：
    1. **测试流量按 session_meta 排**，不看 session_id 前缀。前缀正则只保留在
       写入侧一份，读数侧各写一份的下场是：新增评测脚本忘了加前缀，一次全量
       评测给看板灌进 1542 条消息，而且两边实现谁都不知道对方漏了哪条。
    2. **聚合全部下推 SQL**。早期是 `SELECT * FROM messages WHERE 时间窗`
       全量拉进内存再算，956 条时无感，上万条时每刷一次看板就拉一次全表。

    唯一保留的明细查询是「用户文本」：意图分类与热门问题要做关键词匹配和
    降噪归一，这部分下推不了，所以只取 role='user' 的 content 一列。
    """
    since = datetime.now() - timedelta(days=days)
    try_log_action(admin, "analytics_board", detail={"days": days})

    # 统一过滤条件：时间窗 + 未软删 + 非测试会话
    where = (
        "FROM messages m WHERE m.create_time >= :since AND m.deleted = 0 AND " + BIZ_SQL
    )
    params = {"since": since}

    # 日期轴（含零天）
    dates = []
    base_date = datetime.now().date()
    for i in range(days - 1, -1, -1):
        dates.append((base_date - timedelta(days=i)).strftime("%m/%d"))
    date_key = lambda dt: dt.strftime("%m/%d")  # noqa: E731

    # 1) 日 × 渠道（一条 GROUP BY 替代全量遍历）
    channel_day: dict[str, dict[str, int]] = {d: {"xianyu": 0, "mall": 0} for d in dates}
    for d, xy, mall in db.execute(text(f"""
        SELECT DATE(m.create_time) AS d,
               SUM(LEFT(m.session_id, 7) = 'xianyu-') AS xy,
               SUM(LEFT(m.session_id, 7) <> 'xianyu-') AS mall
        {where}
        GROUP BY DATE(m.create_time)
    """), params).fetchall():
        slot = channel_day.setdefault(date_key(d), {"xianyu": 0, "mall": 0})
        slot["xianyu"] = int(xy or 0)
        slot["mall"] = int(mall or 0)

    # 2) 小时分布（原来在 Python 里对 24 个桶 +1）
    hourly = [0] * 24
    for h, c in db.execute(text(f"""
        SELECT HOUR(m.create_time) AS h, COUNT(*) AS c
        {where}
        GROUP BY HOUR(m.create_time)
    """), params).fetchall():
        if h is not None and 0 <= int(h) < 24:
            hourly[int(h)] = int(c)

    # 3) 会话级聚合：条数（老客阈值）、首见日期（新客）、AI/人工消息数
    #    JSON_EXTRACT 取 intent_snapshot.human：与 ORM 里 snap.get("human") 等价
    sess_rows = db.execute(text(f"""
        SELECT m.session_id AS sid,
               COUNT(*) AS c,
               MIN(m.create_time) AS first_at,
               SUM(m.role = 'assistant'
                   AND JSON_EXTRACT(m.intent_snapshot, '$.human') = 1) AS human_n,
               SUM(m.role = 'assistant'
                   AND (m.intent_snapshot IS NULL
                        OR JSON_EXTRACT(m.intent_snapshot, '$.human') <> 1)) AS ai_n
        {where}
        GROUP BY m.session_id
    """), params).fetchall()
    ai_n = sum(int(r[4] or 0) for r in sess_rows)
    human_n = sum(int(r[3] or 0) for r in sess_rows)
    sess_count = len(sess_rows)

    # 4) 响应时间：同会话「user → assistant」相邻两条的时间差，>1h 视为离线间隔
    #    MySQL 8 窗口函数 LAG 一次算完；原来是把会话消息全捞进内存逐条比
    resp_day: dict[str, tuple[float, float, float]] = {}
    resp_total_s, resp_total_n = 0.0, 0
    for d, n, s, avg_s, max_s, min_s in db.execute(text(f"""
        SELECT DATE(x.create_time) AS d, COUNT(*) AS n, SUM(x.delta) AS s,
               AVG(x.delta) AS a, MAX(x.delta) AS mx, MIN(x.delta) AS mn
        FROM (
            SELECT m.create_time,
                   m.role AS role,
                   LAG(m.role) OVER w AS prev_role,
                   TIMESTAMPDIFF(SECOND,
                       LAG(m.create_time) OVER w, m.create_time) AS delta
            {where}
            WINDOW w AS (PARTITION BY m.session_id ORDER BY m.create_time, m.id)
        ) x
        WHERE x.role = 'assistant' AND x.prev_role = 'user'
          AND x.delta > 0 AND x.delta < 3600
        GROUP BY DATE(x.create_time)
    """), params).fetchall():
        resp_day[date_key(d)] = (
            round(float(avg_s or 0), 1),
            round(float(max_s or 0), 1),
            round(float(min_s or 0), 1),
        )
        resp_total_s += float(s or 0)
        resp_total_n += int(n or 0)

    # 5) 工单（转人工率、尺码自助解决率的分母校正）
    tickets = (
        db.query(HandoffTicket)
        .filter(HandoffTicket.create_time >= since, HandoffTicket.deleted == 0)
        .count()
    )
    ticket_sessions = {
        str(sid)
        for (sid,) in db.query(HandoffTicket.session_id)
        .filter(HandoffTicket.create_time >= since, HandoffTicket.deleted == 0)
        .all()
    }

    # 6) 用户文本（唯一明细查询）：意图分类 + 热门问题 + 会话标签
    user_rows = db.execute(text(f"""
        SELECT m.session_id AS sid, m.content AS content
        {where} AND m.role = 'user'
    """), params).fetchall()

    stage_counter: _Counter = _Counter()
    repair_sessions, after_sale_sessions = set(), set()
    user_texts: list[str] = []
    for sid, content in user_rows:
        t = (content or "").strip()
        if not t:
            continue
        user_texts.append(t)
        stage_counter[_stage_of(t)] += 1
        if any(k in t for k in _STAGE_KEYWORDS["尺码"]):
            repair_sessions.add(sid)
        if any(k in t for k in _STAGE_KEYWORDS["售后"]):
            after_sale_sessions.add(sid)

    repair_resolved = repair_sessions - ticket_sessions
    # 无尺码咨询时返回 None 而不是 100：没数据却显示满分，是把「没测到」伪装成「做得好」
    repair_rate = (
        round(len(repair_resolved) / len(repair_sessions) * 100, 1)
        if repair_sessions
        else None
    )

    today = date_key(datetime.now())
    today_messages = channel_day.get(today, {}).get("xianyu", 0) + channel_day.get(today, {}).get("mall", 0)

    # 热门问题 TOP10（买家原文归一计数，过滤纯订单号类文本）
    norm = _Counter()
    _ORDER_NO_RE = _re.compile(r"^(ORD|NO|DD|E)?[0-9A-Za-z]{8,}$")
    for t in user_texts:
        t2 = _re.sub(r"[\s？?！!。，,.~]+", "", t)
        if len(t2) < 2:
            continue
        key = t2[:14]
        if _ORDER_NO_RE.match(t2) or _ORDER_NO_RE.match(key):
            continue
        norm[key] += 1
    top10 = norm.most_common(10)

    # 客户标签（会话级，优先级：老客 > 售后中 > 尺码咨询 > 选购意向）
    tag_counter: _Counter = _Counter()
    for sid, c, _first_at, _h, _a in sess_rows:
        if int(c) >= 20:
            tag_counter["老客"] += 1
        elif sid in after_sale_sessions:
            tag_counter["售后中"] += 1
        elif sid in repair_sessions:
            tag_counter["尺码咨询"] += 1
        else:
            tag_counter["选购意向"] += 1

    first_seen_dates = [(r[2].strftime("%m/%d") if r[2] else "") for r in sess_rows]
    new_cust = [sum(1 for v in first_seen_dates if v == d) for d in dates]

    xianyu_trend = [channel_day.get(d, {}).get("xianyu", 0) for d in dates]
    mall_trend = [channel_day.get(d, {}).get("mall", 0) for d in dates]
    total_xy = sum(xianyu_trend)
    total_mall = sum(mall_trend)

    # 被排除的测试消息数：留一个可观测指标，否则「过滤生效没有」只能靠感觉
    excluded = db.execute(text("""
        SELECT COUNT(*) FROM messages m
        WHERE m.create_time >= :since AND m.deleted = 0
          AND m.session_id IN (SELECT session_id FROM session_meta WHERE kind <> 'biz')
    """), params).scalar() or 0

    ai_rate = round(ai_n / (ai_n + human_n) * 100, 1) if (ai_n + human_n) else 100.0
    handoff_rate = round(tickets / sess_count * 100, 1) if sess_count else 0.0

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "days": days,
        "excluded_test_messages": int(excluded),
        "kpis": {
            "today_messages": today_messages,
            "avg_response_sec": round(resp_total_s / resp_total_n, 1) if resp_total_n else 0,
            "ai_reply_rate": ai_rate,
            "handoff_rate": handoff_rate,
            "repair_self_service_rate": repair_rate,
            "new_customers_today": new_cust[-1],
        },
        "msg_trend": {"dates": dates, "xianyu": xianyu_trend, "mall": mall_trend},
        "channel_split": [
            {"name": "网页商城", "value": total_mall},
            {"name": "店铺 IM", "value": total_xy},
        ],
        "hourly": hourly,
        "resp_trend": {
            "dates": dates,
            "avg": [resp_day.get(d, (0, 0, 0))[0] for d in dates],
            "max": [resp_day.get(d, (0, 0, 0))[1] for d in dates],
            "min": [resp_day.get(d, (0, 0, 0))[2] for d in dates],
        },
        "top_questions": [{"q": q, "count": c} for q, c in top10],
        "intent_split": [{"name": k, "value": v} for k, v in stage_counter.most_common()],
        "new_cust_trend": {"dates": dates, "counts": new_cust},
        "customer_tags": [{"name": k, "value": v} for k, v in tag_counter.most_common()],
    }
