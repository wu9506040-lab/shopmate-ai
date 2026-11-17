"""多轮记忆评测（20 组，数据在 data/evalsets/evalset_multiturn.json）

为什么单独一个跑批器，而不是写在 e2e 脚本里：
README 写「20 组多轮场景」，但此前仓库里一组都没有——那个数字是空的。
手工脚本只证明"这次跑通了"，评测集才能证明"下次没退化"。

判据来源两处（缺一不可）：
- 用户可见答案：有没有让买家重复报身高/订单号、有没有出兜底话术
- conversation_contexts 表：改口后的值到底写成什么（答案里可能看不出来）

指标四项：
- 二次提问率：带 not_ask 的轮次里，答案仍索要同类信息的比例
- 兜底率：答案里出现"没查准/系统繁忙"的比例
- 缓存串答案：两个语境不同的问题拿到完全相同的答案
- 拦截数：intent=blocked（多轮里被 guard 拦下，链路直接断）

用法：
    backend/.venv/Scripts/python.exe scripts/eval_multiturn.py
    backend/.venv/Scripts/python.exe scripts/eval_multiturn.py --group size --limit 3
"""
import argparse
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, "backend")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join("backend", ".env"))

BASE = "http://127.0.0.1:8000"
EVALSET = os.path.join("data", "evalsets", "evalset_multiturn.json")
REPORT = os.path.join("data", "eval_multiturn_report.json")

FALLBACK_MARKS = ("这块资料我没查准", "系统繁忙", "请稍后重试")

from sqlalchemy import text  # noqa: E402

from app.clients.mysql_client import with_safe_session  # noqa: E402


# ------------------------------------------------------------------
# 基础设施
# ------------------------------------------------------------------
def make_opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def user_id_of(username):
    with with_safe_session(commit=False) as db:
        return int(db.execute(
            text("SELECT id FROM users WHERE username=:u"), {"u": username}
        ).scalar())


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
        req = urllib.request.Request(
            BASE + path, data=data, headers={"Content-Type": ctype}, method="POST"
        )
        try:
            with opener.open(req, timeout=20):
                pass
        except Exception as e:  # noqa: BLE001
            print(f"  {path} 跳过（{e}）")
    return opener, user_id_of(user)


def seed_orders(user_id, order_nos):
    with with_safe_session(commit=True) as db:
        for idx, no in enumerate(order_nos):
            db.execute(text(
                "INSERT INTO orders (order_no, user_id, status, total_amount, "
                "receiver_name, receiver_phone, create_time, deleted) "
                "VALUES (:no, :uid, 'delivered', 599.00, '测试买家', '13800000000', "
                "DATE_SUB(NOW(), INTERVAL :d DAY), 0)"
            ), {"no": no, "uid": user_id, "d": idx + 2})


def chat(opener, query, session_id):
    body = json.dumps({
        "query": query, "session_id": session_id, "sku": None, "order_no": None,
    }).encode()
    req = urllib.request.Request(BASE + "/api/chat", data=body, headers={
        "Content-Type": "application/json", "Accept": "text/event-stream",
        # 打标：评测流量不计入业务看板（session_meta）
        "X-Traffic-Kind": "eval",
    }, method="POST")
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
                t = d.get("type")
                if t == "token":
                    answer += d.get("text") or ""
                elif t == "meta":
                    meta = d
                elif t == "done":
                    sid = d.get("session_id") or sid
    except urllib.error.HTTPError as e:
        meta = {"http_error": e.code}
    return answer, meta, sid


def ctx_of(session_id):
    with with_safe_session(commit=False) as db:
        return db.execute(text(
            "SELECT c.current_order_no, c.facts FROM conversation_contexts c "
            "JOIN conversations v ON v.id = c.conversation_id "
            "WHERE v.session_id = :s AND c.deleted = 0"
        ), {"s": session_id}).fetchone()


# ------------------------------------------------------------------
# 判定
# ------------------------------------------------------------------
def judge_turn(answer, expect, asks_res, prev_answer):
    """返回 fails 列表（空即通过）。

    not_ask 支持三种取值：
    - true   → 三类别全查（最严）
    - "body" → 不许再问身高/体重（尺码推荐的最常见重复提问）
    - "size" → 不许再问「您穿什么码」
    - "order"→ 不许再要订单号
    分类而不是一刀切：买家说「我平时穿 L」，问「M 能穿吗」时模型要身高体重是合理的，
    要的是"别再问已知的那类"，不是"什么都不能问"。
    """
    fails = []
    if not answer.strip():
        # 跨账号复用 session 会被后端直接 404（IDOR 防护）→ 空答案就是"被拒绝"，不是失败
        if not expect.get("allow_empty"):
            fails.append("空答案")
        return fails

    kind = expect.get("not_ask")
    if kind:
        kinds = list(asks_res.keys()) if kind is True else [kind]
        for k in kinds:
            m = asks_res.get(k).search(answer) if asks_res.get(k) else None
            if m:
                fails.append(f"仍索要信息({k})：{m.group(0)}")

    expected_any = expect.get("any") or []
    if expected_any and not any(k in answer for k in expected_any):
        fails.append(f"未命中期望：{expected_any}")

    for bad in expect.get("none") or []:
        if bad in answer:
            fails.append(f"出现禁止内容：{bad}")

    if expect.get("no_fallback") and any(m in answer for m in FALLBACK_MARKS):
        fails.append("出现兜底话术")

    if expect.get("not_same_as_prev") and prev_answer and answer.strip() == prev_answer.strip():
        fails.append("与上一轮答案完全相同（疑似缓存串答案）")

    return fails


