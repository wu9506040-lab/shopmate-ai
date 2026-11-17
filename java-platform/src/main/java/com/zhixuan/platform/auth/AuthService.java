package com.zhixuan.platform.auth;

import java.util.List;
import java.util.Map;

import com.zhixuan.platform.common.ApiException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.security.crypto.bcrypt.BCrypt;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

/**
 * 用户认证（MySQL users 表，SQL 语义与 Python auth_service 对齐）。
 */
@Service
public class AuthService {

    private static final Logger log = LoggerFactory.getLogger(AuthService.class);

    private final JdbcTemplate jdbc;

    /** login 查询列（users 表，含 deleted 软删三件套约定） */
    private static final String LOGIN_SQL = """
            SELECT id, username, password_hash, display_name, email, role, status
            FROM users
            WHERE username = ? AND deleted = 0
            """;

    /** 按 id 取活跃用户（/me 与 token 解析后的身份回查） */
    private static final String BY_ID_SQL = """
            SELECT id, username, password_hash, display_name, email, role, status
            FROM users
            WHERE id = ? AND deleted = 0
            """;

    public AuthService(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /**
     * 登录：用户名查活跃用户 → bcrypt 校验 → status 检查。
     * 任一环节失败一律返回 401「用户名或密码错误」——
     * 不区分「用户不存在/密码错/账号禁用」，避免账号枚举（与 Python 语义一致）。
     */
    public UserPrincipal login(String username, String password) {
        List<Map<String, Object>> rows =
                jdbc.queryForList(LOGIN_SQL, username == null ? "" : username);
        if (rows.isEmpty()) {
            throw new ApiException(401, "用户名或密码错误");
        }
        Map<String, Object> row = rows.get(0);
        String hash = (String) row.get("password_hash");

        boolean ok;
        try {
            ok = BCrypt.checkpw(password == null ? "" : password, hash);
        } catch (IllegalArgumentException e) {
            // hash 格式非法（与 Python verify_password 的 try/except 对齐）
            ok = false;
        }
        if (!ok) {
            throw new ApiException(401, "用户名或密码错误");
        }

        int status = ((Number) row.get("status")).intValue();
        if (status != 1) {
            // Python authenticate：status != 1 → 返回 None → 401 同文案
            log.info("login rejected: user={} status={}", row.get("id"), status);
            throw new ApiException(401, "用户名或密码错误");
        }
        return toPrincipal(row);
    }

    /** token 解析后的身份回查（用户被删/禁用后旧 token 立即失效） */
    public UserPrincipal loadById(long userId) {
        List<Map<String, Object>> rows = jdbc.queryForList(BY_ID_SQL, userId);
        if (rows.isEmpty()) {
            throw new ApiException(401, "未登录或登录已过期");
        }
        Map<String, Object> row = rows.get(0);
        int status = ((Number) row.get("status")).intValue();
        if (status != 1) {
            throw new ApiException(401, "未登录或登录已过期");
        }
        return toPrincipal(row);
    }

    private static UserPrincipal toPrincipal(Map<String, Object> row) {
        String displayName = (String) row.get("display_name");
        String email = (String) row.get("email");
        return new UserPrincipal(
                ((Number) row.get("id")).longValue(),
                (String) row.get("username"),
                displayName,
                email,
                (String) row.get("role"),
                ((Number) row.get("status")).intValue()
        );
    }
}
