"""质检抽样 —— 分桶 + 稳定哈希。

为什么不均匀随机抽：
真实流量里「在吗/你好」占大头，均匀抽 10% 抽到的基本都是问候，真正会出错的
长答案（政策/尺码/价格）反而抽不到。按「意图 × 是否转人工 × 答案长度」分桶，
每桶至少抽 1 条，长答案桶天然多抽——这才是「花同样的钱看到更多问题」。

为什么用哈希而不是 random：同一批数据重跑要抽到同一批（可复现），
否则今天说合格率 82%、明天同一份数据说 79%，没人信这个数。
"""
import hashlib
from typing import Dict, List, Optional, Sequence

SHORT_MAX = 80
MID_MAX = 300


def bucket_key(intent: Optional[str], answer_len: int, handoff: bool = False) -> str:
    if answer_len < SHORT_MAX:
        size = "short"
    elif answer_len < MID_MAX:
        size = "mid"
    else:
        size = "long"
    return f"{intent or 'unknown'}|{'handoff' if handoff else 'ai'}|{size}"


def _h(key: str) -> int:
    return int(hashlib.md5(key.encode("utf-8")).hexdigest()[:8], 16)


def select(
    rows: Sequence[Dict],
    *,
    ratio: float = 0.1,
    min_per_bucket: int = 1,
    seed: str = "",
    max_total: Optional[int] = None,
) -> List[int]:
    """rows 需含 id / bucket 两个键。返回被抽中的 id 列表（稳定、可复现）。"""
    if not rows:
        return []
    pct = max(0, min(100, round(ratio * 100)))
    groups: Dict[str, List[Dict]] = {}
    for r in rows:
        groups.setdefault(r["bucket"], []).append(r)

    picked: List[Dict] = []
    for items in groups.values():
        chosen = [r for r in items if _h(f"{seed}{r['id']}") % 100 < pct] if pct else []
        if len(chosen) < min_per_bucket:
            # 小桶兜底：按哈希顺序补足，保证冷门意图也能被看到
            for r in sorted(items, key=lambda x: _h(f"{seed}{x['id']}")):
                if r not in chosen:
                    chosen.append(r)
                if len(chosen) >= min_per_bucket:
                    break
        picked.extend(chosen)

    if max_total is not None and len(picked) > max_total:
        picked = sorted(picked, key=lambda x: _h(f"{seed}{x['id']}"))[:max_total]
    return [int(r["id"]) for r in picked]
