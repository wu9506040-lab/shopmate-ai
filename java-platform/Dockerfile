# =============================================================
# Java 中台运行镜像（单阶段：本地/CI 构建产物 → 运行）
#
# 为什么不用多阶段（容器内 mvn 编译）：
# 国内拉 maven:3.9-eclipse-temurin-21 基础镜像 ≈500MB，叠加容器内重下
# 全部依赖，实测 >20 分钟仍无进展；而 jar 在本地/CI 构建（可复现、
# 有单测门禁），镜像只负责运行 —— 构建 <30s，镜像更小、攻击面更少。
# =============================================================
FROM eclipse-temurin:21-jre
WORKDIR /app

# 非 root 运行（生产安全基线）
RUN groupadd -r appuser && useradd -r -g appuser appuser

COPY target/cs-platform-*.jar app.jar
RUN chown appuser:appuser app.jar
USER appuser

EXPOSE 8080

# 健康检查：/healthz 会校验 DB 连通（会话真源）
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD wget -q -O- http://127.0.0.1:8080/healthz >/dev/null 2>&1 || exit 1

ENTRYPOINT ["java", "-XX:MaxRAMPercentage=70", "-jar", "/app/app.jar"]
