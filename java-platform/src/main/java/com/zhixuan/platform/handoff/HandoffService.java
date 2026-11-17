package com.zhixuan.platform.handoff;

import java.sql.Timestamp;
import java.time.Duration;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zhixuan.platform.auth.UserPrincipal;
import com.zhixuan.platform.common.ApiException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 人工介入工单（坐席工作台）。
 *
 * 2026-11 业务规则重构：
 * - 回复与结单拆开：坐席可以连续多轮回复，工单保持 taken；
 *   只有显式「处理完成，结单」才结束，不再出现「回一句话就关单」。
 * - 用户 2 分钟无回复 → 系统自动结单（auto_closed=1），坐席不用守着。
 *   若期间用户又发消息，超时窗口重置，等坐席下一轮回复。
 * - 转人工原因可读化：reason_detail 随工单落库并在列表/详情展示，
 *   坐席一眼看清「为什么转人工」以及触发依据。
 *
 * 状态机：pending → taken（认领）→ [多轮 reply] → resolved（结单/自动结单）→ closed（归档）
 */
@Service
public class HandoffService {

    private static final Logger log = LoggerFactory.getLogger(HandoffService.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    /** P0 最前 */
    private static final Map<String, Integer> PRIORITY_ORDER = Map.of("P0", 0, "P1", 1, "P2", 2);
    /** 用户超时未回复自动结单窗口（秒） */
    private static final long AUTO_CLOSE_SECONDS = 120;

    private static final Map<String, String> REASON_LABELS = Map.of(
            "user_requested", "用户主动要求人工",
            "agent_unavailable", "AI 服务异常，自动兜底",
            "business_rule", "业务规则需人工判定",
            "order_modify", "发货后改地址/电话",
            "low_confidence", "AI 置信度不足",
            "no_kb_hit", "知识库未命中",
            "manual", "坐席手动创建");

    private final JdbcTemplate jdbc;

    public HandoffService(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    // ------------------------------------------------------------------
    // 队列
    // ------------------------------------------------------------------

    public Map<String, Object> list(String status, int page, int pageSize) {
        StringBuilder sql = new StringBuilder("""
                SELECT id, ticket_no, session_id, user_id, reason, reason_detail, category, priority,
                       matched_keyword, status, assignee, create_time, update_time,
                       last_agent_reply_at, auto_closed
                FROM handoff_tickets
                WHERE deleted = 0
                """);
        List<Object> args = new ArrayList<>();
        if (status != null && !status.isBlank()) {
            sql.append(" AND status = ?");
            args.add(status);
        }
        sql.append(" ORDER BY id DESC LIMIT 500");
        List<Map<String, Object>> rows = jdbc.queryForList(sql.toString(), args.toArray());

        rows.sort(Comparator
                .comparingInt((Map<String, Object> r) ->
                        PRIORITY_ORDER.getOrDefault((String) r.get("priority"), 9))
                .thenComparing(Comparator.comparingLong(
                        (Map<String, Object> r) -> ((Number) r.get("id")).longValue()).reversed()));

        int total = rows.size();
        int from = Math.max(0, (Math.max(page, 1) - 1) * pageSize);
        int to = Math.min(total, from + pageSize);
        List<Map<String, Object>> items = new ArrayList<>();
        for (int i = from; i < to; i++) {
            items.add(brief(rows.get(i)));
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("total", total);
        resp.put("page", page);
        resp.put("page_size", pageSize);
        resp.put("items", items);
        return resp;
    }

    // ------------------------------------------------------------------
    // 详情（工单 + 原因 + 会话时间线 + 自动结单倒计时）
    // ------------------------------------------------------------------

    public Map<String, Object> detail(long ticketId) {
        Map<String, Object> t = loadTicket(ticketId);
        Map<String, Object> detail = brief(t);
        detail.put("summary", jsonOrNull(t.get("summary")));
        detail.put("reply", t.get("reply"));

        LocalDateTime lastReply = toLocalDateTime(t.get("last_agent_reply_at"));
        detail.put("last_agent_reply_at", lastReply);
        detail.put("auto_close_seconds", AUTO_CLOSE_SECONDS);
        detail.put("auto_close_at", lastReply == null || "resolved".equals(t.get("status"))
                ? null : lastReply.plusSeconds(AUTO_CLOSE_SECONDS));
        detail.put("auto_close_remaining_sec", remainingAutoClose(t));

        List<Map<String, Object>> messages = new ArrayList<>();
        String sessionId = (String) t.get("session_id");
        if (sessionId != null && !sessionId.isBlank()) {
            List<Map<String, Object>> rows = jdbc.queryForList("""
                    SELECT id, role, content, contexts, create_time
                    FROM messages
                    WHERE session_id = ? AND deleted = 0
                    ORDER BY id DESC LIMIT 50
                    """, sessionId);
            for (int i = rows.size() - 1; i >= 0; i--) {
                Map<String, Object> m = rows.get(i);
                Map<String, Object> item = new LinkedHashMap<>();
                item.put("id", ((Number) m.get("id")).longValue());
                item.put("role", m.get("role"));
                item.put("content", m.get("content"));
                item.put("create_time", toLocalDateTime(m.get("create_time")));
                item.put("is_human_agent", isHumanAgentMessage(m.get("contexts")));
                messages.add(item);
            }
        }
        detail.put("messages", messages);
        return detail;
    }

    /** 距离自动结单还剩多少秒；不适用返回 null */
    private Long remainingAutoClose(Map<String, Object> t) {
        if (!"taken".equals(t.get("status"))) {
            return null;
        }
        LocalDateTime last = toLocalDateTime(t.get("last_agent_reply_at"));
        if (last == null) {
            return null;
        }
        long elapsed = Duration.between(last, LocalDateTime.now()).getSeconds();
        return Math.max(0, AUTO_CLOSE_SECONDS - elapsed);
    }

    // ------------------------------------------------------------------
    // 认领 / 多轮回复 / 结单 / 关闭
    // ------------------------------------------------------------------

    public Map<String, Object> take(long ticketId, UserPrincipal admin) {
        Map<String, Object> t = loadTicket(ticketId);
        String status = (String) t.get("status");
        if (!"pending".equals(status)) {
            throw new ApiException(409,
                    "工单状态为 " + status + "，已被 "
                            + (t.get("assignee") == null ? "—" : t.get("assignee")) + " 处理");
        }
        int updated = jdbc.update("""
                UPDATE handoff_tickets SET status = 'taken', assignee = ?
                WHERE id = ? AND status = 'pending' AND deleted = 0
                """, admin.username(), ticketId);
        if (updated == 0) {
            throw new ApiException(409, "工单已被其他坐席认领");
        }
        return okResponse(loadTicket(ticketId));
    }

    /**
     * 发送一条人工回复（不结单）。工单保持 taken，可继续多轮沟通；
     * 同时刷新自动结单计时窗口。
     */
    @Transactional
    public Map<String, Object> reply(long ticketId, UserPrincipal admin, String text) {
        Map<String, Object> t = loadTicket(ticketId);
        String status = (String) t.get("status");
        if ("resolved".equals(status) || "closed".equals(status)) {
            throw new ApiException(409, "工单已结束，不能再回复");
        }
        if (!"taken".equals(status)) {
            throw new ApiException(409, "请先认领工单再回复");
        }
        String normalized = normalize(text);
        if (normalized == null) {
            throw new ApiException(400, "回复内容不能为空");
        }
        appendAgentMessage(t, admin, normalized);
        jdbc.update("""
                UPDATE handoff_tickets
                SET last_agent_reply_at = CURRENT_TIMESTAMP,
                    assignee = COALESCE(assignee, ?), auto_closed = 0
                WHERE id = ? AND deleted = 0
                """, admin.username(), ticketId);
        log.info("handoff reply: id={} by={} (status stays taken)", ticketId, admin.username());
        return okResponse(loadTicket(ticketId));
    }

    /**
     * 处理完成，结单。reply 可选：填了就先追加最后一条回复再结单。
     * 与 reply() 的区别是这里显式结束工单，不会被当成「回一句就关单」。
     */
    @Transactional
    public Map<String, Object> resolve(long ticketId, UserPrincipal admin, String reply) {
        Map<String, Object> t = loadTicket(ticketId);
        if ("resolved".equals(t.get("status"))) {
            throw new ApiException(409, "工单已结单");
        }
        String normalized = normalize(reply);
        if (normalized != null) {
            appendAgentMessage(t, admin, normalized);
        }
        jdbc.update("""
                UPDATE handoff_tickets
                SET status = 'resolved', reply = COALESCE(?, reply),
                    assignee = COALESCE(assignee, ?), auto_closed = 0
                WHERE id = ?
                """, normalized, admin.username(), ticketId);
        log.info("handoff resolved: id={} by={}", ticketId, admin.username());
        return okResponse(loadTicket(ticketId));
    }

    public Map<String, Object> close(long ticketId, UserPrincipal admin) {
        loadTicket(ticketId);
        jdbc.update("""
                UPDATE handoff_tickets
                SET status = 'closed', assignee = COALESCE(assignee, ?)
                WHERE id = ? AND deleted = 0
                """, admin.username(), ticketId);
        return okResponse(loadTicket(ticketId));
    }

    /**
     * 用户 2 分钟未回复 → 自动结单。
     *
     * 为什么用 Java 计时而不是 SQL 方言：本地 H2 profile 与运行库 MySQL 的
     * 日期函数不兼容，统一在应用层比较可保证两套环境行为一致。
     * 每 15s 扫描一次 taken 工单，量级很小（本地演示几十张）。
     */
    @Scheduled(fixedDelay = 15000)
    @Transactional
    public void autoCloseExpired() {
        List<Map<String, Object>> rows;
        try {
            rows = jdbc.queryForList("""
                    SELECT id, ticket_no, session_id, last_agent_reply_at
                    FROM handoff_tickets
                    WHERE deleted = 0 AND status = 'taken'
                      AND last_agent_reply_at IS NOT NULL
                    """);
        } catch (Exception e) {
            log.debug("autoCloseExpired 跳过（表结构可能尚未迁移）: {}", e.getMessage());
            return;
        }
        LocalDateTime now = LocalDateTime.now();
        for (Map<String, Object> r : rows) {
            LocalDateTime last = toLocalDateTime(r.get("last_agent_reply_at"));
            if (last == null || Duration.between(last, now).getSeconds() < AUTO_CLOSE_SECONDS) {
                continue;
            }
            long id = ((Number) r.get("id")).longValue();
            // 用户在这之后又发了消息 → 超时窗口重置，等坐席下一轮回复
            Integer newerUserMsgs = jdbc.queryForObject("""
                    SELECT COUNT(*) FROM messages
                    WHERE session_id = ? AND role = 'user' AND deleted = 0
                      AND create_time > ?
                    """, Integer.class, r.get("session_id"), Timestamp.valueOf(last));
            if (newerUserMsgs != null && newerUserMsgs > 0) {
                jdbc.update("UPDATE handoff_tickets SET last_agent_reply_at = NULL WHERE id = ?", id);
                continue;
            }
            jdbc.update("""
                    UPDATE handoff_tickets
                    SET status = 'resolved', auto_closed = 1,
                        reply = '用户 2 分钟未回复，系统自动结单'
                    WHERE id = ? AND status = 'taken' AND deleted = 0
                    """, id);
            log.info("handoff auto-closed (user idle 2min): id={}", id);
        }
    }

    // ------------------------------------------------------------------

    private void appendAgentMessage(Map<String, Object> t, UserPrincipal admin, String text) {
        Object userId = t.get("user_id");
        String sessionId = (String) t.get("session_id");
        // V26 边界：user_id/session_id 缺失（异常来源的兜底工单可能没有）时
        // 跳过注入而不是 NPE → 500；工单本身的流转不受影响
        if (userId == null || sessionId == null || sessionId.isBlank()) {
            log.warn("appendAgentMessage 跳过：ticket={} 缺少 user_id/session_id", t.get("id"));
            return;
        }
        // 人工回复注入会话：assistant 行 + human_agent_ticket 标记
        String contexts = writeJson(Map.of(
                "human_agent_ticket", t.get("ticket_no"),
                "agent_name", admin.username()));
        jdbc.update("""
                INSERT INTO messages (session_id, user_id, role, content, contexts)
                VALUES (?, ?, 'assistant', ?, ?)
                """, sessionId, ((Number) userId).longValue(), text, contexts);
    }

    private static String normalize(String s) {
        if (s == null || s.isBlank()) {
            return null;
        }
        String v = s.trim();
        return v.length() > 2000 ? v.substring(0, 2000) : v;
    }

    private Map<String, Object> loadTicket(long ticketId) {
        List<Map<String, Object>> rows = jdbc.queryForList("""
                SELECT id, ticket_no, session_id, user_id, reason, reason_detail, category, priority,
                       matched_keyword, status, assignee, summary, reply, create_time, update_time,
                       last_agent_reply_at, auto_closed
                FROM handoff_tickets
                WHERE id = ? AND deleted = 0
                """, ticketId);
        if (rows.isEmpty()) {
            throw new ApiException(404, "工单不存在");
        }
        return rows.get(0);
    }

    private static Map<String, Object> brief(Map<String, Object> t) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", ((Number) t.get("id")).longValue());
        m.put("ticket_no", t.get("ticket_no"));
        m.put("session_id", t.get("session_id"));
        m.put("user_id", ((Number) t.get("user_id")).longValue());
        m.put("reason", t.get("reason"));
        m.put("reason_label", REASON_LABELS.getOrDefault(
                String.valueOf(t.get("reason")), "转人工"));
        m.put("reason_detail", t.get("reason_detail"));
        m.put("category", t.get("category"));
        m.put("priority", t.get("priority"));
        m.put("matched_keyword", t.get("matched_keyword"));
        m.put("status", t.get("status"));
        m.put("assignee", t.get("assignee"));
        m.put("auto_closed", t.get("auto_closed"));
        m.put("last_agent_reply_at", toLocalDateTime(t.get("last_agent_reply_at")));
        m.put("create_time", toLocalDateTime(t.get("create_time")));
        m.put("update_time", toLocalDateTime(t.get("update_time")));
        return m;
    }

    private static Map<String, Object> okResponse(Map<String, Object> ticket) {
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("ok", true);
        resp.put("ticket", brief(ticket));
        return resp;
    }

    private static boolean isHumanAgentMessage(Object contexts) {
        if (contexts == null) {
            return false;
        }
        try {
            JsonNode node = MAPPER.readTree((String) contexts);
            return node.hasNonNull("human_agent_ticket");
        } catch (Exception e) {
            return false;
        }
    }

    private static JsonNode jsonOrNull(Object v) {
        if (v == null) {
            return null;
        }
        try {
            return MAPPER.readTree((String) v);
        } catch (Exception e) {
            return null;
        }
    }

    /**
     * JDBC 时间对象 → LocalDateTime。
     * 为什么兼容三种类型：MySQL Connector/J 9 对 DATETIME 默认返回 LocalDateTime
     * （旧驱动返回 Timestamp），H2 返回 Timestamp —— 只认一种就会在另一套环境
     * 静默变 null（2026-11-15 实测：工单时间字段全空 + 自动结单不触发）。
     */
    private static LocalDateTime toLocalDateTime(Object v) {
        if (v instanceof LocalDateTime dt) {
            return dt;
        }
        if (v instanceof Timestamp ts) {
            return ts.toLocalDateTime();
        }
        if (v instanceof OffsetDateTime odt) {
            return odt.toLocalDateTime();
        }
        return null;
    }

    private static String writeJson(Map<String, Object> map) {
        try {
            return MAPPER.writeValueAsString(map);
        } catch (Exception e) {
            return null;
        }
    }
}
