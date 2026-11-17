"""
Prompt Assembler（M9.5+ · 拆分自 synthesizer.py）

职责：构造 LLM 输入（系统 prompt + 用户 prompt + 结构化上下文格式化）。
- 不调 LLM
- 不做 intent 分类
- 不做流式输出
- 仅文本 / 字符串层面的"格式化 + 拼接"

来源：原 synthesizer.py 模块级 7 个函数（_build_context_block / _build_chat_prompt /
_format_tool_result / _format_policy_docs / _format_history / _build_meta_contexts /
_extract_order_no_from_history）+ 2 个 prompt 常量（已迁移到 config/prompts YAML）。
"""
import json
import logging
import re as _re
from pathlib import Path
from typing import Optional

from app.services.prompt_loader import get_prompt_loader
from app.services.session_service import ANONYMOUS_USER_ID
from app.tools.product_tool import ProductTool
from app.services.order_service import OrderService

logger = logging.getLogger(__name__)


# 系统提示词：从 config/prompts/agent.yaml 的版本清单加载
SYSTEM_PROMPT_BASE = get_prompt_loader().load("agent")


# 未登录场景提示词
NO_LOGIN_PROMPT = get_prompt_loader().load("no_login")

# 商品切片结构化数据（与商城页面、知识库切片由同一脚本生成，数值唯一来源）
_PRODUCT_SLICES_FILE = (
    Path(__file__).resolve().parents[3] / "config" / "product_slices.json"
)


def _load_product_slice(model: str) -> Optional[dict]:
    """读取该款的参数 / 尺码表；文件缺失不阻塞对话。"""
    try:
        with _PRODUCT_SLICES_FILE.open(encoding="utf-8") as f:
            data = json.load(f) or {}
        return data.get(model)
    except Exception as e:
        logger.warning(f"商品切片读取失败 model={model}: {e}")
        return None


def _format_slice_for_prompt(s: dict) -> str:
    """把该款参数与尺码表压成一段可直接引用的 prompt 文本。"""
    lines = [
        f"【当前商品参数】款号={s.get('model', '')}｜品类={s.get('category', '')}"
        f"｜商品编号={s.get('itemId', '')}｜尺码档位={s.get('sizeRange', '')}"
    ]
    params = s.get("params") or []
    if params:
        lines.append(
            "参数：" + "；".join(
                f"{p['label']}={p['value']}" for p in params[:16]
            )
        )
    st = s.get("sizeTable")
    if st:
        cols = st.get("cols") or []
        lines.append(f"尺码表（单位 {st.get('unit', 'CM')}）：" + " | ".join(cols))
        for row in st.get("rows") or []:
            cells = [
                str(row.get("size", "")) if i == 0 else str(row.get(c, "—"))
                for i, c in enumerate(cols)
            ]
            lines.append("  " + " | ".join(cells))
        if st.get("note"):
            lines.append("  备注：" + str(st["note"]))
    if s.get("sizeNote"):
        lines.append("口径：" + str(s["sizeNote"]))
    return "\n".join(lines)


