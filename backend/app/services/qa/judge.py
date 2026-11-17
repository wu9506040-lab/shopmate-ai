"""质检 LLM 层 —— 四维打分 + 批次校准。

三条硬约束（都是「看起来能用、上线就崩」的地方）：
1. **温度 0 + 只输出 JSON**：质检分数要能进趋势图，同一段对话两次评分差 2 分就等于没有分；
2. **维度分优先于总分**：总分是均值，均值会把「答得漂亮但依据是编的」拉到及格线，
   所以判定用 min(维度) 而不是均值；
3. **每批必须混入已知坏答案**：LLM 有讨好倾向，通顺的坏答案爱给 4-5 分。
   坏答案拿高分 → 这一批全部标记 calibrated=False，只能留档不能当结论。
   —— 质检自己也要被质检，否则看板上会出现「合格率 96%」而客诉照旧。
"""
import json
import logging
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DIMS = ("relevance", "grounding", "consistency", "actionable")
DIM_CN = {"relevance": "答在点上", "grounding": "依据可查",
          "consistency": "口径一致", "actionable": "给出下一步"}

SYSTEM = (
    "你是服饰电商客服质检员，只负责打分，不改写答案。"
    "严格按给定四维各打 1-5 分，只输出 JSON，不要任何解释性文字。"
)

PROMPT = """【买家问】
{q}

【AI 答】
{a}

【当轮引用资料摘要】
{ctx}

打分口径（1 分=完全不行，5 分=可以直接发给买家）：
- relevance 答在点上：是否回答了买家真正问的那件事
- grounding 依据可查：答案里的数字/政策是否来自上面资料，有无编造
- consistency 口径一致：是否与现行政策一致，有无前后矛盾、拿过期活动当现行
- actionable 给出下一步：买家看完知不知道该做什么（怎么拍/怎么退/找谁）

注意：若「当轮引用资料摘要」为空，grounding 一律给 3 分——**无法核验不等于编造**，
资料没取到是系统的事，不该让答案背锅（否则订单类、问候类全部被打成低分）。

输出 JSON（不要 markdown 代码块）：
{{"relevance": 4, "grounding": 3, "consistency": 5, "actionable": 2,
  "failure": "none|retrieval|generation|policy|safety",
  "reason": "一句话说清扣分点"}}
"""

# 校准样本：两条「通顺但错」的答案。任何合格质检员都应给 3 分以下。
BAD_SAMPLES: Tuple[Dict, ...] = (
    {
        "q": "签收一个月了还能无理由退吗",
        "a": ("可以的，我们支持 7 天无理由退货，运费我们承担，"
              "另外给您补偿 200 元，您直接寄回就行。"),
        "ctx": "退换货政策：签收 7 天内可申请无理由退换；质量问题 15 天内。",
    },
    {
        "q": "这件加绒夹克多少钱",
        "a": "这件现在 199 元，比双 11 还便宜 30 元，很划算。",
        "ctx": "加绒夹克 128 元；双 11 期间 9 折。",
    },
)


def _parse(reply: str) -> Optional[Dict]:
    if not reply:
        return None
    s = re.sub(r"^```(?:json)?|```$", "", reply.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    out = {}
    for k in DIMS:
        try:
            v = int(round(float(d.get(k))))
        except (TypeError, ValueError):
            return None
        out[k] = max(1, min(5, v))
    out["failure"] = str(d.get("failure") or "none")[:24]
    out["reason"] = str(d.get("reason") or "")[:200]
    return out


def score_of(dims: Dict) -> float:
    return round(sum(dims[k] for k in DIMS) / len(DIMS), 2)


def worst_dim(dims: Dict) -> int:
    return min(dims[k] for k in DIMS)


def judge_one(provider, query: str, answer: str, ctx: str = "") -> Optional[Dict]:
    """单条打分。解析失败返回 None（调用方保留规则层结论，不硬造分数）。"""
    try:
        resp = provider.chat(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": PROMPT.format(
                 q=(query or "")[:400], a=(answer or "")[:700], ctx=(ctx or "")[:600])}],
            temperature=0.0, max_tokens=220,
        )
    except Exception as e:
        logger.warning("质检打分调用失败: %s", e)
        return None
    return _parse((resp or {}).get("reply") or "")


def calibrate(provider, threshold: float = 3.0) -> Tuple[bool, float]:
    """批次校准：坏答案均分必须低于阈值。返回 (是否可信, 坏答案均分)。"""
    scores: List[float] = []
    for s in BAD_SAMPLES:
        d = judge_one(provider, s["q"], s["a"], s["ctx"])
        if d is None:
            return False, 0.0
        scores.append(score_of(d))
    avg = round(sum(scores) / len(scores), 2)
    return avg < threshold, avg
