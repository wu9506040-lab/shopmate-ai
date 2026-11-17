"""admin 质检（QA）API。

四个端点对应质检闭环的四个动作：
  跑批 → 看结果 → 看趋势 → 把低分转成 badcase（进 offline 评测集）
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.clients.mysql_client import get_db
from app.models.user import User
from app.schemas.admin_qa import (
    QaItem, QaListResponse, QaRunRequest, QaRunResponse, QaStatsResponse,
    QaToBadcaseResponse,
)
from app.services.audit_service import try_log_action
from app.services.qa import service as qa_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/qa", tags=["admin-qa"])


def _iso(v) -> Optional[str]:
    return v.isoformat() if v else None


@router.post("/run", response_model=QaRunResponse, summary="[admin] 跑一轮质检")
def run_qa(payload: QaRunRequest, db: Session = Depends(get_db),
           admin: User = Depends(require_admin)):
    try:
        res = qa_service.run_batch(
            db, since_hours=payload.since_hours, limit=payload.limit,
            ratio=payload.ratio, use_llm=payload.use_llm,
        )
    except Exception as e:  # 跑批失败要让管理员看到原因，不能静默返回空结果
        logger.exception("质检跑批失败")
        raise HTTPException(status_code=500, detail=f"质检跑批失败: {type(e).__name__}") from e
    try_log_action(admin, "qa_run", target_type="qa_batch",
                   target_id=str(res.get("batch_id")),
                   detail={k: v for k, v in res.items() if k != "batch_id"})
    return QaRunResponse(**res)


@router.get("/results", response_model=QaListResponse, summary="[admin] 质检结果列表")
def list_qa(verdict: Optional[str] = Query(None, description="pass/low/blocked"),
            limit: int = Query(50, ge=1, le=200),
            offset: int = Query(0, ge=0),
            db: Session = Depends(get_db), _: User = Depends(require_admin)):
    rows, total = qa_service.list_results(db, verdict=verdict, limit=limit, offset=offset)
    items = [QaItem(
        id=r.id, session_id=r.session_id, message_id=r.message_id,
        query=r.query, answer=r.answer, intent=r.intent, verdict=r.verdict,
        source=r.source, score=r.score, dims=r.dims, rule_hits=r.rule_hits,
        failure_layer=r.failure_layer, calibrated=r.calibrated,
        review_status=r.review_status, note=r.note, create_time=_iso(r.create_time),
    ) for r in rows]
    return QaListResponse(total=total, items=items)


@router.get("/stats", response_model=QaStatsResponse, summary="[admin] 质检看板统计")
def qa_stats(days: int = Query(7, ge=1, le=90),
             db: Session = Depends(get_db), _: User = Depends(require_admin)):
    s = qa_service.stats(db, days=days)
    return QaStatsResponse(**s)


@router.post("/{qa_id}/to-badcase", response_model=QaToBadcaseResponse,
             summary="[admin] 低分转 badcase 池")
def qa_to_badcase(qa_id: int, db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    try:
        res = qa_service.to_badcase(db, qa_id, admin_id=admin.id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    try_log_action(admin, "qa_to_badcase", target_type="qa", target_id=str(qa_id),
                   detail=res.get("badcase"))
    return QaToBadcaseResponse(**res)