def _build_context_block(
    sku: Optional[str],
    order_no: Optional[str],
    user_id: Optional[int],
    profile_block: str = "",
    user_context_block: str = "",
) -> str:
    """构建从商品/订单跳转携带的 context 信息（注入 LLM prompt）

    让 LLM 知道用户当前在问哪个商品/哪个订单，避免"您问的是哪款"反问。

    P2 长程记忆：扩 profile_block 参数，把跨 session 用户画像拼到 context_block 末尾。
    profile_block 来自 profile_service.to_prompt_block()，默认空串（开关关闭或匿名）。

    新增 user_context_block：会话入口预加载的用户上下文（最近 5 单 + 画像摘要），
    来自 user_context_preloader.to_prompt_block()。拼到 profile_block 之后，
    让 LLM 看到"用户最近买了什么"。

    Returns:
        多行字符串，每行一个 context 段；无 context 时返回空串。
    """
    lines = []
    # 商品 context
    if sku:
        try:
            products = ProductTool.list_products(category=None, limit=100)
            # 2026-11：报价必须取当期价目，不能用 products 表的基准价
            # （12 月调价后基准价仍是旧价，AI 会答错）
            from app.services.product_price_service import get_current_prices

            price_map = get_current_prices([p.get("sku", "") for p in products])
            product = next((p for p in products if p.get("sku") == sku), None)
            if product:
                attrs = product.get("attributes") or {}
                attrs_str = "、".join(f"{k}={v}" for k, v in attrs.items()) if isinstance(attrs, dict) else ""
                period = price_map.get(product["sku"])
                price = period.price if period else product["price"]
                price_note = (
                    f" | 价格口径={period.label}" if period
                    else " | 价格口径=基准价（当前无生效价目，请提示核实）"
                )
                lines.append(
                    f"【当前商品】SKU={product['sku']} | 名称={product['name']} | "
                    f"价格=¥{price} | 库存={product.get('stock', '?')}" + price_note
                    + (f" | 规格={attrs_str}" if attrs_str else "")
                )
                # 同源商品切片：把该款参数与尺码表直接给到 LLM，
                # 保证页面展示值与客服回答值一致，且不依赖检索是否命中。
                model = attrs.get("model") if isinstance(attrs, dict) else None
                if model:
                    sl = _load_product_slice(str(model))
                    if sl:
                        lines.append(_format_slice_for_prompt(sl))
                    lines.append(
                        "【上下文使用规则】用户从该商品页进入，提问默认指本款；"
                        "直接依据上述参数与尺码表回答，不要反问用户是哪件商品、"
                        "不要索要订单号。本款尺码数值以本段为准，优先级高于通用尺码指南。"
                    )
            else:
                lines.append(f"【当前商品】SKU={sku}（未在售/已下架）")
        except Exception as e:
            logger.warning(f"加载商品 context 失败 sku={sku}: {e}")
            lines.append(f"【当前商品】SKU={sku}")

    # 订单 context（必须登录且属于本人）
    if order_no and user_id and user_id != ANONYMOUS_USER_ID:
        try:
            order = OrderService.get_order_detail(user_id, order_no)
            if order:
                items = order.get("items", [])
                items_str = "、".join(f"{it['product_name']}×{it['qty']}" for it in items[:3])
                if len(items) > 3:
                    items_str += f" 等{len(items)}件"
                logi = order.get("logistics") or {}
                logi_str = ""
                if logi:
                    logi_str = f" | 物流={logi.get('logistics_no', '?')} ({logi.get('status', '')}@{logi.get('last_location', '')})"
                lines.append(
                    f"【当前订单】订单号={order_no} | 状态={order.get('status', '?')} | "
                    f"商品={items_str} | 金额=¥{order.get('total_amount', '?')}{logi_str}"
                )
            else:
                lines.append(f"【当前订单】订单号={order_no}（不存在或不属于当前用户）")
        except Exception as e:
            logger.warning(f"加载订单 context 失败 order_no={order_no}: {e}")
            lines.append(f"【当前订单】订单号={order_no}")

    # P2 长程记忆：profile_block（来自 profile_service.to_prompt_block）
    # 优先级：M9.5 context > profile（profile 是补充信息，不应覆盖当前订单/商品）
    if profile_block:
        lines.append(profile_block)

    # 用户上下文预加载（user_context_block）—— 最近 5 单 + 画像
    # 拼到 profile 之后，让 LLM 能主动关联"用户最近买的商品"
    if user_context_block:
        lines.append(user_context_block)

    return "\n".join(lines)


def _build_chat_prompt(
    *,
    intent: str,
    tool_block: str,
    policy_block: str,
    product_block: str,
    history_block: str,
    query: str,
    context_block: str = "",
    secondary_intent_block: str = "",
    facts_block: str = "",
    l3_block: str = "",
) -> str:
    """组装 chat 模板（按优先级硬约束）
    """
    sections = []
    # context_block 由 stream_dispatcher 拼到 system message 头部，user 段不重复。
    if secondary_intent_block:
        sections.append(f"【用户可能的次要问题】\n{secondary_intent_block}")
    if tool_block:
        sections.append(f"【事实陈述】(最高优先级)\n{tool_block}")
    if policy_block:
        sections.append(f"【政策依据】\n{policy_block}")
    if product_block:
        sections.append(f"【商品知识】\n{product_block}")
    if facts_block:
        # 「以本轮为准」这半句不能省：没有它，买家改口后模型会拿旧值答，
        # 改口覆盖等于白做（facts 里 prev 就是旧值，模型很容易挑错那个）。
        sections.append(
            f"【买家此前提过】（不是本轮说的；与本轮说法冲突时一律以本轮为准）\n{facts_block}"
        )
    # L2（本次会话事实）已在上面插入；L3（上次会话）放其后：越近的越优先。
    # to_prompt_block 自带【上次会话记住的】标题，这里不再包一层。
    if l3_block:
        sections.append(l3_block)
    if history_block:
        sections.append(f"【对话历史】\n{history_block}")
    if not sections:
        sections.append("（无可用资料）")
    sections.append(f"问题：{query}")
    return "\n\n".join(sections)


