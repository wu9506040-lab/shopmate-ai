#!/usr/bin/env bash
# =============================================================================
# Java 中台 P1/P2 最小功能 E2E 冒烟（真实栈 + mock AI 确定性验证）
# 用法：bash java-platform/scripts/e2e_smoke.sh
# 前置：docker 栈在跑（MySQL 3307 + Python API 8000）；Java 平台 :8080 已启动
#       （AI_BASE_URL=http://127.0.0.1:18081 指向 mock）
# 输出：逐条 PASS/FAIL + 汇总
# =============================================================================
set -u
J="${JAVA_BASE_URL:-http://127.0.0.1:8081}"
P="${PY_BASE_URL:-http://127.0.0.1:8000}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-local123}"
PASS=0; FAIL=0; SKIP=0
skip() { echo "SKIP  $1（$2）"; SKIP=$((SKIP+1)); }

check() { # check <名称> <预期> <实际表达式退出码判定>
  local name="$1" expect="$2" actual="$3"
  if [ "$expect" = "$actual" ]; then echo "PASS  $name"; PASS=$((PASS+1));
  else echo "FAIL  $name  预期=[$expect] 实际=[$actual]"; FAIL=$((FAIL+1)); fi
}
has() { # has <名称> < haystack, needle>
  local name="$1" hay="$2" needle="$3"
  case "$hay" in *"$needle"*) echo "PASS  $name"; PASS=$((PASS+1));;
    *) echo "FAIL  $name  实际响应不含[$needle]：$hay"; FAIL=$((FAIL+1));; esac
}

echo "=== A. 鉴权（真实 MySQL，admin 真账号） ==="

R=$(curl -s -o /tmp/e2e_bad.json -w "%{http_code}" -X POST "$J/api/auth/login" \
  -d "username=$ADMIN_USER&password=wrong-pass")
check "A1 错误密码 → 401" "401" "$R"
has "A2 错误密码文案（与 Python 一致，防账号枚举）" "$(cat /tmp/e2e_bad.json)" "用户名或密码错误"

R=$(curl -s -D /tmp/e2e_login.h -o /tmp/e2e_login.json -w "%{http_code}" -X POST "$J/api/auth/login" \
  -d "username=$ADMIN_USER&password=$ADMIN_PASS")
check "A3 正确登录 → 200" "200" "$R"
has "A4 响应含 user.role=admin" "$(cat /tmp/e2e_login.json)" '"role":"admin"'
grep -qi "set-cookie: cs_token=" /tmp/e2e_login.h && { echo "PASS  A5 下发 httpOnly Cookie"; PASS=$((PASS+1)); } || { echo "FAIL  A5 未下发 cs_token Cookie"; FAIL=$((FAIL+1)); }
grep -qi "httponly" /tmp/e2e_login.h && { echo "PASS  A6 Cookie HttpOnly"; PASS=$((PASS+1)); } || { echo "FAIL  A6 Cookie 非 HttpOnly"; FAIL=$((FAIL+1)); }
TOKEN=$(grep -o '"id":[0-9]*' /tmp/e2e_login.json | head -1 | cut -d: -f2)

R=$(curl -s -o /tmp/e2e_me.json -w "%{http_code}" "$J/api/auth/me" -b /tmp/e2e_login.h)
check "A7 Cookie 调 /me → 200" "200" "$R"
has "A8 /me 返回 id 与用户名" "$(cat /tmp/e2e_me.json)" "\"username\":\"$ADMIN_USER\""

# 跨语言互认：Java 签发的 Cookie token 直接打 Python /auth/me
# （需要 Python AI 服务在线；本地 H2 模式下 Python 库里无此用户会 401，Docker 联调阶段验证）
if curl -s -m 3 -o /dev/null "$P/health"; then
  PYTOKEN=$(grep -io "cs_token=[^;]*" /tmp/e2e_login.h | head -1 | cut -d= -f2)
  R=$(curl -s -o /tmp/e2e_py.json -w "%{http_code}" "$P/api/auth/me" -H "Authorization: Bearer $PYTOKEN")
  check "A9 ★Java 签发 token 被 Python 接受（跨语言互认）" "200" "$R"
else
  skip "A9 跨语言互认" "Python 服务不在线（单元测试已用字节级向量等价证明，联调阶段复验）"
