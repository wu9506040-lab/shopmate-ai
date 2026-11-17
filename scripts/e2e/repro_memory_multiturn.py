"""多轮记忆契约验证：同一 session 连问三轮，看清洗与缓存闸门。

验证点：
1. 第 1 轮 history 为空 → 允许缓存；第 2/3 轮 history 非空 → 不再命中缓存
2. 历史里的引用角标被剥掉（看服务日志 [memory] 历史已清洗）
3. 指代能接上（"那它保暖吗" 要落在同一款上）
"""
import json
import sys
import time
import urllib.request
import http.cookiejar

BASE = "http://127.0.0.1:8000"


def chat(query, session_id):
    body = json.dumps({"query": query, "session_id": session_id,
                       "sku": None, "order_no": None}).encode()
    req = urllib.request.Request(BASE + "/api/chat", data=body, headers={
        "Content-Type": "application/json", "Accept": "text/event-stream",
        "X-Traffic-Kind": "eval"}, method="POST")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    answer, meta = "", {}
    with opener.open(req, timeout=90) as resp:
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
                meta = d
            elif t == "done":
                break
    return answer, meta


if __name__ == "__main__":
    sid = "memcheck-%d" % int(time.time())
    turns = [
        "加绒夹克多少钱",
        "那它保暖吗",
        "跟刚才那件比，哪个厚一点",
    ]
    for i, q in enumerate(turns, 1):
        try:
            a, m = chat(q, sid)
        except Exception as e:
            print(f"[{i}] {q!r} -> 请求失败: {type(e).__name__} {e}")
            continue
        has_mark = "[" in a and "]" in a
        print(f"[{i}] {q!r}")
        print(f"    intent={m.get('intent')} method={m.get('intent_method')} 角标残留={has_mark}")
        print(f"    答({len(a)}字): {a[:110]}")