def _format_tool_result(intent: str, tool_result: Optional[dict]) -> str:
    """把 tool 输出格式化成中文自然语言片段（供 LLM 引用）

    溯源：每段加 [订单] / [退款] 来源标签前缀，
    让 LLM 知道这是结构化数据（DB 查的，不是 LLM 编的）
    """
    if not tool_result:
        return ""
    # order_query: 列表 / 详情
    if intent == "order_query":
        if "orders" in tool_result:
            orders = tool_result.get("orders", [])
            if not orders:
                return "[订单] 用户当前没有订单。"
            lines = ["[订单] 用户共有 {} 笔订单：".format(len(orders))]
            for o in orders:
                lines.append(
                    f"- 订单号 {o.get('order_no')} | 状态 {o.get('status')} | "
                    f"金额 ¥{o.get('total_amount')} | 下单时间 {o.get('create_time')}"
                )
            return "\n".join(lines)
        if "order" in tool_result:
            o = tool_result.get("order", {})
            items = tool_result.get("items", [])
            logistics = tool_result.get("logistics", {})
            lines = [
                f"[订单] 订单号 {o.get('order_no')} | 状态 {o.get('status')} | 金额 ¥{o.get('total_amount')}",
                f"[订单] 明细：{len(items)} 件商品",
            ]
            for it in items:
                lines.append(f"  - {it.get('product_name')} × {it.get('qty')} = ¥{it.get('subtotal')}")
            if logistics:
                lines.append(
                    f"[订单] 物流：{logistics.get('status')} | 最新位置 {logistics.get('last_location')} | 单号 {logistics.get('logistics_no')}"
                )
            return "\n".join(lines)
    # refund_query
    if intent == "refund_query":
        tr = tool_result.get("tool_result", tool_result) if "tool_result" in tool_result else tool_result
        if tr.get("refundable") is not None:
            reason = tr.get("reason", "")
            return (
                f"[退款] 退款判断：{'可退' if tr['refundable'] else '不可退'} | 原因：{reason} | "
                f"订单状态 {tr.get('order_status')} | 已签收 {tr.get('days_since_order')} 天"
            )
    return str(tool_result)


def _ctx_char_limit() -> int:
    """给 LLM 的单条资料字符上限（与 meta.text_preview 同口径，自检取白名单依赖它）。"""
    from app.core.config import settings

    return int(getattr(settings, "POLICY_CTX_CHAR_LIMIT", 1000) or 1000)


def _rag_total_chars() -> int:
    """资料位总预算（一整段 policy_block 的上限）。

    单条 1000 是"每条资料最多多长"，管不住"给几条"：top_k=5 就是 5000 字符，
    再加商品块、会话事实、历史（1800），system + user 能被资料挤到没地方放问题。
    总预算的作用是给"资料 → 历史 → 工具结果"排个先后：资料最贵，先封顶。
    """
    try:
        from app.services.config_loader import get_config_loader

        cfg = get_config_loader().load("memory") or {}
        return int(cfg.get("RAG_CTX_TOTAL_CHARS", 3500))
    except Exception:  # pragma: no cover - 仅配置缺失/损坏时触发
        return 3500


def _format_policy_docs(docs: list[dict]) -> str:
    """把 policy RAG 结果格式化

    2026-11：每条前面带「标题 · 分类 · 有效期」，让模型能区分长期政策与临时活动。
    没有元信息的历史条目照旧只输出编号，保证向后兼容（不因缺字段少给资料）。

    2026-11-17 追加总预算：按排名先后分配字符预算，前面的整条给，后面的按剩余
    空间截断，实在放不下就停——并**显式声明少给了几条**。静默截断会让模型
    以为"政策就这几条"，从而把"没提到"当成"不支持"。
    """
    if not docs:
        return ""
    per_doc = _ctx_char_limit()
    total_cap = _rag_total_chars()
    lines: list[str] = []
    used = 0
    for i, d in enumerate(docs, 1):
        text = (d.get("text") or "").strip()
        if not text:
            continue
        meta_bits = [
            str(d.get("title") or "").strip(),
            str(d.get("category") or "").strip(),
            str(d.get("validity_label") or "").strip(),
        ]
        meta = " · ".join(b for b in meta_bits if b)
        prefix = f"（{meta}）" if meta else ""
        head = f"[{i}]{prefix} "
        remain = total_cap - used - (1 if lines else 0)
        # 第一条永远给（哪怕预算配得极小）——返回空资料等于把 RAG 变成裸答
        if lines and remain <= len(head) + 40:
            break
        cap = max(40, min(per_doc, remain - len(head)))
        seg = text[:cap]
        line = head + seg + ("..." if len(text) > cap else "")
        lines.append(line)
        used += len(line) + 1
    total_texts = sum(1 for d in docs if (d.get("text") or "").strip())
    if len(lines) < total_texts:
        lines.append(f"（另有 {total_texts - len(lines)} 条资料因篇幅限制未展示）")
    return "\n".join(lines)


def _format_history(history: Optional[list[dict]]) -> str:
    """复用 pipeline 风格的历史格式"""
    if not history:
        return ""
    lines = []
    for msg in history:
        role = msg.get("role", "")
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if role == "summary":
            # 被裁掉轮次的要点摘录，本身就是一段带标题的文本，不加说话人前缀
            lines.append(content)
        elif role == "user":
            lines.append(f"用户：{content}")
        elif role == "assistant":
            lines.append(f"助手：{content}")
    return "\n".join(lines)


