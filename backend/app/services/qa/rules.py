"""质检规则层 —— 零成本、全量、只看确定性缺陷。

设计原则（这是整个质检模块最重要的一条）：
**不要用 LLM 判「有没有编数字」这类硬事实。** 句级自检（answer_verifier）已经做了，
质检再写一套判据就是两套真相——同一轮对话，自检说拦了、质检说没问题，谁都不信。
所以规则层只做两件事：
  1. 读自检的结论（`answer_dropped`），不重写判据；
  2. 抓自检管不到、但确定是坏的形态（空答、兜底话、截断、承诺性话术）。

命中任何一条 = blocked，不打分、不进抽样，直接进结论——因为这些不需要人再判断。
"""
import re
from typing import Iterable, List, Optional

# 兜底/降级话术：出现即代表这轮没答成，不是「答得不好」
FALLBACK_PHRASES = (
    "这块资料我没查准",
    "系统繁忙",
    "换个说法再发",
    "暂时无法回答",
    "稍后再试",
    "无法为您解答",
)

# 承诺性话术：客服事故高发区，AI 不许自己加码。
# 只拦「谁向买家承诺了什么」，不拦词本身——KB 原文里就有「运费险赔付（最高 12 元）」，
# 按词拦会把正确引用判成事故（首版就误杀了 34 条，全是政策原文）。
PROMISE_PATTERNS = (
    r"(我们|商家|本店)(会|可以)?赔(偿)?",
    # 必须带「您」：政策原文「运费险赔付 12 元」不是承诺，按词拦会误杀（踩过两次）
    r"(赔|补|返)您\s*\d+\s*(元|块)",
    r"给您(赔|补|返)\s*\d+",
    r"保证(能|可以|一定|退|换)",
    r"一定能(退|换|到)",
    r"无条件(退|换)",
)

# 政策类意图：这类答案应该带引用角标 [1]
POLICY_INTENTS = ("policy_query", "faq")

_END_PUNCT = "。！？!?；;…~～）)\"」』"
_ANGLE_RX = re.compile(r"\[\d+\]")

LAYER_OF = {
    "empty_answer": "generation",
    "fallback_phrase": "generation",
    "self_check_dropped": "generation",
    "truncated": "length_fuse",
    "too_long": "length_fuse",
    "no_citation": "retrieval",
    "promise_language": "safety_policy",
}


def effective_len(text: str) -> int:
    """去掉空白与标点后的有效字数。4 字以下基本等于没说话（「好的呢～」）。"""
    return len(re.sub(r"[\s\W_]+", "", text or ""))


def check(
    answer: str,
    *,
    intent: Optional[str] = None,
    dropped: Optional[Iterable[str]] = None,
    min_chars: int = 4,
    max_chars: int = 1200,
) -> List[str]:
    """返回命中的规则码列表（空列表 = 规则层放行，可进抽样）。"""
    hits: List[str] = []
    a = answer or ""
    if effective_len(a) < min_chars:
        hits.append("empty_answer")
        return hits  # 空答不用再判别的了
    if any(p in a for p in FALLBACK_PHRASES):
        hits.append("fallback_phrase")
    if dropped:
        hits.append("self_check_dropped")
    if len(a) >= 180 and a.rstrip()[-1:] not in _END_PUNCT:
        # 截断的典型形态：够长、但结尾停在半句上（max_tokens 掐断）
        hits.append("truncated")
    if len(a) > max_chars:
        hits.append("too_long")
    if intent in POLICY_INTENTS and len(a) > 80 and not _ANGLE_RX.search(a):
        hits.append("no_citation")
    if any(re.search(p, a) for p in PROMISE_PATTERNS):
        hits.append("promise_language")
    return hits


def worst_layer(hits: Iterable[str]) -> Optional[str]:
    """把一个轮次的多条命中归到一个层，供看板聚合。

    优先级按「用户能不能感知到」排：没有正文 > 兜底话 > 截断 > 承诺 > 无引用。
    """
    order = ("empty_answer", "fallback_phrase", "self_check_dropped",
             "truncated", "too_long", "promise_language", "no_citation")
    hs = set(hits or ())
    for code in order:
        if code in hs:
            return LAYER_OF[code]
    return None
