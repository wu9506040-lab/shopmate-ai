"""D2 关键词未命中诊断：拉出真实 LLM 回答全文，判断是「期望过时」还是「真答错」。

用途：七维评测里 D2 的 passed 只看 cite_ok，answer_keywords 不参与判定，
      全绿会掩盖答案层面的问题。本脚本把这层单独摊开看。
"""
import json
import sys
import time
import re
import unicodedata
import urllib.request
import urllib.error
import http.cookiejar
from pathlib import Path

BASE = "http://127.0.0.1:8000"
REPO = Path(__file__).resolve().parent.parent
EVALSET = REPO / "data/evalsets/evalset_v3_fashion.json"

# 这些 case 在最近一轮评测中 answer_kw_ok=False
TARGETS = ["P01", "P10", "P11"]


def _norm(s):
    """与 eval_seven.kw_normalize 保持同一套口径，否则诊断读数和评测对不上。"""
    s = unicodedata.normalize("NFKC", str(s))
    for a, b in (("–", "-"), ("—", "-"), ("~", "-"), (".", "-"), ("．", "-")):
        s = s.replace(a, b)
    return re.sub(r"\s+", "", s).lower()


def _hit(expect_item, answer):
    """期望项支持 A|B 等价说法，任一命中即算通过。"""
    na = _norm(answer)
    return any(_norm(a) in na for a in str(expect_item).split("|") if a.strip())


def chat_once(jar, query, session_id, timeout=90, _retried=False):
    body = json.dumps({"query": query, "session_id": session_id,
                       "sku": None, "order_no": None}).encode()
    r = urllib.request.Request(BASE + "/api/chat", data=body, headers={
        "Content-Type": "application/json", "Accept": "text/event-stream"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    answer, contexts, t0, st = "", [], time.perf_counter(), None
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
                            contexts.append(str(c.get("source") or c.get("doc_key") or ""))
                elif t == "done":
                    break
    except urllib.error.HTTPError as e:
        st = e.code
    if st == 429 and not _retried:
        time.sleep(62)
        return chat_once(jar, query, session_id, timeout, True)
    return st, answer, contexts, round((time.perf_counter() - t0) * 1000)


def main():
    cases = json.loads(EVALSET.read_text(encoding="utf-8"))["cases"]
    jar = http.cookiejar.CookieJar()
    ts = int(time.time())
    out = []
    for c in cases:
        if c.get("id") not in TARGETS:
            continue
        kw = c.get("expected_keywords") or []
        st, ans, ctxs, ms = chat_once(jar, c["query"], f"diag-{ts}-{c['id']}")
        hit = [k for k in kw if _hit(k, ans)]
        miss = [k for k in kw if not _hit(k, ans)]
        print("=" * 70)
        print(f"[{c['id']}] Q: {c['query']}")
        print(f"  期望词 {kw} -> 命中 {hit} / 未中 {miss}")
        print(f"  http={st} ms={ms} ans_len={len(ans)} cite={[x for x in ctxs][:2]}")
        print(f"  答案: {ans}")
        out.append({"id": c["id"], "query": c["query"], "expect": kw,
                    "hit": hit, "miss": miss, "answer": ans, "ms": ms})
        time.sleep(1)
    (REPO / "data" / "diag_d2_keywords.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n写出 data/diag_d2_keywords.json")


if __name__ == "__main__":
    sys.exit(main())
