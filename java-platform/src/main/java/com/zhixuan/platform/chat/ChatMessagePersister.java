package com.zhixuan.platform.chat;

import java.sql.PreparedStatement;
import java.sql.Timestamp;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import org.springframework.jdbc.support.KeyHolder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 消息落库（P3：单一写入者 = Java 平台层）。
 *
 * 语义逐条镜像 Python mysql_store.persist_to_mysql：
 * - UPSERT 会话：新建 → title=首问前200字、message_count=2、first_query=前500字；
 *   续聊 → message_count += 2，last_message_at=now
 * - INSERT 双行消息：user（无附加字段）+ assistant（contexts/scores/intent_snapshot）
 * - 返回 assistant 行 id（前端"按条评价"依赖它，回填进 done 事件）
 *
 * 为什么同步执行：done 帧需要携带 assistant_message_id，落库必须先于转发完成；
 * 单轮 3 条 SQL（索引命中）在本机/内网 <5ms，对首字延迟无影响（done 是末帧）。
 */
@Service
public class ChatMessagePersister {

    private static final Logger log = LoggerFactory.getLogger(ChatMessagePersister.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final JdbcTemplate jdbc;

    public ChatMessagePersister(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /**
     * 落库一轮问答。失败返回 null（不抛：落库失败不能破坏前端已收到的流）。
     */
    @Transactional
    public Long persist(long userId, String query, SseStreamAccumulator acc) {
        if (query == null || query.isBlank() || !acc.persistable()) {
            log.warn("persist skipped: 残答/空答不入库 session={}", acc.sessionId());
            return null;
        }
        String sessionId = acc.sessionId();
        try {
            upsertConversation(userId, sessionId, query);
            insertMessage(sessionId, userId, "user", query, null, null, null);
            return insertMessage(sessionId, userId, "assistant", acc.answer(),
                    acc.contextsJson(), acc.scoresJson(), intentSnapshot(acc.intent()));
        } catch (Exception e) {
            log.error("persist failed: session={} err={}", sessionId, e.getMessage());
            return null;
        }
    }

    private void upsertConversation(long userId, String sessionId, String query) {
        Integer count = jdbc.queryForObject(
                "SELECT COUNT(*) FROM conversations WHERE session_id = ? AND deleted = 0",
                Integer.class, sessionId);
        Timestamp now = Timestamp.valueOf(LocalDateTime.now());
        if (count == null || count == 0) {
            jdbc.update("""
                    INSERT INTO conversations (session_id, user_id, title, message_count,
                        first_query, last_message_at)
                    VALUES (?, ?, ?, 2, ?, ?)
                    """, sessionId, userId, truncate(query, 200), truncate(query, 500), now);
        } else {
            jdbc.update("""
                    UPDATE conversations
                    SET message_count = COALESCE(message_count, 0) + 2, last_message_at = ?
                    WHERE session_id = ? AND deleted = 0
                    """, now, sessionId);
        }
    }

    /** 插入单条消息，返回自增 id（H2/MySQL 均支持 RETURN_GENERATED_KEYS） */
    private Long insertMessage(String sessionId, long userId, String role, String content,
                               String contextsJson, String scoresJson, String intentJson) {
        String sql = """
                INSERT INTO messages (session_id, user_id, role, content, contexts, scores,
                    intent_snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """;
        KeyHolder keyHolder = new GeneratedKeyHolder();
        jdbc.update((java.sql.Connection con) -> {
            PreparedStatement ps = con.prepareStatement(sql, new String[]{"id"});
            ps.setString(1, sessionId);
            ps.setLong(2, userId);
            ps.setString(3, role);
            ps.setString(4, content);
            ps.setString(5, contextsJson);
            ps.setString(6, scoresJson);
            ps.setString(7, intentJson);
            return ps;
        }, keyHolder);
        Number key = keyHolder.getKey();
        return key == null ? null : key.longValue();
    }

    /** 意图快照：与 Python intent_snapshot 同构（human 标记供前端区分人工消息） */
    private static String intentSnapshot(String intent) {
        if (intent == null) {
            return null;
        }
        try {
            return MAPPER.writeValueAsString(Map.of("intent", intent, "human", false));
        } catch (Exception e) {
            return null;
        }
    }

    private static String truncate(String s, int max) {
        return s.length() <= max ? s : s.substring(0, max);
    }

    /** 供测试/诊断：查询某会话消息数 */
    public int countMessages(String sessionId) {
        Integer c = jdbc.queryForObject(
                "SELECT COUNT(*) FROM messages WHERE session_id = ? AND deleted = 0",
                Integer.class, sessionId);
        return c == null ? 0 : c;
    }

    public List<Map<String, Object>> messages(String sessionId) {
        return jdbc.queryForList(
                "SELECT id, role, content, contexts FROM messages WHERE session_id = ? AND deleted = 0 ORDER BY id",
                sessionId);
    }
}
