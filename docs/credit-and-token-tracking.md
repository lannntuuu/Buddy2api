# Credit 与 Token 统计（v2.2.0+）

本文档解释 Buddy2api 怎么统计各通道的 **token 用量** 和 **credit 消耗**，为什么 SOLO / TraeWork
之前显示"没 credit"，v2.2.0 起加了什么来估算，估算值和真实值差多少。

## 1. 概念区分

- **token 用量**（`prompt_tokens / completion_tokens / total_tokens`）：上游 SSE `usage` / `token_usage`
  事件直接回报的**字面数字**，每家都一样可以解析到，是免费的事实。
- **credit 消耗**：每家厂商自己定义的"积分/额度"单位，扣费曲线各家不同；上游**不一定**回报。
  WorkBuddy 的 `usage.credit` 字段直接报；TRAE SOLO 和 TraeWork **不报**。

## 2. 各通道现状（v2.2.0 + 网关估算）

| 通道 | token 统计 | credit 统计（默认） | credit 统计（启用 A 之后） |
|---|---|---|---|
| WorkBuddy | ✅ 上游直接报 | ✅ 上游 `usage.credit` 字段 | ✅ 不变（仍走上游） |
| Trae SOLO | ✅ `token_usage` 事件 | ❌ 上游不报 → `total_credits=0` | ✅ 网关侧 token→credit 估算（traesolo 默认 1000 token / 1 credit） |
| TraeWork | ❌ token_usage 事件被丢（`_SKIP_EVENTS`），只记 0 | ❌ 同上 | ❌ **需要先修 SSE 解析再能估算**（见 §6） |
| QClaw | ✅ | ❌ 上游不报 → `0` | ✅ 网关侧估算（qclaw 默认 1000 token / 1 credit） |
| QwenWork | ✅ | ❌ 上游不报 → `0` | ✅ 网关侧估算（qwenwork 默认 1000 token / 1 credit） |

## 3. 为什么 SOLO / TraeWork 没有 credit

TRAE SOLO 的 `token_usage` 事件原文（`internal/upstream/solosse.go:19-20` 注释里就是它）：

```json
event: token_usage
data: {"prompt_tokens":21,"completion_tokens":142,"total_tokens":163,"reasoning_tokens":135}
```

只有 4 个 token 字段，**没有 `credit` / `cost` 字段**。SOLO 把 credit 信息放在另一条 `pay/ide_user_ent_usage` 路径
（权益包维度），跟 chat 流量解耦，无法直接对应到"这次请求扣了多少 credit"。

## 4. trae2api-web（Go 版）对照

