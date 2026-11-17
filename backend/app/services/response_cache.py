"""
Response Cache - 重复 query 不再调 LLM

按 CLAUDE.md §5 Scope Lock：services/ 做业务编排
被 app/api/chat.py 在 Synthesizer 之前调用

两层缓存：
  L1 Exact match：md5(query) 命中即返上次 answer（最快，0 token）
  L2 Semantic match：embedding 相似度 > 0.95 命中（保 paraphrase 也命中）

设计：
- per-user 缓存（不同用户不复用）
- TTL 10 分钟（10min 内重复 → 命中；过期 → 重算）
- 写入时机：Synthesizer.run_stream 完成后（chat.py 的 done 事件里调 cache.put）
- 读时机：chat.py 在 guard.check 之后、Synthesizer.run_stream 之前
- 降级：Redis 挂了 / embedding 失败 → 静默放行（不误伤）

存储：
- L1 key: rcache:exact:{user_id}:{scope}:{md5(query)}
- L1 value: JSON {"answer", "contexts", "scores", "create_ts"}
- L2 key: rcache:sem:{user_id}:{scope}:{md5(query)}
- L2 value: JSON {"answer", "embedding", "contexts", "scores", "create_ts"}

scope（2026-11 新增）：
    缓存必须按**商品**隔离。policy_query 的 prompt 里带【当前商品】参数与
    尺码表，同一用户 10 分钟内切到另一款问同一句，原来会命中上一款的答案，
    出现「在毛衣页面问洗护，回答夹克的聚酯纤维」。
    scope 用 SKU（不是款号）：同款不同码问「胸围多少」，按款号会答错码数。
    无商品上下文（店铺入口泛问政策）时 scope="global"。

    引用快照（contexts / scores）与答案一起存：
    缓存命中时不再重新检索。否则答案讲 A 款的成分、引用列 B 款的切片，
    前端展示的来源和答案正文互相对不上。
"""
import hashlib
import json
import logging
import time
from typing import Optional

from app.clients.redis_client import get_client as redis_get
from app.core.providers.embedding import EmbeddingError, get_embedding_provider

logger = logging.getLogger(__name__)

# TTL：10 分钟（10min 内同一问题复用）
CACHE_TTL_SECONDS = 600
# Semantic 相似度阈值（0.95 = 几乎同义）
SEMANTIC_THRESHOLD = 0.95
# 最多存的语义缓存条数（per user），防爆内存
MAX_SEMANTIC_ENTRIES_PER_USER = 50

_EXACT_KEY_PREFIX = "rcache:exact:"
_SEM_KEY_PREFIX = "rcache:sem:"
_SEM_INDEX_PREFIX = "rcache:sem_idx:"  # 维护每用户的 sem key 列表


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _scope_key(scope: Optional[str]) -> str:
    """把 scope 归一成一段安全的 Redis key 片段。

    SKU / 款号只含字母数字与横线，这里主动过滤其余字符，
    防止冒号之类的分隔符破坏 key 结构（也顺带挡掉 key 注入）。
    """
    raw = (scope or "").strip()
    if not raw:
        return "global"
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in "-_")
    return safe[:48] or "global"


def _encode(
    answer: str,
    contexts: Optional[list] = None,
    scores: Optional[list] = None,
) -> str:
    """答案 + 引用快照一起序列化。

    contexts/scores 必须跟答案同源写入——缓存命中时前端直接展示快照，
    不再重新检索，这样「答案讲的商品」和「引用列的商品」必然一致。
    """
    return json.dumps(
        {
            "answer": answer[:8000],
            "contexts": contexts or [],
            "scores": scores or [],
            "create_ts": int(time.time()),
        },
        ensure_ascii=False,
    )


