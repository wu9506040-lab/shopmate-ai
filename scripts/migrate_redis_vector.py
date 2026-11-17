"""
Milvus → Redis 8 向量库迁移 + 双后端对拍。

用法（backend venv）：
    PYTHONPATH=backend backend/.venv/Scripts/python.exe scripts/migrate_redis_vector.py --migrate
    PYTHONPATH=backend backend/.venv/Scripts/python.exe scripts/migrate_redis_vector.py --compare --sample 30

--migrate: Milvus scroll 全量（含向量）→ Redis upsert → FT.INFO 对账
--compare: 抽 N 条 chunk 正文当 query，两后端各搜 top-10，
           对比 ID 重合率（放行线 ≥95%）与 score 差（<0.01）
"""
import argparse
import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8")

from app.core.config import settings
from app.core.providers.embedding import get_embedding_provider
from app.clients.vector_db import (
    COLLECTION_NAME, VECTOR_SIZE,
    _milvus_search, _redis_search, _redis_upsert, _redis,
    _redis_index_name, ensure_collection, get_client,
)

TOP_K = 10
PASS_OVERLAP = 0.95
PASS_SCORE_DIFF = 0.01


def cmd_migrate():
    """全量迁移：Milvus → Redis。"""
    client = get_client()
    print(f"[1/3] 从 Milvus 全量拉取 (含向量)...")
    rows = client._raw.query(
        collection_name=COLLECTION_NAME,
        filter="",
        output_fields=["*", "vector"],
        limit=10000,
    )
    print(f"    Milvus 共 {len(rows)} 条")
    if not rows:
        print("!! Milvus 为空，终止")
        return

    print(f"[2/3] 写入 Redis (prefix={settings.VECTOR_REDIS_PREFIX})...")
    settings.VECTOR_BACKEND = "redis"
    # get_client() 已在上面被 milvus 后端实例化并缓存为单例，必须重置
    # 否则 ensure_collection 仍走 Milvus，Redis 索引根本不会创建
    import app.clients.vector_db as _vdb
    _vdb._client = None
    ensure_collection(COLLECTION_NAME, VECTOR_SIZE)
    points = []
    for r in rows:
        vec = r.get("vector")
        if vec is None:
            print(f"    !! {r['id']} 无向量，跳过")
            continue
        payload = {k: v for k, v in r.items() if k not in ("id", "vector")}
        points.append({"id": str(r["id"]), "vector": list(vec), "payload": payload})
    n = _redis_upsert(points, COLLECTION_NAME)
    print(f"    写入 {n} 条")

    import time
    from app.clients.vector_db import _ft_search_raw
    # FT.INFO 的 num_docs 也是异步更新的（实测滞后），用 SEARCH '*' total 对账
    for attempt in range(10):
        total, _ = _ft_search_raw(_redis_index_name(COLLECTION_NAME), "*",
                                  params=None, limit=0, offset=0)
        if total >= len(rows):
            break
        time.sleep(0.3)
    print(f"[3/3] 对账: Milvus={len(rows)} Redis索引={total} → "
          f"{'✅ 一致' if total >= len(rows) else '❌ 不一致'}")


def cmd_compare(sample_n: int):
    """双后端对拍。"""
    client = get_client()
    rows = client._raw.query(
        collection_name=COLLECTION_NAME,
        filter="",
        output_fields=["*", "vector"],
        limit=10000,
    )
    rows = [r for r in rows if r.get("vector")]
    print(f"Milvus 可用向量条数: {len(rows)}")

    random.seed(42)
    samples = random.sample(rows, min(sample_n, len(rows)))
    provider = get_embedding_provider()

    total = overlap_pass = score_pass = 0
    diffs_all = []
    print(f"\n对拍 {len(samples)} 条（query=chunk 正文前 60 字，top-{TOP_K}）")
    print("-" * 70)
    for r in samples:
        query_text = (r.get("text") or "")[:60]
        if not query_text.strip():
            continue
        qvec = provider.embed_text(query_text)
        mil = _milvus_search(qvec, TOP_K, COLLECTION_NAME, None, None)
        settings.VECTOR_BACKEND = "redis"
        red = _redis_search(qvec, TOP_K, COLLECTION_NAME, None, None)
        settings.VECTOR_BACKEND = "milvus"

        mil_ids = {h["id"] for h in mil}
        red_ids = {h["id"] for h in red}
        overlap = len(mil_ids & red_ids) / TOP_K

        # score 差：只对两边共有的 id 比（Milvus sim vs Redis sim）
        mil_map = {h["id"]: h["score"] for h in mil}
        red_map = {h["id"]: h["score"] for h in red}
        common = mil_ids & red_ids
        max_diff = max((abs(mil_map[i] - red_map[i]) for i in common), default=0)

        total += 1
        if overlap >= PASS_OVERLAP:
            overlap_pass += 1
        if max_diff < PASS_SCORE_DIFF:
            score_pass += 1
        diffs_all.append(max_diff)
        mark = "✅" if (overlap >= PASS_OVERLAP and max_diff < PASS_SCORE_DIFF) else "❌"
        print(f"{mark} [{r['id'][:20]:<20}] 重合 {len(mil_ids & red_ids)}/{TOP_K} "
              f"maxscore差={max_diff:.4f}")

    if total == 0:
        print("!! 无可比样本")
        return
    print("-" * 70)
    rate = overlap_pass / total
    srate = score_pass / total
    avg_diff = sum(diffs_all) / len(diffs_all)
    print(f"重合率达标: {overlap_pass}/{total} = {rate:.0%}（放行线 {PASS_OVERLAP:.0%}）")
    print(f"score差达标: {score_pass}/{total} = {srate:.0%}（放行线差 <{PASS_SCORE_DIFF}）")
    print(f"平均 max score 差: {avg_diff:.4f}")
    if rate >= PASS_OVERLAP and avg_diff < PASS_SCORE_DIFF:
        print("\n✅ 对拍通过，可以灰度切换 VECTOR_BACKEND=redis")
    else:
        print("\n❌ 对拍未达标，排查索引参数后再试")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--migrate", action="store_true")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--sample", type=int, default=30)
    args = ap.parse_args()
    if args.migrate:
        cmd_migrate()
    if args.compare:
        cmd_compare(args.sample)
    if not args.migrate and not args.compare:
        print(__doc__)


if __name__ == "__main__":
    main()
