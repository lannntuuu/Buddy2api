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
| Qoder CN | ✅ | ✅ **上游直接报** `usage.credits` + `usage.billable`（v2.2.0 时误走估算，见 §11） | ✅ 不变（仍走上游真值；`billable=false` 免费档记 0） |

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

## 9. 官方真值接口探查结论（2026-09-01）

为给 credit 估算找官方锚点，实拉了 TRAE 各积分接口，结论如下：

### 9.1 账户级总消耗（两个通道返回相同值）
- `POST /trae/api/v2/pay/ide_user_ent_usage`（SOLO 用 `UG_HOST=api.trae.cn`）
- `POST {traework_host}/.../USAGE_PATH`（TraeWork 用各自 host）
- 两者对**同一 TRAE 账号**（`user_id` 一致）返回**相同的** `usage_summary.consumed_amount`（实测 696.52）。
- 该值是**账户级总消耗**，且据官方侧确认是**推算值**（用"已过期积分 + 现有奖励积分剩余"反推），
  **非精确实时账单**，也**不区分产品线**。

### 9.2 按 session 的真实消耗明细（TraeWork 有，TraeSOLO 无）
- `POST /trae/api/v1/pay/query_user_usage_group_by_session`（`UG_HOST`，`Cloud-IDE-JWT` 授权）
- 请求体：`{"start_time","end_time","page_size":≤50,"page_num":1,"usage_type":[N]}`
  - **`page_size` 不能 > 50**，否则返回空（实测 100 返回 0，50 正常）。
  - `total` 字段 = 总记录数；单页最多返回 `page_size` 条，需翻页。
- `usage_type=7` 返回 TraeWork 的真实消耗明细：每条含 `credits_float`（真积分）、
  `model_name`、`usage_time`、`usage_source`、`product_type_list`。
  - 模型分布实测为 `Qwen3.7-Plus` / `DeepSeek-V4-Flash 官方版` / `GLM-5.3` 等
    ——**均为 TraeWork 白名单模型**，确认 `usage_type=7` 是 **TraeWork 专属真值**。
  - 90 天窗口内 `usage_type=[1]~[6]` 均返回 0，说明其它 usage_type 当前无数据/不适用。
- **TraeSOLO（SOLO 模式）官方不能单独查到自己的消耗**，也没有独立的真值接口。

### 9.3 对 credit 统计的影响
- **TraeWork**：可改为对接 `usage_type=7` 官方真值（按 `usage_time` 归日），替代当前恒为 0 的估算。
- **TraeSOLO**：官方无真值接口，且账户总消耗是推算值、不区分产品线，
  **无法用"总量 − TraeWork"精确得到 SOLO 消耗**（还混有 TraeCode IDE 内对话等网关看不到的消耗）。
  因此 TraeSOLO 的 dashboard "credit" 只能是**相对消耗估算**（公式 `total_tokens / scale × model_rate`，
  `scale` 默认 250/可调，绝对量级无官方依据），**明确标注非真积分、不可与官方对账**。
- 结论：**TraeSOLO 保持相对估算现状；TraeWork 值得接官方真值**（避免真值被浪费在恒 0 估算上）。

### 9.4 接口位置（代码中）
- SOLO 积分：`providers/traesolo/quota.py`（`EP_ENT_USAGE` = `/trae/api/v2/pay/ide_user_ent_usage`）
- TraeWork 积分：`providers/traework/quota.py`（`USAGE_PATH`，返回 `usage_summary.consumed_amount`）
- session 明细（待接入 TraeWork）：`/trae/api/v1/pay/query_user_usage_group_by_session`
  当前**项目内尚未在任何 provider 中调用**，需在 traework 侧新增封装。

## 10. TRAE credit 计费公式倒推（2026-09-01，结论：三档 per-token 公式已破解）

带着 51 条 usage_type=7 官方 session 真值（含 input/cache_read/cache_write/output token 明细、
credits_float、cost_money_float）、官方模型倍率表（consumption_rate）、权益包窗口和本地 1409 条
traesolo 网关日志（180.8M tokens），做了系统性倒推。**公式已破解**（初版"无公式"判断系被折扣行误导，已修正）。

### 10.1 基本事实
- API 内部换算：`cost_money_float = credits_float × 0.025`（51/51 条精确成立）。
- **货币口径（2026-09-01 核对）**：官方定价页 Lite ¥49/2000=¥0.0245、Pro ¥99/4000=¥0.0248，
  与内部 0.025 在 2% 内吻合——即 **API 的 money 单位就是人民币零售价，1 credit ≈ ¥0.025（40 credits ≈ ¥1）**。
  真实美元价 = 下列"¥/M"数字 ÷ 7.2 汇率（如 qwen input ≈ $0.28/M，与 GMI $0.15/M 同量级）。
  注意不是 10:1（若按 $0.1/credit 折算单价会高出 4 倍，与推理市场价不符）。
