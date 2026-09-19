# TraeWork code 模式：实测证据留档（spec 41 §3 附证）

- 采集时间：2026-09-19
- 采集对象：本机官方客户端 `C:\Usr\Compiler\TRAE SOLO CN`（TraeWork CN，runMode=solo-lite，v1.107.1）
- 采集方式：静态 bundle 逆向 + 客户端本地数据 + 客户端真实运行日志 + **真实账号端到端实测**
- 结论：`mode="code"` 对应 `agent_id`/`agent_type` = **`solo_agent_lite`**（agent_name "SOLO Code"）；
  **真实账号实测已通过**（上游直连 + 网关 `/v1/chat/completions` + `/v1/responses`）

> 抓包（MITM）在此环境不可用（未安装 mitmproxy），故改用客户端自身数据 + 真实账号直测。
> 其中「运行日志里的真实会话消息」等价于"已发生的真实请求结果"，而 §证据 4 是完整活体实测。

---

## 证据 1：协议契约表（静态，`@byted-icube/solo-lite`）

文件：`resources\app\node_modules\@byted-icube\solo-lite\dist\410.66aabbc9.mjs`

```js
"chat.createSession": {method:"POST", path:"/api/remote/v1/chat_sessions", transport:"api"}
"chat.deleteSession": {method:"DELETE", path:"/api/remote/v1/chat_sessions/:chat_session_id"}
"chat.getMessages":   {method:"GET",  path:"/api/remote/v1/chat_sessions/:chat_session_id/messages"}
```

与网关 `src/providers/traework/constants.py`（`SESSIONS_PATH`）一致。

同文件 agent 清单与 **mode → agent 权威映射**：

```js
// agent 表（模块 69663）
{id:"solo_work_lite",   name:"TRAE Work", type:SoloWorkLite}
{id:"solo_agent_lite",  name:"TRAE Code", type:SoloAgentLite}
{id:"solo_agent_remote",name:"TRAE Code", type:SoloAgentRemote}
{id:"solo_work_remote", name:"TRAE Work", type:SoloWorkRemote}
{id:"solo_design_lite", name:"TRAE Design", ...}

// 映射表
o = { [Code]:{[Local]:SoloAgentLite,[Remote]:SoloAgentRemote},
      [Work]:{[Local]:SoloWorkLite, [Remote]:SoloWorkRemote},
      [Design]:{[Local]:SoloDesignLite,[Remote]:SoloDesignRemote} }
s = { [Work]:SoloWorkLite, [Code]:SoloAgentLite, [Design]:SoloDesignLite }  // 默认回退 Work
```

UI 侧多处同一三元式（模块 177 / 976 / 771 等）：

```js
mode === InputMode.Code ? AgentType.SoloAgentLite : AgentType.SoloWorkLite
```

**发消息体字段**（同文件，`buildSendMessageRequest`）：

```js
{ chat_session_id, content:[], query:JSON.stringify(...), model_name,
  agent_type: e.agentType ?? "", agent_id: e.agentId, ... }
```

**建会话体字段**（`chatApi.createSession(en({...}))`）：

```js
{ source, target, mode, local_project_id, environment_id, initial_message, env,
  remote_project_id, project_name, auto_create_project, parent_session_id,
  origin, is_worktree, worktree_base_branch, session_type }
```

---

## 证据 2：客户端本地状态（vscdb）

文件：`%APPDATA%\TRAE SOLO CN\User\globalStorage\state.vscdb`

- 键 `solo-lite-mode-state-map-<uid>` 含真实 code 会话：
  `"code": { "session": { "mode":"code", "origin":"lite", "session_type":"side_chat", "env":"local" } }`
- 键 `<uid>:AI.agent.model.session_selected_model`：
  code 会话记为 `solo_agent_lite`，work 会话记为 `solo_work_lite`。

---

## 证据 3（最强）：客户端真实运行日志里的会话消息

文件：`%APPDATA%\TRAE SOLO CN\logs\<ts>\window1\renderer.log`

**code 模式真实会话**（2026-09-18T16:09 段）：

