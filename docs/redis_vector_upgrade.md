# 智能客服：Redis 8 向量引擎替换 Milvus 方案

> 2026-11-17 定稿。目标：用 Redis 8 Query Engine 替换 Milvus standalone，
> 下线 Milvus 容器，全程可回滚。

---

## 一、为什么做这件事

| 现状 | 问题 |
|---|---|
| 知识库仅 **186 条向量**（1024 维 COSINE） | Milvus 为亿级设计，纯属大炮打蚊子 |
| Milvus standalone 容器常驻（约 1.5GB+ 内存） | 多养一个中间件：备份、监控、升级都是负担 |
| Redis 本来就在链路里（缓存/会话） | Redis 8 已把向量搜索合入核心，加一种用法不加一个系统 |

**数据规模账**：186 条 × 1024 维 × 4 字节 ≈ **0.7MB**。就算知识库涨到 10 万条也只有
400MB，远在 Redis 单机舒适区内。

## 二、选型结论：Query Engine（Hash + HNSW），不用 Vector Sets

Vector Sets（2025.4 新数据类型）只做"纯相似检索"，不支持：

- 按 source/channel 过滤检索（我们的 `search(channel=...)` 用到）
- 按 source 批量删除（delete-before-write 原语，重灌文档靠它）
- 分页全量遍历（BM25 全量拉取靠 `scroll`）

Query Engine 把向量作为字段存进 Hash（和 8 个标量字段同存），FT.SEARCH 支持
KNN + 标量过滤，是"带标签知识库"的正确姿势。

## 三、现状盘点（改造面）

**唯一触碰点**：`backend/app/clients/vector_db.py`，业务代码只 import 它的
模块级函数。调用方 11 处（vector_impl / bm25_index / policy_service /
rag.ingest / rag.knowledge / rag.pipeline / kb_ops.diagnose / main.health）。

**对外 API 及语义**（Redis 实现必须逐一对齐）：

