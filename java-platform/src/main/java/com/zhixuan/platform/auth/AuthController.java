package com.zhixuan.platform.auth;

import java.time.LocalDateTime;
import java.util.LinkedHashMap;
import java.util.Map;

import com.zhixuan.platform.common.ApiException;
import com.zhixuan.platform.config.JwtProps;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.dao.EmptyResultDataAccessException;
import org.springframework.http.ResponseCookie;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * /api/auth/* —— 与 Python api/auth.py 契约逐字段对齐：
 * - POST /api/auth/login（form-urlencoded，OAuth2PasswordRequestForm 兼容）
 *   → {user:{...}, message:"登录成功"} + Set-Cookie cs_token
 * - POST /api/auth/login?scope=admin（管理台）
 *   → {user:{...}, access_token:"..."} 且不写 Cookie（双端共存，Bearer 按 tab 隔离）
 * - POST /api/auth/logout → {message:"已登出"} + 清 Cookie
 * - GET  /api/auth/me → 用户对象（扁平，前端 getMe 直接 res.json()）
 */
@RestController
public class AuthController {

    private static final org.slf4j.Logger log =
            org.slf4j.LoggerFactory.getLogger(AuthController.class);

    private final AuthService authService;
    private final CurrentUserService currentUser;
    private final JwtProps jwtProps;
    private final com.zhixuan.platform.auth.JwtService jwtService;
    private final JdbcTemplate jdbc;

    public AuthController(AuthService authService, CurrentUserService currentUser,
                          JwtProps jwtProps, JwtService jwtService, JdbcTemplate jdbc) {
        this.authService = authService;
        this.currentUser = currentUser;
        this.jwtProps = jwtProps;
        this.jwtService = jwtService;
        this.jdbc = jdbc;
    }

    @PostMapping(value = "/api/auth/login",
            consumes = org.springframework.http.MediaType.APPLICATION_FORM_URLENCODED_VALUE)
    public Map<String, Object> login(
            @org.springframework.web.bind.annotation.RequestParam String username,
            @org.springframework.web.bind.annotation.RequestParam String password,
            @org.springframework.web.bind.annotation.RequestParam(defaultValue = "mall") String scope,
            HttpServletResponse response) {

        UserPrincipal user = authService.login(username, password);
        String token = jwtService.issue(user.id(), user.role());

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("user", userMap(user, fetchCreateTime(user.id())));
        body.put("message", "登录成功");

        // scope=admin：管理台专用登录（与 Python api/auth.py 契约一致）。
        // 不写共享 Cookie —— 否则会覆盖商城买家的 cs_token、把另一个 tab 踢下线。
        // token 从 body 返回，前端存 sessionStorage 并按 tab 隔离，请求带 Bearer。
        // CurrentUserService 已支持 Bearer 优先 / Cookie 回落，后续接口无需改动。
        if ("admin".equals(scope)) {
            if (!"admin".equals(user.role())) {
                throw new ApiException(403, "该账号不是管理员");
            }
            body.put("access_token", token);
            return body;
        }

        // 默认 mall：Cookie 语义对齐 Python _set_token_cookie：HttpOnly + Path=/ + SameSite=Lax
        ResponseCookie cookie = ResponseCookie.from(jwtProps.cookieName(), token)
                .httpOnly(true)
                .path("/")
                .maxAge(jwtProps.expireHours() * 3600L)
                .sameSite("Lax")
                .build();
        response.addHeader(org.springframework.http.HttpHeaders.SET_COOKIE, cookie.toString());
        return body;
    }

    @PostMapping("/api/auth/logout")
    public Map<String, Object> logout(HttpServletResponse response) {
        ResponseCookie cookie = ResponseCookie.from(jwtProps.cookieName(), "")
                .httpOnly(true)
                .path("/")
                .maxAge(0)
                .build();
        response.addHeader(org.springframework.http.HttpHeaders.SET_COOKIE, cookie.toString());
        return Map.of("message", "已登出");
    }

