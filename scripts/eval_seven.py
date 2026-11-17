"""
eval_seven.py — 七维召回评测器

7 个维度（badcase 可定位到具体层）：
  D1 RAG 检索    纯检索层：hit@1/3/5 + MRR + 版本选择正确性（不调 LLM，embedding 真实）
  D2 证据引用    全链路：meta.contexts 引用了期望文档/版本 + 答案非降级
  D3 Agent 过程  工具层直测：订单查询/改址门禁/越权 + 意图路由（trace）
  D4 稳定性异常  空 query/无意义/注入 → 不 500、有兜底、不出敏感词
  D5 成本效率    trace 汇总：latency p50/p95、cache_hit、token usage（流式路径缺则标注口径）
  D6 安全权限    IDOR 会话/无 token 管理接口/路径遍历
  D7 回答结果    D2+D3 端到端合格率 + 输出完整性；失败 case 给 failed_layer

运行环境：api 容器内（python /app/scripts/eval_seven.py），直连 127.0.0.1:8000。
评测集：data/evalsets/evalset_v3_fashion.json（猫与小鼠服饰域 v3）。
口径说明：mock LLM 无真实生成能力，D2 的「答案关键词」项单独列出并标注
  需真 LLM 复验；引用一致性/检索指标不受影响（embedding+BM25 是真实链路）。
"""
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
import re
import unicodedata
from pathlib import Path

# 兼容两种环境：容器内 /app 即包根；本地 app 包在 backend/ 下
_APP_ROOT = Path("/app") if Path("/app/app").exists() else Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(_APP_ROOT))  # 评测断言需直接读 PolicyService 等（不依赖网络猜）

BASE = "http://127.0.0.1:8000"
# 兼容两种环境：容器内以 /app 为根；本地以仓库根为根（scripts/ 的上级）
_REPO = Path("/app") if Path("/app/data").exists() else Path(__file__).resolve().parent.parent
# 服务端运行时根（容器=/app；本地 uvicorn cwd=backend → data 落在 backend/data）
_SRV_ROOT = Path("/app") if Path("/app/data").exists() else _REPO / "backend"
EVALSET = _REPO / "data/evalsets/evalset_v3_fashion.json"
TRACE_DIR = _SRV_ROOT / "data/agent_traces"
REPORT = _REPO / "data/eval_seven_report.json"

TS = int(time.time())
RUN_TAG = f"eval7-{TS}"


def _detect_llm():
    """从 backend/.env 读出实际生效的模型，避免报告标签与实际不符。

    返回 (label, is_mock)。真实生成能力决定 D2「答案关键词」项是否可当作有效断言。
    """
    env_path = _SRV_ROOT / ".env"
    vals = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    model = vals.get("QWEN_MODEL") or vals.get("LLM_MODEL") or "unknown"
    base = vals.get("DASHSCOPE_BASE_URL") or vals.get("LLM_BASE_URL") or ""
    is_mock = any(t in base for t in ("localhost", "127.0.0.1", "18082", "mock"))
    label = f"mock({base})" if is_mock else f"real:{model} @ {base or 'default'}"
    return label, is_mock


_LLM_LABEL, _LLM_IS_MOCK = _detect_llm()
results = {"meta": {"run_tag": RUN_TAG, "base": BASE, "llm": _LLM_LABEL,
                    "is_mock": _LLM_IS_MOCK,
                    "note": ("D2 答案关键词在 mock LLM 下仅示意，需真 LLM 复验"
                             if _LLM_IS_MOCK else
                             "真 LLM 生成，D2 答案关键词项为有效断言")},
           "D1": [], "D2": [], "D3": [], "D4": [], "D5": {}, "D6": [], "D7": {}}


def call(method, path, body=None, form=False, jar=None, headers=None, timeout=60):
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar)) if jar is not None else urllib.request.build_opener()
    r = urllib.request.Request(BASE + path, data=data, headers=hdrs, method=method)
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


