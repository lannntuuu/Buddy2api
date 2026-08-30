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

- **动态模型表**：每次请求 best-effort 拉取 TRAE 的 `get_detail_param`（1 小时缓存、失败 5 分钟负缓存），
  实测当前账号约 **38 个**模型（含 `Doubao-Seed-Evolving`、`glm-5.3`、`kimi-k3`、`qwen3.8-max` 等新模型）。
- **静态回退**：上游拉不到时用内置 32 个 `config_name` 兜底。
- `auto` 别名落到 **`glm-5.2`**。
- 模型名**大小写不敏感**（`deepseek_v4_flash_official` / `DeepSeek-V4-Flash-Official` 都认），
  内部名后缀 `__dev`/`__max` 自动剥离。
- 列表外的名字 400。`/v1/models` 里 SOLO 模型带 `traesolo/` 前缀列出；不带前缀按 Key 绑定通道解析。
- 通道白名单/别名同样支持 `GET/PUT /admin/channels/traesolo/models`（整体替换，`null` 重置）。

> 注意：`glm-5.2` 在 WorkBuddy 和 Trae SOLO 两个通道都存在，不带前缀时按 Key 通道解析，
> 想明确指 SOLO 就用 `traesolo/glm-5.2`。

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
| 动态模型拉取（`get_detail_param`） | 200，38 个模型入缓存（静态表 32 个兜底） |
| `GET /admin/accounts/1/resources`（配额） | 200，credit 单位，余额 4825 |
| `GET /admin/accounts/1/checkin`（签到） | 200，`already_claimed=true`、credit=200（当日已领） |
| `POST /admin/accounts/1/test`（测试对话） | 200，返回上游回答 |

单元测试：`tests/test_traesolo.py`（50 个用例，全部 mock HTTP），全量 272 用例通过。

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
