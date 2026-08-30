# TraeWork 通道使用说明（Buddy2API）

本文档针对本机这套实例（`C:\Usr\Code\etc\Buddy2api`，服务地址 `http://127.0.0.1:8787`），
说明如何用 **TraeWork 通道的 Key** 访问 TraeWork 大模型，以及排查"连不上"的方法。

> 2026-08-27 更新说明：本实例代码已修复两个 TraeWork 相关 bug，并新增每通道模型配置：
> 1. 回答提取：原来会把模型"思考过程"当回答返回，现在正确取最终答案（`providers/traework/chat.py`）；
> 2. `/v1/responses` 通道分发：原来该端点永远打向 WorkBuddy 后端，现在按 Key 绑定的通道分发到对应 provider（`responses.py`、`server.py`）；
> 3. 每通道模型列表 / 别名可配置：`GET/PUT /admin/channels/{channel}/models`（见第 4 节）。
>
> **改完代码必须重启服务才生效。** 2026-08-27 16:42 重启后的实例已实测通过（见文末"验证记录"）。

---

## 1. 前提检查（30 秒）

```powershell
# 1) 服务在跑吗？（应返回 JSON，channels.traework.active 应为 1）
Invoke-RestMethod http://127.0.0.1:8787/health
```

- `traework.accounts = 1` 且 `active = 1`：账号已导入（本机账号：`lannntuuu`，token 到 2026-09-10）。
- 如果是 0：TRAE SOLO CN 没登录，或登录目录不对（`CB_TRAEWORK_AUTH_DIR`）。
  网页管理页 →「账号」→ 下拉选 **TraeWork** →「重新检测」→「一键导入本机登录」。
- 导入后点该账号的「测试」，返回一句话即说明上游链路通。

## 2. 用哪把 Key

管理页「API Keys」里每把 Key 绑定一个通道，**不要混用**：

| Key（本机） | 绑定通道 | 能访问的模型 |
|---|---|---|
| `Buddy` | workbuddy | WorkBuddy 动态模型列表（当前账号在用 `hy3` 等） |
| `Trae` | traework | 下表 8 个 TraeWork 模型 + `auto`（可自定义，见第 4 节） |

完整 Key 值在管理页「显示/复制」。下面示例用占位符 `sk-cb-TRAE_KEY`。

## 3. TraeWork 可用模型（区分大小写！）

| 模型名 | 备注 |
|---|---|
| `auto` | 别名，实际走 `qwen-3.7-plus` |
| `qwen-3.7-plus` | auto 的目标模型 |
| `qwen-3.5` | |
| `Doubao-Seed-2.1-Turbo` | 注意大小写 |
| `Doubao-Seed-2.0-Code` | |
| `DeepSeek-V4-Flash-Official` | |
| `glm-5` | **不是** `glm-5.2`（那是 WorkBuddy 的） |
| `glm-5.1` | |
| `kimi-k2.5` | |

写法两种都行（Trae Key）：`traework/qwen-3.7-plus`（带前缀）或 `qwen-3.7-plus`（不带前缀，走 Key 绑定通道）。
**列表外的任何名字都会 400**，且 400 不写请求日志。上表是内置默认，可自定义（第 4 节）。

## 4. 自定义模型列表（按通道）

四个通道（`workbuddy` / `qclaw` / `qwenwork` / `traework`）的模型列表和别名都可以用管理 API
配置，**改完立即生效，不需要重启**；不配置就用内置默认。

也可以在网页管理页操作：左侧导航「模型配置」。页面两个区块：「统一模型」（跨平台翻译宽表，见 4.3）
和「各平台设置」（可切换平台的列表：每平台模型白名单 + 平台别名，保存 / 重置默认）。

### 4.1 查看当前生效配置

```bash
curl -H "Authorization: Bearer <admin-token>" http://127.0.0.1:8787/admin/channels/traework/models
```

```json
{
  "channel": "traework",
  "models": ["qwen-3.7-plus", "qwen-3.5", "..."],
  "aliases": {"auto": "qwen-3.7-plus"},
  "defaults": { "models": ["..."], "aliases": {"...": "..."} },
  "customized": { "models": false, "aliases": false }
}
```

- `models` / `aliases`：当前生效值（自定义优先，否则内置默认）；
- `defaults`：内置默认，方便对照；`customized`：是否设置了自定义。

### 4.2 修改 / 重置

```bash
# 修改（models 是整体替换，不是增量合并）
curl -X PUT -H "Authorization: Bearer <admin-token>" -H "Content-Type: application/json" \
  http://127.0.0.1:8787/admin/channels/traework/models \
  -d "{\"models\":[\"qwen-3.7-plus\",\"glm-5\"],\"aliases\":{\"auto\":\"qwen-3.7-plus\"}}"

# 重置回内置默认（传 null）
curl -X PUT -H "Authorization: Bearer <admin-token>" -H "Content-Type: application/json" \
  http://127.0.0.1:8787/admin/channels/traework/models \
  -d "{\"models\":null,\"aliases\":null}"
```

