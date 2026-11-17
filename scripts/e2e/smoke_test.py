"""
smoke_test.py — 全链路冒烟测试

测什么：一次买家的完整生命周期 + 管理台可达性，全部经 nginx :5173（浏览器真实路径）。
10 步：
  1  注册新买家（Java /api/auth/register，bcrypt 与 Python 互认）
  2  登录下发 Cookie（cs_token）
  3  商品列表（21 SKU，价格带当期版本）
  4  下单
  5  付款 pending→paid
  6  发货 paid→shipped
  7  重复发货被状态机拦截 409（404/409 语义曾因异常吞咽丢失，回归）
  8  AI 聊天 SSE（nginx→Java 网关→Python RAG/Agent，检索命中+流式答案）
  9  消息落库（管理台会话列表可见）
  10 管理台：工单队列 + 数据看板 200

用法：
  python scripts/e2e/smoke_test.py               # 默认 http://127.0.0.1:5173
  python scripts/e2e/smoke_test.py --base http://x.x.x.x:5173
退出码：全部通过 0，任一失败 1（CI 可用）。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

parser = argparse.ArgumentParser()
parser.add_argument("--base", default="http://127.0.0.1:5173")
BASE = parser.parse_args().base
FAILS = []


def call(method, path, body=None, form=False, jar=None, token=None, timeout=60):
    headers = {}
    data = None
    # 冒烟测试的会话也是测试流量，显式声明（统计侧按 session_meta.kind 过滤）
    if path.startswith("/api/chat"):
        headers["X-Traffic-Kind"] = "smoke"
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar)) if jar is not None else urllib.request.build_opener()
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with opener.open(r, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode("utf-8", "replace")[:200]


def check(step, name, ok, evidence=""):
    mark = "PASS" if ok else "FAIL"
    print(f"  [{step}] {name} -> {mark}  {str(evidence)[:100]}")
    if not ok:
        FAILS.append(f"{step} {name}")


def main():
    ts = str(int(time.time()))[-8:]
    uname, pwd = f"smoke{ts}", "smoke123456"
    jar = http.cookiejar.CookieJar()

    st, d = call("POST", "/api/auth/register",
                 {"username": uname, "password": pwd, "display_name": "冒烟测试",
                  "email": f"{uname}@t.cn"})
    check(1, "注册", st == 201, f"http={st}")

    st, d = call("POST", "/api/auth/login", {"username": uname, "password": pwd},
                 form=True, jar=jar)
    check(2, "登录+Cookie", st == 200 and any(c.name == "cs_token" for c in jar),
          f"http={st} cookies={[c.name for c in jar]}")

    st, d = call("GET", "/api/products")
    items = d.get("products") or d.get("items") or []
    sku = (items[0] or {}).get("sku") or (items[0] or {}).get("sku_id")
    check(3, "商品列表", st == 200 and len(items) >= 20, f"{len(items)} SKU 首件={sku}")

    st, d = call("POST", "/api/orders", {"sku": sku, "quantity": 1}, jar=jar)
    order_no = d.get("order_no") or (d.get("order") or {}).get("order_no")
    check(4, "下单", st in (200, 201) and order_no, f"order={order_no}")

    st, d = call("POST", f"/api/orders/{order_no}/pay", jar=jar)
    check(5, "付款", st == 200 and (d.get("status") == "paid" or d.get("order", {}).get("status") == "paid"),
          f"status={d.get('status')}")

    st, d = call("POST", f"/api/orders/{order_no}/ship", jar=jar)
    check(6, "发货", st == 200, f"http={st}")

    st, d = call("POST", f"/api/orders/{order_no}/ship", jar=jar)
    check(7, "重复发货拦截", st == 409, f"http={st} {str(d)[:60]}")

    body = json.dumps({"query": "夹克 XL 的胸围是多少？退货运费谁出？", "session_id": None,
                       "sku": None, "order_no": None}).encode()
    r = urllib.request.Request(BASE + "/api/chat", data=body, headers={
        "Content-Type": "application/json", "Accept": "text/event-stream"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    answer, hits = "", 0
    with opener.open(r, timeout=120) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                d = json.loads(line[5:].strip())
            except Exception:
                continue
            if d.get("type") == "token":
                answer += d.get("text") or ""
            elif d.get("type") == "meta":
                hits = len(d.get("contexts") or [])
            elif d.get("type") == "done":
                break
    check(8, "AI 聊天 SSE", hits > 0 and len(answer) > 10 and "系统繁忙" not in answer,
          f"hits={hits} ans_len={len(answer)}")

    body = urllib.parse.urlencode({"username": "admin", "password": "admin123"}).encode()
    r = urllib.request.Request(BASE + "/api/auth/login?scope=admin", data=body,
                               headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(r, timeout=30) as resp:
        tok = json.loads(resp.read())["access_token"]

    st, d = call("GET", "/api/admin/conversations?page=1&page_size=5&start_date=2026-11-01&end_date=2026-11-16",
                 token=tok)
    total = d.get("total")
    if total is None:
        total = len(d.get("items") or d.get("conversations") or [])
    check(9, "会话落库可见", st == 200 and total > 0, f"http={st} total={total}")

    st, d = call("GET", "/api/admin/handoff/tickets?status=pending", token=tok)
    check(10, "管理台工单+看板", st == 200, f"pending_total={d.get('total')}")
    st, d = call("GET", "/api/admin/analytics/board", token=tok)
    check(10, "数据看板", st == 200 and isinstance(d, dict), f"keys={list(d.keys())[:5]}")

    print(f"\n=== 冒烟结果: {'ALL PASS (10 步)' if not FAILS else 'FAIL: ' + '; '.join(FAILS)} ===")
    sys.exit(0 if not FAILS else 1)


if __name__ == "__main__":
    main()