```
"message_type":"task", ... "agent_type":"solo_agent_lite","agent_id":"solo_agent_lite",
"agent_name":"SOLO Code", ...
"model_info":{"config_name":"DeepSeek-V4-Flash-Official", ...}
```

**work 模式真实会话**：

```
"message_type":"task", ... "agent_type":"solo_work_lite","agent_id":"solo_work_lite",
"agent_name":"SOLO MTC", ...
```

**远端（remote）会话**（存在第四种组合，本次未纳入实现）：

```
"agent_type":"solo_work_remote","agent_id":"solo_work_remote","agent_name":"SOLO MTC"
```

**mode 作为真实传输值**（客户端自身按 mode 初始化服务）：

```
[ProjectService]    Projects initialized for mode: {"mode":"code","count":2}
[ProjectService]    Projects initialized for mode: {"mode":"work","count":1}
[RepoGroupService]  Repo groups initialized for mode: {"mode":"code","count":1}
[RepoGroupService]  Repo groups initialized for mode: {"mode":"work","count":1}
[RepoGroupService]  Repo groups initialized for mode: {"mode":"design","count":1}
[UserPreferencePersistenceService] Mode preferences restored for: work
```

→ 明确印证三模式枚举 `work` / `code` / `design`，且 **code 会话的 agent 就是 `solo_agent_lite`**。

---

## 证据 4：真实账号实测（2026-09-19，客户端重新登录后）

首次尝试时凭据已失效：JWT `exp`=2026-10-02（未到期），但上游**所有端点**返回 `code:1001`：
`chat_sessions` 401、`models` 401、UG checkin `code:1001 enable:false`，刷新（ExchangeToken）401；
根因是客户端当时登出（`storage.json` 无主凭据键 `iCubeAuthInfo://icube.cloudide`）。

**用户重新登录后实测，全部通过：**

### 4.1 上游直连（`.tmp/probe/live_verify_modes.py`）

按网关实际会发的 body（`mode` + `agent_id`/`agent_type`）直连
`POST /api/remote/v1/chat_sessions` 与 `{sid}/messages`：

| mode | agent | 建会话 | 发消息 | 回答 |
|---|---|---|---|---|
| `work` | `solo_work_lite` | 200 / code 0 | 200 / code 0 | `pong` |
| `code` | `solo_agent_lite` | 200 / code 0 | 200 / code 0 | `pong` |

两模式 SSE 事件集完全一致：
`done, metadata, model_config, plan_item, platform_timing, session_icon_message,
session_title_message, status_changed, timing_events, token_usage`

→ 印证 `extract_assistant_text` 对 code 模式同样适用。

### 4.2 网关端到端（`.tmp/probe/e2e_gateway_modes.py` / `e2e_responses_code.py`）

通过网关 HTTP 接口（真实账号 + traework Key）实测：

| 路径 | mode | 结果 |
|---|---|---|
| `POST /v1/chat/completions`（非流式） | `work` | 200，`pong` |
| `POST /v1/chat/completions`（流式） | `work` | 200，SSE 正常，含 `[DONE]` |
| `POST /v1/chat/completions`（非流式） | `code` | 200，`pong` |
| `POST /v1/chat/completions`（流式） | `code` | 200，SSE 正常，含 `[DONE]` |
| `POST /v1/responses`（非流式，Codex） | `code` | 200，`status=completed`，`pong` |
| `POST /v1/responses`（流式，Codex） | `code` | 200，`response.completed` |

实测后已把 `traework.mode` 重置回默认（未设置 ⇒ `work`）。

---

## 附：网关实现对表

| mode | 网关发出的 `mode` | 网关发出的 `agent_id`/`agent_type` | 官方 agent_name |
|---|---|---|---|
| 未配置 / `work` | `work` | `solo_work_lite` | SOLO MTC（TRAE Work） |
| `code` | `code` | `solo_agent_lite` | **SOLO Code**（TRAE Code） |
| （未实现）`design` | — | `solo_design_lite` | TRAE Design |

实现见 `src/providers/traework/constants.py::agent_id_for_mode` 与
`src/providers/traework/chat.py::_turn`。
