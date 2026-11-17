package com.zhixuan.platform.auth;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * JWT 跨语言互通测试 —— 本次重构最关键的兼容性验证点。
 *
 * 为什么这么测：Java 签发的 token 必须能被 Python python-jose 接受，反之亦然。
 * HMAC 是确定性算法，两边互通的充要条件是
 * 「同 secret 字节 + 同 claims + 同 base64url 编码」。
 * 因此最强断言是【字节级同一性】：Java 用与 Python 向量完全相同的
 * iat/exp/claims 签发，结果必须与 Python 侧（stdlib HMAC 计算）逐字节相同。
 * 这一条过了，互通在数学上成立，不需要再跑跨服务联调才能确认。
 */
class JwtServiceTest {

    private static final String SECRET = "test-secret-e2e";

    /** Python 侧 stdlib HMAC 生成（claims: sub=1, role=admin, iat=1731570000, exp=1899999999） */
    private static final String PY_VECTOR =
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIiwiaWF0IjoxNzMxNTcwMDAwLCJleHAiOjE4OTk5OTk5OTksInJvbGUiOiJhZG1pbiJ9.OvY8xaNXlYTwHry9ZLmvKCsWac50jn6v5-BkMzTvxco";

    private JwtService service(String secret) {
        return new JwtService(new com.zhixuan.platform.config.JwtProps(secret, 24, "cs_token"));
    }

    @Test
    @DisplayName("★字节级同一性：同输入下 Java 与 Python 签发的 token 完全一致")
    void byteIdenticalWithPythonVector() {
        String token = service(SECRET)
                .issueWithEpochs(1L, "admin", 1731570000L, 1899999999L);
        assertThat(token).isEqualTo(PY_VECTOR);
    }

    @Test
    @DisplayName("Python 侧签发的 token 能被 Java 解析（反向互通）")
    void parsePythonIssuedToken() {
        JwtService.JwtClaims claims = service(SECRET).parse(PY_VECTOR);
        assertThat(claims.subject()).isEqualTo("1");
        assertThat(claims.role()).isEqualTo("admin");
        assertThat(claims.claims().path("exp").asLong()).isEqualTo(1899999999L);
    }

    @Test
    @DisplayName("Java 签发 → 解析 roundtrip：claims 与过期时长正确")
    void issueAndParseRoundtrip() {
        JwtService svc = service(SECRET);
        JwtService.JwtClaims claims = svc.parse(svc.issue(42L, "user"));
        assertThat(claims.subject()).isEqualTo("42");
        assertThat(claims.role()).isEqualTo("user");
        long iat = claims.claims().path("iat").asLong();
        long exp = claims.claims().path("exp").asLong();
        assertThat(exp - iat).isEqualTo(24 * 3600L);
    }

    @Test
    @DisplayName("错误密钥 → 401（签名校验真实生效，不是只解析 payload）")
    void wrongSecretRejected() {
        assertThatThrownBy(() -> service("another-secret").parse(PY_VECTOR))
                .isInstanceOf(com.zhixuan.platform.common.ApiException.class)
                .extracting("status").isEqualTo(401);
    }

    @Test
    @DisplayName("篡改 payload（换用户 id）→ 401（防伪造身份）")
    void tamperedTokenRejected() {
        // 拿合法签名替换到另一个 payload 后面 → 验签必失败
        String otherSig = service(SECRET).issue(999L, "user").split("\\.")[2];
        String tampered = PY_VECTOR.substring(0, PY_VECTOR.lastIndexOf('.') + 1) + otherSig;
        assertThatThrownBy(() -> service(SECRET).parse(tampered))
                .isInstanceOf(com.zhixuan.platform.common.ApiException.class)
                .extracting("status").isEqualTo(401);
    }

    @Test
    @DisplayName("过期 token → 401（签名合法但 exp 已过：单独验证时间校验）")
    void expiredTokenRejected() {
        // 合法签名的过期 token（iat/exp 均在 2001 年）——把「签名错」和「过期」
        // 两个失败原因分离，确保过期路径真的被执行
        String expired = service(SECRET)
                .issueWithEpochs(1L, "admin", 1_000_000_000L, 1_000_003_600L);
        assertThatThrownBy(() -> service(SECRET).parse(expired))
                .isInstanceOf(com.zhixuan.platform.common.ApiException.class)
                .extracting("status").isEqualTo(401);
    }

    @Test
    @DisplayName("alg=none 攻击 → 401（算法替换防护）")
    void algNoneRejected() {
        // header 换成 alg:none + 空签名 —— 若实现信任 header.alg 就会绕过验签
        String attack = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."
                + "eyJzdWIiOiIxIiwicm9sZSI6ImFkbWluIn0.";
        assertThatThrownBy(() -> service(SECRET).parse(attack))
                .isInstanceOf(com.zhixuan.platform.common.ApiException.class)
                .extracting("status").isEqualTo(401);
    }

    @Test
    @DisplayName("短密钥可正常工作（与 python-jose 行为一致——jjwt 会拒，我们不会）")
    void shortSecretStillWorks() {
        JwtService svc = service("short");
        String token = svc.issue(7L, "admin");
        assertThat(svc.parse(token).subject()).isEqualTo("7");
    }
}
