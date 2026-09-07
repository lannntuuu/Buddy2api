# 17a — hy4-preview 数据取证报告（Phase A，只读）

> 分支：`fix/hy4-preview-usage`
> 执行方式：只读取证（未改 db、未改代码、未重启进程）
> DB：`data/codebuddy_gateway.db`（只读 URI `file:...?mode=ro` 打开成功，无需拷贝）
> 工具：`.venv\Scripts\python.exe` + 内置 `sqlite3`

## 0. 一句话结论

`hy4-preview` **没有在任何配置（settings 表）里出现**，既不在 `models`/`model_aliases`/`unified_models`，也不在 `*.<channel>.models`/`*.aliases`/`*.reasoning`。它**只来自客户端请求体里的 `model` 字段**，并被打进了 `logs.model` 列。

日志里 7 条 `hy4-preview` 全部：
- `api_key_id=1`（`Buddy` 键，`default_channel=workbuddy`，`allowed_models=NULL` → 不限制模型）；
- `provider=workbuddy`（绑定到 Tencent 上游 `copilot.tencent.com`）；
- 账户 `account_id=1`（workbuddy 唯一 active 账户）。

其中 **4 条 200 真实返回**（含 tool_calls / stop / length），说明**上游 Tencent 实际接受 `hy4-preview` 这个名字并能产出内容**；失败集中在：
- **2 条 502**：网关自身 `chat_grammar` 解析上游 tool-call 流时报 `The upstream tool call stream had an invalid call id.`（上游工具调用帧畸形，网关 SSE 校验拒绝）；
- **1 条 400**：上游返回错误码 **11128 `Illegal API invocation from an unapproved channel`**（安全策略拦截，疑似大模型/非白名单通道调用）。

> 矛盾点（留给 Phase B 复现）：当前 `models` 设置为 `[{"id":"wb-m"}]`，`hy4-preview` 既不在该白名单也不在任何别名表，`router.bind()` 现行逻辑会把它判为 `UnknownModel` 直接 400 拒绝。而 8/29–8/31 的日志却显示它成功到达上游并 200 返回。推测当时 `models` 设置尚未被收窄为 `wb-m`（或经由其他路径转发）。**也意味着：用户现在再用 `hy4-preview` 很可能直接 400，而非走到上游**——这正是"使用一直有问题"的最可能近况。

## 1. settings 表全部键（13 个）

只读 `SELECT key, length(value) FROM settings ORDER BY key`：

| key | len | 模型相关 | 含 hy4 |
|-----|-----|----------|--------|
| channel_order | 89 | 是（通道顺序） | 否 |
| custom_channels | 684 | 是（gmi/bailian 自定义通道模型+别名） | 否 |
| enabled_channels | 55 | 是 | 否 |
| model_aliases | 39 | 是 | 否 |
| models | 16 | 是 | 否 |
| traesolo.aliases | 38 | 是 | 否 |
| traesolo.credit_rate | 6 | 否 | 否 |
| traesolo.models | 11 | 是 | 否 |
| traework.aliases | 38 | 是 | 否 |
| traework.models | 147 | 是 | 否 |
| unified_models | 163 | 是 | 否 |
| workbuddy.credit_rate | 6 | 否 | 否 |
| workbuddy.reasoning | 25 | 是（按模型思考档位） | 否 |

**模型相关键（11 个）**：`channel_order`, `custom_channels`, `enabled_channels`, `model_aliases`, `models`, `traesolo.aliases`, `traesolo.models`, `traework.aliases`, `traework.models`, `unified_models`, `workbuddy.reasoning`。

**含 `hy4` 的键名 / 键值**：均无。

### 1.1 各模型相关键完整值（已确认无 hy4）