def judge_facts(session_id, expect_facts):
    """查库校验槽位（改口、换单这类语义只有库能证明）。"""
    if not expect_facts:
        return []
    row = ctx_of(session_id)
    if row is None:
        return [f"上下文行不存在，无法校验 {expect_facts}"]
    current_order_no, facts_raw = row[0], row[1] or {}
    try:
        facts = json.loads(facts_raw) if isinstance(facts_raw, str) else facts_raw
    except Exception:  # noqa: BLE001
        facts = {}

    fails = []
    for key, want in expect_facts.items():
        if key == "current_order_no":
            got = current_order_no
        else:
            cell = (facts or {}).get(key)
            got = cell.get("value") if isinstance(cell, dict) else cell
        if str(got) != str(want):
            fails.append(f"槽位 {key} 期望 {want} 实际 {got}")
    return fails


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", default="", help="只跑某一组（size/order/long/...）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 组")
    args = parser.parse_args()

    with open(EVALSET, encoding="utf-8") as f:
        spec = json.load(f)
    asks_res = {
        k: re.compile("|".join(v)) for k, v in spec["asks"].items()
    }

    cases = spec["cases"]
    if args.group:
        cases = [c for c in cases if c.get("group") == args.group]
    if args.limit:
        cases = cases[: args.limit]

    stamp = int(time.time()) % 100000
    order_a = f"ORD20261116{stamp:05d}A"
    order_b = f"ORD20261116{stamp:05d}B"
    repl = {"{order_a}": order_a, "{order_b}": order_b}

    print("== 准备账号 ==")
    opener, uid = register_and_login("mt")
    seed_orders(uid, [order_a, order_b])
    print(f"  uid={uid} order_a={order_a} order_b={order_b}")

    results = []
    blocked_turns = []
    metrics = {"ask_turns": 0, "ask_fail": 0, "turns": 0, "fallback": 0,
               "same_answer": 0, "blocked": 0}

    for case in cases:
        title = f"{case['id']} {case['desc']}"
        print(f"\n== {title} ==")
        session_id = None
        case_fails = []
        prev_answer = ""
        active_opener = opener

        if case.get("special") == "cross_account":
            # 先用主账号建会话，之后换成第二个账号继续发（验证归属隔离）
            for turn in case["turns"][:1]:
                query = _fill(turn["query"], repl)
                _ans, _meta, session_id = chat(opener, query, session_id)

        for idx, turn in enumerate(case["turns"], 1):
            if case.get("special") == "cross_account" and idx == 1:
                continue  # 第一轮已单独发过
            if case.get("special") == "cross_account" and idx == 2:
                active_opener, _uid2 = register_and_login("mtb")

            query = _fill(turn["query"], repl)
            expect = _fill_deep(turn.get("expect") or {}, repl)
            answer, meta, session_id = chat(active_opener, query, session_id)

            metrics["turns"] += 1
            if meta.get("intent") == "blocked":
                metrics["blocked"] += 1
                # 拦截的轮次不落库、也不进任何表，不记在这里就永远查不到
                blocked_turns.append(
                    {"case": case["id"], "turn": idx, "query": query,
                     "reason": meta.get("guard_reason"), "layer": meta.get("guard_layer")}
                )
            if any(m in answer for m in FALLBACK_MARKS):
                metrics["fallback"] += 1
            if expect.get("not_ask"):
                metrics["ask_turns"] += 1

            fails = judge_turn(answer, expect, asks_res, prev_answer)
            if any(f.startswith("仍索要") for f in fails):
                metrics["ask_fail"] += 1
            if any("缓存串答案" in f for f in fails):
                metrics["same_answer"] += 1

            fails += judge_facts(session_id, expect.get("expect_facts"))
            status = "PASS" if not fails else "FAIL"
            print(f"  [{status}] T{idx} {query[:28]} → {answer[:46]}")
            if fails:
                print(f"         {'; '.join(fails)}")
                case_fails.extend(f"{case['id']}.T{idx}: {f}" for f in fails)

            prev_answer = answer

        results.append({
            "id": case["id"], "group": case.get("group"), "desc": case["desc"],
            "session_id": session_id, "fails": case_fails,
            "ok": not case_fails,
        })

    passed = sum(1 for r in results if r["ok"])
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_cases": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "metrics": {
            **metrics,
            "ask_repeat_rate": round(metrics["ask_fail"] / metrics["ask_turns"], 3)
            if metrics["ask_turns"] else None,
            "fallback_rate": round(metrics["fallback"] / metrics["turns"], 3)
            if metrics["turns"] else None,
        },
        "cases": results,
        "blocked_turns": blocked_turns,
    }
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n== 汇总 ==\n  用例 {passed}/{len(results)} 通过")
    print(f"  二次提问率 {report['metrics']['ask_repeat_rate']} "
          f"兜底率 {report['metrics']['fallback_rate']} "
          f"拦截 {metrics['blocked']} 轮")
    print(f"  报告：{REPORT}")
    return 0 if passed == len(results) else 1


def _fill(text, repl):
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


def _fill_deep(value, repl):
    """占位符替换（递归处理 expect 里的 {order_a}）"""
    if isinstance(value, str):
        return _fill(value, repl)
    if isinstance(value, list):
        return [_fill_deep(v, repl) for v in value]
    if isinstance(value, dict):
        return {k: _fill_deep(v, repl) for k, v in value.items()}
    return value


if __name__ == "__main__":
    sys.exit(main())
