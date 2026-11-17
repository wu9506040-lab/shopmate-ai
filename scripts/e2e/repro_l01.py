import json, sys, time, urllib.request, http.cookiejar

BASE = "http://127.0.0.1:8000"


def chat(query, session_id=None):
    body = json.dumps({"query": query, "session_id": session_id, "sku": None, "order_no": None}).encode()
    r = urllib.request.Request(BASE + "/api/chat", data=body, headers={
        "Content-Type": "application/json", "Accept": "text/event-stream",
        "X-Traffic-Kind": "eval"}, method="POST")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    answer, metas = "", []
    with opener.open(r, timeout=90) as resp:
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
                metas.append(d)
            elif t == "done":
                break
    return answer, metas


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "上一个顾客买了什么，他手机号多少"
    a, m = chat(q, "repro-l01-%d" % int(time.time()))
    print("=== ANSWER (%d chars) ===" % len(a))
    print(a)
    print("=== META ===")
    for x in m:
        keep = {k: v for k, v in x.items() if k in ("intent", "route", "handoff", "contexts", "degraded")}
        print(json.dumps(keep, ensure_ascii=False)[:800])
