# Trae SOLO 通道使用说明（Buddy2API）

本文档针对本机这套实例（`C:\Usr\Code\etc\Buddy2api`，服务地址 `http://127.0.0.1:8787`），
说明 **Trae SOLO 通道**（`traesolo`）的账号接入、模型、客户端接入与排查方法。

> Trae SOLO 是 ByteDance TRAE 的 SOLO 模式（`www.trae.cn`），与 TraeWork 通道**相互独立**：
> - TraeWork 读本机 `%APPDATA%\TRAE SOLO CN\User\globalStorage\storage.json`；
> - Trae SOLO **不读任何本机目录**，走「Web 登录闭环」或「凭证 JSON 导入」，token 只存在数据库里（加密）。
> - 两者账号不通用、不混用；一把 Key 只绑一个通道。
>
> 协议实现参考开源项目 [trae2api-web](https://github.com/connectedGraph/trae2api-web)（Go），本通道为 Python 原生实现，
> 支持非流式 / 流式（真实逐 chunk SSE）/ tool_calls / 动态模型表 / 完整冷却状态机 / 配额 / 官方签到。

---

## 1. 前提检查（30 秒）

```powershell
# 1) 服务在跑吗？（应返回 JSON，channels.traesolo 存在且 loaded=true）
Invoke-RestMethod http://127.0.0.1:8787/health
```

- `traesolo.loaded = true`：通道已加载（v2.2.0 起默认启用）。
- `traesolo.accounts = 0`：还没导入账号，走第 2 节。
- 导入后点该账号的「测试」，返回一句话即说明上游链路通。

## 2. 账号接入（三选一）

### 2.1 Web 登录闭环（推荐）

网页管理页「账号」→ 下拉选 **Trae SOLO** → 点「**发起网页登录**」：

1. 浏览器新窗口打开 `https://www.trae.cn/authorization?...`，正常登录 TRAE 账号（账号密码/扫码）；
2. 登录成功后 TRAE 302 跳回 `http://127.0.0.1:8787/authorize?...`，服务端自动完成
   ExchangeToken（换 accessToken）+ GetUserInfo（补 uid/昵称）+ 入库加密；
3. 页面显示「登录成功 · 账号 xxx 已添加」。

**远程部署**（浏览器够不到服务的 `127.0.0.1` 回调）两种做法：

- 发起登录时管理 API 可传回调基地址：`POST /admin/traesolo/login/start` body `{"callback_base":"http://<可达地址>:8787"}`
  （或设环境变量 `CB_TRAESOLO_CALLBACK_BASE`），回调落到该地址；
- 或者跳回失败后，把**浏览器地址栏完整 URL** 粘到管理页「手动完成」
  （等价 `POST /admin/traesolo/login/complete`，body `{"callback":"<完整URL>"}`）。

### 2.2 凭证 JSON 导入

SOLO 的凭据是 JSON（trae2api-web 的 `auths/trae-<uid>.json` 或手动构造）：

```json
{
  "auth": {
    "accessToken": "Cloud-IDE-JWT…",
    "refreshToken": "…",
    "expiresAt": 1790000000000,
    "domain": "trae.cn",
    "apiHost": "https://api.trae.com.cn",
    "machineId": "32位hex",
    "deviceId": "32位hex"
  },
  "account": { "uid": "…", "enterpriseId": "…", "nickname": "…" }
}
```

也接受平铺字段（`accessToken`/`refreshToken`/`uid`/`expiresAt`…，嵌套/平铺自动识别）。
把文件放到 `CB_TRAESOLO_AUTH_DIR` 指定目录（默认不扫任何目录），管理页选 Trae SOLO →「重新检测」→「一键导入」。
同 uid 重复导入按更新处理，不产生重复账号。

### 2.3 管理 API 一览

| 端点 | 说明 |
|---|---|
| `POST /admin/traesolo/login/start` | 发起登录：`{login_url, pending_id, callback_url}` |
| `GET /admin/traesolo/login/result?pending_id=` | 轮询登录状态：`pending/success/failed` |
| `POST /admin/traesolo/login/cancel` | 取消登录会话 |
| `POST /admin/traesolo/login/complete` | 手动闭环：`{"callback":"<完整回调URL>"}` |
| `GET /authorize` | TRAE 登录跳回的回调地址（无需 admin 鉴权，仅本机/可达地址） |

登录会话 10 分钟过期；回调参数识别 `refreshToken` → `userJwt.RefreshToken` → `userJwt.Token` 三级回退。

## 3. 用哪把 Key

管理页「API Keys」里每把 Key 绑定一个通道，**不要混用**：

| Key 绑定通道 | 能访问的模型 |
|---|---|
| `traesolo` | 下文第 4 节 SOLO 模型表 + `auto` |

完整 Key 值在管理页「显示/复制」。下面示例用占位符 `sk-cb-SOLO_KEY`。

## 4. Trae SOLO 可用模型

- **动态模型表**：每次请求 best-effort 拉取 TRAE 的 `get_detail_param`（1 小时缓存、失败 5 分钟负缓存）。
  官方原始返回实测 **42 条**，经下面的非对话过滤后入缓存 **15 个可见对话模型**
  （含 `Doubao-Seed-Evolving`、`step-5-preview`、`glm-5.3`、`deepseek-v4.1-flash`、`kimi-k3`、`qwen3.8-max` 等）。
- **非对话模型全局过滤**：官方返回的 `config_info_list` 里混有内部项，解析/入缓存阶段即剔除：
  `is_invisible_to_user` 为真、`usage` 为 `custom_model`/`summary`、名字含 `subagent`/`sub_agent` 或等于 `summary`。
  实测 42 条 → **15 个可见对话模型**。被剔除的模型（含 `DeepSeek-V4-Pro`、`DeepSeek-V4-Flash`、`glm-5`、
  `glm-5-turbo`、`sagitta`、`aquila` 等官方标记不可见的项）**不再进入白名单候选，也不再可请求**。
- **静态回退**：上游拉不到时用内置 **9 个** `config_name` 兜底（只保留过滤后仍可见的模型，不补齐）。
- `auto` 别名落到 **`glm-5.2`**。
- **模型选择弹窗**：管理页「模型配置 → 各平台设置 → traesolo」点「刷新官方模型表」会**实时拉官方并弹出弹窗**，
  列出官方可见模型（勾选框 + 展示名 + 模型 ID + 官方倍率 + 上下文窗口），当前白名单已选项**预勾选**。
  保存**只写白名单 `models`** 并清理「目标已被剔除」的孤儿别名，思考档位 / 上下文限额不变。
  刷新失败（无可用账号 / 上游不可达）时弹窗报错且不修改任何配置。该弹窗**仅 traesolo** 有；
  密钥型通道的「刷新官方模型表」仍是只刷新不弹窗。
- 模型名**大小写不敏感**（`deepseek_v4_flash_official` / `DeepSeek-V4-Flash-Official` 都认），
  内部名后缀 `__dev`/`__max` 自动剥离。
- 官方 `get_detail_param` 的 `config_name` 可能是全小写（如 `deepseek-v4.1-flash`），而白名单/别名里常是
  混合大小写（如 `DeepSeek-V4.1-Flash`）。**模型命中与官方 rate / display_name 的 lookup 均大小写不敏感**
  （`model_rate` 与 `fetch_model_rates` 已与 `accepts_model` 对齐），因此「刷新官方模型表」后白名单内
  官方存在的模型能正确显示官方倍率与展示名。
- 列表外的名字 400。`/v1/models` 里 SOLO 模型带 `traesolo/` 前缀列出；不带前缀按 Key 绑定通道解析。
- 通道白名单/别名同样支持 `GET/PUT /admin/channels/traesolo/models`（整体替换，`null` 重置）。
- `POST /admin/channels/traesolo/models/refresh` 的返回新增 `official_models` 字段：完整官方可见模型列表
  （不受白名单限制），供上述弹窗使用；原 `model_details`（白名单内明细）语义不变。

> 注意：`glm-5.2` 在 WorkBuddy 和 Trae SOLO 两个通道都存在，不带前缀时按 Key 通道解析，
> 想明确指 SOLO 就用 `traesolo/glm-5.2`。

### 4.1 官方 `get_detail_param` 接口（模型表的唯一来源）

模型表的唯一来源就是这一个官方接口，动态刷新、静态兜底、弹窗候选全部由它派生：

```
POST {agent_host}/api/ide/v1/get_detail_param
```

| 项 | 值 |
|---|---|
| Host | `AGENT_HOST = https://trae-api-cn.mchost.guru`（可被 `channel_hosts` 白名单覆盖） |
| Path | `EP_MODELS = /api/ide/v1/get_detail_param`（`src/providers/traesolo/constants.py`） |
| Headers | `solo_headers(account, stream=False)`（`Cloud-IDE-JWT {access_token}` 等指纹头） |
| Body | `{"function":"solo_work_lite","config_names":null,"need_prompt":false,"current_config_info":null,"poly_prompt":true,"mode_type":null,"agent_type":null}` |

- 调用方：`fetch_model_details()`（`src/providers/traesolo/chat.py`）；成功缓存 1h、失败负缓存 5min。
- 可导入 Postman 的复刻集合：`traesolo_models_get_detail_param.postman_collection.json`（仓库根目录），
  用于把「Postman 原始返回」与「网关解析结果」对照，排查模型表是否失真。

**返回 `config_info_list[]` 的关键字段**（实测 42 条）：

| 字段 | 含义 | 对网关的作用 |
|---|---|---|
| `config_name` | 官方内部 id | 即白名单/请求用的模型 id |
| `usage` | `custom_model` / `summary` / 空 | **值为 `custom_model`/`summary` 时被过滤** |
| `is_invisible_to_user` | 是否对用户隐藏 | 为 `True`/`"true"` 时被过滤 |
| `display_config.display_name` | 官方展示名 | 弹窗/倍率展示 |
| `display_config.model_capability` | `chat_model` / `reasoning_model` / 空 | **仅能力标签，不是可用性判据**（见 4.4） |
| `display_config.fee_model_level` | 计费档位 | 展示 |
| `context_window_tokens.dev` | 上下文窗口 | 展示 |
| `display_contact_config` | JSON 串，含 `consumption_rate.data.rate` | 官方 credit 倍率来源 |
| `custom_models` | `provider//model` 路由列表 | 该 config 背后的第三方路由模板 |
| `model_detail_list` | `__dev` / `__max` 子配置（含 `model_name`、`max_tokens` 等） | 内部模式切换，不区分可用性 |

### 4.2 42 条实测分类

| 类别 | 数量 | 说明 |
|---|---|---|
| **可见对话模型（进白名单候选）** | **15** | `Doubao-Seed-Evolving`、`Doubao-Seed-2.1-Pro`、`Doubao-Seed-2.1-Turbo`、`step-5-preview`、`glm-5.3`、`glm-5.2`、`deepseek-v4.1-flash`、`DeepSeek-V4-Flash-Official`、`DeepSeek-V4-Pro-Official`、`kimi-k3`、`kimi-k2.7-code`、`kimi-k2.6`、`minimax-m3`、`qwen3.8-max`、`qwen-3.7-plus` |
| 官方标记不可见（`is_invisible_to_user=true`） | 12 | `seed-code-pro-0430`、`Doubao-Seed-2.0-Code`、`glm-5`、`glm-5-turbo`、`DeepSeek-V4-Flash`、`DeepSeek-V4-Pro`、`sagitta`、`aquila`、`file_search_agent`、`explore_sub_agent_v2`、`browser_use_subagent`、`computer_use_subagent` |
| `custom_model_*`（`usage=custom_model`） | 14 | 全部被 `is_selectable_model()` 过滤，见 4.3 |
| 其它（`summary` 等） | 1 | `usage=summary`，被过滤 |

**过滤规则**（`is_selectable_model()`，`chat.py`）三条件任一命中即剔除：`is_invisible_to_user` 为真、
`usage` 为 `custom_model`/`summary`、`config_name` 含 `subagent`/`sub_agent` 或等于 `summary`。
过滤发生在**解析/入缓存阶段**，被剔除项不进入白名单候选。

### 4.3 `custom_model_*` 实测可达性（直连上游，绕过网关）

用真实账号 token 直接打 `llm_utils_chat`（单字 prompt、非流式、请求间 ≥3s）实测 14 个 `custom_model_*`：

| 模型 | 占位名直发 | 路由名直发 | 结论 |
|---|---|---|---|
| `custom_model_gpt-5` | ✅ 200 | — | **上游可达**（后端实为 `gpt-5-2025-08-07`） |
| `custom_model_gpt-6` | ✅ 200 | — | **上游可达**（`extra_info.model` / `provider_model_name` 同样是 `gpt-5-2025-08-07`） |
| 其余 12 个（gemini/claude/kimi/deepseek_*/1M/1M_text/doubao_*/no-fc/placeholder） | ❌ `event:error code=4023` | ❌ `code=4001 param invalid` | **不可达** |

关键结论：

- **`custom_model_gpt-5`/`gpt-6` 上游确实能跑**，但它们的 `usage` 是 `custom_model`，
  会被 `is_selectable_model()` 过滤 —— 因此**不会进入网关白名单，网关侧选不到**
  （直接发 `model=custom_model_gpt-5` 会被 bind 层判为 `400 unknown_model`）。
  想启用必须显式写进 `traesolo.models` 白名单。
- 其余 12 个无论用占位名还是其 `custom_models` 里的 `provider//model` 路由名都调不通，
  说明这些 `custom_models` 只是**未激活的路由模板**，并非当前账号已开通的通道。

### 4.4 不可达项的共性（含一条被证伪的推断）

- ❌ **不是** `model_capability`：可达的 gpt-5/6 是 `chat_model`，但**15 个可见常规模型全是
  `reasoning_model`**（且它们经网关可正常调用），所以 `chat_model`/`reasoning_model` 只是能力标签，
  不能当可用性判据。
- ❌ **不是** `is_invisible_to_user`：失败组该项均为 `false`（即"对用户可见"）。
- ❌ **不是** `__dev`/`__max` 子配置：成功与失败组都有。
- ✅ **真正区别在 `custom_models` 路由是否已被上游激活**：常规模型（如 `glm-5.2`、
  `DeepSeek-V4-Flash-Official`）是官方已开通的独立 config，直接可用；失败的那些 custom 路由挂在
  第三方供应商（`gemini//`、`anthropic//`、`Kimi-CN//`、`volcengine-plan//` 等）上但未激活。
  `custom_model_gpt-5/6` 是特例 —— 它们挂在 openai 官方直连路由（`openai//gpt-5` 等）上且已激活。

## 5. 客户端接入

### 5.1 通用 OpenAI 兼容客户端

- **Base URL：`http://127.0.0.1:8787/v1`**（拼重 `/v1/v1` 会 404，见排查表）
- **API Key：** Trae SOLO 那把
- **模型：** `traesolo/glm-5.2` 或 `glm-5.2`（Key 绑 SOLO 时）或 `auto`
- **Stream：** 可开可关。**SOLO 上游本身是 SSE**，网关做真实逐 chunk 转换
  （含 `reasoning_content` 思考增量、`tool_calls` 增量合并、末尾 `usage` + `[DONE]`）。
- **Tools：** 支持 `tools` / `tool_choice`（OpenAI `function` ↔ SOLO `function_call` 自动互转）。

### 5.2 curl 最小验证

```bash
curl http://127.0.0.1:8787/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -H "Authorization: Bearer sk-cb-SOLO_KEY" ^
  -d "{\"model\":\"traesolo/glm-5.2\",\"messages\":[{\"role\":\"user\",\"content\":\"请回复：pong\"}]}"
```

### 5.3 Codex（wire_api = responses）

`/v1/responses` 同样按 Key 绑定通道分发到 SOLO（instructions / tools / reasoning 完整 payload 均可）：

```toml
[model_providers.b2api_traesolo]
name = "Buddy2API Trae SOLO"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "B2API_SOLO_KEY"

[profiles.traesolo]
model = "traesolo/glm-5.2"
model_provider = "b2api_traesolo"
```

## 6. 配额与签到

- **配额**：`GET /admin/accounts/{id}/resources` 走 SOLO `ide_user_ent_usage`（各权益包
  `credits_limit - credits_amount` 求和，单位 credit）。
- **签到**：`GET/POST /admin/accounts/{id}/checkin` 走 SOLO `checkin_credits/status|claim`；
  管理页「一键领取」对 SOLO 账号同样可用（今日已领会提示，不报错）。

## 7. 账号健康与冷却

与 Go 版 trae2api-web 完全对齐的账号状态机（多账号时自动换号，单请求最多换 3 次）：

| 上游信号 | 处理 |
|---|---|
| SSE `error` / body `code:1005`（plan 权益不足） | 该账号冷却 **12 小时** |
| HTTP 429 / 404 | 软冷却 **60 秒** |
| 连续 3 次错误 | 冷却 **10 分钟**（计数清零重来） |
| HTTP 401 / token 失效 | 会话死亡：尝试 refresh，失败则置 `expired`（可手动启用重试） |
| token 距过期 < 24h | 请求前**静默预刷新**（`ExchangeToken`），换号时同步更新 refresh_token |

冷却状态在管理页账号行可见（remaining/原因）。

## 8. 排查：请求"没反应 / 报错"怎么办

**第一步：看日志表里有没有新记录**（网页管理页「日志」或 `codebuddy_gateway.db` 的 `logs` 表）。

| 现象 | 原因 | 处理 |
|---|---|---|
| 404 | URL 拼成 `.../v1/v1/...`，或端口不对 | 核对 Base URL |
| 401 `Invalid API key` | Key 不对/已禁用 | 换正确的 SOLO Key |
| 400 `unknown_model` | 模型名不在当前生效列表，或属于别的通道 | 用列表内名字（`/v1/models` 查 `traesolo/` 前缀项） |
| 403 `key_channel_mismatch` | 模型前缀通道 ≠ Key 绑定通道 | 去掉前缀，或换 SOLO 通道的 Key |
| 503 `No available accounts` | 无 active SOLO 账号 | 第 2 节导入账号 |
| 503 `plan limit` / 账号冷却 12h | SOLO 订阅 plan 权益用尽（上游 code 1005） | 等冷却到期或换账号；这不是网关 bug |
| 502 / 上游 5xx | TRAE 上游故障/限流 | 重试；连续 3 次会自动冷却该账号 |
| 登录页转完没跳回 | 远程够不到回调地址 | 2.1 节：改回调基地址或手动闭环 |
| token 频繁失效 | refresh_token 过期（约 30 天） | 重新走 Web 登录导入 |

## 9. 验证记录（2026-08-27，真实账号 E2E）

在隔离实例（独立 DB，端口 8788）上用真实 TRAE SOLO 账号实测，全部通过：

| 请求 | 结果 |
|---|---|
| Web 登录闭环（真实浏览器 → `/authorize` 回调） | 200，账号自动入库（uid/昵称/token 完整） |
| `POST /v1/chat/completions` + `glm-5.2`（非流式） | 200，~5.7s，usage 从 SSE `token_usage` 正确回填 |
| `POST /v1/chat/completions` + `glm-5.2`（流式） | 200，逐 chunk SSE + `usage` + `[DONE]` |
| tool_calls 请求（`get_weather` 函数） | 200，`function_call`→`function` 互转正确，参数 JSON 合法 |
| 动态模型拉取（`get_detail_param`） | 200，官方 42 条经非对话过滤后 15 个入缓存（静态表 9 个兜底） |
| `GET /admin/accounts/1/resources`（配额） | 200，credit 单位，余额 4825 |
| `GET /admin/accounts/1/checkin`（签到） | 200，`already_claimed=true`、credit=200（当日已领） |
| `POST /admin/accounts/1/test`（测试对话） | 200，返回上游回答 |

单元测试：`tests/test_traesolo.py`（50 个用例，全部 mock HTTP），全量 272 用例通过。

### 9.1 验证记录（模型表实测，真实 prod 账号直连上游）

用 prod 实例 DB 里的真实 traesolo 账号 token，绕过网关直接调官方接口（详见 4.1/4.3）：

| 项目 | 结果 |
|---|---|
| `get_detail_param`（模型表） | 200，`config_info_list` 共 **42 条** |
| `custom_model_gpt-5` 对话 | 200，`extra_info.model` / `timing_cost.provider_model_name` = `gpt-5-2025-08-07` |
| `custom_model_gpt-6` 对话 | 200，后端同样为 `gpt-5-2025-08-07`（未真正切到 gpt-6） |
| 其余 12 个 `custom_model_*` | 占位名 → `code=4023`；路由名 → `code=4001 param invalid`，均不可达 |

探针脚本与存档：`.tmp/call_get_detail_param.py`、`.tmp/probe_custom_models2.py`、
`.tmp/probe_routes.py`、`.tmp/get_detail_param_full.json`、`.tmp/custom_models_reachability2.json`、
`.tmp/route_probe.json`（均为临时产物，可复现）。

> 风控注意：验证一律单字 prompt（`hi`）、非流式、请求间 ≥3s、无并发无重试。
> 这类直连上游的探测**会真实消耗账号积分**，勿对高端模型（gpt-5/6）做批量或长输出试探。

## 10. 环境变量 / 启动参数

| 变量 | 说明 |
|---|---|
| `CB_GATEWAY_PROVIDERS` | 默认已含 `traesolo`；只留 WorkBuddy 时设 `workbuddy` |
| `CB_TRAESOLO_CALLBACK_BASE` | 登录回调基地址（远程部署时指向可达地址；默认取请求自身地址） |
| `CB_TRAESOLO_AUTH_DIR` | 凭证 JSON 扫描目录（可选；默认不扫目录） |

改代码需重启服务；模型白名单/别名/账号/Key 均运行时生效，不需要重启。

## 11. 参见

- `docs/credit-and-token-tracking.md`：token 与 credit 统计的来龙去脉（为什么 SOLO / TraeWork 默认
  没有 credit、`credit_rate` 换算率怎么用、估算值和真实值的差距在哪）。
- `traesolo_models_get_detail_param.postman_collection.json`（仓库根目录）：官方 `get_detail_param`
  的可导入 Postman 复刻集合，用于对照官方原始返回与网关解析结果（见 4.1）。
- `docs/traework-usage.md`：TraeWork 通道说明（与 SOLO 相互独立，勿混用账号/Key）。

### 本文章节索引

| 想了解 | 看 |
|---|---|
| 官方模型表接口与返回字段 | 4.1 |
| 官方 42 条怎么分类、哪些被过滤 | 4.2 |
| `custom_model_*` 到底能不能用（实测） | 4.3 |
| 可用性的判据是什么（含一条被证伪的推断） | 4.4 |
| 刷新按钮 / 模型选择弹窗怎么用 | 第 4 节开头 |
| 模型表实测与对话验证记录 | 9、9.1 |
| 请求没反应 / 报错排查 | 8 |
