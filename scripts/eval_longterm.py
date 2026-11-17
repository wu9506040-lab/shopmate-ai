"""L3 跨会话长期记忆评测（20 组，数据在 data/evalsets/evalset_longterm.json）

为什么需要合成回访：线上 475 个业务会话里真实跨会话回访是 0（高频用户全是
1-2 分钟内批量建会话的脚本）。没有回访数据，L3 的对错只能靠合成用例证明机制。

判据同时看三处：
- meta.l3_hits：本轮实际注入了哪些长期 key
- GET /api/me/profile：库里到底存了什么（含 prev / hits）
- 答案文本：用户可见的行为

**本脚本跑 biz 流量**，会短暂进入看板；结束时会删除本次造的所有会话与用户数据。

用法：
    backend/.venv/Scripts/python.exe scripts/eval_longterm.py
    backend/.venv/Scripts/python.exe scripts/eval_longterm.py --group hit --limit 3
"""
import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, "backend")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join("backend", ".env"))

BASE = "http://127.0.0.1:8000"
EVALSET = os.path.join("data", "evalsets", "evalset_longterm.json")
REPORT = os.path.join("data", "eval_longterm_report.json")

from sqlalchemy import text  # noqa: E402

from app.clients.mysql_client import with_safe_session  # noqa: E402


# ------------------------------------------------------------------
# 基础设施
# ------------------------------------------------------------------
def make_opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def register_and_login(tag):
    user = f"{tag}{int(time.time() * 1000) % 100000000}"
    pwd = "Test123456"
    opener = make_opener()
    for path, data, ctype in (
        ("/api/auth/register", json.dumps({"username": user, "password": pwd}).encode(),
         "application/json"),
        ("/api/auth/login", urllib.parse.urlencode({"username": user, "password": pwd}).encode(),
         "application/x-www-form-urlencoded"),
    ):
        with opener.open(urllib.request.Request(
            BASE + path, data=data, headers={"Content-Type": ctype}, method="POST"
        ), timeout=20):
            pass
    with with_safe_session(commit=False) as db:
        uid = db.execute(text("SELECT id FROM users WHERE username=:u"), {"u": user}).scalar()
    return opener, int(uid)