def _decode(raw) -> Optional[dict]:
    """反序列化成 {"answer", "contexts", "scores"}。

    兼容旧格式：改成 JSON 之前 L1 存的是纯文本 answer，
    解析失败就当作「无引用快照的答案」处理，让存量缓存自然过期即可。
    """
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        text = str(raw)
        return {"answer": text, "contexts": [], "scores": []} if text else None
    if not isinstance(obj, dict):
        return None
    answer = obj.get("answer")
    if not isinstance(answer, str) or not answer:
        return None
    return {
        "answer": answer,
        "contexts": obj.get("contexts") or [],
        "scores": obj.get("scores") or [],
    }


# =============================================================
# L1 Exact Match
# =============================================================
def get_exact(
    query: str, user_id: int, scope: Optional[str] = None
) -> Optional[dict]:
    """Exact match：命中返 {"answer","contexts","scores"}；未命中 / 异常返 None"""
    if user_id <= 0 or not query:
        return None
    try:
        r = redis_get()
        key = f"{_EXACT_KEY_PREFIX}{user_id}:{_scope_key(scope)}:{_md5(query)}"
        return _decode(r.get(key))
    except Exception as e:
        logger.warning(f"[rcache] exact get 异常（放行）: {e}")
        return None


def put_exact(
    query: str,
    user_id: int,
    answer: str,
    scope: Optional[str] = None,
    contexts: Optional[list] = None,
    scores: Optional[list] = None,
) -> None:
    """存 exact 缓存（best-effort）"""
    if user_id <= 0 or not query or not answer:
        return
    try:
        r = redis_get()
        key = f"{_EXACT_KEY_PREFIX}{user_id}:{_scope_key(scope)}:{_md5(query)}"
        r.setex(key, CACHE_TTL_SECONDS, _encode(answer, contexts, scores))
    except Exception as e:
        logger.warning(f"[rcache] exact put 异常: {e}")


# =============================================================
# L2 Semantic Match
# =============================================================
def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return dot  # a/b 假设已 L2 normalized


def _scan_semantic_keys(user_id: int, scope: Optional[str] = None) -> list[str]:
    """扫该用户**当前 scope** 下的 sem cache keys（用 index set 维护，避免 KEYS 全扫）

    index 按 scope 分片：否则同一用户的 A 款缓存会被 B 款的语义匹配捞到，
    语义命中（sim>0.95）比 exact 更容易跨款串味。
    """
    try:
        r = redis_get()
        sk = _scope_key(scope)
        idx_key = f"{_SEM_INDEX_PREFIX}{user_id}:{sk}"
        # 返回 set 里所有 key 名字
        members = r.smembers(idx_key)
        return [f"{_SEM_KEY_PREFIX}{user_id}:{sk}:{m}" for m in members]
    except Exception as e:
        logger.warning(f"[rcache] sem scan 异常: {e}")
        return []


def get_semantic(
    query: str, user_id: int, scope: Optional[str] = None
) -> Optional[dict]:
    """Semantic match：embedding 相似度 > 阈值命中返 {"answer","contexts","scores"}"""
    if user_id <= 0 or not query:
        return None
    try:
        # 1. 算 query embedding
        try:
            q_emb = get_embedding_provider().embed_text(query)
        except EmbeddingError as e:
            logger.warning(f"[rcache] sem embed 失败（放行）: {e}")
            return None

        # 2. 扫该用户当前 scope 下的 sem cache
        keys = _scan_semantic_keys(user_id, scope)
        if not keys:
            return None

        r = redis_get()
        idx_key = f"{_SEM_INDEX_PREFIX}{user_id}:{_scope_key(scope)}"
        # 3. 算每条的 cosine，取最高
        best_sim = 0.0
        best_hit: Optional[dict] = None
        for k in keys:
            raw = r.get(k)
            if not raw:
                # 清理 index set（条目已过期）
                r.srem(idx_key, k.split(":")[-1])
                continue
            try:
                entry = json.loads(raw)
                cached_emb = entry.get("embedding")
                cached_answer = entry.get("answer")
                if not cached_emb or not cached_answer:
                    continue
                sim = _cosine(q_emb, cached_emb)
                if sim > best_sim:
                    best_sim = sim
                    best_hit = {
                        "answer": cached_answer,
                        "contexts": entry.get("contexts") or [],
                        "scores": entry.get("scores") or [],
                    }
            except (json.JSONDecodeError, TypeError):
                continue

        if best_sim >= SEMANTIC_THRESHOLD and best_hit is not None:
            logger.info(
                f"[rcache] sem 命中: user={user_id} scope={_scope_key(scope)} "
                f"sim={best_sim:.3f} query={query[:30]!r}"
            )
            best_hit["answer"] = best_hit["answer"][:8000]
            return best_hit
        return None
    except Exception as e:
        logger.warning(f"[rcache] sem get 异常（放行）: {e}")
        return None


