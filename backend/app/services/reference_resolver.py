"""多轮指代消解（商品维度）

问题：
    第一轮「这件夹克多少钱」答完，第二轮用户说「它洗了会缩水吗」。
    这时 query 里没有任何商品标识，系统只能靠对话历史的自然语言去猜。
    模型猜错就是「在毛衣语境里答夹克的面料」——比不答更糟。

做法：
    每个 session 记一个「最近咨询的商品」（Redis，TTL 30 分钟）。
    - 本轮带了 sku / model → 覆盖记录（用户主动切了商品，以新的为准）
    - 本轮没带，且 query 含指代词 → 把最近那个补回来

边界（重要）：
    1. **不改用户问题原文**。补的是上下文绑定，query 一个字都不动。
       这是硬约束——改写 query 会改变用户意图，检索再准也是答别的问题。
    2. 只补商品，不补话题。「那定制款能退吗」这类指代"上一轮在聊什么"
       的，交给 prompt 里的对话历史，不用结构化补全。
    3. 显式否定不补：「不要这个」「换一款」由 product_switch 处理，
       补全反而会把用户想离开的商品又拽回来。
"""
import json
import logging
import re
import time
from typing import Optional

from app.clients.redis_client import get_client as redis_get

logger = logging.getLogger(__name__)

# 会话内最近商品保留时长（30 分钟）——够覆盖一次连续咨询，
# 超过这个间隔用户多半已经换了话题，强行补全会指错。
LAST_PRODUCT_TTL_SECONDS = 1800

_KEY_PREFIX = "chat:last_product:"

# 指代词（指商品的那些）。刻意不收单字「这」「那」——
# 「这怎么说」「那不行吧」里它们是语气词，补全商品会让检索跑偏。
_REFERENCE_WORDS = (
    "这个", "这款", "这件", "这条", "这双", "这顶",
    "那个", "那款", "那件", "那条", "那双", "那顶",
    "它",
    "上面那个", "上面那款", "上面这件",
    "刚才那个", "刚才那款", "刚刚那个",
    "前面那个", "前面那款",
)

# 显式否定 / 换款信号：出现时不做指代补全（由 product_switch 接管）
_REFUSE_WORDS = (
    "不要这个", "不要这", "换一款", "换一个", "换个", "换件", "换条",
    "别的款", "其他款", "另一款", "看看别的", "不想要这",
)



def _key(session_id: str) -> str:
    return f"{_KEY_PREFIX}{session_id}"


def remember(
    session_id: str,
    model: Optional[str],
    sku: Optional[str] = None,
) -> None:
    """记住本会话最近咨询的商品（best-effort，Redis 挂了静默放行）。"""
    if not session_id or not model:
        return
    try:
        r = redis_get()
        r.setex(
            _key(session_id),
            LAST_PRODUCT_TTL_SECONDS,
            json.dumps(
                {"model": model, "sku": sku, "ts": int(time.time())},
                ensure_ascii=False,
            ),
        )
    except Exception as e:
        logger.warning(f"[ref] remember 失败（放行）: {e}")


def recall(session_id: str) -> Optional[dict]:
    """取回本会话最近咨询的商品；没有 / 过期 / 异常 → None"""
    if not session_id:
        return None
    try:
        raw = redis_get().get(_key(session_id))
        if not raw:
            return None
        obj = json.loads(raw)
        model = obj.get("model")
        if not model:
            return None
        return {"model": model, "sku": obj.get("sku")}
    except Exception as e:
        logger.warning(f"[ref] recall 失败（放行）: {e}")
        return None


def has_reference_word(query: str) -> bool:
    """句子里有没有商品指代词"""
    return any(w in query for w in _REFERENCE_WORDS)


def resolve(
    query: str,
    session_id: str,
    model: Optional[str] = None,
    sku: Optional[str] = None,
) -> Optional[dict]:
    """把「它 / 这个 / 那款」解析成具体商品。

    Args:
        query: 用户原话（只读，绝不修改）
        session_id: 会话 ID
        model: 本轮前端已带的款号（有则说明用户已明确商品，不需要解析）
        sku: 本轮前端已带的 SKU

    Returns:
        {"model": str, "sku": str|None, "reason": str} 或 None
    """
    if not query or not session_id:
        return None

    # 用户这轮已经明确指了商品（前端从详情页带过来的），不需要解析
    if model:
        return None

    # 正在表达"要走"的用户，不要把上一款拽回来
    if any(w in query for w in _REFUSE_WORDS):
        return None

    # 没指代词，就别自作主张绑定
    if not has_reference_word(query):
        return None

    # 句子里已经写了款号 / SKU（FS368998 / HKZ26946 / HM261139），
    # 说明用户自己指明了，以他写的为准，不要用会话里的商品覆盖。
    if re.search(r"[A-Z]{2,4}\d{4,}", query.upper()):
        return None

    last = recall(session_id)
    if not last:
        return None

    logger.info(
        f"[ref] 指代消解: session={session_id[:12]}... "
        f"{last.get('model')}/{last.get('sku')} <- query={query[:40]!r}"
    )
    return {
        "model": last["model"],
        "sku": sku or last.get("sku"),
        "reason": "session_last_product",
    }
