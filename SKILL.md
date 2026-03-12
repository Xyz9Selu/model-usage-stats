---
name: model-usage-stats
description: "OpenClaw 模型使用统计（按模型/Provider 统计调用次数与 token）。当用户要查看各模型调用情况、调用次数统计、token 用量汇总、按天/按时间范围统计 OpenClaw 模型使用情况时使用。"
---

# Model usage stats (OpenClaw)

用 OpenClaw 的 session store 做一个“足够准、零侵入”的统计：

- **调用次数（calls）**：按 `sessionId` 去重计数（因为同一 run 可能以多个 key 出现在 sessions 列表里）。
- **token**：优先累加 `inputTokens` + `outputTokens`；如果缺失则退化为 `totalTokens`。

> 注意：这统计的是 OpenClaw 记录到 session store 的“会话运行/任务 run”维度，不是 provider 账单里的 request 次数；但对“哪个模型用了多少、用了几次”非常实用。

## Quick start

```bash
uv run skills/model-usage-stats/scripts/model_usage_stats.py --all-agents
```

常用：

```bash
# 最近 24 小时
uv run skills/model-usage-stats/scripts/model_usage_stats.py --all-agents --since-hours 24

# 最近 7 天（按天汇总）
uv run skills/model-usage-stats/scripts/model_usage_stats.py --all-agents --since-days 7 --by day

# 导出 CSV
uv run skills/model-usage-stats/scripts/model_usage_stats.py --all-agents --since-days 30 --csv /tmp/openclaw-model-usage.csv
```

## What to check when numbers look weird

1) `openclaw sessions --json` 里是否有重复 key 指向同一个 `sessionId`（正常现象）。
2) 某些 session 可能没有 token（`totalTokensFresh=false`），脚本会把 token 记为 0 或使用可用字段。
3) 如果想要“provider 计费口径的 request 次数”，需要在 provider 控制台看或从 gateway 详细日志聚合（不在本 skill 默认范围内）。