def put_semantic(
    query: str,
    user_id: int,
    answer: str,
    scope: Optional[str] = None,
    contexts: Optional[list] = None,
    scores: Optional[list] = None,
) -> None:
    """存 semantic 缓存（算 embedding + Redis）"""
    if user_id <= 0 or not query or not answer:
        return
    try:
        try:
            q_emb = get_embedding_provider().embed_text(query)
        except EmbeddingError as e:
            logger.warning(f"[rcache] sem put embed 失败: {e}")
            return

        r = redis_get()
        md5 = _md5(query)
        sk = _scope_key(scope)
        entry_key = f"{_SEM_KEY_PREFIX}{user_id}:{sk}:{md5}"
        idx_key = f"{_SEM_INDEX_PREFIX}{user_id}:{sk}"

        # 存 entry（引用快照与答案同源落库）
        payload = json.dumps({
            "query": query[:500],
            "answer": answer[:8000],
            "embedding": q_emb,
            "contexts": contexts or [],
            "scores": scores or [],
            "create_ts": int(time.time()),
        }, ensure_ascii=False)
        pipe = r.pipeline()
        pipe.setex(entry_key, CACHE_TTL_SECONDS, payload)
        pipe.sadd(idx_key, md5)
        # index set 自己设个更长的 TTL（entry TTL 短，index 跟着 entry 走）
        pipe.expire(idx_key, CACHE_TTL_SECONDS * 2)
        pipe.execute()

        # LRU 截断（per user per scope 最多 N 条）
        size = r.scard(idx_key)
        if size > MAX_SEMANTIC_ENTRIES_PER_USER:
            # 简单策略：随机删一批（实际可用 sorted set + ts 淘汰）
            excess = int(size) - MAX_SEMANTIC_ENTRIES_PER_USER
            members = list(r.smembers(idx_key))[:excess]
            if members:
                pipe = r.pipeline()
                for m in members:
                    pipe.delete(f"{_SEM_KEY_PREFIX}{user_id}:{sk}:{m}")
                    pipe.srem(idx_key, m)
                pipe.execute()
    except Exception as e:
        logger.warning(f"[rcache] sem put 异常: {e}")


# =============================================================
# 统一接口
# =============================================================
def get_cached_answer(
    query: str, user_id: int, scope: Optional[str] = None
) -> Optional[dict]:
    """优先 exact → semantic → None

    Returns:
        {"answer": str, "contexts": list, "scores": list}

    scope 传当前商品 SKU（无商品上下文时传 None）。切商品后不会命中
    上一款的答案，因为 key 里带了 scope。
    """
    hit = get_exact(query, user_id, scope)
    if hit is not None:
        logger.info(
            f"[rcache] exact 命中: user={user_id} scope={_scope_key(scope)} "
            f"query={query[:30]!r}"
        )
        return hit
    return get_semantic(query, user_id, scope)


def put_cached_answer(
    query: str,
    user_id: int,
    answer: str,
    scope: Optional[str] = None,
    contexts: Optional[list] = None,
    scores: Optional[list] = None,
) -> None:
    """存两层（best-effort）"""
    put_exact(query, user_id, answer, scope, contexts, scores)
    put_semantic(query, user_id, answer, scope, contexts, scores)
