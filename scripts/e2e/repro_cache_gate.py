"""缓存闸门验证（登录用户版）。

匿名用户 user_id=0 在 response_cache 里直接短路（防串），所以必须登录才能测到缓存。

步骤：
1. 新会话问政策问句 → 首轮（history 空）允许走缓存，miss 后写入
2. 换一个新会话再问同一句 → 应 cache_hit（证明缓存本身没被关死）
3. 在步骤 2 的会话里接着再问同一句（此时有 history）→ 必须 NOT cache_hit
"""
import json
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar

BASE = "http://127.0.0.1:8000"
Q = "退货运费谁出"


def make_opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def register_and_login():
    user = "memcache%d" % int(time.time())
    pwd = "Test123456"
    body = json.dumps({"username": user, "password": pwd}).encode()
    req = urllib.request.Request(BASE + "/api/auth/register", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    opener = make_opener()
    try:
        with opener.open(req, timeout=20) as r:
            print(f"注册 {user}: {r.status}")
    except Exception as e:
        print(f"注册失败（可能已存在，继续登录）: {e}")

    data = urllib.parse.urlencode({"username": user, "password": pwd}).encode()
    req = urllib.request.Request(BASE + "/api/auth/login", data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with opener.open(req, timeout=20) as r:
        print(f"登录 {user}: {r.status}")
    return opener


def chat(opener, query, session_id):
    body = json.dumps({"query": query, "session_id": session_id,
                       "sku": None, "order_no": None}).encode()
    req = urllib.request.Request(BASE + "/api/chat", data=body, headers={
        "Content-Type": "application/json", "Accept": "text/event-stream",
        "X-Traffic-Kind": "eval"}, method="POST")
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
            if d.get("type") == "token":
                answer += d.get("text") or ""
            elif d.get("type") == "meta":
                meta = d
            elif d.get("type") == "done":
                break
    return answer, meta


def one(opener, label, sid):
    a, m = chat(opener, Q, sid)
    print(f"{label}: intent={m.get('intent')} method={m.get('intent_method')} len={len(a)}")
    return m.get("intent_method")


if __name__ == "__main__":
    ts = int(time.time())
    op = register_and_login()
    one(op, "① 首轮（应正常生成并写缓存）", f"cg-a-{ts}")
    time.sleep(1)
    one(op, "② 新会话同句（应 cache_hit）", f"cg-b-{ts}")
    one(op, "③ 有历史后同句（必须 NOT cache_hit）", f"cg-b-{ts}")