规则：

- `models`：非空数组，元素为模型 id 字符串（也接受 `{"id": "..."}` 对象）；`null` = 重置为默认。
- `aliases`：非空对象，`别名 -> 模型id`，键值必须是非空字符串；`null` = 重置为默认。
- 一次请求至少传一个字段，两项可以只传其中一项。
- 修改后 `/v1/models`、路由绑定、请求转发立即按新列表生效。
- **自定义列表是白名单**：不在列表里的模型对该通道 400（例外：QClaw 始终接受 `pool-*` 前缀）。
- `workbuddy` 通道兼容历史配置键（`models` / `model_aliases`），旧配置继续有效；
  其它通道存 `traework.models` / `traework.aliases` 这样的统一键（settings 表）。
- 别名是"通道内"的：`traework` 的 `auto` 只映射到 traework 列表里的模型。

### 4.3 统一模型（跨平台翻译层）

同一个模型在不同平台名字不一样（如 TraeWork 的 `DeepSeek-V4-Flash-Official`
对应 WorkBuddy 的 `deepseek-v4-flash`）时，可以定义一个"统一模型"：客户端只请求统一名
（以 WorkBuddy 命名为准），网关自动按 Key 绑定平台翻译成内部名。

```bash
# 查看
curl -H "Authorization: Bearer <admin-token>" http://127.0.0.1:8787/admin/unified-models

# 修改（整体替换整张表；models 传 [] = 清空）
curl -X PUT -H "Authorization: Bearer <admin-token>" -H "Content-Type: application/json" \
  http://127.0.0.1:8787/admin/unified-models \
  -d '{"models":[{"name":"deepseek-v4-flash","mappings":{"traework":"DeepSeek-V4-Flash-Official","workbuddy":"deepseek-v4-flash"}}]}'
```

规则：

- 统一名唯一；`mappings` = `通道 -> 内部模型名`，至少一个通道。
- **纯翻译层**：翻译出内部名后照常走该通道白名单校验——
  内部名不在通道白名单里照样 400（统一模型不会自动进白名单）。
- 某通道没有该统一名的映射时，该通道请求它 = 400（未知模型）。
- 保存后 `/v1/models` 会列出统一名（按映射通道分别列出），路由立即生效。
- 网页管理页：「模型配置」页的「统一模型」宽表；格子填该平台内部名，留空 = 该平台没有；
  红框 = 内部名不在该平台当前白名单内（提醒你会被 400）。

## 5. 客户端接入

### 5.1 通用 OpenAI 兼容客户端（Cherry Studio / NextChat / OpenCode / 自研代码）

- **Base URL：`http://127.0.0.1:8787/v1`**
  - 客户端会自动拼 `/chat/completions`，最终请求 `http://127.0.0.1:8787/v1/chat/completions`。
  - ⚠️ 常见坑：如果客户端拼的是 `/v1/chat/completions`，Base URL 里**不要**再带 `/v1`，
    否则变成 `http://127.0.0.1:8787/v1/v1/chat/completions` → 404。
    判断方法：请求 404 且日志表无新记录，基本就是 URL 拼重了。
- **API Key：** Trae 那把。
- **模型：** 填 `traework/qwen-3.7-plus` 或 `qwen-3.7-plus` 或 `auto`。
- **Stream：** 可开可关。traework 上游是 agent 式接口，网关不做逐 token 流式；
  但**流式请求会立即返回首包**，并在上游 agent 产生规划/思考文本时**提前分段转发**，
  最后才到达完整回答——客户端不用干等整轮跑完才有第一个字节（实测首包 ~0.1s，
  思考文本 ~6-10s 起，完整回答 ~12-16s）。非流式则等整轮结束后一次性返回。

### 5.2 curl 最小验证（建议先跑这个）

```bash
curl http://127.0.0.1:8787/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -H "Authorization: Bearer sk-cb-TRAE_KEY" ^
  -d "{\"model\":\"traework/qwen-3.7-plus\",\"messages\":[{\"role\":\"user\",\"content\":\"请回复：pong\"}]}"
```

成功返回（200）：

```json
{"id":"traework-...","object":"chat.completion","model":"traework/qwen-3.7-plus",
 "choices":[{"index":0,"message":{"role":"assistant","content":"pong"},"finish_reason":"stop"}],
 "usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}}
```

### 5.3 Codex（wire_api = responses）

Codex 走 `/v1/responses` 端点（2026.2 起强制 responses）。在 `~/.codex/config.toml` 里加：

```toml
[model_providers.b2api_traework]
name = "Buddy2API TraeWork"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "B2API_TRAE_KEY"          # 从这个环境变量读 Key

[profiles.traework]
model = "traework/qwen-3.7-plus"
model_provider = "b2api_traework"
```

然后设置环境变量并指定 profile 启动：

