package com.zhixuan.platform.auth;

/**
 * 已认证用户身份（Java 侧最小字段集，与 Python UserOut 对齐）。
 */
public record UserPrincipal(
        long id,
        String username,
        String displayName,
        String email,
        String role,
        int status
) {
}
