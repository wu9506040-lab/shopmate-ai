package com.zhixuan.platform.auth;

import com.zhixuan.platform.common.ApiException;
import com.zhixuan.platform.config.JwtProps;
import jakarta.servlet.http.Cookie;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.stereotype.Service;

/**
 * 请求身份解析：Authorization: Bearer 优先，回落 httpOnly Cookie。
 *
 * 与 Python deps._extract_token 完全同构 —— 双端共存机制（管理台 Bearer +
 * 商城 Cookie）跨语言保持一致。
 */
@Service
public class CurrentUserService {

    private final JwtService jwtService;
    private final JwtProps jwtProps;
    private final AuthService authService;

    public CurrentUserService(JwtService jwtService, JwtProps jwtProps, AuthService authService) {
        this.jwtService = jwtService;
        this.jwtProps = jwtProps;
        this.authService = authService;
    }

    /** 必填身份：缺失/无效 → 401（文案与 Python deps 一致） */
    public UserPrincipal require(HttpServletRequest request) {
        UserPrincipal user = optional(request);
        if (user == null) {
            throw new ApiException(401, "未登录或登录已过期");
        }
        return user;
    }

    /** 可选身份：chat 网关等场景（Python get_current_user_optional 同构） */
    public UserPrincipal optional(HttpServletRequest request) {
        String token = extractToken(request);
        if (token == null) {
            return null;
        }
        JwtService.JwtClaims claims = jwtService.parse(token);
        long userId;
        try {
            userId = Long.parseLong(claims.subject());
        } catch (NumberFormatException e) {
            throw new ApiException(401, "未登录或登录已过期");
        }
        return authService.loadById(userId);
    }

    private String extractToken(HttpServletRequest request) {
        String authHeader = request.getHeader("Authorization");
        if (authHeader != null && authHeader.regionMatches(true, 0, "Bearer ", 0, 7)) {
            String token = authHeader.substring(7).trim();
            if (!token.isEmpty()) {
                return token;
            }
        }
        Cookie[] cookies = request.getCookies();
        if (cookies != null) {
            for (Cookie c : cookies) {
                if (jwtProps.cookieName().equals(c.getName()) && !c.getValue().isEmpty()) {
                    return c.getValue();
                }
            }
        }
        return null;
    }
}
