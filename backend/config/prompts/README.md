# Prompt 配置目录

业务提示词与规则集中存放，启动时由 `app/services/config_loader.py` 与
`app/services/prompt_loader.py` 加载，修改后需重启服务。

| 文件 | 用途 |
|---|---|
| `agent_v1.yaml` | 客服主提示词（引用标签、尺码口径、退换政策版本） |
| `agent_fc.yaml` / `agent_fc_v1.yaml` / `agent_fc_v2.yaml` | Function Calling 模式提示词 |
| `intent.yaml` | 意图分类提示词 |
| `xianyu/after_sale.yaml` | 闲鱼渠道售后话术 |

业务规则（意图词表、护栏阈值、售后模板）见 `config/business_rules/`。