- `extra_info.input_token` **已包含** cache_read_token（input − cache_read = 独立新输入）。
- cache_write 在全部 51 条中均为 0，单价无法从本样本标定，暂按 input 价处理。

### 10.2 破解出的三档 per-token 公式（46/51 行误差 <1%）
```
credits = ( input_nc × p_in + cache_read × p_cache + output × p_out ) / 1e6 ÷ 0.025
```
单价（官方 money 单位 ≈¥/1M tokens，从干净行精确反解；折美元 ÷7.2）：

| 模型 | input | cache_read | output | cache/in | 命中率 |
|---|---|---|---|---|---|
| qwen3.7-plus | 2.00 | 0.40 | 9.20 | 0.199（≈GMI 0.20） | 23/24 |
| deepseek-v4-flash 官方版 | 1.35 | 0.047 | 3.84 | 0.035 | 15/16 |
| glm-5.3 | 2.80 | 0.70 | 9.80 | 0.250 | 5/8 |

- 结构与 GMI 等推理商的三档计价一致（用户提出的假设，验证成立）：cache_read ≈ input 的 3.5%-25%，
  output ≈ input 的 2.8-4.6 倍。表面单价是 GMI 的 9-18 倍，但那是人民币数字；折美元后
  （÷7.2）与 GMI 同量级——这就是"消耗量看起来对不上/差 10 倍"的真相。
- 代码落地：`providers/traesolo/pricing.py`（`trae_credit_from_usage`）。
- 上游 SSE `token_usage` 事件**原生携带** `cache_read_input_tokens` / `cache_creation_input_tokens`
  字段（2026-09-01 实机探查确认），三档公式的输入完整可得；logs 表已加
  `cache_read_tokens` / `cache_creation_tokens` 两列，新请求起开始记录。

### 10.3 折扣行（off-formula rows）：少数大 session 按深度折扣计费
8 条行不服从标价公式（-39% ~ -96%），全部集中在 8-31 的大 token 会话，疑似限时促销
（如 Seed 2.5 折类）。特征：token 越大折扣越深。**这批行是初版拟合被带偏的原因**——
把它们剔除后公式精确收敛。

### 10.4 活体实验：×model_rate 假设被否定，标价公式实测精确成立
- **实验设计**：发一个已知 token 量的网关请求，立刻查 `entitlement_list.usage_summary.consumed_amount` 的 delta。
  模型 = glm-5.3（rate 0.40）+ DeepSeek-V4-Flash（rate 0.08）。
- **结果**：

| 请求 | tokens | 实扣 delta | 标价估 | 标价 × rate | verdict |
|---|---|---|---|---|---|
| glm-5.3 | 15 in + 86 out | 0.3700 | 0.3710（err 0.3%）| 0.1484（err 60%）| 标价命中 |
| DeepSeek A | 1,973 in + 30 out | 0.1100 | 0.1112（err 1.0%）| — | 标价命中 |
| DeepSeek B（长） | 7,133 in + 85 out | 0.4000 | 0.3982（err 0.45%）| — | 标价命中 |

  - `consumption_rate.rate`（DeepSeek 0.08、GLM 0.40）**不参与计费**，仅是 SOLO 客户端展示用系数。
  - 实时扣费 5 秒内到账，**官方在按标价扣网关流量**。
  - 路线 1：未来请求通过 cache 折扣大幅降低；路线 2：同会话重复 prompt 应可触发 cache hit。

- **意外发现**：`_norm("DeepSeek-V4-Flash-Official")` 归一化后带尾巴连字符
  `"deepseek-v4-flash-"`，查不到价格表，**一直在用 qwen 默认价 (2.00/0.40/9.20) 而非真实价
  (1.35/0.047/3.84)**。修：归一化末尾 `"-"` 剥除。

### 10.5 usage_type=7 与网关 SOLO 流量无关（时间对账，48/48 零匹配）
- 48 条有模型名的官方 session 与网关 traesolo 请求做 ±90s 时间对账：**零匹配**。
- 8-31 案例铁证：官方 session 全部在 14:43-14:59；网关 GLM-5.3 的 70 条请求全部在 15:03-15:10，
  完全错开，官方窗口内无任何 session 计费记录。