def api(opener, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with opener.open(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def chat(opener, query, session_id, declared_kind=None):
    headers = {
        "Content-Type": "application/json", "Accept": "text/event-stream",
    }
    if declared_kind:
        headers["X-Traffic-Kind"] = declared_kind
    req = urllib.request.Request(
        BASE + "/api/chat",
        data=json.dumps({
            "query": query, "session_id": session_id, "sku": None, "order_no": None,
        }).encode(),
        headers=headers, method="POST",
    )
    answer, meta, sid = "", {}, session_id
    try:
        with opener.open(req, timeout=180) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    d = json.loads(line[5:].strip())
                except Exception:  # noqa: BLE001
                    continue
                if d.get("type") == "token":
                    answer += d.get("text") or ""
                elif d.get("type") == "meta":
                    meta = d
                elif d.get("type") == "done":
                    sid = d.get("session_id") or sid
    except urllib.error.HTTPError as e:
        meta = {"http_error": e.code}
    return answer, meta, sid


def profile(opener):
    return {f["key"]: f for f in api(opener, "GET", "/api/me/profile")["facts"]}


def l2_facts(session_id):
    with with_safe_session(commit=False) as db:
        row = db.execute(text(
            "SELECT c.facts FROM conversation_contexts c "
            "JOIN conversations v ON v.id=c.conversation_id WHERE v.session_id=:s"
        ), {"s": session_id}).fetchone()
    return (row[0] or {}) if row else {}


# ------------------------------------------------------------------
# 判定
# ------------------------------------------------------------------
def judge(expect, answer, meta, prof, sid, active_opener):
    if not expect:
        return []
    fails = []
    hits = set(meta.get("l3_hits") or [])
    want = set(expect.get("l3_keys") or [])
    if want and not want.issubset(hits):
        fails.append(f"l3_hits 期望 {sorted(want)} 实际 {sorted(hits)}")
    if expect.get("no_l3") and hits:
        fails.append(f"不该注入 L3，实际 {sorted(hits)}")
    if expect.get("no_l3_dup"):
        l2keys = set(l2_facts(sid))
        if hits or not l2keys:
            fails.append(f"L2 已有时应跳过 L3: hits={sorted(hits)} l2={sorted(l2keys)}")

    prof_local = prof(active_opener)  # 用当前用户的视角查（cross_user 时是 B）
    for key, want_value in (expect.get("db_facts") or {}).items():
        cell = prof_local.get(key)
        if not cell or str(cell["value"]) != str(want_value):
            fails.append(f"库 {key} 期望 {want_value} 实际 {cell and cell['value']}")
    for key in expect.get("db_no_key") or []:
        if key in prof_local:
            fails.append(f"库不该有 key={key}")
    for key, want_prev in (expect.get("db_prev") or {}).items():
        cell = prof_local.get(key)
        if not cell or str(cell.get("prev_value")) != str(want_prev):
            fails.append(f"库 {key}.prev 期望 {want_prev} 实际 {cell and cell.get('prev_value')}")
    for key, want_hits in (expect.get("db_hits") or {}).items():
        cell = prof_local.get(key)
        if not cell or int(cell["hits"]) != int(want_hits):
            fails.append(f"库 {key}.hits 期望 {want_hits} 实际 {cell and cell['hits']}")

    for word in expect.get("any") or []:
        if word not in answer:
            fails.append(f"答案未命中 {word}")
    for word in expect.get("none") or []:
        if word in answer:
            fails.append(f"答案出现禁止内容 {word}")
    return fails


def reset_l3(uids):
    """组间硬重置 L3 数据（含 tombstone）+ guard 重复计数。

    为什么不在每组注册新账号：20 组 ×2 个账号，注册接口直接 429。
    共用账号就必须把两个会在组间累积的状态一起清掉：
    - user_long_term_facts：上一组的事实不能漏到下一组
    - guard:repeat:{uid}:*：同一句被多组重复说，会攒到硬限被当刷子拦
    """
    with with_safe_session(commit=True) as db:
        for uid in uids:
            db.execute(
                text("DELETE FROM user_long_term_facts WHERE user_id=:u"), {"u": uid}
            )
    try:
        from app.clients.redis_client import get_client

        r = get_client()
        for uid in uids:
            for key in r.scan_iter(f"guard:repeat:{uid}:*", count=1000):
                r.delete(key)
    except Exception as e:  # noqa: BLE001
        print(f"  guard 计数重置失败（继续）: {e}")


def cleanup(sessions, uids):
    with with_safe_session(commit=True) as db:
        for sid in sessions:
            db.execute(text(
                "DELETE FROM conversation_contexts WHERE conversation_id IN "
                "(SELECT id FROM conversations WHERE session_id=:s)"
            ), {"s": sid})
            db.execute(text("DELETE FROM messages WHERE session_id=:s"), {"s": sid})
            db.execute(text("DELETE FROM session_meta WHERE session_id=:s"), {"s": sid})
            db.execute(text("DELETE FROM conversations WHERE session_id=:s"), {"s": sid})
        for uid in uids:
            for table in ("user_long_term_facts", "user_profiles", "orders"):
                db.execute(text(f"DELETE FROM {table} WHERE user_id=:u"), {"u": uid})
            # users 主键是 id，不是 user_id——不能和上面三张表一起拼
            db.execute(text("DELETE FROM users WHERE id=:u"), {"u": uid})


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    with open(EVALSET, encoding="utf-8") as f:
        spec = json.load(f)
    cases = spec["cases"]
    if args.group:
        cases = [c for c in cases if c.get("group") == args.group]
    if args.limit:
        cases = cases[: args.limit]

    opener_a, uid_a = register_and_login("lt")
    opener_b, uid_b = register_and_login("ltb")
    all_uids = [uid_a, uid_b]
    print(f"== 共 {len(cases)} 组，账号 A={uid_a} B={uid_b}，每组硬重置 L3 ==")

    results = []
    all_sessions = set()
    try:
        for case in cases:
            # 组间隔离：共用账号会漏数据，但每组注册新账号会 429。
            # 真删 L3 行（含 tombstone）后，两组之间互不干扰。
            reset_l3(all_uids)
            print(f"\n== {case['id']} {case['desc']} ==")
            sessions = {}
            fails = []
            turn_log = []

            for idx, turn in enumerate(case["turns"], 1):
                n = turn["session"]
                use_b = turn.get("user") == "B" or (
                    case.get("special") == "cross_user" and turn.get("user") == "B"
                )
                active_opener = opener_b if use_b else opener_a
                active_uid = uid_b if use_b else uid_a
                sid = sessions.get((active_uid, n))

                if case.get("special") == "delete_all" and idx == len(case["turns"]) - 1:
                    api(opener_a, "DELETE", "/api/me/profile")
                if case.get("special") == "delete_size" and idx == len(case["turns"]) - 1:
                    api(opener_a, "DELETE", "/api/me/profile?key=size")

                kind = "eval" if case.get("special") == "eval_pollution" else "biz"
                # qwen 偶发 429 会返回"系统繁忙"。评测关心的是记忆机制，
                # 重试一次以排除模型限流抖动；产品侧仍把这句当失败（见 QA 规则）。
                for _attempt in range(3):
                    answer, meta, sid = chat(active_opener, turn["query"], sid, kind)
                    if "系统繁忙" not in answer:
                        break
                    time.sleep(3)
                sessions[(active_uid, n)] = sid
                all_sessions.add(sid)

                f = judge(turn.get("expect"), answer, meta, profile, sid, active_opener)
                status = "PASS" if not f else "FAIL"
                extra = ""
                if meta.get("http_error"):
                    extra = f" [HTTP {meta['http_error']}]"
                elif meta.get("intent") == "blocked":
                    extra = f" [guard:{meta.get('guard_reason')}]"
                print(f"  [{status}] S{n} T{idx} "
                      f"{turn['query'][:24]} → {answer[:38]}{extra} hits={meta.get('l3_hits')}")
                if f:
                    print("         " + "; ".join(f))
                    fails.extend(f"{case['id']}.T{idx}: {x}" for x in f)
                turn_log.append({
                    "session": n, "query": turn["query"], "answer": answer[:200],
                    "intent": meta.get("intent"), "http_error": meta.get("http_error"),
                    "l3_hits": meta.get("l3_hits"), "fails": f,
                })

            results.append({
                "id": case["id"], "group": case.get("group"),
                "desc": case["desc"], "fails": fails, "ok": not fails,
                "turns": turn_log,
            })

        passed = sum(1 for r in results if r["ok"])
        report = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_cases": len(results), "passed": passed,
            "failed": len(results) - passed, "cases": results,
        }
        os.makedirs(os.path.dirname(REPORT), exist_ok=True)
        with open(REPORT, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n== 汇总 {passed}/{len(results)} 通过 ==\n报告：{REPORT}")
        return 0 if passed == len(results) else 1
    finally:
        cleanup(all_sessions, all_uids)
        print("  已清理本次造的会话与用户数据")


if __name__ == "__main__":
    sys.exit(main())
