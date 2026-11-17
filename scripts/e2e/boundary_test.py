"""边界/安全 E2E —— 全部经 nginx 5173 浏览器真实路径"""
import json
import sys, urllib.request, urllib.parse, urllib.error, http.cookiejar, time

import argparse
BASE = argparse.ArgumentParser().parse_args().base if "--base" in sys.argv else "http://127.0.0.1:5173"
TS = str(int(time.time()))[-6:]
results = []

def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS" if ok else "FAIL"), name, ("| " + str(detail)[:110] if detail else ""))

def raw(method, path, body=None, headers=None, form=False):
    h = dict(headers or {})
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

def login_mall(user, pwd):
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    body = urllib.parse.urlencode({"username": user, "password": pwd}).encode()
    r = urllib.request.Request(BASE + "/api/auth/login", data=body,
                               headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with op.open(r, timeout=30) as resp:
        json.loads(resp.read())
    return jar, op

# ============ 1. Java 全局异常边界 ============
st, body = raw("POST", "/api/auth/register", body=b"{bad json!!",
               headers={"Content-Type": "application/json"})
check("坏 JSON(register) → 400(非500)", st == 400, f"{st} {body[:80]}")

st, body = raw("POST", "/api/auth/login?scope=admin",
               form=False, body={"username": "admin", "password": "admin123"})
check("JSON 打 form 端点 → 415", st == 415, f"{st}")

st, body = raw("GET", "/api/admin/handoff/tickets/abc", headers={"Authorization": "Bearer x"})
check("ticket id 非数字 → 400/401(非500)", st in (400, 401), f"{st} {body[:60]}")

st, body = raw("POST", "/api/auth/login?scope=admin", form=True, body={"password": "admin123"})
check("login 缺 username → 400(非500)", st == 400, f"{st} {body[:60]}")

# ============ 2. 注册边界 ============
uname = f"bnd{TS}"
st, body = raw("POST", "/api/auth/register", body={"username": uname, "password": " passw0rd123 ",
               "display_name": "边界测试", "email": None})
check("注册(密码带空格) → 201", st == 201, f"{st} {body[:80]}")

st, body = raw("POST", "/api/auth/register", body={"username": uname, "password": "x8charok1"})
check("重复注册 → 409", st == 409, f"{st} {body[:60]}")

st, body = raw("POST", "/api/auth/register", body={"username": "ab", "password": "x8charok1"})
check("username 过短 → 422", st == 422, f"{st}")

st, body = raw("POST", "/api/auth/register", body={"username": f"u{TS}", "password": 12345678})
check("password 非字符串 → 422", st == 422, f"{st} {body[:60]}")

st, body = raw("POST", "/api/auth/register", body={"username": f"e{TS}", "password": "x8charok1", "email": "not-an-email"})
check("email 非法 → 422", st == 422, f"{st}")

# 密码带空格 → 注册什么就登录什么（旧代码 trim 后会 401）
jar_b, op_b = None, None
body = urllib.parse.urlencode({"username": uname, "password": " passw0rd123 "}).encode()
r = urllib.request.Request(BASE + "/api/auth/login", data=body,
                           headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
try:
    with urllib.request.urlopen(r, timeout=30) as resp:
        st2 = resp.status
except urllib.error.HTTPError as e:
    st2 = e.code
check("带空格密码原样可登录", st2 == 200, f"{st2}")

# ============ 3. 管理台登录 + 会话时间字段非 null ============
lb = urllib.parse.urlencode({"username": "admin", "password": "admin123"}).encode()
lr = urllib.request.Request(BASE + "/api/auth/login?scope=admin", data=lb,
                            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
with urllib.request.urlopen(lr, timeout=30) as resp:
    tok = json.loads(resp.read())["access_token"]
AUTH = {"Authorization": "Bearer " + tok}

st, body = raw("GET", "/api/conversations", headers={"Authorization": "Bearer " + tok})
d = json.loads(body)
convs = d.get("conversations") or []
with_time = [c for c in convs if c.get("updated_at")]
check("会话列表时间字段非 null(Connector/J9 修复)", bool(convs) and len(with_time) == len(convs),
      f"total={len(convs)} with_time={len(with_time)}")

# ============ 4. 工单权限：买家 token 打管理接口 → 403 ============
try:
    jar_u, op_b = login_mall(uname, " passw0rd123 ")
    ub = urllib.parse.urlencode({"username": uname, "password": " passw0rd123 "}).encode()
    ur = urllib.request.Request(BASE + "/api/auth/login?scope=admin", data=ub,
                                headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(ur, timeout=30) as resp:
            utok = json.loads(resp.read()).get("access_token", "")
    except urllib.error.HTTPError as e:
        utok = ""
    check("买家 scope=admin → 403", utok == "", "买家拿不到 admin token")
except Exception as e:
    op_b = None
    check("买家 scope=admin → 403", False, str(e)[:80])

# ============ 5. Python 侧：越权会话 404（直连 8000 验证核心逻辑） ============
# smoke 用户的会话 id
st, body = raw("GET", "/api/conversations", headers=AUTH)
d = json.loads(body)
sessions = [c["session_id"] for c in (d.get("conversations") or [])]
victim_sid = sessions[0] if sessions else None

# 用另一个账号直连 Python 聊别人的 session
import socket
try:
    # 直连 python api 需要从容器外走：8000 映射到宿主
    body2 = json.dumps({"query": "你好", "session_id": victim_sid}).encode()
    r2 = urllib.request.Request("http://127.0.0.1:8000/api/chat", data=body2,
                                headers={"Content-Type": "application/json"}, method="POST")
    # 需要 Bearer —— 用 Java 给这个用户签一个：登录后 cookie 里有 cs_token？直连 8000 不吃 Java cookie。
    # 换路径：经 nginx → Java 网关（自动互认），用 op_b 的买家 cookie 发别人的 session_id
    bbody = json.dumps({"query": "测试越权", "session_id": victim_sid}).encode()
    br = urllib.request.Request(BASE + "/api/chat", data=bbody,
                                headers={"Content-Type": "application/json"}, method="POST")
    try:
        with op_b.open(br, timeout=60) as resp:
            raw_stream = resp.read().decode("utf-8", "replace")
            leaked = victim_sid and (victim_sid in raw_stream)
            check("越权会话聊天 → 拒绝(非200携带他人历史)", False,
                  f" unexpectedly got {resp.status}")
    except urllib.error.HTTPError as e:
        check("越权会话聊天 → 404/403", e.code in (403, 404), f"{e.code} {e.read().decode()[:80]}")
except Exception as e:
    check("越权会话聊天 → 404/403", False, str(e)[:100])

# ============ 6. Python 侧：超卖拦截（API 层） ============
# bnd 用户下单服饰 SKU FS368998-M（库存 100）× 999 → 400 库存不足
ob = json.dumps({"sku": "FS368998-M", "qty": 999}).encode()
orr = urllib.request.Request(BASE + "/api/orders", data=ob,
                             headers={"Content-Type": "application/json"}, method="POST")
try:
    with op_b.open(orr, timeout=30) as resp:
        check("超卖拦截 qty99 stock6 → 4xx", False, f" unexpectedly 200: {resp.read()[:80]}")
except urllib.error.HTTPError as e:
    check("超卖拦截 qty99 stock6 → 4xx", 400 <= e.code < 500, f"{e.code} {e.read().decode()[:80]}")

# ============ 7. 账号管理：路径遍历与超长 Cookie ============
st, body = raw("POST", "/api/admin/channels/accounts/..%2F..%2Fetc%2Fpasswd/credentials",
               headers=AUTH, body={"credentials": {"cookie": "x" * 100}})
check("账号 id 路径遍历 → 4xx", 400 <= st < 500, f"{st} {body[:60]}")

long_cookie = "cookie2=" + "a" * 20000
st, body = raw("GET", "/api/admin/channels/accounts", headers=AUTH)
groups = json.loads(body).get("groups", [])
xy = next((g for g in groups if g["key"] == "xianyu"), None)
aid = xy["accounts"][0]["id"] if xy and xy.get("accounts") else None
if aid:
    st, body = raw("POST", f"/api/admin/channels/accounts/{aid}/credentials",
                   headers=AUTH, body={"credentials": {"cookie": long_cookie}})
    check("超长 Cookie(20KB) → 400", st == 400, f"{st} {body[:60]}")
else:
    check("超长 Cookie(20KB) → 400", False, "无闲鱼账号可测")

print("\n==== SUMMARY ====")
fails = [r for r in results if not r[1]]
print(f"total={len(results)} pass={len(results)-len(fails)} fail={len(fails)}")
for name, _, detail in fails:
    print("  FAILED:", name, "|", str(detail)[:120])