- **usage_type=7 只计 TraeWork 客户端（IDE 内）用量；网关 API 流量不产生 session 记录
  （但 live 实验证明它**被实时计入** consumed_amount，session 记录只是按工作流归属的另一个切面）。

### 10.6 Bound 与 8-31 谜团
- 当前包窗口 08-19 15:42 起，总额度 5500（含主包+签到+月度 bonus），已用 697.64。
- 8-31 网关跑了 143.1M tokens。按公式不计 cache：≈10,374 credits（历史行无 cache）；按 cache
  占比 90%（agent 循环常态）估算真实标价：≈445 credits——正好落在月消耗轨迹内。
- 这与 §10.4 活体实验一致：**网关按标价实时扣费，但 agent 流量绝大部分是 cache 命中**，
  实际扣费远低于"全价"。
- 真实 dashboard 数字必须分两栏看：
  - logs.credit（标价口径，历史 cache=0 近似）：10,429.57（修正 DeepSeek 价后），
    含 8-31 = 8,373.88、8-28 = 1,728.84、9-1 = 326.85
  - 官方实际扣费：697.64（9-1 当前，**已被实时按标价扣了**——见 §10.4 实测）

### 10.7 用量统计列扩容（v2：全量缓存追踪）
**问题**：旧版 logs 只有 prompt/completion/total_tokens，cache 信息丢失，3 档公式只能按 cache=0 估算，
agent 循环流量被严重高估。

**方案**：
1. logs 加 4 列：`cache_read_tokens`（已有）、`cache_creation_tokens`（已有）、
   `usage_json` TEXT（整段上游 token_usage）、`credit_source` TEXT（`live` / `historical_backfill`）。
2. `traesolo chat._log` 把 upstream usage dict 整体 dump 到 `usage_json`，并标 `credit_source='live'`。
3. 历史行 cache=0 缺失——用官方 session 真值（usage_type=7 的 51 条记录，恰好是 agent 循环
   的同类型流量）算 per-model-per-day cache 比例，套到历史 traesolo 行。标记 `historical_backfill`。
   - per-day 比例要求 N≥3 条同模型记录，否则回落 model 平均；仍无则用默认 70%。
   - 修正 norm() 尾连字符 bug + 扩展 strip 列表（含"正式版"）。
4. `get_stats().daily` 新增 `cache_status` 字段（`accurate` / `partial` / `approx` / `empty`），
   前端 7 天强度图给每天显示角标，让用户知道当天 credit 的来源。

**落地结果**（重构后）：
- 8-31 credit: 8,375（cache=0）→ **2,930**（cache 反填，cache 占 75%）
- 8-28 credit: 1,809 → **705**（cache 占 33%）
- 9-1 credit: 822 → **592**
- 全月 traesolo total: 10,469 → **3,691 credits**（-65%）
- 1,387 历史行获得 cache_read；新请求起 `credit_source='live'`、cache 100% 实测。
- 活体验证：4k prompt 请求，4032 cache_read（98% 命中），credit 0.162（vs 0.459 全价，-65%）。

### 10.8 对本项目的落地
- traesolo `_log` 改用三档标价公式（`pricing.py`）；无 token 数据时退回旧相对估算。
- **历史 logs.credit 四次重填**（见 `_backfill_formula.py` + `_backfill_cache.py`）：
  - 旧相对模型（÷1000 × rate）：17,747.20
  - 三档公式（cache=0 近似）：14,977.88
  - + DeepSeek 价修正（`_norm` 尾连字符 bug）：10,469.44
  - + 历史 cache 反填（官方 session 比例）：**3,691.08**
- 新请求起 logs 记录 `cache_read_tokens` + `usage_json` + `credit_source='live'`，
  后续重填会越来越准。
- 7 天强度图加 cache_status 角标（accurate / partial / approx），让用户一眼分辨。
- traework 每日 credit 继续 usage_type=7 官方真值（152.57）——它只覆盖 IDE 客户端用量。
- 账户历史总消耗卡片维持"当前已用 + 过期（假设用完）= 1196.52（估）"。
- dashboard 标注改为"官方标价估算"，明确"非实际扣费"；7 天强度图分两行：
  Credit 行 = 标价求和、Work真值行 = 官方 session 真值。
- 分析脚本留档：`_analysis_harvest.py`（采集）、`_analysis_fit*.py`（拟合四轮）、
  `_analysis_match.py`（时间对账）、`_backfill_formula.py`（重填），
  数据快照 `_analysis_data.json`。

## 11. 上游真值优先：Qoder 免费档曾被记成消耗（2026-09-19 修）

