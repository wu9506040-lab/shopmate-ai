package com.zhixuan.platform.common;

import java.util.LinkedHashMap;
import java.util.Map;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 平台层健康检查（供容器 HEALTHCHECK / 负载均衡探活）。
 *
 * 与 Python /health 的差异：平台层需要校验 DB 连通（会话真源），
 * AI 服务健康由网关的上游错误处理兜底，此处不做跨服务探活
 * ——避免「AI 挂 = 平台层判死」的级联误杀。
 */
@RestController
public class HealthController {

    private final JdbcTemplate jdbc;

    public HealthController(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @GetMapping("/healthz")
    public Map<String, Object> healthz() {
        Map<String, Object> body = new LinkedHashMap<>();
        try {
            jdbc.queryForObject("SELECT 1", Integer.class);
            body.put("status", "ok");
            body.put("db", "ok");
        } catch (Exception e) {
            body.put("status", "degraded");
            body.put("db", "down");
        }
        return body;
    }
}
