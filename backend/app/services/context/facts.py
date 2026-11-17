"""会话事实抽取（context 写侧的纯函数层）

职责边界：
- 只做「从这一轮的输入里能确定什么事实」的判定，**不碰 DB、不写表**
- DB 读写归 ContextService；决策消费归 OrderContextResolver / refund_flow

为什么单独一层：
写侧的判据必须能被单测覆盖（不能靠起服务验证）。把正则、优先级、合并规则
放在纯函数里，单测不需要 MySQL/Redis 就能跑。

事实来源优先级（高 → 低）：
1. payload.order_no（用户从 OrderCard 点进来，平台注入，最可信）
2. intent 实体抽取的 entities.order_no
3. 本轮用户原话里的订单号正则
4. 历史里最近出现的订单号（仅当上面都没有时）

第 4 条是兜底：买家第二轮说「那能退吗」，订单号只存在于第一轮的记录里。
注意：历史里 assistant 消息也会回显订单号——这是**好事**，因为 assistant 只
会回显已经校验过归属的单号；但为降低误抓，仍然优先只看 user 消息，找不到
再退到全量。
"""
from __future__ import annotations

import re
from typing import Optional

# 订单号格式：ORD + 8 位日期 + 3-6 位大写字母数字
# 与 config/business_rules/order_context.yaml 的 ORDER_NO_PATTERN、
# prompt_assembler._ORDER_NO_RE 保持一致（三处同源，改动要一起改）
ORDER_NO_RE: re.Pattern = re.compile(r"ORD\d{8}[A-Z0-9]{3,6}", re.IGNORECASE)

# resolved_orders 上限：买家来回切单不会超过这个数，超了丢最旧的
MAX_RESOLVED_ORDERS: int = 10


def is_valid_order_no_format(order_no: Optional[str]) -> bool:
    """只校验格式，不校验归属（归属要查库，归 ContextService 做）。"""
    if not order_no or not isinstance(order_no, str):
        return False
    return bool(ORDER_NO_RE.fullmatch(order_no.strip()))


def extract_order_no_from_text(text: Optional[str]) -> Optional[str]:
    """从单段文本里取第一个订单号（大小写归一为大写）。"""
    if not text:
        return None
    m = ORDER_NO_RE.search(text)
    return m.group(0).upper() if m else None


def extract_order_no_from_history(
    history: Optional[list[dict]],
) -> Optional[str]:
    """从历史里取最近一个订单号（先只看 user 消息，再退到全量）。

    Args:
        history: [{role, content}]，按时间正序（最旧 → 最新）

    Returns:
        订单号或 None
    """
    if not history:
        return None

    user_hit: Optional[str] = None
    any_hit: Optional[str] = None
    for msg in history:
        content = msg.get("content") or ""
        if not content:
            continue
        found = extract_order_no_from_text(content)
        if not found:
            continue
        # 正序遍历 → 后写覆盖前写，最终留下"最近一个"
        any_hit = found
        if (msg.get("role") or "") == "user":
            user_hit = found
    return user_hit or any_hit


def resolve_turn_order_no(
    history: Optional[list[dict]] = None,
    query: Optional[str] = None,
    entities: Optional[dict] = None,
    order_no: Optional[str] = None,
) -> Optional[str]:
    """按优先级判定「这一轮到底在说哪个订单」。

    Returns:
        格式合法的订单号；一个都没有则 None。
        注意：这里**不做归属校验**，调用方必须再过一道 ContextService 的归属检查，
        否则会把别人的订单号记进本会话（越权写入）。
    """
    candidates: list[Optional[str]] = [
        order_no,
        (entities or {}).get("order_no"),
        extract_order_no_from_text(query),
        extract_order_no_from_history(history),
    ]
    for cand in candidates:
        if cand and is_valid_order_no_format(cand):
            return cand.strip().upper()
    return None


def merge_resolved_orders(
    existing: Optional[list], new_order_no: Optional[str],
) -> list:
    """把新订单号并进 resolved_orders：去重、保序（新→旧）、截断到上限。

    Args:
        existing: 库里已有的列表（可能是 [{"order_no": ...}] 或 [str]）
        new_order_no: 本轮判定出的订单号

    Returns:
        新的列表（最多 MAX_RESOLVED_ORDERS 项，最新在前）
    """
    items: list[str] = []
    if new_order_no:
        items.append(new_order_no)
    for it in existing or []:
        if isinstance(it, dict):
            val = it.get("order_no")
        else:
            val = it
        if val and isinstance(val, str):
            items.append(val)

    seen: set[str] = set()
    deduped: list[str] = []
    for it in items:
        key = it.upper()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped[:MAX_RESOLVED_ORDERS]