```
model_aliases  = {"gpt-5.5": "glm-5.2", "auto": "hy3-x"}
models         = [{"id": "wb-m"}]
traesolo.aliases = {"auto": "DeepSeek-V4-Flash-Official"}
traesolo.models  = ["glm-5.2"]
traework.aliases = {"auto": "DeepSeek-V4-Flash-Official"}
traework.models  = ["qwen-3.7-plus","Doubao-Seed-2.1-Turbo","DeepSeek-V4-Flash-Official","qwen-3.5","glm-5.3","glm-5.2","Doubao-Seed-2.0-Code","glm-5.3-flash", ...]  (147 项，无 hy4)
unified_models  = [{"name":"deepseek-v4-flash","mappings":{"workbuddy":"deepseek-v4-flash","traework":"DeepSeek-V4-Flash-Official","traesolo":"DeepSeek-V4-Flash-Official"}}, ...]  (仅 1 条映射，无 hy4)
workbuddy.reasoning = {"glm-5.3-flash": "high"}
custom_channels = [gmi(模型 zai-org/GLM-5.3-Flash), bailian(模型 qwen3.8-27b)]
```

> 代码侧同证：`.py` 全仓 grep `hy4` **零命中**（除本 spec 与本报告），仅 `src/upstream/aliases.py:33` 的 `hy3-preview-agent` 与 `src/providers/qclaw/constants.py` 的 `pool-hy3-preview`。排除 `.venv`/`node_modules`/`data/*.db` 二进制。

## 2. 日志取证（表 `logs`，10 个表之一）

`logs` 列：`id, api_key_id, api_key_name, account_id, account_name, model, stream, prompt_tokens, completion_tokens, total_tokens, credit, finish_reason, duration_ms, status_code, error_msg, created_at, provider, client, client_version, cache_read_tokens, cache_creation_tokens, usage_json, credit_source, reasoning_effort, first_token_ms`。

`model LIKE '%hy4%'` 命中 **7 行**（全部 `hy4-preview`，`provider=workbuddy`，`account_id=1`）：

| id | 时间(UTC) | status | stream | finish_reason | error_msg | tokens(in/out) |
|----|-----------|--------|--------|---------------|-----------|----------------|
| 1346 | 2026-08-31 06:18:30 | **400** | 1 | error | `{"code":11128,"msg":"Illegal API invocation from an unapproved channel",...}` | 0/0 |
| 916 | 2026-08-31 01:41:37 | 200 | 0 | length | (空) | 22/16 |
| 723 | 2026-08-30 07:02:46 | **502** | 1 | error | `The upstream tool call stream had an invalid call id.` | 65690/160 |
| 722 | 2026-08-30 07:02:32 | 200 | 1 | tool_calls | (空) | 65269/2200 |
| 721 | 2026-08-30 07:01:50 | 200 | 1 | stop | (空) | 194398/5971 |
| 720 | 2026-08-30 07:00:13 | 200 | 1 | tool_calls | (空) | 262630/2769 |
| 691 | 2026-08-29 07:39:14 | **502** | 1 | error | `The upstream tool call stream had an invalid call id.` | 166306/902 |

聚合：
- **status 分布**：200×4 / 400×1 / 502×2
- **stream 分布**：stream=1×6 / stream=0×1
- **finish_reason**：error×3 / length×1 / stop×1 / tool_calls×2
- **客户端**：`client=None`×6；`client=zcode` ver `3.10.1`×1（即 id=1346 那条 400/11128）
- **时间跨度**：2026-08-29 07:39:14 → 2026-08-31 06:18:30（最近一条已是 8/31，之后无 hy4 记录 → 大概率已改用其它模型或持续失败被放弃）
- `credit` 全部 0.0（上游未回 credit 或网关未记）

### 2.1 两条失败的根因定位

- **502 `invalid call id`**（id 691、723）：出自 `src/upstream/chat_grammar.py:177` —— 网关对上游 SSE 的 tool-call 帧做语法校验，上游返回了 `invalid call id` 的工具调用帧，被网关自己的解析器判为畸形而中断流。属**上游工具调用流畸形 / 网关 SSE 校验过严**，与模型名本身无关，但仅在 `hy4-preview` 产生工具调用时触发（另两条 tool_calls 200 成功，说明同源时有时好）。
- **400 11128**（id 1346，zcode 客户端）：上游 Tencent 直接返回 `Illegal API invocation from an unapproved channel`。`src/upstream/compaction.py:_COMPACT_11128_MARKERS` 含 `"11128","Illegal API invocation"`，网关会把 11128 当"超长请求"自愈精简重试——但这条明确是"未授权通道"语义，不是内容大小问题，自愈无用。属**上游对 `hy4-preview`（或 zcode 客户端通道）的安全策略拦截**。

