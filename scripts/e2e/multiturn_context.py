"""会话上下文多轮验收（12 组场景）

为什么需要它：这轮改动之前，多轮一条用例都没有——「订单号忘了再问一遍」
这个毛病既无法证明存在，也无法证明修好。

场景分成三类：
A. 复用（该记住的记住）：跨轮复用订单号、换单覆盖、两单来回切
B. 隔离（不该串的不串）：跨用户同 session、报别人的单号、商品咨询不串单
C. 失效与边界：30min 过期、匿名、首轮不写脏、last_intent 更新、resolved 累积

断言来源有两处：
- 用户可见的答案（有没有让买家重复报单号）
- conversation_contexts 表（槽位到底写成什么）

**措辞必须错开**：guard 的 L3 层有同句重复检测，同一账号第 4 次问同一句话
会被判 spam 直接拦下（intent=blocked，不进业务链路也不写上下文）。
第一版脚本这里踩了一次，看起来像"上下文没写"，实际是被防刷拦了。

用法：
    backend/.venv/Scripts/python.exe scripts/e2e/multiturn_context.py
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar

sys.path.insert(0, "backend")

# 本脚本既要打 HTTP 又要直接读 context 表，所以得先把 backend/.env 灌进进程环境
# （从仓库根目录运行时，pydantic 的 env_file 找不到相对路径的 .env）
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join("backend", ".env"))

BASE = "http://127.0.0.1:8000"

from sqlalchemy import text  # noqa: E402

from app.clients.mysql_client import with_safe_session  # noqa: E402

RESULTS = []


# ------------------------------------------------------------------
# 基础设施
# ------------------------------------------------------------------
def make_opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def register_and_login(tag: str):
    """注册并登录一个干净账号（无订单，订单后续插入）。"""
    user = f"{tag}{int(time.time() * 1000) % 100000000}"
    pwd = "Test123456"
    opener = make_opener()
    body = json.dumps({"username": user, "password": pwd}).encode()
    req = urllib.request.Request(
        BASE + "/api/auth/register", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with opener.open(req, timeout=20):
            pass
    except Exception as e:  # noqa: BLE001
        print(f"  注册跳过（{e}）")
    data = urllib.parse.urlencode({"username": user, "password": pwd}).encode()
    req = urllib.request.Request(
        BASE + "/api/auth/login", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
    )
    with opener.open(req, timeout=20):
        pass
    return opener, user_id_of(user)


def user_id_of(username: str) -> int:
    with with_safe_session(commit=False) as db:
        uid = db.execute(
            text("SELECT id FROM users WHERE username=:u"), {"u": username}
        ).scalar()
    return int(uid)


def seed_orders(user_id: int, order_nos: list) -> None:
    with with_safe_session(commit=True) as db:
        for idx, no in enumerate(order_nos):
            db.execute(text(
                "INSERT INTO orders (order_no, user_id, status, total_amount, "
                "receiver_name, receiver_phone, create_time, deleted) "
                "VALUES (:no, :uid, 'delivered', 599.00, '测试买家', '13800000000', "
                "DATE_SUB(NOW(), INTERVAL :d DAY), 0)"
            ), {"no": no, "uid": user_id, "d": idx + 2})


def ctx_row(session_id: str):
    with with_safe_session(commit=False) as db:
        return db.execute(text(
            "SELECT c.user_id, c.last_intent, c.current_order_no, c.resolved_orders "
            "FROM conversation_contexts c JOIN conversations v ON v.id = c.conversation_id "
            "WHERE v.session_id = :s AND c.deleted = 0"
        ), {"s": session_id}).fetchone()


def age_ctx(session_id: str, minutes: int) -> None:
    with with_safe_session(commit=True) as db:
        db.execute(text(
            "UPDATE conversation_contexts c JOIN conversations v ON v.id = c.conversation_id "
            "SET c.update_time = DATE_SUB(NOW(), INTERVAL :m MINUTE) "
            "WHERE v.session_id = :s"
        ), {"m": minutes, "s": session_id})


def chat(opener, query: str, session_id: str):
    body = json.dumps({
        "query": query, "session_id": session_id, "sku": None, "order_no": None,
    }).encode()
    req = urllib.request.Request(BASE + "/api/chat", data=body, headers={
        "Content-Type": "application/json", "Accept": "text/event-stream",
        "X-Traffic-Kind": "eval",
    }, method="POST")
    answer, meta, sid = "", {}, session_id
    try:
        with opener.open(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    d = json.loads(line[5:].strip())
                except Exception:  # noqa: BLE001
                    continue
                t = d.get("type")
                if t == "token":
                    answer += d.get("text") or ""
                elif t == "meta":
                    meta = d
                elif t == "done":
                    sid = d.get("session_id") or sid
    except urllib.error.HTTPError as e:
        # 越权访问他人会话时后端直接 404（IDOR 防护），当作"被拒绝"而不是异常
        meta = {"http_error": e.code}
    return answer, meta, sid


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def no_re_ask(answer: str) -> bool:
    """答案里没有让买家重新报单号。"""
    bad = ("请提供订单号", "订单号是多少", "麻烦提供订单", "发一下订单号", "请告知订单号")
    return not any(b in answer for b in bad)


# ------------------------------------------------------------------
# 场景
# ------------------------------------------------------------------
def main() -> int:
    stamp = int(time.time()) % 100000
    # ORD + 8 位日期 + 3-6 位字母数字（与 ORDER_NO_PATTERN 对齐）
    order_a = f"ORD20261116{stamp:05d}A"
    order_b = f"ORD20261116{stamp:05d}B"
    order_other = f"ORD20261116{stamp:05d}C"

    print("== 准备账号 ==")
    opener_a, uid_a = register_and_login("ctxa")
    seed_orders(uid_a, [order_a, order_b])
    opener_b, uid_b = register_and_login("ctxb")
    seed_orders(uid_b, [order_other])
    print(f"  A(uid={uid_a}) 订单 {order_a} / {order_b}")
    print(f"  B(uid={uid_b}) 订单 {order_other}")

    # ---------- A 类：复用 ----------
    print("\n== 1. 跨轮复用订单号（退款） ==")
    sid = None
    ans1, _m1, sid = chat(opener_a, f"{order_a} 这个单到哪了", sid)
    ans2, meta2, _ = chat(opener_a, "那能退吗", sid)
    check("1.1 第二轮没让买家重复报单号", no_re_ask(ans2), ans2[:80])
    check("1.2 第二轮确实在处理该订单", "退" in ans2 and "不存在" not in ans2, ans2[:80])
    row = ctx_row(sid)
    check("1.3 上下文写入 current_order_no", bool(row) and row[2] == order_a,
          f"row={row}")

    print("\n== 2. 跨轮复用订单号（物流追问） ==")
    sid2 = None
    _a1, _m2, sid2 = chat(opener_a, f"{order_b} 发货了吗", sid2)
    ans2b, _m3, _ = chat(opener_a, "那大概几天到", sid2)
    check("2.1 物流追问不再索要单号", no_re_ask(ans2b), ans2b[:80])

    print("\n== 3. 换单覆盖 ==")
    sid3 = None
    _a3, _m4, sid3 = chat(opener_a, f"{order_a} 到哪了", sid3)
    _a5, _m5, sid3 = chat(opener_a, f"换成 {order_b}，这个到哪了", sid3)
    row3 = ctx_row(sid3)
    check("3.1 current_order_no 被新单覆盖", bool(row3) and row3[2] == order_b, f"row={row3}")
    check("3.2 旧单仍在 resolved_orders",
          bool(row3) and order_a in json.dumps(row3[3] or []), f"resolved={row3[3] if row3 else None}")

    print("\n== 4. 两单来回切 ==")
    sid4 = None
    _a4, _m6, sid4 = chat(opener_a, f"{order_a} 物流到哪了", sid4)
    _a5, _m7, sid4 = chat(opener_a, f"{order_b} 物流到哪了", sid4)
    _a6, _m8, sid4 = chat(opener_a, "回到第一个单，那个到哪了", sid4)
    row4 = ctx_row(sid4)
    resolved = json.dumps(row4[3] or []) if row4 else ""
    check("4.1 两个单都在 resolved_orders",
          order_a in resolved and order_b in resolved, resolved)

    # ---------- B 类：隔离 ----------
    print("\n== 5. 跨用户同 session ==")
    ans5, meta5, _ = chat(opener_b, "那能退吗", sid)
    # 会话归属校验会把越权请求直接挡在门口（404）；放行的情况下也不能带出 A 的单号
    blocked = meta5.get("http_error") == 404
    check("5.1 B 用户没拿到 A 会话的上下文",
          blocked or order_a not in ans5,
          f"blocked={blocked} ans={ans5[:60]}")
    check("5.2 B 的答案里不出现 A 的单号", order_a not in ans5, ans5[:80])

    print("\n== 6. B 报 A 的单号 ==")
    sid6 = None
    ans6, _m10, sid6 = chat(opener_b, f"{order_a} 能退吗", sid6)
    check("6.1 越权单号被拒", "不存在" in ans6 or "不属于" in ans6, ans6[:80])
    row6 = ctx_row(sid6)
    check("6.2 别人的单号没写进上下文",
          not row6 or row6[2] != order_a, f"row={row6}")

    print("\n== 7. 商品咨询不串单 ==")
    sid7 = None
    _a7, _m11, sid7 = chat(opener_a, f"{order_a} 现在什么状态", sid7)
    ans7, _m12, _ = chat(opener_a, "这件加绒夹克保暖吗", sid7)
    check("7.1 商品咨询答案不夹带订单状态",
          "已签收" not in ans7 and order_a not in ans7, ans7[:80])

    # ---------- C 类：失效与边界 ----------
    print("\n== 8. 上下文过期 ==")
    age_ctx(sid3, 40)
    from app.services.context.context_service import get_context_service
    ctx = get_context_service().load(sid3, uid_a)
    check("8.1 空闲 40min 后服务层不再返回旧单", ctx.current_order_no is None,
          f"got={ctx.current_order_no}")

    print("\n== 9. 匿名用户 ==")
    anon = make_opener()
    ans9, _m13, _ = chat(anon, "我的订单到哪了", None)
    check("9.1 匿名仍走引导登录", "登录" in ans9, ans9[:80])

    print("\n== 10. 首轮不写脏数据 ==")
    sid10 = None
    _a10, _m14, sid10 = chat(opener_a, "你好", sid10)
    row10 = ctx_row(sid10)
    check("10.1 无订单号的轮次 current_order_no 为空",
          row10 is not None and row10[2] is None, f"row={row10}")

    print("\n== 11. last_intent 每轮更新 ==")
    sid11 = None
    _a11, m15, sid11 = chat(opener_a, f"帮我查 {order_a} 的物流", sid11)
    row11a = ctx_row(sid11)
    _a12, m16, sid11 = chat(opener_a, "退货运费谁承担", sid11)
    row11b = ctx_row(sid11)
    check("11.1 意图随轮次变化",
          bool(row11a) and bool(row11b) and row11a[1] != row11b[1],
          f"t1_intent={m15.get('intent')} t1_method={m15.get('intent_method')} "
          f"row1={row11a} row2={row11b}")

    print("\n== 12. 上下文行不重复累积 ==")
    with with_safe_session(commit=False) as db:
        cnt = db.execute(
            text("SELECT COUNT(*) FROM conversation_contexts c "
                 "JOIN conversations v ON v.id = c.conversation_id WHERE v.session_id = :s"),
            {"s": sid4},
        ).scalar()
    check("12.1 同一会话只有一行上下文", int(cnt) == 1, f"count={cnt}")

    # ---------- 汇总 ----------
    total = len(RESULTS)
    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    print(f"\n===== {passed}/{total} 通过 =====")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"FAIL: {name} — {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
