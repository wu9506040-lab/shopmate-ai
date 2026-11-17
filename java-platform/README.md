# cs-platform —— 智能客服中台 Java 平台层

> **三部分架构**：前端 Vue3（nginx）｜ **Java 中台（Spring Boot 3）** ｜ Python AI 服务（FastAPI, `backend/`）
> 边界原则：**平台业务在 Java，AI 推理在 Python** —— 对应企业"存量 Java 系统加 AI"的标准形态。

## 职责边界

| Java 平台层（本仓库，:8081） | Python AI 层（backend/，:8000） |
|---|---|
| 鉴权（JWT Cookie + Bearer 双通道） | 意图三级漏斗路由 |
| 会话/消息真源（MySQL） | 多 Agent 协同 + Function Call |
| 会话/消息 CRUD API | RAG 混合检索（BM25+向量+RRF+重排） |
| SSE 网关（透传 + 落库时机） | 幻觉校验 / 引用溯源 |
| 坐席工单（队列/认领/回复结单/归档） | embedding / rerank / ingest |
| 看板网关（鉴权 + 转发，统计口径在 AI 服务） | 数据看板聚合 / 区间报表 / KPI 大盘（AI 服务唯一实现） |

**前端零改动**：平台层采用与 AI 服务相同的路由、请求/响应契约与错误格式
（`{"detail": "..."}`），前端 `api.ts` 的归一化逻辑直接可用。

## 设计决策（为什么这么做）

### 1. 跨语言 JWT 互认：共用 JWT_SECRET + 纯 JDK HMAC 手写 HS256
- **不用 jjwt**：它强制 HMAC 密钥 ≥256bit，python-jose 无此限制——短密钥场景
  Java 会拒收 Python 合法签发的 token（实测踩中，见测试 `shortSecretStillWorks`）。
- 手写实现（约 90 行）可完全控制字节：标准 base64url 无填充、claims 顺序
  （sub/iat/exp/role，对齐 Python `create_access_token`）、exp epoch 秒语义。
- **互通是数学等价的**：HS256 HMAC 确定性算法，同 secret 字节 + 同 claims 字节
  → 同 token。单元测试 `byteIdenticalWithPythonVector` 用 Python 侧真实计算
  的固定向量做金标，Java 签发必须逐字节相等。
- 安全部件：header.alg 显式校验（防 alg=none 攻击）、`MessageDigest.isEqual`
  常数时间签名比对（防时序侧信道）。

### 2. SSE 网关透传：Servlet MVC + SseEmitter + 虚拟线程（非 WebFlux）
- 阻塞读上游流跑在 **Java 21 虚拟线程**上，阻塞成本归零——这是对 reactive
  的务实替代，排障心智模型更简单。
- 透传身份：Java 校验后签发 Bearer 头传给 Python（Python `deps._extract_token`
  Bearer 优先、回落 Cookie，两侧同构）——AI 服务无需解析 Cookie，为无状态化铺路。

### 3. 边界处理（每条对应真实故障模式）
| 边界 | 处理 | 依据 |
|---|---|---|
| 客户端提前断开 | emitter 回调 → 取消上游读 + 关流 | 防连接泄漏 |
| 上游连接失败 | 502 `{"detail":"AI 服务不可用"}` | 前端错误归一化可用 |
| 上游中途断流 | 发 error 事件后 complete | 前端能看到失败原因而非连接突断 |
| 上游非 200 | 统一 502（不泄漏 AI 内部错误码） | 安全 |
| 超时 | emitter onTimeout（300s，对齐 nginx 专线） | 与既有部署一致 |
| SSE 半帧跨包/多事件一包/CRLF | SseFrameParser 独立单测覆盖 | 分帧是最易错环节 |
| 他人会话访问 | 404（不暴露存在性） | 防 IDOR 枚举，与 Python 语义一致 |
| 登录失败统一文案 | 401「用户名或密码错误」 | 防账号枚举 |
| 非法参数（limit/cursor/title 长度） | 422 + FastAPI 风格 detail | 前端零改动 |

