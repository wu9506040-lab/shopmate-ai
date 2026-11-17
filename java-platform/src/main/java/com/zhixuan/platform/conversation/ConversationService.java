package com.zhixuan.platform.conversation;

import java.sql.Timestamp;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zhixuan.platform.auth.UserPrincipal;
import com.zhixuan.platform.common.ApiException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 会话/消息查询 —— SQL 语义逐条对齐 Python api/conversations.py：
 * - 列表：last_message_at DESC + 单 SQL 批量取每会话最后一条消息（GROUP BY MAX(id)）
 * - 消息：所有权 404 → id DESC cursor 分页（fetch limit+1 判 has_more）
 * - 删除：单事务软删 conversation + messages（一致性）
 *
 * 为什么用 JdbcTemplate 显式 SQL 而不是 JPA：表结构真源在 Python SQLAlchemy，
 * JPA 会引入第二份 ORM 映射，字段漂移风险翻倍；显式 SQL 可与 Python 逐行比对。
 */
@Service
public class ConversationService {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final JdbcTemplate jdbc;

    public ConversationService(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    // ---------------------------------------------------------------
    // 会话列表
    // ---------------------------------------------------------------

    private static final String LIST_SQL = """
            SELECT session_id, title, message_count, last_message_at
            FROM conversations
            WHERE user_id = ? AND deleted = 0
            ORDER BY last_message_at DESC
            """;

    public Map<String, Object> listConversations(UserPrincipal user) {
        List<Map<String, Object>> rows = jdbc.queryForList(LIST_SQL, user.id());
        if (rows.isEmpty()) {
            return Map.of("conversations", List.of(), "total", 0);
        }

        // 批量取每会话最后一条消息（复刻 Python 的 GROUP BY MAX(id) JOIN 优化，防 N+1）
        List<String> sessionIds = rows.stream().map(r -> (String) r.get("session_id")).toList();
        String placeholders = String.join(",", java.util.Collections.nCopies(sessionIds.size(), "?"));
        List<Object> args = new ArrayList<>(sessionIds);
        String lastMsgSql = """
                SELECT m.session_id AS sid, m.content AS content
                FROM messages m
                JOIN (
                    SELECT session_id AS sid, MAX(id) AS max_id
                    FROM messages
                    WHERE session_id IN (%s) AND deleted = 0
                    GROUP BY session_id
                ) t ON m.session_id = t.sid AND m.id = t.max_id
                """.formatted(placeholders);
        Map<String, String> lastMsgMap = new LinkedHashMap<>();
        for (Map<String, Object> r : jdbc.queryForList(lastMsgSql, args.toArray())) {
            lastMsgMap.put((String) r.get("sid"), (String) r.get("content"));
        }

        List<Map<String, Object>> items = new ArrayList<>();
        for (Map<String, Object> r : rows) {
            String sid = (String) r.get("session_id");
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("session_id", sid);
            item.put("title", r.get("title"));
            item.put("last_message", lastMsgMap.get(sid));
            item.put("updated_at", toLocalDateTime(r.get("last_message_at")));
            item.put("message_count", r.get("message_count") == null ? 0
                    : ((Number) r.get("message_count")).intValue());
            items.add(item);
        }
        return Map.of("conversations", items, "total", items.size());
    }

    // ---------------------------------------------------------------
    // 消息分页（cursor 向上翻页）
    // ---------------------------------------------------------------

    public Map<String, Object> getMessages(UserPrincipal user, String sessionId,
                                           int limit, Long cursor) {
        // 1. 所有权校验：非本人会话一律 404（不暴露存在性，防越权枚举）
        requireOwnership(user, sessionId);

        // 2. fetch limit+1 判 has_more
        StringBuilder sql = new StringBuilder("""
                SELECT id, role, content, contexts, scores, intent_snapshot, create_time
                FROM messages
                WHERE session_id = ? AND deleted = 0
                """);
        List<Object> args = new ArrayList<>();
        args.add(sessionId);
        if (cursor != null) {
            sql.append(" AND id < ?");
            args.add(cursor);
        }
        sql.append(" ORDER BY id DESC LIMIT ?");
        args.add(limit + 1);

        List<Map<String, Object>> rows = jdbc.queryForList(sql.toString(), args.toArray());
        boolean hasMore = rows.size() > limit;
        List<Map<String, Object>> page = hasMore ? rows.subList(0, limit) : rows;

        List<Map<String, Object>> items = new ArrayList<>();
        Long nextCursor = null;
        for (int i = 0; i < page.size(); i++) {
            Map<String, Object> r = page.get(i);
            items.add(messageItem(r));
            if (hasMore && i == page.size() - 1) {
                nextCursor = ((Number) r.get("id")).longValue();
            }
        }

        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("session_id", sessionId);
        resp.put("messages", items);
        resp.put("has_more", hasMore);
        resp.put("next_cursor", nextCursor);
        resp.put("limit", limit);
        return resp;
    }

    // ---------------------------------------------------------------
    // 软删除（单事务）与改标题
    // ---------------------------------------------------------------

    @Transactional
    public Map<String, Object> deleteConversation(UserPrincipal user, String sessionId) {
        requireOwnership(user, sessionId);
        jdbc.update("UPDATE conversations SET deleted = 1 WHERE session_id = ?", sessionId);
        jdbc.update("UPDATE messages SET deleted = 1 WHERE session_id = ? AND deleted = 0",
                sessionId);
        // Redis 缓存清理是 Python 侧职责（best-effort），平台层通过后续接入
        // 的缓存失效接口处理；P1 阶段接受 Redis 残留（读路径有 MySQL 兜底）
        return Map.of("session_id", sessionId, "message", "已删除");
    }

    public Map<String, Object> updateTitle(UserPrincipal user, String sessionId, String title) {
        if (title == null || title.isBlank() || title.length() > 200) {
            throw new ApiException(422, "title: 标题长度必须为 1-200 字符");
        }
        requireOwnership(user, sessionId);
        int updated = jdbc.update(
                "UPDATE conversations SET title = ? WHERE session_id = ? AND deleted = 0",
                title, sessionId);
        if (updated == 0) {
            throw new ApiException(404, "会话不存在或无权访问");
        }
        return Map.of("session_id", sessionId, "title", title, "message", "标题已更新");
    }

    // ---------------------------------------------------------------

    private void requireOwnership(UserPrincipal user, String sessionId) {
        Integer count = jdbc.queryForObject(
                "SELECT COUNT(*) FROM conversations WHERE session_id = ? AND user_id = ? AND deleted = 0",
                Integer.class, sessionId, user.id());
        if (count == null || count == 0) {
            throw new ApiException(404, "会话不存在或无权访问");
        }
    }

    private static Map<String, Object> messageItem(Map<String, Object> r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("role", r.get("role"));
        m.put("content", r.get("content"));
        m.put("contexts", jsonOrNull(r.get("contexts")));
        m.put("scores", jsonOrNull(r.get("scores")));
        m.put("create_time", toLocalDateTime(r.get("create_time")));
        m.put("human", isHuman(r.get("intent_snapshot")));
        return m;
    }

    /** JSON 列（contexts/scores）→ JsonNode 透传序列化，避免字符串二次转义 */
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

    /** human 标记：intent_snapshot.human == true（与 Python MessageItem.human 对齐） */
    private static boolean isHuman(Object snapshot) {
        if (snapshot == null) {
            return false;
        }
        try {
            JsonNode n = MAPPER.readTree((String) snapshot);
            return n.path("human").asBoolean(false);
        } catch (Exception e) {
            return false;
        }
    }

    /**
     * JDBC 时间对象 → LocalDateTime。
     * V26 修复：与 HandoffService 同款问题 —— MySQL Connector/J 9 对 DATETIME
     * 返回 LocalDateTime（旧驱动/H2 返回 Timestamp），只认 Timestamp 会让
     * 会话列表/消息时间字段在线上静默变 null。
     */
    private static java.time.LocalDateTime toLocalDateTime(Object v) {
        if (v instanceof java.time.LocalDateTime dt) {
            return dt;
        }
        if (v instanceof Timestamp ts) {
            return ts.toLocalDateTime();
        }
        if (v instanceof java.time.OffsetDateTime odt) {
            return odt.toLocalDateTime();
        }
        return null;
    }
}