**问题**：Qoder CN 每次请求都在 usage 里回报本次真实扣费，但 `store_common.log_request`
一律走 `credit = total_tokens / channel_credit_rate` 估算，**整包 usage 只当证据存下来不用**。
后果：`Qwen3.8-Flash`（上游 key `qfmodel`）是 `billable=false` 的**真免费档**，
51 次请求被估算记出 **4612.70 假 credit**；该通道全部 66 行合计 4669.66，
而上游 `billable` 合计真值仅 **0.267**（差 17000 倍）。

**上游真值字段**（Qoder CN，实测 66/66 行都有）：

| 字段 | 含义 |
|---|---|
| `credits` | 本次扣费额（credit） |
| `billable` | `false` = 本次未扣费（免费档）/ `true` = 真扣 |
| `original_credits` | 折扣前原价（错峰促销用，不参与记账） |
| `prompt_tokens_details.cached_tokens` | cache 命中（已有 `extract_cache_tokens` 覆盖） |

**修法**：`store_common.upstream_credit(usage)` —— 上游有真值就用真值，没有才回落估算：

```
billable is False          -> 0.0                    # 免费档，记 0（不是 credits 的值）
credits / credit 存在      -> 原值                    # 计费档，用真值
都没有                      -> None -> token/rate 估算 # qclaw / qwenwork 行为不变
```

因此**「免费档」不再依赖倍率**：Qoder 的 `fetch_model_rates` 仍返回 `rate=None`
（上游只给绝对价、不给 per-model 倍率），但消耗统计不再需要倍率——真值直取。
`<channel>.credit_rate` 对该通道降级为「没有 usage 时的兜底」。

**历史数据重算**：`ops/scripts/oneoff/backfill-upstream-credit.py`（只改 usage_json 里有
真值的行，再重算 `accounts.total_credits`）。实测 qodercn 4669.662 → **0.267**。

**为什么之前没发现**：`credit_source` 只按「有没有 cache 键」判 `live`，
而 Qoder 的 `usage_json` 里明明躺着 `credits`/`billable` 却没人读——判据看的是 cache，
不是「有没有真值」。这是一个**信号在手上但没接线**的 bug，不是上游不报。

## 12. Qwen3.8-Flash 现在到底计不计费（实机复核 2026-09-19 深夜）

上面 §11 证明了「历史上记错了」，但**没有**回答「现在扣不扣」。实测结论：**不计费，现在仍是免费档。**

> **证据强度**：主证据 = 上游响应里的 `billable=false` 原文（连跑 3 轮 + 垃圾 token
> 控制实验证明 200 非回声）；旁证 = 账户 `used` 计数器的上界排除（§12.3）。
> 余额 delta 因精度不足**不构成证据**（§12.6）。

### 12.1 方法

历史日志停在当天 21:13（约 3 小时前），**不能代表"现在"**。所以直接打上游：

1. 从**官方客户端登录缓存**读真 token（`store.read_official_session()`），
   绕开网关 DB 的 master key/解密链路，避免凭据问题污染结论；
2. `chat.test_chat` 发极小请求，读上游 usage 原文；
3. **控制实验**：把 token 换成垃圾值，必须失败——否则 200 可能是兜底回声，不能采信。

### 12.2 结果（连跑 3 轮，稳定复现）

| 模型 | `billable` | `credits` |
|---|---|---|
| `qfmodel`（Qwen3.8-Flash） | **False** ×3 | 0.0031~0.0032 |
| `auto`（对照） | True ×3 | 0.0023~0.0042 |
| `qmodel_38max`（Qwen3.8-Max） | True | 0.0133 |
| `qmodel_latest`（Qwen3.7-Max） | True | 0.0116 |

**控制实验**：垃圾 token → `403 Login expired`（`ok=False`），
证明上面的 200 是真实上游响应，不是回声。

### 12.3 交叉验证：账户计数器（上界排除法）

额度接口 `GET /api/v2/quota/usage`（**bearer 即可**，不签名）：

```
addOnQuota: {"total": 200.0, "used": 12.0, "remaining": 188.0, "unit": "credits"}
```

历史 59 次免费档请求（`billable=false` 全部行）**名义** credits 合计 **33.09**
（其中 `qodercn/Qwen3.8-Flash` 51 行 = 32.21）。
若这些真被扣过，`used` 必然 ≥33.09；实测 `used = 12.0`
（且这 12.0 主要不是网关流量——网关整条通道总共才 66 次请求）。

**33.09 > 12.0 ⇒ 免费档那部分不可能按标价扣过。**

