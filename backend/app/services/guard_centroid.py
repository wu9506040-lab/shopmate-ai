"""
InputGuard 的领域 centroid —— 用代表性问题算电商领域的"中心 embedding"

为什么需要：
- L2 embedding 闲聊识别 = cosine(query, domain_centroid)
- centroid 是一次性算好缓存到 Redis 24h，命中时不用重算
- 代表性问题要覆盖：订单查询 / 商品咨询 / 物流 / 退换货 / 支付 等核心场景

设计：
- CENTROID_SEEDS：30 条高质量 query，覆盖电商各场景
- Redis key: guard:domain_centroid（JSON 数组 1024 维）
- 第一次启动时算，后续 24h 复用
- 计算失败 → 退化（返回 None，让 L2 跳过）
"""
import json
import logging
from typing import List, Optional

from app.clients.redis_client import get_client as redis_get
from app.core.providers.embedding import EmbeddingError, get_embedding_provider

logger = logging.getLogger(__name__)

CENTROID_REDIS_KEY = "guard:domain_centroid"
CENTROID_TTL_SECONDS = 86400  # 24h

# 30 条服饰电商领域代表性问题
# 覆盖：尺码 / 商品参数 / 洗护 / 退换 / 物流 / 活动 / 订单 / 客服
CENTROID_SEEDS: List[str] = [
    # 尺码
    "身高 165 体重 55 穿什么码",
    "XL 的胸围和衣长是多少",
    "腰围 68 选什么码",
    "毛衣平铺宽度怎么换算全围",
    "号型 175/92A 是什么尺码",
    "这件衣服偏大还是偏小",
    # 商品参数
    "加绒夹克是什么面料成分",
    "针织衫含羊毛吗",
    "短袖 T 恤克重多少",
    "西裤有哪几个颜色",
    "弯刀裤是什么版型",
    "货号 HM261139 是什么商品",
    # 洗护
    "毛衣会不会起球",
    "白色 T 恤发黄怎么洗",
    "加绒外套可以机洗吗",
    "深色衣服第一次洗掉色吗",
    # 退换货
    "七天无理由退货条件",
    "退货运费谁出",
    "质量问题 15 天内怎么退",
    "尺码拍错了能换码吗",
    "吊牌剪了还能退吗",
    # 物流 / 改单
    "什么时候能发货",
    "预售款多久发货",
    "满 99 元包邮吗",
    "发货前能改收货地址吗",
    # 活动
    "双十一活动有什么优惠",
    "618 活动现在还能参加吗",
    # 订单 / 客服
    "查一下我最近的订单",
    "我的订单到哪了",
    "联系人工客服",
]


def _get_cached_centroid() -> Optional[List[float]]:
    """从 Redis 取缓存的 centroid"""
    try:
        raw = redis_get().get(CENTROID_REDIS_KEY)
        if not raw:
            return None
        vec = json.loads(raw)
        if not isinstance(vec, list) or len(vec) != get_embedding_provider().get_dim():
            logger.warning(f"centroid 缓存格式异常: type={type(vec)} dim={len(vec) if isinstance(vec, list) else 'N/A'}")
            return None
        return vec
    except Exception as e:
        logger.warning(f"读 centroid 缓存失败: {e}")
        return None


def _save_centroid(vec: List[float]) -> None:
    """存到 Redis（24h TTL）"""
    try:
        redis_get().setex(
            CENTROID_REDIS_KEY,
            CENTROID_TTL_SECONDS,
            json.dumps(vec),
        )
        logger.info(f"domain centroid 已缓存: dim={len(vec)}, TTL={CENTROID_TTL_SECONDS}s")
    except Exception as e:
        logger.warning(f"存 centroid 缓存失败: {e}")


def get_domain_centroid() -> Optional[List[float]]:
    """
    获取电商领域 centroid（懒加载 + Redis 缓存）

    Returns:
        1024 维向量，或 None（计算失败时 — 调用方应跳过 L2 闲聊识别）
    """
    # 1. 先查缓存
    cached = _get_cached_centroid()
    if cached is not None:
        return cached

    # 2. 缓存未命中 → 现算
    try:
        logger.info(f"开始计算 domain centroid（{len(CENTROID_SEEDS)} 条 seeds）...")
        # DashScope embedding batch ≤ 10，分批调用
        embs: List[List[float]] = []
        BATCH = 10
        for i in range(0, len(CENTROID_SEEDS), BATCH):
            batch = CENTROID_SEEDS[i : i + BATCH]
            batch_embs = get_embedding_provider().embed_texts(batch)
            if not batch_embs or len(batch_embs) != len(batch):
                logger.error(f"embed_texts batch 异常: got={len(batch_embs) if batch_embs else 0}, expected={len(batch)}")
                return None
            embs.extend(batch_embs)
        if len(embs) != len(CENTROID_SEEDS):
            logger.error(f"总 embedding 数量异常: got={len(embs)}, expected={len(CENTROID_SEEDS)}")
            return None
        # 算平均（element-wise mean）— 这是 centroid
        dim = len(embs[0])
        centroid = [0.0] * dim
        for emb in embs:
            for i, v in enumerate(emb):
                centroid[i] += v
        n = len(embs)
        centroid = [v / n for v in centroid]
        # L2 normalize（与 embedding API 输出一致）
        norm = sum(v * v for v in centroid) ** 0.5
        if norm > 0:
            centroid = [v / norm for v in centroid]
        _save_centroid(centroid)
        return centroid
    except EmbeddingError as e:
        logger.error(f"算 centroid 失败（embedding 异常）: {e}")
        return None
    except Exception as e:
        logger.exception(f"算 centroid 失败（未知异常）: {e}")
        return None


def clear_centroid_cache() -> None:
    """清缓存（测试 / seeds 变更时用）"""
    try:
        redis_get().delete(CENTROID_REDIS_KEY)
        logger.info("domain centroid 缓存已清")
    except Exception as e:
        logger.warning(f"清 centroid 缓存失败: {e}")
