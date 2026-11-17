#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重放黄金测试集：把每条 query 的【输入 → 服务真实回答原文】完整落盘。
目的：评测报告只存判定不存原文，用户无法核对——本脚本补齐输入输出证据链。
用法：bash scripts/run_eval_local.sh replay（或 backend/.venv python scripts/replay_evalset.py）
输出：data/replay_answers.json + 控制台全文
"""
import json
import sys
import time
import urllib.error
import urllib.request
import http.cookiejar
from pathlib import Path

_REPO = Path("/app") if Path("/app/data").exists() else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "backend"))

BASE = "http://127.0.0.1:8000"
EVALSET = _REPO / "data/evalsets/evalset_v3_fashion.json"
OUT = _REPO / "data/replay_answers.json"

# 用户截图实测的复合 query（评测集外，检索层 badcase 活例）
EXTRA = [{"id": "F01", "query": "双十一买加绒夹克能优惠多少", "note": "复合 query 反例（活动+品类词）"}]


def register_login(username):
    jar = http.cookiejar.CookieJar()
    body = json.dumps({"username": username, "password": "replay12345",
                       "display_name": "重放核验"}).encode()
    r = urllib.request.Request(BASE + "/api/auth/register", data=body,
                               headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(r, timeout=15)
    except urllib.error.HTTPError as e:
        print(f"  register {username} -> {e.code}（已存在则直接登录）")
    body = urllib.parse.urlencode({"username": username, "password": "replay12345"}).encode()
    r = urllib.request.Request(BASE + "/api/auth/login", data=body,
                               headers={"Content-Type": "application/x-www-form-urlencoded"},
                               method="POST")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(r, timeout=15) as resp:
        assert resp.status == 200, f"登录失败 {username}"
    return jar, opener


def chat_once(opener, query, timeout=90, _retried_429=False):
    body = json.dumps({"query": query}).encode()
    r = urllib.request.Request(BASE + "/api/chat", data=body, headers={
        "Content-Type": "application/json", "Accept": "text/event-stream"}, method="POST")
    answer, sources, st = "", [], None
    t0 = time.perf_counter()
    try:
        with opener.open(r, timeout=timeout) as resp:
            st = resp.status
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    d = json.loads(line[5:].strip())
                except Exception:
                    continue
                t = d.get("type")
                if t == "token":
                    answer += d.get("text") or ""
                elif t == "meta":
                    for c in (d.get("contexts") or []):
                        if isinstance(c, dict):
                            sources.append(str(c.get("source") or c.get("doc_key") or "?"))
                elif t == "done":
                    break
    except urllib.error.HTTPError as e:
        st = e.code
    # /api/chat 30/min 固定窗口：重放 32 条连发会撞，等窗口滚动后重试一次
    if st == 429 and not _retried_429:
        time.sleep(62)
        return chat_once(opener, query, timeout, _retried_429=True)
    return st, answer, sources, round((time.perf_counter() - t0) * 1000)


def main():
    cases = json.loads(EVALSET.read_text(encoding="utf-8"))["cases"]
    ts = str(int(time.time()))[-8:]
    jar, opener = register_login(f"replay{ts}")

    out = []
    todo = [c for c in cases if c.get("query") or c["id"] == "S06"] + EXTRA
    print(f"=== 重放 {len(todo)} 条（输入输出原文）===\n")
    for c in todo:
        cid, q = c["id"], c.get("query") or ""
        st, ans, sources, ms = chat_once(opener, q)
        rec = {"id": cid, "query": q, "http": st, "answer": ans,
               "sources": sources[:5], "ms": ms,
               "dim": c.get("dim"), "expected_doc_key": c.get("expected_doc_key")}
        out.append(rec)
        srcs = " | ".join(s for s in sources[:3]) or "-"
        print(f"[{cid}] {q}")
        print(f"  -> http={st} ms={ms} sources=[{srcs}]")
        print(f"  答案: {ans[:300] or '（空）'}")
        print()
        time.sleep(0.3)

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已保存: {OUT}")


if __name__ == "__main__":
    main()