> **该上界论证的假设**：`used` 计数在数小时延迟后是准的（这 59 次发生在 18:41–21:13，
> 我 23:57 才读，间隔 2.7h+）。需要注意 `used` 明显**有延迟**——连发 40 次计费档
> （`auto`，名义 0.15）当场也没推动它（见 §12.6），所以**主证据是上游 `billable` flag 本身**
> （响应原文 + 垃圾 token 控制实验），`used` 只作旁证。

### 12.4 关键含义：`billable=false` 时 `credits` 不是 0

`billable=false` 的响应里 `credits` **仍有值**（0.0032）——那是**标价参考，不是扣费额**。
即 3.8-Flash **有价、只是现在不收**：`price_factor=0` 的**限时促销档**，Qoder 随时可结束。

这对本项目的意义（也是选"真值优先"而非"配倍率"的理由）：

- 促销结束那天上游 `billable` 翻成 `true`，**网关自动开始记真值，零代码改动**；
- 若当初按倍率写死 `qfmodel: 0`，促销一结束就会**持续漏记**，而倍率方案无法自愈。

### 12.4b qfmodel 的**名义**标价结构（不是扣费，是"若要收费会收多少"）

从 59 条历史 usage + 活体剂量实验反解，名义价 = `in_nc×p_in + cached×p_cache + out×p_out`：

| 项 | 单价（每 1M token） | 说明 |
|---|---|---|
| `cached`（cache 命中） | **2.22** | 命中价极稳（2.218~2.243），任一样本都能标定 |
| `out`（输出） | **59.96 ≈ 60.0** | 与三档结构一致（输出约为输入 2~3.4×） |
| `in_nc`（新建输入）**≤1024** | **17.77** | 小请求档 |
| `in_nc`（新建输入）**>1024** | **27.77** | 长上下文档 |

即**输入价有长度分档**：`in_nc≤1024` 时 17.77，超过则整体按 27.77。
两档分别对 36/23 行拟合：高档中位误差 **0.003%**、低档 **0.70%**（低档行全是 in_nc≈65 的
短请求，绝对值极小 → **相对误差被放大**；其 `credits` 只有 0.002~0.005，末位抖动即可占几个百分点）。

> ⚠️ **诚实标注两处不确定性**：
> 1. **分档的键**有两种可能，历史数据无法区分——按 `in_nc` 分（step_in）与按
>    `prompt` 分（step_prompt），59 行里 45 行 `cached/in_nc ≥ 0.5`（最高 1361×），
>    此时隐含输入价对 `p_cache` 极度敏感（分母 `in_nc` 近 0），**这些行不能定档**。
>    只有 14 行 `cached/in_nc<0.5` 可靠，它们全部一致支持分档存在（in_nc 65→低档、
>    26k~301k→高档），但**不足以区分键是 in_nc 还是 prompt**。
> 2. **分档点 1024 是实测夹出来的**（活体：prompt=965 → 低档；prompt=1025 → 高档），
>    不是官方文档值，促销/调价可能变。
>
> 结论：**别把这两个数字写进代码当倍率**——这正是本项目走"真值优先"的原因。
> 上面只用于解释"为什么 3.8-Flash 看起来有价"。

### 12.5 顺带纠正

- `qmodel_38max`（Qwen3.8-Max）在目录里带 `is_free` 标记，但**实测 `billable=true`、在计费**。
  **真免费只有 `qfmodel` 一个**。之前文档把两者并提，易误读（见 `multi-channel-v2.md` §模型表）。
- 判"是否免费"**必须看 `billable`**，不能看目录的 `is_free`/`price_factor` 标记。

### 12.6 两个已排除的假象（免得后人重走）

- **额度接口"坏了一直 401"**：假象。真因是探针复制的 DB 与 master key 不匹配，
  `access_token` 解出来是空串 → 上游自然 401。用真 token 复测即 `ok=true, remaining=188.0`。
  （`fetch_quota` 的 bearer 路径**本来就是对的**，不要"修"它。）
- **用余额 delta 验证单次请求**：不可行。`used`/`remaining` 只有 1 位小数，
  单次约 0.003 credits 落在显示精度之下；40 次连发（名义约 0.15）也**没推动计数器**
  （对照组 `auto` 同样没动）——该接口对账粒度太粗，**不能用作单请求级证据**。
  本节的结论来自 `billable` flag + `used` 上界排除，不依赖 delta。

### 12.7 复测建议

促销可能结束，结论有时效性。复查只要跑一次：看 `billable` 是否仍为 `false`，
以及 §11 的 `upstream_credit` 是否已自动改记真值（无需改代码）。

