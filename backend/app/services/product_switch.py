"""换款意图识别与真实商品候选匹配。

用户从某个商品页进入会话后想换一款时，不反问「您要换哪一个」，
而是从在售商品切片里匹配几款真实商品做成候选卡片，点选后由前端切换绑定。
候选只来自 product_slices.json，绝不让模型凭空编商品。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_SLICES_FILE = Path(__file__).resolve().parents[2] / "config" / "product_slices.json"

# 明确的「换」语义——只收"换一件商品"的说法。
# 2026-11 修：不再收裸单字「换」。此前 "我要换货" / "这件怎么换洗" /
# "更换配件" 全部命中，用户申请售后换货却收到一排商品卡片，直接走错流程。
_SWITCH_HINTS = (
    "换一款", "换一个", "换一件", "换一条", "换件", "换条", "换个", "换款",
    "换成", "别的款", "其他款", "另一款", "另外一款",
    "看看别的", "看别的", "不想要这",
)

# 售后/洗护语义否决：命中即让位给退款流程或商品问答，不弹换款卡片。
# 与 _SWITCH_HINTS 同时命中时（"我要换货，另外看看别的款"），售后优先——
# 售后是明确诉求，换款只是顺带，先解决售后。
_AFTERSALE_WORDS = (
    "换货", "换码", "换尺码", "换大小", "换颜色", "换个颜色",
    "换色", "换个色", "换新", "换洗", "更换配件", "退换",
)

# (用户可能说的词, 目标品类, 标题关键词)
_CATEGORY_RULES = (
    (("裤", "裤子", "西裤", "弯刀裤", "长裤"), "裤装", ("裤",)),
    (("毛衣", "针织", "开衫", "套头"), "毛衣", ("毛衣", "针织", "开衫")),
    (("t恤", "T恤", "短袖", "polo", "POLO", "体恤"), "T恤", ("T恤", "POLO", "短袖")),
    (("夹克", "外套", "加绒", "上衣"), "外套", ("夹克", "外套")),
)

_ATTRIBUTE_WORDS = ("加绒", "黑色", "棕色", "咖色", "灰色", "杏色", "宽松", "大码", "短款")


def _load_specs() -> dict:
    try:
        return json.loads(_SLICES_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def match_product_choices(
    query: str, current_model: Optional[str], limit: int = 4
) -> Optional[list[dict]]:
    """命中换款语义时返回候选；不匹配返回 None（走正常检索）。"""
    if not current_model or not query:
        return None
    # 售后语义优先：「我要换货」「怎么换洗」不是换商品
    if any(word in query for word in _AFTERSALE_WORDS):
        return None
    if not any(hint in query for hint in _SWITCH_HINTS):
        return None

    specs = _load_specs()
    if not specs:
        return None

    lowered = query.lower()
    wanted_category = None
    wanted_title_words: tuple[str, ...] = ()
    for words, category, title_words in _CATEGORY_RULES:
        if any(w.lower() in lowered for w in words):
            wanted_category = category
            wanted_title_words = title_words
            break

    scored: list[tuple[int, dict]] = []
    for model, spec in specs.items():
        if model == current_model:
            continue
        title = str(spec.get("title") or "")
        category = str(spec.get("category") or "")
        score = 0
        if wanted_category:
            if category == wanted_category:
                score += 3
            if any(w in title for w in wanted_title_words):
                score += 2
        for word in _ATTRIBUTE_WORDS:
            if word in query and word in title:
                score += 1
        scored.append((score, {
            "model": model,
            "sku": f"{model}-M",
            "name": title,
            "category": category,
            "cover": f"/products/{model}-scene.webp",
        }))

    # 稳定排序：命中品类的排前面，其余按知识库原顺序兜底
    ordered = [item for _, item in sorted(scored, key=lambda pair: -pair[0])]
    if wanted_category:
        ordered = [i for i in ordered if i["category"] == wanted_category] + [
            i for i in ordered if i["category"] != wanted_category
        ]
    return ordered[:limit] or None
