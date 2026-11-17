"""句级答案自检（纯规则，0 token）

设计要点：
- 不按「整篇」缓冲，而按「句子」缓冲。首字只多等第一句生成完，
  不会把整段生成时间加到首字延迟上；前端仍是原来的 token 流，无需改动。
- 自检依据是 meta.contexts 里的资料预览（与 LLM 实际可见的 text[:500] 对齐）。
  因为主提示词要求每个事实句末标 [n]，可以纯字符串地检查：
    1. 引用编号是否超出实际资料条数
    2. 句里的数字能否在被引用资料的原文里找到（挡住「凭空编数字 / 串款数值」）
    3. 说了「我不知道」却还挂着引用标签
- 判不过只替换「那一句」，后续句子照常流；不重生成（越改越花）。
- 没有检索资料时不校验，避免误杀闲聊 / 引导 / 降级答案。
"""
from __future__ import annotations

import re
from typing import Optional

_CITE_RX = re.compile(r"\[(\d{1,2})\]")
_NUM_RX = re.compile(r"\d+(?:\.\d+)?")
# 标识符（订单号 / 货号 / SKU）：字母开头且带数字，如 ORD202611165CF92C、FS368998-M、HM261139。
# 这些串里的数字是标识符的一部分，不是「事实数字」，拿去和资料比对必然对不上。
# 实测踩过：模型复述用户订单号 ORD202611165CF92C，`202611165` 被判成幻觉数字，
# 整句被替换成兜底话术——问「什么时候发货」得到「资料没查准」，用户侧等同于故障。
_ID_RX = re.compile(r"[A-Za-z]{2,}\d[\w-]*")
# 日期整体跳过数字校验：`2026-09-15` 拆成 2026/09/15 三个数字去比对，
# 只要资料预览里少一个（截断或换成「2026 秋冬」写法）整句就被替换。
# 日期编造的风险远低于误杀代价——用户会拿日期去对页面，而误杀直接把答案变兜底话术。
_DATE_RX = re.compile(r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?")
_SENTENCE_END = "。！？!?\n"
# 店铺常驻政策事实：退换政策文档不一定每轮都进 top-k（问尺码时通常召回的是尺码指南），
# 但「7 天无理由 / 15 天质量保障 / 运费险最高 12 元」是固定的店铺承诺，
# 提示词也要求尺码结论后补一句试穿邀请。若按「资料里没有这个数字」拦掉，
# 用户问尺码时会看到「这块资料我没查准」——实测已发生。
# 因此只在这几个数字与政策关键词同句共现时放行，不扩大到任意数字。
_POLICY_NUMS = {"7", "15", "12"}
_POLICY_KW = ("无理由", "退换", "换码", "运费险", "质保", "售后")

FALLBACK_SENTENCE = "这块资料我没查准，建议转人工核对～"
# 拦句后剩多少有效字符才不认输。低于这个数说明核心结论被拦掉了，
# 此时必须补一句兜底话术，否则用户会收到一条"没头没尾"的消息。
FALLBACK_MIN_CHARS = 15


def collect_allowed_numbers(contexts: list[dict]) -> set[str]:
    """从检索资料里收集答案允许出现的数字。

    正文预览之外必须连 title / source / category 一起扫：商品款号 FS368998
    只出现在 source 与标题里，「2026 秋冬修订版」的年份只在标题里，
    漏掉会把正常答案误判成编数字（实测踩过）。
    """
    allowed: set[str] = set()
    for item in contexts or []:
        if not isinstance(item, dict):
            continue
        for key in ("text_preview", "title", "source", "category"):
            text = str(item.get(key) or "")
            allowed.update(m.group(0) for m in _NUM_RX.finditer(text))
    return allowed


def _is_unit_conversion(num: str, allowed: set[str]) -> bool:
    """数字是不是资料里某个值的单位换算（斤↔kg）。

    本店尺码资料用斤制（95~110 斤），模型答给用户时会换算成 kg（「95~110 斤即 47.5~55kg」）。
    47.5 在资料里当然找不到，但它是 95 斤的正确换算——按幻觉拦掉，用户看到的就是
    「这块资料我没查准」，而正确答案就在下一句。
    只认 2 倍关系（1kg = 2 斤），不放大到任意倍数，避免把真编造放过去。
    """
    try:
        v = float(num)
    except (TypeError, ValueError):
        return False
    for cand in (v * 2, v / 2):
        for text in (f"{cand:.4f}".rstrip("0").rstrip("."), str(int(cand)) if cand == int(cand) else None):
            if text and text in allowed:
                return True
    return False


def verify_sentence(sentence: str, allowed: set[str], doc_count: int) -> tuple[bool, str]:
    """校验一句话是否有资料依据。返回 (是否通过, 原因)。"""
    cites = [int(m.group(1)) for m in _CITE_RX.finditer(sentence)]
    if doc_count and cites and max(cites) > doc_count:
        return False, "cite_out_of_range"
    if "我不知道" in sentence and cites:
        return False, "unknown_with_cite"
    if allowed:
        body = _CITE_RX.sub("", sentence)
        body = _DATE_RX.sub("", body)  # 日期整体跳过（拆开比对必然误杀）
        body = _ID_RX.sub("", body)    # 摘掉订单号/货号，避免标识符里的数字被当事实数字
        policy_sentence = any(k in sentence for k in _POLICY_KW)
        for match in _NUM_RX.finditer(body):
            num = match.group(0)
            if num in allowed:
                continue
            if policy_sentence and num in _POLICY_NUMS:
                continue  # 店铺常驻政策数字（7 天无理由 / 15 天 / 运费险 12 元）
            if _is_unit_conversion(num, allowed):
                continue
            return False, "number_not_in_sources:" + num
    return True, ""


def _drain(buffer: str) -> tuple[list[str], str]:
    """只按句末标点切句，返回 (句子列表, 未成句残余)。

    不能用引用标签 `[n] - ` 或固定字数切：那会把尺码表 / 多资料段落从中间切开，
    替换句会插进表格行里（实测踩过）。没有句号的答案整段攒到 finish 一次判。
    """
    sentences: list[str] = []
    current = ""
    for char in buffer:
        current += char
        if char in _SENTENCE_END:
            sentences.append(current)
            current = ""
    return sentences, current


class SentenceVerifier:
    """把一个 token 流按句缓冲、逐句校验，输出可直接下发的文本片段。"""

    def __init__(self, contexts: Optional[list[dict]] = None,
                 user_query: Optional[str] = None):
        items = [c for c in (contexts or []) if isinstance(c, dict)]
        self.allowed = collect_allowed_numbers(items)
        # 用户自己说的数字（身高体重、订单里的年份等）答回复述是合理的，
        # 资料里未必有；不并入会把「您说的 168cm 对应 L 码」判成编数字。
        if user_query:
            q = _ID_RX.sub("", str(user_query))
            self.allowed |= {m.group(0) for m in _NUM_RX.finditer(q)}
        # 引用编号 [n] 对应 contexts 的第 n 条（1-based），所以上界是总条数。
        # 早先只数 type=="policy" 的条数：一旦检索结果混入商品条目，编号就错位，
        # 正常引用 [3] 会被判成「超出范围」而整句替换掉。
        self.doc_count = len(items)
        self.enabled = bool(self.allowed or self.doc_count)
        self.buffer = ""
        self.flagged = False
        self.reasons: list[str] = []
        # 被替换掉的原句：不记下来就永远不知道自检丢了什么，
        # 排查时只能看到「答案变短了」而无法判断是误杀还是真幻觉。
        self.dropped: list[dict] = []

    def feed(self, text: str) -> list[str]:
        if not text:
            return []
        if not self.enabled:
            return [text]
        self.buffer += text
        sentences, self.buffer = _drain(self.buffer)
        return [self._check(s) for s in sentences if s]

    def finish(self) -> str:
        if not self.buffer:
            return ""
        rest, self.buffer = self.buffer, ""
        return self._check(rest)

    def _check(self, sentence: str) -> str:
        ok, reason = verify_sentence(sentence, self.allowed, self.doc_count)
        if ok:
            return sentence
        self.flagged = True
        if reason:
            self.reasons.append(reason)
        # 原句留档（截断），供 trace 回放判断这次替换是误杀还是真拦住了幻觉
        try:
            self.dropped.append({"reason": reason, "sentence": sentence.strip()[:120]})
            if len(self.dropped) > 10:
                del self.dropped[: len(self.dropped) - 10]
        except Exception:  # noqa: BLE001
            pass
        # 拦下就**丢弃这一句**，不在这里塞兜底话术。
        # 曾经的写法是 replace 成「这块资料我没查准」，后果是：模型前半句给了正确结论、
        # 后半句自己算了个折后价（128×0.9=115.2，资料里没有）被拦，用户看到的是
        # 「参考价 128 元。这块资料我没查准」——前后自相矛盾，比少一句话更糟。
        # 是否认输交给 need_fallback() 按"剩下还有没有内容"判断。
        return ""

    def need_fallback(self, final_text: str) -> bool:
        """拦过句、且最终答案几乎没内容时才认输（补兜底话术）。

        判据是"剩余有效字符数"，不是"是否拦过句"：拦掉一句次要补充不该让整轮回答变成故障。
        """
        if not self.flagged:
            return False
        return len(re.sub(r"[\s\W_]+", "", final_text or "")) < FALLBACK_MIN_CHARS