# 买家自述事实的白名单标签。**只白名单**：订单号/地址/手机号走 tool 参数，
# 一旦进 prompt 文本，模型就会在回答里复述（BUGS 35 就是这么来的）。
_FACT_LABELS: tuple[tuple[str, str], ...] = (
    ("size", "尺码"),
    ("height_cm", "身高（cm）"),
    ("weight_kg", "体重（kg）"),
)


def _format_facts_block(facts: Optional[dict]) -> str:
    """把会话事实格式化成 prompt 片段（没有内容返回空串，section 不出现）。"""
    if not facts:
        return ""
    lines = []
    for key, label in _FACT_LABELS:
        cell = facts.get(key)
        value = cell.get("value") if isinstance(cell, dict) else cell
        if value is None or value == "":
            continue
        lines.append(f"- {label}：{value}")
    return "\n".join(lines)


def _build_meta_contexts(
    policy_docs: Optional[list[dict]] = None,
    products: Optional[list[dict]] = None,
    tool_result: Optional[dict] = None,
) -> tuple[list[dict], list[float]]:
    """P0-H：把 RAG 检索结果 / tool 结果转成 meta contexts（暴露给前端/调试用）

    Returns:
        (contexts, scores) — contexts 是 [{source, text_preview, type}, ...]，
        scores 是对应余弦相似度（policy_docs 有 score，products/tool 没 score 用 None 占位）

    之前所有 meta 都硬编码 "contexts": []，前端看不到检索命中，调试也看不到来源
    """
    contexts: list[dict] = []
    scores: list[float] = []
    if policy_docs:
        limit = _ctx_char_limit()
        for d in policy_docs:
            text = (d.get("text") or "").strip()
            contexts.append({
                "source": d.get("source") or d.get("payload", {}).get("source", "knowledge_base"),
                # 2026-11：带上标签与有效期，前端/调试能直接看出"这条是临时活动还是长期政策"
                "title": d.get("title") or "",
                "category": d.get("category") or "",
                "validity_label": d.get("validity_label") or "",
                # 与 _format_policy_docs 给 LLM 的呈现上限对齐（同取 _ctx_char_limit）：
                # 句级自检的数字白名单据此提取，范围不能小于模型实际可见的资料
                "text_preview": text[:limit] + ("..." if len(text) > limit else ""),
                "type": "policy",
            })
            score = d.get("score")
            if isinstance(score, (int, float)):
                scores.append(float(score))
    if products:
        for p in products:
            contexts.append({
                "source": f"product:{p.get('sku', '?')}",
                "text_preview": f"{p.get('name', '')} | ¥{p.get('price', '?')} | 库存 {p.get('stock', '?')}",
                "type": "product",
            })
            scores.append(0.0)  # tool 结果无 cosine 分数，用 0 占位
    if tool_result:
        # order_query 的 tool_result 是 dict / list，统一抽出关键标识
        if "order" in tool_result:
            o = tool_result["order"]
            contexts.append({
                "source": f"order:{o.get('order_no', '?')}",
                "text_preview": f"状态 {o.get('status')} | 金额 ¥{o.get('total_amount')}",
                "type": "order",
            })
        elif "orders" in tool_result:
            for o in tool_result["orders"]:
                contexts.append({
                    "source": f"order:{o.get('order_no', '?')}",
                    "text_preview": f"状态 {o.get('status')} | 金额 ¥{o.get('total_amount')}",
                    "type": "order",
                })
        scores.append(0.0)
    return contexts, scores


# 从历史消息中提取最近一个订单号
# 订单号格式：ORD + 8位日期(YYYYMMDD) + uuid4().hex[:6].upper()（3-6位大写字母数字混合）
# 修复：原 regex 只匹配纯数字，遗漏了字母后缀（如 ORD20260704899EBA）
_ORDER_NO_RE = _re.compile(r"ORD\d{8}[A-Z0-9]{3,6}")


def _extract_order_no_from_history(history: Optional[list[dict]]) -> Optional[str]:
    """从对话历史里提取最近一个出现的订单号

    用于多轮对话：用户第一轮 "ORD20260628004 啥情况"，第二轮只说 "那能退吗"
    时，refund handler 能从 history 自动补上 order_no。

    扫描规则：从最新消息往前扫，user 和 assistant 消息都看，
    返回第一个匹配 ORD + 8位日期 + 3位序号 的字符串。
    """
    if not history:
        return None
    # history 通常按时间正序（最旧 → 最新），所以反向遍历找最近一个
    for msg in reversed(history):
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        m = _ORDER_NO_RE.search(content)
        if m:
            return m.group(0)
    return None
