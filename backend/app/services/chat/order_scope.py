"""区分「问我自己那一单」和「问普遍规则」。

为什么需要这个判定：
    intent 分类器把「退货运费谁出」「七天无理由怎么算」一律判成 refund_query，
    然后退款链路第一步就是解析订单。用户根本没下单（或在别的平台买的），
    解析出 0 单，链路直接回「您当前没有订单，暂时无法处理哦～」——
    一个纯政策问题被订单解析吃掉了。实测 60 条多轮对话里 16 条栽在这里。

判定逻辑：
    有个人锚点（我这单 / 我上周买的 / 订单号 ORD…）→ 是在问自己的单，别改判
    没有个人锚点 + 有规则/疑问词          → 是在问规则，降为 policy_query 走 RAG

保守优先：宁可漏改（还是走订单链路，现在链路也修成「查不到就说查不到」）
也不要错改（把真退款请求当政策问答，用户会拿不到人工）。
"""
from __future__ import annotations

import re

# 订单号格式（与 intent.yaml ORDER_NO_RE_PATTERN 保持一致）
_ORDER_NO_RE = re.compile(r"(ORD\d{8}[A-Z0-9]{3,6})|(\b\d{15,20}\b)", re.IGNORECASE)

# 指向「我自己某一单」的锚点。
# 用短前缀而不是完整短语：中文说法太散（我这单 / 我这个订单 / 我那个包裹 / 我的快递），
# 逐个穷举一定漏。宁可多认（漏改回退到订单链路，代价小），不可少认
# （把真退款请求当成政策问答，用户拿不到人工）。
_PERSONAL_MARKERS = (
    "我的", "我这", "我那", "我买", "我下", "我刚",
    "我上周", "我昨天", "我前天", "我上次",
    # 「我要退款怎么操作」含疑问词但没有「我的」——没有这组词会被误改判成
    # 政策问答，真退款请求拿不到人工。代价：「我要是退货…」这类假设句也带
    # 「我要」，会漏改成走订单链路——方向是保守侧，可接受。
    "我要", "我想", "我需要",
    "帮我查", "查一下我", "给我查", "帮我看看我",
    "订单号", "这单", "那单", "这一单", "那一单",
    "给我退", "退我", "退到我的",
)

# 规则/疑问特征（说明问的是「普遍怎么算」，不是「我这一单怎么办」）
_POLICY_MARKERS = (
    "怎么算", "怎么退", "怎么办", "怎么做", "怎么申请", "怎么操作", "怎么联系",
    "如何", "什么条件", "什么情况", "需要什么", "要什么", "什么要求",
    "哪些", "哪几种", "能不能", "可不可以", "可以吗", "行不行",
    "规则", "政策", "规定", "条款", "流程", "标准",
    "多久", "几天", "多少天", "多长时间",
    "谁承担", "谁出", "谁付", "运费谁",
    "有什么", "包括", "范围",
    # 追问式（无个人锚点时才生效，见 should_reroute_to_policy）
    "呢", "为什么", "还有", "那如果", "如果", "是不是",
    # 「那要是洗过了」——假设式追问，没有常规疑问词，靠「要是」识别
    "要是",
)


def has_order_no(query: str) -> bool:
    """句子里有没有订单号。"""
    return bool(_ORDER_NO_RE.search(query or ""))


def has_personal_reference(query: str) -> bool:
    """句子里有没有指向「我自己某一单」的锚点。"""
    q = query or ""
    if has_order_no(q):
        return True
    return any(k in q for k in _PERSONAL_MARKERS)


def is_policy_style(query: str) -> bool:
    """句子里有没有规则/疑问特征。"""
    q = query or ""
    return any(k in q for k in _POLICY_MARKERS)


def should_reroute_to_policy(
    query: str, intent: str, has_locked_order: bool = False,
) -> bool:
    """是否把 refund_query / order_query 降级为 policy_query。

    Args:
        query: 本轮用户原话（未改写）
        intent: 分类器给出的主意图
        has_locked_order: 会话是否已锁定某一单（ctx.current_order_no 非空）。
            锁定了就说明用户正在处理那一单，别再判成政策问答。
    """
    if intent not in ("refund_query", "order_query"):
        return False
    if has_locked_order:
        return False
    if has_personal_reference(query):
        return False
    return is_policy_style(query)
