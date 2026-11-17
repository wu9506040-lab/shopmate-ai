package com.zhixuan.platform.auth;

import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zhixuan.platform.common.ApiException;
import com.zhixuan.platform.config.JwtProps;

/**
 * JWT 签发/校验 —— 纯 JDK HMAC 实现，与 Python python-jose 字节级互通。
 *
 * 为什么手写而不用 jjwt：
 * 1. jjwt 强制 HMAC 密钥 ≥256bit，python-jose 无此限制 —— 密钥短于 32 字节时
 *    Java 会拒收 Python 合法签发的 token，互通直接破裂（实测踩中）。
 *    HS256 JWT 本身是极简标准（三段 base64url + HMAC），自实现可完全控制字节，
 *    保证两侧行为一致。
 * 2. 互通条件（三方对齐）：同 secret 原始字节 + 同 claims（sub=String(user_id)、
 *    iat/exp=epoch 秒、role）+ 标准 base64url 无填充。
 *
 * 安全部件：
 * - 解析时显式校验 header.alg == HS256（防 alg=none / 算法替换攻击）
 * - 签名比对用 MessageDigest.isEqual（常数时间，防时序侧信道）
 * - 过期校验显式执行（exp epoch 秒语义与 jose 一致）
 */
public class JwtService {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Base64.Encoder B64E = Base64.getUrlEncoder().withoutPadding();
    private static final Base64.Decoder B64D = Base64.getUrlDecoder();

    /**
     * 标准头（与 python-jose 默认头字节一致：紧凑 JSON、无空格）。
     *
     * ⚠️ 必须用字面量常量而非 Map 序列化：Map.of 的迭代顺序不保证
     * （实测同一 JVM 不同次运行会给出 {"typ","alg"} 的另一种键序），
     * 键序变化 → 签名输入变化 → token 字节不可复现，
     * 与 Python 向量的字节级互通断言会间歇性失败。
     */
    private static final byte[] HEADER =
            "{\"alg\":\"HS256\",\"typ\":\"JWT\"}".getBytes(StandardCharsets.UTF_8);

    private final JwtProps props;

    public JwtService(JwtProps props) {
        this.props = props;
    }

    /** 签发 token：claims 与 Python create_access_token 完全一致 */
    public String issue(long userId, String role) {
        long now = Instant.now().getEpochSecond();
        return issueWithEpochs(userId, role, now, now + props.expireHours() * 3600L);
    }

    /** 确定性签发（包级可见，供互认测试固定 iat/exp 向量） */
    String issueWithEpochs(long userId, String role, long iat, long exp) {
        // 顺序对齐 Python：{sub, iat, exp} 后 update(role) → sub/iat/exp/role。
        // 验签方不依赖键序，但同序可让「同输入 → 字节级同 token」可测。
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("sub", String.valueOf(userId));
        payload.put("iat", iat);
        payload.put("exp", exp);
        payload.put("role", role);

        try {
            String header = B64E.encodeToString(HEADER);
            String body = B64E.encodeToString(jsonBytes(payload));
            String signingInput = header + "." + body;
            String signature = B64E.encodeToString(hmacSha256(
                    signingInput.getBytes(StandardCharsets.UTF_8)));
            return signingInput + "." + signature;
        } catch (Exception e) {
            throw new IllegalStateException("JWT 签发失败", e);
        }
    }

    /**
     * 校验并解析 token。失败（签名错/过期/格式错/alg 替换）→ 401，
     * 文案与 Python deps 401 完全一致。
     */
    public JwtClaims parse(String token) {
        try {
            String[] parts = token == null ? new String[0] : token.split("\\.");
            if (parts.length != 3) {
                throw new ApiException(401, "未登录或登录已过期");
            }

            // 1. header.alg 必须是 HS256（拒绝 alg=none / RS256 替换攻击）
            JsonNode header = MAPPER.readTree(B64D.decode(parts[0]));
            if (!"HS256".equals(header.path("alg").asText())) {
                throw new ApiException(401, "未登录或登录已过期");
            }

            // 2. 签名校验（常数时间比较）
            byte[] expected = hmacSha256(
                    (parts[0] + "." + parts[1]).getBytes(StandardCharsets.UTF_8));
            byte[] actual;
            try {
                actual = B64D.decode(parts[2]);
            } catch (IllegalArgumentException e) {
                throw new ApiException(401, "未登录或登录已过期");
            }
            if (!MessageDigest.isEqual(expected, actual)) {
                throw new ApiException(401, "未登录或登录已过期");
            }

            // 3. 过期校验（exp epoch 秒；缺失视为永不过期——与 jose 行为一致，
            //    本系统签发的 token 恒带 exp）
            JsonNode payload = MAPPER.readTree(B64D.decode(parts[1]));
            long exp = payload.path("exp").asLong(0);
            if (exp > 0 && exp < Instant.now().getEpochSecond()) {
                throw new ApiException(401, "未登录或登录已过期");
            }
            return new JwtClaims(payload.path("sub").asText(), payload);

        } catch (ApiException e) {
            throw e;
        } catch (Exception e) {
            throw new ApiException(401, "未登录或登录已过期");
        }
    }

    private byte[] hmacSha256(byte[] data) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(
                    props.secret().getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            return mac.doFinal(data);
        } catch (GeneralSecurityException e) {
            throw new IllegalStateException("HMAC 初始化失败", e);
        }
    }

    private static byte[] jsonBytes(Map<String, Object> map) {
        try {
            return MAPPER.writeValueAsBytes(map);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** 解析结果（subject + 原始 claims） */
    public record JwtClaims(String subject, JsonNode claims) {
        public String role() {
            return claims.path("role").asText(null);
        }
    }
}