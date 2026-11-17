"""知识运营闭环。

四步：
  1. diagnose —— 归因（把「答得不好」判成「该干什么」）
  2. report   —— 按知识源聚合待办清单
  3. ticket   —— 出工单与草稿（根因不同开不同的单：补新卡 / 改检索锚点 / 改卡）
  4. verify   —— 入库后回归验证，通过才关单并回填 expected_source

设计前提（来自 BUGS 31/32 的教训）：
- 归因要可复现：判据是「查库 + 比对检索结果」，不让 LLM 判根因
- 结论要带证据：运营看到"no_kb"必须能知道"凭什么"
- 关单要可复核：不是人点完成，是同一批问句 rank 真的变了
"""

from app.services.kb_ops.diagnose import (  # noqa: F401
    ROOT_CONTEXT_MISSING,
    ROOT_GENERATION,
    ROOT_KB_EXPIRED,
    ROOT_KB_STALE,
    ROOT_NO_KB,
    ROOT_RANKING_WEAK,
    ROOT_RETRIEVAL_MISS,
    ROOT_UNKNOWN,
    ROOT_CAUSE_LABELS,
    classify,
    diagnose_one,
    run_batch,
)
from app.services.kb_ops.report import source_report  # noqa: F401
from app.services.kb_ops.ticket import (  # noqa: F401
    KIND_ADD,
    KIND_PATCH,
    KIND_UPDATE,
    STATUS_APPLIED,
    STATUS_CLOSED,
    STATUS_DRAFTED,
    STATUS_OPEN,
    STATUS_REOPENED,
    apply_ticket,
    draft_ticket,
    list_tickets,
    load_store,
    open_ticket,
    save_store,
)
from app.services.kb_ops.verify import (  # noqa: F401
    backfill_expected_sources,
    settle_pending,
    settle_ticket,
    verify_ticket,
)