fi

R=$(curl -s -o /dev/null -w "%{http_code}" "$J/api/auth/me")
check "A10 无凭证 /me → 401" "401" "$R"

echo "=== B. 会话/消息（真实 MySQL 数据） ==="

R=$(curl -s -o /tmp/e2e_convs.json -w "%{http_code}" "$J/api/conversations" -b /tmp/e2e_login.h)
check "B1 会话列表 → 200" "200" "$R"
has "B2 响应含 conversations/total 键" "$(cat /tmp/e2e_convs.json)" '"total"'
SID=$(grep -o '"session_id":"[^"]*"' /tmp/e2e_convs.json | head -1 | cut -d'"' -f4)

R=$(curl -s -o /tmp/e2e_msgs.json -w "%{http_code}" "$J/api/conversations/$SID/messages?limit=5" -b /tmp/e2e_login.h)
check "B3 消息分页 → 200" "200" "$R"
has "B4 分页元数据 has_more/next_cursor/limit" "$(cat /tmp/e2e_msgs.json)" '"has_more"'
has "B5 消息含 human 标记字段" "$(cat /tmp/e2e_msgs.json)" '"human"'

R=$(curl -s -o /dev/null -w "%{http_code}" "$J/api/conversations/$SID/messages?limit=0" -b /tmp/e2e_login.h)
check "B6 边界：limit=0 → 422" "422" "$R"
R=$(curl -s -o /dev/null -w "%{http_code}" "$J/api/conversations/$SID/messages?limit=101" -b /tmp/e2e_login.h)
check "B7 边界：limit=101 → 422" "422" "$R"
R=$(curl -s -o /tmp/e2e_404.json -w "%{http_code}" "$J/api/conversations/nonexistent-session/messages" -b /tmp/e2e_login.h)
check "B8 边界：他人/不存在会话 → 404（不暴露存在性）" "404" "$R"
R=$(curl -s -o /dev/null -w "%{http_code}" "$J/api/conversations/$SID/messages?cursor=0" -b /tmp/e2e_login.h)
check "B9 边界：cursor=0 → 422" "422" "$R"

LONGT=$(python -c "print('字'*201)")
R=$(curl -s -o /dev/null -w "%{http_code}" -X PATCH "$J/api/conversations/$SID/title" \
  -H "Content-Type: application/json" -d "{\"title\":\"$LONGT\"}" -b /tmp/e2e_login.h)
check "B10 边界：标题 201 字符 → 422" "422" "$R"

echo "=== C. SSE 网关（mock AI，确定性断言） ==="

SSE_FILE=/tmp/e2e_sse.txt
curl -s -N -o "$SSE_FILE" -w "%{http_code}" -X POST "$J/api/chat" \
  -H "Content-Type: application/json" \
  -d '{"query":"测试透传","session_id":"e2e-sess"}' -b /tmp/e2e_login.h > /tmp/e2e_chat.code
check "C1 网关透传 → 200 SSE" "200" "$(cat /tmp/e2e_chat.code)"
# 顺序断言：meta 在 token 之前，done 收尾（事件协议完整性）
METALINE=$(grep -n '"type":"meta"' "$SSE_FILE" | head -1 | cut -d: -f1)
TOKENLINE=$(grep -n '"type":"token"' "$SSE_FILE" | head -1 | cut -d: -f1)
DONELINE=$(grep -n '"type":"done"' "$SSE_FILE" | head -1 | cut -d: -f1)
if [ -n "$METALINE" ] && [ -n "$TOKENLINE" ] && [ -n "$DONELINE" ] && [ "$METALINE" -lt "$TOKENLINE" ] && [ "$TOKENLINE" -lt "$DONELINE" ]; then
  echo "PASS  C2 事件顺序 meta→token→done"; PASS=$((PASS+1))
else
  echo "FAIL  C2 事件顺序异常 meta=$METALINE token=$TOKENLINE done=$DONELINE"; FAIL=$((FAIL+1))