### 4. 数据访问：JdbcTemplate 显式 SQL（非 JPA）
表结构真源在 Python SQLAlchemy models，JPA 会引入第二份 ORM 映射，字段漂移
风险翻倍。显式 SQL 与 Python 逐条对齐（列表的 GROUP BY MAX(id) 防 N+1、
cursor 分页 fetch limit+1 判 has_more、单事务软删）。

### 5. 本地开发零 Docker：H2 内存库（local profile）
`--spring.profiles.active=local` → H2（MySQL 兼容模式）+ 种子数据 + mock AI。
生产 profile 仍强制真实 MySQL + fail-fast 的 JWT_SECRET（local 的开发密钥
只在 local yml 中）。**本地开发不依赖 Docker，代码就绪后统一打包进容器。**

### 6. 数据权属：平台层是唯一写入者
- 网关转发带 `X-Platform-Persist: java`，AI 服务据此跳过自身写库（保留 Redis 热历史）
- 平台层在 **done 帧同步落库**（UPSERT 会话 + 双行消息），并把 `assistant_message_id`
  回填进 done 再转发——前端"按条评价"契约不破
- **残答禁入**：无 done（用户中断/上游断流）不落库，累积器单测锁死
- 直连 AI 服务（不经网关）时保留其原生写库行为，两种部署形态都成立

### 7. AI 服务的独立部署模式
- `PLATFORM_AI_ONLY=true` → AI 服务仅注册内核路由：`/api/chat`、`/api/intent`、
  `/api/channels`、`/api/admin/ingest`（知识库数据面）；平台业务路由
  （auth / 会话 / 工单 / 看板 / 商城 / 闲鱼后台）不注册
- 该模式下身份解析**信任网关签发的 JWT claims**（不查 MySQL）：AI 服务无状态、
  可水平扩展；代价是「用户禁用」依赖 token 过期生效（网关侧已拦截，可接受的取舍）
- 默认 `false` 为全量模式，兼容直连部署与既有 1200+ 测试
- 验证方式（确定性，不起服务）：容器内 `app.openapi()['paths']` 对比两种模式
  —— 全量 65 条 / AI-only 13 条，chat+ingest 保留、auth+conversations+products 不注册

### 8. 容器拓扑与网关路由
- 镜像：`java-platform/Dockerfile` 单阶段（本地/CI 产出的 jar → JRE 21 运行），
  非 root 运行，`/healthz` 探活校验 DB 连通
- compose `java-api` 服务（宿主 8081 → 容器 8080），depends_on mysql / ai 服务健康
- nginx 按 location 分配（回滚 = 注释对应 location，落到 `/api/` 兜底）：
  | 路径 | 归属 | 说明 |
  |---|---|---|
  | `/api/auth/*` | Java | 平台层是身份唯一权威 |
  | `/api/conversations*` | Java | 会话/消息真源读取 |
  | `/api/admin/handoff*` | Java | 坐席工单（队列/认领/结单/归档） |
  | `/api/admin/analytics/board`（精确） | Java 转发 → AI 服务 | 平台层验权后原样转发，统计只有 AI 服务一份实现 |
  | `/api/chat`（SSE） | Java 网关 → AI 服务 | 鉴权+落库在 Java，推理在 AI |
  | 其余 `/api/*` | AI 服务 | 商品/区间报表/KPI/闲鱼后台，后续在 Java 补齐 |

## 测试矩阵（为什么这么测）

### 单元测试（18 个，`mvn test`，无需外部依赖）
| 组 | 覆盖 | 为什么 |
|---|---|---|
| JwtServiceTest (8) | 字节级互认金标 / 反向解析 / roundtrip / 错密钥 / 篡改 / 过期 / alg=none / 短密钥 | 跨服务身份互通的最大风险点；金标向量由 Python 侧独立计算（非 Java 自签自验），失败原因各自隔离（篡改≠过期≠签名错） |
| SseFrameParserTest (10) | 半帧跨包 / 多事件一包 / CRLF / 多行 data / 注释行 / id 状态泄漏 / flush / 畸形行 / 前导空格 | SSE 分帧是最易出边界 bug 的纯函数，必须逐场景确定性断言 |
| SseStreamAccumulatorTest (6) | token 拼接 / meta 上下文捕获 / **无 done 不落库** / 空答不落库 / 噪声帧 / done 识别 | 落库内容与时机的正确性全押在这里；残答入库会污染历史且评价指向半截回答 |