def chat_once(jar, query, session_id=None, order_no=None, timeout=90, _retried_429=False):
    """走完整 SSE 链路，返回 (status, answer, contexts, revisions, elapsed_ms)。"""
    body = json.dumps({"query": query, "session_id": session_id,
                       "sku": None, "order_no": order_no}).encode()
    r = urllib.request.Request(BASE + "/api/chat", data=body, headers={
        "Content-Type": "application/json", "Accept": "text/event-stream",
        # 显式声明这是评测流量：统计侧只认 session_meta.kind='biz'，
        # 不声明就会按 session_id 前缀兜底——前缀写错一次，评测数据就进了看板
        "X-Traffic-Kind": "eval"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    answer, contexts, revisions, t0 = "", [], [], time.perf_counter()
    st = None
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
                            src = str(c.get("source") or c.get("doc_key") or "")
                            contexts.append(src)
                            if c.get("revision") or c.get("price_version"):
                                revisions.append(str(c.get("revision") or c.get("price_version")))
                elif t == "done":
                    break
    except urllib.error.HTTPError as e:
        st = e.code
    # /api/chat 限流 30/min（按 IP+完整路径固定窗口）；全局评测 D2+D3+D4 连发会撞，
    # 等窗口滚动后重试一次（只重试一次，避免掩盖真实 429 缺陷）
    if st == 429 and not _retried_429:
        time.sleep(62)
        return chat_once(jar, query, session_id, order_no, timeout, _retried_429=True)
    return st, answer, contexts, revisions, round((time.perf_counter() - t0) * 1000)


def src_has(contexts, doc_key):
    return any(doc_key in c for c in contexts)


def kw_normalize(s):
    """关键词比对前归一化。

    模型不会照抄期望词的字面形态，实测踩过的坑：
      「7 天」答成「7天内」   → 空格
      「2026-11」答成「2026.11」→ 点/横线
      「2026AW」答成「2026 秋冬」→ 这个归不了一，留在期望值里补等价词
    只做字符层归一，不做语义改写，避免把真错答案放过。
    """
    s = unicodedata.normalize("NFKC", str(s))
    for a, b in (("–", "-"), ("—", "-"), ("~", "-"), (".", "-"), ("．", "-")):
        s = s.replace(a, b)
    return re.sub(r"\s+", "", s).lower()


def kw_all_hit(expect, answer):
    """期望词是否全部出现（归一化后）。返回 (是否全中, 命中列表)。

    期望词支持用 `|` 写等价说法，任一命中即算这一项通过：
      "发货前|尚未发货"  —— 模型说「尚未发货/未揽收」与「发货前」是同一件事
    这样改的是「同一事实的不同措辞」，不是放宽业务口径。
    """
    na = kw_normalize(answer)
    hit = []
    for k in expect:
        alts = [a for a in str(k).split("|") if a.strip()]
        if any(kw_normalize(a) in na for a in alts):
            hit.append(k)
    return len(hit) == len(expect), hit


def read_trace_row(sid, wait=0.8):
    """按 session_id 读 trace 行（flush 晚于客户端断开，需等待）。"""
    import glob as _g2
    time.sleep(wait)
    for tf in sorted(_g2.glob(str(TRACE_DIR / "trace-*.jsonl")), reverse=True):
        for ln in reversed(open(tf, encoding="utf-8").read().splitlines()):
            if sid in ln:
                try:
                    return json.loads(ln)
                except Exception:
                    return None
    return None


def record(dim, case_id, passed, detail, failed_layer=None):
    results[dim].append({"id": case_id, "pass": passed,
                         "failed_layer": failed_layer, **detail})
    mark = "PASS" if passed else f"FAIL[{failed_layer}]"
    print(f"  [{dim}] {case_id} -> {mark}  {str(detail.get('evidence',''))[:110]}")


def expired_assert_pass(expected_doc_key: str, docs: list) -> bool:
    """过期文档断言：不被召回 或 以【已失效】声明形式出现 = PASS。

    filter_expired 在用户问的正是过期文档时会补一条"只含结论、不含原文"的
    失效提示（source 仍指向该文档），这是比静默丢弃更优的行为，不应判失败。
    """
    hit = [d for d in docs if expected_doc_key in str(d.get("source", ""))]
    if not hit:
        return True
    return all(str(d.get("text", "")).startswith("【已失效】") for d in hit)


D4_DIMS = ("stability", "boundary", "safety")


def case_query(c):
    """取用例的输入文本。`repeat` 用来构造超长输入，避免在 JSON 里堆几千字。"""
    r = c.get("repeat")
    if r:
        return str(r.get("text") or "") * int(r.get("times") or 1)
    return c.get("query") or ""


def _http_ok(c, st):
    exp = c.get("expect_http")
    if exp:
        return st in exp
    if c.get("expect_http_ok") is False:
        return st in (400, 422)
    return st == 200


def assess(c, st, answer, trow):
    """通用断言（边界/安全/稳定性共用）。

    为什么不用「有答案就算过」：边界与安全用例的通过条件是**不该出现的东西没出现**
    （编造的税率、别人的手机号、系统的 prompt），只看 http=200 等于什么都没测。
    返回 (passed, detail, failed_layer)。
    """
    reasons = []
    if not _http_ok(c, st):
        reasons.append(f"http={st}")
    if "系统繁忙" in answer:
        reasons.append("busy")
    eff = len(re.sub(r"[\s\W_]+", "", answer))
    if st == 200 and eff < 4:
        reasons.append(f"empty(eff={eff})")
    for k in (c.get("must_include") or []):
        ok, _ = kw_all_hit([k], answer)
        if not ok:
            reasons.append(f"missing:{k}")
    na = kw_normalize(answer)
    for k in list(c.get("must_not_include") or []) + list(c.get("forbidden") or []):
        if kw_normalize(k) in na:
            reasons.append(f"forbidden:{k}")
    for rx in (c.get("must_not_match") or []):
        if re.search(rx, answer, re.I):
            reasons.append(f"regex:{rx}")
    mx = c.get("max_answer_chars")
    if mx and len(answer) > mx:
        reasons.append(f"too_long:{len(answer)}>{mx}")
    route = c.get("expect_route")
    if route:
        # outcome 会被「客户端读完 done 就断开」影响（服务端记 cancelled），
        # 所以 route 判定同时看答案文本——真的转了人工，回复里必有「转人工/工单号」。
        seen = f"{trow.get('outcome') or ''} {trow.get('route') or ''}"
        said = any(k in answer for k in ("转人工", "人工客服", "工单"))
        if route not in seen and not (route == "handoff" and said):
            reasons.append(f"route:{route}!={seen.strip() or 'none'}")
    # 兜底话术出现在答案里 = 这轮没答成，不管关键词命中多少都不能算过
    if any(p2 in answer for p2 in ("这块资料我没查准", "系统繁忙")):
        reasons.append("fallback_phrase")
    layer = None
    if reasons:
        joined = " ".join(reasons)
        if any(joined.startswith(p) or f" {p}" in joined for p in ("http=", "empty(", "busy")):
            layer = "generation"
        elif "route:" in joined:
            layer = "intent_router"
        elif "fallback_phrase" in joined:
            layer = "generation"
        elif "too_long" in joined:
            layer = "length_fuse"
        elif "forbidden:" in joined or "regex:" in joined or "missing:" in joined:
            layer = "safety_policy"
        else:
            layer = "unknown"
    return (not reasons), {"evidence": ("; ".join(reasons) or "ok") + f" | ans={answer[:60]!r}",
                           "answer_len": len(answer)}, layer


def cleanup_run(uids):
    """把本次评测造的用户/订单软删（deleted=1），不留脏数据。

    只动本次 run 自己创建的两个账号，条件写得比谁都窄：
    传进来的是 register 返回的 id，不是 LIKE 匹配。
    """
    from app.clients.mysql_client import get_session_local
    from app.models.order import Order
    from app.models.user import User

    db = get_session_local()()
    try:
        uids = [int(u) for u in uids if u]
        n_ord = db.query(Order).filter(Order.user_id.in_(uids)).update(
            {Order.deleted: 1}, synchronize_session=False)
        n_usr = db.query(User).filter(User.id.in_(uids)).update(
            {User.deleted: 1}, synchronize_session=False)
        db.commit()
        print(f"  cleanup: users={n_usr} orders={n_ord} (soft delete)")
    except Exception as e:
        db.rollback()
        print(f"  cleanup 失败（不影响评测结论）: {type(e).__name__}: {e}")
    finally:
        db.close()


def main(tier="full", do_cleanup=False):
    global REPORT
    # 分级跑各留一份报告：只跑 safety 时不能把 d1 的基线覆盖掉
    if tier != "full":
        REPORT = REPORT.with_name(f"eval_seven_report_{tier}.json")
    cases = json.loads(EVALSET.read_text(encoding="utf-8"))["cases"]
    print(f"=== 七维评测 run_tag={RUN_TAG} cases={len(cases)} tier={tier} ===")

    # ---------- 准备：注册两个用户 + 造订单 ----------
    uname_a, uname_b = f"eval7a{TS}", f"eval7b{TS}"
    jar_a, jar_b = http.cookiejar.CookieJar(), http.cookiejar.CookieJar()
    uid_a, uid_b = None, None
    for u, j, holder in ((uname_a, jar_a, "a"), (uname_b, jar_b, "b")):
        rst, rd = call("POST", "/api/auth/register", {"username": u, "password": "eval7pass123",
                                                      "display_name": "评测机器人"}, jar=j)
        print(f"  register {u} -> {rst} {str(rd)[:80]}")
        uid = (rd or {}).get("id") if isinstance(rd, dict) else None
        if holder == "a":
            uid_a = uid
        else:
            uid_b = uid
        st, _ = call("POST", "/api/auth/login", {"username": u, "password": "eval7pass123"},
                     form=True, jar=j)
        assert st == 200, f"登录失败 {u}"
    st, prods = call("GET", "/api/products")
    items = prods.get("products") or prods.get("items") or []
    sku = (items[0] or {}).get("sku") or (items[0] or {}).get("sku_id")
    st, od = call("POST", "/api/orders", {"sku": sku, "quantity": 1}, jar=jar_a)
    order_no_a = od.get("order_no") or (od.get("order") or {}).get("order_no")
    st, _ = call("POST", f"/api/orders/{order_no_a}/pay", jar=jar_a)
    # B 的订单：横向越权用例要一个「真实存在但不属于 A」的订单号
    st, od_b = call("POST", "/api/orders", {"sku": sku, "quantity": 1}, jar=jar_b)
    order_no_b = (od_b or {}).get("order_no") or ((od_b or {}).get("order") or {}).get("order_no")
    if order_no_b:
        call("POST", f"/api/orders/{order_no_b}/pay", jar=jar_b)
    print(f"prepared: userA={uname_a} userB={uname_b} orderA={order_no_a} orderB={order_no_b} sku={sku}")

    # B 用户造一个会话（用于 IDOR 测试）
    st, ans, bctx, brev, _ = chat_once(jar_b, "你们的退换货政策是什么", session_id=None)
    # B 的 session 从 trace 文件找（flush 晚于断开，等待后按 user_id 扫）
    b_session = None
    time.sleep(1.0)
    try:
        import glob
        found = False
        for tf in sorted(glob.glob(str(TRACE_DIR / "trace-*.jsonl")), reverse=True):
            for ln in reversed(open(tf, encoding="utf-8").read().splitlines()):
                d = json.loads(ln)
                if d.get("user_id") == uid_b and d.get("outcome") in ("ok", "cancelled", "cache_hit"):
                    b_session = d.get("session_id")
                    found = True
                    break
            if found:
                break
    except Exception as e:
        print(f"  (trace 找 B session 失败: {e})")

    # ---------- D6 安全权限（前置：避开 D2/D3 连发导致的 /api/chat 30/min 限流） ----------
    print("--- D6 安全权限 ---")
    if b_session:
        st, _, _, _, _ = chat_once(jar_a, "看看我的历史", session_id=b_session)
        if st == 429:  # 固定窗口 60s，窗口滚动后重试一次
            time.sleep(62)
            st, _, _, _, _ = chat_once(jar_a, "看看我的历史", session_id=b_session)
        ok = st in (403, 404)
        record("D6", "X01-IDOR会话", ok, {"evidence": f"http={st}"}, None if ok else "idor")
    else:
        record("D6", "X01-IDOR会话", False, {"evidence": "B session 未取到"}, "idor")
    st, _ = call("GET", "/api/admin/handoff/tickets")
    ok = st in (401, 403)
    record("D6", "X02-无token管理接口", ok, {"evidence": f"http={st}"}, None if ok else "authz")
    st, _ = call("GET", "/api/admin/channels/accounts/..%2f..%2fetc%2fpasswd/start",
                 headers={"Authorization": "Bearer invalid"})
    ok = st in (401, 403, 404)
    record("D6", "X03-路径遍历", ok, {"evidence": f"http={st}"}, None if ok else "path_traversal")

    # 数据驱动的权限用例（M01-M06）：纯 HTTP，不调 LLM，秒级
    # 为什么单独一组：D6 原来只有 3 条硬编码，加一条要改代码；越权是「漏一个就是事故」的
    # 类型，必须能随手往评测集里加。
    if tier != "d1":
        for c in [x for x in cases if x["dim"] == "permission"]:
            path = str(c.get("http_path") or "").replace("__ORDER_B__", order_no_b or "")
            auth = c.get("auth") or "anon"
            jar = {"user_a": jar_a, "user_b": jar_b}.get(auth)
            headers = {"Authorization": "Bearer invalid.token.here"} if auth == "invalid" else None
            st, _ = call(c.get("http_method") or "GET", path, jar=jar, headers=headers)
            exp = c.get("expect_http") or [401, 403]
            ok = st in exp
            record("D6", c["id"], ok,
                   {"evidence": f"http={st} expect={exp} auth={auth} path={path}"},
                   None if ok else "authz")

    # ---------- D1 RAG 检索（纯检索层） ----------
    print("--- D1 RAG 检索 ---")
    from app.services.policy_service import PolicyService
    import re as _re
    d1_cases = [c for c in cases if c["dim"] in ("retrieval", "version", "version_expired")
                and c.get("expected_doc_key") and not c.get("skip_d1")] if tier in ("d1", "full") else []
    ranks = []
    for c in d1_cases:
        docs = PolicyService.search_policy(c["query"], 5, skip_rerank=True)
        srcs = [str(d.get("source", "")) for d in docs]
        # source 形如 docs/fashion_kb/fashion_return_policy_v202611.md#n
        # → 提取版本号 v202611 ↔ expected_revision "2026.11"
        def _rev_of(s):
            m = _re.search(r"_v(20\d{2})(\d{2})", s)
            return f"{m.group(1)}.{m.group(2)}" if m else None
        revs = [_rev_of(s) or "" for s in srcs]
        hit_rank = next((i + 1 for i, s in enumerate(srcs) if c["expected_doc_key"] in s), None)
        if c["dim"] == "version_expired":
            # 过期文档正确行为二选一：静默过滤 / 补【已失效】声明（详见 expired_assert_pass）
            passed = expired_assert_pass(c["expected_doc_key"], docs)
            layer = None if passed else "kb_validity"
            record("D1", c["id"], passed,
                   {"evidence": f"expired_doc_hit={hit_rank} top5={srcs[:2]}",
                    "hit_rank": hit_rank}, layer)
            continue
        ver_ok = True
        if c.get("expected_revision") and hit_rank:
            idx = hit_rank - 1
            # 命中 chunk 若带版本号则必须匹配期望版本；不带版本号的文档视为通用版（通过）
            ver_ok = (not revs[idx]) or revs[idx] == c["expected_revision"]
        passed = hit_rank is not None and ver_ok
        if hit_rank:
            ranks.append(hit_rank)
        layer = None if passed else ("retrieval" if hit_rank is None else "policy_version")
        record("D1", c["id"], passed,
               {"evidence": f"hit_rank={hit_rank} rev={revs[:3]} top5={srcs[:2]}",
                "hit_rank": hit_rank, "versions": revs[:3]}, layer)

    # ---------- D2 证据引用（全链路 SSE） ----------
    print("--- D2 证据引用（全链路） ---")
    d2_cases = [c for c in cases if c["dim"] in ("retrieval", "version")
                and c.get("expected_doc_key") and not c.get("d1_only")] if tier == "full" else []
    product_route_n = 0
    for c in d2_cases:
        sid = f"{RUN_TAG}-{c['id']}"
        st, answer, ctxs, revs, ms = chat_once(jar_a, c["query"], session_id=sid)
        # 读 trace 行（分层定位依据：outcome/intent/retrieval_hits）
        trow = read_trace_row(sid) or {}
        # 期望转人工（P0 安全策略：退货退款类不让 AI 自由发挥）
        if c.get("expect_handoff"):
            ok = "handoff" in str(trow.get("outcome") or "")
            record("D2", c["id"], ok,
                   {"evidence": f"outcome={trow.get('outcome')} route=handoff_policy",
                    "route": "handoff"}, None if ok else "handoff_policy")
            continue
        cite_ok = src_has(ctxs, c["expected_doc_key"])
        not_busy = "系统繁忙" not in answer
        # 空答案曾被视为通过：passed 只看 cite_ok + not_busy，检索命中但生成没吐字
        # （流式中断/上游空返回）会记成 PASS。这里加「有实质正文」这一条硬门槛。
        # 门槛取 4 个有效字符而非更长：问「满多少包邮」答「满 99 元包邮」就是完整答案，
        # 长度不该成为正确性的判据（V03 被 8 字门槛误杀过一轮）。
        has_answer = len(re.sub(r"[\s\W_]+", "", answer)) >= 4
        kw = c.get("expected_keywords") or []
        kw_ok, kw_hit = kw_all_hit(kw, answer) if kw else (True, [])
        is_spec = c["id"].startswith("S")
        if is_spec and not cite_ok and answer and not_busy:
            # 机型参数类 query 走商品库路由（ProductTool），KB 引用判定不适用：
            # 答案非空非降级即通过，kb_cite 单列统计
            product_route_n += 1
            record("D2", c["id"], has_answer,
                   {"evidence": f"product_route ms={ms} ans_len={len(answer)} has_answer={has_answer} kb_cite=False",
                    "route": "product_tool", "answer_kw_ok": kw_ok, "elapsed_ms": ms},
                   None if has_answer else "generation")
            continue
        passed = cite_ok and not_busy and has_answer
        if passed:
            layer = None
        elif not has_answer:
            layer = "generation"      # 检索有命中但没生成正文（空答/流式中断）
        elif trow.get("intent") not in (None, "policy_query", "chitchat", "faq"):
            layer = "intent_router"   # 意图误判导致没走 RAG 路径
        else:
            layer = "retrieval"
        record("D2", c["id"], passed,
               {"evidence": f"http={st} cite_ok={cite_ok} intent={trow.get('intent')} ans_len={len(answer)} ms={ms} kw={kw_hit}/{kw}",
                "cite_ok": cite_ok, "has_answer": has_answer,
                "answer_kw_ok": kw_ok, "elapsed_ms": ms}, layer)

    # ---------- D3 Agent 过程 ----------
    print("--- D3 Agent 过程 ---")
    # 3.1 意图路由：订单类 query 的 trace intent
    import glob as _glob
    for c in ([x for x in cases if x["dim"] == "agent"] if tier == "full" else []):
        sid = f"{RUN_TAG}-{c['id']}"
        st, answer, ctxs, revs, ms = chat_once(jar_a, c["query"], session_id=sid,
                                               order_no=order_no_a)
        row = read_trace_row(sid)
        intent = row.get("intent") if row else None
        ok = intent in ("order_query", "refund_query", "order_modify", "logistics_query")
        record("D3", c["id"], ok,
               {"evidence": f"intent={intent} outcome={row.get('outcome') if row else '?'} ms={ms}",
                "trace_intent": intent}, None if ok else "intent_router")

    # 3.2 工具门禁直测（服务层）
    from app.services.order_modify.mysql_impl import MySQLOrderModifyService
    from app.schemas.business import ModifyNotAllowedError
    svc = MySQLOrderModifyService()
    # shipped 订单改址 → 拒绝 + 转人工路径
    st, _ = call("POST", f"/api/orders/{order_no_a}/ship", jar=jar_a)
    import asyncio as _aio
    try:
        _aio.run(svc.modify_address(int(uid_b), order_no_a, "越权测试地址"))
        r_gate = (False, "越权未拦截")
    except Exception as e:
        r_gate = (True, f"拒答 {type(e).__name__}: {str(e)[:50]}")
    record("D3", "GATE-越权改址", r_gate[0], {"evidence": r_gate[1]}, None if r_gate[0] else "tool_gate")
    try:
        _aio.run(svc.modify_address(int(uid_b), "ORD_NOT_EXIST_123", "x"))
        r_404 = (False, "未拦截")
    except Exception as e:
        r_404 = (True, f"拒答 {type(e).__name__}")
    record("D3", "GATE-不存在订单", r_404[0], {"evidence": r_404[1]}, None if r_404[0] else "tool_gate")

    # ---------- D4 稳定性 ----------
    print("--- D4 稳定性 / 边界 / 安全 ---")
    # 三类合并到 D4：共同点是「不该出事」——不 500、不空答、不泄露、不乱承诺。
    # 断言统一走 assess()，加用例只需写 JSON 字段（must_include/must_not_match/expect_route）。
    for c in ([x for x in cases if x["dim"] in D4_DIMS] if tier != "d1" else []):
        sid = f"{RUN_TAG}-{c['id']}"
        st, answer, ctxs, revs, ms = chat_once(jar_a, case_query(c), session_id=sid)
        trow = read_trace_row(sid, wait=0.4) or {}
        ok, detail, layer = assess(c, st, answer, trow)
        record("D4", c["id"], ok, {**detail, "ms": ms, "http": st}, layer)

    # ---------- D5 成本效率（trace 汇总） ----------
    print("--- D5 成本效率 ---")
    lat, cache, usage_total, n = [], 0, 0, 0
    try:
        import glob
        for tf in glob.glob(str(TRACE_DIR / "trace-*.jsonl")):
            for ln in open(tf, encoding="utf-8"):
                if RUN_TAG not in ln:
                    continue
                d = json.loads(ln)
                # 口径：ok/cancelled/cache_hit 都计入（评测器读到 done 即断开，
                # 服务端记 cancelled；latency_ms 均有效）
                if d.get("outcome") in ("ok", "cancelled", "cache_hit") and d.get("latency_ms"):
                    lat.append(d["latency_ms"])
                    n += 1
                    if d.get("cache_hit"):
                        cache += 1
                    u = d.get("usage") or {}
                    usage_total += u.get("total") or 0
    except Exception as e:
        print(f"  trace 汇总失败: {e}")
    results["D5"] = {"trace_n": n, "latency_p50_ms": round(statistics.median(lat)) if lat else None,
                     "latency_p95_ms": round(sorted(lat)[int(len(lat) * 0.95) - 1]) if len(lat) > 1 else None,
                     "cache_hit_n": cache, "usage_total_tokens": usage_total or None,
                     "usage_note": (
                         "真实 token 数（SSE 流末帧 usage）"
                         if usage_total else
                         "provider 未返回 usage：兼容端点不支持 stream_options，成本需从平台账单取"
                     )}
    print(f"  trace_n={n} p50={results['D5']['latency_p50_ms']}ms "
          f"p95={results['D5']['latency_p95_ms']}ms cache={cache} usage={usage_total or '-'}")

    # ---------- D7 回答结果（端到端汇总） ----------
    print("--- D7 回答结果 ---")
    d2_items = results["D2"]
    d2_pass = sum(1 for i in d2_items if i["pass"])
    e2e = d2_pass + sum(1 for i in results["D3"] if i["pass"])
    e2e_total = len(d2_items) + len(results["D3"])
    integrity = sum(1 for i in d2_items if i["pass"])
    results["D7"] = {"e2e_pass": e2e, "e2e_total": e2e_total,
                     "e2e_rate": round(e2e / e2e_total, 3) if e2e_total else None,
                     "output_integrity": integrity,
                     "failed_layers": {}}
    for dim in ("D1", "D2", "D3", "D4", "D6"):
        for i in results[dim]:
            if not i["pass"]:
                L = i.get("failed_layer") or "unknown"
                results["D7"]["failed_layers"][L] = results["D7"]["failed_layers"].get(L, 0) + 1
    print(f"  端到端 {e2e}/{e2e_total}  失败分层 {results['D7']['failed_layers']}")

    # ---------- 汇总 ----------
    summary = {}
    for dim in ("D1", "D2", "D3", "D4", "D6"):
        items = results[dim]
        summary[dim] = f"{sum(1 for i in items if i['pass'])}/{len(items)}" if items else "-"
    if d1_cases and ranks:
        summary["D1_MRR"] = round(sum(1 / r for r in ranks) / len(ranks), 3)
        summary["D1_hit5"] = f"{len(ranks)}/{len(d1_cases)}"
    # D2 答案信息完整率：关键字全中率。与 D2 的 PASS 口径分开统计——
    # PASS 判的是「引用对不对」（cite_ok），这里是「答得全不全」，混在一起会互相稀释。
    d2_ans = [i for i in results["D2"] if i.get("answer_kw_ok") is not None]
    if d2_ans:
        ans_ok = sum(1 for i in d2_ans if i["answer_kw_ok"])
        summary["D2_答案完整率"] = f"{ans_ok}/{len(d2_ans)}"
    results["meta"]["summary"] = summary
    REPORT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"报告: {REPORT}")
    if do_cleanup:
        cleanup_run([uid_a, uid_b])


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="七维评测（支持分级跑）")
    ap.add_argument("--tier", choices=("d1", "safety", "full"), default="full",
                    help="d1=只跑检索层（秒级）；safety=边界安全权限（不跑 D2/D3）；full=全量")
    ap.add_argument("--cleanup", action="store_true", help="跑完软删本次创建的测试账号与订单")
    args = ap.parse_args()
    main(tier=args.tier, do_cleanup=args.cleanup)
