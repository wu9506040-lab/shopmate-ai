package com.zhixuan.platform.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * JWT 配置。secret 必须与 Python 侧 settings.JWT_SECRET 完全一致，
 * 这是 Java 签发的 token 能被 Python decode_access_token 接受的前提。
 */
@ConfigurationProperties(prefix = "platform.jwt")
public record JwtProps(String secret, int expireHours, String cookieName) {

    public JwtProps {
        if (secret == null || secret.isBlank()) {
            // fail-fast：与 Python config._validate_jwt_secret 哲学一致，
            // 宁可拒绝启动也不带弱密钥上线
            throw new IllegalStateException(
                    "JWT_SECRET 未配置（Java 与 Python 必须共用同一密钥）");
        }
        if (expireHours <= 0) {
            expireHours = 24;
        }
        if (cookieName == null || cookieName.isBlank()) {
            cookieName = "cs_token";
        }
    }
}