### 2.2 api_keys / accounts 关联

- `api_keys`：`id=1` 名 `Buddy`，`default_channel=workbuddy`，`allowed_models=NULL`（不限模型）。全部 7 条 hy4 请求都出自此键。
- `accounts`：仅 `workbuddy` 1 个 active 账户（`id=1`），故全部 hy4 请求打到同一上游账号。

## 3. 代码路径确认（只读静态阅读）

- `src/gateway/router.py:bind()`：`hy4-preview` 无 `channel/` 前缀 → 走 `_key_channel()`（=workbuddy）；`translate_unified("workbuddy","hy4-preview")` 不变；`provider.accepts_model("hy4-preview")` 查 `models=[{id:wb-m}]` 与别名表 → **不匹配** → `_other_channel_ids` 也否 → `raise UnknownModel("hy4-preview")` → HTTP 400 `unknown_model`。
  - ⚠️ **这与 8/29–8/31 日志中 4 条 200 相矛盾**：说明当时 `models` 设置更宽（含 hy4-preview 或整个 workbuddy 列表），或经其他转发路径；而当前生产配置已收窄为 `wb-m`，会导致现网请求直接 400。
- `src/providers/workbuddy/__init__.py:accepts_model/translate_model`：查 `list_models()`(=settings `models`) 与 `alias_map()`(=`model_aliases`)。`resolve_model_alias("hy4-preview")` 原样返回（别名表无此键）。
- `src/upstream/proxy.py:build_backend_body`：`body["model"] = resolve_model_alias(raw_model)` → 仍 `"hy4-preview"`，直接透传给 Tencent。即**一旦通过 bind，模型名原样上行**，上游自己决定是否认识。
- `src/upstream/chat_grammar.py:177`：502 `invalid call id` 的抛出点（SSE tool-call 语法校验）。
- `src/upstream/compaction.py:_is_11128_error`：11128 检测与"超长请求自愈精简"逻辑（对 1346 的"未授权通道"语义无效）。

## 4. Phase A 结论（回答 spec 问题）

1. **hy4-preview 的真实来源**：客户端请求体 `model` 字段（"客户端侧传来的模型名"），**非**任何网关配置。通道身份 = workbuddy 直连上游 id（`hy4-preview` 原样透传）。
2. **失败点**：① 现网 `models=[{"id":"wb-m"}]` 白名单不含 `hy4-preview`，`bind()` 会直接 400 `unknown_model`（最可能的"一直有问题"近况）；② 即便放行，上游对工具调用流偶发畸形致网关 502 `invalid call id`（chat_grammar）；③ 上游对（zcode 客户端/`hy4-preview`）偶发 11128 安全策略拦截。
3. **上游是否真有 hy4**：8/29–8/31 有 4 条 200 真实返回（含 stop/tool_calls/length），证明**Tencent 上游彼时确实提供 `hy4-preview`**。但当前 db 白名单已不包含它，故现在请求根本到不了上游。

## 5. 给 Phase B / Phase C 的线索（尚未执行）

- 复现：用 `Buddy` 键发 `{"model":"hy4-preview",...}` → 预期现网 400 `unknown_model`（与日志 200 矛盾，需确认线上 `models` 是否确为 `wb-m` 且 bind 是否真拦截）。
- 修复候选（按 spec Phase C）：
  1. **配置修复（最小）**：把 `hy4-preview` 加入 `models` 白名单（或加 `model_aliases` 指向真实模型）；先备份 db（含 -wal/-shm）。因上游已证可用，这是首选。
  2. **代码修复**：若 502 `invalid call id` 高频，检查 `chat_grammar.py:177` 校验是否对 `hy4-preview` 工具流过严；11128 的"未授权通道"语义不应走超长请求自愈。
  3. **清晰报错**：白名单拒绝时已是结构化 400，可接受；若决定不支持，应在 `/v1/models` 与文档明确。

（本阶段未做任何写操作，未触发 Phase B/C。）
