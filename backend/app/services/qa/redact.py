"""质检专用脱敏。

为什么单独一份、不复用 trace 的 `_redact`：
- trace 只遮手机号与订单号，因为它落盘在自己机器上、只给排查用；
- 质检会把 query/answer **发给上游大模型**，属数据出境，口径必须更严：
  手机号 / 订单号 / 身份证 / 邮箱 一律打码。

不做地名与门牌号替换：正则一激进就会把「加绒」「M 码」这类正常词切碎，
反而让模型看不懂答案——宁可留地名也不能毁语义。
"""
import re

_RE_PHONE = re.compile(r"1[3-9]\d{9}")
_RE_IDCARD = re.compile(r"\d{17}[\dXx]")
_RE_ORDER = re.compile(r"[A-Z]{2,}\d[\w-]{4,}")
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def redact(text: str, max_len: int = 800) -> str:
    """打码后再截断。顺序不能反：先截断会把手机号切成半截漏掉。"""
    if not text:
        return ""
    s = str(text)
    s = _RE_IDCARD.sub(lambda m: m.group(0)[:6] + "********" + m.group(0)[-4:], s)
    s = _RE_PHONE.sub(lambda m: m.group(0)[:3] + "****" + m.group(0)[-4:], s)
    s = _RE_EMAIL.sub(lambda m: m.group(0)[:2] + "***@" + m.group(0).split("@")[-1], s)
    s = _RE_ORDER.sub(lambda m: m.group(0)[:7] + "***", s)
    return s[:max_len]
