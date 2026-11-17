#!/usr/bin/env bash
# =============================================================================
# 坐席工单 E2E（经 nginx 灰度路径 /api/admin/handoff → Java 中台）
# 前置：docker 栈运行 + 已插入 fixture 工单 HFT-E2E000001（见 tmp/handoff_fixture.sql）
# 用法：bash java-platform/scripts/e2e_handoff.sh
# =============================================================================
set -u
J="${BASE_URL:-http://localhost:5173}"
USER_NAME="${ADMIN_USER:-admin}"
PASS="${ADMIN_PASS:-admin123}"
JAR=/tmp/e2e_handoff_jar.txt
PASS_CNT=0; FAIL_CNT=0

check() { local name="$1" expect="$2" actual="$3";
  if [ "$expect" = "$actual" ]; then echo "PASS  $name"; PASS_CNT=$((PASS_CNT+1));
  else echo "FAIL  $name  预期=[$expect] 实际=[$actual]"; FAIL_CNT=$((FAIL_CNT+1)); fi; }
has() { local name="$1" hay="$2" needle="$3";
  case "$hay" in *"$needle"*) echo "PASS  $name"; PASS_CNT=$((PASS_CNT+1));;
    *) echo "FAIL  $name  不含[$needle]"; FAIL_CNT=$((FAIL_CNT+1));; esac; }

rm -f "$JAR"
R=$(curl -s -c "$JAR" -o /dev/null -w "%{http_code}" -X POST "$J/api/auth/login" \
  -d "username=$USER_NAME&password=$PASS")
check "H1 管理员登录" "200" "$R"

R=$(curl -s -b "$JAR" -o /tmp/h_list.json -w "%{http_code}" "$J/api/admin/handoff/tickets?page=1&page_size=20")
check "H2 工单队列 → 200" "200" "$R"
has "H3 队列含 fixture 工单" "$(cat /tmp/h_list.json)" "HFT-E2E000001"
has "H4 队列含分页元数据" "$(cat /tmp/h_list.json)" '"page_size"'

R=$(curl -s -b "$JAR" -o /tmp/h_detail.json -w "%{http_code}" "$J/api/admin/handoff/tickets/26")
check "H5 工单详情 → 200" "200" "$R"
has "H6 详情含会话最近消息" "$(cat /tmp/h_detail.json)" '"messages"'
has "H7 详情含 summary 快照" "$(cat /tmp/h_detail.json)" '"note"'

R=$(curl -s -b "$JAR" -o /tmp/h_take.json -w "%{http_code}" -X POST "$J/api/admin/handoff/tickets/26/take")
check "H8 认领工单 → 200" "200" "$R"
has "H9 认领后状态 taken" "$(cat /tmp/h_take.json)" '"status":"taken"'
has "H10 认领人记录为 admin" "$(cat /tmp/h_take.json)" '"assignee":"admin"'

R=$(curl -s -b "$JAR" -o /tmp/h_take2.json -w "%{http_code}" -X POST "$J/api/admin/handoff/tickets/26/take")
check "H11 重复认领 → 409（状态机保护）" "409" "$R"

R=$(curl -s -b "$JAR" -o /tmp/h_resolve.json -w "%{http_code}" -X POST "$J/api/admin/handoff/tickets/26/resolve" \
  -H "Content-Type: application/json" -d '{"reply":"已为您处理退款，请注意查收。"}')
check "H12 回复结单 → 200" "200" "$R"
has "H13 结单状态 resolved" "$(cat /tmp/h_resolve.json)" '"status":"resolved"'

# 人工回复已注入会话（细节回读：最后一条为 assistant 且标记 human_agent）
R=$(curl -s -b "$JAR" -o /tmp/h_detail2.json -w "%{http_code}" "$J/api/admin/handoff/tickets/26")
check "H14 结单后详情 → 200" "200" "$R"
has "H15 人工回复进入会话时间线" "$(cat /tmp/h_detail2.json)" "已为您处理退款"
has "H16 人工消息带 human_agent 标记" "$(cat /tmp/h_detail2.json)" '"is_human_agent":true'

# 关闭（fixture 已 resolved，关闭语义仍生效：归档）
R=$(curl -s -b "$JAR" -o /tmp/h_close.json -w "%{http_code}" -X POST "$J/api/admin/handoff/tickets/26/close")
check "H17 关闭归档 → 200" "200" "$R"
has "H18 关闭后状态 closed" "$(cat /tmp/h_close.json)" '"status":"closed"'

R=$(curl -s -b "$JAR" -o /dev/null -w "%{http_code}" "$J/api/admin/handoff/tickets/999999")
check "H19 不存在工单 → 404" "404" "$R"

R=$(curl -s -o /dev/null -w "%{http_code}" "$J/api/admin/handoff/tickets")
check "H20 未登录 → 401（fail-closed）" "401" "$R"

echo ""
echo "=== 汇总：PASS=$PASS_CNT FAIL=$FAIL_CNT ==="
[ "$FAIL_CNT" = "0" ] && echo "✅ 工单链路全部通过" || echo "❌ 存在失败"
exit $FAIL_CNT
