# 平台 × 模型 × 日 用量统计

按平台（provider）、模型（model）、自然日三个维度聚合请求日志中的 token 用量，基于现有 `logs` 表实时查询，不额外建表。

## 接口

```
GET /admin/provider-model-usage
```

管理端点，需要 Admin Token（HttpOnly Cookie 或 `Authorization: Bearer <token>`）。

### 参数（全部可选）

| 参数 | 说明 | 约束 |
|------|------|------|
| `provider` | 平台 id，如 `workbuddy` / `qclaw` / `qwenwork` / `traework` / `traesolo` | 必须是已知且已启用的通道 |
| `model` | 模型 id，精确匹配 | 必须在该平台当前生效的模型白名单内（`channel_model_ids()`，含别名）；传 model 必须同时传 provider |
| `days` | 最近 N 天（含今天） | 正整数；与 `start_date` 互斥 |
| `start_date` | 起始日期（本地时区，含） | ISO 格式 `YYYY-MM-DD`；与 `days` 互斥 |
| `end_date` | 结束日期（含） | 只传 `end_date` 时起始默认为该日期前 89 天；只传 `start_date` 时结束默认为今天 |

时间参数冲突（如 `days` 与 `start_date` 同传）、非法日期、负数 `days`、model 不在白名单等均返回 `400`；未携带有效凭证返回 `401`；触发限流返回 `429`。

### 限流

按管理凭证（Cookie 优先，其次 Authorization header，无凭证按客户端 IP）做滑动窗口限流：

- 默认 **30 次 / 分钟**，可用环境变量 `CB_GATEWAY_USAGE_RATE_LIMIT` 调整
- 超限返回 `429` + `{"detail": "Too many requests"}`

### 返回结构

```json
{
  "providers": {
    "qclaw": {
      "models": {
        "gpt-5.5": {
          "daily": [
            {
              "date": "2025-06-01",
              "requests": 12,
              "prompt_tokens": 1200,
              "completion_tokens": 600,
              "total_tokens": 1800,
              "credit": 0.18,
              "avg_duration_ms": 950
            }
          ],
          "summary": { "requests": 12, "prompt_tokens": 1200, "completion_tokens": 600, "total_tokens": 1800, "credit": 0.18, "avg_duration_ms": 950 }
        }
      },
      "summary": { "requests": 12, "prompt_tokens": 1200, "completion_tokens": 600, "total_tokens": 1800, "credit": 0.18, "avg_duration_ms": 950 }
    }
  },
  "totals": { "requests": 12, "prompt_tokens": 1200, "completion_tokens": 600, "total_tokens": 1800, "credit": 0.18, "avg_duration_ms": 950 }
}
```

- `daily` 按日期降序
- `summary`：模型小计、平台汇总；`totals`：全局合计
- `avg_duration_ms` = `SUM(duration_ms) / COUNT(*)`（加权平均）

### 示例

```bash
# 最近 7 天全部平台
curl -s http://127.0.0.1:8787/admin/provider-model-usage?days=7

# 仅今天（本机当天 00:00 ~ 23:59:59）
curl -s http://127.0.0.1:8787/admin/provider-model-usage?days=1

# 只看 qclaw 平台的某个模型，2025-06-01 ~ 2025-06-30
curl -s 'http://127.0.0.1:8787/admin/provider-model-usage?provider=qclaw&model=gpt-5.5&start_date=2025-06-01&end_date=2025-06-30'
```

## 前端

Web UI 顶部导航新增 **「用量统计」** 页面：

- 时间范围：今日 / 近 7 天 / 近 30 天 / 近一年快捷按钮 + 自定义日期区间（「今日」对应 `days=1`，区间为本机当天 00:00 至 23:59:59）；**页面默认显示今日**
- 平台筛选：全部或具体平台；选定平台后出现该平台白名单内的模型下拉
- 顶部三张汇总卡片（请求数 / Token 总量 / Credit 消耗）
- 明细表：平台汇总行 → 模型小计行 → 每日明细行（日期降序）

## 数据说明

- 数据来源为请求日志 `logs` 表（含 `provider`、`model`、`*_tokens`、`credit`、`duration_ms`、`created_at` 字段），按本地时区的自然日聚合
- 查询范围受日志保留期 `CB_GATEWAY_LOG_RETENTION_DAYS`（默认 90 天）约束，超期日志已被清理
- 失败请求同样记录 token/credit（如有），如只关心成功请求可先在「请求日志」页按状态筛后对照
- 「请求日志」页每行带 **Client** 列：常态只显示发起客户端（如 `codex`），鼠标悬浮显示 `client client_version`；展开详情里可看完整的 Client 与 Client 版本，用于按客户端追溯请求来源；顶部搜索框支持按 `client` 或 `client_version` 关键词检索