def should_use_context_order(intent: Optional[str]) -> bool:
    """只有订单类意图才允许把 current_order_no 当「当前单」。

    商品/政策咨询里的「这个」「那件」指商品，不是订单；一旦把旧订单号带进去，
    就会出现"买家问衣服，助手答订单"的串单。这是本模块最容易出错的地方，
    所以用白名单而不是黑名单。
    """
    return (intent or "") in {"order_query", "refund_query", "logistics_query"}


# =============================================================
# 服装事实：尺码 / 身高 / 体重（2026-11 记忆预算同批）
# =============================================================
# 为什么单独抽这三个：它们只活在 history 里。第 3 轮说过 170/60，到第 8 轮
# 只能靠 20 条窗口侥幸捞到，滚掉就重新问一遍——买家最烦的重复提问。
#
# 难点不是"抓不到"，是"抓错"。下面每条规则都对应一个真实误抓来源：
#   170~180cm  ← 商品尺码表原文，不是买家身高
#   ¥199       ← 价格
#   S/M/L      ← 尺码对照表的表头，不是买家尺码
#   订单号里的数字 ← 12 位里有的是 4 位数
# 宁可漏抓（买家再说一遍），也不能抓错（按错的身高推荐码数）。

FACT_KEYS: tuple[str, ...] = ("size", "height_cm", "weight_kg")

# 尺码 token：前后不能紧跟字母数字（避免 "XL" 命中的 "L"、"M码" 之外的东西）
_SIZE_RE: re.Pattern = re.compile(
    r"(?<![A-Za-z0-9])(\d?X{0,3}[SML]|均码)(?![A-Za-z0-9])", re.IGNORECASE
)
# 断码表里的 S 后面是 "/" 或 "、"，买家说自己的码通常带"码"字或前置"我穿"
_SIZE_SUFFIX = ("码",)
_SIZE_PREFIX_HINTS = ("我穿", "我拿", "我要", "我买", "平时", "穿", "拿")

# 区间符号：命中位置前后 2 字符内有这些，说明这串数字是"范围"，不是买家数据
_RANGE_MARKS = ("~", "～", "-", "–", "—", "至", "到", "—")

# 身高：带单位（cm/厘米/公分）
_HEIGHT_CM_RE: re.Pattern = re.compile(
    r"(?<![\d.])(1[4-9]\d|20\d)\s*(?:cm|CM|厘米|公分)"
)
# 身高：口语（1米7 / 1米75 / 1.7米 / 1.75米）
# 两个分支语义不同：小数部分 1 位是 ×10（1.7→170）、2 位是原值（1.75→175）
_HEIGHT_M_RE: re.Pattern = re.compile(
    r"(?<![\d.])1\s*(?:[.．]\s*(\d{1,2})|米\s*(\d{1,2}))"
)
# 身高：中文数字口语（一米七五 / 一米七）——线下买家很常说，不认就是漏
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_HEIGHT_CN_RE: re.Pattern = re.compile(
    r"[一1]\s*米\s*([零一二三四五六七八九]{1,2})"
)
# 身高：裸数字（必须靠"身高"提示词，见 _extract_height）
_HEIGHT_BARE_RE: re.Pattern = re.compile(r"(?<![\d.])(1[4-9]\d|20\d)(?![\d.])")

# 体重：kg / 斤（斤要换算，但原值一起留，避免四舍五入来回失真）
_WEIGHT_KG_RE: re.Pattern = re.compile(
    r"(?<![\d.])(\d{2,3}(?:\.\d)?)\s*(?:kg|KG|公斤|千克)"
)
_WEIGHT_JIN_RE: re.Pattern = re.compile(r"(?<![\d.])(\d{2,3}(?:\.\d)?)\s*(?:斤|市斤)")

# 裸数字（2-3 位）：身高/体重的无单位写法共用，是否采纳由各自的上下文规则决定
_BARE_NUM_RE: re.Pattern = re.compile(r"(?<![\d.])(\d{2,3}(?:\.\d)?)(?![\d.])")

# 数字后面跟这些词，说明它在说别的（价格/折扣/年份/数量），不是身体数据
_NOT_BODY_SUFFIX = ("元", "块", "折", "岁", "天", "单", "件", "个", "月", "年", "号", "分", "克")

HEIGHT_RANGE = (140, 210)
WEIGHT_KG_RANGE = (30, 180)


def _near(text: str, start: int, end: int, span: int = 3) -> str:
    """取命中位置前后 window（用于判断是不是区间/是不是价格）。"""
    return text[max(0, start - span):min(len(text), end + span)]


