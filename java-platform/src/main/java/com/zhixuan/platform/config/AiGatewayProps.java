package com.zhixuan.platform.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.time.Duration;

/** Python AI 服务连接配置（SSE 网关透传目标） */
@ConfigurationProperties(prefix = "platform.ai")
public record AiGatewayProps(
        String baseUrl,
        String chatPath,
        Gateway gateway
) {
    public record Gateway(long timeoutMs, long connectTimeoutMs) {
    }

    public AiGatewayProps {
        if (baseUrl == null || baseUrl.isBlank()) {
            baseUrl = "http://localhost:8000";
        }
        if (chatPath == null || chatPath.isBlank()) {
            chatPath = "/api/chat";
        }
        if (gateway == null) {
            gateway = new Gateway(300_000L, 5_000L);
        }
    }

    public Duration connectTimeout() {
        return Duration.ofMillis(gateway.connectTimeoutMs());
    }

    public Duration gatewayTimeout() {
        return Duration.ofMillis(gateway.timeoutMs());
    }
}
