#!/bin/bash
# 本地直跑入口（非 Docker）：从已停容器提取凭据，连本机 Docker 基础设施
cd "$(dirname "$0")" || exit 1

CT_API=$(docker inspect customer-service-api --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null)
CT_JAVA=$(docker inspect customer-service-java --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null)

export JWT_SECRET=$(echo "$CT_API" | grep '^JWT_SECRET=' | cut -d= -f2-)
export DB_HOST=127.0.0.1
export DB_PORT=3307
export DB_NAME=$(echo "$CT_JAVA" | grep '^DB_NAME=' | cut -d= -f2-)
export DB_USER=$(echo "$CT_JAVA" | grep '^DB_USER=' | cut -d= -f2-)
export DB_PASSWORD=$(echo "$CT_JAVA" | grep '^DB_PASSWORD=' | cut -d= -f2-)
# AI_BASE_URL 默认即 http://localhost:8000，显式声明便于排查
export AI_BASE_URL=http://localhost:8000

# 本机 8080 常被其他服务占用（与容器 8081→8080 映射同理），本地直听 8081
exec java -jar target/cs-platform-0.1.0.jar --server.port=8081