def _is_range(text: str, start: int, end: int) -> bool:
    """命中数字是否处于一个区间里（170~180 / 170-180 / 170至180）。"""
    return any(m in _near(text, start, end, span=2) for m in _RANGE_MARKS)


def _looks_like_other_number(text: str, end: int) -> bool:
    """数字后面紧跟价格/年份等单位 → 不是身体数据。"""
    tail = text[end:end + 1]
    return tail in _NOT_BODY_SUFFIX


def _extract_height_cm(text: str) -> Optional[int]:
    """身高（cm）。顺序：带单位 → 1米7 口语 → 裸数字（最严）。"""
    for m in _HEIGHT_CM_RE.finditer(text):
        if _is_range(text, m.start(), m.end()):
            continue
        v = int(m.group(1))
        if HEIGHT_RANGE[0] <= v <= HEIGHT_RANGE[1]:
            return v

    for m in _HEIGHT_M_RE.finditer(text):
        if _is_range(text, m.start(), m.end()):
            continue
        digits = m.group(1) or m.group(2)
        if not digits:
            continue
        # "1.7米" 小数 1 位 → ×10；"1.75米" 2 位 → 原值
        v = 100 + int(digits) * (10 if len(digits) == 1 else 1)
        if HEIGHT_RANGE[0] <= v <= HEIGHT_RANGE[1]:
            return v

    for m in _HEIGHT_CN_RE.finditer(text):
        if _is_range(text, m.start(), m.end()):
            continue
        cn = m.group(1)
        digits = "".join(str(_CN_DIGITS[c]) for c in cn)
        v = 100 + int(digits) * (10 if len(digits) == 1 else 1)
        if HEIGHT_RANGE[0] <= v <= HEIGHT_RANGE[1]:
            return v

    # 裸数字：默认不抓。只有「身高170」或「我170，60kg」这种上下文才认，
    # 否则 ¥199、订单里的 2026 都会被当成身高。
    for m in _HEIGHT_BARE_RE.finditer(text):
        if _is_range(text, m.start(), m.end()) or _looks_like_other_number(text, m.end()):
            continue
        before4 = text[max(0, m.start() - 4):m.start()]
        after4 = text[m.end():m.end() + 4]
        if before4[-1:] in ("¥", "￥", "."):
            continue
        if "身高" in before4:
            pass
        elif "我" in before4[-2:] and any(k in after4 for k in ("体重", "斤", "kg", "穿", "，", ",")):
            pass
        else:
            continue
        v = int(m.group(1))
        if HEIGHT_RANGE[0] <= v <= HEIGHT_RANGE[1]:
            return v
    return None


def _extract_weight_kg(text: str) -> Optional[float]:
    """体重（kg）。斤自动除 2；裸数字只认「体重60」这种写法。"""
    for m in _WEIGHT_KG_RE.finditer(text):
        if _is_range(text, m.start(), m.end()):
            continue
        v = float(m.group(1))
        if WEIGHT_KG_RANGE[0] <= v <= WEIGHT_KG_RANGE[1]:
            return round(v, 1)

    for m in _WEIGHT_JIN_RE.finditer(text):
        if _is_range(text, m.start(), m.end()):
            continue
        v = float(m.group(1)) / 2
        if WEIGHT_KG_RANGE[0] <= v <= WEIGHT_KG_RANGE[1]:
            return round(v, 1)

    for m in _BARE_NUM_RE.finditer(text):
        before4 = text[max(0, m.start() - 4):m.start()]
        if "体重" not in before4:
            continue
        if _is_range(text, m.start(), m.end()) or _looks_like_other_number(text, m.end()):
            continue
        v = float(m.group(1))
        if WEIGHT_KG_RANGE[0] <= v <= WEIGHT_KG_RANGE[1]:
            return round(v, 1)
    return None


def _extract_size(text: str) -> Optional[str]:
    """尺码。必须带「码」字或前置「我穿/我要/平时穿」。

    不加这两道，「尺码表：S/M/L/XL 都有货」里的 S 会被记成买家穿 S。
    """
    for m in _SIZE_RE.finditer(text):
        token = m.group(1).upper()
        after1 = text[m.end():m.end() + 1]
        before4 = text[max(0, m.start() - 4):m.start()]
        if any(after1.startswith(s) for s in _SIZE_SUFFIX) or any(
            h in before4 for h in _SIZE_PREFIX_HINTS
        ):
            return token
    return None


def extract_clothing_facts(text: Optional[str]) -> dict:
    """从一段文本里抽服装事实（尺码/身高/体重）。抽不到就是空 dict。

    只返回**白名单** key。订单号、地址、手机号不在其中——它们走
    current_order_no / tool 参数，永不进 prompt 文本。
    """
    if not text or not isinstance(text, str):
        return {}

    facts: dict = {}
    size = _extract_size(text)
    height = _extract_height_cm(text)
    weight = _extract_weight_kg(text)
    if size:
        facts["size"] = size
    if height:
        facts["height_cm"] = height
    if weight:
        facts["weight_kg"] = weight
    return facts


