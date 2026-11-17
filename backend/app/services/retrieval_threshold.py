"""检索相似度阈值：按意图分档。

一个固定阈值同时服务"退货运费怎么算"和"你好"，必然有一头是错的：

- 政策类（退换/运费/发票/活动）答错代价最高，宁可这条不进候选、让上层走
  兜底话术，也不能把 0.42 分的弱相关条目喂给模型编规则。
- 商品事实类（尺码/面料/洗护）问法千变万化，阈值卡太死会漏召回。
- 寒暄/闲聊本来就没有对应知识库答案，卡 0.4 只会白白丢掉语气上下文。

因此按 query 意图决定阈值，档位从严到松：
    policy(0.52) > fact(0.45) > default(0.40) > chitchat(0.28)

这里只做"选阈值"，不改写 query、不动召回条数与排序 —— 用户问题的意图
始终是原句本身。
"""

from __future__ import annotations

import logging
from typing import Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)

# 档位名（用于日志与灰度观测）
TIER_POLICY = "policy"
TIER_FACT = "fact"
TIER_CHITCHAT = "chitchat"
TIER_DEFAULT = "default"

# 政策/售后/交易类：命中即按最严档
_POLICY_WORDS = (
    "退换", "退货", "退款", "换货", "无理由", "七天", "7天",
    "运费", "运费险", "包邮", "邮费",
    "发货", "物流", "快递", "签收", "到货", "几天到",
    "售后", "保修", "质保", "维修", "破损", "污渍", "少发", "漏发", "错发",
    "发票", "开票", "优惠", "满减", "折扣", "活动", "券", "会员", "积分",
    "支付", "付款", "分期", "取消订单", "改地址", "改收货",
    "投诉", "客服", "人工", "隐私",
)

# 商品事实类：尺码、面料、洗护、参数
_FACT_WORDS = (
    "尺码", "码数", "号型", "选码", "偏大", "偏小", "合身",
    "胸围", "衣长", "肩宽", "腰围", "臀围", "裤长", "袖长", "腿围",
    "面料", "成分", "材质", "克重", "含毛", "含棉", "里料", "填充",
    "洗涤", "洗护", "机洗", "手洗", "干洗", "水洗", "起球", "缩水", "掉色", "色差",
    "颜色", "配色", "库存", "现货", "补货", "断码",
    "款号", "货号", "参数", "版型", "修身", "宽松", "厚薄", "弹力", "保暖",
    "搭配", "适合", "身高", "体重",
)

# 寒暄/闲聊：没有知识库答案的问法
_CHITCHAT_WORDS = (
    "你好", "您好", "在吗", "在么", "在不在", "有人吗",
    "谢谢", "多谢", "感谢", "收到", "好的", "好滴", "嗯", "哦", "噢",
    "哈哈", "嘿嘿", "笑死", "你是谁", "你是机器人", "机器人",
    "能聊", "聊会", "早上好", "中午好", "下午好", "晚上好", "晚安",
    "拜拜", "再见", "辛苦了", "厉害",
)

# 极短且无实义词时按闲聊处理（"嗯嗯" "在的" "1"）
_SHORT_QUERY_LEN = 6


def _hit(text: str, words: Tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def classify(query: str) -> str:
    """判定 query 的意图档位（只读文本，不做改写）。"""
    text = (query or "").strip().lower()
    if not text:
        return TIER_DEFAULT

    # 政策词优先级最高：一句话里既要退换又问尺码时，按严档处理
    if _hit(text, _POLICY_WORDS):
        return TIER_POLICY
    if _hit(text, _FACT_WORDS):
        return TIER_FACT
    if len(text) <= _SHORT_QUERY_LEN and _hit(text, _CHITCHAT_WORDS):
        return TIER_CHITCHAT

    # 短句无实义词（"在的" "嗯嗯呢"）也归闲聊
    if len(text) <= 3 and not any(ch.isdigit() for ch in text):
        return TIER_CHITCHAT
    return TIER_DEFAULT


def threshold_for(tier: str) -> float:
    """档位 → 阈值（关闭分档时统一回落到 default 基线）。"""
    if not settings.RETRIEVAL_THRESHOLD_ENABLED:
        return settings.RETRIEVAL_THRESHOLD_DEFAULT
    return {
        TIER_POLICY: settings.RETRIEVAL_THRESHOLD_POLICY,
        TIER_FACT: settings.RETRIEVAL_THRESHOLD_FACT,
        TIER_CHITCHAT: settings.RETRIEVAL_THRESHOLD_CHITCHAT,
    }.get(tier, settings.RETRIEVAL_THRESHOLD_DEFAULT)


def resolve(query: str, relax: float = 0.0) -> Tuple[float, str]:
    """返回 (阈值, 档位)。

    Args:
        query: 用户原句（不做任何改写）
        relax: 降档量（空召回重试那一轮用），结果不低于 FLOOR
    """
    tier = classify(query)
    base = threshold_for(tier)
    value = base - max(0.0, relax)
    if relax > 0:
        value = max(value, settings.RETRIEVAL_THRESHOLD_FLOOR)
    return round(value, 3), tier
