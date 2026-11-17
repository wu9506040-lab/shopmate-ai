# -*- coding: utf-8 -*-
"""生成商城前端自包含的商品参数 / 尺码表数据与款级图集。

数据源为店铺商品页转录结果；产出写入 frontend/src，随源码一起进仓库，
云端部署不依赖 data/shop_raw。运行：python scripts/build_catalog_specs.py
"""
from __future__ import annotations

import json
import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parent.parent
CATALOG = ROOT / "data" / "shop_raw" / "shop_catalog.json"
PUB = ROOT / "frontend" / "public" / "products"
OUT = ROOT / "frontend" / "src" / "data" / "catalogSpecs.ts"

# 与 deploy/mysql/init/04_products_seed.sql 的品类口径保持一致
META = {
    "982672368998": ("外套", "M-3XL"),
    "978285881698": ("毛衣", "M-3XL"),
    "1089069302643": ("外套", "M-5XL"),
    "999496911653": ("裤装", "M-5XL"),
    "1022023947855": ("T恤", "M-5XL"),
    "1083744906804": ("毛衣", "M-5XL"),
    "1090908072708": ("裤装", "M-5XL"),
    "992876185196": ("毛衣", "M-3XL"),
    "1076110041235": ("T恤", "M-5XL"),
}

# 商品页参数区出现的字段名（顺序无关，用于判定「标签 / 值」两种排布）
LABELS = {
    "参数信息", "面料", "适用季节", "版型", "品牌", "适用对象", "材质成分",
    "适用性别", "货号", "上市年份季节", "袖长规格", "里料", "领口设计",
    "衣长", "尺码", "颜色", "功能", "领型设计", "款式细节", "穿着方式",
    "上市时间", "适用人群", "适用场景", "袖长类型", "吊牌价", "产地",
    "厚薄", "裤型", "服装版型", "风格", "是否商场同款", "腰型", "裤长",
    "弹力", "图案", "面料成分", "衣门襟", "袖型", "下摆设计", "填充物",
    "含绒量", "克重", "织法", "领型", "季节", "基础风格", "细分风格",
    "版型分类", "克重范围", "袖长", "款式", "工艺", "上市年份", "适用人群",
}

# 展示顺序：靠前的先出现在详细参数表
ORDER = [
    "品牌", "货号", "材质成分", "面料", "里料", "版型", "服装版型", "风格",
    "裤型", "厚薄", "功能", "领口设计", "领型设计", "袖长规格", "袖长类型",
    "衣长", "穿着方式", "款式细节", "适用季节", "上市年份季节", "上市时间",
    "适用人群", "适用性别", "适用场景", "产地", "吊牌价", "尺码", "颜色",
]

# 关键属性卡片：优先取到的三个
HIGHLIGHT_KEYS = ["面料", "材质成分", "版型", "服装版型", "适用季节", "风格", "裤型"]

CATEGORY_NOTE = {
    "外套": "上衣类按「胸围 + 衣长」为主口径，肩宽 / 袖长为辅助；本店主线为宽松版型。",
    "衬衫": "上衣类按「胸围 + 衣长」为主口径，肩宽 / 袖长为辅助；本店主线为宽松版型。",
    "T恤": "上衣类按「胸围 + 衣长」为主口径，肩宽 / 袖长为辅助；本店主线为宽松版型。",
    "毛衣": "针织类请注意表头口径：开衫按「全围」标注，套头按「平铺单面宽」标注，全围 = 单面宽 × 2。",
    "裤装": "裤装类以「腰围 + 臀围」为主口径，裤长 / 腿围为辅助；腰围取肚脐上方两指处水平围量。",
}


def parse_params(text: str) -> dict[str, str]:
    """解析商品页参数文本。

    淘宝参数区是两段混排：
    - 「品牌」之前 = 顶部属性卡片，顺序为「值 标签」（值在前）；
    - 「品牌」起 = 下方参数列表，顺序为「标签 值」，且成分等值可能跨多个空格分词。
   早期按 token 双向猜测会在跨词值处错位（曾把「货号」解析成成分），
    因此这里按分界点分段解析，值允许跨 token 直到遇到下一个标签。
    """
    tokens = [t for t in text.split() if t and t != "参数信息"]
    out: dict[str, str] = {}

    def put(label: str, value: str) -> None:
        if not value:
            return
        if label in out and out[label] != value:
            out[label] = out[label] + "," + value  # 同标签重复（如两段颜色）合并
        else:
            out[label] = value

    try:
        split_at = tokens.index("品牌")
    except ValueError:
        split_at = len(tokens)
    head, tail = tokens[:split_at], tokens[split_at:]

    # 卡片区：值 标签 成对
    i = 0
    while i + 1 < len(head):
        val, label = head[i], head[i + 1]
        if label in LABELS and val not in LABELS:
            put(label, val)
            i += 2
        else:
            i += 1

    # 列表区：标签 值（值可跨 token）
    i = 0
    while i < len(tail):
        label = tail[i]
        if label not in LABELS:
            i += 1
            continue
        j = i + 1
        while j < len(tail) and tail[j] not in LABELS:
            j += 1
        put(label, " ".join(tail[i + 1:j]))
        i = j
    return out