| API | 现语义 | Redis 8 等价实现 |
|---|---|---|
| `ensure_collection(name, size)` | 建 collection，HNSW M=16 efC=128 COSINE | `FT.CREATE {prefix}:idx ON HASH ... SCHEMA vector VECTOR HNSW 8 TYPE FLOAT32 DIM 1024 DISTANCE_METRIC COSINE M 16 EF_CONSTRUCTION 128` |
| `upsert_points(points)` | points: {id, vector, payload} | key=`kb:{id}`，`HSET` vector 存 float32 bytes（`struct.pack`），payload 键值照存 |
| `search(vec, top_k, channel, score_threshold)` | COSINE 距离，**score 越小越像**，threshold 以下剔除 | `FT.SEARCH kb:idx "*=>[KNN $k @vector $v AS score]"` + `@channel:{...}` 过滤；返回 `score` 字段 |
| `delete_points(ids)` | 按 id 删 | 直接 `DEL kb:{id}...`（id 即 key，最简单） |
| `delete_source_points(source)` | 按 source 精确匹配删 | `FT.SEARCH @source:{val}` 圈 key → 批量 DEL。**注意**：source 含路径特殊字符，TAG 语法需转义（`{}` 内 `\` 转义 `,;<>[]{}` 等分隔符） |
| `scroll(name, limit, offset, with_vectors)` | 分页遍历，返回 (records, next_offset) | `FT.SEARCH` LIMIT offset/limit；with_vectors 时 `HGET` vector 字段反解 |
| `retrieve(name, ids)` | 按 id 批量取 | `HGETALL` 逐个/`FMGET` |
| `get_collection_info(name)` | {vectors_count, vector_size} | `FT.INFO`（num_docs / DIM） |
| `health_check(timeout)` | ok/latency/points_count/circuit | `FT.INFO` 包一层，断路器复用 |
| `get_client()` 裸透传 | ingest/diagnose 特殊需求 | Redis 实现暴露同等方法（scroll/retrieve/get_collections/create_collection_if_missing） |

**关键语义对齐点**（实测确认）：
- **score 语义**：Milvus COSINE 返回的是**相似度**（自向量=1.0，越大越像；
  探针实测：A 方向 query 下 A 点 score=1.0、正交 B 点 score=0.0），业务阈值
  0.28~0.48 是"最低相似度线"。Redis KNN 的 score 是**距离**（自向量=0），
  Redis 后端换算 `sim = 1 - distance` 对齐，threshold 逻辑与排序方向两侧一致
- **score_threshold 剔除发生在返回前**：两侧在组装 hits 时过滤，保持一致

## 四、实施五步

### ① 镜像升级（10 分钟，零风险）
`deploy/docker-compose.yml`：`redis:7-alpine` → `redis:8-alpine`。
RDB 格式 8 兼容 7，缓存数据无缝续用。起服务后 `INFO server` 确认 8.x。

### ② 双后端抽象（约 1 天，核心工作）
`vector_db.py` 改为按 `settings.VECTOR_BACKEND`（`milvus` 默认 / `redis`）分发：
- 公共层保留：断路器、metrics、threshold 过滤、`__all__` 签名不变
- `MilvusStoreClient` 原样保留
- 新增 `RedisVectorClient`：实现上表全部方法（redis-py 5.2.1 已支持 8 的查询引擎命令）

新增配置：`VECTOR_BACKEND: str = "milvus"`、`VECTOR_REDIS_PREFIX: str = "kb:"`。

**测试**：新增 `tests/test_vector_db_redis.py`——用 fakeredis 或真 Redis 8 容器跑
全部 10 个方法 + 变异验证（故意错一个过滤条件断言测试变红）。

### ③ 数据迁移 + 对拍（半天）
- 迁移脚本 `scripts/migrate_milvus_to_redis.py`：Milvus scroll 全量（186 条）→
  逐条 HSET → `FT.INFO` 对账条数
- **对拍脚本** `scripts/compare_vector_backends.py`：取评测集里的真实 query，
  两个后端各跑 top-10，对比 ID 重合率 + score 差值。
  **放行标准：重合率 ≥ 95% 且 score 差 < 0.01**，不达标不切换

### ④ 灰度切换（观察 1-2 天）
`VECTOR_BACKEND=redis` 重启 → 跑完整回归（pytest 全量 + 前端点检问答质量）→
观察日志延迟。异常一行配置切回 milvus（两个后端并存期间 Milvus 数据不动）。

### ⑤ 下线收尾
稳定一周后：compose 删 milvus 容器（省内存）、requirements 删 pymilvus、
清理 `MILVUS_URI` 配置、更新 README 架构图。
Milvus 数据卷保留一个月兜底再删。

## 五、风险与对策

| 风险 | 概率 | 对策 |
|---|---|---|
| source 值含特殊字符，TAG 过滤语法出错 | 中 | 迁移脚本对 source 做 TAG 转义；对拍覆盖含路径的 source |
| Redis 8 容器升级丢缓存数据 | 低 | RDB 向后兼容；丢了也可全量重建（kb_ops 管道本来就有） |
| HNSW 参数不一致导致召回漂移 | 低 | M/efConstruction 与 Milvus 对齐（16/128）；对拍卡 95% 门槛 |
| scroll 大结果集变慢 | 低 | 现有 186 条无感；真到几万条再谈（那时也早该留专业库了） |
| redis-py 某命令行为差异 | 低 | 变异测试锁行为；灰度期双后端随时切回 |

## 六、验收标准

- [ ] 全量 pytest 通过（含新增 test_vector_db_redis.py）
- [ ] 对拍重合率 ≥ 95%、score 差 < 0.01
- [ ] `VECTOR_BACKEND=redis` 下健康检查 200、60 条压测通过率不低于基线
- [ ] 灰度 1-2 天无向量相关 ERROR 日志
- [ ] Milvus 容器下线后内存释放 ≥ 1GB

## 七、工作量

| 步骤 | 耗时 |
|---|---|
| ① 镜像升级 | 10 分钟 |
| ② 双后端 + 测试 | 1 天 |
| ③ 迁移 + 对拍 | 半天 |
| ④ 灰度 | 1-2 天（挂机观察） |
| ⑤ 下线 | 1 小时 |

**纯动手约 1.5 天。** 附带收益：少养一个 Milvus、简历多一条
「Redis 8 向量引擎替换专用向量库（双后端灰度 + 检索对拍迁移）」。
