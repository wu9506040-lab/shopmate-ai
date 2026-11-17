# 智能客服中台 · 文档总览

> 本项目是面向服饰电商的 AI 客服中台（演示店铺：猫与小鼠 / 2026AW）。

## 目录

| 路径 | 内容 |
|---|---|
| `docs/fashion_kb/` | 客服知识库：尺码指南（2026AW 现行版 + 2025 旧版）、退换货政策（2026.11 新政 + 2026.06 旧版）、发货物流、双 11 活动与已过期 618、面料洗护 FAQ、订单修改规则、商品参数总表 |
| `data/evalsets/evalset_v3_fashion.json` | 68 条黄金评测集（检索 35 / 版本时效 7 / 边界·安全·权限 21 / Agent·稳定性 5） |
| `scripts/` | 知识库灌库、评测、重放、商品数据 seed、mock LLM、端到端测试 |
| `backend/` | FastAPI 服务（意图路由 / RAG / Agent / SSE / 管理台 API） |
| `java-platform/` | Java 中台（鉴权 / 会话 / SSE 网关 / 数据看板） |
| `frontend/` | Vue3 商城、聊天、管理台 |
| `deploy/` | Docker Compose（MySQL / Redis / Milvus / 应用层） |

## 知识库与检索

- 向量库：Milvus standalone（`knowledge_base` 集合，1024 维 COSINE），BM25 + RRF 混合检索
- 有效期过滤：已废止/过期文档不进候选；用户问到时补【已失效】声明
- 灌库：`KB_DIR=docs/fashion_kb python scripts/ingest_kb.py`（增量、幂等）

## 评测

`bash scripts/run_eval_local.sh`（七维评测）/ `bash scripts/run_eval_local.sh replay` 参数见脚本；
七维覆盖 D1 检索、D2 引用、D3 Agent、D4 稳定性、D5 成本、D6 安全、D7 端到端结果。
