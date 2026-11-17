#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mock LLM 服务（OpenAI 兼容）—— 无真实模型额度时的测试替身。

定位：只替掉「大模型生成」这一环，Python 侧意图识别 / RAG 检索 /
Prompt 组装 / 流式透传 / 落库全部真实运行。

协议：POST /v1/chat/completions（同时支持 stream=true/false）

应答策略（2026-11 服饰域重写，修复 v1 的"模板与检索脱节"缺陷）：
  1. 解析最后一条 user message：按「问题：」拆出 query 与【参考资料】块；
     资料块按 [n] 编号还原各 chunk 原文
  2. query 关键词命中 chunk 行 → 拼装答案（保留 [n] 引用标记，可验证引用链路）
  3. 无命中行 → 取 top1 chunk 前几句（不编造资料外的数字）
  4. 无资料（闲聊/订单工具流）→ 服饰客服人设模板

启动：python scripts/mock_llm_server.py（默认 18082）
接入：deploy/docker-compose.mock.yml 把 api 的 DASHSCOPE_BASE_URL
      指向 http://host.docker.internal:18082/v1
"""
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------- 服饰域人设模板（无检索资料时使用） ----------

DEFAULT_REPLY = (
    "您好，我是猫与小鼠服饰客服[1]。可以帮您处理：\n"
    "- 尺码推荐（身高体重即可，宽松版型喜修身可小一码）\n"
    "- 退换货（七天无理由，2026.11 起商家承担运费）\n"
    "- 物流（现货 48 小时内发货，满 99 元包邮）\n"
    "请告诉我您想了解的具体问题~"
)

ORDER_REPLY = (
    "帮您查询订单了[1]。如需改地址或换码：发货前随时可改（30 分钟内处理）；"
    "已揽收在途不可改地址，可派送时联系快递员改约或拒收退回。"
)

GREETING_REPLY = "在的~ 有什么可以帮您？尺码、退换、物流都可以问我[1]。"

# 关键域词（答案拼装时的优先命中词，覆盖三套尺码口径/政策/洗护）
DOMAIN_KEYWORDS = [
    "尺码", "腰围", "臀围", "胸围", "衣长", "肩宽", "袖长", "裤长", "腿围",
    "号型", "身高", "体重", "公斤", "斤", "宽松", "修身", "版型", "平铺",
    "退", "换", "七天", "吊牌", "洗涤", "运费", "运费险", "包邮", "满减",
    "发货", "预售", "快递", "拦截", "起球", "色差", "开线", "缩水", "掉色",
    "羊毛", "聚酯", "氨纶", "锦纶", "加绒", "克重", "货号", "成分",
    "双11", "双十一", "618", "活动", "优惠", "赠品", "9 折", "9折",
]


def split_query_and_context(user_text: str):
    """从 RAG 组装的 user message 中拆出 query 与 [n] 编号的资料 chunks。

    支持两种真实格式：
      A) pipeline.py：基于以下内容回答问题：\\n[1] chunk\\n\\n[2] chunk ... 问题：{query}
      B) orchestrator：【政策依据】\\n[1]（文档名 · 类目 · 有效期） 内容\\n[2]（...） 内容
         ...\\n问题：{query}   ← 每行一个 chunk，续行并入当前 chunk
    """
    if not user_text:
        return "", []
    m = re.search(r"(?:^|\n)问题[:：]\s*(.+)\s*$", user_text, re.DOTALL)
    query = m.group(1).strip() if m else user_text
    # 资料块 = 第一个 [数字] 起到「问题：」/「对话历史」前的内容
    ctx_match = re.search(r"\[1\](.*?)(?=\n对话历史：|\n问题[:：])", user_text, re.DOTALL)
    chunks = []
    if ctx_match:
        raw = ctx_match.group(1)
        cur_no, buf = 1, []
        for ln in raw.splitlines():
            mm = re.match(r"^\[(\d+)\]\s*(.*)$", ln)
            if mm:
                if buf:
                    chunks.append((cur_no, "\n".join(buf).strip()))
                cur_no = int(mm.group(1))
                buf = [mm.group(2)] if mm.group(2).strip() else []
            elif ln.strip():
                buf.append(ln.strip())
        if buf:
            chunks.append((cur_no, "\n".join(buf).strip()))
    return query, chunks


def tokenize(query: str) -> list:
    """query → 关键词集合：域词表命中 + 中文 2-gram（过滤停用字）。"""
    toks = set()
    for kw in DOMAIN_KEYWORDS:
        if kw in query:
            toks.add(kw)
    # 数字单位（尺码数值类问题：126 / 260g / 5XL / 45-55 等）
    for num in re.findall(r"\d+(?:\.\d+)?\s*(?:cm|CM|g|G|kg|KG|斤|元|天|小时|折)?", query):
        n = num.strip()
        if len(n) >= 1:
            toks.add(n)
    # 中文 2-gram
    han = re.sub(r"[^\u4e00-\u9fa5]", " ", query)
    for seg in han.split():
        for i in range(len(seg) - 1):
            g = seg[i:i + 2]
            if g not in ("你们", "请问", "什么", "怎么", "可以", "是不是", "多少"):
                toks.add(g)
    return toks


def score_line(line: str, toks: set) -> int:
    return sum(1 for t in toks if t in line)


def build_answer(query: str, chunks: list) -> str:
    """按检索资料拼装答案：命中块 + [n] 引用。绝不输出资料外的数字。

    表格感知：markdown 表格（连续 | 开头行）作为整体参与打分——
    语义在表头（"胸围"）、数值在数据行（"126"），必须联合命中。
    """
    if not chunks:
        return DEFAULT_REPLY
    toks = tokenize(query)
    if not toks:
        toks = set(DOMAIN_KEYWORDS[:6])
    picked = {}  # (chunk_no, block_text) -> score
    for no, content in chunks:
        lines = [ln.strip() for ln in content.split("\n") if ln.strip()]
        blocks, cur_table = [], []
        for ln in lines:
            if ln.startswith("|"):
                cur_table.append(ln)
            else:
                if cur_table:
                    blocks.append("\n".join(cur_table))
                    cur_table = []
                blocks.append(ln)
        if cur_table:
            blocks.append("\n".join(cur_table))
        for blk in blocks:
            if len(blk) < 6:  # 跳过标题/孤立符号行
                continue
            s = score_line(blk, toks)
            if s > 0:
                picked[(no, blk)] = s
    if not picked:
        # 无命中：取 top1 chunk 前 3 行（忠实转述，不编造）
        no, content = chunks[0]
        lines = [ln.strip() for ln in content.split("\n") if ln.strip()][:3]
        body = "\n".join(lines)
        return f"根据资料[{no}]：\n{body}"
    # 按分数排序，取前 5 块；表格块整体保留（表头+数值行联合语义）
    ranked = sorted(picked.items(), key=lambda kv: -kv[1])[:5]
    seen, ordered = set(), []
    for (no, blk), _ in ranked:
        key = blk[:40]
        if key in seen:
            continue
        seen.add(key)
        if blk.startswith("|"):
            ordered.append(f"[{no}]\n{blk}")
        else:
            ordered.append(f"- {blk} [{no}]")
    head = f"根据知识库资料回答「{query[:20]}」：\n" if len(query) > 6 else ""
    return head + "\n".join(ordered)


def _classify_json(query: str, multi: bool) -> str:
    """意图分类 prompt → 严格 JSON（让 LLM 兜底链路真实可测，不落 default）。"""
    q = query.strip()
    if re.search(r"在吗|你好|您好|在不在|hi|hello", q, re.IGNORECASE):
        intent = "chitchat"
    elif re.search(r"我要退款|我要退货|我要换货|我要换码|帮我换码|申请换码|想退|能退吗|不想要了", q):
        intent = "refund_query"
    elif re.search(r"我的订单|查.*订单|订单状态|物流|快递|到哪|签收|发货|运单", q):
        intent = "order_query"
    elif re.search(r"多少钱|价格|参数|规格|尺码|胸围|腰围|克重|货号|成分|版型|颜色", q):
        intent = "product_query"
    else:
        intent = "policy_query"
    if multi:
        return json.dumps({"intents": [{"intent": intent, "confidence": 0.85}]}, ensure_ascii=False)
    return json.dumps({"intent": intent, "confidence": 0.85}, ensure_ascii=False)


def pick_reply(messages: list) -> str:
    raw = last_user_text(messages)
    # 意图分类 prompt（intent_service._llm_classify / _llm_classify_multi）
    # 必须优先识别：否则分类请求会走客服话术模板 → 解析失败 → fallback default
    if "输出严格 JSON" in raw or "只输出 JSON" in raw:
        qm = re.search(r"Q[：:]\s*(.+?)\s*$", raw, re.DOTALL)
        return _classify_json(qm.group(1) if qm else raw, "intents" in raw)
    query, chunks = split_query_and_context(raw)
    if not query:
        return DEFAULT_REPLY
    q = query.strip()
    if not chunks:
        # 无检索资料：闲聊/订单工具流模板
        if re.search(r"订单|发货|物流|快递|到哪|多久", q):
            return ORDER_REPLY
        if re.search(r"你好|您好|在吗|在不在|hi|hello|hey", q, re.IGNORECASE):
            return GREETING_REPLY
        return DEFAULT_REPLY
    return build_answer(query, chunks)


def last_user_text(messages: list) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
    return ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": {"message": "bad json"}})
            return

        messages = body.get("messages") or []
        stream = bool(body.get("stream"))
        if os.environ.get("MOCK_DEBUG"):
            try:
                with open(os.environ.get("MOCK_DEBUG_FILE", r"E:\智能客服\data\shop_raw\mock_last_messages.json"),
                          "w", encoding="utf-8") as _f:
                    json.dump([{"role": m.get("role"), "content": (m.get("content") or "")[:4000]}
                               for m in messages], _f, ensure_ascii=False, indent=1)
            except Exception:
                pass
        reply = pick_reply(messages)

        if not stream:
            self._json(200, {
                "id": "mock-chat-1", "object": "chat.completion",
                "model": body.get("model", "mock"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": reply}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": len(reply),
                          "total_tokens": 100 + len(reply)},
            })
            return

        # 流式：按小段吐出（模拟真实 token 流，验证 SSE 逐帧转发）
        # 注意：HTTP/1.1 无 Content-Length 的响应必须用 Transfer-Encoding: chunked，
        # 否则客户端（httpx/openai SDK）会一直等长度 → ReadTimeout（实测踩坑）
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(payload: bytes) -> None:
            self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
            self.wfile.flush()

        try:
            for i in range(0, len(reply), 6):
                piece = {"id": "mock-chat-1", "object": "chat.completion.chunk",
                         "model": body.get("model", "mock"),
                         "choices": [{"index": 0, "delta": {"content": reply[i:i + 6]},
                                      "finish_reason": None}]}
                chunk(f"data: {json.dumps(piece, ensure_ascii=False)}\n\n".encode())
            done = {"id": "mock-chat-1", "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            chunk(f"data: {json.dumps(done, ensure_ascii=False)}\n\n".encode())
            chunk(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")   # chunked 终止块
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv[1:]) >= 1 else 18082
    print(f"mock LLM listening on 127.0.0.1:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