    /**
     * 注册（公开）。契约对齐 Python /api/auth/register：JSON 入参、201 返回 {user}。
     * 为什么必须补在 Java：nginx 已把 /api/auth/* 灰度到 Java，Python 的 register
     * 不再可达 —— 不补则商城注册页 404（2026-11-15 链路冒烟实测发现）。
     * 密码哈希 bcrypt rounds=12，与 Python BCRYPT_ROUNDS 一致（共享 users 表互认）。
     */
    @PostMapping(value = "/api/auth/register",
            consumes = org.springframework.http.MediaType.APPLICATION_JSON_VALUE)
    @org.springframework.web.bind.annotation.ResponseStatus(org.springframework.http.HttpStatus.CREATED)
    public Map<String, Object> register(@org.springframework.web.bind.annotation.RequestBody Map<String, Object> body) {
        String username = str(body.get("username"));
        // V26 边界：密码必须显式字符串且不做 trim —— str() 的 trim 会静默改密码
        // （注册 " abc12345 " 存成 "abc12345"，登录时 Spring 不 trim → 永远 401）。
        // 非字符串（数字等）与 pydantic 行为一致返回 422。
        String password = rawString(body.get("password"));
        String displayName = str(body.get("display_name"));
        String email = str(body.get("email"));

        // 入参校验（422 语义对齐 FastAPI/pydantic 错误文案）
        if (username == null || !username.matches("^[a-zA-Z0-9_]{3,64}$")) {
            throw new ApiException(422, "username: 3-64 位字母数字下划线");
        }
        if (password == null || password.length() < 8 || password.length() > 128) {
            throw new ApiException(422, "password: 密码长度 8-128 字符");
        }
        if (displayName != null && displayName.length() > 200) {
            throw new ApiException(422, "display_name: 最长 200 字符");
        }
        if (email != null && !email.matches("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$")) {
            throw new ApiException(422, "email: 邮箱格式不正确");
        }

        // 唯一性预检（users.username/email 均有 UNIQUE，友好提示优先于 DB 报错）
        Integer dupName = jdbc.queryForObject(
                "SELECT COUNT(*) FROM users WHERE username = ? AND deleted = 0", Integer.class, username);
        if (dupName != null && dupName > 0) {
            throw new ApiException(409, "用户名已存在: " + username);
        }
        if (email != null) {
            Integer dupMail = jdbc.queryForObject(
                    "SELECT COUNT(*) FROM users WHERE email = ? AND deleted = 0", Integer.class, email);
            if (dupMail != null && dupMail > 0) {
                throw new ApiException(409, "邮箱已被使用: " + email);
            }
        }

        String hash = org.springframework.security.crypto.bcrypt.BCrypt.hashpw(password,
                org.springframework.security.crypto.bcrypt.BCrypt.gensalt(12));
        try {
            jdbc.update("""
                    INSERT INTO users (username, password_hash, display_name, email, role, status, deleted)
                    VALUES (?, ?, ?, ?, 'user', 1, 0)
                    """, username, hash, displayName, email);
        } catch (org.springframework.dao.DuplicateKeyException e) {
            // V26 并发窗口：预检与 INSERT 之间同名注册竞态 —— 数据库 UNIQUE 兜底。
            // 不捕获会落到 DataAccessException → 500；对用户报 409 才是正确语义。
            throw new ApiException(409, "用户名或邮箱已被使用");
        }
        log.info("register: username={} email={}", username, email == null ? "-" : email);

        Map<String, Object> row = jdbc.queryForList("""
                SELECT id, username, display_name, email, role, status
                FROM users WHERE username = ? AND deleted = 0
                """, username).get(0);
        UserPrincipal user = new UserPrincipal(
                ((Number) row.get("id")).longValue(),
                (String) row.get("username"),
                (String) row.get("display_name"),
                (String) row.get("email"),
                (String) row.get("role"),
                ((Number) row.get("status")).intValue());
        return Map.of("user", userMap(user, fetchCreateTime(user.id())));
    }

    private static String str(Object v) {
        if (v == null) {
            return null;
        }
        String s = String.valueOf(v).trim();
        return s.isEmpty() ? null : s;
    }

    /**
     * V26：显式字符串提取（不 trim）。密码等语义敏感字段必须：
     * 1. 类型严格 —— 数字/布尔等非字符串与 pydantic 一致返回 422；
     * 2. 原样保留 —— 任何 trim 都会静默改变密码内容。
     */
    private static String rawString(Object v) {
        return v instanceof String s ? s : null;
    }

    @GetMapping("/api/auth/me")
    public Map<String, Object> me(HttpServletRequest request) {
        UserPrincipal user = currentUser.require(request);
        return userMap(user, fetchCreateTime(user.id()));
    }

    // ------------------------------------------------------------------

    /** create_time 单独取（UserPrincipal 不携带，保持身份对象轻量） */
    private LocalDateTime fetchCreateTime(long userId) {
        try {
            java.sql.Timestamp ts = jdbc.queryForObject(
                    "SELECT create_time FROM users WHERE id = ?",
                    java.sql.Timestamp.class, userId);
            return ts == null ? null : ts.toLocalDateTime();
        } catch (EmptyResultDataAccessException e) {
            throw new ApiException(401, "未登录或登录已过期");
        }
    }

    /** 字段名保持 snake_case —— 前端 types.ts User 契约不变 */
    private static Map<String, Object> userMap(UserPrincipal u, LocalDateTime createTime) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", u.id());
        m.put("username", u.username());
        m.put("display_name", u.displayName());
        m.put("email", u.email());
        m.put("role", u.role());
        m.put("status", u.status());
        m.put("create_time", createTime);
        return m;
    }
}
