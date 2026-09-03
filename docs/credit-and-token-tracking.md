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