def model_of(item_id: str) -> str:
    return "FS" + item_id[-6:]


def norm_size_table(raw: dict | None) -> dict | None:
    """只保留展示需要的列，多行备注合并为一条。"""
    if not raw:
        return None
    cols = [str(c) for c in (raw.get("cols") or [])]
    rows = []
    for r in raw.get("rows") or []:
        item: dict[str, object] = {"size": r.get("size", "")}
        for col in cols[1:]:
            if col in r:
                item[col] = r[col]
        rows.append(item)
    note = "；".join(
        str(raw[k]) for k in ("note", "note2", "note3") if raw.get(k)
    )
    out = {"unit": raw.get("unit", "CM"), "cols": cols, "rows": rows}
    if note:
        out["note"] = note
    return out


def norm_fit_report(raw: list | None) -> list | None:
    if not raw:
        return None
    return [
        {
            "height": r.get("身高"),
            "weight": r.get("体重"),
            "size": r.get("尺码"),
            "note": r.get("适应") or r.get("建议") or "",
        }
        for r in raw
    ]


def build_specs() -> dict[str, dict]:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    specs: dict[str, dict] = {}
    for item in catalog["products"]:
        item_id = str(item["id"])
        if item_id not in META:
            continue
        category, size_range = META[item_id]
        model = model_of(item_id)
        params = parse_params(item.get("params_text") or "")

        ordered = [k for k in ORDER if k in params]
        ordered += [k for k in params if k not in ORDER]

        highlights = []
        for k in HIGHLIGHT_KEYS:
            if k in params and len(highlights) < 3:
                highlights.append({"label": k, "value": params[k]})

        tables = item.get("tables_transcribed") or {}
        size_table = norm_size_table(tables.get("size_table"))
        fit_report = norm_fit_report(tables.get("fit_report"))

        specs[model] = {
            "itemId": item_id,
            "model": model,
            "title": item.get("title", ""),
            "category": category,
            "sizeRange": size_range,
            "highlights": highlights,
            "params": [{"label": k, "value": params[k]} for k in ordered],
            "sizeTable": size_table,
            "fitReport": fit_report,
            "sizeNote": CATEGORY_NOTE.get(category, "表值为平铺手工测量，1-3CM 误差属正常范围。"),
        }
    return specs


def build_model_gallery() -> int:
    """款级图集：同款所有尺码共用一套五张主图，切尺码不再换图。"""
    made = 0
    for item_id in META:
        model = model_of(item_id)
        srcs = [PUB / f"{model}-{s}.webp" for s in ("M", "L", "XL", "2XL", "3XL")]
        if not all(p.is_file() for p in srcs):
            print("  跳过图集（源图缺失）:", model)
            continue
        for idx, src in enumerate(srcs, 1):
            dst = PUB / f"{model}-{idx}.webp"
            if dst.resolve() != src.resolve():
                shutil.copy2(src, dst)
            made += 1
        shutil.copy2(srcs[0], PUB / f"{model}-scene.webp")
    return made


SLICE_DIR = ROOT / "docs" / "fashion_kb"
BACKEND_SLICES = ROOT / "backend" / "config" / "product_slices.json"

# 切片文档的可检索前缀：检索层据此识别「商品切片」并按款隔离
SLICE_DOC_PREFIX = "product_"


def _cell(v: object) -> str:
    return str(v if v is not None else "—").replace("|", "／").replace("\n", " ")


