#!/usr/bin/env python3
"""
Mock AI 服务（SSE）—— E2E 确定性验证用。

为什么不用真实 Python AI 服务做透传测试：
真实 /api/chat 会触发 LLM 调用（花钱、非确定、耗时 8-15s），
无法做「预计结果 vs 实际结果」的确定性断言。
本 mock 以与真实 /api/chat 相同的 SSE 事件协议输出脚本化事件流，
专测「Java 网关的透传行为」这一个变量。

事件序列（与真实协议一致的 meta → token → done 结构）：
  1. data: {"type":"meta","intent":"policy_query"}
  2. data: {"type":"token","text":"您好"}        (id: 1)
  3. data: {"type":"token","text":"，"}          (id: 2)
  4. : keep-alive 注释行                          （应被网关吞掉）
  5. data: {"type":"token","text":"请问"}
  6. data: {"type":"done","session_id":"sess-1"} (id: 3)
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/api/chat":
            self.send_response(404)
            self.end_headers()
            return
        # 读掉请求体（Content-Length）
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)

        # 鉴权边界：无 Authorization 头 → 401（验证网关会带 Bearer）
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"detail":"AI \xe6\x9c\xaa\xe6\x8e\x88\xe6\x9d\x83"}')
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        events = [
            (None, '{"type":"meta","intent":"policy_query"}'),
            ("1", '{"type":"token","text":"你好"}'),
            ("2", '{"type":"token","text":"，"}'),
            (None, "__COMMENT__"),  # 注释行标记
            ("3", '{"type":"token","text":"请问"}'),
            ("4", '{"type":"done","session_id":"sess-1"}'),
        ]
        for seq, data in events:
            if data == "__COMMENT__":
                self.wfile.write(b": keep-alive\n\n")
                continue
            if seq:
                self.wfile.write(f"id: {seq}\n".encode())
            self.wfile.write(f"data: {data}\n\n".encode())
            self.wfile.flush()

    def log_message(self, *args):
        pass  # 静默


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 18081), Handler).serve_forever()
