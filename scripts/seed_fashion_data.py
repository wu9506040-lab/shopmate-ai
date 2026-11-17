# -*- coding: utf-8 -*-
"""服饰域商品数据重灌（复刻店铺：猫与小鼠 2026AW 在售 9 款）。

数据源：data/shop_raw/harvest_open_tabs.json（真实标题/价格/主图）
动作：① 历史商品软删（deleted=1，历史订单快照不受影响）
      ② 按真实商品 × 尺码档位生成 SKU（status=1 上架）
幂等：重复跑先清本脚本生成的 FS% SKU 再插入。
"""
import json, os, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import pymysql

BASE = r"E:\智能客服\data\shop_raw"
rows = json.load(open(os.path.join(BASE, "harvest_open_tabs.json"), encoding="utf-8"))

# 品类 / 版型 / 成分 / 尺码档（与 docs/fashion_kb/fashion_product_specs_v2026aw.md 一致）
META = {
    "982672368998": ("外套", "M-3XL", "美式休闲短款夹克", "聚酯纤维"),
    "764266902404": ("衬衫", "M-5XL", "复古格子衬衫（前后衣长）", "棉涤混纺"),
    "978285881698": ("毛衣", "M-3XL", "慵懒风双拉链开衫", "针织"),
    "1089069302643": ("外套", "M-5XL", "加绒保暖夹克 HM261139", "PU 涂层 + 聚酯"),
    "999496911653": ("裤装", "M-5XL", "直筒垂感休闲西裤", "涤纶 95% 氨纶 5%"),
    "1022023947855": ("T恤", "M-5XL", "华棉 260g 短袖 T 恤", "260g 华棉"),
    "1083744906804": ("毛衣", "M-5XL", "亨利领针织衫 MY154", "聚酯91.8 锦纶3.7 绵羊毛2.9 氨纶1.6"),
    "1090908072708": ("裤装", "M-5XL", "加绒弯刀裤 HKZ26946", "面层聚酯 底层聚酯+氨纶"),
    "992876185196": ("毛衣", "M-3XL", "假两件毛衣 MS093", "100% 聚酯纤维"),
    "1076110041235": ("T恤", "M-5XL", "大码仿麻长袖 POLO", "仿麻纹理聚酯"),
}
SIZES = ["M", "L", "XL", "2XL", "3XL", "4XL", "5XL"]

con = pymysql.connect(
    host=os.getenv("SEED_DB_HOST", "127.0.0.1"),
    port=int(os.getenv("SEED_DB_PORT", "3307")),
    user=os.getenv("SEED_DB_USER", "cs_user"),
    password=os.getenv("SEED_DB_PASSWORD", ""),
    database=os.getenv("SEED_DB_NAME", "customer_service"),
    autocommit=True,
)
cur = con.cursor()

# ① 软删所有历史商品
cur.execute("UPDATE products SET deleted=1 WHERE deleted=0")
print("软删旧商品:", cur.rowcount, "行")

# ② 幂等：清掉本脚本此前生成的 SKU
cur.execute("DELETE FROM products WHERE sku LIKE 'FS%'")
print("清理旧 FS SKU:", cur.rowcount, "行")

n = 0
for r in rows:
    pid = r.get("id") or ""
    if not pid.isdigit() or pid not in META:
        continue
    cat, size_range, desc, fabric = META[pid]
    sizes = SIZES[:5] if size_range.endswith("3XL") else SIZES
    price = float(str(r.get("price") or "0").replace("￥", "").replace("¥", "") or 0)
    cover = (r.get("main_images") or [None])[0]
    title = r["title"].strip()
    for size in sizes:
        sku = f"FS{pid[-6:]}-{size}"
        attrs = {
            "model": f"FS{pid[-6:]}",
            "category": cat,
            "spec_label": size,
            "cover_url": cover,
            "fabric": fabric,
            "size_range": size_range,
            "version": "2026AW",
            "item_id": pid,
        }
        cur.execute(
            "INSERT INTO products (sku, name, description, price, attributes, review_text, stock, status, deleted) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0)",
            (sku, title, f"{desc}｜{cat}｜{size_range}", price,
             json.dumps(attrs, ensure_ascii=False), "", 100, 1),
        )
        n += 1
print("插入服饰 SKU:", n, "行")
cur.execute("SELECT COUNT(*) FROM products WHERE deleted=0 AND status=1")
print("上架商品总数:", cur.fetchone()[0])
cur.execute("SELECT sku, name, price FROM products WHERE deleted=0 ORDER BY id LIMIT 3")
for x in cur.fetchall():
    print("  ", x)
con.close()