def render_slice_md(s: dict) -> str:
    """一份商品 = 一个知识库切片：参数 + 尺码表 + 试穿 + 选码口径。"""
    model = s["model"]
    item_no = next((p["value"] for p in s["params"] if p["label"] == "货号"), "")
    tags = ", ".join([s["category"], model, item_no, "商品参数", "尺码表", "2026AW"])

    param_rows = "\n".join(
        f"| {_cell(p['label'])} | {_cell(p['value'])} |" for p in s["params"]
    )

    size_lines = ""
    st = s["sizeTable"]
    if st:
        cols = st["cols"]
        head = "| " + " | ".join(_cell(c) for c in cols) + " |"
        sep = "| " + " | ".join("---" for _ in cols) + " |"
        body = "\n".join(
            "| " + " | ".join(
                _cell(row.get("size")) if i == 0 else _cell(row.get(c))
                for i, c in enumerate(cols)
            ) + " |"
            for row in st["rows"]
        )
        size_lines = (
            f"\n## 尺码表（{model}｜单位 {_cell(st['unit'])}）\n\n"
            f"{head}\n{sep}\n{body}\n\n"
            f"{_cell(st.get('note') or '表值为平铺手工测量，1-3CM 误差属正常范围。')}\n"
        )

    fit_lines = ""
    if s["fitReport"]:
        rows = "\n".join(
            f"| {_cell(f['height'])} | {_cell(f['weight'])} | {_cell(f['size'])} | {_cell(f['note'])} |"
            for f in s["fitReport"]
        )
        fit_lines = (
            "\n## 试穿参考\n\n"
            "| 身高(cm) | 体重(kg) | 试穿尺码 | 适配范围 |\n"
            "|---|---|---|---|\n" + rows + "\n"
        )

    return f"""---
title: {_cell(s["title"])}（{model}）· 商品参数与尺码表
doc_key: {SLICE_DOC_PREFIX}{model}
revision: '2026AW'
doc_type: product_spec
category: 商品切片
tags: [{tags}]
validity: permanent
effective_from: 2026-09-15
issuer: 猫与小鼠客服中心
version: 2026AW
---

# {_cell(s["title"])}（{model}）

> 本页为该款商品参数与尺码数据的唯一来源；商城商品页与 AI 客服均以本页数值为准。
> 款号 {model}｜商品编号 {s["itemId"]}｜货号 {_cell(item_no)}｜品类 {s["category"]}｜尺码档位 {s["sizeRange"]}｜系列 2026AW

## 商品参数

| 参数 | 值 |
|---|---|
{param_rows}
{size_lines}{fit_lines}
## 选码说明

{_cell(s["sizeNote"])}
胸围沿腋下最丰满处水平围量一周；腰围取肚脐上方两指处水平围量；衣长自后领中点量至下摆；裤长自腰头上沿量至裤脚。
宽松版型按推荐表选码即可，喜修身可小一码；加绒款建议大一码；身高体重跨界时以体重维度优先。
"""


def write_slices(specs: dict[str, dict]) -> int:
    """知识库按商品切片 + 后端结构化数据（与前端 TS 同源）。"""
    BACKEND_SLICES.parent.mkdir(parents=True, exist_ok=True)
    BACKEND_SLICES.write_text(
        json.dumps(specs, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for model, s in specs.items():
        (SLICE_DIR / f"{SLICE_DOC_PREFIX}{model}.md").write_text(
            render_slice_md(s), encoding="utf-8"
        )
    return len(specs)


def main() -> None:
    specs = build_specs()
    print("款级参数记录 =", len(specs))
    for model, s in specs.items():
        print("  %-12s %-4s 参数%2d项  尺码表%s  试穿%s" % (
            model, s["category"], len(s["params"]),
            "有" if s["sizeTable"] else "无",
            "有" if s["fitReport"] else "无",
        ))

    made = build_model_gallery()
    print("款级图集文件 =", made, "->", PUB)

    n_slices = write_slices(specs)
    print("商品切片 =", n_slices, "->", SLICE_DIR, "；后端结构化数据 ->", BACKEND_SLICES)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(specs, ensure_ascii=False, indent=2)
    ts = """// 商品参数与尺码表（源自店铺商品页，随源码自包含）
// 生成脚本：scripts/build_catalog_specs.py —— 请勿手工编辑本文件。

export interface SpecParam {
  label: string;
  value: string;
}

export interface SizeTable {
  unit: string;
  cols: string[];
  rows: Array<Record<string, string | number>>;
  note?: string;
}

export interface FitReportRow {
  height: number;
  weight: number;
  size: string;
  note: string;
}

export interface CatalogSpec {
  itemId: string;
  model: string;
  title: string;
  category: string;
  sizeRange: string;
  highlights: SpecParam[];
  params: SpecParam[];
  sizeTable: SizeTable | null;
  fitReport: FitReportRow[] | null;
  sizeNote: string;
}

export const CATALOG_SPECS: Record<string, CatalogSpec> = %s;

export function specForModel(model?: string | null): CatalogSpec | null {
  if (!model) return null;
  return CATALOG_SPECS[model] || null;
}
""" % body
    OUT.write_text(ts, encoding="utf-8")
    print("已生成 ->", OUT)


if __name__ == "__main__":
    main()
