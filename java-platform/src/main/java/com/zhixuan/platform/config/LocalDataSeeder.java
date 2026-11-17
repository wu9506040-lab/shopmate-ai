package com.zhixuan.platform.config;

import java.time.LocalDateTime;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.CommandLineRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Profile;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.crypto.bcrypt.BCrypt;

/**
 * 本地开发数据种子（仅 local profile）。
 *
 * 为什么存在：本地 H2 每次启动都是空库，E2E 需要稳定的账号与对话数据；
 * 种子用与线上完全相同的 BCrypt 代码路径生成 hash —— 登录验证链路
 * （hash 生成 → checkpw 校验）在本地即被完整覆盖，不需要真实 MySQL。
 */
@Configuration
public class LocalDataSeeder {

    private static final Logger log = LoggerFactory.getLogger(LocalDataSeeder.class);

    @Bean
    @Profile("local")
    public CommandLineRunner seedData(JdbcTemplate jdbc) {
        return args -> {
            Integer userCount = jdbc.queryForObject("SELECT COUNT(*) FROM users", Integer.class);
            if (userCount != null && userCount == 0) {
                String adminHash = BCrypt.hashpw("local123", BCrypt.gensalt(10));
                String userHash = BCrypt.hashpw("user123", BCrypt.gensalt(10));
                jdbc.update("""
                        INSERT INTO users (username, password_hash, display_name, email, role, status)
                        VALUES ('admin', ?, '本地管理员', 'admin@local.test', 'admin', 1),
                               ('buyer1', ?, '本地买家', 'buyer1@local.test', 'user', 1)
                        """, adminHash, userHash);
                log.info("local seed: 2 users created (admin/local123, buyer1/user123)");
            }

            Integer convCount = jdbc.queryForObject(
                    "SELECT COUNT(*) FROM conversations", Integer.class);
            if (convCount != null && convCount == 0) {
                Long adminId = jdbc.queryForObject(
                        "SELECT id FROM users WHERE username = 'admin'", Long.class);
                Long buyerId = jdbc.queryForObject(
                        "SELECT id FROM users WHERE username = 'buyer1'", Long.class);

                // admin 两个会话（一个多轮消息用于测分页，一个单条）
                seedConversation(jdbc, adminId, "seed-conv-0001", "退货政策咨询", 6);
                seedConversation(jdbc, adminId, "seed-conv-0002", "物流时效问题", 2);
                // buyer1 一个会话（用于验证越权 404）
                seedConversation(jdbc, buyerId, "seed-conv-buyer", "买家会话", 2);
                // 工单种子（用于坐席工作台 E2E：pending 可认领）
                jdbc.update("""
                        INSERT INTO handoff_tickets (ticket_no, session_id, user_id, reason,
                            category, priority, status, summary)
                        VALUES ('HFT-SEED000001', 'seed-conv-0001', ?, 'user_request',
                            'refund', 'P1', 'pending', '{"note":"seed ticket"}')
                        """, adminId);
                log.info("local seed: 3 conversations + 1 handoff ticket created");
            }
        };
    }

    /** 造一个会话：n 条消息（user/assistant 交替），last_message_at 依次递减保证排序确定性 */
    private static void seedConversation(JdbcTemplate jdbc, Long userId,
                                         String sessionId, String title, int msgCount) {
        jdbc.update("""
                INSERT INTO conversations (session_id, user_id, title, message_count,
                    first_query, last_message_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """, sessionId, userId, title, msgCount, title + "首问",
                LocalDateTime.now().minusMinutes(seedConversationOrder.getAndIncrement()));

        for (int i = 1; i <= msgCount; i++) {
            String role = i % 2 == 1 ? "user" : "assistant";
            jdbc.update("""
                    INSERT INTO messages (session_id, role, content, contexts, intent_snapshot)
                    VALUES (?, ?, ?, ?, ?)
                    """, sessionId, role,
                    role.equals("user") ? "第" + i + "条：用户提问" : "第" + i + "条：客服回答",
                    role.equals("assistant") ? "[{\"doc\":\"退换货政策\",\"score\":0.92}]" : null,
                    role.equals("assistant") ? "{\"intent\":\"policy_query\",\"human\":false}" : null);
        }
    }

    private static final java.util.concurrent.atomic.AtomicLong seedConversationOrder =
            new java.util.concurrent.atomic.AtomicLong(1);
}
