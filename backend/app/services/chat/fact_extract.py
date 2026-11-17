"""会话事实抽取：从对话文本里抽「能被模式证明」的三类事实。

## 为什么单独成模块

同一个"175 码"，现在有三套抽法：
  - L2 白名单（`prompt_assembler._FACT_LABELS`：size / height_cm / weight_kg）
  - L3 正则（`l3_capture.extract_fabric_pref`：面料偏好）
  - 摘要正则（`memory_contract._COMPACT_PATTERNS`：金额 / 尺寸 / 偏好）

三套口径不一致，同一个事实在三个地方可能是三个值。这里先统一
「从对话文本抽事实」这一层；L2/L3 的迁移单独做（它们各有落库和 hits 语义，
一起动风险太大）。

## 三类事实

- **entities**：买家说过的可验证事实（尺码 / 预算 / 金额）→ 进摘要
- **commitments**：助手做过的承诺（已登记 / 已补发）→ 进摘要。
  这是"你上次说给我补发"的**唯一依据**——原来摘要只抽买家原话，
  助手说的承诺一条都进不去，第 12 轮买家追问时模型只能否认。
- **constraints**：买家的否定约束（别要黑色）→ 归一成"排除：黑色"。
  不归一的话，摘要里"要红色"和"不要红色"两句话并列，谁新谁旧靠模型猜。

## 规矩（沿用记忆契约）

1. **只抽原文**，不改写：错了能回到原话核对。
2. **整句含敏感项就整句不抽**：订单号 / 手机号 / 身份证 / 地址。
   宁可少记一条，不做"去掉手机号留下别的"这种危险裁剪。
3. **只抽能被模式证明的**：不写自由文本摘要（会复读兜底话术）。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# 引用角标：只在拼 prompt 时剥，DB 和前端不动（BUGS 28 的假引用）
CITATION_RE = re.compile(r"[ \t]*\[\d{1,2}\]")

# 敏感项：出现即整句放弃
SENSITIVE_RE = re.compile(
    r"ORD\d{8}[A-Z0-9]{3,6}"          # 订单号
    r"|(?<!\d)1[3-9]\d{9}(?!\d)"       # 手机号（中文紧邻数字时 \b 不生效）
    r"|(?<!\d)\d{17}[\dXx](?!\d)"      # 身份证
    r"|(?:收货地址|住在|快递地址)"
)

# 分句符含逗号：一句话常同时装两个事实（"我穿165码，预算500左右"），
# 不切开会按第一个 key 判冲突，连带把不冲突的"预算500"一起丢掉。
_SENT_SPLIT_RE = re.compile(r"[。！？!?；;，,\n]+")

# (正则, key)。key 用于和 L2 比对冲突；text 仍是原句——模型看到的是人话，
# 不是 {"size": 175} 这种结构，结构只在代码里用。
_ENTITY_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(\d{2,3})\s*(?:码|cm|厘米|公分)"), "size"),
    (re.compile(r"预算\s*(\d+)"), "budget"),
    (re.compile(r"(\d+)\s*(?:元|块)"), "budget"),
    # 偏好/否定原句也要留一条：光有"排除：黑色"读不出买家原来怎么说，
    # 而"我不想买加绒的"这种半个事实一旦被裁，后面推荐就会跑偏。
    # 整体包一个捕获组：_entity_of 统一取 group(1)，各 pattern 都得有组。
    (re.compile(r"((?:不想买|不想要|不喜欢|不要|别要|别买|喜欢|讨厌|偏好|怕)"
                r"[^\s，。！？!?；;]{1,8})"), "pref"),
)

# 助手承诺：只认**有副作用**的动词。"已为您查询"不算——查询没承诺任何事，
# 抽进摘要会让模型以为"答应过要处理"。
_COMMITMENT_RE = re.compile(
    r"已(?:经)?(?:为您|帮您|给您|给你)?"
    r"(登记|提交|申请|安排|补发|退款|加急|备注|修改|处理)"
    r"([^。！？!?；;\n，,]{0,10})"
)

# 否定约束。**不放"怕"**——"怕冷"意思是需要保暖，抽成"排除：冷"会答反。
_NEGATION_RE = re.compile(
    r"(?:不要|别要|不用|不接受|不想要|不喜欢|讨厌)"
    r"([^\s，。！？!?；;\n]{1,6})"
)


def strip_marks(text: str) -> str:
    """剥引用角标 + 收敛空白。"""
    if not text:
        return text
    cleaned = CITATION_RE.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    return cleaned.strip()


def _dedup(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for it in items:
        if it and it not in out:
            out.append(it)
    return out


def _entity_of(sent: str) -> tuple[Optional[str], Optional[str]]:
    """返回 (key, value)。一句话同时含尺码和预算时只取第一个——
    第二条会用同一句文本再占一次摘要预算，不划算。"""
    for pattern, key in _ENTITY_PATTERNS:
        m = pattern.search(sent)
        if m:
            return key, m.group(1)
    return None, None


def _negations(sent: str) -> list[str]:
    return [m.group(1).strip() for m in _NEGATION_RE.finditer(sent)]


def _commitments(text: str) -> list[str]:
    out = []
    for m in _COMMITMENT_RE.finditer(text):
        phrase = (m.group(1) + m.group(2)).strip()[:12]
        if phrase not in out:
            out.append(phrase)
    return out


def extract_round_facts(rounds: Optional[list[list[dict]]]) -> dict:
    """从「轮」里抽三类事实。rounds 是 `split_rounds()` 的产物。

    Returns:
        {
          "entities":    [{"text": "我预算500元", "key": "budget", "value": "500"}],
          "commitments": ["登记补发"],
          "constraints": ["黑色"],
        }
    """
    entities: list[dict] = []
    commitments: list[str] = []
    constraints: list[str] = []
    seen_text: set[str] = set()

    for rnd in rounds or []:
        for msg in rnd:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role") or ""
            content = strip_marks(msg.get("content") or "")
            if not content:
                continue

            if role == "user":
                for sent in _SENT_SPLIT_RE.split(content):
                    sent = sent.strip(" ，,\t")
                    # 太短没信息（"在吗"），太长不是一条事实（是一段叙述）
                    if not 3 <= len(sent) <= 40:
                        continue
                    if SENSITIVE_RE.search(sent):
                        continue
                    key, value = _entity_of(sent)
                    if key and sent not in seen_text:
                        seen_text.add(sent)
                        entities.append({"text": sent, "key": key, "value": value})
                    constraints.extend(_negations(sent))

            elif role == "assistant":
                commitments.extend(_commitments(content))

    return {
        "entities": entities,
        "commitments": _dedup(commitments),
        "constraints": _dedup(constraints),
    }


def _norm(value: Any) -> str:
    """值归一：L2 里可能存 "175"、"175码"、"175 cm"，比数字就够。"""
    return re.sub(r"[^\d.]", "", str(value if value is not None else ""))


def conflicts_with_known(entity: dict, known_facts: Optional[dict]) -> bool:
    """摘要里的事实是否已被 L2 覆盖（同 key 不同值）。

    为什么必须判：买家第 5 轮说 170、第 8 轮改口 175，L2 已经是 175，
    而摘要（从被裁的旧轮次抽的）还写着 170。两处同时进 prompt，
    模型看到两个尺码，答哪个全看运气。
    """
    if not known_facts:
        return False
    cell = known_facts.get(entity.get("key") or "")
    if cell is None:
        return False
    value = cell.get("value") if isinstance(cell, dict) else cell
    if value is None or value == "":
        return False
    return _norm(value) != _norm(entity.get("value"))


__all__ = [
    "CITATION_RE",
    "SENSITIVE_RE",
    "conflicts_with_known",
    "extract_round_facts",
    "strip_marks",
]
