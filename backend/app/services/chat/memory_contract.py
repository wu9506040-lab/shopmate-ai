"""记忆契约：什么能进上下文，进来以后算什么。

## 为什么需要（2026-11-16 线上体检）

- 1300 条 assistant 消息里 **237 条（18.2%）是兜底话术**（"这块资料我没查准"/"系统繁忙"），
  全部被无差别写进 history。现在没炸只因为没有多轮（475 个业务会话 p50=2 条、max=4）。
  一旦聊起来，第 3 轮模型会看到"助手：这块资料我没查准"——那是它自己说过的话，
  它会当成已承认的事实，要么复读兜底，要么在兜底上继续编。
- **1079 条（83%）带引用角标 `[n]`**。角标是**当轮 contexts 数组的下标**，跨轮完全不对应。
  拼回 history 后模型照着复读，新答案里的 `[3]` 指向的是另一份资料——
  引用看着还在，其实是假的，比"没有引用"更难查。
- 响应缓存 key 是 `user_id + scope + md5(query)`，**history 不参与**。多轮里同一句
  在不同上下文答案不同（"退货运费谁出"在第 1 轮和第 3 轮答案相反），会确定性命中旧答案。

## 三条规矩

1. **失败的回答不当事实**：DB 照旧全存（审计要留证），拼 prompt 时替换成占位。
   存的时候求全，用的时候求干净——这两个要求是相反的，所以分开做。
2. **历史不带角标**：只在拼 prompt 时剥，DB 与前端展示不受影响。
3. **多轮不查缓存**：由调用方判断（history 非空即跳过），不在本模块。
4. **按轮 + 字符预算**（2026-11 补）：原来只按条数（Redis 保留 20 条），
   而 `_format_history` 是**全量拼接、没有任何长度上限**。一条长答案 300-500 token，
   20 条能吃掉 6000+ token，把政策/商品资料位挤出去。改按「轮」切并设字符预算：
   资料位是主角，历史是配角，配角不能把主角挤掉。

## 边界

- 只处理"喂给模型的历史"，不动 messages 表、不动 SSE 输出、不动前端。
- 失败判定是**形态判定**（含兜底话术/内部指令泄漏），不做语义判断——
  语义判断要靠质检的 LLM judge，这里只拦确定坏的。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.services.chat.fact_extract import (
    conflicts_with_known,
    extract_round_facts,
    strip_marks,
)

logger = logging.getLogger(__name__)

# 失败答案的形态标记。前三项复用质检规则层的口径（qa/rules.py FALLBACK_PHRASES），
# 后两项是内部话术原样透传——同样不能当事实，模型会照着学。
INVALID_ANSWER_MARKERS: tuple[str, ...] = (
    "这块资料我没查准",
    "系统繁忙",
    "请稍后重试",
    "用户尚未登录",
    "请礼貌引导用户登录",
)

# 占位话术：短、无指令性。
# 不用"（上一条我说没查准）"——那等于让模型确认自己失败过，下一轮容易先道歉再答。
# 也不用整条丢弃——会出现连续两条 user 消息，部分模型对交替结构敏感。
INVALID_ANSWER_PLACEHOLDER = "（上一条回答无效）"


def strip_citation_marks(text: str) -> str:
    """剥掉引用角标，并收敛多余空白（实现已统一到 fact_extract）。"""
    return strip_marks(text)


def is_invalid_answer(text: Optional[str]) -> bool:
    """这条助手回答能不能当事实用（形态判定）。

    空答也算无效——没有内容的历史只占 token。
    """
    if not text or not text.strip():
        return True
    return any(m in text for m in INVALID_ANSWER_MARKERS)


def sanitize_history(history: Optional[list[dict]]) -> tuple[list[dict], dict]:
    """按契约清洗历史，返回 (新历史, 统计)。

    统计用于日志与排查：本轮吃了多少脏东西（这次清洗省掉了几条、剥了几个角标）。

    不修改入参（调用方可能还要用原始 history 做别的判断，比如改写）。
    """
    stats = {"total": 0, "invalid": 0, "citations": 0}
    if not history:
        return [], stats

    cleaned: list[dict] = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or ""
        content = msg.get("content") or ""
        stats["total"] += 1

        # 失败回答只对 assistant 生效：user 说了什么都是有信息量的（哪怕是"在吗"）
        if role == "assistant" and is_invalid_answer(content):
            stats["invalid"] += 1
            cleaned.append({**msg, "content": INVALID_ANSWER_PLACEHOLDER})
            continue

        before = content
        content = strip_citation_marks(content)
        if content != before:
            stats["citations"] += 1
        cleaned.append({**msg, "content": content})

    return cleaned, stats


def sanitize_history_safe(history: Optional[list[dict]]) -> list[dict]:
    """清洗的永不抛版本（历史清洗不该成为主链路的新失败点）。"""
    try:
        cleaned, stats = sanitize_history(history)
        if stats["invalid"] or stats["citations"]:
            logger.info(
                f"[memory] 历史已清洗: total={stats['total']} "
                f"invalid={stats['invalid']} citations={stats['citations']}"
            )
        return cleaned
    except Exception as e:  # pragma: no cover - 兜底分支
        logger.warning(f"[memory] 历史清洗失败，用原文: {e}")
        return history or []


def can_use_cache(history: Optional[list[Any]]) -> bool:
    """多轮是否允许读写响应缓存。

    缓存 key 不含会话状态，多轮里同一句 query 在不同上下文答案不同
    （"退货运费谁出"：正常退货买家承担 / 质量问题商家承担）。
    命中上一轮答案就是确定性答反，所以只要有历史就不用缓存。
    """
    return not history


# =============================================================
# 预算：从「条数」改成「轮 + 字符」
# =============================================================
def _budget_rules() -> tuple[int, int, int, int]:
    """(max_rounds, char_budget, max_msg_chars, compact_summary_chars)。

    配置文件缺失不该让主链路挂——几个数都有默认值，宁可预算用默认口径。
    """
    try:
        from app.services.config_loader import get_config_loader

        rules = get_config_loader().load("memory")
    except Exception as e:  # pragma: no cover - 仅在配置缺失时触发
        logger.warning(f"[memory] memory.yaml 加载失败，用默认预算: {e}")
        rules = {}
    return (
        int(rules.get("MAX_HISTORY_ROUNDS", 10)),
        int(rules.get("HISTORY_CHAR_BUDGET", 1800)),
        int(rules.get("MAX_SINGLE_MSG_CHARS", 600)),
        int(rules.get("COMPACT_SUMMARY_CHAR_BUDGET", 240)),
    )


def split_rounds(history: Optional[list[dict]]) -> list[list[dict]]:
    """把消息流切成「轮」：user 起头，其后到下一个 user 之前都算同一轮。

    为什么按轮而不是按条丢：只丢 assistant 会留下一个没有回答的问题，
    只丢 user 会让模型看到"助手自问自答"。裁剪单位必须是轮。
    """
    rounds: list[list[dict]] = []
    current: list[dict] = []
    for msg in history or []:
        if not isinstance(msg, dict):
            continue
        if (msg.get("role") or "") == "user":
            if current:
                rounds.append(current)
            current = [msg]
        elif current:
            current.append(msg)
        else:
            # 历史开头就是 assistant（回填截断的产物）→ 单独成段
            rounds.append([msg])
    if current:
        rounds.append(current)
    return rounds


# =============================================================
# 被裁轮次的要点摘录
# =============================================================
# 为什么要有这一层：原实现超预算就整轮丢。买家第 2 轮说"预算 500"，
# 第 50 轮问推荐——预算不在 L2 白名单槽位里，原文又被裁掉，事实就永久消失。
# 但也不能让 LLM 写自由文本摘要：不可验证、会复读兜底话术、还可能带出订单号。
# 所以只抽"能被模式证明"的短句。

# 抽取规则统一在 fact_extract（原来这里和 L2/L3 各有一套正则，口径不一致）。
# 2026-11-17 补了两类：助手承诺（已登记补发）和否定约束（排除：黑色）。
# 原来只抽买家原话，导致"你上次说给我补发"无据可查，模型只能否认。


def compact_round(
    round_msgs: Optional[list[dict]],
    max_sents: int = 2,
    known_facts: Optional[dict] = None,
) -> str:
    """把一轮（或多轮）里"能证明的事实"压成一行。抽不到就返回空串。

    Args:
        known_facts: L2 会话事实。摘要里与它冲突的实体要丢掉——
            买家第 5 轮说 170、第 8 轮改口 175，摘要还写着 170，
            两处同时进 prompt，模型答哪个全看运气。
    """
    if not round_msgs:
        return ""

    facts = extract_round_facts([round_msgs])
    entities = facts["entities"]
    if known_facts:
        entities = [e for e in entities if not conflicts_with_known(e, known_facts)]

    parts = [e["text"] for e in entities[:max_sents]]
    # 承诺只留前两条：摘要用于"追责"，不需要逐条复述
    if facts["commitments"]:
        parts.append("已办：" + "、".join(facts["commitments"][:2]))
    # 已写在实体原句里的约束不重复（"别要黑色的" → 不再加"排除：黑色"）
    extra = [c for c in facts["constraints"] if not any(c in p for p in parts)]
    if extra:
        parts.append("排除：" + "、".join(extra[:2]))
    return "；".join(parts)


def fit_history_budget(
    history: Optional[list[dict]],
    max_rounds: Optional[int] = None,
    char_budget: Optional[int] = None,
    max_msg_chars: Optional[int] = None,
    summary_budget: Optional[int] = None,
    known_facts: Optional[dict] = None,
) -> tuple[list[dict], dict]:
    """按轮 + 字符预算裁剪历史，返回 (新历史, 统计)。

    规则：
    - 从最新一轮往旧取，超预算就停（不切半轮）
    - **至少保留最后一轮**：本轮问题的上文全丢了，比预算超标更糟
    - 单条超长截断（保留开头，"…"结尾）——结论通常在开头
    - 预算按截断后的长度算，否则一条 5000 字答案会让后面所有轮都进不来
    """
    stats = {
        "rounds": 0, "kept_rounds": 0, "dropped_rounds": 0, "chars": 0,
        "truncated": 0, "compacted_rounds": 0, "summary_chars": 0,
    }
    if not history:
        return [], stats

    if (
        max_rounds is None or char_budget is None
        or max_msg_chars is None or summary_budget is None
    ):
        d_rounds, d_chars, d_msg, d_summary = _budget_rules()
        max_rounds = d_rounds if max_rounds is None else max_rounds
        char_budget = d_chars if char_budget is None else char_budget
        max_msg_chars = d_msg if max_msg_chars is None else max_msg_chars
        summary_budget = d_summary if summary_budget is None else summary_budget

    rounds = split_rounds(history)
    stats["rounds"] = len(rounds)

    kept: list[list[dict]] = []
    used = 0
    for rnd in reversed(rounds):
        if len(kept) >= max_rounds:
            break
        # 按截断后长度预估，保证预算判断跟最终字符数一致
        cost = sum(
            min(len(m.get("content") or ""), max_msg_chars) for m in rnd
        )
        if kept and used + cost > char_budget:
            break
        kept.append(rnd)
        used += cost
    kept.reverse()

    out: list[dict] = []
    for rnd in kept:
        for msg in rnd:
            content = msg.get("content") or ""
            if len(content) > max_msg_chars:
                content = content[:max_msg_chars] + "…"
                stats["truncated"] += 1
            out.append({**msg, "content": content})

    # 被裁掉的轮次不是直接蒸发：先摘出"能证明的短句"，放在历史最前面。
    # 摘要用独立 role，由 _format_history 原样渲染（不加"用户/助手"前缀）。
    kept_ids = {id(r) for r in kept}
    dropped = [r for r in rounds if id(r) not in kept_ids]
    summary_parts = []
    for rnd in dropped:
        one = compact_round(rnd, known_facts=known_facts)
        if one:
            summary_parts.append(one)
            stats["compacted_rounds"] += 1
    summary_prefix = "【较早对话要点】"
    if summary_parts and summary_budget > len(summary_prefix) + 1:
        summary = "；".join(summary_parts)
        # 每个被裁轮各自压出一条"已办：X"，合并成一串（否则摘要里五个"已办："）
        summary = summary.replace("；已办：", "、")
        avail = summary_budget - len(summary_prefix)
        if len(summary) > avail:
            summary = summary[: avail - 1] + "…"
        out.insert(0, {"role": "summary", "content": summary_prefix + summary})
        stats["summary_chars"] = len(out[0]["content"])

    stats["kept_rounds"] = len(kept)
    stats["dropped_rounds"] = len(rounds) - len(kept)
    stats["chars"] = sum(len(m.get("content") or "") for m in out)
    return out, stats


def prepare_history_safe(
    history: Optional[list[dict]],
    known_facts: Optional[dict] = None,
) -> list[dict]:
    """装配前的完整处理：清洗 → 预算。

    调用方只需换这一处；清洗/预算各自的失败都不该让主链路 500。
    known_facts 传 L2 会话事实，用来压掉摘要里已被改口覆盖的旧值。
    """
    cleaned = sanitize_history_safe(history)
    try:
        fitted, stats = fit_history_budget(cleaned, known_facts=known_facts)
        if stats["dropped_rounds"] or stats["truncated"]:
            logger.info(
                f"[memory] 历史已按预算裁剪: rounds={stats['rounds']} "
                f"kept={stats['kept_rounds']} dropped={stats['dropped_rounds']} "
                f"chars={stats['chars']} truncated={stats['truncated']}"
            )
        return fitted
    except Exception as e:  # pragma: no cover - 兜底分支
        logger.warning(f"[memory] 历史预算裁剪失败，用清洗结果: {e}")
        return cleaned