fi
grep -q '"intent":"policy_query"' "$SSE_FILE" && { echo "PASS  C3 meta 内容透传无损"; PASS=$((PASS+1)); } || { echo "FAIL  C3 meta 内容丢失"; FAIL=$((FAIL+1)); }
grep -q '"session_id":"sess-1"' "$SSE_FILE" && { echo "PASS  C4 done 帧透传"; PASS=$((PASS+1)); } || { echo "FAIL  C4 done 帧丢失"; FAIL=$((FAIL+1)); }
# 注释行不应被透传成 data 帧（keep-alive 由网关自行处理）
grep -v '^:' "$SSE_FILE" | grep -q 'keep-alive' && { echo "FAIL  C5 注释行泄漏为数据"; FAIL=$((FAIL+1)); } || { echo "PASS  C5 keep-alive 注释被网关过滤"; PASS=$((PASS+1)); }

# 上游 401（mock 无 Bearer 拒绝）不会出现——网关必带 Bearer；反向验证：无登录态调网关
R=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$J/api/chat" \
  -H "Content-Type: application/json" -d '{"query":"hi"}')
check "C6 无登录态调网关 → 401（fail-closed）" "401" "$R"

echo "=== E. P3 落库（Java 单一写入者，mock 流 done 触发）==="

# C1 的 chat 已产生一个 done（session=sess-1，tokens: 你好/，/请问）
R=$(curl -s -o /tmp/e2e_convs2.json -w "%{http_code}" "$J/api/conversations" -b /tmp/e2e_login.h)
check "E1 会话列表 → 200" "200" "$R"
has "E2 done 后自动建会话 sess-1" "$(cat /tmp/e2e_convs2.json)" '"sess-1"'

R=$(curl -s -o /tmp/e2e_pmsg.json -w "%{http_code}" \
  "$J/api/conversations/sess-1/messages?limit=10" -b /tmp/e2e_login.h)
check "E3 落库消息可读 → 200" "200" "$R"
has "E4 assistant 回答为 token 拼接结果" "$(cat /tmp/e2e_pmsg.json)" "你好，请问"
has "E5 user 消息入库" "$(cat /tmp/e2e_pmsg.json)" '"role":"user"'
has "E6 human 标记存在（intent_snapshot）" "$(cat /tmp/e2e_pmsg.json)" '"human":false'

# 再次对话同一 session → message_count 递增（续聊不新建会话）
curl -s -N -o /dev/null -X POST "$J/api/chat" -H "Content-Type: application/json" \
  -d '{"query":"再问一次","session_id":"sess-1"}' -b /tmp/e2e_login.h
CNT=$(curl -s "$J/api/conversations/sess-1/messages?limit=20" -b /tmp/e2e_login.h \
  | grep -o '"role"' | wc -l | tr -d ' ')
check "E7 续聊追加消息（4 条）" "4" "$CNT"

# done 帧回填 assistant_message_id（前端按条评价契约）
grep -q '"assistant_message_id":[0-9]' "$SSE_FILE" && { echo "PASS  E8 done 帧回填 assistant_message_id"; PASS=$((PASS+1)); } || { echo "FAIL  E8 done 帧缺少 assistant_message_id"; FAIL=$((FAIL+1)); }

echo "=== D. 上游故障边界（AI 服务不可用） ==="
# 临时把 Java 指向关闭端口不可行（进程已启动）——改用 Python 侧不存在的路径场景：
# 直接对真实 Python 8000 的 /api/chat 透传（真实 LLM 链路，只验证连接成功 + 首个事件到达）
R=$(timeout 30 curl -s -N -o /tmp/e2e_real_sse.txt -w "%{http_code}" -X POST "$J/api/chat" \
  -H "Content-Type: application/json" \
  -d '{"query":"在吗"}' -b /tmp/e2e_login.h)
if curl -s -m 3 -o /dev/null "$P/health"; then
  check "D1 真实 Python /api/chat 经网关 → 200" "200" "$R"
  grep -q '"type"' /tmp/e2e_real_sse.txt && { echo "PASS  D2 真实链路有事件返回"; PASS=$((PASS+1)); } || { echo "WARN  D2 真实链路无事件（LLM 未配置/超时，人工复核）"; }
else
  skip "D1/D2 真实链路透传" "Python 服务不在线（C 组 mock 已确定性验证透传行为，联调阶段复验）"
fi

echo ""
echo "=== 汇总：PASS=$PASS FAIL=$FAIL SKIP=$SKIP ==="
[ "$FAIL" = "0" ] && echo "✅ 全部通过" || echo "❌ 存在失败，逐条排查"
exit $FAIL