参考项目 [trae2api-web](https://github.com/connectedGraph/trae2api-web) 处理方式**和我们一致**：

- 解析 `token_usage` → ✅（`solosse.go:171-172`、`handler_test.go`）
- 上报 OpenAI `usage`（流式最后一块、非流式末尾）→ ✅
- 把 token 换算成 credit → ❌（Go 全文搜 `credit` 只有 `credits_limit/credits_amount`，那是
  `ide_user_ent_usage` 接口的字段，跟 chat 流量无关）
- 账号级 credit 累计 → ❌
- UI 显示 credit 消耗 → ❌

也就是说：**Go 版跟我们一样没 credit 统计**，因为上游不报，Go 版也只解析 token。这是设计一致的行为，
不是 Buddy2api 的疏漏。

## 5. v2.2.0+ A：网关侧 token→credit 估算

### 5.1 机制

对于上游**不报 credit** 但**报 token** 的通道（SOLO / QClaw / QwenWork），按

```
credit = round(total_tokens / rate, 6)         # rate>0：估算
credit = 0                                       # rate<=0 或未配：保持原行为
```

在每个 provider 的 `_log` 里把估算值写入 `record_request` 的 `credit` 字段，由 `database.record_request`
累加到账号 `total_credits`。

### 5.2 换算率配置

每通道一个换算率（tokens per 1 credit），存 `settings` 表键 `<channel>.credit_rate`：

```bash
# 查看（含默认与自定义标记）
curl -H "Authorization: Bearer <admin-token>" \
     http://127.0.0.1:8787/admin/channels/traesolo/models

# 设置
curl -X PUT -H "Authorization: Bearer <admin-token>" -H "Content-Type: application/json" \
     http://127.0.0.1:8787/admin/channels/traesolo/models \
     -d '{"credit_rate": 250}'

# 关闭估算（恢复 0）
curl -X PUT -H "Authorization: Bearer <admin-token>" -H "Content-Type: application/json" \
     http://127.0.0.1:8787/admin/channels/traesolo/models \
     -d '{"credit_rate": 0}'

# 重置回内置默认（1000）
curl -X PUT -H "Authorization: Bearer <admin-token>" -H "Content-Type: application/json" \
     http://127.0.0.1:8787/admin/channels/traesolo/models \
     -d '{"credit_rate": null}'
```

或者在网页管理页「模型配置 → 各平台设置」里改，**保存时跟 models/aliases 一起提交**就行（互不影响）。
"重置默认" 会同时把 models/aliases/credit_rate 三项都还原。

### 5.3 默认值

`DEFAULT_CREDIT_RATE = 1000.0`（tokens per 1 credit）。**内置默认仅作为占位值**，不是真实定价参考：

- 这是个**估算**，不是上游账单的真实扣费；
- TRAE / QClaw / QwenWork 没有公开的"每 1k token = X credit"对照表，单一常量不可能准；
- 不同模型 input / output 单价可能差几倍，签到 / 工具调用还可能单独计费，单值无法表达这些差异；
- **更准的方案是按 (通道, 模型, input/output) 列出多组系数**——但这需要你自己有数据，且每家都可能调价，
  维护成本高（结论：我们没做这一步；你以后如果拿得到稳定的报价表，可以加进 settings）。

### 5.4 它显示在哪里

- 管理页 dashboard "今日 24 小时趋势 → 额度" 折线（按 `t.credit` 聚合）；
- 请求日志的 **credit 列**；
- 账号行 `credit-cell` 在 `credit_snapshot=0` 且 `total_credits>0` 时显示「累计消耗(估算) X」；
- 上游实际 credit 余额仍走 `GET /admin/accounts/{id}/resources`（按 `ide_user_ent_usage` 实时拉），
  不被本估算影响——本估算只动账号级别的"网关侧消耗"那个数字。

## 6. TraeWork 的特殊情况

TraeWork 的 `_log` 把 `total_tokens=0` 写死——`token_usage` 事件在它的 `SKIP_EVENTS` 里被直接丢了
（`providers/traework/chat.py:142-153`）。所以：

- **token 统计就**没有**（不只是 credit）**——A 估出来也是 0。
- 要给 TraeWork 也算上 credit，需要先单独修它的 SSE 解析把 `token_usage` 解析出来（参考
  `solosse.go:88-89` 的做法）。这是另一个改动，牵动它现有工作流，**当前未做**。

## 7. 调优建议

- **粗看消耗趋势**：保留默认 1000 / channel 即可，dashboard 趋势图能看出"今天 / 这周"相对消耗。
- **按模型定系**（如果你能查到各家公开的 token→credit 报价）：把 `credit_rate` 调成**该通道主要模型的
  换算率**，例如"1000 token ≈ 0.1 credit" → 配 `credit_rate=10000`，就是同一组数字的倒数；
  数字越保守（rate 越大），估算值越小，趋势图越平。
- **想关掉估算**：把 rate 设为 `0` 即可，保留 token 统计，credit 列恒为 0。
- **想同时用真实上游 credit**：SOLO 支持 `ide_user_ent_usage` 实时拉余额（"刷新官方额度"），那是
  上游"权益包维度"的余额，不是按请求累加的，跟本估算不冲突；可以**并用**——网关估算看趋势，
  上游快照看余额。

## 8. 字段在数据库里的对应

| 字段 | 来源 | 是否每请求累加 |
|---|---|---|
| `accounts.total_tokens` | `_log` 写入的 `total_tokens` | ✅（每个 provider 调 `_log`） |
| `accounts.total_credits` | `_log` 写入的 `credit` | ✅（WorkBuddy=上游；其他=估算或 0） |
| `accounts.credit_limit` | 当前**未**自动填 SOLO 权益包；可手动调 | ✗ |
| `accounts.credit_baseline` | `set credit_limit` 时同步（避免重置后统计跳变） | ✗ |
| `logs.credit` | 当次请求的 credit（单条） | — |
| `logs.prompt_tokens / completion_tokens / total_tokens` | 当次请求的 token（单条） | — |

> **关于精确对账**：目前没有"按上游真实账单对账"的反向同步——上游不报"本次扣了多少 credit"，
> 就没有办法校验估算值。所以本估算**只用于趋势观察**，**不要拿它和上游真实余额做差额计算**
> （差额来源之一就是估算偏差）。