def flatten_facts(stored: Optional[dict]) -> dict:
    """把库里的 facts 结构压平成 {key: value}（兼容裸值与 {value, prev} 两种）。"""
    out: dict = {}
    for k, cell in (stored or {}).items():
        if k not in FACT_KEYS:
            continue
        v = cell.get("value") if isinstance(cell, dict) else cell
        if v is not None:
            out[k] = v
    return out


def merge_facts(
    existing: Optional[dict],
    new: Optional[dict],
    updated_at: Optional[str] = None,
) -> tuple[dict, list[dict]]:
    """合并事实并记录改口，返回 (新 facts, 覆盖事件列表)。

    改口语义：新值直接覆盖旧值，旧值留在 prev 里备查。
    买家说「我记错了，是 175」——不是两个事实并存，是一个事实更新了。
    history 里两条并列时模型会自己挑一个，那才是真问题。
    """
    facts: dict = dict(existing or {})
    changes: list[dict] = []
    for key, value in (new or {}).items():
        if key not in FACT_KEYS or value is None:
            continue
        cell = facts.get(key)
        old = cell.get("value") if isinstance(cell, dict) else cell
        if old == value:
            continue
        facts[key] = {
            "value": value,
            "prev": old,
            "updated_at": updated_at,
        }
        if old is not None:
            changes.append({"key": key, "from": old, "to": value})
    return facts, changes


# 「我记错了」类改口句：正常抽取规则抓不到，因为它没有单位也没有「身高」字样。
# 只在文本带这些标记、且库里已有同类事实时才启用，见 extract_corrections。
_CORRECTION_MARKS: tuple[str, ...] = (
    "记错", "说错", "搞错", "不对", "应该是", "改成", "改一下", "更正", "我刚才说",
)
# 改口是小幅修正，不是从 170 改成 60。差得太多说明那个数字在说别的事
_CORRECTION_MAX_DELTA = {"height_cm": 40, "weight_kg": 25}


def extract_corrections(text: Optional[str], known: Optional[dict]) -> dict:
    """从改口句里识别"哪个事实被改成了什么"。

    例：known={"height_cm": 170}，文本「我记错了，是175」→ {"height_cm": 175}

    为什么需要这个：`175` 裸数字没有任何单位/提示词，正常规则一律不抓。
    但它配合"记错"就是明确的事实更新。判据用「离旧值最近」消歧——
    同一句里 175 既在身高范围也在体重范围，只有和 170 比才说得通。
    """
    if not text or not known:
        return {}
    if not any(m in text for m in _CORRECTION_MARKS):
        return {}

    numbers = [float(m.group(1)) for m in _BARE_NUM_RE.finditer(text)]
    if not numbers:
        return {}

    out: dict = {}
    for key, old in known.items():
        rng = {"height_cm": HEIGHT_RANGE, "weight_kg": WEIGHT_KG_RANGE}.get(key)
        if rng is None or old is None:
            continue
        try:
            old_val = float(old)
        except (TypeError, ValueError):
            continue
        candidates = [n for n in numbers if rng[0] <= n <= rng[1]]
        if not candidates:
            continue
        best = min(candidates, key=lambda n: abs(n - old_val))
        if abs(best - old_val) > _CORRECTION_MAX_DELTA.get(key, 40):
            continue
        out[key] = int(best) if key == "height_cm" else round(best, 1)
    return out


def facts_from_history(
    history: Optional[list[dict]], max_msgs: int = 6
) -> dict:
    """从最近几条 user 消息里补抽事实（首轮写入丢失时的补偿）。

    与订单号同一套补偿思路：Java 网关代落库时首轮写不进去，但下一轮
    history 里还留着原话。只取 user 消息——assistant 会复述尺码表，
    从它嘴里抽出来的"L"可能是表格里的示例。
    """
    if not history:
        return {}
    picked: list[str] = []
    for msg in reversed(history):
        if (msg.get("role") or "") != "user":
            continue
        content = msg.get("content") or ""
        if content:
            picked.append(content)
        if len(picked) >= max_msgs:
            break
    merged: dict = {}
    for content in reversed(picked):  # 旧 → 新，后覆盖前
        merged.update(extract_clothing_facts(content))
        # 历史里也允许改口句（「先说170，后说记错了是175」），
        # 所以每条都要拿当前已知值去消歧
        merged.update(extract_corrections(content, merged))
    return merged

