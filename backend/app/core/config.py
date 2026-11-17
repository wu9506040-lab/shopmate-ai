"""
应用配置 - 统一从环境变量读取（pydantic-settings）

按 §6 规则：core/ 层核心能力，提供全局配置
- Docker 容器内：不读 .env，靠 docker-compose.yml 的 environment 注入
- 本地开发：自动读项目根 .env（如果有）
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- MySQL ----
    # 强制从环境变量读取，禁止硬编码凭据
    # 启动时由 _validate_database_url() 校验：未设置或包含已知占位符密码则 ValueError
    DATABASE_URL: str = ""

    # ---- JWT ----
    # 强制从环境变量读取，禁止占位符默认值
    # 启动时由 _validate_jwt_secret() 校验：未设置或使用占位符则 ValueError
    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 24

    # ---- bcrypt ----
    BCRYPT_ROUNDS: int = 12

    # ---- 应用 ----
    APP_ENV: str = "dev"

    # ---- Cookie ----
    # production 必须 True（要求 HTTPS），dev False 方便 curl/Postman 调试
    COOKIE_SECURE: bool = False
    COOKIE_SAMESITE: str = "lax"
    COOKIE_NAME: str = "cs_token"
    COOKIE_MAX_AGE: int = 86400  # 24h，与 JWT_EXPIRE_HOURS 对齐

    # ---- 外部服务 ----
    REDIS_URL: str = "redis://localhost:6379/0"
    VECTOR_COLLECTION: str = "knowledge_base"
    MILVUS_URI: str = "http://localhost:19530"

    # ---- 向量库后端（Redis 8 Query Engine / Milvus 双后端，灰度切换用）----
    # milvus: 走 pymilvus standalone（原路径）；redis: 走 Redis 8 FT.* HNSW 索引
    VECTOR_BACKEND: str = "milvus"
    # Redis 后端的 key 前缀与索引名（索引 = {prefix}idx，key = {prefix}{id}）
    VECTOR_REDIS_PREFIX: str = "kb:"

    # ---- Prompt 加载器 ----
    # 相对 backend 根目录的路径；绝对路径可直接覆盖
    # 例如：容器内 /app/config/prompts → PROMPT_DIR=/app/config/prompts
    # docker compose 默认 cwd=/app，相对路径 "config/prompts" 即可（推荐）
    PROMPT_DIR: str = "config/prompts"

    # ---- 业务规则加载器（）----
    # 相对 backend 根目录的路径；绝对路径可直接覆盖
    # 例如：容器内 /app/config/business_rules → BUSINESS_RULES_DIR=/app/config/business_rules
    # 业务规则启动时加载，不参与热更新（roadmap §3.5：改规则需重启服务）
    BUSINESS_RULES_DIR: str = "config/business_rules"

    # ---- LLM (DashScope OpenAI 兼容) ----
    QWEN_API_KEY: str = ""
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    QWEN_MODEL: str = "qwen-max"
    # 意图分类/改写这类高频小任务用的轻量模型。
    # 用 settings 而不是 os.environ：pydantic-settings 读 .env 不会写进 os.environ，
    # 之前 os.environ.get("INTENTION_MODEL") 在 .env 里配了也读不到（只有测试用
    # 真实环境变量才生效）。
    # 默认 qwen-flash 而非 qwen-turbo：实测 turbo 免费额度 QPS 极紧（连续 6 次
    # 全 429），而 flash 同价位、速度相当且限流更宽松。
    INTENTION_MODEL: str = "qwen-flash"
    # 支持 chat 与 embedding 使用不同 LLM 提供商（向量空间绑定原提供商，不可混用）
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_BASE_URL: str = ""

    # ---- 日志 ----
    LOG_LEVEL: str = "INFO"

    # LangGraph 开关（默认关闭，需手动设 true 启用）----
    # 收口语义：true → RefundFlow（LangGraph 主引擎）；false → 降级引擎 V2
    # （RefundService 复合工具 + 政策 RAG，不依赖 LangGraph/Checkpointer——
    # 老部署兼容档）。图引擎异常时 refund_flow.py 保险丝也会落到 V2，
    # 每次兜底计 metrics v3_engine="v2"（V2 退役条件见 refund_handler 模块注释）。
    USE_LANGGRAPH_REFUND: bool = False

    # ---- Demo 访客登录（公开 demo 站点用）----
    # True 时开放 /api/public/demo-account 一键体验；生产默认开启（这是简历展示用项目）
    # 安全策略：每次创建一个隔离的 visitor_xxx 用户，30 天过期自动清理
    ENABLE_DEMO_LOGIN: bool = True

    # ---- 速率限制（开放 demo 必加，防止被刷 token）----
    RATE_LIMIT_PER_MINUTE: int = 30

    # ---- CORS（不允许 "*"+credentials 组合，强制显式 origin 白名单）----
    # dev: ["http://localhost:5173","http://127.0.0.1:5173","http://120.79.27.124:5173"]
    # prod: ["http://120.79.27.124:5173"]
    # 用逗号分隔的字符串写入环境变量，逗号分隔解析成 list
    CORS_ALLOWED_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ---- 两阶段检索：粗排 top-K → LLM rerank → 精排 top-k ----
    # 开启后 PolicyService.search_policy 走 Vector top-15 → rerank → top-3
    # 关闭则保持原样（直接 Vector top-3）
    # 成本：每次 policy_query 多 ~500 token（rerank 调用）
    # 收益：长尾召回 top-1 准确率 +15-25%
    USE_RERANK: bool = True
    # 粗排候选数（rerank.py 单 prompt 上限 15）
    RERANK_CANDIDATE_TOP_K: int = 15
    # 召回为空时重试一次（原 query 必留一路，改写只追加确定性名词）
    RAG_RETRY_ON_EMPTY: bool = True

    # ---- 检索相似度阈值：按意图分档 ----
    # 一个阈值管所有问题是不对的：
    #   政策类答错代价最高（退换/运费/发票说错就是客诉），宁可召回空走兜底
    #   闲聊类本来就没有知识库答案，卡太严等于白丢上下文
    # 分档后阈值由 retrieval_threshold.resolve() 按 query 意图选出
    RETRIEVAL_THRESHOLD_ENABLED: bool = True
    # 政策档 0.52 试过：会把双11促销的整段候选挤出向量集，RRF 融合后反而
    # 让已过期的 618 文档排到第一（V04 实测回归）。0.48 是实测拐点：
    # 比基线严，又不至于把答案本身卡掉。
    RETRIEVAL_THRESHOLD_POLICY: float = 0.48     # 政策/售后/交易：从严
    RETRIEVAL_THRESHOLD_FACT: float = 0.45       # 尺码/面料/洗护等商品事实：中等
    RETRIEVAL_THRESHOLD_DEFAULT: float = 0.40    # 泛问：保持原基线
    RETRIEVAL_THRESHOLD_CHITCHAT: float = 0.28   # 寒暄/闲聊：放宽
    # 空召回重试那一轮整体降一档（只降阈值，不改问题本身）
    RETRIEVAL_THRESHOLD_RETRY_RELAX: float = 0.06
    RETRIEVAL_THRESHOLD_FLOOR: float = 0.22      # 降档下限，防止退化成"什么都召回"
    # 句级答案自检（纯规则，0 token；判不过只替换那一句）
    ENABLE_ANSWER_VERIFY: bool = True

    # ---- 回答分段（连发/多问题合并后，一整块太长读不下去）----
    # 触发条件：单段累计超过 MAX_CHARS 且正好落在句末标点 → 插一个空行分段。
    # 只在句末切，绝不在句中切（切在句中等于把一句话劈成两半）。
    ANSWER_SEGMENT_ENABLED: bool = True
    ANSWER_SEGMENT_MAX_CHARS: int = 120      # 单段软上限（汉字数）
    ANSWER_SEGMENT_MAX_SEGMENTS: int = 3     # 最多切成 3 段，再多就合并着说

    # ---- 空回复保险丝 ----
    # 本轮一个可见字符都没发出时，出口强制补这一句。
    # 不依赖任何业务分支，只检查"出口有没有东西"，所以未来新加路径也漏不出去。
    ENABLE_EMPTY_ANSWER_GUARD: bool = True

    # ---- 混合检索：vector (dense) + BM25 (sparse) + RRF ----
    # 开启后 PolicyService.search_policy 走 hybrid 路径：
    #   vector top-K + BM25 top-K → RRF 融合 → rerank → top-k
    # 关闭则仅用 vector 检索（保持单一 dense 路径）
    # 成本：BM25 索引启动时一次性构建（<1s for 67 docs），查询时几乎零开销（纯 Python）
    # 收益：解决"关键词精确命中但语义不相似"的召回盲区
    # 典型案例：用户搜 "HKZ26946 Pro Max 续航"，BM25 召回精确含型号的 doc，
    #          vector 召回含"续航"语义的 doc，融合后两者都进 top
    USE_HYBRID_BM25: bool = True
    # BM25 候选数（与 vector 粗排一致，保证融合时两路平衡）
    BM25_TOP_K: int = 15
    # RRF k 常数（Cormack 2009 论文推荐 60）
    RRF_K: int = 60

    # A4: Multi-Query 检索增强 ----
    # 启用后 query_rewriter 输出 N 路变体 → policy_service RRF 融合检索
    # 默认 false（灰度用），改 settings 不需改代码
    ENABLE_MULTI_QUERY: bool = False
    # 变体数量上限（N ≤ 3 控 Vector 调用次数 ×N 的成本）
    MULTI_QUERY_COUNT: int = 3
    # 触发条件（MVP: coref_only；扩展: any / never）
    MULTI_QUERY_TRIGGER: str = "coref_only"

    # A5: Multi-Query 并行检索 ----
    # 启用后 search_multi_policy 用 ThreadPoolExecutor 并行调用 N 路 search_policy
    # 默认 true（性能优化：3 路串行 → 并行，~2.9x 加速）；关掉回退串行（debug 用）
    MULTI_QUERY_PARALLEL: bool = True
    # ThreadPoolExecutor max_workers（与 MULTI_QUERY_COUNT 默认对齐；per-request executor）
    MULTI_QUERY_WORKERS: int = 3

    # A8: 融合后 rerank ----
    # True 时 search_multi_policy 走 N 路粗排 → RRF 融合 → 1×rerank → top-k
    #   - LLM rerank 调用次数：N → 1（默认 3 → 1，省 ~66% token）
    #   - rerank 视角：单路内部 → 全局融合候选（信息更全）
    # False 时回退 A5 路径：每路各自 rerank → RRF 融合（debug / A/B 对比用）
    MULTI_QUERY_FUSE_FIRST_RERANK: bool = True

    # ---- P2 长程记忆：跨 session 用户画像 ----
    # 启用后 orchestrator 每轮加载 user_profiles，注入到 LLM prompt 的 context_block 之后
    # 默认 false（灰度用），未启用时所有 profile_service 调用短路返空
    ENABLE_USER_PROFILE: bool = False
    # profile_block 注入 prompt 的硬上限（防 prompt 膨胀；与 prompt_assembler MAX_PROFILE_PROMPT_LEN 对齐）
    USER_PROFILE_PROMPT_MAX_LEN: int = 200

    # ---- L3 跨会话长期事实 ----
    # 与 ENABLE_USER_PROFILE（自由文本画像，已废弃）不是一回事：
    # L3 只存白名单槽位，带查看/删除权和 90 天 TTL。
    # 关闭时读写两侧都短路，且 GET/DELETE /api/me/profile 仍可用（删历史数据）。
    ENABLE_LONG_TERM_MEMORY: bool = True
    L3_TTL_DAYS: int = 90

    # ---- Prompt 版本管理（manifest 模式）----
    # 启用后 prompt_loader 支持 load(name, version=...) 多版本接口
    # 关闭则所有调用走 manifest 的 default_version（行为与旧版完全一致）
    # 默认 false（灰度用），需要时再开 true
    ENABLE_PROMPT_VERSIONING: bool = False

    # ---- C2: Agent Function Calling 框架 ----
    # 语义收敛为"是否启用全科兜底"——不再整体接管（旧两套引擎
    # 灰度切换已合成一套多 Agent）。True 时 method=default / 未注册意图的
    # 开放问题路由到 GeneralistAgent（agent_runner FC 编排，工具见
    # tools/registry.py 的 lookup_order / search_product / search_policy）；
    # False 时开放问题保持旧行为回落 policy_expert。
    ENABLE_AGENT_FC: bool = False
    # Agent 主循环最大工具轮次（防 LLM 死循环/成本失控）
    # 经验值 5：足够覆盖"查订单 → 查商品 → 查政策"三步场景
    MAX_AGENT_TURNS: int = 5
    # 只读工具同参重复调用上限（含首次）。2 = 允许重看一次，第 3 次拦截。
    # 写操作工具（modify_address/auto_delivery/...）不受此值影响：一律只允许一次。
    AGENT_REPEAT_READ_LIMIT: int = 2
    # 连续 N 轮"全部调用都是重复"→ 强制收敛（tool_choice=none 让它基于已有信息作答）
    AGENT_MAX_DUP_TURNS: int = 2
    # Agent 轮次改用流式 FC：边生成边下发（TTFT = 首句生成时间，而非整段生成完）。
    # 关掉即回到"非流式 chat + 逐字伪流式"，行为与改造前完全一致——出问题时
    # 一个开关回滚，不必改代码。provider 无 stream_chat_events 时也会自动退回。
    AGENT_STREAM_FC: bool = True


    # ---- 用户上下文 + 订单 Resolver + 业务流 ----
    # ENABLE_CONTEXT_STORE: ContextService 是否读/写 conversation_contexts 表
    # 关闭时所有 ContextService 调用短路（不读不写），orchestrator 走老路径
    # 默认 true：写侧（turn_recorder）已落地并有单测覆盖，读侧只在订单类意图生效，
    # 空上下文时行为与关闭完全一致，所以默认打开无额外风险。
    # 紧急回滚：置 false 即回到「每轮靠 20 条热历史重新猜订单号」的旧行为。
    ENABLE_CONTEXT_STORE: bool = True
    # ENABLE_ORDER_RESOLVER: OrderContextResolver 是否参与 _handle_order 决策
    # 关闭时 Resolver 返 DIRECT_ANSWER（orchestrator 走老路径 list_user_orders + LLM）
    # 默认 false（灰度用）；开 true 后 _handle_order 走 0/1/N 决策树
    ENABLE_ORDER_RESOLVER: bool = False
    # ENABLE_BUSINESS_FLOW: business_flow 工厂是否启用（M14 §10 阶段 3 占位）
    # 关闭时 refund_query / order_query 走原有分派路径，不走显式状态机
    # 默认 false（灰度用）
    ENABLE_BUSINESS_FLOW: bool = False
    # SSE_CARD_V2: 是否在 SSE meta 事件携带 OrderCard payload
    # 关闭时 orchestrator 不填 meta.card 字段（向后兼容老前端）
    # 默认 true（M14 阶段 2 落地的 SSE 协议扩展，前端可选订阅）
    SSE_CARD_V2: bool = True
    # ENABLE_ESCALATION_HANDOFF: 转人工兜底（M14 V3）
    # 开启后：Agent 异常 / 用户说"转人工" → 走 EscalationService，handoff payload 推到 SSE meta
    # 关闭时：所有 handoff 触发降级为"系统繁忙，请稍后再试"文本
    # 默认 false（灰度用）；demo 时可开 true
    ENABLE_ESCALATION_HANDOFF: bool = False

    # 用户上下文预加载 ----    # 开启后 chat.py 入口调用 user_context_preloader.preload(user_id)，
    # 加载最近 5 单（含产品）+ 用户画像，注入 LLM prompt 的 context_block 末尾
    # 关闭则不加载（不影响主流程）
    # 默认 false（灰度用），验证完再开 true
    ENABLE_USER_CONTEXT_PRELOAD: bool = True
    # 预加载订单上限（与 user_context_preloader.MAX_RECENT_ORDERS 对齐）
    USER_CONTEXT_PRELOAD_MAX_ORDERS: int = 5

    # ---- 订单多渠道查询（自有商城 + 淘宝/抖店/…） ----
    # ORDER_CHANNELS: 参与查询的渠道，逗号分隔；local 强制保留（自有商城是兜底）。
    # 语义变化：本地库查不到时不再直接判定「没有订单」，而是并发去各平台问一遍，
    #   只有所有渠道都查通且都是 0 单，才能说「确实没有」。有渠道没查成，
    #   话术会如实说「没查到 / 接口不通」，而不是「您没有订单」。
    # 未配置凭证的外部渠道标记 DISABLED，不发请求，不影响对话延迟。
    ORDER_CHANNELS: str = "local"
    # 单渠道查询超时（秒）。外部平台慢时会吃满这个时间，默认压到 2s ——
    # 宁可如实说「没查到」，也不要为了等一个平台把整轮对话卡住。
    ORDER_CHANNEL_TIMEOUT: float = 2.0
    # 淘宝开放平台（TOP）凭证：三件套缺任一则该渠道不启用
    TAOBAO_APP_KEY: str = ""
    TAOBAO_APP_SECRET: str = ""
    TAOBAO_SESSION_KEY: str = ""
    # 抖店开放平台凭证：缺任一则该渠道不启用
    DOUYIN_APP_KEY: str = ""
    DOUYIN_APP_SECRET: str = ""
    DOUYIN_SHOP_ID: str = ""

    # ---- 三部分架构（前端 | Java 中台 | Python AI）路由收窄开关 ----
    # False（默认）：保持全量路由，兼容直连部署与历史测试。
    # True：Python 只注册 AI 内核路由（chat/intent/channels/知识库入库），
    #       平台业务路由（auth/会话/工单/看板/商城/闲鱼后台）由 Java 中台接管，
    #       且身份解析改为「信任网关签发的 JWT claims」（不查 MySQL，AI 服务无状态）。
    PLATFORM_AI_ONLY: bool = False

    # 闲鱼 MCP 渠道接入 ----
    # 开启后 tools/registry.py 注册 5 个 xianyu_* 工具（list_conversations /
    # conversation_history / get_item / list_my_items / send_message）
    # 通过 MCP stdio 协议连 goofish-mcp 子进程，调闲鱼 IM/商品 API
    # 关闭则所有 xianyu_* 工具返 {"error": "xianyu_tools_disabled"}
    # 默认 false（灰度用）；需同时配置 GOOFISH_COOKIE_PATH + 装 goofish-cli
    ENABLE_XIANYU_TOOLS: bool = False

    # ---- RAG ingest MySQL 失败回滚 Vector ----
    # 触发场景：upsert_points 成功 → upsert_knowledge_meta 失败 → 孤儿点残留
    # 开启后：自动 delete_points(chunk_ids) 回滚；rollback 失败仅 log warning（不掩盖 MySQL 错误）
    # 关闭则保留原行为：静默成功 + warn log（与 M14 V3 前一致）
    # 默认 True（数据一致性优先）；切 False 即可观察老行为用于 A/B 对比
    RAG_ROLLBACK_ON_MYSQL_FAIL: bool = True

    # ---- RAG chunk_id 基于内容 hash 稳定 ----
    # 旧逻辑：point_id = uuid5(source + ":" + i)，基于下标
    #   问题：source 中增/删 chunk → 后续所有 ID 整体偏移 → 删除旧点找不到新 ID → 不幂等
    # 新逻辑：point_id = uuid5(source + ":" + chunk_hash[:32])，基于内容 sha256
    #   收益：同一 source 的同一段文本永远得到同一 ID → 重跑幂等、增量更新安全
    # 开启后：新数据用新 ID（与旧数据共存，不影响检索）
    # 关闭则回退旧逻辑（仅用于 A/B 对比）
    # 默认 True（数据稳定性优先）；历史数据可用 scripts/migrate_chunk_id.py 处理
    RAG_CHUNK_ID_BY_CONTENT_HASH: bool = True

    # ---- （2.1'）：RAG 切片策略 ----
    # "sentence" = 句边界贪心切片（默认：chunk 以句号/问号/换行为界收尾，
    #              overlap 整句携带，不再切断从句——2026-10-10 审计把 500 硬切
    #              列为与竞品同型短板）
    # "char"     = 2026-10 之前的字符级滑窗（保留作 A/B 对照与回退开关）
    RAG_CHUNK_STRATEGY: str = "sentence"

    # ---- 给 LLM 呈现的单条资料字符上限 ----
    # 原为硬编码 500。尺码表/价目表这类切片本身有 600 字上限，截到 500 会把表格后半截
    # （斤制速查的 2XL/3XL 行）整段切掉，模型看不到就靠相邻码硬凑——
    # 实测「180cm/180斤」把它答成「3XL 身高 170~180cm」，那是 XL 的范围。
    # 现调到 1000：600 字的切片可完整呈现，又不会让 5 条上下文撑爆 prompt。
    # ⚠️ _format_policy_docs 与 _build_meta_contexts 必须同口径（自检按 preview 取白名单）。
    POLICY_CTX_CHAR_LIMIT: int = 1000

    # ---- RAG BM25 索引后台异步重建 ----
    # 开启后 ingest 完成后触发后台线程重建 BM25 索引（不阻塞主流程）
    #   - 收益：避免首次 BM25 检索触发懒加载导致 1-3s RT spike
    #   - 关闭则保留懒加载（首次调用时构建，<100KB 数据集耗时 <1s）
    # 关闭则 BM25 索引只在首次 bm25_search 调用时懒构建（与原行为一致）
    # 默认 True（响应优先）；切 False 可观察懒加载行为（A/B 对比）
    RAG_BM25_EAGER_BUILD: bool = True

    # ---- RRF 类型加权（按 doc_type 给最终 rrf_score 加权重）----
    # 业务策略：policy（政策）类文档加权 > faq > product（商品信息）
    #   - policy: 1.2（用户最关心"能不能退/怎么退"，policy 命中应优先）
    #   - faq: 1.0（中性，按 RRF 排名）
    #   - product: 0.9（次要，避免商品信息压制政策命中）
    # 关闭则空 dict，所有 doc 一视同仁（与原 RRF 行为一致）
    # 收益：政策类 query 召回 top-1 准确率 +10-20%（场景：用户问"运费险怎么买"）
    RAG_TYPE_BOOST: dict = {
        "policy": 1.2,
        "faq": 1.0,
        "product": 0.9,
        # （2.1'）：词表对齐——ingest 归一化产出 promo 类目（促销活动），
        # 修复审计发现的"实际 doc_type 13 种 vs boost 键 3 个→加权全部落空"
        "promo": 1.0,
    }

    # ---- KnowledgeSource 抽象 ----
    # 知识库接入类型（默认 vector；预留 elasticsearch / filesystem 等）
    # 知识源实现见 app/rag/vector_impl.py

    # ---- LLM 客户端：retry + 指数退避 + 断路器 ----
    # 解决现网抖动：DashScope 5xx / 网络超时 / 偶发 429 时不直接降级到兜底文本
    # 而是重试 N 次（指数退避 + 抖动），仍失败则断路器开路避免雪崩
    # 可重试错误：429 / 5xx / Timeout / ConnectionError
    # 不可重试：400 / 401 / 403（业务错，重试无意义）
    # 总尝试次数 = LLM_MAX_RETRIES + 1（含首次）
    LLM_MAX_RETRIES: int = 3
    # 退避基础延迟（秒）：wait = base * 2^attempt + jitter(0-50%)
    LLM_RETRY_BASE_DELAY: float = 1.0
    # 断路器：连续失败 N 次开路
    LLM_CIRCUIT_FAILURE_THRESHOLD: int = 5
    # 断路器开路后多久进入 HALF_OPEN 探活
    LLM_CIRCUIT_RECOVERY_TIMEOUT: float = 60.0

    # 请求级超时预算 ----
    # /chat 全链墙钟预算；client 层每次 HTTP 调用超时= min(分段上限, 剩余预算)，
    # 重试/排队/流式推进前过 deadline.check()，超线快速失败走既有降级矩阵。
    CHAT_DEADLINE_SECONDS: float = 90.0
    # 分段单次调用上限（秒）：防单级吃光预算，饿死下游生成段
    LLM_CALL_TIMEOUT: float = 30.0          # 通用 chat/stream 单次上限（原硬编码 30.0）
    RERANK_CALL_TIMEOUT: float = 20.0       # 精排级：超线→保原序降级（已有显形）
    INTENT_CALL_TIMEOUT: float = 10.0       # 意图 LLM 兜底：超线→默认 policy（已有）
    REWRITE_CALL_TIMEOUT: float = 6.0       # 查询改写：超线→用原 query（已有）
    # OpenAI SDK 内建重试清零：项目自有 retry+退避+断路器，SDK 再叠 2 次是
    # "最坏 10 分钟"的根因放大器（30s×3×4 叠加）；单 attempt=单 HTTP，熔断计数才诚实
    LLM_SDK_MAX_RETRIES: int = 0
    # 流式并发闸（stream_dispatcher._LLM_SEMAPHORE）：容量与排队等待上限
    LLM_SEMAPHORE_SIZE: int = 10
    LLM_SEMAPHORE_ACQUIRE_TIMEOUT: float = 15.0
    # Agent 记忆持久化：refund_graph 状态落盘，进程重启可恢复可审计
    AGENT_CHECKPOINTER_TYPE: str = "sqlite"   # sqlite | memory | none
    AGENT_CHECKPOINTER_DB: str = "data/agent_checkpoints.sqlite3"
    # Agent 决策追踪：一轮一行 JSONL，业务价值指标的数据源
    AGENT_TRACE_ENABLED: bool = True
    # 是否把问句/答案正文写进 trace（脱敏+截断后）。badcase 回放要用，
    # 生产若在意数据留存可用 .env 关掉，只保留 query_len / answer_len。
    AGENT_TRACE_STORE_TEXT: bool = True
    AGENT_TRACE_TEXT_MAX: int = 600
    AGENT_TRACE_DIR: str = "data/agent_traces"
    # 质检（QA）：规则层全量扫、LLM 层按比例抽样打分。
    # 比例别调太高：一条 = 一次上游调用，5000 会话/天 × 10% 已经是每天 500 次。
    QA_ENABLED: bool = True
    QA_BATCH_LIMIT: int = 500
    QA_SAMPLE_RATIO: float = 0.1
    QA_MIN_ANSWER_CHARS: int = 4
    # 受控写操作审批门控：确认票据 TTL（秒），过期需重新确认
    AGENT_TICKET_TTL_SECONDS: int = 600
    # 双意图双专家协作：一句话两个意图时，主专家答完补答次意图
    ENABLE_DUAL_INTENT_DISPATCH: bool = False
    # 虚拟商品自动发货：卡密库存与交付记录的 SQLite 路径
    DELIVERY_DB: str = "data/delivery.db"


settings = Settings()


def _validate_jwt_secret() -> None:
    """启动时校验 JWT_SECRET：必须 ≥32 字符且不是占位符

    为什么：占位符或弱密钥等于无签名校验，攻击者可伪造任意 token
    """
    _PLACEHOLDERS = {
        "change_me_at_least_32_chars_random_xxx_string_here",
        "",
        "secret",
        "changeme",
    }
    secret = settings.JWT_SECRET
    if secret in _PLACEHOLDERS:
        raise ValueError(
            "JWT_SECRET 未设置或使用占位符默认值。\n"
            "请在 deploy/.env.dev（或部署环境）设置 JWT_SECRET=<至少 32 字符随机字符串>\n"
            "生成命令：python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if len(secret) < 32:
        raise ValueError(
            f"JWT_SECRET 长度 {len(secret)} < 32 字符，强度不足。"
            "请用 secrets.token_hex(32) 生成。"
        )


_validate_jwt_secret()


def _validate_database_url() -> None:
    """启动时校验 DATABASE_URL：必须设置且不含已知占位符密码

    为什么：默认值带密码会让 docker-compose 不显式注入时也能"看似启动"，给运维错觉

    注意：dev 环境允许 .env.dev 里的弱密码占位符（dev_user_2026 等），
         因为 .env.dev 不进 Git 且明确写"开发用"。
         prod 环境必须用 secrets manager 注入真实强密码，禁止占位符。
    """
    _PLACEHOLDER_PWD_FRAGMENTS_DEV = ()  # dev 环境允许 .env.dev 弱密码
    _PLACEHOLDER_PWD_FRAGMENTS_PROD = ("dev_user_2026", "rootpass_cs_2026", "change_me", "password", "secret")
    url = settings.DATABASE_URL
    if not url:
        raise ValueError(
            "DATABASE_URL 未设置。\n"
            "请在 deploy/.env.dev（或部署环境）设置：\n"
            "DATABASE_URL=mysql+pymysql://cs_user:<密码>@mysql:3306/customer_service?charset=utf8mb4"
        )
    # prod / production 环境必须严格：禁止任何占位符密码
    if settings.APP_ENV in ("prod", "production"):
        for frag in _PLACEHOLDER_PWD_FRAGMENTS_PROD:
            if frag in url:
                raise ValueError(
                    f"APP_ENV={settings.APP_ENV} 环境下，DATABASE_URL 含占位符密码 '{frag}'。"
                    "生产环境必须从 secrets manager 注入真实密码。"
                )


_validate_database_url()
