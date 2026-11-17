# 评测与运维脚本

> 服饰电商客服中台（猫与小鼠 2026AW）的本地验证工具集。
> Python 统一使用 `backend/.venv/Scripts/python.exe`；服务需先由
> `backend/run_local.sh`、`java-platform/run_local.sh` 与前端 vite 拉起。

## 评测

| 脚本 | 用途 |
|---|---|
| `eval_seven.py` | 七维评测：检索 / 引用 / Agent / 稳定性 / 成本 / 安全 / 端到端 |
| `run_eval_local.sh` | 本地跑七维评测的入口（自动切到 backend 加载 `.env` 与 venv） |
| `replay_evalset.py` | 重放黄金测试集，把「问题 → 服务回答原文 → 引用」落盘 `data/replay_answers.json` |
| `eval_hitk.py` | 检索 hit@K / 延迟评测 |
| `eval_agent_fc.py` | Agent Function Calling 决策质量评测（mock / live 双模式） |

## 全链路验证

| 脚本 | 用途 |
|---|---|
| `e2e/smoke_test.py` | 10 步买家全链路：注册 → 商品 → 下单 → 付款 → 发货 → 重复发货 409 → SSE 聊天 → 落库 → 管理台 |
| `e2e/boundary_test.py` | 16 项边界与安全：参数校验、越权、超卖、路径遍历、超大 Cookie |

两个脚本默认走 `http://127.0.0.1:5173`（浏览器真实路径）。

## 数据与知识库

| 脚本 | 用途 |
|---|---|
| `ingest_kb.py` | 把 `docs/fashion_kb/` 灌入向量库 |
| `doc_parsers.py` | 知识库文档解析与语义切分 |
| `seed_fashion_data.py` | 按店铺商品页生成 57 个服饰尺码 SKU |
| `mock_llm_server.py` | 本地生成替身（OpenAI 兼容，默认 18082），无模型额度时供全链路验证 |

## 闲鱼渠道

| 脚本 | 用途 |
|---|---|
| `run_xianyu_daemon.py` | 渠道常驻进程入口 |
| `watchdog_xianyu_daemon.py` | 守护与自愈 |
| `extract_xianyu_cookies.py` / `.ps1` / `parse_xianyu_cookies.py` | 凭据提取与解析 |
| `xianyu/` | Windows 服务安装 / 卸载与说明 |

## CI

| 脚本 | 用途 |
|---|---|
| `ci_env_diff.py` | 部署环境变量一致性检查 |

## 注意

- `/api/chat` 有 30 次/分钟固定窗口限流；连续跑多个评测脚本之间需留足窗口。
- `data/` 下的评测报告与重放原文不入库。
- 生成环节在无真实模型额度时由 `mock_llm_server.py` 承担，评测结论只覆盖检索 / 引用 / 链路，不代表真模型质量。
