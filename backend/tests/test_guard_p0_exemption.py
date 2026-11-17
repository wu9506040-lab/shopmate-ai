"""guard 判域闸 与 P0 风险闸 的先后顺序回归测试

背景（真实事故）：
    买家说「再不处理我就打 12315 投诉你们」，L2 语义判域把它判成 off_domain
    （不是问商品参数，属"闲聊"），guard 在 P0 检测之前执行 → 直接回欢迎语，
    真投诉从未进入转人工，工单表里一张都没有。

修复：
    chat.py 在 guard 拦截分支前先算 detect_p0_escalate(payload.query)，
    命中 P0 且 guard 原因是 off_domain 时豁免判域闸，继续走 P0 → 建工单。
    仅豁免 off_domain；违禁品类目（涉毒/枪支）即便含风险词也照拦。

覆盖：
1. off_domain + P0 投诉话术 → 出 handoff 流（不是 blocked）
2. off_domain + 普通闲聊 → 仍 blocked（判域闸本身没被废掉）
3. banned_category + P0 话术 → 仍 blocked（违禁品不受豁免）
4. P0 只算一次：handoff 用的就是 guard 之前那次结果
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://u:p@localhost:3306/db?charset=utf8mb4")


def _parse_sse(chunks: list) -> list:
    """解析 chat.py 产出的 SSE chunk 列表为事件 dict 列表。"""
    import json

    events = []
    for chunk in chunks:
        for line in str(chunk).splitlines():
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:].strip()))
                except Exception:  # noqa: BLE001
                    pass
    return events


def _guard(allowed: bool, reason: str, layer: str = "L2", category=None, response=None):
    """构造 GuardResult（用真实 dataclass，字段变更会立刻暴露）"""
    from app.services.guard import GuardResult

    return GuardResult(
        allowed=allowed, reason=reason, response=response,
        layer=layer, category=category,
    )


def _run_chat(query: str, guard_result, handoff_mock):
    """用最小 mock 执行 chat()，返回解析后的事件列表。"""
    import asyncio

    from app.api.chat import chat
    from app.schemas.chat import ChatRequest

    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    request.client.host = "127.0.0.1"
    request.headers = {"x-forwarded-for": "", "user-agent": "test"}
    payload = ChatRequest(query=query)
    user = MagicMock()
    user.id = 10009
    user.username = "user_10009"

    async def _collect(coro):
        response = await coro
        out = []
        async for item in response.body_iterator:
            out.append(item.decode("utf-8") if isinstance(item, bytes) else item)
        return out

    with patch("app.api.chat.load_history_with_fallback", return_value=[]), \
         patch("app.api.chat.behavior_monitor.record_request"), \
         patch("app.api.chat.input_guard.check", return_value=guard_result), \
         patch("app.api.chat.IntentService.classify", MagicMock(return_value={
             "intent": "chitchat", "entities": {"order_no": None, "sku": None, "keywords": []},
         })), \
         patch("app.api.chat.get_escalation_service", return_value=handoff_mock), \
         patch("app.api.chat.try_log_action"), \
         patch("app.api.chat.badcase_service.enqueue_guard_blocked_safe"), \
         patch("app.api.chat.badcase_service.enqueue_repeat_soft_safe"):
        chunks = asyncio.run(_collect(chat(request, payload, user)))
    return _parse_sse(chunks)


def _handoff_service():
    """escalation.handoff 的 mock：返回带 to_dict 的工单对象"""
    svc = MagicMock()
    ticket = MagicMock()
    ticket.handoff_id = "H00000001"
    ticket.reason_label = "已转人工"
    ticket.to_dict.return_value = {"handoff_id": "H00000001", "priority": "P0"}
    svc.handoff.return_value = ticket
    return svc


class TestGuardP0Exemption:
    """判域闸不得吞掉 P0 风险话术"""

    def test_off_domain_complaint_creates_handoff(self):
        """off_domain + 「打 12315 投诉」→ 出 handoff 流，而不是 blocked"""
        svc = _handoff_service()
        events = _run_chat(
            "再不处理我就打 12315 投诉你们",
            _guard(allowed=False, reason="off_domain", response="您好，我是智能客服～"),
            svc,
        )
        metas = [e for e in events if e.get("type") == "meta"]
        assert metas, "没有产生 meta 事件"
        assert metas[0]["intent"] == "handoff", f"应转人工，实际 intent={metas[0]['intent']}"
        assert svc.handoff.called, "未创建工单"
        # 建单时必须带上命中依据，客服才追得回原因
        kwargs = svc.handoff.call_args.kwargs
        assert kwargs.get("matched_keyword"), "工单缺少 matched_keyword"
        assert kwargs.get("detected_category") == "complaint"

    def test_off_domain_chitchat_still_blocked(self):
        """off_domain 但没有 P0 词 → 仍被拦（判域闸没被废掉）"""
        svc = _handoff_service()
        events = _run_chat(
            "今天天气不错啊朋友",
            _guard(allowed=False, reason="off_domain", response="您好，我是智能客服～"),
            svc,
        )
        metas = [e for e in events if e.get("type") == "meta"]
        assert metas and metas[0]["intent"] == "blocked"
        assert not svc.handoff.called, "闲聊不该建工单"

    def test_banned_category_not_exempted(self):
        """违禁品类目即便含风险词也照拦，不给豁免开天窗"""
        svc = _handoff_service()
        events = _run_chat(
            "我要投诉你们卖的这种毒品",
            _guard(
                allowed=False, reason="banned", layer="L2",
                category="涉毒", response="该商品不支持咨询",
            ),
            svc,
        )
        metas = [e for e in events if e.get("type") == "meta"]
        assert metas and metas[0]["intent"] == "blocked", "违禁品不该因含投诉词被放行"
        assert not svc.handoff.called

    def test_p0_detected_once_and_reused(self):
        """P0 匹配只做一次：guard 前算的结果直接复用，避免两次匹配漂移"""
        svc = _handoff_service()
        with patch("app.api.chat.detect_p0_escalate",
                   MagicMock(return_value=("complaint", "投诉"))) as spy:
            events = _run_chat(
                "随便一句话",
                _guard(allowed=False, reason="off_domain"),
                svc,
            )
        metas = [e for e in events if e.get("type") == "meta"]
        assert metas and metas[0]["intent"] == "handoff"
        assert spy.call_count == 1, f"detect_p0_escalate 被调用 {spy.call_count} 次，应为 1 次"