```powershell
$env:B2API_TRAE_KEY = "sk-cb-TRAE_KEY"
codex --profile traework
```

`/v1/responses` 现在会按 Key 绑定通道分发到 traework provider，
非流式和流式都已实测（支持 instructions / tools / reasoning 等完整 Codex payload）。
给 Codex 建 Key 时类型选 **Codex**（会按 Codex 特征 prompt 做清洗）。

### 5.4 接口一览

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat 格式，所有通道 |
| `POST /v1/responses` | OpenAI Responses 格式（Codex），所有通道 |
| `GET /v1/models` | 模型列表（带 `channel` 字段），需要客户端 Key |
| `GET /admin/channels/{channel}/models` | 查看通道模型/别名配置（admin token） |
| `PUT /admin/channels/{channel}/models` | 修改/重置通道模型/别名配置（admin token） |
| `GET /health` | 服务/账号健康检查，无需 Key |

## 6. 排查：请求"没反应 / 报错"怎么办

**第一步：看日志表里有没有新记录。**

```powershell
# 日志保留在 codebuddy_gateway.db 的 logs 表；也可直接看网页管理页「日志」
python -c "import sqlite3,time; db=sqlite3.connect(r'C:\Usr\Code\etc\Buddy2api\codebuddy_gateway.db'); db.row_factory=sqlite3.Row; [print(dict(r)) for r in db.execute('SELECT id,api_key_name,provider,model,status_code,error_msg,created_at FROM logs ORDER BY id DESC LIMIT 5')]"
```

- **没有新记录** → 请求根本没到网关，或死在路由层（400/403 不记日志）。
  依次检查：服务在跑（`/health`）、端口 8787、URL 有没有 `/v1` 拼重、Key 是否 Trae 那把、
  模型名是否在当前生效列表里（`GET /admin/channels/traework/models` 或 `GET /v1/models`）。
- **有记录** → 看 `status_code` 和 `error_msg`，对照下表。

| 现象 | 原因 | 处理 |
|---|---|---|
| 404 | URL 拼成 `.../v1/v1/...`，或端口不对 | 核对 Base URL（见 5.1 坑） |
| 401 `Invalid API key` | Key 不对/已禁用 | 换正确的 Trae Key |
| 400 `unknown_model` | 模型名不在当前生效列表（大小写敏感），或模型属于别的通道 | 用列表内名字；自定义列表见第 4 节 |
| 403 `key_channel_mismatch` | 模型前缀的通道 ≠ Key 绑定的通道 | 去掉前缀，或换对应通道的 Key |
| 429 `daily limit` | Key 的每日请求上限 | 调大上限或换 Key |
| 503 `channel_unavailable` / `No available accounts` | traework 无 active 账号，或 token 过期且刷新失败 | 管理页导入/重新导入账号，点「测试」；确认 TRAE SOLO CN 已登录 |
| 502 / 上游报错（含 `code:6004` 频率限制） | 上游（WorkBuddy/Trae）限流 | WorkBuddy 限流会提示恢复时间；期间把客户端切到 Trae Key + traework 模型 |
| 200 但回答是"复述你问题"的思考文本 | 旧代码提取 bug | 重启服务加载新代码 |

**第二步：curl 通但客户端不通** → 客户端配置问题：对比 5.1/5.3 检查
Base URL、Key、模型名、以及客户端用的是 chat 接口还是 responses 接口。

## 7. 重启服务（加载新代码）

```powershell
# 停掉当前实例
Get-NetTCPConnection -LocalPort 8787 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }

# 启动（在项目目录；本机用 conda 环境或 .venv，start.bat 会自动处理）
cd C:\Usr\Code\etc\Buddy2api
python server.py --port 8787
```

启动成功标志：`/health` 有响应，控制台打印「账号: N 个」「通道: workbuddy,qclaw,qwenwork,traework」。
注意：重启会重新生成 admin token（除非设置了 `CB_GATEWAY_ADMIN_TOKEN`）。
（模型列表/别名是运行时生效的，改配置不需要重启；重启只用于加载代码改动。）

## 8. 验证记录（2026-08-27，当前实例）

17:04 对运行中的服务（进程 16:42:30 启动）实测，全部 200 且回答为 `pong`：

| 请求 | 结果 |
|---|---|
| `POST /v1/chat/completions` + `traework/qwen-3.7-plus`（非流式） | 200，content=`pong` |
| `POST /v1/chat/completions` + `qwen-3.7-plus`（不带前缀） | 200，content=`pong` |
| `POST /v1/chat/completions` + `traework/qwen-3.7-plus`（流式） | 200，SSE 正常 |
| `POST /v1/responses` + `traework/qwen-3.7-plus`（非流式） | 200，`status=completed`，text=`pong` |
| `POST /v1/responses` + 流式 | 200，`response.completed`，text=`pong` |
| `POST /v1/responses` + Codex 完整 payload（instructions+tools+reasoning，流式） | 200，`response.completed` |