### E2E 冒烟（28 项，`scripts/e2e_smoke.sh`）
| 组 | 验证 | 为什么 |
|---|---|---|
| A 鉴权 (10) | 真实登录链路 + Cookie/Bearer 双通道 + ★跨语言互认 | 平台层身份是全架构信任根 |
| B 会话 (10) | CRUD 契约 + 参数边界（limit=0/101、cursor=0、标题超长、越权 404） | 边界值是最可能回归的地方；404 不暴露存在性单独断言 |
| C SSE 网关 (6) | **mock AI 确定性验证**：事件顺序 meta→token→done、内容无损、注释过滤、fail-closed | 不用真实 LLM：花钱/非确定/慢，无法做「预计 vs 实际」断言；mock 隔离出「透传行为」单一变量 |
| E 落库 (8) | done 后自动建会话、token 拼接入库、续聊追加、`assistant_message_id` 回填 | 单一写入者的正确性证明；回填失败会让前端按条评价退化，必须断言 |
| D 真实链路 (2) | 真实 Python /api/chat 经网关可用 | mock 证明透传正确后，真实链路只验证连通性 |

**预计 vs 实际**：全部用例按预期结果断言，实际运行 28/28 PASS（A9 首轮失败
为测试环境密钥配置问题——local profile 开发密钥 vs Python 真实密钥，
属预期内的环境分叉，带真实密钥重启后通过）。

## 运行

```bash
# 单元测试
java -Dmaven.home=E:/apache-maven-3.9.6 -Dclassworlds.conf=E:/apache-maven-3.9.6/bin/m2.conf \
  "-Dmaven.multiModuleProjectDirectory=<项目根目录>/java-platform" \
  -classpath E:/apache-maven-3.9.6/boot/plexus-classworlds-2.7.0.jar \
  org.codehaus.plexus.classworlds.launcher.Launcher \
  -s java-platform/mvn-settings.xml -f java-platform/pom.xml test

# 本地启动（H2 + 种子：admin/local123、buyer1/user123）
java -jar java-platform/target/cs-platform-0.1.0.jar \
  --spring.profiles.active=local --server.port=8081 \
  --platform.ai.base-url=http://127.0.0.1:18081

# E2E（需先起 mock AI）
python \
  java-platform/scripts/mock_ai_server.py &
bash java-platform/scripts/e2e_smoke.sh
```

## 后续规划

- [x] 消息落库在平台层（done 帧同步落库 + 回填 id）
- [x] AI 服务独立部署模式（PLATFORM_AI_ONLY，13 条内核路由 + claims 身份）
- [x] 容器化 + nginx 路由分配（auth/conversations/chat）
- [x] 坐席工单 API（队列/认领/结单/归档，20 项 E2E）
- [x] 数据看板网关（11 组图表；曾有两份聚合实现，已收敛为 AI 服务单源 + 逐字段对账脚本）
- [ ] 商品橱窗 / 区间报表 / KPI 大盘 / 闲鱼后台 → 路由全量收拢

## 本地无模型额度时：mock LLM 模式

真实大模型欠费/断网时，用自写 mock 顶替「生成」这一步，链路其余部分全真实：

```bash
# 1. 启动 mock LLM（OpenAI 兼容，按关键词返回带引用的售后话术）
python scripts/mock_llm_server.py 18082

# 2. 叠加 mock 覆盖启动 api（意图/RAG/引用/落库真实，仅生成是假的）
cd deploy && docker compose --env-file .env.dev \
  -f docker-compose.yml -f docker-compose.mock.yml up -d api

# 3. 回到真实模型：去掉 -f docker-compose.mock.yml 重新 up 即可
```

> 与 `java-platform/scripts/mock_ai_server.py` 的区别：那个替的是整个
> AI 服务（只测网关透传）；这个只替 LLM 生成（可测完整 RAG 链路）。
